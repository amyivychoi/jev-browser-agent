"""Structured, read-only page observation powered by Browser Use."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
from typing import Any

# Browser Use otherwise creates configuration under ~/.config/browseruse at import time.
os.environ.setdefault(
    "BROWSER_USE_CONFIG_DIR",
    os.path.join(tempfile.gettempdir(), "browser-agent-browser-use"),
)
os.environ["ANONYMIZED_TELEMETRY"] = "false"

from browser_use import BrowserSession
from browser_harness.daemon import get_ws_url


class TargetLost(RuntimeError):
    """The page the run was working on no longer exists.

    Browser Use's session manager recovers from a detached focus target by
    switching to any remaining tab. That tab belongs to the user, not to the
    run, so its DOM must never be read, offered to a model, or acted on. The
    run stops instead.
    """


class BrowserUsePageReader:
    """Read one existing Chrome target with Browser Use's structured DOM service.

    Browser Use only observes here. Browser Harness remains the sole action executor.
    """

    def __init__(self, cdp_url: str, target_id: str):
        if not cdp_url:
            raise ValueError("BU_CDP_URL is required for Browser Use page observation")
        # Browser Harness resolves Chrome 147+'s disabled /json/version discovery
        # endpoint via DevToolsActivePort. Browser Use's own HTTP discovery does not,
        # so pass it the same resolved browser WebSocket URL used by the Harness.
        self.cdp_url = get_ws_url()
        self.target_id = target_id
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="browser-use-reader", daemon=True)
        self.thread.start()
        self._ready = threading.Event()
        self.loop.call_soon_threadsafe(self._ready.set)
        if not self._ready.wait(timeout=5):
            raise RuntimeError("Browser Use page reader did not start")
        self.session: BrowserSession | None = None
        try:
            self._submit(self._start(), timeout=30)
        except Exception:
            self._stop_loop()
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coroutine, *, timeout: float = 20):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)

    async def _start(self) -> None:
        self.session = BrowserSession(
            cdp_url=self.cdp_url,
            is_local=False,
            keep_alive=True,
            enable_default_extensions=False,
            dom_highlight_elements=False,
            highlight_elements=False,
            cross_origin_iframes=True,
        )
        await self.session.start()
        targets = {target.target_id for target in self.session.get_page_targets()}
        if self.target_id not in targets:
            raise RuntimeError("Browser Use could not attach to the Browser Harness page")
        # Keep the Browser Harness-owned, background target selected without activating it.
        self.session.agent_focus_target_id = self.target_id

    def observe(self, main_frame_id: str) -> dict[str, Any]:
        return self._submit(self._observe(main_frame_id), timeout=30)

    def page_target_ids(self) -> list[str]:
        """Every page target Browser Use can currently see."""
        return self._submit(self._page_target_ids(), timeout=10)

    async def _page_target_ids(self) -> list[str]:
        assert self.session is not None
        return [target.target_id for target in self.session.get_page_targets()]

    def focus(self, target_id: str) -> None:
        """Observe a different tab from now on, as Browser Use does after a click."""
        self.target_id = target_id

    async def _observe(self, main_frame_id: str) -> dict[str, Any]:
        assert self.session is not None
        # Assigning agent_focus_target_id does not make the target exist. A
        # closed or crashed tab leaves the session manager free to recover onto
        # one of the user's own tabs, which would then be read as if it were
        # ours. Check first, and stop rather than read the wrong page.
        if self.target_id not in await self._page_target_ids():
            raise TargetLost(
                "The page this run was working on was closed. "
                "No other tab is read or acted on; start a fresh run."
            )
        self.session.agent_focus_target_id = self.target_id
        summary = await self.session.get_browser_state_summary(
            include_screenshot=False,
            cached=False,
        )
        if summary.state_error:
            raise RuntimeError(f"Browser Use could not read page structure: {summary.state_error}")

        dom_state = summary.dom_state
        page_info = summary.page_info
        text = dom_state.llm_representation()[:6000]
        headings = _headings(dom_state._root)
        actions, guards, candidate_diagnostics = _actions(dom_state.selector_map, self.target_id, main_frame_id)
        scroll = {
            "y": page_info.scroll_y if page_info else 0,
            "height": page_info.page_height if page_info else 0,
            "above": page_info.pixels_above if page_info else 0,
            "below": page_info.pixels_below if page_info else 0,
        }
        if scroll["below"] > 0:
            actions.append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560})
        if scroll["above"] > 0:
            actions.append({"id": "scroll_up", "kind": "scroll", "label": "Scroll up", "delta": -560})
        actions.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
        semantic = {
            "url": summary.url,
            "title": summary.title,
            "text": text,
            "headings": headings,
            "actions": [
                {key: value for key, value in action.items() if key not in {"rect", "guard"}}
                for action in actions
            ],
            "scroll": scroll,
        }
        marker, page_key = _page_identity(
            target_id=self.target_id,
            url=summary.url,
            title=summary.title,
            headings=headings,
            actions=actions,
            guards=guards,
            scroll=scroll,
        )
        width = page_info.viewport_width if page_info else 1120
        height = page_info.viewport_height if page_info else 780
        return {
            **semantic,
            "w": width,
            "h": height,
            "actions": actions,
            "guards": guards,
            "target_id": self.target_id,
            "page_key": [marker, page_key],
            "fingerprint": page_key,
            "source": "browser-use",
            "candidate_diagnostics": candidate_diagnostics,
            "omitted_actions": candidate_diagnostics.get("capped_actions", 0),
        }

    async def _close(self) -> None:
        if self.session is not None:
            await self.session.stop()
            self.session = None

    def close(self) -> None:
        try:
            self._submit(self._close(), timeout=10)
        finally:
            self._stop_loop()

    def _stop_loop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread.is_alive():
            self.thread.join(timeout=5)


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _own_name(node) -> str:
    """The element's own accessible name. Never borrowed from other elements."""
    attrs = node.attributes or {}
    name = node.ax_node.name if node.ax_node and node.ax_node.name else ""
    for candidate in (attrs.get("aria-label"), attrs.get("title"), name, attrs.get("placeholder"), attrs.get("alt")):
        text = _clean(candidate)
        # A whole result row (preview text included) is not a control name.
        if text and len(text) <= 120:
            return text
    return ""


def _own_text(node) -> str:
    """Visible text of the control itself, used only when it reads like a label.

    Skipped for value-bearing controls, whose child text is their content or
    their option list rather than a name.
    """
    if node.tag_name in {"select", "textarea", "input"}:
        return ""
    text = _clean(node.get_all_children_text())
    return text if text and len(text) <= 120 else ""


def _heading_title(node) -> str:
    """A result title for this control: its own heading text, or an ancestor heading.

    Descendant subtrees are searched only two levels deep, so a container that
    merely wraps a result list never inherits the first title inside it.
    """

    def own(item, depth: int) -> str:
        if item.tag_name in HEADING_TAGS:
            source = item.ax_node.name if item.ax_node and item.ax_node.name else item.get_all_children_text()
            return _clean(source)
        if depth <= 0:
            return ""
        for child in item.children:
            found = own(child, depth - 1)
            if found:
                return found
        return ""

    found = own(node, 2)
    if found:
        return found
    parent, hops = node.parent_node, 0
    while parent is not None and hops < 4:
        if parent.tag_name in HEADING_TAGS:
            source = parent.ax_node.name if parent.ax_node and parent.ax_node.name else parent.get_all_children_text()
            return _clean(source)
        parent, hops = parent.parent_node, hops + 1
    return ""


def _label(node) -> str:
    return _own_name(node) or _own_text(node) or _heading_title(node)[:160] or _role(node)


def _role(node) -> str:
    if node.ax_node and node.ax_node.role:
        return node.ax_node.role.lower().replace(" ", "-")
    attrs = node.attributes or {}
    if attrs.get("role"):
        return attrs["role"].lower()
    return {
        "a": "link",
        "button": "button",
        "input": "textbox",
        "select": "combobox",
        "textarea": "textbox",
    }.get(node.tag_name, node.tag_name)


# Counters and relative times live inside otherwise stable control labels:
# an inbox link reads "收件箱 (3)" until a mail arrives, a row reads "2 分钟前"
# until the clock ticks. Freshness asks whether this is still the same control,
# so these are normalised away. Identity-bearing words are untouched.
VOLATILE_LABEL_PATTERNS = (
    re.compile(r"[（(\[]\s*\d[\d,.\s]*\s*[）)\]]"),          # (3)  [12]  （1,024）
    re.compile(r"\b\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b", re.I),
    re.compile(r"\d+\s*(?:秒|分钟|分|小时|天|周|个?月|年)前"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),             # 09:41
    re.compile(r"\d[\d,.]*"),                                 # any remaining digit run
)


def _page_identity(*, target_id, url, title, headings, actions, guards, scroll) -> tuple[str, str]:
    """The page's identity pair: (marker, page_key).

    Identity covers what a decision depends on: location, offered actions and
    their state. Ambient text churn (ads, clocks, lazy content) must not
    invalidate a fresh decision, and neither must a label rewriting its own
    counter or relative time — see _stable_label.
    """
    identity = {
        "target_id": target_id,
        "url": url,
        # An unread counter in the tab title ("(3) 收件箱") must not read as
        # a different page.
        "title": _stable_label(title or ""),
        # Each heading is {"level", "text"}; only the text carries churn.
        "headings": [
            {"level": item.get("level"), "text": _stable_label(item.get("text") or "")}
            for item in headings
        ],
        "actions": [
            {
                **{key: action.get(key) for key in ("id", "kind", "role", "value", "control_type")},
                "label": _stable_label(action.get("label") or ""),
            }
            for action in actions
        ],
        "guards": guards,
        "scrollable": {"above": scroll["above"] > 0, "below": scroll["below"] > 0},
    }
    page_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    marker = hashlib.sha256(
        json.dumps(
            {"target_id": target_id, "url": url, "title": _stable_label(title or "")},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return marker, page_key


def _stable_label(value: str) -> str:
    """A label with self-changing parts removed, for freshness comparison only.

    The model always receives the real label; this is never shown to it.
    """
    text = value
    for pattern in VOLATILE_LABEL_PATTERNS:
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def _guard(node) -> dict[str, Any]:
    attrs = node.attributes or {}
    snap = node.snapshot_node
    return {
        "backend_node_id": node.backend_node_id,
        "target_id": node.target_id,
        "tag": node.tag_name,
        "role": _role(node),
        # Compared for freshness, never displayed. See _stable_label.
        "label": _stable_label(_label(node)),
        "value": None if attrs.get("type", "").lower() == "password" else (
            snap.input_value if snap and snap.input_value is not None else attrs.get("value", "")
        ),
        "checked": snap.input_checked if snap and snap.input_checked is not None else attrs.get("aria-checked", attrs.get("checked")),
        "selected": attrs.get("aria-selected", attrs.get("selected")),
        "expanded": attrs.get("aria-expanded"),
        "pressed": attrs.get("aria-pressed"),
        "href": attrs.get("href"),
        "disabled": "disabled" in attrs or attrs.get("aria-disabled") == "true",
        "readonly": "readonly" in attrs or attrs.get("aria-readonly") == "true",
    }


def _form_backend_id(node) -> int | None:
    parent = node.parent_node
    while parent is not None:
        if parent.tag_name == "form":
            return parent.backend_node_id
        parent = parent.parent_node
    return None


def _rect(node) -> dict[str, float] | None:
    snap = node.snapshot_node
    bounds = snap.clientRects if snap else None
    if not bounds:
        return None
    return {"x": bounds.x, "y": bounds.y, "w": bounds.width, "h": bounds.height}


def _actions(
    selector_map,
    target_id: str,
    main_frame_id: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
    actions: list[dict[str, Any]] = []
    guards: dict[str, dict[str, Any]] = {}
    diagnostics = {
        "selector_nodes": len(selector_map),
        "other_target": 0,
        "frame_unspecified": 0,
        "other_frame": 0,
        "invisible": 0,
        "missing_rect": 0,
        "sensitive_input": 0,
        "not_semantic": 0,
        "no_operation": 0,
        "nested_duplicate": 0,
        "registered_nodes": 0,
        "registered_actions": 0,
        "title_links": 0,
        "dropped_labels": [],
    }
    parents_of_candidates: set[int] = set()
    for index, node in selector_map.items():
        if node.target_id != target_id:
            diagnostics["other_target"] += 1
            continue
        # Browser Use's main-target DOM nodes often omit frameId; target_id
        # already binds those nodes to this page. Reject only an explicit frame
        # ID that identifies a different frame (for example an iframe).
        if node.frame_id is None:
            diagnostics["frame_unspecified"] += 1
        elif node.frame_id != main_frame_id:
            diagnostics["other_frame"] += 1
            continue
        if node.is_visible is False:
            diagnostics["invisible"] += 1
            continue
        attrs = node.attributes or {}
        tag = node.tag_name
        input_type = attrs.get("type", "").lower()
        if tag == "input" and input_type in {"password", "file", "hidden"}:
            diagnostics["sensitive_input"] += 1
            continue
        rect = _rect(node)
        if rect is None:
            # Browser Use can identify actionable nodes without providing geometry.
            # The final Browser Harness hit test obtains fresh bounds before acting.
            diagnostics["missing_rect"] += 1
        guard = _guard(node)
        node_id = node.backend_node_id
        role = _role(node)
        label = _label(node)
        # A result title link is a link/button whose own name is missing or is
        # itself the heading text. A wrapper container never qualifies.
        clickable_role = tag in {"a", "button"} or role in {"button", "link"}
        title = _heading_title(node)
        heading_label = title if clickable_role and title and title[:120] in {label, label[:120]} else ""
        semantic_control = (
            tag in {"a", "button", "input", "select", "textarea"}
            or role in {"button", "link", "checkbox", "radio", "switch", "option", "menuitem", "textbox", "searchbox", "spinbutton", "combobox"}
        )
        if not semantic_control and not (node.has_js_click_listener and role != "generic"):
            diagnostics["not_semantic"] += 1
            if len(diagnostics["dropped_labels"]) < 12:
                diagnostics["dropped_labels"].append({"reason": "not_semantic", "role": role, "label": label[:80]})
            continue
        guards[str(node_id)] = guard
        parent = node.parent_node
        hops = 0
        while parent is not None and hops < 6:
            parents_of_candidates.add(parent.backend_node_id)
            parent, hops = parent.parent_node, hops + 1
        diagnostics["registered_nodes"] += 1
        value = guard["value"]
        common = {
            "node": node_id,
            "backend_node_id": node_id,
            "target_id": node.target_id,
            "role": role,
            "label": label,
            "form_node": _form_backend_id(node),
            "control_type": input_type or None,
            "rect": rect,
            "value": value or "",
            # A link that opens a new tab cannot advance the observed target.
            # Pop-up targets are out of scope, so clicking it again only opens
            # another tab; the agent must not retry it.
            "new_tab": attrs.get("target", "").strip().lower() in {"_blank", "blank"},
            "guard": guard,
        }
        if tag == "select":
            current_value = value or ""
            for option_index, option in enumerate(node.children, start=1):
                if option.tag_name != "option":
                    continue
                option_attrs = option.attributes or {}
                if "disabled" in option_attrs or option_attrs.get("selected") == "true":
                    continue
                option_label = _label(option)
                option_value = option_attrs.get("value", option.get_all_children_text().strip())
                if not option_value:
                    continue
                actions.append({
                    **common,
                    "id": f"e{index}-{option_index}",
                    "kind": "select",
                    "value": option_value,
                    "current_value": current_value,
                    "label": f"{label} → {option_label}",
                    "option_label": option_label,
                })
            continue

        editable = (
            tag in {"input", "textarea"}
            or "contenteditable" in attrs
            or role in {"textbox", "searchbox", "spinbutton", "combobox"}
        ) and input_type not in {"button", "submit", "reset", "image", "checkbox", "radio"}
        if editable and not guard["readonly"] and not guard["disabled"]:
            actions.append({**common, "id": f"e{index}", "kind": "fill"})
            actions.append({
                **common,
                "id": f"e{index}-click",
                "kind": "click",
                "label": f"Open {label}",
            })
        elif (
            node.has_js_click_listener
            or tag in {"a", "button"}
            or role in {"button", "link", "checkbox", "radio", "switch", "option", "menuitem"}
        ):
            # Geometry may be absent; Browser Harness re-resolves fresh bounds and
            # hit-tests before acting. Dropping the candidate here would hide a
            # real result link from the model instead.
            actions.append({
                **common,
                "id": f"e{index}",
                "kind": "click",
                "heading_label": heading_label or None,
            })
        else:
            diagnostics["no_operation"] += 1
            if len(diagnostics["dropped_labels"]) < 12:
                diagnostics["dropped_labels"].append({"reason": "no_operation", "role": role, "label": label[:80]})

    # A container that only wraps another candidate adds noise without adding reach.
    interactive_nodes = {action["node"] for action in actions}
    redundant = {
        node_id
        for node_id in interactive_nodes & parents_of_candidates
        if not any(
            action["node"] == node_id and (action["kind"] != "click" or action.get("heading_label"))
            for action in actions
        )
    }
    if redundant:
        kept = [action for action in actions if action["node"] not in redundant]
        # Never let de-duplication empty the candidate list.
        if kept:
            diagnostics["nested_duplicate"] = len(actions) - len(kept)
            actions = kept

    # Keep one candidate per identical target: innermost/first wins.
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for action in actions:
        key = (action["kind"], action["label"], action["guard"].get("href"), action.get("value"))
        if action["kind"] == "click" and key in seen:
            diagnostics["nested_duplicate"] += 1
            continue
        seen.add(key)
        unique.append(action)
    actions = unique

    diagnostics["registered_actions"] = len(actions)
    diagnostics["title_links"] = sum(bool(action.get("heading_label")) for action in actions)
    limit = 60

    def rank(action: dict[str, Any]) -> tuple:
        if action.get("heading_label"):
            tier = 0
        elif action["kind"] in {"fill", "select"}:
            tier = 1
        elif action.get("control_type") == "submit":
            tier = 1
        elif action["kind"] == "click" and action["role"] in {"button", "link"}:
            tier = 2
        else:
            tier = 3
        return (tier, int(action["id"].split("-")[0][1:]))

    actions.sort(key=rank)
    diagnostics["capped_actions"] = max(0, len(actions) - limit)
    return actions[:limit], guards, diagnostics


def _headings(root) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def visit(item) -> None:
        node = item.original_node
        if not item.should_display or node.is_visible is False:
            return
        tag = node.tag_name
        role = node.ax_node.role.lower() if node.ax_node and node.ax_node.role else ""
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or role == "heading":
            text = " ".join((node.get_all_children_text() or (node.ax_node.name if node.ax_node else "")).split())
            if text:
                level = tag[1:] if tag.startswith("h") and len(tag) == 2 else ""
                found.append({"level": level, "text": text[:400]})
        for child in item.children:
            visit(child)

    if root is not None:
        visit(root)
    return found[:16]
