"""QA: how the orchestrator page renders a run's cost.

Executes the real `renderRunCost` from web/assets/orchestrator/tasks.js in
QuickJS, together with the real `abbrevTokens`/`formatUsd` from
web/assets/format.js, rather than re-stating the formatting rules in Python --
the argument tests/test_qa_transport_status.py makes for itself: a Python copy
of display logic keeps passing after the JS the browser runs stops agreeing
with it.

Only the two functions are extracted, not the module: tasks.js imports eight
other page modules, and pulling those in would make this a test of the
import graph.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - exercised only without the dev deps
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]
TASKS_JS = ROOT / "web" / "assets" / "orchestrator" / "tasks.js"
FORMAT_JS = ROOT / "web" / "assets" / "format.js"

# A DOM small enough to read, recording what the renderer put on the element.
DOM_STUB = """
var STUB = {created: []};

function FakeSpan() {
  var span = {
    tagName: "span", className: "", textContent: "", title: null,
    setAttribute: function (k, v) { span[k] = v; },
  };
  return span;
}

var runCost = {
  hidden: false,
  title: null,
  dataset: {},
  children: [],
  replaceChildren: function () {
    runCost.children = Array.prototype.slice.call(arguments);
  },
  removeAttribute: function (name) { runCost[name] = null; },
};

var document = {
  createElement: function () {
    var span = FakeSpan();
    STUB.created.push(span);
    return span;
  },
};

var el = {runCost: runCost};
var state = {cost: null};

/** The rendered row as flat text, in order, plus the row's own flags. */
function rendered() {
  return {
    hidden: runCost.hidden,
    partial: runCost.dataset.partial || null,
    title: runCost.title,
    parts: runCost.children.map(function (c) {
      return {text: c.textContent, cls: c.className, title: c.title};
    }),
  };
}
"""


def _extract_function(source: str, name: str) -> str:
    """One top-level function's exact source, brace-balanced.

    Same helper as tests/test_qa_transport_status.py, including its reason for
    balancing the parameter list separately: a default argument containing
    braces would otherwise be mistaken for the body.
    """
    marker = f"function {name}("
    start = source.index(marker)
    paren_start = source.index("(", start)
    depth = 0
    j = paren_start
    for j in range(paren_start, len(source)):
        if source[j] == "(":
            depth += 1
        elif source[j] == ")":
            depth -= 1
            if depth == 0:
                break
    brace_start = source.index("{", j)
    depth = 0
    i = brace_start
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                break
    return source[start:i + 1]


def _render(cost) -> dict:
    tasks = TASKS_JS.read_text(encoding="utf-8")
    fmt = FORMAT_JS.read_text(encoding="utf-8")
    script = "\n".join([
        DOM_STUB,
        _extract_function(fmt, "abbrevTokens"),
        _extract_function(fmt, "formatUsd"),
        _extract_function(tasks, "_costPart"),
        _extract_function(tasks, "renderRunCost"),
        f"state.cost = {json.dumps(cost)};",
        "renderRunCost();",
        "JSON.stringify(rendered());",
    ])
    return json.loads(quickjs.Context().eval(script))


def _cost(**overrides) -> dict:
    base = {
        "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "turns": 0,
        "errors": 0, "cost_partial": False, "cost_note": "",
        "planner_missing": False,
    }
    base.update(overrides)
    return base


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class RunCostRenderTests(unittest.TestCase):

    def _text(self, out: dict) -> str:
        return " ".join(p["text"] for p in out["parts"])

    def test_nothing_is_shown_before_the_first_turn(self):
        """A orchestrator that has not started showing "0 turns · $0.0000" is
        noise; the row reappears the moment the planning turn lands."""
        out = _render(_cost())
        self.assertTrue(out["hidden"])
        self.assertEqual(out["parts"], [])

    def test_nothing_is_shown_before_the_figure_has_been_fetched(self):
        """null is "not fetched yet", which is not the same as "spent
        nothing" -- and must not render as a zero."""
        out = _render(None)
        self.assertTrue(out["hidden"])

    def test_a_single_turn_is_not_pluralised(self):
        out = _render(_cost(turns=1, input_tokens=10, cost_usd=0.001))
        self.assertIn("1 turn", self._text(out))
        self.assertNotIn("1 turns", self._text(out))

    def test_several_turns_are_pluralised(self):
        out = _render(_cost(turns=4, input_tokens=10, cost_usd=0.001))
        self.assertIn("4 turns", self._text(out))

    def test_tokens_are_abbreviated_with_the_exact_figure_in_the_title(self):
        """The shortened form is a reading aid, not the number."""
        out = _render(_cost(turns=1, input_tokens=12_345, output_tokens=6_789,
                            cost_usd=0.01))
        text = self._text(out)
        self.assertIn("12.3 K in", text)
        self.assertIn("6.8 K out", text)
        titles = [p["title"] for p in out["parts"] if p["title"]]
        self.assertIn("12345 input tokens", titles)
        self.assertIn("6789 output tokens", titles)

    def test_the_cost_keeps_four_decimals(self):
        """A whole run can cost well under a cent, and "$0.00" reads as
        free. usage.js keeps two, which is right for a figure aggregated over
        days -- so the precision is the caller's, not the formatter's."""
        out = _render(_cost(turns=1, input_tokens=10, cost_usd=0.0004))
        self.assertIn("$0.0004", self._text(out))

    def test_a_partial_total_is_marked_on_the_row(self):
        """The stylesheet hangs an asterisk off this flag: a number that
        quietly omits some of the run's turns is worse than one marked
        incomplete."""
        out = _render(_cost(
            turns=2, input_tokens=10, cost_usd=0.01, cost_partial=True,
            cost_note="Some turns ran on a backend where the reported cost is "
                      "not meaningful.",
        ))
        self.assertEqual(out["partial"], "yes")
        self.assertIn("not meaningful", out["title"])

    def test_a_complete_total_carries_no_marker_or_tooltip(self):
        out = _render(_cost(turns=2, input_tokens=10, cost_usd=0.01))
        self.assertIsNone(out["partial"])
        self.assertIsNone(out["title"])

    def test_failed_turns_are_shown_and_explained(self):
        """They spent tokens and are counted, so the row says so rather than
        leaving the reader to wonder why the total looks high."""
        out = _render(_cost(turns=3, input_tokens=10, cost_usd=0.01, errors=2))
        self.assertIn("2 failed", self._text(out))
        classes = [p["cls"] for p in out["parts"]]
        self.assertIn("run-cost-errors", classes)

    def test_no_failures_adds_no_row(self):
        out = _render(_cost(turns=3, input_tokens=10, cost_usd=0.01))
        self.assertNotIn("failed", self._text(out))

    def test_a_run_with_tokens_but_no_meaningful_cost_still_shows_tokens(self):
        """Entirely on a gateway: the tokens are real counts whatever served
        them, and dropping the whole row would hide a run that did work."""
        out = _render(_cost(
            turns=2, input_tokens=5_000, output_tokens=900, cost_usd=0.0,
            cost_partial=True, cost_note="Some turns ran on a backend where "
                                         "the reported cost is not meaningful.",
        ))
        self.assertFalse(out["hidden"])
        self.assertIn("5.0 K in", self._text(out))
        self.assertEqual(out["partial"], "yes")


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class SharedFormattersTests(unittest.TestCase):
    """format.js on its own. It exists so the two pages cannot disagree
    about how a token count is written; usage.js's own `_abbrev` is now an
    alias of it."""

    def _call(self, expression: str):
        fmt = FORMAT_JS.read_text(encoding="utf-8")
        script = "\n".join([
            _extract_function(fmt, "abbrevTokens"),
            _extract_function(fmt, "formatUsd"),
            f"JSON.stringify({{value: {expression}}})",
        ])
        return json.loads(quickjs.Context().eval(script))["value"]

    def test_small_counts_are_printed_whole(self):
        self.assertEqual(self._call("abbrevTokens(999)"), "999")

    def test_thousands(self):
        self.assertEqual(self._call("abbrevTokens(1500)"), "1.5 K")

    def test_millions(self):
        self.assertEqual(self._call("abbrevTokens(2500000)"), "2.5 M")

    def test_billions(self):
        self.assertEqual(self._call("abbrevTokens(3200000000)"), "3.2 B")

    def test_a_missing_count_is_zero_not_nan(self):
        self.assertEqual(self._call("abbrevTokens(null)"), "0")
        self.assertEqual(self._call("abbrevTokens(undefined)"), "0")

    def test_a_null_amount_has_no_dollar_figure(self):
        """Distinct from 0: "nothing to show" and "zero" are different
        statements, and the caller renders them differently."""
        self.assertIsNone(self._call("formatUsd(null)"))
        self.assertIsNone(self._call("formatUsd(undefined)"))

    def test_a_non_numeric_amount_has_no_dollar_figure(self):
        self.assertIsNone(self._call('formatUsd("about a tenner")'))

    def test_zero_is_a_figure(self):
        self.assertEqual(self._call("formatUsd(0)"), "$0.0000")

    def test_the_precision_is_the_callers(self):
        self.assertEqual(self._call("formatUsd(1.23456, 2)"), "$1.23")


if __name__ == "__main__":
    unittest.main()
