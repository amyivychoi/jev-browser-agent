"""The run's own page being closed must stop the run, not redirect it.

Browser Use's session manager recovers from a detached focus target by switching
to any remaining tab. Those tabs are the user's. Observed live: after the focus
target was closed, observe() succeeded and returned another tab's page while
still reporting the dead target's id, so page identity never matched again and
the run reported stale-page three times over.
"""

import asyncio
import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage, fingerprint
from jev_ultrafast.page_reader import BrowserUsePageReader, TargetLost


def page():
    state = {
        "url": "https://mail.example.test/inbox",
        "title": "收件箱",
        "text": "收件箱",
        "headings": [{"level": "1", "text": "收件箱"}],
        "scroll": {"y": 0, "above": 0, "below": 0},
        "actions": [
            {"id": "e1", "kind": "click", "label": "会议记录", "role": "link", "value": "", "node": 10},
        ],
        "guards": {"10": {"tag": "a", "label": "会议记录"}},
    }
    state["fingerprint"] = fingerprint(state)
    return state


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": None,
        "goal": "打开收件箱",
        "history": [],
        "decisions": [],
        "text_calls": [],
        "review_calls": [],
        "reviews": 0,
        "stale_retries": 0,
        "status": "ready",
        "stop_reason": None,
        "stop_detail": None,
        "plan": ["打开收件箱"],
        "plan_index": 0,
        "elapsed_ms": 0,
        "started_at": time.perf_counter(),
        "record": False,
    }
    return a


def test_a_closed_page_stops_the_run(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(side_effect=TargetLost("page closed")))
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "target-lost"


def test_a_closed_page_is_not_reported_as_a_stale_page(runner, monkeypatch):
    """The two stops are different failures and must not be conflated."""
    monkeypatch.setattr(loop, "choose", Mock(side_effect=TargetLost("page closed")))
    runner.command("tick")
    assert runner.state["stop_reason"] != "stale-page"
    assert runner.state["stale_retries"] == 0  # No retry budget is spent.


def test_no_other_tab_is_observed_after_the_page_is_lost(runner, monkeypatch):
    """Re-observing would read a tab belonging to the user."""
    monkeypatch.setattr(loop, "choose", Mock(side_effect=TargetLost("page closed")))
    runner.command("tick")
    assert runner.state["browser"].observe.call_count == 0


def test_a_lost_page_is_never_sent_to_the_reviewer(runner, monkeypatch):
    """A verdict on someone else's tab would be meaningless and leak it."""
    review = Mock()
    monkeypatch.setattr(loop, "review_page", review)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=TargetLost("page closed")))
    runner.command("tick")
    assert review.call_count == 0
    assert runner.state["reviews"] == 0


def test_the_stop_says_what_happened(runner, monkeypatch):
    monkeypatch.setattr(
        loop, "choose", Mock(side_effect=TargetLost("The page this run was working on was closed."))
    )
    runner.command("tick")
    assert "closed" in runner.state["stop_detail"]


def test_a_stale_page_still_takes_the_stale_path(runner, monkeypatch):
    """The new branch must not swallow ordinary freshness failures."""
    monkeypatch.setattr(loop, "choose", Mock(side_effect=StalePage("Page changed")))
    runner.command("tick")
    assert runner.state["stop_reason"] is None  # Retrying, not stopped.
    assert runner.state["stale_retries"] == 1
    assert runner.state["browser"].observe.call_count == 1


def test_target_lost_is_not_a_stale_page_subclass():
    """A broad `except StalePage` must not catch a lost target by accident."""
    assert not issubclass(TargetLost, StalePage)


def test_the_reader_refuses_to_observe_a_target_that_is_gone():
    """The check that was missing: a dead target read as a live page."""
    class Session:
        """Records any focus assignment, so the test can prove none happened."""

        def __init__(self):
            self.focus_writes = []

        @property
        def agent_focus_target_id(self):
            return None

        @agent_focus_target_id.setter
        def agent_focus_target_id(self, value):
            self.focus_writes.append(value)

    reader = BrowserUsePageReader.__new__(BrowserUsePageReader)
    reader.target_id = "DEAD"
    reader.session = Session()

    async def page_target_ids():
        return ["ALIVE_ONE", "ALIVE_TWO"]

    reader._page_target_ids = page_target_ids
    with pytest.raises(TargetLost):
        asyncio.run(reader._observe("frame-1"))
    # Focus is never pointed at a surviving tab, which would be the user's.
    assert reader.session.focus_writes == []
