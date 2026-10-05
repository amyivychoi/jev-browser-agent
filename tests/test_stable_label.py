"""Freshness must survive a page that rewrites its own labels.

A mailbox increments an unread count and re-renders relative timestamps on its
own. If that reads as "the page changed", every action raises StalePage and the
run stops with stale-page while nothing meaningful moved.
"""

import pytest

from jev_ultrafast.page_reader import _page_identity, _stable_label


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("收件箱 (3)", "收件箱 (4)"),                      # unread counter
        ("Inbox (12)", "Inbox (13)"),
        ("(3) 收件箱", "(15) 收件箱"),                     # counter in a tab title
        ("Inbox [1,024]", "Inbox [1,025]"),
        ("会议记录 2 分钟前", "会议记录 3 分钟前"),          # relative time, zh
        ("Report 5 minutes ago", "Report 6 minutes ago"),  # relative time, en
        ("Report 1 hour ago", "Report 2 hours ago"),
        ("Draft saved 09:41", "Draft saved 09:42"),        # wall clock
        ("Unread 3", "Unread 7"),
    ],
)
def test_self_changing_labels_compare_equal(before, after):
    assert _stable_label(before) == _stable_label(after)


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("收件箱", "草稿箱"),              # different mailbox folders
        ("Inbox", "Archive"),
        ("Compose", "Delete"),
        ("下一页", "上一页"),
        ("Sign in", "Sign out"),
    ],
)
def test_distinct_controls_stay_distinct(one, other):
    """Normalisation must not merge two controls into one identity."""
    assert _stable_label(one) != _stable_label(other)


def test_identity_words_survive_normalisation():
    assert _stable_label("收件箱 (3)") == "收件箱"
    assert _stable_label("Inbox (12)") == "Inbox"


def test_a_purely_numeric_label_degrades_to_empty_not_a_crash():
    """Page numbers normalise to nothing; the guard still has tag/role/href."""
    assert _stable_label("42") == ""
    assert _stable_label("") == ""


# The shapes below mirror what _headings and _actions actually produce. Testing
# _stable_label alone let a crash through: headings are dicts, not strings.

def identity(*, title="收件箱", headings=None, actions=None, guards=None):
    return _page_identity(
        target_id="T1",
        url="https://mail.example.test/inbox",
        title=title,
        headings=[{"level": "1", "text": "收件箱"}] if headings is None else headings,
        actions=[{"id": "e1", "kind": "click", "role": "link", "label": "会议记录", "value": ""}]
        if actions is None
        else actions,
        guards={"10": {"tag": "a", "role": "link", "label": "会议记录"}} if guards is None else guards,
        scroll={"above": 0, "below": 0},
    )


def test_identity_accepts_the_real_heading_shape():
    """_headings yields {"level", "text"} dicts, never bare strings."""
    marker, page_key = identity()
    assert isinstance(marker, str) and isinstance(page_key, str)


def test_a_heading_counter_does_not_change_page_identity():
    before = identity(headings=[{"level": "1", "text": "收件箱 (3)"}])
    after = identity(headings=[{"level": "1", "text": "收件箱 (4)"}])
    assert before == after


def test_a_different_heading_does_change_page_identity():
    before = identity(headings=[{"level": "1", "text": "收件箱"}])
    after = identity(headings=[{"level": "1", "text": "草稿箱"}])
    assert before != after


def test_a_heading_level_change_still_changes_identity():
    """Only the text is normalised; structure is not."""
    before = identity(headings=[{"level": "1", "text": "收件箱"}])
    after = identity(headings=[{"level": "2", "text": "收件箱"}])
    assert before != after


def test_a_title_counter_does_not_change_the_marker():
    assert identity(title="(3) 收件箱")[0] == identity(title="(15) 收件箱")[0]


def test_an_action_label_counter_does_not_change_page_identity():
    before = identity(actions=[{"id": "e1", "kind": "click", "label": "收件箱 (3)"}])
    after = identity(actions=[{"id": "e1", "kind": "click", "label": "收件箱 (4)"}])
    assert before == after


def test_a_changed_field_value_still_changes_page_identity():
    """Freshness must keep catching real state changes."""
    before = identity(actions=[{"id": "e1", "kind": "fill", "label": "搜索", "value": ""}])
    after = identity(actions=[{"id": "e1", "kind": "fill", "label": "搜索", "value": "会议"}])
    assert before != after


def test_a_changed_guard_still_changes_page_identity():
    before = identity(guards={"10": {"tag": "a", "label": "会议记录"}})
    after = identity(guards={"10": {"tag": "button", "label": "会议记录"}})
    assert before != after


@pytest.mark.parametrize(
    "missing",
    [
        {"level": "1"},                      # heading with no text
        {"text": "收件箱"},                   # heading with no level
        {},                                  # neither
    ],
)
def test_an_incomplete_heading_does_not_crash_identity(missing):
    """Real DOM reads produce partial nodes; identity must survive them."""
    marker, page_key = identity(headings=[missing])
    assert isinstance(page_key, str)


def test_an_action_without_a_label_does_not_crash_identity():
    marker, page_key = identity(actions=[{"id": "wait", "kind": "wait"}])
    assert isinstance(page_key, str)


def test_a_missing_title_does_not_crash_identity():
    marker, page_key = identity(title=None)
    assert isinstance(marker, str)
