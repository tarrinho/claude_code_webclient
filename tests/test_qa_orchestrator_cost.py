"""QA: what one orchestrator run has spent.

The engine has recorded usage for every turn it spends since the fix that
added ``_record_usage`` -- one row per model, ``origin="orchestrator"``, keyed
on the synthetic chat id. Nothing ever read those rows back per run, so the
page that shows a orchestrator's progress could not say what the progress had
cost. The rows were there the whole time; this is the read side.

Two id shapes carry the spend, and they are not symmetric, which is the whole
reason a schema change was needed. A task's rows are keyed
``subtask_<task id>`` and the task ids are in ``orchestrator_tasks``, so they
are reachable from the orchestrator. The planner's are keyed on a bare
``uuid4`` with nothing tying it back -- so the first and often largest turn of
a run was recorded, correctly attributed to the owner, and unreachable from
the orchestrator that spent it. Hence ``orchestrators.planner_chat_id``.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
from tests.testing_model import TESTING_MODEL

HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class OrchestratorCostTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await db.orchestrator_create("o-1", "Build it", None, "admin")

    async def _spend(
        self,
        chat_id: str,
        *,
        provider: str = "through_claude_code",
        input_tokens: int = 100,
        output_tokens: int = 20,
        cost_usd: float | None = 0.01,
        is_error: bool = False,
        owner: str = "admin",
    ) -> None:
        await db.usage_record(
            chat_id=chat_id,
            owner_id=owner,
            model=TESTING_MODEL,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=1000,
            is_error=is_error,
            origin="orchestrator",
        )

    # ── Nothing spent ──────────────────────────────────────────────────

    async def test_a_run_with_no_turns_reports_zero_not_unknown(self):
        """0.0 rather than None: a orchestrator with no turns has provably
        spent nothing, which is a different statement from "we cannot say",
        and the UI renders the two differently."""
        cost = await db.orchestrator_cost("o-1", "admin")
        self.assertEqual(cost["cost_usd"], 0.0)
        self.assertEqual(cost["turns"], 0)
        self.assertFalse(cost["cost_partial"])

    async def test_an_unknown_orchestrator_reports_zero(self):
        cost = await db.orchestrator_cost("no-such-run", "admin")
        self.assertEqual(cost["turns"], 0)

    async def test_another_owners_request_gets_no_figure(self):
        """What this pins down is orchestrator_tasks_get's join scope and the
        usage query's own owner filter. The orchestrator_get call is scoped
        too, but as defence in depth: unscoping it alone changes no result,
        because there is no arrangement of rows where the other two pass and
        it would have refused. Recorded here so a later reader does not take
        this test as proof of a scope it does not exercise."""
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1")
        cost = await db.orchestrator_cost("o-1", "someone-else")
        self.assertEqual(cost["turns"], 0)
        self.assertEqual(cost["cost_usd"], 0.0)

    # ── Task turns ─────────────────────────────────────────────────────

    async def test_a_tasks_spend_is_counted(self):
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1", input_tokens=500, output_tokens=60,
                          cost_usd=0.03)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["turns"], 1)
        self.assertEqual(cost["input_tokens"], 500)
        self.assertEqual(cost["output_tokens"], 60)
        self.assertAlmostEqual(cost["cost_usd"], 0.03, places=6)

    async def test_every_task_in_the_run_is_summed(self):
        for n in range(3):
            await db.orchestrator_task_create("o-1", f"t-{n}", "T", None)
            await self._spend(f"subtask_t-{n}", input_tokens=100, cost_usd=0.01)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["turns"], 3)
        self.assertEqual(cost["input_tokens"], 300)
        self.assertAlmostEqual(cost["cost_usd"], 0.03, places=6)

    async def test_another_runs_tasks_are_not_counted(self):
        """The reason the chat ids are gathered in Python and passed as
        parameters rather than matched with ``LIKE 'subtask_%'``: task ids are
        globally unique but carry no orchestrator in them, so a LIKE would
        sum every run on the host into each of them."""
        await db.orchestrator_create("o-2", "Other", None, "admin")
        await db.orchestrator_task_create("o-1", "t-mine", "T", None)
        await db.orchestrator_task_create("o-2", "t-theirs", "T", None)
        await self._spend("subtask_t-mine", cost_usd=0.01)
        await self._spend("subtask_t-theirs", cost_usd=0.99)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["turns"], 1)
        self.assertAlmostEqual(cost["cost_usd"], 0.01, places=6)

    async def test_a_failed_turn_still_counts(self):
        """CLAUDE.md rule 5: a turn that ran and then errored has been paid
        for. Counting only successes makes the caller that fails most look
        like the cheapest."""
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1", cost_usd=0.02, is_error=True)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["turns"], 1)
        self.assertEqual(cost["errors"], 1)
        self.assertAlmostEqual(cost["cost_usd"], 0.02, places=6)

    # ── The planning turn ──────────────────────────────────────────────

    async def test_the_planning_turn_is_counted_once_recorded(self):
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        await self._spend("plan-uuid", input_tokens=900, cost_usd=0.05)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["turns"], 1)
        self.assertEqual(cost["input_tokens"], 900)
        self.assertAlmostEqual(cost["cost_usd"], 0.05, places=6)

    async def test_the_planner_and_the_tasks_are_summed_together(self):
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        await self._spend("plan-uuid", input_tokens=900, cost_usd=0.05)
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1", input_tokens=100, cost_usd=0.01)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["turns"], 2)
        self.assertEqual(cost["input_tokens"], 1000)
        self.assertAlmostEqual(cost["cost_usd"], 0.06, places=6)

    async def test_a_run_with_no_recorded_planner_is_flagged_partial(self):
        """The column is written before the planning turn runs, and the
        writer never raises -- so a run can legitimately reach here without
        one. Reporting the tasks' spend as the run's total would understate
        it silently."""
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1", cost_usd=0.01)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertTrue(cost["planner_missing"])
        self.assertTrue(cost["cost_partial"])
        self.assertIn("planning turn", cost["cost_note"])

    async def test_a_run_that_has_spent_nothing_is_not_flagged_partial(self):
        """An unstarted orchestrator has no planner id and no turns. Marking
        that partial would put an asterisk on every new orchestrator."""
        cost = await db.orchestrator_cost("o-1", "admin")
        self.assertTrue(cost["planner_missing"])
        self.assertFalse(cost["cost_partial"])
        self.assertEqual(cost["cost_note"], "")

    # ── Backends where cost is not meaningful ──────────────────────────

    async def test_a_gateway_turns_tokens_count_but_its_cost_does_not(self):
        """Same rule routes/misc.py applies everywhere else cost is shown:
        the figure is priced with Anthropic rates and means nothing for a
        third-party backend. Tokens are counts and are meaningful whatever
        served them."""
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        await self._spend("plan-uuid", provider="through_claude_code",
                          input_tokens=100, cost_usd=0.01)
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1", provider="proxy",
                          input_tokens=400, cost_usd=9.99)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["input_tokens"], 500)
        self.assertAlmostEqual(cost["cost_usd"], 0.01, places=6)
        self.assertTrue(cost["cost_partial"])
        self.assertIn("not meaningful", cost["cost_note"])

    async def test_a_run_entirely_on_a_gateway_reports_no_cost_but_real_tokens(self):
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        await self._spend("plan-uuid", provider="direct", input_tokens=700,
                          cost_usd=5.0)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertEqual(cost["input_tokens"], 700)
        self.assertEqual(cost["cost_usd"], 0.0)
        self.assertTrue(cost["cost_partial"])

    async def test_a_missing_cost_value_does_not_break_the_sum(self):
        """cost_usd is nullable: the CLI does not always report one."""
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        await self._spend("plan-uuid", cost_usd=None, input_tokens=100)
        await db.orchestrator_task_create("o-1", "t-1", "T", None)
        await self._spend("subtask_t-1", cost_usd=0.02, input_tokens=100)

        cost = await db.orchestrator_cost("o-1", "admin")

        self.assertAlmostEqual(cost["cost_usd"], 0.02, places=6)
        self.assertEqual(cost["turns"], 2)


class PlannerChatIdTests(unittest.IsolatedAsyncioTestCase):
    """The column, and the writer that must never break a run."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await db.orchestrator_create("o-1", "Build it", None, "admin")

    async def test_it_is_stored_and_read_back(self):
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        row = await db.orchestrator_get("o-1", "admin")
        self.assertEqual(row["planner_chat_id"], "plan-uuid")

    async def test_a_fresh_orchestrator_has_none(self):
        row = await db.orchestrator_get("o-1", "admin")
        self.assertIsNone(row["planner_chat_id"])

    async def test_the_writer_never_raises(self):
        """Accounting must not become a second thing that fails a run -- the
        rule db.usage_record and orchestrator_mark_degraded both state for
        themselves. A run whose planner id was lost reports a partial cost;
        a run that crashed writing it reports nothing at all."""
        with patch.object(db, "db_conn") as conn:
            conn.execute.side_effect = RuntimeError("database is locked")
            await db.orchestrator_set_planner_chat("o-1", "plan-uuid")

        row = await db.orchestrator_get("o-1", "admin")
        self.assertIsNone(row["planner_chat_id"])


class OrchestratorCostEndpointTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/orchestrators/{id}/tasks carries the figure.

    Alongside the tasks rather than in an endpoint of its own: the page that
    shows progress is the page that should say what it cost, and it already
    polls this one.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(self.password))

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "admin", "password": self.password}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    async def test_the_response_carries_the_cost(self):
        await db.orchestrator_create("o-1", "Build it", None, "admin")
        await db.orchestrator_set_planner_chat("o-1", "plan-uuid")
        await db.usage_record(
            chat_id="plan-uuid", owner_id="admin", model=TESTING_MODEL,
            provider="through_claude_code", input_tokens=1200,
            output_tokens=300, cost_usd=0.07, origin="orchestrator",
        )
        client = self._login()

        body = client.get("/api/orchestrators/o-1/tasks").json()

        self.assertIn("cost", body)
        self.assertEqual(body["cost"]["turns"], 1)
        self.assertEqual(body["cost"]["input_tokens"], 1200)
        self.assertAlmostEqual(body["cost"]["cost_usd"], 0.07, places=6)

    async def test_an_unstarted_orchestrator_reports_zeros_not_an_error(self):
        await db.orchestrator_create("o-1", "Build it", None, "admin")
        client = self._login()

        body = client.get("/api/orchestrators/o-1/tasks").json()

        self.assertEqual(body["cost"]["turns"], 0)
        self.assertEqual(body["cost"]["cost_usd"], 0.0)

    async def test_the_task_list_still_comes_back(self):
        """The figure is an addition, not a replacement."""
        await db.orchestrator_create("o-1", "Build it", None, "admin")
        await db.orchestrator_task_create("o-1", "t-1", "Do it", None)
        client = self._login()

        body = client.get("/api/orchestrators/o-1/tasks").json()

        self.assertEqual(body["count"], 1)
        self.assertEqual(body["tasks"][0]["id"], "t-1")


if __name__ == "__main__":
    unittest.main()
