"""QA: routes/specs.py -- list/content/delete for the design specs gallery.
Design: docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import routes.specs as specs_routes
import specs_gallery


def _request(role="user"):
    return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": role}))


class ListRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_returns_enriched_entries(self):
        with patch.object(specs_gallery, "discover_specs", return_value=[
                {"path": "docs/superpowers/specs/x-design.md", "title": "X", "mtime": 1.0}]), \
             patch.object(specs_gallery, "enrich", side_effect=lambda root, s: {**s, "referenced_by": [], "status": "spec_only", "author": None, "date": None}):
            resp = await specs_routes.handle_specs_list(_request())
        body = resp.body
        self.assertIn(b"docs/superpowers/specs/x-design.md", body)


class ContentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_id_is_404(self):
        with patch.object(specs_gallery, "decode_id", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await specs_routes.handle_spec_content(_request(), "bogus")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_known_id_returns_rendered_html(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.md"
        path.write_text("# Hello\n")
        with patch.object(specs_gallery, "decode_id", return_value=path), \
             patch.object(specs_gallery, "render_markdown", return_value="<h1>Hello</h1>"):
            resp = await specs_routes.handle_spec_content(_request(), "whatever")
        self.assertIn(b"<h1>Hello</h1>", resp.body)


class DeleteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_gets_403(self):
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_delete(_request(role="user"), "whatever")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_admin_deletes_and_never_touches_git(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.md"
        path.write_text("# Hello\n")
        with patch.object(specs_gallery, "decode_id", return_value=path), \
             patch("subprocess.run") as mock_run:
            resp = await specs_routes.handle_spec_delete(_request(role="admin"), "whatever")
        mock_run.assert_not_called()
        self.assertFalse(path.exists())
        self.assertIn(b'"ok":true', resp.body.replace(b" ", b""))

    async def test_admin_delete_of_already_gone_file_is_idempotent(self):
        with patch.object(specs_gallery, "decode_id", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await specs_routes.handle_spec_delete(_request(role="admin"), "whatever")
        self.assertEqual(ctx.exception.status_code, 404)
