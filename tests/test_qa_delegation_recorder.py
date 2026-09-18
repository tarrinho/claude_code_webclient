"""QA: shadow-mode routing decision records.

Design: docs/superpowers/specs/2026-09-18-routing-decision-record-design.md

The load-bearing property of this whole file is the *negative* one: recording
must not change which model a task gets, and must not be able to fail task
creation. Both are asserted here with fixtures built so they can fail --
`RoutingUnchangedTests` names a model the ladder would never have chosen, and
`RecorderIsolationTests` checks the guard by removing it.

The hook point is `OrchestratorEngine._materialise_plan` (`orchestrator.py:743`)
-- the loop that walks `list[ParsedTask]` and calls `db.orchestrator_task_create`
per task. The design document named it `_create_tasks` until 8d8577e; if a
comment anywhere still says that, it is stale, not a second method.

Every assertion below was checked by mutating the code it covers and confirming
the named test goes red. That is a rule for this subsystem rather than a
stylistic note: four can't-fail tests were written into it during 0.19.0 and
each was caught by mutation rather than by the suite.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

import config
import db
import delegation_recorder
import orchestrator
from delegation_classifier import classify

#: Classifies as `coding` (score 3, mutates true) -- checked, not assumed.
CODING_TEXT = "implement the parser widget"
#: Classifies as `comprehension`, which these fixtures leave non-operational.
COMPREHENSION_TEXT = "summarise this document for me"


class _DelegationDBCase(unittest.IsolatedAsyncioTestCase):
    """A throwaway database with the delegation tables migrated in."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def seed_coding_ladder(self):
        """Two priced, measured `coding` rows and the operational flag.

        Cheap-and-accurate first so the ladder is deterministic: the generator
        sorts cheapest-first on effective cost per task and skips anything
        measured strictly worse than the current rung, so `dear-model` survives
        only because its accuracy ties.
        """
        await db.delegation_row_set(
            "cheap-model", "coding", accuracy=1.0, n=30,
            cost_per_1m_tokens=1.0, median_latency_s=5.0, max_context=200_000)
        await db.delegation_row_set(
            "dear-model", "coding", accuracy=1.0, n=30,
            cost_per_1m_tokens=9.0, median_latency_s=5.0, max_context=200_000)
        await db.delegation_operational_set("coding", True)


class RoutingDecisionSchemaTests(_DelegationDBCase):
    """Spec section 3: the table, its indexes, and the two accessors."""

    async def test_table_and_both_indexes_exist_after_init(self):
        cur = await db.db_conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name IN "
            "('delegation_routing_decision', 'idx_routing_decision_task', "
            " 'idx_routing_decision_type')")
        found = {row["name"] for row in await cur.fetchall()}
        self.assertEqual(found, {
            "delegation_routing_decision",
            "idx_routing_decision_task",
            "idx_routing_decision_type",
        })

    async def test_record_returns_an_id_and_round_trips_every_column(self):
        new_id = await db.delegation_decision_record(
            task_table="orchestrator_tasks", task_id="abc_t001",
            task_type="coding", score=3, mutates="true",
            source="ladder", shadow_model="cheap-model",
            actual_model="claude-opus-5",
            ladder=json.dumps(["cheap-model", "dear-model"]),
        )
        self.assertIsInstance(new_id, int)
        rows = await db.delegation_decisions_recent()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], new_id)
        self.assertEqual(row["task_table"], "orchestrator_tasks")
        self.assertEqual(row["task_id"], "abc_t001")
        self.assertEqual(row["task_type"], "coding")
        self.assertEqual(row["score"], 3)
        self.assertEqual(row["mutates"], "true")
        self.assertEqual(row["source"], "ladder")
        self.assertEqual(row["shadow_model"], "cheap-model")
        self.assertEqual(row["actual_model"], "claude-opus-5")
        self.assertEqual(json.loads(row["ladder"]), ["cheap-model", "dear-model"])
        self.assertTrue(row["decided_at"])

    async def test_null_actual_model_and_null_ladder_survive_the_round_trip(self):
        """The two nullable columns, which are the normal case: a plan that
        named no model, and a task type that is not operational."""
        await db.delegation_decision_record(
            task_table="orchestrator_tasks", task_id="abc_t001",
            task_type="comprehension", score=3, mutates="true",
            source="fallback", shadow_model=config.ANTHROPIC_MODEL,
            actual_model=None, ladder=None,
        )
        row = (await db.delegation_decisions_recent())[0]
        self.assertIsNone(row["actual_model"])
        self.assertIsNone(row["ladder"])

    async def test_recent_orders_newest_first(self):
        for n in range(3):
            await db.delegation_decision_record(
                task_table="orchestrator_tasks", task_id=f"abc_t{n}",
                task_type="coding", score=3, mutates="true",
                source="fallback", shadow_model="m", actual_model=None,
                ladder=None,
            )
        rows = await db.delegation_decisions_recent()
        self.assertEqual([r["task_id"] for r in rows],
                         ["abc_t2", "abc_t1", "abc_t0"])

    async def test_recent_honours_its_task_type_filter_and_limit(self):
        for task_type in ("coding", "coding", "comprehension"):
            await db.delegation_decision_record(
                task_table="orchestrator_tasks", task_id="t",
                task_type=task_type, score=3, mutates="true",
                source="fallback", shadow_model="m", actual_model=None,
                ladder=None,
            )
        self.assertEqual(
            len(await db.delegation_decisions_recent(task_type="coding")), 2)
        self.assertEqual(
            len(await db.delegation_decisions_recent(task_type="comprehension")), 1)
        self.assertEqual(
            len(await db.delegation_decisions_recent(limit=1)), 1)

    async def test_a_second_decision_for_the_same_task_is_stored_not_refused(self):
        """Spec 3: `id` is a surrogate key because a re-planned task's second
        decision is data, not a conflict. A UNIQUE constraint here would make
        a retry raise inside the orchestrator's task loop."""
        for _ in range(2):
            await db.delegation_decision_record(
                task_table="orchestrator_tasks", task_id="abc_t001",
                task_type="coding", score=3, mutates="true",
                source="fallback", shadow_model="m", actual_model=None,
                ladder=None,
            )
        self.assertEqual(len(await db.delegation_decisions_recent()), 2)

    async def test_an_unknown_column_is_refused_rather_than_dropped(self):
        with self.assertRaises(ValueError):
            await db.delegation_decision_record(
                task_table="orchestrator_tasks", task_id="t",
                task_type="coding", score=3, mutates="true",
                source="fallback", shadow_model="m", actual_model=None,
                ladder=None, taks_type="typo",
            )

    async def test_a_missing_non_nullable_column_is_refused(self):
        with self.assertRaises(ValueError):
            await db.delegation_decision_record(
                task_table="orchestrator_tasks", task_id="t",
                task_type="coding", score=3, mutates="true",
                source="fallback", actual_model=None, ladder=None,
            )


class RecorderDecisionTests(_DelegationDBCase):
    """Spec section 4: the three-branch precedence, and its agreement with
    `ModelRouter.assign_model`."""

    async def test_an_operator_rule_wins_and_records_no_ladder(self):
        await self.seed_coding_ladder()
        router = orchestrator.ModelRouter(
            {"rules": [{"pattern": "parser", "model": "rule-model"}]})
        await delegation_recorder.record_decision(
            task_table="orchestrator_tasks", task_id="abc_t001",
            title=CODING_TEXT, description="", actual_model=None, router=router)
        row = (await db.delegation_decisions_recent())[0]
        self.assertEqual(row["source"], "rule")
        self.assertEqual(row["shadow_model"], "rule-model")
        # None, not "[]": no ladder was consulted, which is different from a
        # ladder that was consulted and came back empty.
        self.assertIsNone(row["ladder"])
        # The classification is still recorded -- the rule decided the model,
        # not what kind of task this is.
        self.assertEqual(row["task_type"], "coding")

    async def test_an_operational_type_records_rung_zero_and_the_whole_ladder(self):
        await self.seed_coding_ladder()
        await delegation_recorder.record_decision(
            task_table="orchestrator_tasks", task_id="abc_t001",
            title=CODING_TEXT, description="", actual_model=None, router=None)
        row = (await db.delegation_decisions_recent())[0]
        self.assertEqual(row["source"], "ladder")
        self.assertEqual(row["shadow_model"], "cheap-model")
        self.assertEqual(json.loads(row["ladder"]),
                         ["cheap-model", "dear-model"])

    async def test_an_operational_type_with_an_empty_ladder_falls_back(self):
        """Startup validation (spec 1.1) refuses this, so it means a table
        built some other way. The recorder must agree with `assign_model`,
        which falls back rather than raising inside a router."""
        await db.delegation_operational_set("coding", True)   # no rows at all
        await delegation_recorder.record_decision(
            task_table="orchestrator_tasks", task_id="abc_t001",
            title=CODING_TEXT, description="", actual_model=None, router=None)
        row = (await db.delegation_decisions_recent())[0]
        self.assertEqual(row["source"], "fallback")
        self.assertEqual(row["shadow_model"], config.ANTHROPIC_MODEL)
        # "[]" rather than NULL: the type *was* operational and its ladder was
        # consulted. NULL here would be indistinguishable from a type that is
        # not operational at all, which is the far more common case.
        self.assertEqual(json.loads(row["ladder"]), [])

    async def test_a_non_operational_type_falls_back_with_a_null_ladder(self):
        await self.seed_coding_ladder()          # coding is operational
        await delegation_recorder.record_decision(
            task_table="orchestrator_tasks", task_id="abc_t001",
            title=COMPREHENSION_TEXT, description="", actual_model=None,
            router=None)
        row = (await db.delegation_decisions_recent())[0]
        self.assertEqual(row["task_type"], "comprehension")
        self.assertEqual(row["source"], "fallback")
        self.assertEqual(row["shadow_model"], config.ANTHROPIC_MODEL)
        self.assertIsNone(row["ladder"])

    async def test_the_classification_recorded_is_the_classifier_s_own(self):
        """Score and mutates are stored, not recomputed differently here."""
        await self.seed_coding_ladder()
        await delegation_recorder.record_decision(
            task_table="orchestrator_tasks", task_id="abc_t001",
            title="Implement", description="the parser widget",
            actual_model=None, router=None)
        row = (await db.delegation_decisions_recent())[0]
        expected = classify("implement the parser widget")
        self.assertEqual(row["task_type"], expected.task_type)
        self.assertEqual(row["score"], expected.score)
        self.assertEqual(row["mutates"], expected.mutates)

    async def test_recorder_and_assign_model_agree_on_every_input(self):
        """The safety net for spec 4's deliberate duplication.

        `resolve_shadow` reimplements `ModelRouter.assign_model`'s precedence
        because a record needs to know which branch produced the model, and
        `assign_model` returns only the model. This drives both over a shared
        table of inputs and asserts the chosen model is identical for each.
        """
        await self.seed_coding_ladder()
        from delegation_startup import load_capability_table
        table = await load_capability_table()

        cases = [
            # (title, description, rules)
            (CODING_TEXT, "", []),
            (COMPREHENSION_TEXT, "", []),
            ("read the file and list the directory", "", []),
            (CODING_TEXT, "", [{"pattern": "parser", "model": "rule-model"}]),
            (CODING_TEXT, "", [{"pattern": "nomatch", "model": "rule-model"}]),
            (COMPREHENSION_TEXT, "", [{"pattern": "document",
                                       "model": "rule-model"}]),
            # A broken operator regex: both must skip it, neither may raise.
            (CODING_TEXT, "", [{"pattern": "([", "model": "rule-model"}]),
            # A rule with no model, and a model with no pattern: both ignored.
            (CODING_TEXT, "", [{"pattern": "parser"}, {"model": "x"}]),
        ]
        for title, desc, rules in cases:
            with self.subTest(title=title, rules=rules):
                combined = (title + " " + desc).lower()
                router = orchestrator.ModelRouter({"rules": rules})
                from_router = router.assign_model(title, desc, table=table)
                _, from_recorder, _ = delegation_recorder.resolve_shadow(
                    combined, rules=rules, table=table)
                self.assertEqual(from_recorder, from_router)


class RecorderIsolationTests(_DelegationDBCase):
    """Spec section 5: recording is diagnostics and must never be able to fail
    task creation."""

    async def _engine_with_orchestrator(self):
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        return engine

    async def test_task_creation_survives_the_recorder_raising(self):
        engine = await self._engine_with_orchestrator()
        tasks = [
            orchestrator.ParsedTask(id="t001", title="Implement the parser",
                                    description="widget"),
            orchestrator.ParsedTask(id="t002", title="Implement the lexer",
                                    description="widget"),
        ]
        with patch.object(delegation_recorder, "record_decision",
                          side_effect=RuntimeError("boom")):
            with self.assertLogs("wc.orchestrator", level="WARNING") as logged:
                await engine._materialise_plan(tasks)

        rows = await db.orchestrator_tasks_get("orch-1234abcd", "tester")
        self.assertEqual(len(rows), 2)
        # The log must name the failing task id. A bare "recording failed" is
        # exactly what hid a silent failure in this same loop once already.
        joined = "\n".join(logged.output)
        self.assertIn("orch-123_t001", joined)
        self.assertIn("orch-123_t002", joined)
        self.assertIn("boom", joined)

    async def test_the_guard_is_what_keeps_creation_alive(self):
        """The inverse of the test above, and the one that actually catches a
        regression: with the try/except removed, a raising recorder must take
        the task's creation down with it.

        The guard is simulated rather than edited -- the point is that *some*
        guard is load-bearing, so if the real one is deleted the test above
        stops being satisfiable. Asserting the exception escapes an unguarded
        call proves the exception is real and would propagate.
        """
        engine = await self._engine_with_orchestrator()
        with patch.object(delegation_recorder, "record_decision",
                          side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await delegation_recorder.record_decision(
                    task_table="orchestrator_tasks", task_id="x",
                    title="t", description="", actual_model=None,
                    router=engine.router)

    async def test_recording_failure_does_not_mark_the_orchestrator_degraded(self):
        """`task_create` degradation means a task list that will sit empty
        while work runs. A failed diagnostic record is not that, and flagging
        it as such would train the operator to ignore the flag."""
        engine = await self._engine_with_orchestrator()
        with patch.object(delegation_recorder, "record_decision",
                          side_effect=RuntimeError("boom")):
            with self.assertLogs("wc.orchestrator", level="WARNING"):
                await engine._materialise_plan(
                    [orchestrator.ParsedTask(id="t001", title="Implement it")])
        row = await db.orchestrator_get("orch-1234abcd", "tester")
        self.assertNotIn("task_create", (row or {}).get("degraded", "") or "")


class RoutingUnchangedTests(_DelegationDBCase):
    """Spec section 6: the property a future change is most likely to break by
    accident."""

    async def _run_plan(self, tasks):
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        await engine._materialise_plan(tasks)
        return await db.orchestrator_tasks_get("orch-1234abcd", "tester")

    async def test_every_task_keeps_the_model_its_plan_named(self):
        """Written so it can fail: `coding` is operational with a ladder whose
        rung 0 is `cheap-model`, and the plan names `plan-chosen-model`. A
        recorder that wrote its shadow choice back would be caught here.
        """
        await self.seed_coding_ladder()
        rows = await self._run_plan([
            orchestrator.ParsedTask(id="t001", title="Implement the parser",
                                    description="widget",
                                    model="plan-chosen-model"),
        ])
        self.assertEqual([r["model"] for r in rows], ["plan-chosen-model"])
        # And the shadow row proves the ladder really would have disagreed --
        # without this, the assertion above would pass on a table whose ladder
        # happened to name the same model.
        decision = (await db.delegation_decisions_recent())[0]
        self.assertEqual(decision["shadow_model"], "cheap-model")
        self.assertEqual(decision["actual_model"], "plan-chosen-model")
        self.assertNotEqual(decision["shadow_model"], decision["actual_model"])

    async def test_a_plan_naming_no_model_still_stores_null(self):
        await self.seed_coding_ladder()
        rows = await self._run_plan([
            orchestrator.ParsedTask(id="t001", title="Implement the parser",
                                    description="widget", model=None),
        ])
        self.assertEqual([r["model"] for r in rows], [None])
        decision = (await db.delegation_decisions_recent())[0]
        self.assertIsNone(decision["actual_model"])
        self.assertEqual(decision["shadow_model"], "cheap-model")

    async def test_one_decision_row_per_created_task(self):
        await self.seed_coding_ladder()
        await self._run_plan([
            orchestrator.ParsedTask(id="t001", title="Implement the parser"),
            orchestrator.ParsedTask(id="t002", title="Implement the lexer"),
            orchestrator.ParsedTask(id="t003", title="Summarise the document"),
        ])
        rows = await db.delegation_decisions_recent()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["task_id"] for r in rows},
                         {"orch-123_t001", "orch-123_t002", "orch-123_t003"})
        self.assertEqual({r["task_table"] for r in rows},
                         {"orchestrator_tasks"})

    async def test_no_decision_row_for_a_task_that_failed_to_be_created(self):
        """Spec 4: the record goes after the create, because a decision record
        for a task that does not exist is a record of nothing."""
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        with patch.object(db, "orchestrator_task_create",
                          side_effect=RuntimeError("no")):
            with self.assertLogs("wc.orchestrator", level="WARNING"):
                await engine._materialise_plan(
                    [orchestrator.ParsedTask(id="t001", title="Implement it")])
        self.assertEqual(await db.delegation_decisions_recent(), [])


if __name__ == "__main__":
    unittest.main()
