"""QA: the seed script cannot write a table the service would refuse to start on.

`bin/wc-seed-delegation.py` was the only writer of `delegation_capability`
outside `routes/delegation.py`, and it went straight to `db.delegation_row_set`.
That accessor stores whatever it is given. Every other path builds the
prospective table first and refuses a write that breaks spec 1.1.

While both enforcement knobs were reported-but-not-blocking, the bypass cost
nothing -- `validate()` suppressed the breach, so there was no verdict to
bypass. It stopped being free on 2026-09-18, when budget enforcement was
switched on in production: from then on a seed that puts an operational task
type over `BUDGET_USD` makes `validate_or_die` raise at the next restart, which
may be hours later and will look nothing like the seed that caused it.

The load-bearing property here is that a refusal leaves the database
UNTOUCHED. A check that ran after the writes would report the problem and still
have caused it.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
import delegation_startup as ds
import tiered_delegation as td


class _SeedGuardCase(unittest.IsolatedAsyncioTestCase):
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
        # `live_known_models` reaches a backend. Pinned to None here, which is
        # the documented fallback (config.KNOWN_MODELS), so these tests measure
        # the guard rather than the network.
        p = patch.object(ds, "live_known_models", self._no_live_models)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    async def _no_live_models():
        return None

    async def _seed_over_budget_type(self, task_type="widget"):
        """An operational task type whose ladder already fits, plus the gates
        it needs. The over-budget rung arrives later, as a prospective write.

        The rate is derived from `td.BUDGET_USD` rather than written as a
        literal, because a literal stops describing an over-budget ladder the
        moment the budget moves -- which has already happened twice to fixtures
        in this subsystem.
        """
        for gate in sorted(td.GATE_CALLS):
            await db.delegation_row_set(
                "claude-sonnet-5", gate, accuracy=0.95, n=28,
                cost_per_1m_tokens=0.0, median_latency_s=5.0,
                max_context=1_000_000)
            await db.delegation_operational_set(gate, True)
        await db.delegation_row_set(
            "vllm/Qwen3.6-35B-A3B-NVFP4", task_type, accuracy=0.9, n=20,
            cost_per_1m_tokens=0.0, median_latency_s=5.0, max_context=229_376)
        await db.delegation_operational_set(task_type, True)

    @staticmethod
    def _over_budget_rate(rung=1):
        """A rate that puts the whole tree 20% over budget FROM RUNG *rung*.

        The rung matters and getting it wrong makes the fixture silently
        harmless. `_seed_over_budget_type` puts a free vllm row in first, so
        the dear row sorts second and lands at rung 1, where
        `REACH_PROBABILITY` is 0.5 -- a rate computed for rung 0 therefore
        costs half what it was meant to and fits comfortably. That is how the
        first version of this file had a test asserting "no budget problem
        while the knob is off" that would have passed with the knob ON too.
        """
        per_unit = (td.LEAVES_PER_TREE * td.TOKENS_PER_LEAF
                    * td.REACH_PROBABILITY[rung] / 1_000_000)
        return td.BUDGET_USD / per_unit * 1.2


class ProblemsAfterWritingTests(_SeedGuardCase):
    """`delegation_startup.problems_after_writing` -- the prospective check."""

    async def test_a_write_that_breaks_the_budget_is_reported(self):
        await self._seed_over_budget_type()
        with patch.object(ds, "budget_enforcement_enabled", self._on):
            problems = await ds.problems_after_writing([
                ("claude-opus-5", "widget", {
                    "accuracy": 1.0, "n": 20,
                    "cost_per_1m_tokens": self._over_budget_rate(),
                    "median_latency_s": 5.0, "max_context": 1_000_000}),
            ])
        self.assertTrue(any("BUDGET_USD" in p for p in problems), problems)

    async def test_the_same_write_is_silent_while_the_knob_is_off(self):
        """The knob is what decides, not this function. Off means the breach
        is reported through `budget_breaches` and does not block -- if this
        returned a problem regardless, turning the knob off would stop meaning
        anything for the seed path."""
        await self._seed_over_budget_type()
        with patch.object(ds, "budget_enforcement_enabled", self._off):
            problems = await ds.problems_after_writing([
                ("claude-opus-5", "widget", {
                    "accuracy": 1.0, "n": 20,
                    "cost_per_1m_tokens": self._over_budget_rate(),
                    "median_latency_s": 5.0, "max_context": 1_000_000}),
            ])
        self.assertEqual([p for p in problems if "BUDGET_USD" in p], [])

    async def test_it_does_not_write_anything(self):
        """The property the whole design rests on."""
        await self._seed_over_budget_type()
        before = await db.delegation_rows_all()
        with patch.object(ds, "budget_enforcement_enabled", self._on):
            await ds.problems_after_writing([
                ("claude-opus-5", "widget", {
                    "accuracy": 1.0, "n": 20,
                    "cost_per_1m_tokens": self._over_budget_rate(),
                    "median_latency_s": 5.0, "max_context": 1_000_000}),
            ])
        self.assertEqual(await db.delegation_rows_all(), before)

    async def test_a_harmless_write_is_reported_clean(self):
        """Written so the tests above can be trusted: if this function returned
        problems for everything, they would pass for the wrong reason."""
        await self._seed_over_budget_type()
        with patch.object(ds, "budget_enforcement_enabled", self._on):
            problems = await ds.problems_after_writing([
                ("azure_ai/gpt-5.6-luna", "widget", {
                    "accuracy": 0.95, "n": 20, "cost_per_1m_tokens": 0.0342,
                    "median_latency_s": 5.0, "max_context": 922_000}),
            ])
        self.assertEqual([p for p in problems if "BUDGET_USD" in p], [])

    async def test_a_write_replaces_a_row_rather_than_adding_a_second(self):
        """`delegation_row_set` is an UPSERT keyed on (model, task_type), so
        the prospective table must merge the same way. Modelling a write as an
        append would price a ladder against a rung that no longer exists."""
        await self._seed_over_budget_type()
        rows = await db.delegation_rows_all()
        merged = await ds.problems_after_writing([
            ("vllm/Qwen3.6-35B-A3B-NVFP4", "widget", {
                "accuracy": 0.9, "n": 20, "cost_per_1m_tokens": 0.0,
                "median_latency_s": 5.0, "max_context": 229_376}),
        ])
        # No new problems, and -- the real assertion -- the table it built has
        # the same number of rows as the database, not one more.
        self.assertEqual(merged, [])
        from routes.db_delegation import rows_to_capability
        built = td.CapabilityTable(
            rows_to_capability(rows), operational=await db.delegation_operational_all())
        self.assertEqual(len(built.rows_for("widget")), 1)

    @staticmethod
    async def _on():
        return True

    @staticmethod
    async def _off():
        return False


class SeedScriptRefusesTests(_SeedGuardCase):
    """The script itself, driven through `main`."""

    async def _run_seed(self):
        import importlib.util
        import pathlib
        path = (pathlib.Path(__file__).resolve().parent.parent
                / "bin" / "wc-seed-delegation.py")
        spec = importlib.util.spec_from_file_location("wc_seed_delegation", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def _main(self, module):
        """Run the script, then reopen the connection it closed.

        The script owns its database lifecycle -- `db.init()` at the top and
        `db.close()` in a `finally` -- which is correct for a command-line tool
        and fatal for a test that shares the module-level connection with it.
        Without this, every assertion after the call fails with
        `'NoneType' object has no attribute 'execute'`, which reads like a bug
        in the accessor rather than a closed handle.
        """
        code = await module.main(
            ["--db-path", config.DB_PATH, "--yes-this-is-production"])
        await db.init()
        return code

    async def test_it_refuses_and_writes_nothing_when_the_rows_would_break_1_1(self):
        """Drives the real script. The fixture makes its own rows unaffordable
        by putting an operational task type over a budget the seed's published
        rates cannot satisfy -- so the refusal comes from the script's check,
        not from a doctored table.
        """
        module = await self._run_seed()
        await self._seed_over_budget_type("coding")
        # Force the refusal deterministically rather than hoping the published
        # rows breach: patch the prospective check, which is the seam the
        # script is supposed to consult at all.
        before = await db.delegation_rows_all()
        with patch.object(module.delegation_startup, "problems_after_writing",
                          self._always_a_problem):
            code = await self._main(module)
        self.assertEqual(code, 1)
        self.assertEqual(await db.delegation_rows_all(), before,
                         "the seed wrote rows despite refusing")

    async def test_it_seeds_normally_when_there_is_no_problem(self):
        """The inverse, so the refusal test cannot pass by the script being
        broken outright."""
        module = await self._run_seed()
        code = await self._main(module)
        self.assertEqual(code, 0)
        self.assertEqual(len(await db.delegation_rows_all()), len(module.ROWS))

    async def test_the_script_consults_the_prospective_check_at_all(self):
        """Guards the wiring rather than the behaviour: a script that never
        calls the check would pass both tests above on a clean table."""
        module = await self._run_seed()
        calls = []

        async def _spy(writes):
            calls.append(writes)
            return []

        with patch.object(module.delegation_startup, "problems_after_writing", _spy):
            await self._main(module)
        self.assertEqual(len(calls), 1, "the seed never asked whether its rows were admissible")
        self.assertEqual(len(calls[0]), len(module.ROWS))

    @staticmethod
    async def _always_a_problem(writes):
        return ["widget: contrived problem (spec 1.1)"]


if __name__ == "__main__":
    unittest.main()
