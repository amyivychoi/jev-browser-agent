"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text, review_context, review_page
from .page_reader import TargetLost
from .questions import MAX_REVIEWS, MAX_STEPS


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            stop_reason=None,
            stop_detail=None,
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            review_calls=[],
            reviews=0,
            stale_retries=0,
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            if page.get("screenshot"):
                (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def review(self, *, trigger):
        """Judge the current page against the goal and report the outcome.

        Returns "met" when the page already satisfies the goal, "continue" to
        grant one more pass on a freshly observed page, or "unresolved" when the
        reviewer could not say. Reports only: the caller owns the run's status,
        and Browser Harness remains the sole executor.
        """
        state = self.state
        state["reviews"] += 1
        record = {"step": len(state["history"]), "trigger": trigger}
        try:
            verdict = review_page(review_context(state["goal"], state["page"], state["history"]))
        except (RuntimeError, ValueError, KeyError) as error:
            state["review_calls"].append({**record, "status": "error", "error": str(error)})
            return "unresolved"
        state["review_calls"].append({**record, "status": "ok", **verdict})
        if verdict["met"]:
            state["plan_index"] = 1
            return "met"
        # Not met: clear the stale counter so the granted pass starts even.
        state["stale_retries"] = 0
        return "continue"

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(
                self.state["page"]["actions"],
                self.state["history"],
                self.state["page"]["url"],
            )[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except TargetLost as error:
                # The run's own page is gone. Any remaining tab belongs to the
                # user, so nothing is re-observed, reviewed, or acted on.
                state["decision"] = None
                state["status"] = "blocked"
                state["stop_reason"] = "target-lost"
                state["stop_detail"] = str(error)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            except StalePage:
                state["decision"] = None
                state["stale_retries"] = state.get("stale_retries", 0) + 1
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                # Retries are exhausted, but a page that rewrites itself may
                # already satisfy the goal. Ask the chat model to judge the page
                # before reporting a dead end.
                outcome = "retrying" if state["stale_retries"] < 3 else "stale-page"
                if outcome == "stale-page" and state["reviews"] < MAX_REVIEWS:
                    verdict = self.review(trigger="stale-page")
                    outcome = {"met": "review-goal-met", "continue": "retrying"}.get(
                        verdict, "stale-page"
                    )
                state["status"] = {
                    "retrying": "ready",
                    "review-goal-met": "done",
                    "stale-page": "blocked",
                }[outcome]
                state["stop_reason"] = None if outcome == "retrying" else outcome
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            state["decision"] = choose(state["page"], state["goal"], state["history"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    state["stop_reason"] = None
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["stop_reason"] = "model-done" if selected == "DONE" else "model-blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            target_guard_before = page.get("guards", {}).get(str(action.get("node")))
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                state["stop_reason"] = "action-budget"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page, action):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    try:
                        text, helper = field_text(context)
                    except (RuntimeError, ValueError) as error:
                        state["text_calls"].append(
                            {"field": action["label"], "status": "error", "error": str(error)}
                        )
                        raise
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            tabs_before = state["browser"].page_targets()
            state["browser"].act(action, page, text=text)
            # Browser Use switches to a tab opened by a click; do the same here so
            # the run continues on the page the action actually produced.
            followed_target = state["browser"].follow_new_tab(tabs_before)
            # Only a real target id counts as a follow; anything else is "no new tab".
            followed = isinstance(followed_target, str) and bool(followed_target)
            state["stale_retries"] = 0
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"].get(selected),
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "followed_new_tab": followed,
                    "from_url": page["url"],
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            target_guard_after = state["page"].get("guards", {}).get(str(action.get("node")))
            target_unchanged = target_guard_before is not None and target_guard_before == target_guard_after
            no_progress = (
                action["kind"] in {"click", "fill", "select"}
                and target_unchanged
                and page["url"] == state["page"]["url"]
                # Following a new tab is progress, even though the old target is intact.
                and not followed
            )
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                target_unchanged=target_unchanged,
                no_progress=no_progress,
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            repeated_fill = (
                len(repeated) >= 2
                and repeated[-1]["kind"] == "fill"
                and repeated[-2]["kind"] == "fill"
                and repeated[-1]["action"] == repeated[-2]["action"]
                and repeated[-1]["url"] == repeated[-2]["url"]
            )
            stuck_on_same_target = (
                len(repeated) == 3
                and len({(h["action"], h["kind"], h["url"]) for h in repeated}) == 1
                and repeated[0]["kind"] in {"click", "fill", "select"}
                and all(h.get("target_unchanged") for h in repeated)
                and not any(h.get("followed_new_tab") for h in repeated)
            )
            no_progress = len(repeated) == 3 and all(
                h["page_changed"] is False and h["kind"] != "wait" and not h.get("followed_new_tab")
                for h in repeated
            )
            state["stop_reason"] = (
                "repeated-fill" if repeated_fill else
                "same-target-no-change" if stuck_on_same_target else
                "three-actions-no-change" if no_progress else None
            )
            state["status"] = "blocked" if state["stop_reason"] else "ready"
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
