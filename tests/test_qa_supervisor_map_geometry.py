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

    # test_the_percentage_buttons_set_an_absolute_scale removed with the
    # five explicit-percentage zoom buttons (50/100/150/200/500%) at Pedro's
    # request. Fit-to-view plus the continuous +/- pair covers the same
    # range; see web/index.html's zoom-controls and supervisor-map.js's
    # button wiring.


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapGeometryTests(unittest.TestCase):

    # The four tests below were written for the radial layout and are rewritten
    # here for the horizontal one (2026-09-10). They were failing against
    # correct code -- they asserted `rotate(theta) translate(r,0)` transforms, a
    # 2*pi angular span, and a radius of min(w,h)/2 - 40, none of which a
    # horizontal tree produces. The defects they were written to catch are named
    # in each docstring and still pinned, because those defects are about
    # deriving geometry from the measured box and not pinning nodes to constants
    # -- both of which still apply, just in different coordinates.

    def test_root_and_descendants_share_one_origin(self):
        """Defect 2: the root's transform was `translate(200,200)` while every
        other node was positioned by the shared formula.

        Still the property worth pinning after the restructure -- only the
        formula changed. Every node, root included, is placed by
        `translate(marginLeft + d.x + horizontalGap, marginTop + d.y)`, so the
        root lands on that formula's own output for d.x = d.y = 0 rather than
        on a constant somebody chose.
        """
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
            self.assertRegex(
                t, r"^translate\(-?[\d.]+, -?[\d.]+\)$",
                "every node is placed by one cartesian formula; a rotate() "
                "here means the radial layout has come back in part",
            )
        # marginLeft 60 + horizontalGap 30, marginTop 40, at d.x = d.y = 0.
        self.assertEqual(transforms[0], "translate(90, 40)")
        self.assertGreater(
            len(set(transforms)), 1,
            "every node landed on the same point, so the layout ran but "
            "placed nothing",
        )

    def test_the_view_box_and_backdrop_come_from_the_measured_box(self):
        """Defect 3, in its surviving form: geometry that must follow the
        panel's real size still does.

        The radius this test used to check is gone -- node spacing is fixed now
        (see the next test) -- but the viewBox and the pointer-catching rect are
        still derived from `getBoundingClientRect`, and those were the parts
        that made a 400x400 assumption visible to the reader.
        """
        out = _run("""
          STUB.svgBox = {width: 900, height: 600, left: 0, top: 0};
          renderSupervisorMap(DATA);
          JSON.stringify({
            viewBox: stubSvg().__attrs.viewBox,
            bgWidth: stubFindByClass(stubSvg(), "map-bg").__attrs.width,
            bgHeight: stubFindByClass(stubSvg(), "map-bg").__attrs.height
          });
        """)
        self.assertEqual(out["viewBox"], "0 0 900 600")
        # The rect exists to catch pointer events; sized to 400x400 it covered
        # only the top corner of a 930px-tall panel.
        self.assertEqual(out["bgWidth"], 900)
        self.assertEqual(out["bgHeight"], 600)

    def test_the_layout_spacing_is_fixed_and_not_a_fit(self):
        """What replaced the radius, and the defect that came with it.

        The restructure called `.size([...])` and then `.nodeSize([20, 30])` on
        the same layout. Those are mutually exclusive in d3-hierarchy -- one
        flag, two setters, both writing dx/dy, later call wins -- so the
        `.size()` values were overwritten and the canvas dimensions never
        reached the layout while the code read as though they did. The dead
        call is removed; this asserts which of the two is in force so it cannot
        be quietly added back alongside.
        """
        out = _run("""
          STUB.svgBox = {width: 900, height: 600, left: 0, top: 0};
          renderSupervisorMap(DATA);
          JSON.stringify({
            size: STUB.treeSize,
            nodeSize: STUB.treeNodeSize,
            calls: STUB.treeCalls
          });
        """)
        self.assertEqual(
            out["nodeSize"], [20, 30],
            "20px between siblings, 30px between levels",
        )
        self.assertIsNone(out["size"])
        # Asserted on the calls, not on the values, and that distinction was
        # found by mutation. Re-adding `.size(...)` before `.nodeSize(...)`
        # leaves `tree.size()` reading back null exactly as it does now -- d3
        # behaves that way and so does the stub -- so every value-based
        # assertion here passed against the defect this test exists to catch.
        # The only observable difference is that the setter was called.
        sizing = [c for c in out["calls"] if c in ("size", "nodeSize")]
        self.assertEqual(
            sizing, ["nodeSize"],
            f"the layout should use exactly one sizing setter; saw {sizing}. "
            f"size() and nodeSize() are mutually exclusive in d3, so calling "
            f"both means one of them is dead code that reads as though it "
            f"applies",
        )

    def test_an_unmeasurable_box_falls_back_instead_of_collapsing(self):
        """A hidden or detached panel reports width 0.

        Under the radial layout this was sharp: the radius came from the box,
        so a zero box put every node at radius 0 -- one unclickable dot. Fixed
        spacing removes that failure mode by construction, which is worth
        asserting rather than assuming: the nodes must still be spread even
        when nothing can be measured. The viewBox still needs its fallback,
        and it is WIDTH x HEIGHT (800x400), not the 400x400 this test was
        written against.
        """
        out = _run("""
          STUB.svgBox = {width: 0, height: 0, left: 0, top: 0};
          renderSupervisorMap(DATA);
          var nodeSel = stubFindNodeSelection();
          JSON.stringify({
            nodeSize: STUB.treeNodeSize,
            viewBox: stubSvg().__attrs.viewBox,
            transforms: nodeSel.__computed.transform
          });
        """)
        self.assertEqual(out["viewBox"], "0 0 800 400")
        self.assertEqual(out["nodeSize"], [20, 30])
        self.assertGreater(
            len(set(out["transforms"])), 1,
            "an unmeasurable panel collapsed every node onto one point",
        )

    def test_the_initial_view_is_centred_and_finite(self):
        """zoomToFit runs at the end of every render. It used to call
        `_tree.bounds()`, which is not a d3 API, so it always threw and the
        map was never fitted. Its replacement must produce usable numbers."""
        out = _run("""
          renderSupervisorMap(DATA);
          // Recompute the tree's own bounds from the laid-out nodes. The
          // horizontal layout puts d.x and d.y in cartesian coordinates
          // already, so there is no polar conversion to mirror -- this used to
          // apply one, matching the radial transforms, and reproducing it here
          // now would measure a shape the map never draws.
          //
          // The assertion below is that the fit maps this centre onto the
          // centre of the box the SVG actually drew. Checking only that the
          // numbers are finite and inside the box would pass just as happily
          // while fitting to a stale rectangle in the corner.
          var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
          _root.descendants().forEach(function (d) {
            if (d.x < minX) minX = d.x;
            if (d.x > maxX) maxX = d.x;
            if (d.y < minY) minY = d.y;
            if (d.y > maxY) maxY = d.y;
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
class MapCollapseTests(unittest.TestCase):
    """Collapsing a node used to be impossible rather than temporary.

    The click handler recorded the id in `_collapsed`, moved the node's
    children aside and re-rendered -- and the re-render cleared `_collapsed`
    and rebuilt the hierarchy from the data with every child present. So the
    only effect of clicking a branch was a full redraw that changed nothing,
    which reads exactly like an unresponsive control.
    """

    def _collapse_first_branch(self) -> str:
        return """
          renderSupervisorMap(DATA);
          var nodeSel = stubFindNodeSelection();
          var before = nodeSel.__data.length;
          // The transport node: the first datum with children under the root.
          var branch = nodeSel.__data.filter(function (d) {
            return d.depth === 1 && d.children;
          })[0];
          nodeSel.__handlers.click.call(null, {stopPropagation: function () {}}, branch);
          var after = stubFindNodeSelection().__data.length;
        """

    def test_a_collapsed_branch_stays_collapsed_through_the_re_render(self):
        out = _run(self._collapse_first_branch() + """
          JSON.stringify({before: before, after: after});
        """)
        self.assertLess(
            out["after"], out["before"],
            "collapsing removed no nodes from the tree",
        )

    def test_a_collapsed_branch_stays_collapsed_through_a_refresh(self):
        """The map now refreshes itself on a timer. A refresh that silently
        re-expanded everything would undo the user's choice every few
        seconds."""
        out = _run(self._collapse_first_branch() + """
          renderSupervisorMap(DATA);   // what the poll does
          var refreshed = stubFindNodeSelection().__data.length;
          JSON.stringify({after: after, refreshed: refreshed});
        """)
        self.assertEqual(out["refreshed"], out["after"])

    def test_clicking_it_again_expands_it(self):
        out = _run(self._collapse_first_branch() + """
          var again = stubFindNodeSelection().__data.filter(function (d) {
            return d.depth === 1 && d._children;
          })[0];
          stubFindNodeSelection().__handlers.click.call(
            null, {stopPropagation: function () {}}, again);
          JSON.stringify({before: before, reopened: stubFindNodeSelection().__data.length});
        """)
        self.assertEqual(out["reopened"], out["before"])

    def test_closing_the_map_forgets_what_was_collapsed(self):
        """Reopening is a fresh look at the tree, not a resumed session."""
        out = _run(self._collapse_first_branch() + """
          closeSupervisorMap();
          renderSupervisorMap(DATA);
          JSON.stringify({before: before,
                          reopened: stubFindNodeSelection().__data.length});
        """)
        self.assertEqual(out["reopened"], out["before"])


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapViewPersistenceTests(unittest.TestCase):
    """The view survives a refresh but not a close."""

    def test_a_refresh_keeps_the_readers_own_zoom(self):
        """Re-fitting on every poll tick would yank someone who has zoomed
        into a branch back out to the whole tree, every few seconds, without
        being asked."""
        out = _run("""
          renderSupervisorMap(DATA);
          _zoom.transform(stubSvg(), d3.zoomIdentity.translate(70, 40).scale(2.5));
          var chosen = STUB.transforms[STUB.transforms.length - 1];
          renderSupervisorMap(DATA);   // what the poll does
          var after = STUB.transforms[STUB.transforms.length - 1];
          JSON.stringify({chosen: chosen, after: after});
        """)
        self.assertEqual(out["after"]["k"], out["chosen"]["k"])
        self.assertEqual(out["after"]["x"], out["chosen"]["x"])
        self.assertEqual(out["after"]["y"], out["chosen"]["y"])

    def test_reopening_the_map_fits_the_tree_again(self):
        """The one place a view is deliberately forgotten."""
        out = _run("""
          renderSupervisorMap(DATA);
          _zoom.transform(stubSvg(), d3.zoomIdentity.translate(70, 40).scale(2.5));
          var chosen = STUB.transforms[STUB.transforms.length - 1];
          closeSupervisorMap();
          renderSupervisorMap(DATA);
          var after = STUB.transforms[STUB.transforms.length - 1];
          JSON.stringify({chosen: chosen, after: after});
        """)
        self.assertNotEqual(out["after"]["k"], out["chosen"]["k"])


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapDrawerActionTests(unittest.TestCase):
    """Which actions the drawer offers, per node kind.

    Offering the wrong one is not cosmetic: a task's id is a task id, so
    "Open conversation" on a task node would request a conversation that does
    not exist, and Stop posts to a per-conversation endpoint.
    """

    def _actions_for(self, node_js: str) -> dict:
        return _run(f"""
          renderSupervisorMap(DATA);
          showDetail({node_js});
          JSON.stringify({{
            open: document.getElementById("mapDetailOpen").hidden,
            stop: document.getElementById("mapDetailStop").hidden
          }});
        """)

    def test_a_running_conversation_offers_both(self):
        out = self._actions_for('{id: "c1", type: "chat", status: "running", label: "C"}')
        self.assertFalse(out["open"])
        self.assertFalse(out["stop"])

    def test_an_idle_conversation_cannot_be_stopped(self):
        """There is nothing to stop, and a button that reports "Nothing was
        running" every time is noise."""
        out = self._actions_for('{id: "c1", type: "chat", status: "idle", label: "C"}')
        self.assertFalse(out["open"])
        self.assertTrue(out["stop"])

    def test_a_task_offers_neither(self):
        out = self._actions_for('{id: "t1", type: "task", status: "busy", label: "T"}')
        self.assertTrue(out["open"])
        self.assertTrue(out["stop"])

    def test_a_machine_offers_neither(self):
        out = self._actions_for('{id: "m1", type: "machine", status: "running", label: "M"}')
        self.assertTrue(out["open"])
        self.assertTrue(out["stop"])

    def test_a_terminal_session_offers_neither(self):
        """It lives in a terminal; the console has no conversation for it."""
        out = self._actions_for('{id: "s1", type: "session", status: "running", label: "S"}')
        self.assertTrue(out["open"])
        self.assertTrue(out["stop"])

    def test_opening_a_conversation_asks_app_js_by_event(self):
        """app.js imports this module, so importing selectChat back would be
        a cycle. The event is the seam, and it must carry the id."""
        out = _run("""
          var seen = null;
          document.dispatchEvent = function (e) { seen = e; };
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          STUB.elHandlers["mapDetailOpen"].click();
          JSON.stringify({type: seen && seen.type, id: seen && seen.detail.id});
        """)
        self.assertEqual(out["type"], "wc:map-open-chat")
        self.assertEqual(out["id"], "c1")

    def test_closing_the_drawer_forgets_which_node_it_described(self):
        """A stale node would let a later button press act on whatever was
        open last."""
        out = _run("""
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          var during = _detailNode && _detailNode.id;
          hideDetail();
          JSON.stringify({during: during, after: _detailNode});
        """)
        self.assertEqual(out["during"], "c1")
        self.assertIsNone(out["after"])


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapThemeTests(unittest.TestCase):
    """Colours come from CSS variables, not from literals.

    The map was the one SVG in this app that ignored the theme. Its node
    outlines were `#fff` -- a white ring on a white panel in light mode, so
    invisible on exactly the theme where an outline matters most.
    """

    def _with_theme(self, values: dict[str, str], probe: str) -> dict:
        table = json.dumps(values)
        return _run(f"""
          var THEME = {table};
          getComputedStyle = function () {{
            return {{getPropertyValue: function (name) {{
              return THEME[name] || "";
            }}}};
          }};
          {probe}
        """)

    def test_a_status_colour_is_read_from_its_variable(self):
        out = self._with_theme(
            {"--map-running": " #abcdef "},
            'JSON.stringify({running: statusColor("running")});',
        )
        self.assertEqual(out["running"], "#abcdef")

    def test_an_unset_variable_falls_back_to_the_literal(self):
        """A stylesheet that has not loaded must not produce empty fills."""
        out = self._with_theme(
            {}, 'JSON.stringify({running: statusColor("running")});',
        )
        self.assertEqual(out["running"], "#10b981")

    def test_an_unknown_status_uses_the_idle_colour(self):
        out = self._with_theme(
            {"--map-idle": "#123456"},
            'JSON.stringify({odd: statusColor("no-such-status")});',
        )
        self.assertEqual(out["odd"], "#123456")

    def test_node_outlines_follow_the_theme(self):
        """The specific literal that was wrong."""
        out = self._with_theme({"--map-outline": "#222222"}, """
          renderSupervisorMap(DATA);
          var strokes = [];
          (stubFindNodeSelection().__children || []).forEach(function (per) {
            (per.__children || []).forEach(function (c) {
              if (c.__attrs.stroke) strokes.push(c.__attrs.stroke);
            });
          });
          JSON.stringify({strokes: strokes});
        """)
        self.assertIn("#222222", out["strokes"])
        self.assertNotIn("#fff", out["strokes"])

    def test_the_colours_are_re_read_on_every_render(self):
        """The theme toggle changes the variables under a page that is already
        loaded, so reading them once at module load would leave the map on the
        colours of whichever theme happened to be active first."""
        out = self._with_theme({"--map-outline": "#111111"}, """
          renderSupervisorMap(DATA);
          THEME["--map-outline"] = "#999999";
          renderSupervisorMap(DATA);
          var strokes = [];
          (stubFindNodeSelection().__children || []).forEach(function (per) {
            (per.__children || []).forEach(function (c) {
              if (c.__attrs.stroke) strokes.push(c.__attrs.stroke);
            });
          });
          JSON.stringify({strokes: strokes});
        """)
        self.assertIn("#999999", out["strokes"])
        self.assertNotIn("#111111", out["strokes"])


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapMachineCountTests(unittest.TestCase):
    """The centre tooltip's machine count.

    It filtered the root's direct children and took `.length`, so it counted
    transport groups that contain a machine rather than machines: three
    backends behind one transport reported "1 machine". Underreporting
    capacity is the wrong direction for a number whose whole job is telling
    the user how much of the fleet is in play.
    """

    FLEET = """
      var FLEET = {
        id: "root", label: "You", status: "running", type: "center",
        children: [
          {id: "t1", label: "T1", status: "idle", type: "transport", children: [
            {id: "m1", label: "M1", status: "idle", type: "machine"},
            {id: "m2", label: "M2", status: "idle", type: "machine"},
            {id: "m3", label: "M3", status: "idle", type: "machine"}
          ]},
          {id: "direct", label: "Direct", status: "idle", type: "transport", children: [
            {id: "m4", label: "M4", status: "idle", type: "machine"},
            {id: "c1", label: "Chat", status: "idle", type: "chat"}
          ]}
        ]
      };
    """

    def _tooltip(self, extra: str = "") -> str:
        out = _run(self.FLEET + f"""
          renderSupervisorMap(FLEET);
          var nodeSel = stubFindNodeSelection();
          var root = nodeSel.__data.filter(function (d) {{ return d.depth === 0; }})[0];
          {extra}
          nodeSel.__handlers.mouseenter.call(
            null, {{pageX: 0, pageY: 0}}, root);
          JSON.stringify({{text: document.getElementById("mapTooltip").textContent}});
        """)
        return out["text"]

    def test_every_machine_is_counted_not_every_group(self):
        self.assertIn("4 machines", self._tooltip())

    def test_the_group_count_is_separate_from_the_machine_count(self):
        text = self._tooltip()
        self.assertIn("2 groups", text)
        self.assertIn("4 machines", text)

    def test_collapsing_a_branch_does_not_lose_its_machines(self):
        """A count that dropped when the user collapsed something would read
        as the map losing track of the fleet."""
        text = self._tooltip("""
          var branch = nodeSel.__data.filter(function (d) {
            return d.depth === 1 && d.children;
          })[0];
          nodeSel.__handlers.click.call(null, {stopPropagation: function () {}}, branch);
          nodeSel = stubFindNodeSelection();
          root = nodeSel.__data.filter(function (d) { return d.depth === 0; })[0];
        """)
        self.assertIn("4 machines", text)


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class MapAccessibilityTests(unittest.TestCase):
    """What a screen reader and a keyboard reach."""

    def test_the_figure_names_itself(self):
        """Without role and aria-label the map arrives as a stack of
        unlabelled groups with no statement of what the figure is. Each node
        already carries its own label; only the figure can say how big it is."""
        out = _run("""
          renderSupervisorMap(DATA);
          JSON.stringify({role: stubSvg().__attrs.role,
                          label: stubSvg().__attrs["aria-label"]});
        """)
        self.assertEqual(out["role"], "tree")
        self.assertIn("Supervisor map", out["label"])
        self.assertIn("2 groups", out["label"])

    def test_an_empty_map_is_labelled_too(self):
        out = _run("""
          renderSupervisorMap({id: "root", children: []});
          JSON.stringify({role: stubSvg().__attrs.role,
                          label: stubSvg().__attrs["aria-label"]});
        """)
        self.assertEqual(out["role"], "tree")
        self.assertIn("nothing running", out["label"])

    def test_zoom_animations_are_dropped_under_reduced_motion(self):
        """styles.css already neutralises CSS transitions, but d3 animates
        attributes from JavaScript and that rule cannot reach it -- so the
        zoom controls kept animating for a reader who had asked them not to."""
        out = _run("""
          globalThis.window = {matchMedia: function () { return {matches: true}; }};
          JSON.stringify({reduced: _motionMs(300)});
        """)
        self.assertEqual(out["reduced"], 0)

    def test_animations_are_kept_when_motion_is_not_restricted(self):
        out = _run("""
          globalThis.window = {matchMedia: function () { return {matches: false}; }};
          JSON.stringify({normal: _motionMs(300)});
        """)
        self.assertEqual(out["normal"], 300)

    def test_a_browser_without_matchmedia_still_animates(self):
        """Failing closed here would silently remove the animation for
        everyone on an older browser."""
        out = _run('JSON.stringify({fallback: _motionMs(300)});')
        self.assertEqual(out["fallback"], 300)

    def test_opening_the_drawer_moves_focus_into_it(self):
        """It used to open with focus left wherever it was, so a keyboard
        reader had to Tab through the whole map to reach a panel that had just
        appeared in front of them."""
        out = _run("""
          var drawer = document.getElementById("mapDetailDrawer");
          var closeBtn = document.getElementById("mapDetailClose");
          drawer.__focusables = [closeBtn];
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          JSON.stringify({focused: STUB.focusCalls});
        """)
        self.assertIn("mapDetailClose", out["focused"])

    def test_closing_the_drawer_returns_focus_to_where_it_was(self):
        out = _run("""
          var drawer = document.getElementById("mapDetailDrawer");
          var closeBtn = document.getElementById("mapDetailClose");
          var origin = document.getElementById("someNode");
          drawer.__focusables = [closeBtn];
          document.body.__focusables = [origin, closeBtn];
          document.activeElement = origin;
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          STUB.focusCalls = [];
          hideDetail();
          JSON.stringify({focused: STUB.focusCalls});
        """)
        self.assertEqual(out["focused"], ["someNode"])

    def test_focus_is_not_handed_to_an_element_that_left_the_page(self):
        """A re-render replaces every node, so the element the drawer was
        opened from may no longer exist by the time it closes."""
        out = _run("""
          var drawer = document.getElementById("mapDetailDrawer");
          var closeBtn = document.getElementById("mapDetailClose");
          var origin = document.getElementById("goneNode");
          drawer.__focusables = [closeBtn];
          document.body.__focusables = [closeBtn];   // origin is not in it
          document.activeElement = origin;
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          STUB.focusCalls = [];
          hideDetail();
          JSON.stringify({focused: STUB.focusCalls});
        """)
        self.assertEqual(out["focused"], [])

    def test_tab_at_the_last_control_wraps_to_the_first(self):
        """Tab used to walk straight out of the drawer into the map behind
        it, which is still on screen."""
        out = _run("""
          var drawer = document.getElementById("mapDetailDrawer");
          var first = document.getElementById("mapDetailClose");
          var last = document.getElementById("mapDetailStop");
          drawer.__focusables = [first, last];
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          document.activeElement = last;
          var prevented = false;
          STUB.focusCalls = [];
          STUB.elHandlers["mapDetailDrawer"].keydown({
            key: "Tab", shiftKey: false,
            preventDefault: function () { prevented = true; }
          });
          JSON.stringify({prevented: prevented, focused: STUB.focusCalls});
        """)
        self.assertTrue(out["prevented"])
        self.assertEqual(out["focused"], ["mapDetailClose"])

    def test_shift_tab_at_the_first_control_wraps_to_the_last(self):
        out = _run("""
          var drawer = document.getElementById("mapDetailDrawer");
          var first = document.getElementById("mapDetailClose");
          var last = document.getElementById("mapDetailStop");
          drawer.__focusables = [first, last];
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          document.activeElement = first;
          STUB.focusCalls = [];
          STUB.elHandlers["mapDetailDrawer"].keydown({
            key: "Tab", shiftKey: true, preventDefault: function () {}
          });
          JSON.stringify({focused: STUB.focusCalls});
        """)
        self.assertEqual(out["focused"], ["mapDetailStop"])

    def test_tab_between_controls_is_left_to_the_browser(self):
        """Only the two edges are redirected; the browser already moves focus
        correctly in between, and intercepting that would fight it."""
        out = _run("""
          var drawer = document.getElementById("mapDetailDrawer");
          var first = document.getElementById("mapDetailClose");
          var mid = document.getElementById("mapDetailOpen");
          var last = document.getElementById("mapDetailStop");
          drawer.__focusables = [first, mid, last];
          renderSupervisorMap(DATA);
          showDetail({id: "c1", type: "chat", status: "running", label: "C"});
          document.activeElement = mid;
          var prevented = false;
          STUB.focusCalls = [];
          STUB.elHandlers["mapDetailDrawer"].keydown({
            key: "Tab", shiftKey: false,
            preventDefault: function () { prevented = true; }
          });
          JSON.stringify({prevented: prevented, focused: STUB.focusCalls});
        """)
        self.assertFalse(out["prevented"])
        self.assertEqual(out["focused"], [])


class MapThemeStylesheetTests(unittest.TestCase):
    """The other half of the theme change, which no JS test can see.

    supervisor-map.js falls back to its old dark literals when a variable is
    unset -- deliberately, so a stylesheet that has not loaded does not
    produce empty fills. That fallback also means a missing light-theme value
    fails silently: the map keeps drawing dark-theme greens on a white panel
    and every JS test still passes. So the stylesheet is checked here.
    """

    CSS = ROOT / "web" / "assets" / "styles.css"
    INDEX = ROOT / "web" / "index.html"
    # --map-error and --map-done point at existing semantic variables (--bad,
    # --muted) which the light theme already redefines, so they are declared
    # once in :root and correctly absent from the light block.
    LIGHT_OVERRIDES = (
        "--map-running", "--map-busy", "--map-waiting", "--map-idle",
        "--map-transport", "--map-machine",
    )

    def _block(self, selector: str) -> str:
        css = self.CSS.read_text(encoding="utf-8")
        start = css.index(selector)
        return css[start:css.index("}", start)]

    def test_every_variable_the_map_reads_is_declared(self):
        root = self._block(":root {")
        source = (ROOT / "web" / "assets" / "supervisor-map.js").read_text(
            encoding="utf-8"
        )
        for name in sorted(set(re.findall(r'"(--map-[a-z-]+)"', source))):
            with self.subTest(variable=name):
                self.assertIn(name, root, f"{name} is read by the map but never declared")

    def test_the_light_theme_overrides_the_status_colours(self):
        """The dark greens and ambers are chosen against #1c2230 and are too
        light on a white panel; an automatic flip is not what this needs."""
        light = self._block('html[data-theme="light"] {')
        for name in self.LIGHT_OVERRIDES:
            with self.subTest(variable=name):
                self.assertIn(name, light)

    def test_the_stylesheet_cache_bust_covers_this_change(self):
        """A browser holding an older styles.css would get the new JS reading
        variables that its cached stylesheet does not define -- and the
        fallback would hide it."""
        html = self.INDEX.read_text(encoding="utf-8")
        match = re.search(r"styles\.css\?v=(\d+)", html)
        self.assertIsNotNone(match, "styles.css is loaded without a cache-bust")
        self.assertGreaterEqual(
            int(match.group(1)), 40,
            "styles.css?v= must be at least 40 -- the version that first "
            "declared the --map-* variables supervisor-map.js reads",
        )


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
