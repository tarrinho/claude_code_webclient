"""QA: serving an image out of a chat's workspace, and refusing everything else.

This is the first endpoint in the application that reads a file the user
names, in a tool whose runner already launches Claude with
--dangerously-skip-permissions. The boundary is therefore the narrowest one
that still works: a chat may read images inside its own work_dir and nowhere
else. These tests are mostly about what it refuses.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import config
import db
from routes import chats as chat_routes

# The smallest valid PNG, so the tests exercise a real file rather than bytes
# that happen to have the right extension.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class _Req:
    def __init__(self, path=None, user="admin"):
        self.state = SimpleNamespace(session={"user": user, "role": "admin"})
        self.query_params = {} if path is None else {"path": path}


class ChatFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.root / "p"))
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.work = self.root / "p" / "chat"
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "shot.png").write_bytes(_PNG)
        (self.work / "notes.txt").write_text("not an image")
        # A file the workspace must never reach, one level up.
        (self.root / "p" / "outside.png").write_bytes(_PNG)
        await db.chat_create("c1", "Chat", None, str(self.work), "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _get(self, path):
        return await chat_routes.handle_chat_file(_Req(path), "c1")

    async def test_serves_an_image_from_the_workspace(self):
        resp = await self._get("shot.png")
        self.assertEqual(resp.media_type, "image/png")
        self.assertEqual(Path(resp.path).read_bytes(), _PNG)

    async def test_renders_inline_rather_than_downloading(self):
        resp = await self._get("shot.png")
        self.assertIn("inline", resp.headers["content-disposition"])

    async def test_traversal_is_refused(self):
        for path in (
            "../outside.png",
            "../../outside.png",
            "shot.png/../../outside.png",
            "./../outside.png",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPException) as ctx:
                    await self._get(path)
                self.assertEqual(ctx.exception.status_code, 403)

    async def test_absolute_path_is_refused(self):
        """An absolute path would otherwise escape by replacing the root."""
        with self.assertRaises(HTTPException) as ctx:
            await self._get("/etc/passwd")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_symlink_out_of_the_workspace_is_refused(self):
        """resolve() is why: a prefix check on the raw path would pass this."""
        link = self.work / "escape.png"
        link.symlink_to(self.root / "p" / "outside.png")
        with self.assertRaises(HTTPException) as ctx:
            await self._get("escape.png")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_non_image_inside_the_workspace_is_refused(self):
        """Otherwise this reads any file a turn happened to write."""
        with self.assertRaises(HTTPException) as ctx:
            await self._get("notes.txt")
        self.assertEqual(ctx.exception.status_code, 415)

    async def test_missing_file_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._get("nope.png")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_empty_path_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._get("   ")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_another_owners_chat_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_file(_Req("shot.png", user="bob"), "c1")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_oversized_image_is_refused(self):
        big = self.work / "big.png"
        big.write_bytes(_PNG)
        with (
            patch.object(chat_routes, "_IMAGE_MAX_BYTES", 4),
            self.assertRaises(HTTPException) as ctx,
        ):
            await self._get("big.png")
        self.assertEqual(ctx.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
