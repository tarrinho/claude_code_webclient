# Generated Images Gallery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** add a global, owner-scoped gallery in Settings listing every image any conversation has ever generated, with view and delete — the "see it inline in the chat" half already exists and is untouched by this plan.

**Architecture:** a new `generated_images` table records each image the moment `routes/chats.py` already discovers it (no new scan); a new `routes/images.py` exposes list/serve/delete over that table, with `chat_title`/`work_dir` snapshotted so an image stays manageable after its chat is deleted; a new Settings tab renders the list and reuses the existing image-viewer lightbox and delete-confirmation popup.

**Tech Stack:** FastAPI + aiosqlite (backend), vanilla JS ES modules (frontend), unittest/`IsolatedAsyncioTestCase` (tests) — no new dependency.

**Spec:** `docs/superpowers/specs/2026-09-12-generated-images-gallery-design.md`

## Global Constraints

- No image-processing library is added. Full images only, no server-side thumbnails.
- No cascade delete when a chat is deleted — `generated_images` rows have no foreign key to `chats` and survive it, by design.
- Deleting an image never rewrites the original chat message — file + index row only.
- An id belonging to another user's image 404s, never 403 — matches this codebase's existing convention (`test_qa_chat_files.py::test_another_owners_chat_is_404`) of not confirming existence to a non-owner.
- Run tests with `.venv/bin/python -m pytest <file> -v` (bare `pytest` skips the browser layer — not relevant here, nothing in this plan touches `test_frontend_browser.py`).
- **Do not use `owner_id="admin"` in any new test fixture.** `routes/db_chats.py::chat_create` (current HEAD, commit `344cf00`) raises `ValueError("owner_id must be a real user UUID, not 'admin'")` — two existing test files (`test_qa_chat_files.py`, `test_qa_chat_generated_images.py`) still call it with `"admin"` and are consequently broken at HEAD independent of this plan. That is a pre-existing defect, out of scope here — do not fix it as part of this work, and do not copy its pattern. Use a 32-character placeholder string instead, e.g. `"o" * 32`, matching this codebase's own placeholder-id style (`test_qa_chat_generated_images.py` uses `"g" * 32` for a chat id).

---

### Task 1: `generated_images` table and its CRUD module

**Files:**
- Modify: `db.py:678-696` (add table + indexes to the existing `executescript` block, right after the `system_samples` table)
- Create: `routes/db_images.py`
- Modify: `db.py:19-21` (`__getattr__`'s `_SYMBOLS` dict — add four new entries)
- Test: `tests/test_qa_generated_images_db.py`

**Interfaces:**
- Produces: `db.generated_image_record(chat_id: str, chat_title: str, work_dir: str, owner_id: str, paths: list[str]) -> None`, `db.generated_images_list(owner_id: str, limit: int = 60, before_id: int | None = None) -> tuple[list[dict], bool]`, `db.generated_image_get(image_id: int, owner_id: str) -> dict | None`, `db.generated_image_delete(image_id: int, owner_id: str) -> bool`. Task 3 consumes all four.

- [ ] **Step 1: Add the table to `db.py`'s schema block**

In `db.py`, find this exact text (the end of the `system_samples` table, immediately before the `executescript` call closes):

```python
        CREATE INDEX IF NOT EXISTS idx_system_samples_at ON system_samples(created_at);
    """)
```

Replace it with:

```python
        CREATE INDEX IF NOT EXISTS idx_system_samples_at ON system_samples(created_at);

        -- One row per image a turn generated, recorded the moment
        -- routes/chats.py's existing _new_workspace_images() finds it (no
        -- new scan). chat_title and work_dir are snapshotted, not joined
        -- live: ARCHITECTURE.md already documents that deleting a chat
        -- deliberately leaves its workspace on disk, so an image must stay
        -- servable and identifiable after its chat row is gone. No foreign
        -- key to chats for the same reason -- a chats row can disappear
        -- without this table needing an explicit cascade decision.
        CREATE TABLE IF NOT EXISTS generated_images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     TEXT NOT NULL,
            chat_title  TEXT NOT NULL,
            work_dir    TEXT NOT NULL,
            owner_id    TEXT NOT NULL,
            path        TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_generated_images_owner
            ON generated_images(owner_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_images_chat_path
            ON generated_images(chat_id, path);
    """)
```

- [ ] **Step 2: Create `routes/db_images.py`**

```python
# db_images.py — the generated_images table: one row per image a turn
# produced, recorded when routes/chats.py's _new_workspace_images() first
# finds it. Extracted from db.py the same way routes/db_queue.py and
# routes/db_chats.py are, so the turn-completion path does not need the
# full database module.
from __future__ import annotations

from pathlib import Path
from typing import Any

import db


async def generated_image_record(
    chat_id: str, chat_title: str, work_dir: str, owner_id: str, paths: list[str],
) -> None:
    """Record newly-discovered images for one turn.

    INSERT OR IGNORE against the (chat_id, path) unique index: the same
    image reported twice (the turn-completion path re-running for any
    reason) is a no-op, not a duplicate row.
    """
    if not paths:
        return
    now = db._now()
    await db.db_conn.executemany(
        "INSERT OR IGNORE INTO generated_images "
        "(chat_id, chat_title, work_dir, owner_id, path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(chat_id, chat_title, work_dir, owner_id, path, now) for path in paths],
    )
    await db.db_conn.commit()


def _file_exists(row: dict[str, Any]) -> bool:
    """Self-healing check: a row whose file is gone (deleted outside this
    feature, or a workspace removed by hand) is skipped on read rather than
    requiring a reconciliation job to keep the table honest."""
    try:
        return (Path(row["work_dir"]) / row["path"]).is_file()
    except (OSError, ValueError):
        return False


async def generated_images_list(
    owner_id: str, limit: int = 60, before_id: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of an owner's generated images, newest first.

    Mirrors routes/db_chats.py's messages_page: fetch limit+1 so "more
    remain" is a fact about this page, not a guess from limit alone. Rows
    whose file no longer exists are filtered out and do not count against
    the page or the has_more calculation from the caller's point of view --
    a page can come back shorter than `limit` for that reason, which is
    fine; the client's "Load more" just asks again with the next cursor.
    """
    if before_id is None:
        cur = await db.db_conn.execute(
            "SELECT id, chat_id, chat_title, work_dir, owner_id, path, created_at "
            "FROM generated_images WHERE owner_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (owner_id, limit + 1),
        )
    else:
        cur = await db.db_conn.execute(
            "SELECT id, chat_id, chat_title, work_dir, owner_id, path, created_at "
            "FROM generated_images WHERE owner_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT ?",
            (owner_id, before_id, limit + 1),
        )
    rows = [dict(r) for r in await cur.fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows = [r for r in rows if _file_exists(r)]
    return rows, has_more


async def generated_image_get(image_id: int, owner_id: str) -> dict[str, Any] | None:
    """One row, owner-checked. None if it does not exist, belongs to
    someone else, or its file is gone -- all three read as "not found" to
    the caller, which is what makes the API route's 404 (never 403)
    correct without the route needing to tell those cases apart itself."""
    cur = await db.db_conn.execute(
        "SELECT id, chat_id, chat_title, work_dir, owner_id, path, created_at "
        "FROM generated_images WHERE id = ? AND owner_id = ?",
        (image_id, owner_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    row = dict(row)
    return row if _file_exists(row) else None


async def generated_image_delete(image_id: int, owner_id: str) -> bool:
    """Delete the file (if present) then the row. Returns whether a row
    was removed. A missing file is not an error -- it means there is only
    the row left to clean up, which this still does."""
    cur = await db.db_conn.execute(
        "SELECT work_dir, path FROM generated_images WHERE id = ? AND owner_id = ?",
        (image_id, owner_id),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    try:
        (Path(row["work_dir"]) / row["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    cur = await db.db_conn.execute(
        "DELETE FROM generated_images WHERE id = ? AND owner_id = ?",
        (image_id, owner_id),
    )
    await db.db_conn.commit()
    return bool(cur.rowcount)
```

- [ ] **Step 3: Register the four new symbols in `db.py`'s `__getattr__`**

In `db.py`, find the `# queue` section header inside `_SYMBOLS` (around line 22) and add a new section immediately above it:

```python
        # generated images
        "generated_image_record": "routes.db_images",
        "generated_images_list": "routes.db_images",
        "generated_image_get": "routes.db_images",
        "generated_image_delete": "routes.db_images",
        # queue
        "QUEUE_MAX": "routes.db_queue",
```

- [ ] **Step 4: Write the failing tests**

Create `tests/test_qa_generated_images_db.py`:

```python
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
```

- [ ] **Step 5: Run the tests, confirm they fail for the right reason**

Run: `.venv/bin/python -m pytest tests/test_qa_generated_images_db.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'generated_image_record'` (Steps 1-3 not yet applied if run out of order; if applied, these should already pass — this step exists to confirm the test file itself is syntactically correct and exercises real behavior before trusting a later green run).

- [ ] **Step 6: Apply Steps 1-3 if not already done, then run again**

Run: `.venv/bin/python -m pytest tests/test_qa_generated_images_db.py -v`
Expected: PASS, all 9 tests.

- [ ] **Step 7: Commit**

```bash
git add db.py routes/db_images.py tests/test_qa_generated_images_db.py
git commit -m "feat(images): generated_images table and its CRUD module

New table records one row per generated image, snapshotting chat_title
and work_dir so an image stays servable and identifiable after its
chat is deleted (matches the existing decision to leave a deleted
chat's workspace on disk). Self-healing reads skip rows whose file no
longer exists. No wiring into the turn-completion path yet -- that is
the next task.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Wire recording into the turn-completion path

**Files:**
- Modify: `routes/chats.py:1338-1343`
- Modify: `tests/test_qa_chat_generated_images.py` (add test methods to the existing `TurnAppendsGeneratedImagesQA` class — it already has the exact fixture and `_run` helper this needs, no reason to duplicate it in a new file)

**Interfaces:**
- Consumes: `db.generated_image_record(chat_id, chat_title, work_dir, owner_id, paths)` from Task 1.

- [ ] **Step 1: Write the failing tests first**

In `tests/test_qa_chat_generated_images.py`, add these two methods to the existing `TurnAppendsGeneratedImagesQA` class (after `test_an_image_with_no_text_answer_is_still_kept`):

```python
    async def test_a_generated_image_is_recorded_in_the_gallery_table(self):
        write = lambda: (self.work / "chart.png").write_bytes(b"x")
        await self._run(_events("Here is the result.", write=write))
        rows, _ = await db.generated_images_list("admin")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "chart.png")
        self.assertEqual(rows[0]["chat_id"], self.chat_id)
        self.assertEqual(rows[0]["chat_title"], "cweb-img")

    async def test_a_pre_existing_image_is_not_recorded_either(self):
        """Same boundary as the markdown-append behaviour above: only
        images newer than the turn's own start count as generated."""
        import os
        old = self.work / "logo.png"
        old.write_bytes(b"x")
        os.utime(old, (1_000_000.0, 1_000_000.0))
        await self._run(_events("Done."))
        rows, _ = await db.generated_images_list("admin")
        self.assertEqual(rows, [])
```

Note: this class's existing fixture calls `db.chat_create(self.chat_id, "cweb-img", None, str(self.work), "admin")`, which currently raises `ValueError` at HEAD (see Global Constraints). **Do not change the owner_id here** — that is a pre-existing defect in a different area of the codebase, out of scope for this plan. If Step 2 below (running the tests) fails with that `ValueError` rather than the expected `AttributeError`/assertion failure, that confirms the pre-existing defect is still present and unrelated to this task; note it in the task's completion message and move on. (If some other session has already fixed it by the time this task runs, the tests will simply behave as written with no `ValueError` to route around.)

- [ ] **Step 2: Run the new tests to verify they fail for the right reason**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_generated_images.py -v -k "recorded_in_the_gallery or not_recorded_either"`
Expected: FAIL — either the pre-existing `ValueError` from `chat_create` (see the note above — not this task's concern), or (once that is not in the way) `AttributeError`/empty-list assertion failure, since the wiring in Step 3 has not been added yet.

- [ ] **Step 3: Wire the recording call**

In `routes/chats.py`, find this exact text (around line 1338):

```python
        images = await asyncio.to_thread(
            _new_workspace_images, chat["work_dir"], turn_started_at
        )
        if images:
            image_text = _image_markdown(images)
            assistant = f"{assistant}\n\n{image_text}" if assistant.strip() else image_text
```

Replace it with:

```python
        images = await asyncio.to_thread(
            _new_workspace_images, chat["work_dir"], turn_started_at
        )
        if images:
            image_text = _image_markdown(images)
            assistant = f"{assistant}\n\n{image_text}" if assistant.strip() else image_text
            # Same discovery, recorded once for the cross-chat gallery in
            # Settings. No new scan -- images is already in hand.
            await db.generated_image_record(
                chat_id, chat["title"], chat["work_dir"], owner, images,
            )
```

- [ ] **Step 4: Run the tests again, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_generated_images.py -v`
Expected: PASS for every test **except** any that were already failing at HEAD due to the pre-existing `chat_create("admin")` defect noted above (if that defect is still present, the whole file's fixture fails at `asyncSetUp` and every test in it errors — that is unchanged by this task; confirm by checking whether the failures are `ValueError: owner_id must be a real user UUID` rather than anything about images).

- [ ] **Step 5: Commit**

```bash
git add routes/chats.py tests/test_qa_chat_generated_images.py
git commit -m "feat(images): record generated images for the cross-chat gallery

One line added at the point _new_workspace_images/_image_markdown
already run -- no new filesystem scan. Recording happens only when
images were actually found, same condition the existing markdown-
append already guards on.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: List, serve, and delete API

**Files:**
- Create: `routes/images.py`
- Modify: `app.py` (import + `include_router`)
- Test: `tests/test_qa_images_api.py`

**Interfaces:**
- Consumes: the four `db.generated_image_*` functions from Task 1.
- Produces: `GET /api/images`, `GET /api/images/{id}/file`, `DELETE /api/images/{id}` — Task 5 (frontend) consumes these three routes directly by URL, not by importing anything from this module.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_images_api.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qa_images_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'routes.images'`

- [ ] **Step 3: Create `routes/images.py`**

```python
"""Routes for /api/images: the cross-chat generated-images gallery.

List, serve, and delete for the generated_images table (routes/db_images.py).
Serving is deliberately independent of the originating chat still existing --
see routes/db_images.py's own docstring for why work_dir is snapshotted.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import db

_log = logging.getLogger("wc.app")

router = APIRouter()

# Same extension set routes/chats.py's _CHAT_FILE_TYPES restricts image
# serving to -- kept as its own copy rather than importing from routes.chats,
# since generated_images never stores a .pdf path (routes/chats.py's
# _GENERATED_IMAGE_EXTENSIONS already excludes it) and importing routes.chats
# here for one dict is not worth the coupling.
_IMAGE_TYPES: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


async def handle_images_list(request: Request):
    """GET /api/images?limit=60&before_id=<id> -- an owner's generated
    images, newest first."""
    session = request.state.session
    limit_raw = request.query_params.get("limit")
    try:
        limit = min(int(limit_raw), 200) if limit_raw else 60
    except ValueError:
        limit = 60
    before_raw = request.query_params.get("before_id")
    before_id = int(before_raw) if before_raw and before_raw.isdigit() else None

    rows, has_more = await db.generated_images_list(
        session["user"], limit=limit, before_id=before_id)
    return JSONResponse({
        "images": [
            {
                "id": r["id"],
                "chat_id": r["chat_id"],
                "chat_title": r["chat_title"],
                "path": r["path"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
        "has_more": has_more,
    })


async def handle_image_file(request: Request, image_id: int):
    """GET /api/images/{id}/file -- serve the bytes directly from the
    row's snapshotted work_dir, independent of whether its chat still
    exists. Same path-containment check routes/chats.py:handle_chat_file
    applies, authorized against owner_id on the row instead of a live
    chat_get lookup."""
    session = request.state.session
    row = await db.generated_image_get(image_id, session["user"])
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found")

    root = Path(row["work_dir"]).resolve()
    try:
        candidate = (root / row["path"]).resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="Image not found")
    if not candidate.is_relative_to(root):
        _log.warning(
            "generated_image_outside_workspace: user=%s image_id=%s",
            session["user"], image_id,
        )
        raise HTTPException(status_code=404, detail="Image not found")

    media_type = _IMAGE_TYPES.get(candidate.suffix.lower())
    if media_type is None or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(candidate, media_type=media_type)


async def handle_image_delete(request: Request, image_id: int):
    """DELETE /api/images/{id} -- remove the file (if present) and the row."""
    session = request.state.session
    deleted = await db.generated_image_delete(image_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Image not found")
    return JSONResponse({"ok": True})


@router.get("/api/images")
async def _api_images_list(request: Request):
    return await handle_images_list(request)


@router.get("/api/images/{image_id}/file")
async def _api_image_file(request: Request, image_id: int):
    return await handle_image_file(request, image_id)


@router.delete("/api/images/{image_id}")
async def _api_image_delete(request: Request, image_id: int):
    return await handle_image_delete(request, image_id)
```

- [ ] **Step 4: Register the router in `app.py`**

Find this exact line (around line 58):

```python
from routes.orchestrators import router as orchestrators_router
```

Add immediately after it:

```python
from routes.images import router as images_router
```

Then find this exact line (around line 592):

```python
app.include_router(orchestrators_router)
```

Add immediately after it:

```python
app.include_router(images_router)
```

- [ ] **Step 5: Run the tests again, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_images_api.py -v`
Expected: PASS, all 9 tests.

- [ ] **Step 6: Run `py_compile` on the touched Python files**

Run: `.venv/bin/python -m py_compile app.py routes/images.py routes/db_images.py routes/chats.py db.py`
Expected: no output, exit code 0.

- [ ] **Step 7: Commit**

```bash
git add app.py routes/images.py tests/test_qa_images_api.py
git commit -m "feat(images): list/serve/delete API for the generated-images gallery

Three routes, all owner-scoped: GET /api/images (paginated), GET
/api/images/{id}/file (serves from the row's snapshotted work_dir --
works even if the originating chat has been deleted), DELETE
/api/images/{id} (file then row). Another owner's image id 404s,
never 403, matching this codebase's existing convention.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Generalize the image viewer and export the confirm dialog

**Files:**
- Modify: `web/assets/conversation.js:113-149` (`openImageViewer`)
- Modify: `web/assets/machines.js:1333` (`_showConfirmDialog` — add `export`)
- Test: none new — `tests/test_frontend_syntax.py` already parses every file under `web/assets/`; Step 3 below re-runs it.

**Interfaces:**
- Produces: `openImageViewer(srcUrl: string, label: string, captionText: string)` (signature changed from `(path, label)` — the URL is now fully caller-constructed) and `_showConfirmDialog(title: string, message: string, onYes: function)`, now exported from `machines.js`. Task 5 imports both.

- [ ] **Step 1: Generalize `openImageViewer`**

In `web/assets/conversation.js`, find this exact block:

```js
export function openImageViewer(path, label) {
  if (!_imageChatId) return;
  const back = document.createElement('div');
  back.className = 'image-viewer';
  back.setAttribute('role', 'dialog');
  back.setAttribute('aria-modal', 'true');
  back.setAttribute('aria-label', label || path);

  const img = document.createElement('img');
  img.alt = label || path;
  img.src = `/api/chats/${encodeURIComponent(_imageChatId)}/file?path=${encodeURIComponent(path)}`;

  const cap = document.createElement('div');
  cap.className = 'image-viewer-cap';
  cap.textContent = path;

  img.addEventListener('error', () => { cap.textContent = `Could not load ${path}`; });
```

Replace it with:

```js
/** Full-size viewer, given a direct URL rather than a chat-scoped path --
 *  used both by imageChip (which builds the /api/chats/.../file URL itself)
 *  and by the Images gallery (which builds an /api/images/{id}/file URL). */
export function openImageViewer(srcUrl, label, captionText) {
  const back = document.createElement('div');
  back.className = 'image-viewer';
  back.setAttribute('role', 'dialog');
  back.setAttribute('aria-modal', 'true');
  back.setAttribute('aria-label', label || captionText || srcUrl);

  const img = document.createElement('img');
  img.alt = label || captionText || srcUrl;
  img.src = srcUrl;

  const cap = document.createElement('div');
  cap.className = 'image-viewer-cap';
  cap.textContent = captionText || srcUrl;

  img.addEventListener('error', () => { cap.textContent = `Could not load ${captionText || srcUrl}`; });
```

- [ ] **Step 2: Update the one existing call site, `imageChip`**

In the same file, find this exact block:

```js
function imageChip(label, path) {
  const isPdf = path.toLowerCase().endsWith('.pdf');
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = isPdf ? 'file-chip pdf-chip' : 'image-chip';
  chip.textContent = label || path.split('/').pop();
  chip.title = isPdf ? `Open ${path}` : `Show ${path}`;
  chip.addEventListener('click', () => (
    isPdf ? openPdfViewer(path, label) : openImageViewer(path, label)
  ));
  return chip;
}
```

Replace it with:

```js
function imageChip(label, path) {
  const isPdf = path.toLowerCase().endsWith('.pdf');
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = isPdf ? 'file-chip pdf-chip' : 'image-chip';
  chip.textContent = label || path.split('/').pop();
  chip.title = isPdf ? `Open ${path}` : `Show ${path}`;
  chip.addEventListener('click', () => {
    if (isPdf) return openPdfViewer(path, label);
    if (!_imageChatId) return;
    const url = `/api/chats/${encodeURIComponent(_imageChatId)}/file?path=${encodeURIComponent(path)}`;
    openImageViewer(url, label, path);
  });
  return chip;
}
```

- [ ] **Step 3: Export `_showConfirmDialog` from `machines.js`**

In `web/assets/machines.js`, find this exact line:

```js
function _showConfirmDialog(title, message, onYes) {
```

Replace it with:

```js
export function _showConfirmDialog(title, message, onYes) {
```

- [ ] **Step 4: Run the frontend syntax gate**

Run: `.venv/bin/python -m pytest tests/test_frontend_syntax.py -v`
Expected: PASS (this test parses every `web/assets/*.js` file; it catches a syntax error in either edit above, but does not check call sites elsewhere — that is why Step 5 exists).

- [ ] **Step 5: Manually confirm no other caller of `openImageViewer` exists**

Run: `grep -rn "openImageViewer" web/assets/`
Expected: exactly two matches — the definition in `conversation.js`, and the one call site in `imageChip` updated in Step 2. If a third exists, update it the same way (build the URL at the call site) before continuing.

- [ ] **Step 6: Commit**

```bash
git add web/assets/conversation.js web/assets/machines.js
git commit -m "refactor(images): generalize the image viewer to a direct URL

openImageViewer(srcUrl, label, captionText) replaces
openImageViewer(path, label) -- the one existing caller (imageChip)
now builds its own /api/chats/.../file URL before calling it, so the
viewer itself no longer depends on the chat-scoped _imageChatId
module state. Needed by the Images gallery (next task), which has no
chat context to hang a path off of. Also exports _showConfirmDialog
from machines.js for the gallery's delete confirmation, reusing the
same styled Yes/No popup the Backends panel already ships.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: The Images Settings tab

**Files:**
- Modify: `web/index.html` (new tab button, new panel)
- Create: `web/assets/images.js`
- Modify: `web/assets/app.js` (`_switchTab`'s tab map and dispatch, tab-button wiring)
- Modify: `web/assets/styles.css` (grid + tile rules)

**Interfaces:**
- Consumes: `openImageViewer` and `_showConfirmDialog` from Task 4; `GET /api/images`, `GET /api/images/{id}/file`, `DELETE /api/images/{id}` from Task 3.
- Produces: `loadImages(force?: boolean)`, imported and called by `app.js` — no other module needs anything from `images.js`.

- [ ] **Step 1: Add the tab button and panel to `web/index.html`**

Find this exact line (the last settings tab button):

```html
      <button class="settings-tab" id="tabApp" role="tab" tabindex="-1" aria-selected="false" data-tab="app" aria-controls="panelApp">App</button>
```

Add immediately after it:

```html
      <button class="settings-tab" id="tabImages" role="tab" tabindex="-1" aria-selected="false" data-tab="images" aria-controls="panelImages">Images</button>
```

Find this exact text (the end of the Usage panel, right before the Statistics tab's comment):

```html
        <div class="usage-body" id="usageBody"></div>
      </div>
      <!-- Statistics tab: the same rows as Usage, over time rather than summed.
```

Replace it with:

```html
        <div class="usage-body" id="usageBody"></div>
      </div>
      <!-- Images tab: every image any conversation has generated, across
           all chats. Seeing one inline (in the chat it came from) already
           works without this tab; this is the cross-chat management view. -->
      <div class="settings-panel" id="panelImages" role="tabpanel" aria-labelledby="tabImages" tabindex="0" hidden>
        <p>Every image a conversation has generated. Deleting one here removes the file; the conversation it came from is left as-is.</p>
        <div class="skills-toolbar usage-toolbar">
          <span class="skills-count" id="imagesCount" role="status" aria-live="polite"></span>
        </div>
        <div class="images-grid" id="imagesGrid"></div>
        <button type="button" class="btn-secondary" id="imagesLoadMore" hidden>Load more</button>
      </div>
      <!-- Statistics tab: the same rows as Usage, over time rather than summed.
```

- [ ] **Step 2: Add the CSS**

Append to `web/assets/styles.css`:

```css
.images-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin-top:10px}
.image-tile{position:relative;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel);aspect-ratio:1/1}
.image-tile img{width:100%;height:100%;object-fit:cover;cursor:pointer;display:block}
.image-tile-meta{position:absolute;left:0;right:0;bottom:0;padding:4px 6px;font-size:.68rem;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.72));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.image-tile-meta a{color:#fff;text-decoration:underline}
.image-tile-delete{position:absolute;top:4px;right:4px;width:24px;height:24px;border-radius:6px;border:1px solid rgba(255,255,255,.4);background:rgba(0,0,0,.55);color:#fff;cursor:pointer;font-size:14px;line-height:1;padding:0}
.image-tile-delete:hover{background:var(--danger,#c0504d)}
#imagesLoadMore{margin-top:12px}
```

- [ ] **Step 3: Create `web/assets/images.js`**

```js
// images.js — Settings > Images: every generated image across all chats.
//
// Seeing an image inline, in the conversation it came from, already works
// (conversation.js's imageChip + openImageViewer, served by
// /api/chats/{id}/file). This module is the cross-chat management view: it
// never re-derives that inline behaviour, it only lists what
// routes/db_images.py already recorded and lets you delete or re-view one.
import {apiFetch} from './api.js?v=2741508';
import {openImageViewer} from './conversation.js?v=1434625';
import {_showConfirmDialog} from './machines.js?v=15382680';

const byId = id => document.getElementById(id);

let _nextBeforeId = null;
let _hasMore = false;

function _fileUrl(image) {
  return `/api/images/${encodeURIComponent(image.id)}/file`;
}

function _tile(image) {
  const tile = document.createElement('div');
  tile.className = 'image-tile';
  tile.dataset.imageId = String(image.id);

  const img = document.createElement('img');
  img.loading = 'lazy';
  img.alt = image.path;
  img.src = _fileUrl(image);
  img.addEventListener('click', () => {
    openImageViewer(_fileUrl(image), image.path, `${image.chat_title} — ${image.path}`);
  });

  const meta = document.createElement('div');
  meta.className = 'image-tile-meta';
  meta.textContent = image.chat_title;

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'image-tile-delete';
  del.textContent = '×';
  del.setAttribute('aria-label', `Delete ${image.path}`);
  del.addEventListener('click', event => {
    event.stopPropagation();
    _showConfirmDialog(
      'Delete this image?',
      `This removes the file. The conversation it came from is left as-is. Delete ${image.path}?`,
      () => _deleteImage(image.id, tile),
    );
  });

  tile.append(img, meta, del);
  return tile;
}

async function _deleteImage(imageId, tile) {
  try {
    const response = await apiFetch(`/api/images/${encodeURIComponent(imageId)}`, {method: 'DELETE'});
    if (response.ok) tile.remove();
  } catch {
    // The tile staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
  }
}

/** Load the first page. force=true (Settings tab just opened) always
 *  refetches rather than showing a stale in-memory list. */
export async function loadImages(force = false) {
  const grid = byId('imagesGrid');
  if (!grid) return;
  if (!force && grid.children.length) return;

  grid.replaceChildren();
  _nextBeforeId = null;
  await _loadPage();
}

async function _loadPage() {
  const grid = byId('imagesGrid');
  const countEl = byId('imagesCount');
  const moreBtn = byId('imagesLoadMore');
  if (!grid) return;

  const params = new URLSearchParams({limit: '60'});
  if (_nextBeforeId) params.set('before_id', String(_nextBeforeId));

  let payload;
  try {
    const response = await apiFetch(`/api/images?${params}`);
    if (!response.ok) return;
    payload = await response.json();
  } catch {
    return;
  }

  const images = payload.images || [];
  images.forEach(image => grid.appendChild(_tile(image)));
  _hasMore = !!payload.has_more;
  _nextBeforeId = images.length ? images[images.length - 1].id : _nextBeforeId;

  if (countEl) {
    const total = grid.children.length;
    countEl.textContent = `${total} image${total === 1 ? '' : 's'}`;
  }
  if (moreBtn) moreBtn.hidden = !_hasMore;
}

export function _wireImagesLoadMore() {
  byId('imagesLoadMore')?.addEventListener('click', () => _loadPage());
}
```

- [ ] **Step 4: Wire `app.js`**

Find this exact block in `_switchTab` (around line 440):

```js
  const map = {
    backends: 'panelBackends', usage: 'panelUsage', stats: 'panelStats',
    server: 'panelServer', skills: 'panelSkills', app: 'panelApp',
  };
  const activeId = map[tab] || 'panelBackends';
  ['panelBackends', 'panelUsage', 'panelStats', 'panelServer', 'panelSkills',
   'panelApp'].forEach(id => {
```

Replace it with:

```js
  const map = {
    backends: 'panelBackends', usage: 'panelUsage', stats: 'panelStats',
    server: 'panelServer', skills: 'panelSkills', app: 'panelApp',
    images: 'panelImages',
  };
  const activeId = map[tab] || 'panelBackends';
  ['panelBackends', 'panelUsage', 'panelStats', 'panelServer', 'panelSkills',
   'panelApp', 'panelImages'].forEach(id => {
```

A few lines below that, find this exact line — the last dispatch in `_switchTab`, right before the function's closing brace:

```js
  if (tab === 'skills') loadSkills();
}
```

Replace it with:

```js
  if (tab === 'skills') loadSkills();
  if (tab === 'images') loadImages(true);
}
```

Near the top of `app.js`, find this exact line:

```js
import {renderVoiceSettingsFields, collectVoiceSettingsFields} from './voice-settings.js?v=5515949';
```

Add immediately after it:

```js
import {loadImages, _wireImagesLoadMore} from './images.js?v=1';
```

Find this exact line:

```js
  byId('refreshBackendsBtn')?.addEventListener('click', _refreshBackends);
```

Add immediately after it:

```js
  _wireImagesLoadMore();
```

- [ ] **Step 5: Resync asset cache-busters**

Run: `python3 bin/wc-asset-versions.py --check`
Expected: reports at least one stale reference (the placeholder `?v=1` on `images.js` in Step 4, and possibly others from work landed by other sessions in the meantime).

Run: `python3 bin/wc-asset-versions.py`
Expected: `updated N reference(s) across M file(s)`.

Run: `python3 bin/wc-asset-versions.py --check`
Expected: `all N asset references match their content`.

- [ ] **Step 6: Run the frontend syntax gate**

Run: `.venv/bin/python -m pytest tests/test_frontend_syntax.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/index.html web/assets/images.js web/assets/app.js web/assets/styles.css
git commit -m "feat(images): Images tab in Settings

Seventh Settings tab, following the existing tab pattern exactly.
Grid of lazy-loaded full images (no thumbnail generation -- see the
design spec's Scope section for why), each tile showing its
originating chat and a delete button that reuses the existing styled
Yes/No confirmation popup. Clicking a tile opens the same lightbox
viewer already used inline in conversations. 'Load more' is an
explicit button, matching this app's existing preference for
explicit refresh over infinite scroll.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: End-to-end QA test

**Files:**
- Create: `tests/test_qa_images_gallery.py`

**Interfaces:**
- Consumes: `_start_turn` (Task 2's wiring), `db.generated_images_list`/`generated_image_delete` (Task 1), `images_routes.handle_images_list`/`handle_image_delete` (Task 3).

- [ ] **Step 1: Write the test**

```python
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
        rows, _ = await db.generated_images_list(self.owner)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "chart.png")
        self.assertEqual(rows[0]["chat_id"], self.chat_id)

    async def test_deleting_from_the_gallery_leaves_the_message_untouched(self):
        await self._run_turn()
        rows, _ = await db.generated_images_list(self.owner)
        image_id = rows[0]["id"]

        from types import SimpleNamespace
        req = SimpleNamespace(state=SimpleNamespace(session={"user": self.owner}))
        await images_routes.handle_image_delete(req, image_id)

        rows_after, _ = await db.generated_images_list(self.owner)
        self.assertEqual(rows_after, [])

        assistant_rows = [
            m for m in await db.messages_get(self.chat_id) if m["role"] == "assistant"
        ]
        self.assertIn("![chart.png](chart.png)", assistant_rows[-1]["content"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_qa_images_gallery.py -v`
Expected: PASS, both tests.

- [ ] **Step 3: Run every test file this plan touched, together**

Run: `.venv/bin/python -m pytest tests/test_qa_generated_images_db.py tests/test_qa_chat_generated_images.py tests/test_qa_images_api.py tests/test_qa_images_gallery.py tests/test_frontend_syntax.py -v`
Expected: PASS across the board, with the sole exception noted in Task 2 if the pre-existing `chat_create("admin")` defect is still present in `tests/test_qa_chat_generated_images.py`'s other, unrelated tests at the time this runs — that is not this plan's regression.

- [ ] **Step 4: Commit**

```bash
git add tests/test_qa_images_gallery.py
git commit -m "test(images): end-to-end gallery QA

Ties Task 2's recording, Task 1's table, and Task 3's delete route
together in one scenario: a generated image reaches the gallery with
no extra step, and deleting it from there leaves the original
message's markdown link untouched -- the decision the design spec
commits to, pinned so a future change can't silently reverse it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```
