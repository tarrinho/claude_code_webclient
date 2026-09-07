"""Two ways the auto-answer controls were shipped and correct on disk, yet
invisible or wrong on screen. Neither was caught by the source-inspection
tests in test_qa_auto_answer_ui.py, because both bugs were about what a
browser actually renders, not about what the markup and JS say.

1. Cache-buster miss (1ddb00a -> be65700). The toggle and popover landed in
   app.js and styles.css without bumping either file's `?v=` query, so a
   browser that had either asset cached from before that commit kept serving
   the old file and the controls never appeared. Reported live by the user.

2. Positioning against the page instead of the button (be65700 -> 6cb4bbc).
   .auto-answer-menu is position:absolute with no positioned ancestor in
   .strip-right, so it resolved against the page and rendered near the
   bottom of the viewport instead of under the info button. Also reported
   live -- "The history should appear next to the button. And not at the
   bottom."

Both are the class of bug this repo has a standing pattern for:
tests/test_qa_supervisor_layout.py asserts panel geometry through a real
headless render rather than trusting source inspection, for exactly this
reason -- "present in the DOM" and "correctly positioned on screen" are
different claims.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
ASSETS = WEB / "assets"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


# ── Cache-buster floor ───────────────────────────────────────────────────────
#
# This does not catch every future omission -- nothing short of hashing the
# asset into the query string would -- but it pins the floor this bug was
# fixed at, so a revert or a bad merge that drops the bump without touching
# the number is caught.

class CacheBusterTests(unittest.TestCase):
    def setUp(self):
        self.html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_app_js_carries_a_cache_buster_at_or_above_the_fix(self):
        match = re.search(r"app\.js\?v=(\d+)", self.html)
        self.assertIsNotNone(match, "app.js must carry a version query")
        self.assertGreaterEqual(
            int(match.group(1)), 26,
            "app.js?v= must be at least 26 -- the auto-answer toggle shipped "
            "in 1ddb00a without bumping it, which is why a browser with the "
            "old file cached never saw the new controls",
        )

    def test_styles_css_carries_a_cache_buster_at_or_above_the_fix(self):
        match = re.search(r"styles\.css\?v=(\d+)", self.html)
        self.assertIsNotNone(match, "styles.css must carry a version query")
        self.assertGreaterEqual(
            int(match.group(1)), 28,
            "styles.css?v= must be at least 28 -- the popover's positioning "
            "fix in 6cb4bbc changed this file and needed a second bump on "
            "top of be65700's",
        )


# ── Popover geometry ─────────────────────────────────────────────────────────

def probe_geometry(width: int = 1440, height: int = 900) -> dict[str, str]:
    """Render the real index.html + styles.css headlessly and report the
    auto-answer info button's and menu's boxes.

    app.js is not loaded: the controls are forced visible by an injected
    script instead, since driving the real toggle would need a live backend.
    This means the test is decided by markup and CSS alone -- which is
    exactly where both regressions above live.
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (ASSETS / "styles.css").read_text(encoding="utf-8")
    html = re.sub(r'<link rel="stylesheet"[^>]*>',
                  lambda _m: f"<style>{css}</style>", html)
    html = re.sub(r'<script type="module"[^>]*></script>', "", html)

    probe = """
<script>
window.addEventListener("load", function () {
  document.getElementById("workspaceStrip").style.display = "flex";
  ["autoAnswerToggle", "autoAnswerInfo", "autoAnswerMenu"].forEach(function (id) {
    document.getElementById(id).hidden = false;
  });
  var out = {};
  ["autoAnswerInfo", "autoAnswerMenu", "runState"].forEach(function (id) {
    var r = document.getElementById(id).getBoundingClientRect();
    out[id] = [Math.round(r.x), Math.round(r.y),
               Math.round(r.width), Math.round(r.height)].join(",");
  });
  // The menu's positioning anchor, measured by class because it has no id:
  // .auto-answer-menu is position:absolute inside .auto-answer-group's
  // position:relative, so the group -- not the info badge -- is what its
  // top/right resolve against. See the alignment tests below.
  var g = document.querySelector(".auto-answer-group").getBoundingClientRect();
  out.autoAnswerGroup = [Math.round(g.x), Math.round(g.y),
                         Math.round(g.width), Math.round(g.height)].join(",");
  out.viewport = window.innerWidth + "," + window.innerHeight;
  document.title = JSON.stringify(out);
});
</script></body>"""
    html = html.replace("</body>", probe, 1)

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             # Points chromium's profile inside the TemporaryDirectory so it
             # is removed even if chromium has to be killed. Without this a
             # single run of a chromium-driven suite leaked a ~126 MB profile
             # per invocation and filled a 1.9 GB tmpfs earlier in this work.
             f"--user-data-dir={page.parent}/chrome-profile",
             f"--window-size={width},{height}",
             "--virtual-time-budget=3000", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    match = re.search(r"<title>(.*?)</title>", result.stdout, re.DOTALL)
    if not match:
        raise AssertionError("probe produced no title; chromium output: "
                             + result.stdout[:400])
    return json.loads(match.group(1))


def box(geometry: dict[str, str], key: str) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in geometry[key].split(","))
    return x, y, w, h


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class AutoAnswerMenuGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geo = probe_geometry()

    def test_the_menu_opens_directly_under_the_info_button(self):
        """The regression itself. Before the fix this menu rendered near the
        foot of the viewport; now it sits a few pixels below the button.
        """
        # Against the group, for the same reason as the alignment test below:
        # the menu is `top:calc(100% + 6px)` of the group, while the info badge
        # is a 14px corner badge pulled up to `top:-6px`, so its bottom edge
        # sits well above the group's and measuring from it inflated the gap
        # by the badge's own offset rather than by any drift.
        _gx, gy, _gw, gh = box(self.geo, "autoAnswerGroup")
        _mx, my, _mw, _mh = box(self.geo, "autoAnswerMenu")
        anchor_bottom = gy + gh
        self.assertGreater(my, anchor_bottom,
                           "the menu must open below the button, not overlap it")
        self.assertLess(
            my - anchor_bottom, 20,
            f"menu top is {my - anchor_bottom}px below the anchor's bottom "
            f"edge -- more than a small gap means it has drifted away from "
            f"the button again, the way it did when it had no positioned "
            f"ancestor and rendered near the bottom of the page",
        )

    def test_the_menu_is_not_pinned_to_the_page_bottom(self):
        """Direct regression check for the reported bug: with no positioned
        ancestor, top:100% resolved against the page and the menu rendered
        far down the viewport regardless of where the button was.
        """
        _mx, my, _mw, _mh = box(self.geo, "autoAnswerMenu")
        _vw, vh = (int(v) for v in self.geo["viewport"].split(","))
        self.assertLess(
            my, vh * 0.5,
            "the menu rendered in the bottom half of the viewport, which is "
            "the shape of the original bug regardless of the button's position",
        )

    def test_the_menu_aligns_with_the_button_it_belongs_to(self):
        """Right edges matching is what "next to the button" means here --
        the popover is right-aligned under the control that opens it.

        Measured against `.auto-answer-group`, not `#autoAnswerInfo`. The menu
        is `right:0` inside the group, so the group is what it aligns to. The
        info badge is deliberately `right:-6px` -- a corner badge overhanging
        the robot icon, asserted by
        `test_the_info_badge_sits_on_the_icons_top_right_corner` in
        test_frontend_browser.py -- so requiring the menu to match the *badge*
        contradicts that on purpose and failed by exactly the 6px of overhang.
        The badge moving to the corner is the newer, tested intent; this
        assertion's original premise predates it.
        """
        gx, _gy, gw, _gh = box(self.geo, "autoAnswerGroup")
        mx, _my, mw, _mh = box(self.geo, "autoAnswerMenu")
        self.assertAlmostEqual(gx + gw, mx + mw, delta=2,
                               msg="the menu's right edge must line up with "
                                   "the right edge of the group it is "
                                   "anchored to")

    def test_the_menu_does_not_land_past_run_state(self):
        """Guards the specific wrong anchor: .strip-right's own right edge is
        past #run-state, since that span is the last child in the group. A
        menu anchored to .strip-right instead of its own wrapper would sit to
        the right of the status dot rather than under the info button.
        """
        rx, _ry, rw, _rh = box(self.geo, "runState")
        mx, _my, mw, _mh = box(self.geo, "autoAnswerMenu")
        self.assertLessEqual(
            mx + mw, rx + rw,
            "the menu's right edge is past #runState's -- it has drifted "
            "back to being anchored against .strip-right as a whole",
        )


if __name__ == "__main__":
    unittest.main()
