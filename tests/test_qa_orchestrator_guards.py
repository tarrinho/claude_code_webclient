"""The properties the chat-based orchestrator design rests on, pinned so they
cannot rot. In the spirit of test_qa_api_tokens.py::DevExemptionIsGoneTests:
these assert an architectural property, not a behaviour, by reading source.

Guard 1 is the most important test in this design: the gallery and usage
benefits a task's turn gets are INHERITED from `_start_turn`, not
implemented again by the orchestrator, and hold only while every task turn
actually goes through it. If `_take_turn` (or anything the scheduler calls)
starts calling the runner directly, usage accounting and generated-image
capture are lost silently -- no exception, just a turn that produced no
usage rows and no gallery entries.

SCOPING NOTE -- why these guards read `_take_turn`, `run_tasks`, and
`create_task_chat`, and not the whole module:

The plan this design was built from assumed a later task would delete the
legacy `OrchestratorEngine`/`PlanParser` plan-grammar engine, at which point
`orchestrator.py` would contain zero `runner.run_turn`/`runner.stream_turn`
calls and zero `<<PLAN` occurrences, and the guards below could legitimately
be asserted module-wide. That deletion does NOT happen in this branch:
`delegation_recorder.py` still hooks `OrchestratorEngine._materialise_plan`,
`routes/orchestrators.py` still constructs `OrchestratorEngine` for the
legacy flow, and eleven other test files still exercise `OrchestratorEngine`
and `PlanParser` directly. So `orchestrator.py` currently, correctly,
contains several `runner.run_turn` calls and several `<<PLAN` occurrences
that belong to the legacy engine, not to the new chat-based scheduler.

A module-wide "no runner.run_turn / no <<PLAN anywhere in orchestrator.py"
assertion would therefore fail today -- not because the new design is
broken, but because the old engine it coexists with still uses both. Do
NOT "fix" the guards below by widening them to the whole module and then
either skipping them or deleting the legacy engine to make them pass as a
side effect: widen them ONLY once `OrchestratorEngine` and `PlanParser` are
actually deleted, at which point the module-wide form becomes the correct,
stronger guard and should replace the scoped one here.
"""
from __future__ import annotations

import ast
import inspect
import unittest


def _source_without_docstring(func) -> str:
    """`inspect.getsource(func)` including its own docstring.

    A docstring that *explains* the property under test (as `_take_turn`'s
    does: "guard 1 asserts that it routes through _start_turn") will itself
    contain the string a naive `assertIn` looks for -- so the assertion
    would keep passing even after the real call is deleted, as long as
    nobody also edits the prose. That was verified empirically while
    writing this file: temporarily replacing the `_start_turn` call with
    `runner.run_turn` did NOT fail a plain `assertIn("_start_turn", src)`
    check, because the docstring line survived the edit. Stripping the
    docstring first means the assertion can only be satisfied by actual
    code.
    """
    src = inspect.getsource(func)
    tree = ast.parse(src)
    fn = tree.body[0]
    if (
        fn.body
        and isinstance(fn.body[0], ast.Expr)
        and isinstance(getattr(fn.body[0], "value", None), ast.Constant)
        and isinstance(fn.body[0].value.value, str)
    ):
        fn.body = fn.body[1:]
    return ast.unparse(fn)


class TheNewSchedulerHasNoSecondExecutionPathTests(unittest.TestCase):
    """Guards scoped to the new chat-based scheduler only -- see the module
    docstring for why this is not (yet) a module-wide assertion.
    """

    def test_take_turn_routes_through_start_turn(self):
        """If this fails, usage recording AND gallery capture have both
        stopped, silently -- which is the state this design replaced."""
        import orchestrator
        code = _source_without_docstring(orchestrator._take_turn)
        self.assertIn("_start_turn", code)

    def test_run_tasks_never_calls_the_runner_directly(self):
        import orchestrator
        src = inspect.getsource(orchestrator.run_tasks)
        for forbidden in ("runner.run_turn", "runner.stream_turn"):
            self.assertNotIn(
                forbidden, src,
                f"{forbidden} bypasses _start_turn, so the turn records no "
                "usage and its images never reach the gallery",
            )

    def test_create_task_chat_never_calls_the_runner_directly(self):
        import orchestrator
        src = inspect.getsource(orchestrator.create_task_chat)
        for forbidden in ("runner.run_turn", "runner.stream_turn"):
            self.assertNotIn(
                forbidden, src,
                f"{forbidden} bypasses _start_turn, so the turn records no "
                "usage and its images never reach the gallery",
            )


class NoProseReachesArgvTests(unittest.TestCase):
    def test_a_model_from_plan_text_cannot_reach_the_cli(self):
        """`[:--mcp-config=/tmp/evil.json]` once handed attacker-chosen argv
        to the child. Membership of the allowlist is the only route now.

        Asserted by BEHAVIOUR (calling validate_plan), not by grepping the
        module for a regex name -- the legacy plan grammar's `_MODEL_RE`
        still exists in this module for the legacy engine (see module
        docstring), so a source-text assertion would be scoping this guard
        to the wrong property.
        """
        import orchestrator
        rows, errors = orchestrator.validate_plan(
            '[{"title":"A","prompt":"x","depends_on":[],'
            '"model":"--mcp-config=/tmp/evil.json"}]',
            {"claude-opus-5"},
        )
        self.assertEqual(rows, [])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
