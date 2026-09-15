"""QA: a zoom event arriving after the map is torn down must not throw.

Reported from the browser console, repeating many times after opening the
supervisor map:

    supervisor-map.js:229 Uncaught TypeError:
      Cannot read properties of null (reading 'attr')
        at SVGSVGElement.<anonymous>
        ... zoomToFit -> renderSupervisorMap -> _loadMap -> _openMap

Line 229 is the first statement of the zoom handler,
``_viewport.attr("transform", event.transform)``.

The sequence. ``zoomToFit`` ends every render with a 300ms transition on the
svg (``_svg.transition().call(_zoom.transform, ...)``), and d3 drives that
transition frame by frame, emitting a zoom event each time -- which is why the
reported stack has a long ladder of ``requestAnimationFrame`` entries.
``_loadMap`` calls ``closeSupervisorMap()`` before each render, and that nulls
``_viewport`` while leaving the zoom behaviour bound to the svg. Every
remaining frame of the in-flight transition then reached the handler with
nothing to transform, throwing once per frame until the transition finished.
Anything that re-enters _loadMap while a fit is still animating does it: a
second open, or the map's own 10-second poll.

These execute the real web/assets/supervisor-map.js inside QuickJS against the
d3/DOM stub, so the crash is reproduced rather than described -- the first of
them fails with exactly the reported TypeError when the guard is removed. The
stub's zoom object is captured by wrapping ``d3.zoom`` before the render,
which is how a test can deliver the event that only a real transition would
otherwise produce.

Both halves are asserted. The guard makes a stale frame harmless; the
interrupt in closeSupervisorMap stops those frames being emitted at all. The
guard is what these tests can execute -- the stub's selections have no
``interrupt`` -- so the interrupt is asserted on source, and the production
call is written defensively for the same reason.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_qa_supervisor_map_geometry import _run, quickjs

ROOT = Path(__file__).resolve().parents[1]
MAP_JS = ROOT / "web" / "assets" / "supervisor-map.js"

_CAPTURE_ZOOM = """
  var captured = null;
  var origZoom = d3.zoom;
  d3.zoom = function () { var z = origZoom(); captured = z; return z; };
"""


@unittest.skipIf(quickjs is None, "quickjs not installed (requirements-dev.txt)")
class StaleZoomEventTests(unittest.TestCase):

    def test_a_zoom_event_after_teardown_does_not_throw(self):
        """The reported crash, reproduced. Without the guard this fails with
        "cannot read property 'attr' of null" -- the browser's wording differs,
        the defect is the same."""
        out = _run(_CAPTURE_ZOOM + """
          renderSupervisorMap(DATA);
          closeSupervisorMap();
          var threw = null;
          try { captured.__handlers.zoom.call(null, {transform: d3.zoomIdentity}); }
          catch (e) { threw = String(e); }
          JSON.stringify({threw: threw});
        """)
        self.assertIsNone(
            out["threw"],
            "a zoom frame arriving after the map was torn down threw; this is "
            "the console error the user reported, once per animation frame",
        )

    def test_the_handler_still_works_while_the_map_is_open(self):
        """The guard must not turn zooming into a no-op: an open map has a
        viewport, and the transform has to reach it or the map stops panning
        and scaling entirely."""
        out = _run(_CAPTURE_ZOOM + """
          renderSupervisorMap(DATA);
          var t = d3.zoomIdentity.translate(11, 22).scale(2);
          captured.__handlers.zoom.call(null, {transform: t});
          var vp = stubFindByClass(stubSvg(), "map-viewport");
          JSON.stringify({transform: vp && vp.__attrs && vp.__attrs.transform});
        """)
        self.assertIsNotNone(
            out["transform"],
            "the viewport never received the transform, so the guard is "
            "swallowing live zooms as well as stale ones",
        )
        self.assertIn("2", str(out["transform"]))

    def test_a_render_after_teardown_recovers(self):
        """Tearing down and drawing again is the normal path -- _loadMap does
        exactly that on every poll -- so the guard must not leave the map
        permanently inert."""
        out = _run(_CAPTURE_ZOOM + """
          renderSupervisorMap(DATA);
          closeSupervisorMap();
          renderSupervisorMap(DATA);
          var t = d3.zoomIdentity.translate(5, 6).scale(3);
          captured.__handlers.zoom.call(null, {transform: t});
          var vp = stubFindByClass(stubSvg(), "map-viewport");
          JSON.stringify({transform: vp && vp.__attrs && vp.__attrs.transform});
        """)
        self.assertIsNotNone(
            out["transform"],
            "after a close and a fresh render the zoom no longer reaches the "
            "viewport",
        )


class TeardownInterruptsAnimationTests(unittest.TestCase):
    """Asserted on source: the stub's selections have no `interrupt`, so this
    half cannot be executed here. It is the half that stops the stale frames
    being produced in the first place, rather than tolerating them."""

    def setUp(self):
        self.src = MAP_JS.read_text(encoding="utf-8")

    def _close_fn(self) -> str:
        match = re.search(
            r"export function closeSupervisorMap\(.*?\n\}", self.src, re.DOTALL)
        self.assertIsNotNone(match, "closeSupervisorMap moved or was renamed")
        return match.group(0)

    def test_teardown_interrupts_transitions_on_the_svg(self):
        self.assertRegex(
            self._close_fn(), r"_svg\.interrupt\(\)",
            "teardown does not stop in-flight transitions, so zoom frames "
            "keep arriving after the state they need is cleared",
        )

    def test_the_interrupt_runs_before_the_state_is_cleared(self):
        """Interrupting after nulling _viewport would still let the frames
        already queued for this tick run against a torn-down map."""
        body = self._close_fn()
        self.assertLess(
            body.index("interrupt()"), body.index("_viewport = null"),
            "the interrupt happens after _viewport is cleared, which is the "
            "window the crash lives in",
        )

    def test_the_call_tolerates_a_selection_without_interrupt(self):
        """This module holds selections from more than one source, and a
        teardown that throws would leave the map half torn down."""
        self.assertRegex(
            self._close_fn(),
            r"typeof _svg\.interrupt === \"function\"",
            "the interrupt is called unguarded",
        )


if __name__ == "__main__":
    unittest.main()
