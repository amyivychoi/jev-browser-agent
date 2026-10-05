"""Following a tab opened by a click, as Browser Use does natively.

The regression these cover: a `target="_blank"` mailbox link leaves the original
target untouched and the original URL unchanged, so a run that does not notice
the new tab reads its own success as a stall and clicks again.
"""

import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint


def page(url="https://example.test/", node=10):
    state = {
        "url": url,
        "title": "Inbox",
        "text": "QQ邮箱",
        "scroll": {"y": 0},
        "actions": [
            {
                "id": "e1", "kind": "click", "label": "QQ邮箱", "role": "link", "value": "",
                "node": node, "new_tab": True, "guard": {"href": "https://mail.qq.com/"},
            },
        ],
        "guards": {str(node): {"tag": "a", "label": "QQ邮箱", "role": "link"}},
    }
    state["fingerprint"] = fingerprint(state)
    return state


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "CLICK",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(
            fresh=Mock(return_value=True),
            observe=Mock(return_value=p),
            page_targets=Mock(return_value={"T1"}),
            follow_new_tab=Mock(return_value=None),
        ),
        "page": p,
        "decision": decision(),
        "goal": "Open my mailbox",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def click(runner, times=1):
    for _ in range(times):
        runner.state["decision"] = decision()
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})


def test_following_a_new_tab_counts_as_progress(runner):
    """The observation is deliberately identical; only the follow differs."""
    runner.state["browser"].follow_new_tab.return_value = "NEWTAB"
    click(runner)
    step = runner.state["history"][-1]
    assert step["followed_new_tab"] is True
    assert step["no_progress"] is False
    assert step["target_unchanged"] is True  # The old target really is untouched.
    assert runner.state["status"] == "ready"


def test_repeated_follows_never_trip_the_stall_stops(runner):
    runner.state["browser"].follow_new_tab.return_value = "NEWTAB"
    click(runner, times=3)
    assert runner.state["stop_reason"] is None
    assert runner.state["status"] == "ready"


def test_the_tab_list_is_captured_before_the_click(runner):
    """A tab opened by this action is only new relative to the pre-click list."""
    click(runner)
    runner.state["browser"].follow_new_tab.assert_called_once_with({"T1"})
    order = [name for name, _, _ in runner.state["browser"].mock_calls]
    assert order.index("page_targets") < order.index("act") < order.index("follow_new_tab")


def test_an_unfollowed_repeat_still_stops(runner):
    """Without a new tab, three identical clicks remain a stall."""
    click(runner, times=3)
    assert runner.state["stop_reason"] == "same-target-no-change"
    assert runner.state["status"] == "blocked"
    assert all(step["no_progress"] for step in runner.state["history"])


def test_only_a_target_id_counts_as_a_follow(runner):
    """follow_new_tab returns a target id or None. Nothing else is a follow.

    A truthy non-id would otherwise disable every no-progress stop at once.
    """
    runner.state["browser"].follow_new_tab.return_value = Mock()
    click(runner, times=3)
    assert runner.state["stop_reason"] == "same-target-no-change"
