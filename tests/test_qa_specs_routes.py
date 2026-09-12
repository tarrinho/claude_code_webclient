"""QA: routes/specs.py -- list/content/delete for the design specs gallery.
Design: docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import routes.specs as specs_routes
import specs_gallery


def _request(role="user"):
    return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": role}))


class _RealSpecsRootMixin:
    """A temp tree shaped like a real checkout: a genuine spec under
    docs/superpowers/specs/, plus a non-spec file (config.py) sitting right
    next to the specs root -- the exact shape C2's exploit used
    (decode_id(encode_id("config.py"), repo_root) resolving successfully).
    Patches routes.specs._REPO_ROOT for the test's duration so the real
    (unmocked) discover_specs/enrich/decode_id/encode_id run against it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        specs_dir = self.root / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        self.spec_path = specs_dir / "2026-01-01-a-design.md"
        self.spec_path.write_text("# A Design\n\nbody text\n")
        self.non_spec_path = self.root / "config.py"
        self.non_spec_path.write_text("SECRET = 'not-a-spec'\n")
        self._patcher = patch.object(specs_routes, "_REPO_ROOT", self.root)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)


class ListRouteTests(_RealSpecsRootMixin, unittest.IsolatedAsyncioTestCase):
    async def test_list_entries_carry_a_working_id(self):
        """Regression test for C1: encode_id() existed but nothing on the
        list path ever called it, so every entry the API returned omitted
        `id` and view/delete were both non-functional. Task 4's original
        test mocked discover_specs/enrich with hand-written dicts that also
        omitted id, so it agreed with the bug instead of catching it --
        this one runs the real discover_specs/enrich/encode_id against a
        real temp file instead."""
        resp = await specs_routes.handle_specs_list(_request())
        body = json.loads(resp.body)
        specs = body["specs"]
        self.assertEqual(len(specs), 1)
        entry = specs[0]
        self.assertTrue(entry.get("id"))
        decoded = specs_gallery.decode_id(entry["id"], self.root)
        self.assertEqual(decoded, self.spec_path)


class ContentRouteTests(_RealSpecsRootMixin, unittest.IsolatedAsyncioTestCase):
    async def test_unknown_id_is_404(self):
        with patch.object(specs_gallery, "decode_id", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await specs_routes.handle_spec_content(_request(), "bogus")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_a_real_non_spec_file_is_404_not_served(self):
        """Regression test for C2: decode_id() only checked that the
        resolved path stayed inside root and was a file -- it never checked
        the path was actually one of the discovered specs. That let
        decode_id(encode_id("config.py"), repo_root) resolve successfully,
        and handle_spec_content served config.py's real source as "spec
        content". config.py here is a real, existing, non-spec file sitting
        in the temp root right next to the real spec -- the same shape as
        the confirmed exploit."""
        spec_id = specs_gallery.encode_id("config.py")
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_content(_request(), spec_id)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_a_genuine_spec_still_renders(self):
        """Positive case alongside the regression test above: the
        membership check must not reject real specs, only non-specs."""
        spec_id = specs_gallery.encode_id("docs/superpowers/specs/2026-01-01-a-design.md")
        resp = await specs_routes.handle_spec_content(_request(), spec_id)
        self.assertIn(b"A Design", resp.body)


class DeleteRouteTests(_RealSpecsRootMixin, unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_gets_403(self):
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_delete(_request(role="user"), "whatever")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_real_non_spec_file_is_404_not_deleted(self):
        """Regression test for C2's other half: the same membership gap let
        an admin delete any file in the repo, not just specs. config.py
        must survive a delete request built from its own (validly encoded,
        validly contained) id."""
        spec_id = specs_gallery.encode_id("config.py")
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_delete(_request(role="admin"), spec_id)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertTrue(self.non_spec_path.exists())

    async def test_admin_deletes_a_genuine_spec_and_never_touches_git(self):
        spec_id = specs_gallery.encode_id("docs/superpowers/specs/2026-01-01-a-design.md")
        with patch("subprocess.run") as mock_run:
            resp = await specs_routes.handle_spec_delete(_request(role="admin"), spec_id)
        mock_run.assert_not_called()
        self.assertFalse(self.spec_path.exists())
        self.assertIn(b'"ok":true', resp.body.replace(b" ", b""))

    async def test_admin_delete_of_already_gone_file_is_idempotent(self):
        with patch.object(specs_gallery, "decode_id", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await specs_routes.handle_spec_delete(_request(role="admin"), "whatever")
        self.assertEqual(ctx.exception.status_code, 404)
