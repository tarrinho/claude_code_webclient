# tests/test_qa_delegation_table.py
"""QA: the spec 2.6 benchmark table as a real database table.

Section 2.6 is explicit that this is a table rather than a constant, because
three separate things write and read it. The properties pinned here are the
ones that make it usable as a source of truth: a row round-trips with its
NULLs intact, and NULL is not confused with zero.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
from routes.db_delegation import rows_to_capability


class DelegationTableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start(); self.root_patch.start()
        self.addCleanup(self.db_patch.stop); self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_a_row_round_trips(self):
        await db.delegation_row_set(
            "vllm/Qwen3.6-35B-A3B-NVFP4", "coding",
            accuracy=0.66, n=44, cost_per_1m_tokens=0.0,
            median_latency_s=26.8, max_context=229376)
        rows = await db.delegation_rows_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(rows[0]["n"], 44)

    async def test_null_accuracy_is_preserved_and_is_not_zero(self):
        """A TBD accuracy makes a row ineligible (2.6). Reading it back as 0.0
        would make it eligible and measured-worst, which is the opposite."""
        await db.delegation_row_set("azure_ai/gpt-5.4-mini", "coding",
                                    accuracy=None, n=None,
                                    cost_per_1m_tokens=0.5261,
                                    median_latency_s=None, max_context=1050000)
        rows = await db.delegation_rows_all()
        self.assertIsNone(rows[0]["accuracy"])

    async def test_zero_cost_is_preserved_and_is_not_null(self):
        """Zero is a real rate -- the self-hosted model is free. None means
        unpriced, which 2.7 makes ineligible rather than free."""
        await db.delegation_row_set("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        rows = await db.delegation_rows_all()
        self.assertEqual(rows[0]["cost_per_1m_tokens"], 0.0)
        self.assertIsNotNone(rows[0]["cost_per_1m_tokens"])

    async def test_setting_the_same_pair_twice_updates_rather_than_duplicates(self):
        await db.delegation_row_set("m", "coding", accuracy=0.5, n=2,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=1.0, max_context=1000)
        await db.delegation_row_set("m", "coding", accuracy=0.9, n=20,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=1.0, max_context=1000)
        rows = await db.delegation_rows_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["accuracy"], 0.9)

    async def test_no_task_type_is_operational_by_default(self):
        """1.1's bootstrap exemption, and this plan's whole scope: nothing
        routes until something is flipped, and nothing flips it here."""
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_operational_can_be_set_and_cleared(self):
        await db.delegation_operational_set("long-context", True)
        self.assertEqual(await db.delegation_operational_all(), {"long-context"})
        await db.delegation_operational_set("long-context", False)
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_rows_convert_to_capability_rows(self):
        await db.delegation_row_set("m", "coding", accuracy=0.66, n=44,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=26.8, max_context=229376)
        rows = rows_to_capability(await db.delegation_rows_all())
        self.assertEqual(rows[0].model, "m")
        self.assertEqual(rows[0].accuracy, 0.66)


if __name__ == "__main__":
    unittest.main()
