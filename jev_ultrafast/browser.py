"""Browser Use observes structure; Browser Harness validates and executes actions."""

import hashlib
import json
import os
import sys
import time

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from .page_reader import BrowserUsePageReader


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        # Tabs this run opened and then moved on from; closed on teardown.
        self.extra_targets: set[str] = set()
        self.reader = None
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        self.frame_id = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        try:
            self.reader = BrowserUsePageReader(os.getenv("BU_CDP_URL", ""), self.target)
        except Exception:
            self.close()
            raise

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        self.frame_id = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        info = self.reader.observe(self.frame_id)
        info["fingerprint"] = fingerprint(info)
        if screenshot:
            # Screenshots are only for the local UI; all page semantics come from Browser Use.
            info["screenshot"] = self.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        return info

    def _target_infos(self):
        """Page targets in the shared Chrome, keyed by id, with opener info."""
        infos = cdp("Target.getTargets").get("targetInfos", [])
        return {t["targetId"]: t for t in infos if t["type"] == "page"}

    def follow_new_tab(self, known_targets):
        """Switch observation and execution to a tab opened by the last action.

        Browser Use switches focus to a new tab automatically after a click.
        Browser Harness owns execution here, so both halves move together: the
        reader's observation target and the CDP session used for input.
        """
        new_targets = [t for t in self.reader.page_target_ids() if t not in known_targets]
        if not new_targets:
            return None
        # Only follow a tab this run actually opened. The daemon shares the
        # user's Chrome, so a "new" target may equally be one of the user's own
        # tabs (e.g. a chat app spawning a page); switching to it would read and
        # act on the wrong page. Pick the new target whose opener is one of our
        # own targets; ignore every foreign tab.
        own = {self.target, *self.extra_targets}
        target = None
        for t in new_targets:
            opener = self._target_infos().get(t, {}).get("openerId")
            if opener in own:
                target = t
                break
        if target is None:
            return None
        try:
            session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        except Exception as error:
            print(f"Could not attach to the new tab: {error}", file=sys.stderr, flush=True)
            return None
        self.extra_targets.add(self.target)
        self.target, self.session = target, session
        self.reader.focus(target)
        try:
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:
            # Metrics are a convenience; the new tab is already usable without them.
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") in {"interactive", "complete"}:
                    break
            except StalePage:
                pass
            time.sleep(0.05)
        self.frame_id = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        return target

    def page_targets(self):
        return set(self.reader.page_target_ids())

    def fresh(self, page, action=None):
        self.frame_id = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        current = self.reader.observe(self.frame_id)
        if action is not None and action["kind"] in {"click", "fill", "select"}:
            node = action.get("backend_node_id")
            if type(node) is not int or action.get("target_id") != self.target:
                return False
            expected = page.get("guards", {}).get(str(node))
            return expected is not None and current.get("guards", {}).get(str(node)) == expected
        # For page-level actions, require an identical structured Browser Use observation.
        return current.get("page_key") == page.get("page_key")

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "fill" and (not isinstance(text, str) or not text or len(text) > 2000):
            raise ValueError("Text entry requires a valid generated value; no action executed.")
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        return result

    def close(self):
        # Close the Browser Harness-owned target while its CDP connection is live.
        # Browser Use's observer owns a separate WebSocket and is torn down after it.
        target, self.target = self.target, None
        # Close every tab this run owned, including ones it followed into.
        owned = ([target] if target else []) + sorted(self.extra_targets)
        self.extra_targets = set()
        for item in owned:
            try:
                cdp("Target.closeTarget", targetId=item)
            except Exception as error:
                print(f"Browser Harness target cleanup failed: {error}", file=sys.stderr, flush=True)
        reader, self.reader = self.reader, None
        if reader is not None:
            try:
                reader.close()
            except Exception as error:
                # Cleanup is best-effort; a stale observer connection must not
                # prevent the local service from stopping or restarting.
                print(f"Browser Use observer cleanup failed: {error}", file=sys.stderr, flush=True)


def fingerprint(state):
    # Prefer the reader's stable identity key so ambient page text churn cannot
    # invalidate an otherwise valid decision.
    identity = state.get("page_key")
    content = (
        {"page_key": identity, "actions": state.get("actions")}
        if identity
        else {key: state.get(key) for key in ("url", "title", "text", "headings", "actions", "scroll")}
    )
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    """Perform only operations approved from a fresh Browser Use state."""
    operation = request["operation"]
    session = request["session"]
    if operation != "act":
        raise ValueError("Browser Harness receives actions only; Browser Use owns page observation.")

    action = request["action"]
    kind = action["kind"]
    if kind == "wait":
        time.sleep(0.1)
        return {"executed": action["id"]}
    if kind == "scroll":
        delta = action.get("delta")
        if type(delta) not in (int, float) or abs(delta) > 2000:
            raise ValueError("Invalid observed scroll action")
        cdp("Input.dispatchMouseEvent", session_id=session, type="mouseWheel", x=550, y=650, deltaX=0, deltaY=delta)
        return {"executed": action["id"]}

    backend_node_id = action.get("backend_node_id")
    if type(backend_node_id) is not int or action.get("target_id") is None:
        raise ValueError("Action is not bound to a Browser Use DOM node")
    if kind not in {"click", "fill", "select"}:
        raise ValueError("Unsupported observed action")
    if kind == "select" and not isinstance(action.get("value"), str):
        raise ValueError("Dropdown value is not an observed string")

    resolved = cdp("DOM.resolveNode", session_id=session, backendNodeId=backend_node_id)
    object_id = resolved.get("object", {}).get("objectId")
    if not object_id:
        raise StalePage("Browser Use target is no longer available. Observe again.")
    try:
        result = cdp(
            "Runtime.callFunctionOn",
            session_id=session,
            objectId=object_id,
            functionDeclaration="""function(action) {
              const e=this;
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) return null;
              const s=getComputedStyle(e);
              if (s.display==='none' || s.visibility==='hidden' || Number(s.opacity)===0) return null;
              const r=e.getBoundingClientRect();
              if (!r.width || !r.height) return null;
              const root=e.getRootNode();
              const at=(x,y)=>(root.elementFromPoint ? root.elementFromPoint(x,y) : document.elementFromPoint(x,y));
              // An inline link that wraps onto two lines -- a search result's
              // url line plus its title heading -- has a union box whose centre
              // falls in the leading between the two line boxes, where the hit
              // test lands on a neighbouring element and the action is refused.
              // Try the union centre first, then each line box, and click the
              // first point that really belongs to this element. Occlusion is
              // still caught: an overlay covers every candidate point.
              let point=null;
              for (const box of [r, ...e.getClientRects()]) {
                const x=box.x+box.width/2, y=box.y+box.height/2;
                if (!box.width || !box.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
                const hit=at(x,y);
                if (hit===e || e.contains(hit)) { point={x,y}; break; }
              }
              if (!point) return null;
              const type=(e.getAttribute('type')||'').toLowerCase();
              if (action.guard?.tag && e.tagName.toLowerCase()!==action.guard.tag) return null;
              if (action.kind==='fill' && action.guard?.value!==null &&
                  String(e.value??'')!==String(action.guard.value??'')) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true' ||
                  ['password','file','hidden','checkbox','radio','button','submit','reset'].includes(type))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return point;
            }""",
            arguments=[{"value": action}],
            returnByValue=True,
        )
        if result.get("exceptionDetails"):
            raise StalePage("Target changed during final safety validation. Observe again.")
        target = result.get("result", {}).get("value")
        if target is None:
            if kind == "select":
                raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
            raise StalePage("Target changed or is covered. Observe again.")
        if kind != "select":
            x, y = target["x"], target["y"]
            for event in ("mousePressed", "mouseReleased"):
                cdp("Input.dispatchMouseEvent", session_id=session, type=event, x=x, y=y, button="left", clickCount=1)
            if kind == "fill":
                cdp(
                    "Input.dispatchKeyEvent",
                    session_id=session,
                    type="keyDown",
                    key="a",
                    code="KeyA",
                    modifiers=4 if sys.platform == "darwin" else 2,
                    commands=["selectAll"],
                )
                cdp(
                    "Input.dispatchKeyEvent",
                    session_id=session,
                    type="keyUp",
                    key="a",
                    code="KeyA",
                    modifiers=4 if sys.platform == "darwin" else 2,
                )
                cdp("Input.insertText", session_id=session, text=request["text"])
        return {"executed": action["id"]}
    finally:
        try:
            cdp("Runtime.releaseObject", session_id=session, objectId=object_id)
        except Exception:
            pass
