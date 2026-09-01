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
    return re.sub(r"<!--.*?-->", "", html[html.index("<body"):], flags=re.DOTALL)


def page_css() -> str:
    """Every rule that styles the page: inlined `<style>` plus any `<link>`ed
    stylesheet, concatenated.

    Written this way for a move that has not happened yet.
    `docs/superpowers/specs/2026-09-01-file-structure-design.md` step 5 drops
    `supervisor.html`'s inlined stylesheet block, so a test that greps this file
    for a CSS rule would start failing on a change that broke nothing. Reading
    both places means the assertions survive the extraction and keep asserting
    the same property.
    """
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.DOTALL))
    for path in linked_stylesheets():
        css += "\n" + path.read_text(encoding="utf-8")
    return css


def linked_stylesheets() -> list[Path]:
    """Filesystem paths of the page's `<link rel=stylesheet>` targets."""
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    found = []
    for href in re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html):
        path = (SUPERVISOR_HTML.parent / href.split("?", 1)[0].lstrip("/")).resolve()
        if path.exists():
            found.append(path)
    return found


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
        # Inline the page's linked stylesheets rather than relying on relative
        # hrefs resolving. The page is copied to a temp directory, so a
        # `<link href="/assets/styles.css">` would 404 -- silently, exactly as
        # the relative `supervisor.js` tag does -- and every geometry
        # assertion below would then measure an unstyled page and fail for a
        # reason that has nothing to do with the layout.
        #
        # There are no linked stylesheets today; the CSS is inlined. This
        # exists because file-structure step 5 removes that inlined block, and
        # this probe should survive the move rather than be a reason not to
        # make it.
        extra = "".join(
            f"<style>{p.read_text(encoding='utf-8')}</style>"
            for p in linked_stylesheets()
        )
        if extra:
            page.write_text(html.replace("</head>", extra + "</head>", 1),
                            encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             # Chromium writes a ~126 MB profile per launch. Without this it
             # picks its own /tmp/org.chromium.Chromium.scoped_dir.* and
             # leaves it behind, so a single run of this file leaked 11 of
             # them and filled a 1.9 GB tmpfs -- after which every browser
             # test in the suite fails on a timeout and leaks another.
             f"--user-data-dir={page.parent}/chrome-profile",
             f"--window-size={width},{height}",
             "--virtual-time-budget=4000", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>(.*?)</title>", result.stdout, re.DOTALL)
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


class EventLogResetTests(unittest.TestCase):
    """One supervisor's events must not appear under another's name.

    Source-level, because the behaviour is a single ordering property:
    selectSupervisor must clear the log before showActiveSupervisor repopulates
    it. Driving it in a browser would need the whole API surface stubbed to
    assert one statement's presence and position.
    """

    def setUp(self):
        self.source = (REPO / "web" / "supervisor.js").read_text(
            encoding="utf-8")

    def _select_supervisor_body(self) -> str:
        start = self.source.index("function selectSupervisor(")
        brace = self.source.index("{", start)
        depth, pos = 1, brace + 1
        while depth:
            if self.source[pos] == "{":
                depth += 1
            elif self.source[pos] == "}":
                depth -= 1
            pos += 1
        return self.source[brace + 1:pos - 1]

    def test_the_log_is_cleared_on_switch(self):
        body = self._select_supervisor_body()
        self.assertIn("eventLog.length = 0", body,
                      "the eventLog array must be truncated on switch")
        self.assertRegex(
            body, r'el\.eventLog\.innerHTML\s*=\s*""',
            "the #event-log element must be emptied on switch")

    def test_it_is_cleared_before_the_panel_repopulates(self):
        """Clearing after showActiveSupervisor would erase the new log."""
        body = self._select_supervisor_body()
        self.assertLess(
            body.index("eventLog.length = 0"),
            body.index("showActiveSupervisor()"),
            "the log must be cleared before the new supervisor is shown",
        )


class ResizeDragTests(unittest.TestCase):
    """A drag must survive the pointer leaving the 5px handle.

    The listeners were bound to the handle itself, so any drag quicker than the
    pointer could stay inside it stopped mid-gesture with no sign of why. This
    mattered little while only the side handles existed and became a
    first-use failure once the Event Log's handle was wired.
    """

    def setUp(self):
        self.source = (REPO / "web" / "supervisor.js").read_text(
            encoding="utf-8")

    def test_the_drag_listeners_are_on_document(self):
        self.assertIn('document.addEventListener("mousemove", onResizeMove)',
                      self.source)
        self.assertIn('document.addEventListener("mouseup", onResizeEnd)',
                      self.source)

    def test_they_are_not_bound_to_the_handle(self):
        self.assertNotIn('newHandle.addEventListener("mousemove"', self.source)
        self.assertNotIn('newHandle.addEventListener("mouseup"', self.source)

    def test_they_are_released_when_the_drag_ends(self):
        """Document-level listeners outlive the gesture unless removed, so
        without this every drag would stack another pair.
        """
        start = self.source.index("function onResizeEnd()")
        body = self.source[start:start + 600]
        self.assertIn(
            'document.removeEventListener("mousemove", onResizeMove)', body)
        self.assertIn(
            'document.removeEventListener("mouseup", onResizeEnd)', body)


class ResizeHandleWiringTests(unittest.TestCase):
    """The Event Log's handle must exist, since its JS already did.

    onResizeMove has a complete `resizing === "bottom"` branch computing
    height from window.innerHeight - e.clientY, and reinitResizeHandles
    already selects a row-resize cursor for it. No element carried
    data-resize="bottom", so the whole path was dead code.
    """

    def test_the_bottom_handle_exists(self):
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        self.assertIn('data-resize="bottom"', html)
        self.assertIn("resize-handle horizontal", html)

    def test_the_horizontal_variant_is_styled(self):
        """Without this rule the handle keeps width:5px/col-resize and sits on
        the wrong edge with the wrong cursor.

        Reads page_css(), not the HTML, so the assertion follows the rule when
        the inlined stylesheet block is extracted.
        """
        css = page_css()
        self.assertIn(".resize-handle.horizontal", css)
        self.assertRegex(css, r"\.resize-handle\.horizontal\s*\{[^}]*"
                              r"cursor:\s*row-resize")

    def test_panel_bottom_is_a_containing_block(self):
        """The handle is position:absolute. Without position on #panel-bottom
        it resolves against the initial containing block and lands at the top
        of the page rather than on the bar's top edge.
        """
        rule = re.search(r"#panel-bottom\s*\{(.*?)\}", page_css(), re.DOTALL)
        self.assertIsNotNone(rule, "#panel-bottom rule not found")
        self.assertRegex(rule.group(1), r"position:\s*relative")


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class MaximizeTests(unittest.TestCase):
    """Each body.max-* class must fill the area below the topbar.

    These rules use `position:absolute; top:44px`, resolved against the
    viewport. Giving #shell a position, transform, filter or contain would
    re-root them silently: the page looks right at rest and every maximize
    button lands in the wrong place. This is the assertion that catches it,
    and it is the reason #shell carries a comment forbidding those properties.
    """

    TOPBAR_H = 44

    def _assert_fills_below_topbar(self, panel: str, body_class: str):
        geo = probe_geometry(body_class=body_class)
        vw, vh = (int(v) for v in geo["viewport"].split(","))
        x, y, w, h = box(geo, panel)
        self.assertEqual(y, self.TOPBAR_H,
                         f"{panel} under {body_class} does not start at the "
                         f"topbar's lower edge")
        self.assertEqual(x, 0, f"{panel} under {body_class} is inset")
        self.assertEqual(w, vw, f"{panel} under {body_class} is not full width")
        self.assertEqual(h, vh - self.TOPBAR_H,
                         f"{panel} under {body_class} does not fill the height")

    def test_max_left(self):
        self._assert_fills_below_topbar("#panel-left", "max-left")

    def test_max_center(self):
        self._assert_fills_below_topbar("#panel-center", "max-center")

    def test_max_right(self):
        self._assert_fills_below_topbar("#panel-right", "max-right")

    def test_max_bottom(self):
        """Checked without asserting y: its rule anchors with bottom:0 and a
        height calc rather than a top offset.
        """
        geo = probe_geometry(body_class="max-bottom")
        vw, vh = (int(v) for v in geo["viewport"].split(","))
        x, _y, w, h = box(geo, "#panel-bottom")
        self.assertEqual(x, 0)
        self.assertEqual(w, vw)
        self.assertEqual(h, vh - self.TOPBAR_H,
                         "max-bottom sets height:calc(100vh - 44px)")


if __name__ == "__main__":
    unittest.main()
