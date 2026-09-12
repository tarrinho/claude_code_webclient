"""QA: the generated_images table -- recording, owner-scoped listing,
self-healing reads, and delete. Mirrors test_qa_chat_files.py's fixture
style (temp DB + temp PROJECTS_ROOT)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

_OWNER = "o" * 32
_OTHER = "p" * 32


class GeneratedImagesDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.work = Path(self.tmp.name) / "p" / "chat"
        self.work.mkdir(parents=True)
        (self.work / "shot.png").write_bytes(b"x")

    async def _cleanup(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_a_recorded_image_is_listed(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, has_more = await db.generated_images_list(_OWNER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "shot.png")
        self.assertEqual(rows[0]["chat_title"], "Chat One")
        self.assertFalse(has_more)

    async def test_recording_the_same_path_twice_is_not_a_duplicate(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(len(rows), 1)

    async def test_another_owners_images_are_not_listed(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OTHER, ["shot.png"])
        rows, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(rows, [])

    async def test_a_row_whose_file_is_gone_is_skipped_on_read(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        (self.work / "shot.png").unlink()
        rows, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(rows, [])

    async def test_get_returns_none_for_another_owner(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, _ = await db.generated_images_list(_OWNER)
        image_id = rows[0]["id"]
        self.assertIsNone(await db.generated_image_get(image_id, _OTHER))
        self.assertIsNotNone(await db.generated_image_get(image_id, _OWNER))

    async def test_delete_removes_the_file_and_the_row(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, _ = await db.generated_images_list(_OWNER)
        image_id = rows[0]["id"]
        self.assertTrue(await db.generated_image_delete(image_id, _OWNER))
        self.assertFalse((self.work / "shot.png").exists())
        rows, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(rows, [])

    async def test_delete_with_missing_file_still_removes_the_row(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        (self.work / "shot.png").unlink()
        rows, _ = await db.generated_images_list(_OWNER)
        # The listing already filtered this row out, so fetch its id directly.
        cur = await db.db_conn.execute(
            "SELECT id FROM generated_images WHERE owner_id = ?", (_OWNER,))
        row = await cur.fetchone()
        self.assertTrue(await db.generated_image_delete(row["id"], _OWNER))

    async def test_delete_of_someone_elses_image_does_nothing(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        cur = await db.db_conn.execute(
            "SELECT id FROM generated_images WHERE owner_id = ?", (_OWNER,))
        row = await cur.fetchone()
        self.assertFalse(await db.generated_image_delete(row["id"], _OTHER))
        self.assertTrue((self.work / "shot.png").exists())

    async def test_pagination_cursor(self):
        for n in range(3):
            (self.work / f"s{n}.png").write_bytes(b"x")
            await db.generated_image_record(
                "c1", "Chat One", str(self.work), _OWNER, [f"s{n}.png"])
        page1, has_more = await db.generated_images_list(_OWNER, limit=2)
        self.assertEqual(len(page1), 2)
        self.assertTrue(has_more)
        page2, has_more2 = await db.generated_images_list(
            _OWNER, limit=2, before_id=page1[-1]["id"])
        self.assertEqual(len(page2), 1)
        self.assertFalse(has_more2)


if __name__ == "__main__":
    unittest.main()
