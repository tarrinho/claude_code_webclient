"""The supervisor page's four panels must all be reachable.

``web/supervisor.html`` shipped a stray ``</div>`` that closed ``#layout``
before ``#panel-right`` opened, so the Task Detail panel became a sibling of
<body>. At 1440x900 it rendered at y=813 in an 813px viewport, and ``body``
sets ``overflow:hidden``, so it could never be scrolled to.
``renderTaskDetail()`` wrote correct HTML into an element no user could see,
and clicking a task in the tree read as a no-op.

Geometry is asserted rather than DOM presence: "present but at y=813 in an
813px viewport" would satisfy a naive visibility check while being invisible
to a person. The div-balance assertion is the regression guard -- it fails on
the original fault directly, independent of any rendering.
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
SUPERVISOR_HTML = REPO / "web" / "supervisor.html"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")

PANELS = ("#panel-left", "#panel-center", "#panel-right", "#panel-bottom")


def body_markup() -> str:
    """The <body> of supervisor.html with HTML comments stripped.

    Comments are removed because they contain tags -- a commented-out </div>
    would otherwise be counted by the balance check below.
    """
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", html[html.index("<body"):], flags=re.S)


def div_depth_trace() -> tuple[int, dict[str, int]]:
    """Return (final depth, {panel id: depth at which it opens}).

    Final depth 0 means balanced. A panel opening at a different depth from
    its siblings is the ejection this module exists to catch.
    """
    depth = 0
    opens: dict[str, int] = {}
    for match in re.finditer(r"<(/?)div\b([^>]*)>", body_markup()):
        if match.group(1):
            depth -= 1
            continue
        depth += 1
        found = re.search(r'id="([^"]+)"', match.group(2))
        if found:
            opens[found.group(1)] = depth
    return depth, opens


def probe_geometry(width: int = 1440, height: int = 900,
                   body_class: str = "") -> dict[str, str]:
    """Render the real page headlessly and report each panel's box.

    The app script is stripped, and that is load-bearing rather than tidiness.
    Chromium does not exit when supervisor.js is loaded under --dump-dom -- it
    hangs until the timeout, which is what has test_qa_supervisor_ux_shortcuts
    failing 27 of 28 at the time of writing. Measured by cweb5:

        supervisor.html alone, no supervisor.js    rc=0,   1s
        supervisor.html + supervisor.js, no stubs  rc=124, hangs

    Stripping it also removes API fetches that would fail with no server, and
    every assertion here is decided by markup and CSS alone. If this probe ever
    starts timing out, check whether the script tag is being stripped before
    looking anywhere else.

    Results come back through document.title because --dump-dom is the only
    channel headless chromium offers without a debugging port.
    """
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    html = re.sub(r'<script src="supervisor\.js[^"]*"></script>', "", html)
    if body_class:
        html = html.replace("<body>", f'<body class="{body_class}">', 1)
    probe = """
<script>
window.addEventListener("load", function () {
  var out = {};
  ["#layout", "#shell", "#panel-left", "#panel-center",
   "#panel-right", "#panel-bottom"].forEach(function (sel) {
    var el = document.querySelector(sel);
    if (!el) { out[sel] = "MISSING"; return; }
    var r = el.getBoundingClientRect();
    out[sel] = [Math.round(r.x), Math.round(r.y),
                Math.round(r.width), Math.round(r.height)].join(",");
  });
  var pr = document.querySelector("#panel-right");
  out["panel-right.parent"] = pr
    ? (pr.parentElement.id || pr.parentElement.tagName) : "MISSING";
  out["viewport"] = window.innerWidth + "," + window.innerHeight;
  document.title = JSON.stringify(out);
});
</script></body>"""
    html = html.replace("</body>", probe, 1)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             f"--window-size={width},{height}",
             "--virtual-time-budget=4000", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>(.*?)</title>", result.stdout, re.S)
    if not match:
        raise AssertionError("probe produced no title; chromium output: "
                             + result.stdout[:400])
    return json.loads(match.group(1))


def box(geometry: dict[str, str], selector: str) -> tuple[int, int, int, int]:
    raw = geometry[selector]
    if raw == "MISSING":
        raise AssertionError(f"{selector} is not in the document")
    x, y, w, h = (int(v) for v in raw.split(","))
    return x, y, w, h


class MarkupNestingTests(unittest.TestCase):
    """Decided by the markup alone, so it runs without a browser."""

    def test_the_body_divs_balance(self):
        """The original fault was a stray closer; -1 is its signature."""
        depth, _ = div_depth_trace()
        self.assertEqual(depth, 0,
                         "unbalanced <div> nesting in supervisor.html body")

    def test_panel_right_is_a_child_of_layout(self):
        """The ejection itself. #panel-right must sit with its siblings."""
        _, opens = div_depth_trace()
        self.assertIn("panel-right", opens)
        self.assertEqual(
            opens["panel-right"], opens["panel-left"],
            "#panel-right opens at a different depth from #panel-left, so it "
            "is not a sibling of the other panels",
        )

    def test_panel_bottom_is_not_inside_the_layout_row(self):
        """It is styled as a full-width bar (border-top, and max-bottom uses
        left:0/right:0). As a child of the horizontal row it rendered 168x200
        at x=1272, and at 1024px wide it pushed #panel-right off the right
        edge entirely. This fails if it is ever moved back.
        """
        _, opens = div_depth_trace()
        self.assertIn("panel-bottom", opens)
        self.assertEqual(
            opens["panel-bottom"], opens["layout"],
            "#panel-bottom must be a sibling of #layout, not a child of it",
        )


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class PanelGeometryTests(unittest.TestCase):
    """Every panel must be on screen, not merely in the document."""

    @classmethod
    def setUpClass(cls):
        cls.geo = probe_geometry()

    def test_panel_right_reports_layout_as_its_parent(self):
        self.assertEqual(self.geo["panel-right.parent"], "layout")

    def test_every_panel_is_inside_the_viewport(self):
        """y=813 in an 813px viewport is the bug, and has non-zero size."""
        self._assert_all_on_screen(self.geo)

    def test_every_panel_is_inside_a_smaller_viewport(self):
        """1024x768. The panels have fixed 320px side widths and 300px centre
        minimum, so a narrower viewport is where a wrong flex basis shows up
        as an overflow that 1440px hides. Not a responsive test -- that is
        sub-project C; this only asserts nothing escapes the viewport.
        """
        self._assert_all_on_screen(probe_geometry(width=1024, height=768))

    def _assert_all_on_screen(self, geo: dict[str, str]):
        vw, vh = (int(v) for v in geo["viewport"].split(","))
        for selector in PANELS:
            with self.subTest(panel=selector, viewport=f"{vw}x{vh}"):
                x, y, w, h = box(geo, selector)
                self.assertGreater(w, 0, f"{selector} has zero width")
                self.assertGreater(h, 0, f"{selector} has zero height")
                self.assertLess(y, vh,
                                f"{selector} starts below the viewport")
                self.assertLessEqual(y + h, vh + 1,
                                     f"{selector} extends past the viewport")
                self.assertLessEqual(x + w, vw + 1,
                                     f"{selector} extends past the right edge")


if __name__ == "__main__":
    unittest.main()
