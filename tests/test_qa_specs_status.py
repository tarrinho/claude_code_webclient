"""QA: manual status override for the design-specs gallery.

Covers routes/db_specs.py's storage functions directly, and
routes/specs.py's overlay of that storage on top of specs_gallery.py's
auto-computed status (list + the new PUT .../status endpoint).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import config
import db
import routes.specs as specs_routes
import specs_gallery
from routes.db_specs import ALLOWED_STATUSES


def _request(role="user", body=None, query=None):
    # query_params is always present on a real Request, and handle_specs_list
    # reads it for ?refresh=1. A stand-in without it passes here and fails the
    # moment a handler consults the query string.
    session = SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": role}),
        query_params=query or {},
    )
    if body is not None:
        session.json = AsyncMock(return_value=body)
    return session


class _RealSpecsRootMixin:
    """Same shape as test_qa_specs_routes.py's mixin, plus a real temp DB so
    the new spec_status table exists (routes/db_specs.py needs db.db_conn)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        specs_dir = self.root / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        self.spec_path = specs_dir / "2026-01-01-a-design.md"
        self.spec_path.write_text("# A Design\n\nbody text\n")
        self._patcher = patch.object(specs_routes, "_REPO_ROOT", self.root)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()

    @property
    def rel_spec_path(self) -> str:
        return str(self.spec_path.relative_to(self.root))


class SpecStatusStorageTests(_RealSpecsRootMixin, unittest.IsolatedAsyncioTestCase):
    """routes/db_specs.py's functions directly, no HTTP layer involved."""

    async def test_unset_path_is_absent_from_get_all(self):
        result = await db.spec_status_get_all({self.rel_spec_path})
        self.assertEqual(result, {})

    async def test_set_then_get_all_round_trips(self):
        ok = await db.spec_status_set(self.rel_spec_path, "planning")
        self.assertTrue(ok)
        result = await db.spec_status_get_all({self.rel_spec_path})
        self.assertEqual(result, {self.rel_spec_path: "planning"})

    async def test_set_again_overwrites_rather_than_duplicates(self):
        await db.spec_status_set(self.rel_spec_path, "planning")
        await db.spec_status_set(self.rel_spec_path, "done")
        result = await db.spec_status_get_all({self.rel_spec_path})
        self.assertEqual(result, {self.rel_spec_path: "done"})

    async def test_invalid_status_is_rejected_and_not_written(self):
        ok = await db.spec_status_set(self.rel_spec_path, "bogus")
        self.assertFalse(ok)
        result = await db.spec_status_get_all({self.rel_spec_path})
        self.assertEqual(result, {})

    async def test_empty_path_set_returns_empty_dict(self):
        result = await db.spec_status_get_all(set())
        self.assertEqual(result, {})


class SpecStatusEndpointTests(_RealSpecsRootMixin, unittest.IsolatedAsyncioTestCase):
    """PUT /api/specs/{id}/status via handle_spec_status_set."""

    async def test_non_admin_gets_403(self):
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_status_set(
                _request(role="user", body={"status": "done"}), "whatever")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_unknown_spec_id_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_status_set(
                _request(role="admin", body={"status": "done"}), "not-a-real-id")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_a_real_non_spec_file_is_404_not_settable(self):
        """Same membership gap the delete/content routes already guard --
        a manual status must not be settable on a file that is not one of
        discover_specs()'s own entries."""
        non_spec = self.root / "config.py"
        non_spec.write_text("SECRET = 1\n")
        spec_id = specs_gallery.encode_id("config.py")
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_status_set(
                _request(role="admin", body={"status": "done"}), spec_id)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_invalid_status_value_is_400(self):
        spec_id = specs_gallery.encode_id(self.rel_spec_path)
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_status_set(
                _request(role="admin", body={"status": "bogus"}), spec_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_missing_status_field_is_400(self):
        spec_id = specs_gallery.encode_id(self.rel_spec_path)
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_status_set(
                _request(role="admin", body={}), spec_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_every_allowed_status_is_accepted(self):
        spec_id = specs_gallery.encode_id(self.rel_spec_path)
        for status in ALLOWED_STATUSES:
            resp = await specs_routes.handle_spec_status_set(
                _request(role="admin", body={"status": status}), spec_id)
            body = json.loads(resp.body)
            self.assertEqual(body, {"ok": True, "status": status})


class SpecStatusOverlayInListTests(_RealSpecsRootMixin, unittest.IsolatedAsyncioTestCase):
    """handle_specs_list overlays the manual status when one has been set,
    and leaves the auto-computed one alone otherwise."""

    async def test_no_override_keeps_auto_status_and_manual_flag_false(self):
        resp = await specs_routes.handle_specs_list(_request())
        body = json.loads(resp.body)
        entry = body["specs"][0]
        self.assertEqual(entry["status"], "spec_only")  # no plan dir exists
        self.assertFalse(entry["status_manual"])

    async def test_override_replaces_status_and_sets_manual_flag_true(self):
        await db.spec_status_set(self.rel_spec_path, "implementing")
        resp = await specs_routes.handle_specs_list(_request())
        body = json.loads(resp.body)
        entry = body["specs"][0]
        self.assertEqual(entry["status"], "implementing")
        self.assertTrue(entry["status_manual"])


if __name__ == "__main__":
    unittest.main()
