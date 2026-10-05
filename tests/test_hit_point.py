"""Where the pre-execution check decides to click.

A search result's title is an inline <a> wrapping an <h3>, laid out on two
lines: the site's url above, the title below. getBoundingClientRect() returns
the union of those two line boxes, and the union's centre falls in the leading
between them -- a gap that belongs to whatever element is painted there, not to
the link. Taking that one point as the only candidate refused the correct link
three times in a row on a live run, reported as "the page changed".

Measured on https://www.google.com/search (viewport 1470x745):

    union box      y=323 h=45     centre [490, 345] -> hit=span   refused
    line box 0     y=352 h=16     centre [490, 360] -> hit=h3     accepted
    line box 1     y=323 h=16     centre [303, 331] -> hit=cite   accepted

These tests run the function straight out of browser.py's source against pages
built here, so they neither copy the shipped code nor depend on Google's
markup. They need the browser Browser Harness already talks to; without it
there is nothing to measure and they skip.
"""

import json
import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "jev_ultrafast" / "browser.py"


def validation_js():
    """The pre-execution check, lifted from the module that ships it."""
    match = re.search(
        r'functionDeclaration="""(function\(action\) \{.*?\})""",', SOURCE.read_text(), re.S
    )
    assert match, "the validation function moved; this test must follow it"
    return match.group(1)


# An inline link whose text wraps onto a second line. Its client rects are
# font-metric fragment boxes and carry no leading, so generous line-height
# leaves a gap between them that belongs to the container instead:
#
#   union  y=53 h=71   centre [173, 88] -> hits #row     (the gap)
#   rect 0 y=53 h=23   centre [173, 65] -> hits the link
#   rect 1 y=101 h=23  centre [84, 113] -> hits the link
#
# Same mechanism as the search result measured in the module docstring, built
# from layout rules rather than from one site's markup.
WRAPPED_LINK = """
<style>
  body { margin:0; font:16px/3 sans-serif; }
  #row { position:relative; width:300px; padding:40px 0 0 40px; }
</style>
<div id="row">
  <a id="subject" href="#gone">a title long enough to wrap onto a second line</a>
</div>
"""
# The leading between the two fragment boxes, where the union centre lands.
GAP = range(77, 101)

PLAIN_BUTTON = """
<style>body{margin:0}#subject{position:absolute;left:30px;top:30px;width:160px;height:40px}</style>
<button id="subject">Search</button>
"""

COVERED_BUTTON = """
<style>
  body{margin:0}
  #subject{position:absolute;left:30px;top:30px;width:160px;height:40px}
  #sheet{position:fixed;inset:0;background:rgba(0,0,0,.4)}
</style>
<button id="subject">Search</button>
<div id="sheet"></div>
"""

EMPTY_BOX = """
<style>body{margin:0}#subject{position:absolute;left:0;top:0;width:0;height:0;overflow:hidden}</style>
<a id="subject" href="#gone"></a>
"""

OFFSCREEN_LINK = """
<style>body{margin:0}#subject{position:absolute;left:30px;top:4000px}</style>
<a id="subject" href="#gone">Far below the fold</a>
"""

# The classic off-canvas hiding idiom: present in the DOM, not on the screen.
OFF_CANVAS_LINK = """
<style>body{margin:0}#subject{position:absolute;left:-9999px;top:20px;width:200px;height:40px}</style>
<a id="subject" href="#gone">Hidden off to the left</a>
"""

CLICK = {"id": "probe", "kind": "click", "guard": {"tag": "a", "value": None}}
CLICK_BUTTON = {"id": "probe", "kind": "click", "guard": {"tag": "button", "value": None}}


@pytest.fixture(scope="module")
def cdp():
    """The browser Browser Harness drives, or a skip when it is not reachable."""
    helpers = pytest.importorskip("browser_harness.helpers")
    try:
        helpers.cdp("Target.getTargets")
    except Exception as error:  # daemon down, Chrome closed, approval pending
        pytest.skip(f"no browser to measure layout in: {type(error).__name__}: {error}")
    return helpers.cdp


@pytest.fixture
def judge(cdp):
    """Build a page, then report what the check decides about #subject.

    Returns None for a refusal -- the value browser_operation turns into
    StalePage -- or the click point it settled on. Opens a scratch tab per
    call and closes only the tabs it opened itself.
    """
    opened = []

    def run(body, action=CLICK, *, js=None):
        before = {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t["type"] == "page"}
        target = cdp("Target.createTarget", url="about:blank")["targetId"]
        opened.append((target, before))
        session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        cdp(
            "Runtime.evaluate",
            session_id=session,
            expression=f"document.write({json.dumps(body)}); document.close(); 1",
            returnByValue=True,
        )
        handle = cdp(
            "Runtime.evaluate", session_id=session, expression="document.getElementById('subject')"
        )
        object_id = handle["result"]["objectId"]
        result = cdp(
            "Runtime.callFunctionOn",
            session_id=session,
            objectId=object_id,
            functionDeclaration=js or validation_js(),
            arguments=[{"value": action}],
            returnByValue=True,
        )
        assert not result.get("exceptionDetails"), json.dumps(result.get("exceptionDetails"))[:300]
        return result.get("result", {}).get("value")

    yield run
    for target, before in opened:
        now = {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t["type"] == "page"}
        if target in now:
            cdp("Target.closeTarget", targetId=target)
        for extra in now - before - {target}:
            cdp("Target.closeTarget", targetId=extra)


def test_a_link_wrapped_onto_two_lines_is_clickable(judge):
    point = judge(WRAPPED_LINK)
    assert point is not None, "the result-title shape was refused; this is the live failure"


def test_the_chosen_point_lies_on_a_line_of_the_link_not_in_the_leading(judge):
    point = judge(WRAPPED_LINK)
    assert point["y"] not in GAP, f"clicked in the leading at y={point['y']}"


def test_the_union_centre_alone_would_have_refused_this_link(judge):
    # Proves the page reproduces the live failure, so the test above is not
    # passing for some unrelated reason.
    previous = """function(action) {
      const e=this;
      const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
      if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
      const root=e.getRootNode();
      const hit=(root.elementFromPoint ? root.elementFromPoint(x,y) : document.elementFromPoint(x,y));
      if (!(hit===e || e.contains(hit))) return null;
      return {x,y};
    }"""
    assert judge(WRAPPED_LINK, js=previous) is None


def test_an_ordinary_button_still_gets_its_centre(judge):
    point = judge(PLAIN_BUTTON, CLICK_BUTTON)
    assert point == {"x": 110, "y": 50}


def test_a_covered_control_is_still_refused(judge):
    # Every candidate point lands on the overlay, so trying more points must
    # not weaken occlusion detection.
    assert judge(COVERED_BUTTON, CLICK_BUTTON) is None


# The three refusals below assert the outcome, not which check produces it.
# Measured: for each of these shapes the geometry filters and the hit test
# refuse independently -- an out-of-viewport point makes elementFromPoint
# return null, and a 0x0 box's centre lands on whatever is painted behind it.
# Removing a single filter therefore leaves them refused, so they pin the
# behaviour the run depends on rather than one line of the implementation.
def test_an_element_with_no_box_is_still_refused(judge):
    assert judge(EMPTY_BOX) is None


def test_an_element_below_the_fold_is_still_refused(judge):
    # The run scrolls first; it does not click at coordinates it cannot see.
    assert judge(OFFSCREEN_LINK) is None


def test_an_element_hidden_off_canvas_is_still_refused(judge):
    assert judge(OFF_CANVAS_LINK) is None


def test_a_guard_mismatch_is_still_refused(judge):
    # Geometry succeeding must not let a changed element through.
    assert judge(WRAPPED_LINK, {**CLICK, "guard": {"tag": "button", "value": None}}) is None
