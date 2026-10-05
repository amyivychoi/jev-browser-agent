"""Candidate quality contracts: a result title link must always reach the model."""

from jev_ultrafast import model, page_reader


class Ax:
    def __init__(self, name="", role=""):
        self.name = name
        self.role = role


class Node:
    """Minimal stand-in for a Browser Use DOM node."""

    def __init__(self, tag, *, name="", role="", text="", attrs=None, children=(), nid=1, visible=True, js=False):
        self.tag_name = tag
        self.ax_node = Ax(name, role)
        self.attributes = dict(attrs or {})
        self.children = list(children)
        self.backend_node_id = nid
        self.target_id = "T"
        self.frame_id = None
        self.is_visible = visible
        self.has_js_click_listener = js
        self.snapshot_node = None
        self.parent_node = None
        self._text = text
        for child in self.children:
            child.parent_node = self

    def get_all_children_text(self):
        return self._text or " ".join(c.get_all_children_text() for c in self.children)


def serp():
    """A Google-shaped result: an outer wrapper, a title link, and its h3."""
    heading = Node("h3", text="Gödel's incompleteness theorems", nid=4)
    link = Node("a", attrs={"href": "/wiki/Godel"}, children=[heading], nid=3)
    row = Node("div", children=[link], nid=2, js=True)
    wrapper = Node("div", children=[row], nid=1, js=True)
    return wrapper, row, link


def test_wrapper_containers_collapse_to_the_one_real_result_link():
    wrapper, row, link = serp()
    assert page_reader._label(link) == "Gödel's incompleteness theorems"
    # A distant container must not claim the title, and must not reach the model
    # as a near-duplicate of the link it merely wraps.
    assert page_reader._heading_title(wrapper) == ""
    actions, _guards, diag = page_reader._actions({1: wrapper, 2: row, 3: link}, "T", "F")
    assert [a["node"] for a in actions] == [link.backend_node_id]
    assert diag["nested_duplicate"] == 2


def test_own_accessible_name_wins_over_a_nearby_heading():
    heading = Node("h3", text="Some result title", nid=9)
    link = Node("a", attrs={"aria-label": "Next page", "href": "/p2"}, children=[heading], nid=8)
    assert page_reader._label(link) == "Next page"


def test_result_title_link_survives_without_geometry():
    _wrapper, _row, link = serp()
    actions, guards, diag = page_reader._actions({3: link}, "T", "F")
    clicks = [a for a in actions if a["kind"] == "click"]
    assert clicks, f"result link was dropped: {diag}"
    assert clicks[0]["heading_label"] == "Gödel's incompleteness theorems"
    assert str(link.backend_node_id) in guards


def test_only_the_real_link_is_marked_a_result_title():
    wrapper, row, link = serp()
    actions, _guards, _diag = page_reader._actions({1: wrapper, 2: row, 3: link}, "T", "F")
    titles = [a for a in actions if a.get("heading_label")]
    assert len(titles) == 1
    assert titles[0]["node"] == link.backend_node_id


def test_dropped_candidates_are_reported_in_diagnostics():
    generic = Node("div", text="just a box", nid=7)
    _actions_out, _guards, diag = page_reader._actions({7: generic}, "T", "F")
    assert diag["not_semantic"] == 1
    assert diag["dropped_labels"][0]["reason"] == "not_semantic"


def test_result_links_are_not_hidden_by_a_populated_search_form():
    actions = [
        {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "godel",
         "node": 10, "form_node": 5},
        {"id": "e2", "kind": "click", "label": "Search", "role": "button", "value": "",
         "node": 11, "form_node": 5, "control_type": "submit"},
        {"id": "e3", "kind": "click", "label": "Gödel's incompleteness theorems", "role": "link",
         "value": "", "node": 12, "form_node": None, "heading_label": "Gödel's incompleteness theorems",
         "guard": {"href": "/wiki/Godel"}},
    ]
    _elements, targets, _controls = model.action_space(actions, [], "https://example.test/search?q=godel")
    assert "e3" in {a["id"] for a in targets["CLICK"].values()}


def search_page(*, results=True):
    """A Google-shaped SERP: query retained in the box, no detectable type=submit,
    and a dismiss-style control alongside the results."""
    actions = [
        {"id": "e1", "kind": "fill", "label": "Search", "role": "combobox", "value": "godel",
         "node": 10, "form_node": 5},
        {"id": "e2", "kind": "click", "label": "Close", "role": "button", "value": "",
         "node": 11, "form_node": 5, "guard": {}},
    ]
    if results:
        actions.append(
            {"id": "e3", "kind": "click", "label": "Gödel's incompleteness theorems", "role": "link",
             "value": "", "node": 12, "form_node": None,
             "heading_label": "Gödel's incompleteness theorems", "guard": {"href": "/wiki/Godel"}}
        )
    return actions


def test_a_dismiss_control_does_not_hide_visible_results():
    # An obstructed form plus a "Close" button must not reduce a SERP to that
    # one useless button: Jev would click it, stall, and report BLOCKED.
    _elements, targets, _controls = model.action_space(
        search_page(), [], "https://www.google.com/search?q=godel"
    )
    assert "e3" in {a["id"] for a in targets["CLICK"].values()}


def test_a_dismiss_control_still_wins_when_no_results_are_visible():
    _elements, targets, _controls = model.action_space(
        search_page(results=False)
        + [{"id": "e9", "kind": "click", "label": "Privacy policy", "role": "link", "value": "",
            "node": 19, "form_node": None, "guard": {}}],
        [],
        "https://www.google.com/",
    )
    assert {a["id"] for a in targets["CLICK"].values()} == {"e2"}


def test_a_result_title_link_survives_one_stale_no_progress():
    history = [{"choice": "e3", "no_progress": True, "from_url": "https://example.test/"}]
    actions = [
        {"id": "e3", "kind": "click", "label": "Result", "role": "link", "value": "", "node": 12,
         "form_node": None, "heading_label": "Result", "guard": {"href": "/a"}},
    ]
    _elements, targets, _controls = model.action_space(actions, history, "https://example.test/")
    assert targets["CLICK"]["1"]["id"] == "e3"


def test_a_result_title_link_is_suppressed_after_two_strikes():
    history = [
        {"choice": "e3", "no_progress": True, "from_url": "https://example.test/"},
        {"choice": "e3", "no_progress": True, "from_url": "https://example.test/"},
    ]
    actions = [
        {"id": "e3", "kind": "click", "label": "Result", "role": "link", "value": "", "node": 12,
         "form_node": None, "heading_label": "Result", "guard": {"href": "/a"}},
    ]
    _elements, targets, _controls = model.action_space(actions, history, "https://example.test/")
    assert "CLICK" not in targets


def test_a_new_tab_link_is_flagged_to_the_model():
    actions = [
        {"id": "e3", "kind": "click", "label": "QQ邮箱", "role": "link", "value": "", "node": 12,
         "form_node": None, "new_tab": True, "guard": {"href": "https://mail.qq.com/"}},
    ]
    payload, _t, _c, _o = model._provider_payload({
        "url": "https://example.test/", "title": "t", "text": "", "headings": [], "actions": actions,
    }, "open mail", [])
    criteria = payload["questions"]["click_target"]["criteria"]["1"]
    assert "opens a new tab" in criteria
    assert "will follow" in criteria


def test_an_ordinary_ineffective_click_is_still_suppressed():
    history = [{"choice": "e4", "no_progress": True, "from_url": "https://example.test/"}]
    actions = [
        {"id": "e4", "kind": "click", "label": "Dead", "role": "button", "value": "", "node": 13,
         "form_node": None, "guard": {}},
    ]
    _elements, targets, _controls = model.action_space(actions, history, "https://example.test/")
    assert "CLICK" not in targets
