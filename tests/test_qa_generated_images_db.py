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
        rows, has_more, next_before_id = await db.generated_images_list(_OWNER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "shot.png")
        self.assertEqual(rows[0]["chat_title"], "Chat One")
        self.assertFalse(has_more)
        self.assertEqual(next_before_id, rows[0]["id"])

    async def test_recording_the_same_path_twice_is_not_a_duplicate(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, _, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(len(rows), 1)

    async def test_another_owners_images_are_not_listed(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OTHER, ["shot.png"])
        rows, _, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(rows, [])

    async def test_a_row_whose_file_is_gone_is_skipped_on_read(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        (self.work / "shot.png").unlink()
        rows, _, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(rows, [])

    async def test_get_returns_none_for_another_owner(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, _, _ = await db.generated_images_list(_OWNER)
        image_id = rows[0]["id"]
        self.assertIsNone(await db.generated_image_get(image_id, _OTHER))
        self.assertIsNotNone(await db.generated_image_get(image_id, _OWNER))

    async def test_delete_removes_the_file_and_the_row(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        rows, _, _ = await db.generated_images_list(_OWNER)
        image_id = rows[0]["id"]
        self.assertTrue(await db.generated_image_delete(image_id, _OWNER))
        self.assertFalse((self.work / "shot.png").exists())
        rows, _, _ = await db.generated_images_list(_OWNER)
        self.assertEqual(rows, [])

    async def test_delete_with_missing_file_still_removes_the_row(self):
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["shot.png"])
        (self.work / "shot.png").unlink()
        rows, _, _ = await db.generated_images_list(_OWNER)
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

    async def test_delete_refuses_to_unlink_outside_the_workspace(self):
        """Not reachable today (the only real caller passes workspace-
        relative paths from a followlinks=False walk), but a row with a
        traversal-shaped path must not be able to use the destructive
        unlink to remove a file outside work_dir. The row is still
        removed -- only the file portion is refused."""
        sentinel = Path(self.tmp.name) / "p" / "sentinel.txt"
        sentinel.write_bytes(b"do not touch")
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["../sentinel.txt"])
        cur = await db.db_conn.execute(
            "SELECT id FROM generated_images WHERE owner_id = ? AND path = ?",
            (_OWNER, "../sentinel.txt"),
        )
        row = await cur.fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(await db.generated_image_delete(row["id"], _OWNER))
        self.assertTrue(sentinel.exists())

    async def test_pagination_cursor(self):
        for n in range(3):
            (self.work / f"s{n}.png").write_bytes(b"x")
            await db.generated_image_record(
                "c1", "Chat One", str(self.work), _OWNER, [f"s{n}.png"])
        page1, has_more, next_before_id = await db.generated_images_list(
            _OWNER, limit=2)
        self.assertEqual(len(page1), 2)
        self.assertTrue(has_more)
        self.assertEqual(next_before_id, page1[-1]["id"])
        page2, has_more2, next_before_id2 = await db.generated_images_list(
            _OWNER, limit=2, before_id=next_before_id)
        self.assertEqual(len(page2), 1)
        self.assertFalse(has_more2)
        self.assertEqual(next_before_id2, page2[-1]["id"])

    async def test_a_fully_filtered_page_still_advances_the_cursor(self):
        """The self-healing case finding 2 exists for: if every file in a
        page is gone by the time it's read, the visible rows come back
        empty, but next_before_id must still be a real, advancing id --
        otherwise the client's cursor (derived from the empty page) would
        never move and "Load more" would refetch the same dead page
        forever."""
        (self.work / "gone1.png").write_bytes(b"x")
        (self.work / "gone2.png").write_bytes(b"x")
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["gone1.png"])
        await db.generated_image_record(
            "c1", "Chat One", str(self.work), _OWNER, ["gone2.png"])
        # Simulate a workspace cleaned up by hand -- not via the delete API.
        (self.work / "gone1.png").unlink()
        (self.work / "gone2.png").unlink()

        rows1, has_more1, next_before_id1 = await db.generated_images_list(
            _OWNER, limit=1)
        self.assertEqual(rows1, [])
        self.assertIsNotNone(next_before_id1)

        rows2, has_more2, next_before_id2 = await db.generated_images_list(
            _OWNER, limit=1, before_id=next_before_id1)
        self.assertEqual(rows2, [])
        self.assertIsNotNone(next_before_id2)
        # The cursor advanced instead of repeating the same dead page.
        self.assertNotEqual(next_before_id2, next_before_id1)
        self.assertFalse(has_more2)


if __name__ == "__main__":
    unittest.main()
