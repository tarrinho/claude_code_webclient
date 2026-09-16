# tests/test_qa_delegation_startup.py
"""QA: spec 1.1's startup validation, and its bootstrap exemption.

The exemption is the part worth testing hardest. Section 2.6 ships mostly
unmeasured, so a check that refused every TBD would mean the system could
never start a first time -- and a validation gate that cannot be satisfied
gets satisfied with junk values instead.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
import delegation_startup as ds


class StartupValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _row(self, model, task_type, **kw):
        defaults = dict(accuracy=None, n=None, cost_per_1m_tokens=None,
                        median_latency_s=None, max_context=None)
        defaults.update(kw)
        await db.delegation_row_set(model, task_type, **defaults)

    async def test_an_empty_table_starts_fine(self):
        """The bootstrap case: nothing measured, nothing operational."""
        table = await ds.validate_or_die()
        self.assertEqual(table.ladder("coding"), [])

    async def test_a_non_operational_type_may_hold_tbd(self):
        await self._row("m", "coding", cost_per_1m_tokens=1.0)
        await ds.validate_or_die()  # must not raise

    async def test_an_operational_type_with_a_tbd_ladder_row_refuses_to_start(self):
        await self._row("m", "coding", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await db.delegation_operational_set("coding", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        self.assertIn("coding", str(ctx.exception))

    async def test_an_operational_type_with_no_eligible_row_refuses_to_start(self):
        """'At least one ladder-eligible row must exist' -- a type whose rows
        are all TBD has an empty ladder, and an empty ladder cannot route."""
        await self._row("m", "coding", cost_per_1m_tokens=1.0)
        await db.delegation_operational_set("coding", True)
        with self.assertRaises(ds.DelegationConfigError):
            await ds.validate_or_die()

    async def test_a_complete_operational_type_starts(self):
        await self._row("m", "long-context", accuracy=1.0, n=10,
                        cost_per_1m_tokens=0.0, median_latency_s=12.0,
                        max_context=229376)
        await db.delegation_operational_set("long-context", True)
        table = await ds.validate_or_die()
        self.assertEqual(table.ladder("long-context"), ["m"])

    async def test_the_error_names_every_broken_invariant_not_just_the_first(self):
        """'The error lists every broken invariant so the operator can fix the
        data before deployment' -- one at a time means one restart each."""
        await self._row("a", "coding", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await self._row("b", "reasoning", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await db.delegation_operational_set("coding", True)
        await db.delegation_operational_set("reasoning", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        message = str(ctx.exception)
        self.assertIn("coding", message)
        self.assertIn("reasoning", message)


if __name__ == "__main__":
    unittest.main()
