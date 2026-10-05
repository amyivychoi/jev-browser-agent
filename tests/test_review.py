"""The page review that runs when stale-page retries are exhausted.

A page that rewrites itself can exhaust the retries while already satisfying the
goal. The reviewer judges the page before the run reports a dead end. It reports
only: it never selects an action, and Browser Harness stays the sole executor.
"""

import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, fingerprint


def page():
    state = {
        "url": "https://mail.example.test/inbox",
        "title": "收件箱",
        "text": "收件箱 会议记录",
        "headings": ["收件箱"],
        "scroll": {"y": 0, "above": 0, "below": 0},
        "actions": [
            {
                "id": "e1",
                "kind": "click",
                "label": "会议记录",
                "role": "link",
                "value": "",
                "node": 10,
            },
        ],
        "guards": {"10": {"tag": "a", "label": "会议记录"}},
    }
    state["fingerprint"] = fingerprint(state)
    return state


@pytest.fixture
def runner():
    """An agent one StalePage away from exhausting its retries."""
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
        "stale_retries": 2,
        "status": "ready",
        "stop_reason": None,
        "plan": ["打开收件箱"],
        "plan_index": 0,
        "elapsed_ms": 0,
        "started_at": time.perf_counter(),
        "record": False,
    }
    return a


def verdict(met, reason="仍在加载"):
    return {
        "met": met,
        "reason": reason,
        "observation": None,
        "model": "deepseek-flash",
        "latency_ms": 20,
        "usage": {},
    }


@pytest.fixture
def stale(monkeypatch):
    """Make the next decision raise StalePage, as a re-rendering page does."""
    monkeypatch.setattr(loop, "choose", Mock(side_effect=StalePage("Page changed")))


def test_goal_already_met_finishes_the_run(runner, stale, monkeypatch):
    monkeypatch.setattr(loop, "review_page", Mock(return_value=verdict(True, "收件箱已打开")))
    runner.command("tick")
    assert runner.state["status"] == "done"
    assert runner.state["stop_reason"] == "review-goal-met"
    assert runner.state["review_calls"][-1]["met"] is True


def test_goal_not_met_grants_another_pass_instead_of_stopping(runner, stale, monkeypatch):
    monkeypatch.setattr(loop, "review_page", Mock(return_value=verdict(False)))
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["stop_reason"] is None
    # The counter is reset so the granted pass starts even.
    assert runner.state["stale_retries"] == 0


def test_a_failing_reviewer_leaves_the_original_stop_in_place(runner, stale, monkeypatch):
    monkeypatch.setattr(loop, "review_page", Mock(side_effect=ValueError("no verdict")))
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "stale-page"
    assert runner.state["review_calls"][-1]["status"] == "error"


def test_rescues_are_bounded(runner, stale, monkeypatch):
    """A page that always reports not-met must still be able to stop."""
    review = Mock(return_value=verdict(False))
    monkeypatch.setattr(loop, "review_page", review)
    runner.state["reviews"] = loop.MAX_REVIEWS
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "stale-page"
    assert review.call_count == 0  # Budget spent, so not consulted at all.


def test_retries_are_not_reviewed_before_they_run_out(runner, stale, monkeypatch):
    """The reviewer is a last resort, not a per-retry consultation."""
    review = Mock(return_value=verdict(True))
    monkeypatch.setattr(loop, "review_page", review)
    runner.state["stale_retries"] = 0
    runner.command("tick")
    assert review.call_count == 0
    assert runner.state["status"] == "ready"


def test_the_reviewer_judges_a_freshly_observed_page(runner, stale, monkeypatch):
    """Judging the pre-stale page would be judging a page that no longer exists."""
    after = page()
    after["title"] = "会议记录"
    runner.state["browser"].observe = Mock(return_value=after)
    seen = {}
    monkeypatch.setattr(
        loop,
        "review_page",
        Mock(side_effect=lambda context: seen.update(context) or verdict(False)),
    )
    runner.command("tick")
    assert seen["page"]["title"] == "会议记录"


def test_review_reports_a_verdict_and_never_an_action():
    """The verdict carries no action field the loop could execute."""
    post = Mock(
        return_value={"choices": [{"message": {"content": '{"met":true,"reason":"ok"}'}}]}
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(model, "post_json", post)
        mp.setenv("DEEPSEEK_API_KEY", "test-key")
        result = model.review_page({"goal": "x", "page": {}})
    assert set(result) == {"met", "reason", "observation", "model", "latency_ms", "usage", "reasoning"}
    assert result["met"] is True


def test_a_fenced_verdict_is_still_read():
    content = '```json\n{"met": false, "reason": "登录墙", "observation": null}\n```'
    post = Mock(return_value={"choices": [{"message": {"content": content}}]})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(model, "post_json", post)
        mp.setenv("DEEPSEEK_API_KEY", "test-key")
        result = model.review_page({"goal": "x", "page": {}})
    assert result["met"] is False
    assert result["reason"] == "登录墙"


@pytest.mark.parametrize(
    "content, finish, expected",
    [
        ('{"met":"yes"}', "stop", "met_is_not_boolean"),
        ('{"met":true}', "length", "truncated"),
        ("not json at all", "stop", "invalid_json"),
        ("", "stop", "empty_response"),
        ('["met"]', "stop", "not_json_object"),
    ],
)
def test_an_unusable_verdict_is_refused_rather_than_guessed(content, finish, expected):
    post = Mock(
        return_value={
            "choices": [{"message": {"content": content}, "finish_reason": finish}]
        }
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(model, "post_json", post)
        mp.setenv("DEEPSEEK_API_KEY", "test-key")
        with pytest.raises(ValueError, match=expected):
            model.review_page({"goal": "x", "page": {}})


def test_the_reviewer_sees_the_goal_and_the_page_but_not_the_whole_transcript():
    history = [{"step": i, "action": f"a{i}", "kind": "click", "url": "u"} for i in range(20)]
    context = model.review_context("打开收件箱", page(), history)
    assert context["goal"] == "打开收件箱"
    assert len(context["recent_actions"]) == 8
    assert context["page"]["url"] == "https://mail.example.test/inbox"
