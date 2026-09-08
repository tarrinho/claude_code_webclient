"""QA: the supervisor map's coordinate system and zoom wiring.

These execute the real web/assets/supervisor-map.js inside QuickJS against the
d3/DOM stub in tests/js/d3_dom_stub.js, for the reason test_frontend_syntax.py
and test_qa_transport_status.py both state for themselves: a Python
re-implementation of the geometry would keep passing after the JS that the
browser runs stopped agreeing with it.

Each test names the defect it pins down. All four were live in committed code:

1. Nodes were joined straight onto the root <svg>, and the zoom object was
   created with no `.on("zoom", ...)` handler at all -- so wheel, drag, Fit,
   +/- and the percentage buttons had nothing to move and every zoom control
   was inert. A transform on a root <svg> is not rendered, so a container
   group is the prerequisite for any of it working.
2. The root node was placed at a hardcoded `translate(200,200)` -- a fixed
   point in the SVG's own coordinates -- while its children were laid out
   polar about (0,0). The centre circle therefore sat detached from the tree
   radiating out of a corner.
3. The layout radius came from constants (400x400) rather than the panel the
   SVG actually occupies (measured at ~380x930 on this deployment), so the
   tree used a fraction of the available area.
4. The percentage buttons computed `currentTransform.x * target` -- a
   translation already in scaled pixels multiplied by a scale factor -- and
   subtracted it from the box centre. Pressing 100% from a panned view threw
   the map off-screen.

What these tests do NOT cover: whether the resulting picture looks right. The
stub does not implement d3's radial layout (see its header), and no browser
runs here. Visual confirmation is tests/test_frontend_browser.py's job.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - exercised only without the dev deps
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]
MAP_JS = ROOT / "web" / "assets" / "supervisor-map.js"
STUB_JS = Path(__file__).resolve().parent / "js" / "d3_dom_stub.js"

# A shape with all four node kinds the renderer branches on, so `each()` walks
# every branch rather than only the one a minimal fixture would reach.
SAMPLE_DATA = """
var DATA = {
  id: "root", label: "Console", status: "running", type: "center",
  children: [
    {
      id: "t1", label: "Transport A", status: "idle", type: "transport",
      children: [
        {id: "m1", label: "Machine 1", status: "idle", type: "machine",
         capacity_existing: 1, capacity_total: 4,
         children: [{id: "c1", label: "Chat 1", status: "running", type: "chat"}]}
      ]
    },
    {id: "m2", label: "Local", status: "idle", type: "machine",
     capacity_existing: 0, capacity_total: 3}
  ]
};
"""


def _module_source() -> str:
    """supervisor-map.js with module syntax removed so it runs as a script.

    Same three strips as test_frontend_syntax._parse, and for the same reason:
    `export` is only legal at a module's top level, and this is eval'd as a
    script so that its top-level functions land in the shared global scope
    where a probe appended after it can call them.
    """
    src = MAP_JS.read_text(encoding="utf-8")
    src = re.sub(r"^\s*import\s.*?;\s*$", "", src, flags=re.MULTILINE | re.DOTALL)
    src = re.sub(
        r"^\s*export\s+(?=(?:async\s+)?(?:function|class|const|let|var)\b)",
        "", src, flags=re.MULTILINE,
    )
    src = re.sub(r"^\s*export\s*\{.*?\};\s*$", "", src, flags=re.MULTILINE | re.DOTALL)
    src = re.sub(r"^\s*export\s+default\s+", "", src, flags=re.MULTILINE)
    # Top-level `let`/`const` in QuickJS script mode are block-scoped to the
    # eval, which would hide the module's own state from the probe. `var`
    # reaches the global object, which is what lets a probe read _viewport.
    src = re.sub(r"^(let|const)\s", "var ", src, flags=re.MULTILINE)
    return src


def _run(probe: str) -> dict:
    """Render the map once, then evaluate *probe* and return its JSON result."""
    script = "\n".join([
        STUB_JS.read_text(encoding="utf-8"),
        _module_source(),
        SAMPLE_DATA,
        probe,
    ])
    return json.loads(quickjs.Context().eval(script))


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapZoomWiringTests(unittest.TestCase):

    def test_a_zoom_handler_is_registered(self):
        """Defect 1: d3.zoom() was built with scaleExtent and nothing else."""
        out = _run("""
          renderSupervisorMap(DATA);
          JSON.stringify({handlers: STUB.zoomHandlers});
        """)
        self.assertIn("zoom", out["handlers"])

    def test_the_zoom_handler_moves_the_container_group(self):
        """The handler must write to the group, not to the <svg>: a transform
        attribute on a root <svg> element is ignored by the renderer."""
        out = _run("""
          renderSupervisorMap(DATA);
          var vp = stubFindByClass(stubSvg(), "map-viewport");
          var before = vp.__attrs.transform;
          // Drive the handler the way a wheel event would.
          _zoom.transform(stubSvg(), d3.zoomIdentity.translate(11, 22).scale(3));
          JSON.stringify({
            existed: !!vp,
            before: String(before),
            after: String(vp.__attrs.transform),
            svgHasTransform: stubSvg().__attrs.transform === undefined ? false : true
          });
        """)
        self.assertTrue(out["existed"])
        self.assertIn("translate(11,22)", out["after"])
        self.assertIn("scale(3)", out["after"])
        self.assertNotEqual(out["before"], out["after"])
        self.assertFalse(
            out["svgHasTransform"],
            "the <svg> itself must not carry the zoom transform - it is not rendered there",
        )

    def test_nodes_are_joined_onto_the_viewport_group(self):
        """Defect 1: `_svg.selectAll(".node")` put every node outside the
        group the zoom transform moves, so panning moved nothing."""
        out = _run("""
          renderSupervisorMap(DATA);
          var calls = STUB.selectAllCalls.filter(function (c) { return c.selector === ".node"; });
          var nodeSel = stubFindNodeSelection();
          JSON.stringify({calls: calls, foundUnderViewport: !!nodeSel});
        """)
        self.assertTrue(out["calls"], 'no selectAll(".node") happened at all')
        for call in out["calls"]:
            self.assertEqual(call["onClass"], "map-viewport")
        self.assertTrue(out["foundUnderViewport"])

    def test_the_percentage_buttons_set_an_absolute_scale(self):
        """Defect 4: data-zoom is an absolute level, so the call must be
        scaleTo(target) and must not synthesise a translation from it."""
        out = _run("""
          renderSupervisorMap(DATA);
          // Pan somewhere first: the old arithmetic only misbehaved from a
          // panned view, which is why it survived manual testing.
          _zoom.transform(stubSvg(), d3.zoomIdentity.translate(500, -300).scale(2));
          var btn = STUB.buttonHandlers[0];
          btn.fn.call(btn.el, {});
          var last = STUB.transforms[STUB.transforms.length - 1];
          JSON.stringify({scaleTo: STUB.scaleToCalls, last: last});
        """)
        self.assertEqual(out["scaleTo"], [1.0])
        self.assertEqual(out["last"]["k"], 1.0)
        for axis in ("x", "y"):
            self.assertEqual(
                out["last"][axis], {"x": 500, "y": -300}[axis],
                "an absolute zoom level must not move the view",
            )


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapGeometryTests(unittest.TestCase):

    def test_root_and_descendants_share_one_origin(self):
        """Defect 2: the root's transform was `translate(200,200)` while every
        other node was polar about (0,0)."""
        out = _run("""
          renderSupervisorMap(DATA);
          var nodeSel = stubFindNodeSelection();
          JSON.stringify({transforms: nodeSel.__computed.transform});
        """)
        transforms = out["transforms"]
        self.assertGreater(len(transforms), 1)
        self.assertNotIn(
            "translate(200,200)", transforms[0],
            "the root must not be pinned to a hardcoded point in SVG coordinates",
        )
        for t in transforms:
            self.assertRegex(t, r"^rotate\(-?[\d.]+\) translate\(-?[\d.]+,0\)$")
        # The root's radius is 0, so the shared formula already places it at
        # the group's origin -- that is what makes the special case removable.
        self.assertIn("translate(0,0)", transforms[0])

    def test_the_layout_radius_comes_from_the_measured_box(self):
        """Defect 3: RADIUS was min(400,400)/2 - 40 regardless of the panel."""
        out = _run("""
          STUB.svgBox = {width: 900, height: 600, left: 0, top: 0};
          renderSupervisorMap(DATA);
          JSON.stringify({
            size: STUB.treeSize,
            viewBox: stubSvg().__attrs.viewBox,
            bgWidth: stubFindByClass(stubSvg(), "map-bg").__attrs.width,
            bgHeight: stubFindByClass(stubSvg(), "map-bg").__attrs.height
          });
        """)
        self.assertEqual(out["viewBox"], "0 0 900 600")
        # min(900, 600)/2 - 40
        self.assertEqual(out["size"][1], 260)
        self.assertAlmostEqual(out["size"][0], 2 * 3.141592653589793, places=6)
        # The rect exists to catch pointer events; sized to 400x400 it covered
        # only the top corner of a 930px-tall panel.
        self.assertEqual(out["bgWidth"], 900)
        self.assertEqual(out["bgHeight"], 600)

    def test_an_unmeasurable_box_falls_back_instead_of_collapsing(self):
        """A hidden or detached panel reports width 0. Deriving the radius
        from that would put every node at radius 0 -- one unclickable dot."""
        out = _run("""
          STUB.svgBox = {width: 0, height: 0, left: 0, top: 0};
          renderSupervisorMap(DATA);
          JSON.stringify({size: STUB.treeSize, viewBox: stubSvg().__attrs.viewBox});
        """)
        self.assertEqual(out["viewBox"], "0 0 400 400")
        self.assertEqual(out["size"][1], 160)

    def test_the_initial_view_is_centred_and_finite(self):
        """zoomToFit runs at the end of every render. It used to call
        `_tree.bounds()`, which is not a d3 API, so it always threw and the
        map was never fitted. Its replacement must produce usable numbers."""
        out = _run("""
          renderSupervisorMap(DATA);
          // Recompute the tree's own bounds from the laid-out nodes, using the
          // same polar-to-Cartesian conversion the node transforms use. The
          // assertion below is that the fit maps this centre onto the centre
          // of the box the SVG actually drew -- checking only that the numbers
          // are finite and inside the box would pass just as happily while
          // fitting to a stale 400x400 rectangle in the corner.
          var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
          _root.descendants().forEach(function (d) {
            var theta = d.x - Math.PI / 2;
            var px = d.y * Math.cos(theta), py = d.y * Math.sin(theta);
            if (px < minX) minX = px;
            if (px > maxX) maxX = px;
            if (py < minY) minY = py;
            if (py > maxY) maxY = py;
          });
          var vb = stubSvg().__attrs.viewBox.split(" ");
          JSON.stringify({
            last: STUB.transforms[STUB.transforms.length - 1],
            count: STUB.transforms.length,
            boxW: parseFloat(vb[2]),
            boxH: parseFloat(vb[3]),
            cx: (minX + maxX) / 2,
            cy: (minY + maxY) / 2
          });
        """)
        last = out["last"]
        self.assertGreater(out["count"], 0, "no zoom transform was ever applied")
        for key in ("k", "x", "y"):
            self.assertIsInstance(last[key], (int, float))
            self.assertEqual(last[key], last[key], f"{key} is NaN")
        self.assertGreater(last["k"], 0)
        self.assertAlmostEqual(
            last["x"], out["boxW"] / 2 - out["cx"] * last["k"], places=6,
            msg="the fit must centre the tree in the box the SVG drew",
        )
        self.assertAlmostEqual(
            last["y"], out["boxH"] / 2 - out["cy"] * last["k"], places=6,
            msg="the fit must centre the tree in the box the SVG drew",
        )


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapNodeKindTests(unittest.TestCase):
    """The three node kinds the renderer had no branch for.

    The backend now emits `task` (previously mislabelled `chat`), `session`
    (previously not emitted at all) and `more` (previously a silent `[:4]`
    slice). Without matching branches here, all three fall through to the
    generic small-filled-circle case and a task is indistinguishable from a
    conversation.
    """

    def test_a_task_and_a_session_can_open_the_drawer(self):
        out = _run('JSON.stringify({'
                   'chat: _hasDetail("chat"), machine: _hasDetail("machine"),'
                   'task: _hasDetail("task"), session: _hasDetail("session")});')
        self.assertEqual(out, {"chat": True, "machine": True,
                               "task": True, "session": True})

    def test_the_overflow_marker_has_no_drawer(self):
        """It stands for nodes that are not in this response, so a drawer
        could only repeat the count already on its label."""
        out = _run('JSON.stringify({more: _hasDetail("more")});')
        self.assertFalse(out["more"])

    def test_a_session_is_drawn_as_a_square(self):
        """Sessions are shells a person is sitting at, not work the console
        started. Telling them apart at a glance is the point of the map."""
        out = _run("""
          renderSupervisorMap({
            id: "root", label: "You", status: "running", type: "center",
            children: [{id: "s1", label: "cweb1", status: "running", type: "session"}]
          });
          var nodeSel = stubFindNodeSelection();
          var shapes = [];
          (nodeSel.__children || []).forEach(function (per) {
            (per.__children || []).forEach(function (c) { shapes.push(c.__tag); });
          });
          JSON.stringify({shapes: shapes});
        """)
        self.assertIn("rect", out["shapes"])

    def test_the_overflow_marker_is_hollow_and_dashed(self):
        """It must not read as a thing that is running."""
        out = _run("""
          renderSupervisorMap({
            id: "root", label: "You", status: "running", type: "center",
            children: [{id: "direct-more", label: "+3 more", status: "idle",
                        type: "more", hidden_count: 3}]
          });
          var nodeSel = stubFindNodeSelection();
          var circles = [];
          (nodeSel.__children || []).forEach(function (per) {
            (per.__children || []).forEach(function (c) {
              if (c.__tag === "circle") circles.push(c.__attrs);
            });
          });
          JSON.stringify({circles: circles});
        """)
        marker = [c for c in out["circles"] if c.get("fill") == "none"]
        self.assertTrue(marker, out["circles"])
        self.assertTrue(any(c.get("stroke-dasharray") for c in marker))


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapTeardownTests(unittest.TestCase):

    def test_closing_the_map_drops_the_viewport_handle(self):
        """closeSupervisorMap removes every child of the <svg>. Keeping the
        old group in `_viewport` would leave the zoom handler writing to a
        detached node after a close/reopen."""
        out = _run("""
          renderSupervisorMap(DATA);
          closeSupervisorMap();
          JSON.stringify({viewport: _viewport === null});
        """)
        self.assertTrue(out["viewport"])

    def test_an_empty_map_still_gets_a_viewbox(self):
        """The empty branch is a second, separate SVG setup. Left without a
        viewBox it configured the element differently from the populated
        branch, so the first real render inherited a stale coordinate space."""
        out = _run("""
          renderSupervisorMap({id: "root", children: []});
          JSON.stringify({
            viewBox: stubSvg().__attrs.viewBox,
            emptyShown: document.getElementById("mapStatusEmpty").hidden === false
          });
        """)
        self.assertEqual(out["viewBox"], "0 0 900 600")
        self.assertTrue(out["emptyShown"])


if __name__ == "__main__":
    unittest.main()
