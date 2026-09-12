"""QA: /api/images -- list, serve, delete. Mirrors test_qa_chat_files.py's
fixture style and its "404, never 403" convention for another owner's row."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import config
import db
from routes import images as images_routes

_OWNER = "o" * 32
_OTHER = "p" * 32


class _Req:
    def __init__(self, user=_OWNER, query=None):
        self.state = SimpleNamespace(session={"user": user, "role": "admin"})
        self.query_params = query or {}


class ImagesApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.work = Path(self.tmp.name) / "p" / "chat"
        self.work.mkdir(parents=True)
        (self.work / "shot.png").write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"))
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _image_id(self):
        rows, _ = await db.generated_images_list(_OWNER)
        return rows[0]["id"]

    async def test_list_returns_the_owners_image(self):
        resp = await images_routes.handle_images_list(_Req())
        body = resp.body
        import json
        data = json.loads(body)
        self.assertEqual(len(data["images"]), 1)
        self.assertEqual(data["images"][0]["path"], "shot.png")
        self.assertFalse(data["has_more"])

    async def test_list_does_not_include_another_owners_image(self):
        resp = await images_routes.handle_images_list(_Req(user=_OTHER))
        import json
        data = json.loads(resp.body)
        self.assertEqual(data["images"], [])

    async def test_serve_returns_the_file(self):
        image_id = await self._image_id()
        resp = await images_routes.handle_image_file(_Req(), image_id)
        self.assertEqual(resp.media_type, "image/png")

    async def test_serve_survives_the_chat_being_deleted(self):
        """The whole point of snapshotting work_dir: no chats row exists
        for chat_id "c1" at all in this test, and serving still works."""
        image_id = await self._image_id()
        resp = await images_routes.handle_image_file(_Req(), image_id)
        self.assertEqual(resp.media_type, "image/png")

    async def test_serve_another_owners_image_is_404(self):
        image_id = await self._image_id()
        with self.assertRaises(HTTPException) as ctx:
            await images_routes.handle_image_file(_Req(user=_OTHER), image_id)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_serve_unknown_id_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await images_routes.handle_image_file(_Req(), 99999)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_delete_removes_it_from_a_later_list(self):
        image_id = await self._image_id()
        await images_routes.handle_image_delete(_Req(), image_id)
        resp = await images_routes.handle_images_list(_Req())
        import json
        data = json.loads(resp.body)
        self.assertEqual(data["images"], [])

    async def test_delete_another_owners_image_is_404_and_does_nothing(self):
        image_id = await self._image_id()
        with self.assertRaises(HTTPException) as ctx:
            await images_routes.handle_image_delete(_Req(user=_OTHER), image_id)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertTrue((self.work / "shot.png").exists())


if __name__ == "__main__":
    unittest.main()
