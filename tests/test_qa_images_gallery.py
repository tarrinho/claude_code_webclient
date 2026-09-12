"""QA: a generated image reaches the gallery without any extra step, and
deleting it from the gallery leaves the original chat's message untouched.

Mirrors tests/test_qa_chat_generated_images.py's fixture (same _start_turn
path) plus tests/test_qa_images_api.py's route-level assertions, tying the
two together end to end -- this is the scenario the design spec's own
"decisions" section commits to, asserted directly so a future change to
either side cannot silently break the seam between them.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
from routes import chats as chat_routes
from routes import images as images_routes


def _events(*texts, write=None):
    async def gen():
        if write is not None:
            write()
        for text in texts:
            yield {"type": "text", "content": text}
        yield {"type": "done"}
    return gen


class GeneratedImageReachesTheGalleryQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.work = Path(config.PROJECTS_ROOT) / "w"
        self.work.mkdir(parents=True)
        self.chat_id = "i" * 32
        self.owner = "o" * 32
        await db.chat_create(self.chat_id, "gallery-e2e", None, str(self.work), self.owner)

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _run_turn(self):
        import runner
        write = lambda: (self.work / "chart.png").write_bytes(b"x")
        chat = await db.chat_get(self.chat_id, self.owner)
        with patch.object(runner, "stream_turn", lambda *a, **k: _events(
                "Here is the result.", write=write)()):
            turn = await chat_routes._start_turn(chat, self.owner, "make a chart", None)
            await turn.task

    async def test_image_appears_in_the_gallery_with_no_extra_step(self):
        await self._run_turn()
        rows, _, _ = await db.generated_images_list(self.owner)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "chart.png")
        self.assertEqual(rows[0]["chat_id"], self.chat_id)

    async def test_deleting_from_the_gallery_leaves_the_message_untouched(self):
        await self._run_turn()
        rows, _, _ = await db.generated_images_list(self.owner)
        image_id = rows[0]["id"]

        from types import SimpleNamespace
        req = SimpleNamespace(state=SimpleNamespace(session={"user": self.owner}))
        await images_routes.handle_image_delete(req, image_id)

        rows_after, _, _ = await db.generated_images_list(self.owner)
        self.assertEqual(rows_after, [])

        assistant_rows = [
            m for m in await db.messages_get(self.chat_id) if m["role"] == "assistant"
        ]
        self.assertIn("![chart.png](chart.png)", assistant_rows[-1]["content"])


if __name__ == "__main__":
    unittest.main()
