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

# Fix round 1 (2026-09-21): the first version of this file matched raw
# source TEXT ("_start_turn" as a substring; "runner.run_turn" as a
# substring). Both forms were defeated by ordinary, non-adversarial edits:
#
#   * guard 1 kept passing when `_take_turn` was rewritten to call
#     `runner.run_turn` as long as SOME string containing "_start_turn"
#     remained anywhere in the source -- first caught in the shape of a
#     stale docstring, then again (reviewer finding) in the shape of an
#     ordinary non-docstring assignment `_note = "_start_turn"`. A
#     docstring-only fix closed that one instance and left the class open.
#   * guards 2/3 kept passing under `from runner import run_turn` followed
#     by a bare `run_turn(...)` call, and under
#     `getattr(runner, "run_turn")` -- neither contains the literal
#     substring "runner.run_turn".
#
# The fix is structural, not lexical: parse the function to an AST and
# require a real CODE reference (a `Name`/`Attribute` node, a `Call`, or an
# import), which a string literal (an `ast.Constant`) can never produce --
# regardless of where in the function that literal sits.


def _references_identifier(func, name: str) -> bool:
    """True iff `func`'s body contains an actual code reference to `name`:
    a `Name` node, an `Attribute` node whose `.attr` is `name`, or an
    import (`import`/`from ... import`) binding `name`.

    Deliberately NOT a text/substring search: a string literal such as a
    docstring, a comment (comments never reach the AST at all), or a plain
    `_note = "name"` assignment is an `ast.Constant`, never a `Name` or
    `Attribute`, so none of those can satisfy this check.
    """
    tree = ast.parse(inspect.getsource(func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name) == name or alias.name == name:
                    return True
    return False


_FORBIDDEN_RUNNER_CALLS = ("run_turn", "stream_turn")


def _reaches_runner_turn_call(func) -> str | None:
    """Return the forbidden name reached, or None if the function contains
    no way -- however spelled -- of reaching `runner.run_turn` /
    `runner.stream_turn`, the two calls that bypass `_start_turn` and lose
    usage recording and gallery capture silently.

    Structural, on the AST, so it survives routine refactors that a
    substring check does not:

      * `runner.run_turn(...)`               -- Attribute access, attr in
        the forbidden set, caught regardless of the base expression's name
        (not just a base literally spelled "runner").
      * `from runner import run_turn` then a bare `run_turn(...)` call --
        caught two ways: the `ImportFrom` alias itself, and the bare
        `Name` reference at the call site (so it is still caught even if
        inspection only sees the call site and not the import).
      * `getattr(runner, "run_turn")` -- a `Call` to `getattr` where one
        argument is the string constant `"run_turn"`/`"stream_turn"`,
        caught independently of what the resulting callable is later
        named.
    """
    tree = ast.parse(inspect.getsource(func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_RUNNER_CALLS:
            return node.attr
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_RUNNER_CALLS:
            return node.id
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _FORBIDDEN_RUNNER_CALLS:
                    return alias.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value in _FORBIDDEN_RUNNER_CALLS:
                    return arg.value
    return None


class TheNewSchedulerHasNoSecondExecutionPathTests(unittest.TestCase):
    """Guards scoped to the new chat-based scheduler only -- see the module
    docstring for why this is not (yet) a module-wide assertion.
    """

    def test_take_turn_routes_through_start_turn(self):
        """If this fails, usage recording AND gallery capture have both
        stopped, silently -- which is the state this design replaced.

        Checks for a real `_start_turn` reference (Name/Attribute/import),
        not a substring anywhere in the source: see the fix-round-1 note
        above `_references_identifier` for why a plain substring check was
        insufficient.
        """
        import orchestrator
        self.assertTrue(
            _references_identifier(orchestrator._take_turn, "_start_turn"),
            "_take_turn no longer contains a real reference to _start_turn",
        )

    def test_run_tasks_never_calls_the_runner_directly(self):
        import orchestrator
        forbidden = _reaches_runner_turn_call(orchestrator.run_tasks)
        self.assertIsNone(
            forbidden,
            f"run_tasks can reach runner.{forbidden}, which bypasses "
            "_start_turn, so the turn records no usage and its images "
            "never reach the gallery",
        )

    def test_create_task_chat_never_calls_the_runner_directly(self):
        import orchestrator
        forbidden = _reaches_runner_turn_call(orchestrator.create_task_chat)
        self.assertIsNone(
            forbidden,
            f"create_task_chat can reach runner.{forbidden}, which "
            "bypasses _start_turn, so the turn records no usage and its "
            "images never reach the gallery",
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
