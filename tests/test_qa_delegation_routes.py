"""QA: the settings page's endpoints -- spec 9.2.

The load-bearing property is that a write which would break an invariant of an
operational task type is REFUSED, not stored. The table is live-editable and
the ladders regenerate from it, so a check that ran only at startup would let
an operator break routing at 15:00 and discover it at the next restart.

Two of the scenarios below (the "allowed" flip, and the unknown-column
rejection) go beyond the values sketched in the task brief:

* the "flip succeeds" scenario needs a *reviewer-gate* row and a model id
  that actually resolves against `config.KNOWN_MODELS`, because
  `CapabilityTable.validate()` (already committed, tiered_delegation.py)
  checks model resolution and the worst-case-path's gate latency for every
  operational type -- a fixture with no reviewer-gate row and a fake model
  id cannot pass those checks no matter how the route is written, so a test
  that used them would never be satisfiable by a spec-correct implementation.
* the unknown-column test exercises the "validate and constrain what the
  write endpoint accepts" requirement directly at the route layer, rather
  than relying on `db.delegation_row_set`'s own `ValueError` to leak out as
  an unhandled 500.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import routes.delegation as delegation_routes


def _request(role="admin", body=None, query=None):
    request = SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": role}),
        query_params=query or {},
    )
    if body is not None:
        request.json = AsyncMock(return_value=body)
    return request


class DelegationRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_the_list_reports_nothing_operational(self):
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        self.assertEqual(body["operational"], [])

    async def test_a_row_can_be_written_and_read_back(self):
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.9, "n": 4,
            "cost_per_1m_tokens": 1.0, "median_latency_s": 2.0,
            "max_context": 1000}))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(len(body["rows"]), 1)

    async def test_a_non_admin_cannot_write(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(role="user", body={
                "model": "m", "task_type": "coding"}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_non_admin_cannot_flip_operational(self):
        """The flip endpoint is the highest-privilege action in this change --
        it submits a task type to production routing -- and had no test of
        its own admin gate; only handle_row_put's was covered."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(role="user", body={
                "task_type": "coding", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_non_dict_row_body_is_refused_with_400(self):
        """A bare list or string body must not reach the first `.get()` call
        and surface as an unhandled 500."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body=["not", "an", "object"]))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_dict_operational_body_is_refused_with_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body="x"))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_numeric_value_is_refused_and_the_page_stays_readable(self):
        """Reproduced defect: a PUT with accuracy="abc" used to return 200 and
        store TEXT (SQLite REAL affinity does not coerce it), and every later
        GET raised TypeError from CapabilityTable.ladder's sort key -- a
        permanent 500 on the only page that could fix the cell. The write must
        be refused, and a normal GET afterwards must still succeed."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "m", "task_type": "coding", "accuracy": "abc"}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("accuracy", str(ctx.exception.detail))
        response = await delegation_routes.handle_delegation_get(_request())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["rows"], [])

    async def test_a_dict_value_is_also_refused(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "m", "task_type": "coding", "cost_per_1m_tokens": {"a": 1}}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_an_unknown_column_is_refused_not_passed_through(self):
        """'Validate and constrain what the write endpoint accepts. Unknown
        column names must be refused, not passed through.' A typo in a column
        name (or a probing client) must get a 400 naming the problem, not a
        write of the four columns it did recognise."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "m", "task_type": "coding", "accuracy": 0.9,
                "bogus_column": 1}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("bogus_column", str(ctx.exception.detail))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(body["rows"], [])

    async def test_a_partial_write_does_not_null_the_other_columns(self):
        """`db.delegation_row_set` is a full-row UPSERT with no partial-column
        mode (an omitted keyword is stored as None), but the settings page
        edits one cell at a time (delegation.js's `_saveRow` sends exactly one
        column per PUT). The route must merge onto the stored row, or every
        single-cell edit would blank the other four columns."""
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.9, "n": 4,
            "cost_per_1m_tokens": 1.0, "median_latency_s": 2.0,
            "max_context": 1000}))
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.95}))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(len(body["rows"]), 1)
        row = body["rows"][0]
        self.assertEqual(row["accuracy"], 0.95)
        self.assertEqual(row["n"], 4)
        self.assertEqual(row["cost_per_1m_tokens"], 1.0)
        self.assertEqual(row["median_latency_s"], 2.0)
        self.assertEqual(row["max_context"], 1000)

    async def test_a_row_write_that_would_break_an_already_operational_type_is_refused(self):
        """The scoping rule (spec 1.1) is that a write is checked against the
        invariants of task types *already* operational -- exercised here by
        flipping one operational first, then writing a row that blanks a
        column a ladder-eligible row of that type needs."""
        from fastapi import HTTPException
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=None, n=None,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=None)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))

        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "claude-sonnet-5", "task_type": "long-context",
                "median_latency_s": None}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("long-context", str(ctx.exception.detail))
        # And the stored row is untouched by the refused write.
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        row = next(r for r in body["rows"] if r["task_type"] == "long-context")
        self.assertEqual(row["median_latency_s"], 12.0)

    async def test_flipping_a_type_operational_on_incomplete_data_is_refused(self):
        """9.2: the flip re-runs 1.1's validation immediately and refuses with
        the offending column named, rather than accepting it and failing at the
        next restart."""
        from fastapi import HTTPException
        await db.delegation_row_set("m", "coding", accuracy=0.9, n=4,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "coding", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("coding", str(ctx.exception.detail))

    async def test_flipping_a_complete_type_operational_is_allowed(self):
        # A real, resolvable model id (spec 1.1's model-resolution invariant)
        # and a reviewer-gate row (5.1's gate latency, used by the worst-case
        # check) are both required for validate() to clear -- see the module
        # docstring.
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=None, n=None,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=None)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))
        self.assertEqual(await db.delegation_operational_all(), {"long-context"})

    async def test_a_refused_flip_leaves_the_stored_state_alone(self):
        """'the stored value is left as it was' -- a rejected write that half
        applied would be worse than one that failed outright."""
        from fastapi import HTTPException
        await db.delegation_row_set("m", "coding", accuracy=0.9, n=4,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        with self.assertRaises(HTTPException):
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "coding", "operational": True}))
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_coding_is_never_flipped_operational_by_this_module(self):
        """Spec 12 forbids flipping `coding` operational until the gate-type
        validation question is decided. Nothing in this route module may do
        it -- not as a default, not as a side effect of another call."""
        self.assertEqual(await db.delegation_operational_all(), set())
        await delegation_routes.handle_delegation_get(_request())
        self.assertEqual(await db.delegation_operational_all(), set())


if __name__ == "__main__":
    unittest.main()
