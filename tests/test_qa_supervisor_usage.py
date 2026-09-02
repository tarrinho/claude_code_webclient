"""QA: a supervisor turn's cost is recorded, and attributed to the supervisor.

Usage is recorded by the *caller*. `runner` collects the frames and hands them
over through `take_last_usage`; `app.py` does that for every conversation turn.
`supervisor.py` never did — so a supervisor fanning out ten subtasks spent ten
turns' worth of tokens and appeared in the usage tables as nothing at all. It is
the one caller that can attribute them, because it owns the synthetic chat ids
(`supervisor_<uuid>`, `subtask_<id>`) that no `chats` row matches.

`origin="supervisor"` rather than the default `"web"`: the column exists so spend
can be told apart by where it came from, and a fan-out is the case most worth
separating — one request becoming a dozen turns without the user issuing a dozen
prompts.

Two cases here matter more than the happy path:

* **A failed turn is still recorded.** A task that ran two minutes and then
  errored has been paid for. Recording only on success would make the
  cheapest-looking supervisor the one that fails most.
* **Accounting never breaks a turn.** A write that fails is logged and swallowed,
  because the turn has already succeeded by then.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import config
import runner
import supervisor


class UsageRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = supervisor.SupervisorEngine("sup-1", "pedro")
        self.rows: list[dict] = []

    def _frame(self, models=None, cost=0.42, is_error=False):
        return {
            "type": "usage",
            "models": models if models is not None else {
                "claude-sonnet-5": {
                    "input_tokens": 1200, "output_tokens": 300,
                    "cache_read_tokens": 900, "cache_creation_tokens": 100,
                    "cost_basis": "api",
                },
            },
            "cost_usd": cost,
            "duration_ms": 8100,
            "is_error": is_error,
        }

    async def _record(self, frame, chat_id="subtask_t001", model="claude-sonnet-5"):
        async def fake_usage_record(**kwargs):
            self.rows.append(kwargs)
            return len(self.rows)

        with (
            patch.object(runner, "take_last_usage", return_value=frame),
            patch("db.usage_record", fake_usage_record),
        ):
            await self.engine._record_usage(chat_id, model)
        return self.rows

    async def test_a_turn_is_recorded(self):
        rows = await self._record(self._frame())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["chat_id"], "subtask_t001")
        self.assertEqual(row["owner_id"], "pedro")
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["input_tokens"], 1200)
        self.assertEqual(row["output_tokens"], 300)
        self.assertEqual(row["cache_read_tokens"], 900)
        self.assertEqual(row["cache_creation_tokens"], 100)

    async def test_it_is_attributed_to_the_supervisor(self):
        """The whole point of recording it separately."""
        rows = await self._record(self._frame())
        self.assertEqual(rows[0]["origin"], "supervisor")

    async def test_cost_is_charged_once_across_models(self):
        """The CLI reports cost for the whole turn, not per model.

        Attaching it to every row would bill a two-model turn twice — the same
        rule `app.py` applies on the conversation path.
        """
        rows = await self._record(self._frame(models={
            "claude-sonnet-5": {"input_tokens": 10, "output_tokens": 1},
            "claude-haiku-4-5": {"input_tokens": 20, "output_tokens": 2},
        }))
        self.assertEqual(len(rows), 2)
        charged = [r for r in rows if r["cost_usd"] is not None]
        self.assertEqual(len(charged), 1, "cost was applied to more than one row")
        self.assertEqual(charged[0]["cost_usd"], 0.42)

    async def test_an_errored_turn_is_still_recorded(self):
        rows = await self._record(self._frame(is_error=True))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_error"])

    async def test_an_empty_frame_writes_nothing(self):
        """No usage reported is not the same as a zero-cost turn.

        Writing a row of zeroes would put a turn in the tables that the CLI never
        priced, which is worse than the gap it fills.
        """
        rows = await self._record({})
        self.assertEqual(rows, [])

    async def test_a_write_failure_does_not_raise(self):
        """Accounting must not fail a turn that has already succeeded."""
        async def exploding_record(**kwargs):
            raise RuntimeError("database is locked")

        with (
            patch.object(runner, "take_last_usage", return_value=self._frame()),
            patch("db.usage_record", exploding_record),
        ):
            await self.engine._record_usage("subtask_t001", "claude-sonnet-5")

    async def test_the_provider_reflects_the_transport(self):
        with patch.object(config, "PROXY_ENABLED", True):
            rows = await self._record(self._frame())
        self.assertEqual(rows[0]["provider"], "proxy")


class CallSiteTests(unittest.TestCase):
    """Every turn the engine makes must record. Asserted on the source because
    driving all three paths needs a live CLI, and a missed call site is silent —
    it looks exactly like a supervisor that happened to be cheap."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.source = Path(supervisor.__file__).read_text(encoding="utf-8")

    def _body(self, name):
        """One method's source, bounded by the next method rather than by a
        character count. A fixed window silently truncated `_execute_task` short
        of its own failure handler, so the case asserting that handler records
        anything raised IndexError instead of failing or passing."""
        start = self.source.index(f"    async def {name}(")
        rest = self.source[start + 10:]
        ends = [
            offset for offset in (
                rest.find("\n    async def "), rest.find("\n    def "),
                rest.find("\nclass "),
            ) if offset != -1
        ]
        return rest[:min(ends)] if ends else rest

    def test_the_planner_turn_records(self):
        self.assertIn("_record_usage", self._body("_run_planner_turn"))

    def test_the_task_records_on_both_the_success_and_failure_paths(self):
        """Positionally, not by presence — and that distinction was found by
        mutation.

        Both of these cases originally asserted only that `_record_usage`
        appeared *somewhere* in the method. Deleting the success-path call then
        left both green, because the failure-path call still satisfied the
        search: two tests, one property, and the property was "the string exists"
        rather than "both paths record". A supervisor whose successful turns cost
        nothing and whose failures cost money would have shipped.

        The `except` line is the boundary. One call must sit on each side of it.
        """
        task = self._body("_execute_task")
        boundary = task.index("except Exception as exc:")
        before = task[:boundary].count("await self._record_usage(")
        after = task[boundary:].count("await self._record_usage(")
        self.assertGreaterEqual(
            before, 1,
            "the success path does not record, so a completed task costs nothing",
        )
        self.assertGreaterEqual(
            after, 1,
            "the failure path does not record, and a failed turn has still been "
            "paid for -- often more than a successful one",
        )

    def test_the_chat_id_is_bound_before_the_try(self):
        """Or the failure handler raises UnboundLocalError over the real fault.

        `_build_dep_context` and a path resolve both run inside that try and both
        can raise. This file has already lost one real error message to exactly
        that substitution.
        """
        task = self._body("_execute_task")
        assign = task.index('task_chat_id = f"subtask_{task_id}"')
        try_at = task.index("\n        try:")
        self.assertLess(
            assign, try_at,
            "task_chat_id is assigned inside the try, so the failure handler "
            "that uses it can raise NameError and mask the real exception",
        )


if __name__ == "__main__":
    unittest.main()
