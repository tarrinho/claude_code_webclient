"""QA: the d3 stub must not be more permissive than d3.

`tests/js/d3_dom_stub.js` exists so `supervisor-map.js` can be exercised
without a browser. That only works while the stub *refuses* what d3 refuses.
A stub that quietly accepts more than the real library turns the whole
harness into a source of false confidence: the tests pass, and the module
behaves differently in front of a user.

The concrete case, found 2026-09-10. The horizontal-layout restructure made
`supervisor-map.js` chain `.nodeSize()`, which the stub's tree layout did not
implement, so every test in `test_qa_supervisor_map_geometry.py` -- all 35 --
died at render with `TypeError: not a function`. The harness had stopped
exercising the module at all while looking like an ordinary batch of failures.

Adding `.nodeSize()` fixed 31 of them, and immediately exposed why fidelity is
the property worth pinning: `supervisor-map.js:221-224` calls `.size()` *and
then* `.nodeSize()` on the same layout. In d3-hierarchy those are one flag and
two setters -- both write dx/dy, `size` clears the flag, `nodeSize` sets it,
and whichever is called last decides how dx/dy are read while the other's
values are simply gone. So the `.size([CANVAS_H - ..., CANVAS_W - ...])` call
is dead code and the canvas dimensions never reach the layout.

The tempting shortcut is to make the stub honour both, because that turns the
remaining `MapGeometryTests` failures green. It would also be the exact
mistake this file exists to prevent: the map would still ignore its canvas in
a browser, and the suite would no longer say so. These tests fail if the stub
ever becomes that accommodating.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - reported by the capability guard
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]
STUB_JS = ROOT / "tests" / "js" / "d3_dom_stub.js"


def _eval(probe: str) -> dict:
    """Evaluate *probe* against the stub alone and return its JSON result."""
    script = "\n".join([STUB_JS.read_text(encoding="utf-8"), probe])
    return json.loads(quickjs.Context().eval(script))


# Three levels deep, and that depth is load-bearing rather than incidental.
#
# The first version of this fixture was two levels, which made maxDepth == 1 --
# and at maxDepth 1, fixed spacing (`depth * dy`) and proportional spacing
# (`(depth / maxDepth) * dy`) are arithmetically the same number. A mutation
# that replaced the nodeSize branch with the proportional formula passed every
# test in this file. Found by running that mutation; the tests were asserting
# a coincidence of the fixture, not the behaviour.
#
# With maxDepth == 2 the two formulas diverge (320 versus 160 at the deepest
# node), so the assertions can actually tell the modes apart.
_TREE_LITERAL = """{
    id: 'root', kind: 'transport',
    children: [
      {id: 'a', kind: 'chat', children: [{id: 'a1', kind: 'chat'}]},
      {id: 'b', kind: 'chat'}
    ]
  }"""

_HIERARCHY = f"""
  var root = d3.hierarchy({_TREE_LITERAL});
"""


@unittest.skipUnless(quickjs is not None, "needs quickjs to evaluate the stub")
class TreeSizeAndNodeSizeAreMutuallyExclusiveTests(unittest.TestCase):
    """d3-hierarchy's contract, asserted against the stub."""

    def test_node_size_last_wins_and_size_reads_back_null(self):
        """The shape supervisor-map.js actually produces. In d3,
        `tree.size()` returns null once nodeSize is in effect -- the earlier
        size values are not merged, not remembered, and not applied."""
        out = _eval(_HIERARCHY + """
          var t = d3.tree().size([500, 900]).nodeSize([20, 160]);
          t(root);
          JSON.stringify({size: STUB.treeSize, nodeSize: STUB.treeNodeSize});
        """)
        self.assertIsNone(
            out["size"],
            "the stub still reports a size after nodeSize was set; d3 reports "
            "null, and a module calling both would look correct here while "
            "ignoring its canvas in a browser",
        )
        self.assertEqual(out["nodeSize"], [20, 160])

    def test_size_last_wins_and_node_size_reads_back_null(self):
        """The same rule in the other direction, so the stub encodes
        last-call-wins rather than a hardcoded preference for nodeSize."""
        out = _eval(_HIERARCHY + """
          var t = d3.tree().nodeSize([20, 160]).size([500, 900]);
          t(root);
          JSON.stringify({size: STUB.treeSize, nodeSize: STUB.treeNodeSize});
        """)
        self.assertEqual(out["size"], [500, 900])
        self.assertIsNone(out["nodeSize"])

    def test_the_two_modes_place_nodes_differently(self):
        """Reading back the setters is not enough -- the flag has to actually
        change the layout. If both modes produced the same coordinates, a
        module could switch between them with no test able to tell, which is
        the same blind spot in a different place."""
        probe = _HIERARCHY + f"""
          var fit = d3.tree().size([500, 900]);
          fit(root);
          var fitted = root.descendants().map(function (n) {{ return [n.x, n.y]; }});
          var root2 = d3.hierarchy({_TREE_LITERAL});
          var fixed = d3.tree().nodeSize([500, 900]);
          fixed(root2);
          var spaced = root2.descendants().map(function (n) {{ return [n.x, n.y]; }});
          JSON.stringify({{fitted: fitted, spaced: spaced}});
        """
        out = _eval(probe)
        # Deliberately the SAME pair of numbers for both modes. Given
        # different numbers the coordinates differ whatever the stub does with
        # them, so the assertion would pass on a stub that had only one mode
        # and scaled it -- which is exactly the mutation that got through the
        # first version of this test.
        self.assertNotEqual(
            out["fitted"], out["spaced"],
            "size([500,900]) and nodeSize([500,900]) produced identical "
            "coordinates, so the stub has one layout mode wearing two names",
        )

    def test_node_size_scales_with_the_values_given(self):
        """Fixed spacing means the numbers passed in are the spacing -- not a
        proportion of some box the stub invented."""
        out = _eval(_HIERARCHY + """
          var t = d3.tree().nodeSize([20, 160]);
          t(root);
          var depths = root.descendants().map(function (n) { return [n.depth, n.y]; });
          JSON.stringify({depths: depths});
        """)
        for depth, y in out["depths"]:
            self.assertEqual(
                y, depth * 160,
                f"depth {depth} should sit at {depth * 160} with dy=160",
            )

    def test_separation_is_still_chainable_after_both(self):
        """supervisor-map.js chains .separation() between the two setters, so
        neither may break the fluent interface -- a stub that returned
        undefined here would resurrect the original "not a function" crash in
        a new place."""
        out = _eval(_HIERARCHY + """
          var t = d3.tree()
            .size([500, 900])
            .separation(function (a, b) { return a.parent === b.parent ? 1 : 1.2; })
            .nodeSize([20, 160]);
          t(root);
          JSON.stringify({ok: typeof t === 'function', nodeSize: STUB.treeNodeSize});
        """)
        self.assertTrue(out["ok"])
        self.assertEqual(out["nodeSize"], [20, 160])


@unittest.skipUnless(quickjs is not None, "needs quickjs to evaluate the stub")
class TheStubProvidesWhatTheModuleCallsTests(unittest.TestCase):
    """The gap that caused the 35 failures was a missing method, and the only
    reason it took a full run to find is that nothing compared the two lists.
    This does."""

    def test_every_d3_namespace_method_the_map_calls_exists_in_the_stub(self):
        import re
        module = (ROOT / "web" / "assets" / "supervisor-map.js").read_text(
            encoding="utf-8")
        used = set(re.findall(r"\bd3\.([a-zA-Z]+)", module))
        self.assertTrue(used, "found no d3 calls in supervisor-map.js")
        missing = sorted(
            name for name in used
            if not _eval("JSON.stringify({has: typeof d3[%r] !== 'undefined'})"
                         % name)["has"]
        )
        self.assertEqual(
            missing, [],
            f"supervisor-map.js calls d3 methods the stub does not define: "
            f"{missing}. Every test that renders the map will fail with "
            f"'TypeError: not a function' rather than with anything about the "
            f"behaviour under test.",
        )

    def test_the_tree_layout_provides_the_chain_the_map_uses(self):
        import re
        module = (ROOT / "web" / "assets" / "supervisor-map.js").read_text(
            encoding="utf-8")
        # The methods chained onto d3.tree() in the module's own source.
        chain = set(re.findall(r"\.(size|nodeSize|separation)\(", module))
        self.assertTrue(chain, "found no tree-layout chain in supervisor-map.js")
        for name in sorted(chain):
            with self.subTest(method=name):
                out = _eval(
                    "JSON.stringify({ok: typeof d3.tree()[%r] === 'function'})"
                    % name)
                self.assertTrue(
                    out["ok"],
                    f"d3.tree().{name}() is called by supervisor-map.js but is "
                    f"not a function on the stub's layout",
                )


if __name__ == "__main__":
    unittest.main()
