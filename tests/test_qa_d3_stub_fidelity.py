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
        # Checked against the stub's declared list, not `typeof`. The layout is
        # wrapped in a proxy that answers every method name with a function
        # that throws a descriptive error, so `typeof layout.foo === "function"`
        # is now true for anything at all and a typeof check here would pass
        # for a method the stub does not implement -- which is the exact gap
        # this test exists to close.
        # d3.tree() first: the list is derived when a layout is built, so it is
        # empty before the first call.
        implemented = set(_eval(
            "d3.tree(); JSON.stringify({names: STUB.treeImplemented})")["names"])
        for name in sorted(chain):
            with self.subTest(method=name):
                self.assertIn(
                    name, implemented,
                    f"d3.tree().{name}() is called by supervisor-map.js but is "
                    f"not implemented by the stub; it is declared as "
                    f"{sorted(implemented)}",
                )


@unittest.skipUnless(quickjs is not None, "needs quickjs to evaluate the stub")
class AnUnimplementedMethodSaysSoTests(unittest.TestCase):
    """Reaching past the stub must still fail -- but say what is missing.

    Asked for by the stub's author after the 2026-09-10 incident: the intended
    behaviour is a loud failure, and that part worked. What did not work is
    attribution. All 35 tests in test_qa_supervisor_map_geometry.py died with a
    bare "TypeError: not a function" naming no method and no file, so they read
    as 35 behavioural regressions from the layout restructure and stayed
    unattributed for a week.
    """

    def test_calling_an_unimplemented_layout_method_still_throws(self):
        """The loud failure is the point and must survive. A stub that
        silently accepted an unknown method would let the suite agree with
        code that breaks in a browser -- strictly worse than the TypeError."""
        out = _eval("""
          var threw = false, msg = '';
          try { d3.tree().somethingNobodyImplemented([1, 2]); }
          catch (e) { threw = true; msg = e.message; }
          JSON.stringify({threw: threw, msg: msg});
        """)
        self.assertTrue(out["threw"], "an unimplemented method was accepted")

    def test_the_error_names_the_method_the_file_and_the_fix(self):
        out = _eval("""
          var msg = '';
          try { d3.tree().somethingNobodyImplemented([1, 2]); }
          catch (e) { msg = e.message; }
          JSON.stringify({msg: msg});
        """)
        msg = out["msg"]
        self.assertIn("somethingNobodyImplemented", msg, "names the method")
        self.assertIn("d3_dom_stub", msg, "names the file to edit")
        self.assertIn("nodeSize", msg,
                      "lists what is implemented, so the gap is obvious")
        self.assertNotEqual(
            msg, "not a function",
            "this is the message the incident produced; it is what the proxy "
            "exists to replace",
        )

    def test_the_implemented_list_is_derived_not_declared(self):
        """Closes the one direction a hand-written list drifts silently.

        Raised by the stub's author: understating the list makes two tests
        fail, but *adding* a method to `_tree()` without listing it made the
        coverage check go quiet on exactly the method just added. The list is
        derived with the proxy's own rule now -- own function-valued keys,
        minus the `__`-prefixed internals -- so the two definitions of
        "implemented" cannot disagree.

        Asserted by comparing the list against that rule applied
        independently, rather than against a hardcoded set of names, so adding
        a real method to the stub does not fail this test.
        """
        out = _eval("""
          var t = d3.tree();
          var derived = [];
          for (var k in t) {
            if (k.slice(0, 2) !== '__' && typeof t[k] === 'function'
                && Object.prototype.hasOwnProperty.call(t, k)) {
              derived.push(k);
            }
          }
          JSON.stringify({declared: STUB.treeImplemented, derived: derived.sort()});
        """)
        self.assertEqual(
            sorted(out["declared"]), sorted(out["derived"]),
            "STUB.treeImplemented disagrees with the layout's own methods, so "
            "it is being maintained by hand somewhere and can drift",
        )
        self.assertIn(
            "nodeSize", out["declared"],
            "the derivation produced nothing useful; a rule that returns an "
            "empty list agrees with itself and proves nothing",
        )

    def test_the_implemented_methods_are_not_shadowed_by_the_proxy(self):
        """The proxy must pass real methods through untouched, or it converts
        a working stub into one that throws on everything."""
        out = _eval(_HIERARCHY + """
          var t = d3.tree().nodeSize([20, 160]).separation(function () { return 1; });
          t(root);
          JSON.stringify({nodeSize: STUB.treeNodeSize, placed: root.descendants().length});
        """)
        self.assertEqual(out["nodeSize"], [20, 160])
        self.assertEqual(out["placed"], 4)

    def test_the_message_survives_a_chained_call(self):
        """The hole in the first version of this, and the one that matters:
        the module reaches the unimplemented method *through a chain*.

        `supervisor-map.js` calls `.separation(...).nodeSize(...)`. When the
        setters returned the raw layout instead of the proxy, `separation`
        handed back an unwrapped object and `.nodeSize` on it threw a bare
        `TypeError: not a function` again -- so the descriptive error existed
        and the real incident still could not reach it. Found by replaying the
        incident rather than by reading the code.
        """
        out = _eval("""
          var msg = '';
          try {
            d3.tree()
              .separation(function () { return 1; })
              .stillNotImplemented([1, 2]);
          } catch (e) { msg = e.message; }
          JSON.stringify({msg: msg});
        """)
        self.assertIn(
            "stillNotImplemented", out["msg"],
            "a chained call escaped the proxy and produced a bare TypeError; "
            "every setter must return the proxy, not the layout",
        )

    def test_the_message_survives_two_chained_setters(self):
        """Same property one step further out, since the real chain is three
        calls long and a single-setter test would pass on a proxy that only
        re-wraps once."""
        out = _eval("""
          var msg = '';
          try {
            d3.tree()
              .nodeSize([20, 30])
              .separation(function () { return 1; })
              .alsoNotImplemented();
          } catch (e) { msg = e.message; }
          JSON.stringify({msg: msg});
        """)
        self.assertIn("alsoNotImplemented", out["msg"])

    def test_internal_fields_still_read_as_absent(self):
        """`layout.__size ? ... : ...` runs inside the stub. If the proxy
        answered __-prefixed reads with a function, every such truthiness test
        would invert and the layout would silently pick the wrong mode."""
        out = _eval("""
          var t = d3.tree();
          JSON.stringify({
            size: typeof t.__size,
            nodeSize: typeof t.__nodeSize,
            val: typeof t.__nodeSizeVal
          });
        """)
        self.assertEqual(
            [out["size"], out["nodeSize"], out["val"]],
            ["undefined", "undefined", "undefined"],
        )


if __name__ == "__main__":
    unittest.main()
