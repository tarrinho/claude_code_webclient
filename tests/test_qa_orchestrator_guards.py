"""The properties the chat-based orchestrator design rests on, pinned so they
cannot rot. In the spirit of test_qa_api_tokens.py::DevExemptionIsGoneTests:
these assert an architectural property, not a behaviour of a particular
plan -- but see the history below for why "architectural property" no
longer means "read the source".

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
calls, and the guards below could legitimately be asserted module-wide.
That deletion does NOT happen in this branch: `delegation_recorder.py`
still hooks `OrchestratorEngine._materialise_plan`,
`routes/orchestrators.py` still constructs `OrchestratorEngine` for the
legacy flow, and eleven other test files still exercise `OrchestratorEngine`
and `PlanParser` directly. So `orchestrator.py` currently, correctly,
contains several `runner.run_turn` calls that belong to the legacy engine,
not to the new chat-based scheduler. Widen the scope to module-wide ONLY
once `OrchestratorEngine` and `PlanParser` are actually deleted.

WHY THESE ARE BEHAVIOURAL, NOT SOURCE INSPECTION -- read this before
"simplifying" these back to a source/AST check:

Round 1 asserted `assertIn("_start_turn", inspect.getsource(_take_turn))`
and `assertNotIn("runner.run_turn", inspect.getsource(...))`. Mutation
testing walked through five ordinary-looking one-line edits, and every one
of them defeated a text- or AST-based check that had just been "fixed" to
catch the previous one:

  1. a stale docstring mentioning "_start_turn" kept guard 1 green after
     the real call was deleted (substring match over raw source text);
  2. an ordinary non-docstring string literal, `_note = "_start_turn"`,
     did the same after the check was moved to AST-node matching, because
     nothing yet required the reference to be something other than a
     `Constant`;
  3. `from runner import run_turn` + a bare `run_turn(...)` call defeated
     the `"runner.run_turn"` substring check for guards 2/3 -- an ordinary
     import-style choice, not an evasive edit;
  4. `getattr(runner, "run_" + "turn")` was structurally invisible to an
     AST check for a `getattr(..., "run_turn")` `Constant` argument,
     because `"run_" + "turn"` is a `BinOp`, not a `Constant`. An f-string
     or a variable holding the name defeats it identically;
  5. leaving the now-unused `from routes.chats import _start_turn` import
     in place while rewriting the body to call `runner.run_turn` directly
     kept the AST "is there a reference to _start_turn" check green, since
     nothing required the reference to be CALLED rather than merely
     imported-and-unused -- a stale import left behind by a refactor is one
     of the most ordinary things that happens in any codebase.

The common defect was never in a particular check -- it was in asking "does
the text or the AST mention X" when the question that actually matters is
"does calling this actually reach X". No enumeration of spellings closes
that gap, because the next spelling is always one edit away. The guards
below instead patch the real destination (`routes.chats._start_turn`, and
`runner.run_turn`/`runner.stream_turn`) and observe whether it is actually
invoked when the code under test runs. No source spelling can defeat an
assertion about what was actually called.

Do NOT revert these to a source or AST inspection "for simplicity" -- that
is exactly the regression this history documents.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import config
import db


class TakeTurnRoutesThroughStartTurnTests(unittest.IsolatedAsyncioTestCase):
    """Guard 1, behavioural: patch the real `routes.chats._start_turn` and
    assert `orchestrator._take_turn` actually calls it. No source spelling
    of the call site can defeat this -- the mock either observed a call or
    it did not.
    """

    async def test_take_turn_calls_start_turn(self):
        """If this fails, usage recording AND gallery capture have both
        stopped, silently -- which is the state this design replaced."""
        import orchestrator
        import turns

        chat = {"id": "chat-1"}
        stub_task = asyncio.ensure_future(asyncio.sleep(0))
        await stub_task
        stub_live_turn = turns.LiveTurn(
            chat_id="chat-1", owner="owner-1", prompt="the prompt",
            state="done", task=stub_task,
        )
        start_turn_mock = AsyncMock(return_value=stub_live_turn)

        with patch("routes.chats._start_turn", start_turn_mock):
            result = await orchestrator._take_turn(
                chat, "owner-1", "the prompt", None)

        start_turn_mock.assert_awaited_once_with(
            chat, "owner-1", "the prompt", None)
        self.assertIs(
            result, stub_live_turn,
            "_take_turn must return exactly what _start_turn produced",
        )


class TheSchedulerNeverReachesTheRunnerDirectlyTests(unittest.IsolatedAsyncioTestCase):
    """Guards 2 and 3, behavioural: drive a REAL `orchestrator.run_tasks()`
    run over one task, with `routes.chats._start_turn` replaced by a
    harmless stub (so no CLI is ever actually spawned) and
    `runner.run_turn`/`runner.stream_turn` replaced by tripwires that
    record whether they were ever reached.

    Deliberately does NOT stub `orchestrator._take_turn` -- doing so would
    bypass the very path under test, and this is the one place a bypass
    must be visible: `_take_turn` is meant to be the single door the
    scheduler goes through, so the test has to leave that door as the real
    function and only replace what is on the other side of it.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_a_real_run_never_reaches_the_runner(self):
        import orchestrator

        owner = uuid.uuid4().hex
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, owner)
        await db.orchestrator_task_create(orch, "task1", "Do it", "do it")

        async def stub_start_turn(chat, owner_id, prompt, model):
            import turns
            # A real `_start_turn` always persists the assistant's reply
            # before its LiveTurn settles "done" -- this stub must too, now
            # that run_tasks (fix 3) treats a "done" turn with no captured
            # output as failed (the exact 2026-08-30 signature: a refused or
            # empty turn recorded as a completed task). Without this write,
            # this stub's state="done" would (correctly, post-fix) be read
            # as failed, which is not what this guard is testing.
            await db.messages_batch(
                chat["id"], [("user", prompt), ("assistant", "stub answer")]
            )
            task = asyncio.ensure_future(asyncio.sleep(0))
            await task
            return turns.LiveTurn(
                chat_id=chat["id"], owner=owner_id, prompt=prompt,
                state="done", task=task,
            )

        # Tripwires: the correct code path never touches either of these.
        run_turn_mock = AsyncMock(name="runner.run_turn")
        stream_turn_mock = Mock(name="runner.stream_turn")

        with patch("routes.chats._start_turn", side_effect=stub_start_turn), \
             patch("runner.run_turn", run_turn_mock), \
             patch("runner.stream_turn", stream_turn_mock):
            await orchestrator.run_tasks(orch, owner, "parent-chat", self.tmp.name)

        run_turn_mock.assert_not_called()
        stream_turn_mock.assert_not_called()

        task = await db.orchestrator_task_get(orch, "task1", owner)
        self.assertEqual(
            task["status"], "done",
            "the run must actually complete via the real (stubbed) path, "
            "not merely avoid the tripwires by not running at all",
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
