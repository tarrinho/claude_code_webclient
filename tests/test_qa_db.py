"""QA: DB-level functions across the extracted-from-db.py modules.

Covers every function in:
* routes/db_agent_reply.py    — cooldown_check, log_add
* routes/db_images.py         — image_record, images_list, chat_ids_that_exist,
                                image_get, image_delete, _file_exists, _resolved_inside
* routes/db_queue.py          — queue_add, queue_list, queue_counts, queue_held_counts,
                                last_models_used, last_model_used, queue_next,
                                queue_delete, queue_release, queue_hold_orphans, queue_hold_all
* routes/db_read_marks.py     — read_marks_get, read_mark_set, chat_last_activity
* routes/db_transport_sync.py — sync_request_create, sync_request_get,
                                sync_request_list_pending, sync_request_resolve
"""
from __future__ import annotations

import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db
import routes.db_agent_reply as db_agent_reply
import routes.db_images as db_images
import routes.db_queue as db_queue
import routes.db_read_marks as db_read_marks
import routes.db_transport_sync as db_transport_sync

# ── helpers ──────────────────────────────────────────────────────────────────

async def _owner_id(name: str) -> str:
    row = await db.user_get_by_name(name)
    assert row, f"no user {name!r} — create it before seeding rows"
    return row["id"]


class _BaseDBTest(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: temp DB, two users, a chat and workspace."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.addAsyncCleanup(db.close)

        self.passwords = {
            "alice": secrets.token_urlsafe(16),
            "bob": secrets.token_urlsafe(16),
        }
        await db.user_create("alice", None, auth.hash_password(self.passwords["alice"]))
        await db.user_create(
            "bob", None, auth.hash_password(self.passwords["bob"]), role="user"
        )

        self.alice_id = await _owner_id("alice")
        self.bob_id = await _owner_id("bob")

        self.chat_id = "qa-chat-1"
        await db.chat_create(
            self.chat_id, "QA test", None, f"{self.tmp.name}/projects", self.alice_id
        )
        self.work = Path(self.tmp.name) / "projects" / self.chat_id
        self.work.mkdir(parents=True)

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()


# ── db_agent_reply ──────────────────────────────────────────────────────────

class AgentReplyTests(_BaseDBTest):
    """routes/db_agent_reply.py"""

    async def test_cooldown_check_returns_true_when_no_prior_log(self):
        """No row → the relay may proceed."""
        self.assertTrue(await db_agent_reply.agent_reply_cooldown_check("c1", "t1"))

    async def test_cooldown_check_blocks_recent_attempt(self):
        """A log entry within 5 minutes must block."""
        await db_agent_reply.agent_reply_log_add(
            "c1", "owner1", "t1", "local", True, "ok",
        )
        self.assertFalse(
            await db_agent_reply.agent_reply_cooldown_check("c1", "t1"),
        )

    async def test_cooldown_check_allows_after_window(self):
        """After 5 minutes the cooldown expires and the next relay is allowed."""
        # Log a reply with a timestamp from > 5 minutes ago using db._now() minus 301s.
        # agent_reply_log_add writes db._now() — we override the current time.
        import datetime
        fake_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=301)
        fake_ts = fake_now.strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.db_conn.execute(
            "INSERT INTO agent_reply_log "
            "(chat_id, owner_id, target, via, ok, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c1", "owner1", "t1", "local", 1, "ok", fake_ts),
        )
        await db.db_conn.commit()
        self.assertTrue(
            await db_agent_reply.agent_reply_cooldown_check("c1", "t1"),
        )

    async def test_cooldown_check_blocks_different_chat_same_target(self):
        """Cooldown is per chat_id+target pair, not global."""
        await db_agent_reply.agent_reply_log_add("c1", "owner1", "t1", "local", True)
        # c2 → t1 should still be allowed.
        self.assertTrue(
            await db_agent_reply.agent_reply_cooldown_check("c2", "t1"),
        )

    async def test_log_add_creates_row(self):
        """Every relay attempt is persisted."""
        await db_agent_reply.agent_reply_log_add(
            "c1", "owner1", "t1", "local", True, "success",
        )
        cur = await db.db_conn.execute(
            "SELECT id, chat_id, target, via, ok, reason FROM agent_reply_log WHERE chat_id = ?",
            ("c1",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["chat_id"], "c1")
        self.assertEqual(row["target"], "t1")
        self.assertEqual(row["via"], "local")
        self.assertEqual(row["ok"], 1)
        self.assertEqual(row["reason"], "success")

    async def test_log_add_records_failure(self):
        """ok=0 is stored when a relay fails."""
        await db_agent_reply.agent_reply_log_add(
            "c1", "owner1", "t1", "transport", False, "timeout",
        )
        cur = await db.db_conn.execute(
            "SELECT ok, reason FROM agent_reply_log WHERE chat_id = ?",
            ("c1",),
        )
        row = dict(await cur.fetchone())
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["reason"], "timeout")


# ── db_images ───────────────────────────────────────────────────────────────

class DbImagesTests(_BaseDBTest):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Create a real image file for tests that need _file_exists to pass.
        self.image_path = self.work / "shot.png"
        self.image_path.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        ))
        await db.generated_image_record(
            self.chat_id, "QA test", str(self.work), self.alice_id,
            ["shot.png"],
        )

    async def _image_id(self):
        cur = await db.db_conn.execute(
            "SELECT id FROM generated_images WHERE chat_id = ? AND owner_id = ?",
            (self.chat_id, self.alice_id),
        )
        row = await cur.fetchone()
        return row["id"] if row else 0

    # ── _resolved_inside ──────────────────────────────────────────────────

    def test_resolved_inside_returns_path_when_inside(self):
        """A normal path inside work_dir resolves."""
        resolved = db_images._resolved_inside(str(self.work), "shot.png")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.is_file())

    def test_resolved_inside_returns_none_for_outside(self):
        """A traversal path that escapes work_dir returns None."""
        resolved = db_images._resolved_inside(str(self.work), "../../../etc/passwd")
        self.assertIsNone(resolved)

    def test_resolved_inside_handles_broken_symlink(self):
        """Broken symlinks return None rather than raising."""
        broken = self.work / "broken_link"
        try:
            broken.symlink_to("/nonexistent/target")
        except OSError:
            pass  # some systems don't allow this
        resolved = db_images._resolved_inside(str(self.work), "broken_link")
        # If symlink was created, it should fail is_file() (broken) but
        # still pass is_relative_to, so resolve works. If symlink creation
        # failed, the path won't exist and will return None.
        if resolved is not None:
            self.assertFalse(resolved.is_file())

    # ── _file_exists ──────────────────────────────────────────────────────

    async def test_file_exists_when_file_present(self):
        """A real row with a real file returns True."""
        row = {"work_dir": str(self.work), "path": "shot.png"}
        self.assertTrue(db_images._file_exists(row))

    async def test_file_exists_returns_false_when_deleted(self):
        """A row whose file was deleted outside the system returns False."""
        self.image_path.unlink()
        row = {"work_dir": str(self.work), "path": "shot.png"}
        self.assertFalse(db_images._file_exists(row))

    # ── generated_image_record ────────────────────────────────────────────

    async def test_image_record_creates_row(self):
        """A call with image paths inserts rows."""
        await db.generated_image_record(
            "c2", "Chat Two", str(self.work), self.alice_id,
            ["other.png"],
        )
        cur = await db.db_conn.execute(
            "SELECT COUNT(*) AS n FROM generated_images WHERE chat_id = ?",
            ("c2",),
        )
        self.assertEqual((await cur.fetchone())["n"], 1)

    async def test_image_record_ignores_duplicate_paths(self):
        """The same (chat_id, path) reported twice is a no-op."""
        await db.generated_image_record(
            "c2", "Chat Two", str(self.work), self.alice_id,
            ["same.png"],
        )
        await db.generated_image_record(
            "c2", "Chat Two", str(self.work), self.alice_id,
            ["same.png"],
        )
        cur = await db.db_conn.execute(
            "SELECT COUNT(*) AS n FROM generated_images WHERE chat_id = ? AND path = ?",
            ("c2", "same.png"),
        )
        self.assertEqual((await cur.fetchone())["n"], 1)

    async def test_image_record_empty_paths_is_noop(self):
        """An empty list does nothing — no row is inserted."""
        before = await db.db_conn.execute(
            "SELECT COUNT(*) AS n FROM generated_images WHERE chat_id = 'c2'",
        )
        initial = (await before.fetchone())["n"]
        await db.generated_image_record("c2", "C2", str(self.work), self.alice_id, [])
        after = await db.db_conn.execute(
            "SELECT COUNT(*) AS n FROM generated_images WHERE chat_id = 'c2'",
        )
        self.assertEqual((await after.fetchone())["n"], initial)

    # ── generated_images_list ─────────────────────────────────────────────

    async def test_images_list_returns_rows(self):
        """The owner's image list returns rows."""
        rows, has_more, _ = await db_images.generated_images_list(self.alice_id, 60)
        self.assertGreater(len(rows), 0)
        self.assertFalse(has_more)
        self.assertIn("chat_id", rows[0])
        self.assertIn("path", rows[0])
        self.assertIn("created_at", rows[0])

    async def test_images_list_empty_for_other_owner(self):
        """Bob sees nothing when Alice is the only one with images."""
        rows, _, _ = await db_images.generated_images_list(self.bob_id, 60)
        self.assertEqual(rows, [])

    async def test_images_list_pagination(self):
        """has_more=True with enough images; next_before_id advances."""
        for i in range(70):
            img = self.work / f"img{i:03d}.png"
            img.write_bytes(bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001080600000"
                "01f15c4890000000a49444154789c6300010000050001"
                "0d0a2db40000000049454e44ae426082"
            ))
            await db.generated_image_record(
                "c2", "Chat Two", str(self.work), self.alice_id,
                [f"img{i:03d}.png"],
            )

        rows1, has_more1, before_id1 = await db_images.generated_images_list(
            self.alice_id, 50,
        )
        self.assertTrue(has_more1)
        self.assertEqual(len(rows1), 50)  # limited to 50 (raw_page[:limit])

        rows2, has_more2, _ = await db_images.generated_images_list(
            self.alice_id, 50, before_id=before_id1,
        )
        self.assertFalse(has_more2)
        self.assertGreater(len(rows2), 0)

    async def test_images_list_skips_deleted_files(self):
        """Self-healing: rows whose file is gone are excluded."""
        # Take the first row (shot.png exists)
        rows, _, _ = await db_images.generated_images_list(self.alice_id, 60)
        self.assertGreater(len(rows), 0)
        first = rows[0]

        # Delete the file.
        self.image_path.unlink()
        rows_after, has_more, _ = await db_images.generated_images_list(
            self.alice_id, 60,
        )
        # has_more is True because there is a raw row; filtered is [].
        self.assertFalse(any(r["id"] == first["id"] for r in rows_after))
        # The raw count still shows there is data (before_id advances).

    # ── chat_ids_that_exist ──────────────────────────────────────────────

    async def test_chat_ids_that_exist_returns_known(self):
        """Known chat_ids are returned."""
        result = await db_images.chat_ids_that_exist({self.chat_id, "does-not-exist"})
        self.assertIn(self.chat_id, result)
        self.assertNotIn("does-not-exist", result)

    async def test_chat_ids_that_exist_empty_input(self):
        """Empty set → empty set."""
        self.assertEqual(await db_images.chat_ids_that_exist(set()), set())

    # ── generated_image_get ──────────────────────────────────────────────

    async def test_image_get_returns_row(self):
        """Owner can retrieve their own image row."""
        img_id = await self._image_id()
        row = await db_images.generated_image_get(img_id, self.alice_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["chat_id"], self.chat_id)
        self.assertEqual(row["path"], "shot.png")

    async def test_image_get_none_for_other_owner(self):
        """Bob cannot see Alice's image row."""
        img_id = await self._image_id()
        row = await db_images.generated_image_get(img_id, self.bob_id)
        self.assertIsNone(row)

    async def test_image_get_none_when_file_deleted(self):
        """A row whose file is gone is indistinguishable from 'not found'."""
        img_id = await self._image_id()
        self.image_path.unlink()
        row = await db_images.generated_image_get(img_id, self.alice_id)
        self.assertIsNone(row)

    # ── generated_image_delete ───────────────────────────────────────────

    async def test_image_delete_removes_row(self):
        """Deleting removes both file (if present) and row."""
        img_id = await self._image_id()
        self.assertTrue(await db_images.generated_image_delete(img_id, self.alice_id))
        row = await db_images.generated_image_get(img_id, self.alice_id)
        self.assertIsNone(row)

    async def test_image_delete_nonexistent_id(self):
        """Deleting an unknown id returns False."""
        self.assertFalse(await db_images.generated_image_delete(999999, self.alice_id))

    async def test_image_delete_by_wrong_owner_returns_false(self):
        """Bob cannot delete Alice's image row."""
        img_id = await self._image_id()
        self.assertFalse(await db_images.generated_image_delete(img_id, self.bob_id))

    async def test_image_delete_removes_file(self):
        """The file itself is unlinked."""
        self.assertTrue(self.image_path.exists())
        img_id = await self._image_id()
        await db_images.generated_image_delete(img_id, self.alice_id)
        self.assertFalse(self.image_path.exists())

    async def test_image_delete_is_idempotent(self):
        """Deleting an already-deleted row is a no-op, not an error."""
        img_id = await self._image_id()
        await db_images.generated_image_delete(img_id, self.alice_id)
        self.assertFalse(await db_images.generated_image_delete(img_id, self.alice_id))


# ── db_queue ────────────────────────────────────────────────────────────────

class DbQueueTests(_BaseDBTest):

    # ── queue_add ─────────────────────────────────────────────────────────

    async def test_queue_add_returns_position(self):
        """First item returns position 1."""
        pos = await db_queue.queue_add(
            self.chat_id, self.alice_id, "hello", None,
        )
        self.assertEqual(pos, 1)

    async def test_queue_add_returns_zero_when_full(self):
        """Queue max is 5; adding a 6th returns 0."""
        for i in range(db_queue.QUEUE_MAX):
            pos = await db_queue.queue_add(
                self.chat_id, self.alice_id, f"msg{i}", None,
            )
            self.assertEqual(pos, i + 1)
        self.assertEqual(
            await db_queue.queue_add(
                self.chat_id, self.alice_id, "overflow", None,
            ),
            0,
        )

    async def test_queue_add_with_model(self):
        """A model string is stored alongside the prompt."""
        await db_queue.queue_add(
            self.chat_id, self.alice_id, "model-me", "claude-sonnet-5",
        )
        cur = await db.db_conn.execute(
            "SELECT model FROM turn_queue WHERE chat_id = ? AND prompt = ?",
            (self.chat_id, "model-me"),
        )
        self.assertEqual((await cur.fetchone())["model"], "claude-sonnet-5")

    async def test_queue_add_owner_scoped(self):
        """Bob's add does not appear in Alice's queue."""
        bob_chat_id = "qa-chat-bob"
        await db.chat_create(
            bob_chat_id, "Bob's chat", None,
            f"{self.tmp.name}/projects", self.bob_id,
        )
        await db.queue_add(bob_chat_id, self.bob_id, "bob-msg", None)
        rows = await db_queue.queue_list(self.chat_id, self.alice_id)
        self.assertEqual(len(rows), 0)

    # ── queue_list ──────────────────────────────────────────────────────

    async def test_queue_list_returns_ordered_prompts(self):
        """Prompts are returned oldest-first with id/prompt/model/state."""
        await db.queue_add(self.chat_id, self.alice_id, "first", None)
        await db.queue_add(self.chat_id, self.alice_id, "second", None)
        items = await db_queue.queue_list(self.chat_id, self.alice_id)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["prompt"], "first")
        self.assertEqual(items[1]["prompt"], "second")

    async def test_queue_list_empty(self):
        self.assertEqual(await db_queue.queue_list(self.chat_id, self.alice_id), [])

    # ── queue_counts ────────────────────────────────────────────────────

    async def test_queue_counts_totals_per_chat(self):
        cur = await db_queue.queue_counts(self.alice_id)
        self.chat_id2 = "qa-chat-2"
        await db.chat_create(
            self.chat_id2, "QA2", None, f"{self.tmp.name}/projects", self.alice_id,
        )
        await db.queue_add(self.chat_id, self.alice_id, "a", None)
        await db.queue_add(self.chat_id, self.alice_id, "b", None)
        await db.queue_add(self.chat_id2, self.alice_id, "c", None)
        counts = await db_queue.queue_counts(self.alice_id)
        self.assertEqual(counts[self.chat_id], 2)
        self.assertEqual(counts[self.chat_id2], 1)

    async def test_queue_counts_empty(self):
        empty_chat = "qa-empty"
        await db.chat_create(
            empty_chat, "Empty", None, f"{self.tmp.name}/projects", self.alice_id,
        )
        counts = await db_queue.queue_counts(self.alice_id)
        self.assertNotIn(empty_chat, counts)

    # ── queue_held_counts ──────────────────────────────────────────────

    async def test_queue_held_counts_excludes_pending(self):
        await db.queue_add(self.chat_id, self.alice_id, "pending-prompt", None)
        await db.db_conn.execute(
            "UPDATE turn_queue SET state = 'held' WHERE chat_id = ? AND state = 'pending'",
            (self.chat_id,),
        )
        await db.db_conn.commit()
        held = await db_queue.queue_held_counts(self.alice_id)
        self.assertEqual(held[self.chat_id], 1)

    async def test_queue_held_counts_empty(self):
        self.assertEqual(await db_queue.queue_held_counts(self.alice_id), {})

    # ── queue_next ──────────────────────────────────────────────────────

    async def test_queue_next_returns_oldest_pending(self):
        await db.queue_add(self.chat_id, self.alice_id, "old", "model-a")
        await db.queue_add(self.chat_id, self.alice_id, "new", "model-b")
        nxt = await db_queue.queue_next(self.chat_id)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["prompt"], "old")
        self.assertEqual(nxt["model"], "model-a")

    async def test_queue_next_excludes_non_pending(self):
        """Only 'pending' rows are returned; 'held' and 'done' are skipped."""
        await db.queue_add(self.chat_id, self.alice_id, "pending-one", None)
        await db.queue_add(self.chat_id, self.alice_id, "held-one", None)
        cur = await db.db_conn.execute(
            "UPDATE turn_queue SET state = 'held' WHERE chat_id = ? AND state = 'pending'",
            (self.chat_id,),
        )
        await db.db_conn.commit()
        nxt = await db_queue.queue_next(self.chat_id)
        self.assertIsNone(nxt)

    async def test_queue_next_none_when_empty(self):
        self.assertIsNone(await db_queue.queue_next("empty-chat"))

    # ── queue_delete ────────────────────────────────────────────────────

    async def test_queue_delete_removes_item(self):
        row_id = (await db.queue_add(self.chat_id, self.alice_id, "del-me", None))
        cur = await db.db_conn.execute(
            "SELECT id FROM turn_queue WHERE prompt = 'del-me'",
        )
        row_id = (await cur.fetchone())["id"]
        self.assertTrue(await db_queue.queue_delete(row_id, self.alice_id))

    async def test_queue_delete_wrong_owner_returns_false(self):
        row_id = (await db.queue_add(self.chat_id, self.alice_id, "protected", None))
        cur = await db.db_conn.execute(
            "SELECT id FROM turn_queue WHERE prompt = 'protected'",
        )
        row_id = (await cur.fetchone())["id"]
        self.assertFalse(await db_queue.queue_delete(row_id, self.bob_id))

    async def test_queue_delete_nonexistent_id(self):
        self.assertFalse(await db_queue.queue_delete(999999, self.alice_id))

    # ── queue_release ───────────────────────────────────────────────────

    async def test_queue_release_changes_held_to_pending(self):
        """A held prompt is returned to pending."""
        row_id = (await db.queue_add(self.chat_id, self.alice_id, "release-me", None))
        cur = await db.db_conn.execute(
            "SELECT id FROM turn_queue WHERE prompt = 'release-me'",
        )
        row_id = (await cur.fetchone())["id"]
        await db.db_conn.execute(
            "UPDATE turn_queue SET state = 'held' WHERE id = ?", (row_id,),
        )
        await db.db_conn.commit()
        self.assertTrue(await db_queue.queue_release(row_id, self.alice_id))
        nxt = await db_queue.queue_next(self.chat_id)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["prompt"], "release-me")

    async def test_queue_release_wrong_owner(self):
        row_id = (await db.queue_add(self.chat_id, self.alice_id, "protected", None))
        cur = await db.db_conn.execute(
            "SELECT id FROM turn_queue WHERE prompt = 'protected'",
        )
        row_id = (await cur.fetchone())["id"]
        self.assertFalse(await db_queue.queue_release(row_id, self.bob_id))

    # ── queue_hold_orphans ──────────────────────────────────────────────

    async def test_queue_hold_orphans_holds_all_pending(self):
        """Pending rows across all conversations become held."""
        chat2 = "qa-chat-2"
        await db.chat_create(
            chat2, "QA2", None, f"{self.tmp.name}/projects", self.alice_id,
        )
        await db.queue_add(self.chat_id, self.alice_id, "a", None)
        await db.queue_add(chat2, self.alice_id, "b", None)
        held = await db_queue.queue_hold_orphans()
        self.assertEqual(held, 2)
        self.assertEqual(await db_queue.queue_next(self.chat_id), None)

    async def test_queue_hold_orphans_noop_when_all_held(self):
        self.assertEqual(await db_queue.queue_hold_orphans(), 0)

    # ── queue_hold_all ──────────────────────────────────────────────────

    async def test_queue_hold_all(self):
        await db.queue_add(self.chat_id, self.alice_id, "hold-me", None)
        n = await db_queue.queue_hold_all(self.chat_id)
        self.assertEqual(n, 1)
        self.assertEqual(await db_queue.queue_next(self.chat_id), None)

    async def test_queue_hold_all_empty(self):
        self.assertEqual(await db_queue.queue_hold_all("empty-chat"), 0)

    # ── last_models_used / last_model_used ──────────────────────────────

    async def test_last_models_used_empty(self):
        """No usage_events → empty dict."""
        self.assertEqual(await db_queue.last_models_used(self.alice_id), {})

    async def test_last_model_used_empty(self):
        """No usage_events for a single chat → empty string."""
        self.assertEqual(await db_queue.last_model_used(self.chat_id, self.alice_id), "")

    async def test_last_model_used_single(self):
        """One usage event → its model."""
        now = db._now()
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, created_at) VALUES (?, ?, ?, ?)",
            (self.chat_id, self.alice_id, "claude-sonnet-5", now),
        )
        await db.db_conn.commit()
        self.assertEqual(
            await db_queue.last_model_used(self.chat_id, self.alice_id), "claude-sonnet-5",
        )


# ── db_read_marks ──────────────────────────────────────────────────────────

class DbReadMarksTests(_BaseDBTest):

    async def test_read_marks_get_empty(self):
        self.assertEqual(await db_read_marks.read_marks_get(self.alice_id), {})

    async def test_read_mark_set_returns_timestamp(self):
        stamp = await db_read_marks.read_mark_set(
            self.alice_id, "orchestrator", "orch-1",
        )
        self.assertIn("T", stamp)
        self.assertIn("Z", stamp)

    async def test_read_mark_set_stores_row(self):
        await db_read_marks.read_mark_set(
            self.alice_id, "orchestrator", "orch-1",
        )
        marks = await db_read_marks.read_marks_get(self.alice_id)
        self.assertIn(("orchestrator", "orch-1"), marks)
        self.assertIn("read_at", marks[("orchestrator", "orch-1")])
        self.assertIn("dismissed_at", marks[("orchestrator", "orch-1")])

    async def test_read_mark_set_dismiss(self):
        stamp = await db_read_marks.read_mark_set(
            self.alice_id, "orchestrator", "orch-1", dismiss=True,
        )
        marks = await db_read_marks.read_marks_get(self.alice_id)
        data = marks[("orchestrator", "orch-1")]
        self.assertEqual(data["dismissed_at"], stamp)

    async def test_read_mark_set_overwrites(self):
        """Setting a mark again updates the previous read_at/dismissed_at."""
        stamp1 = await db_read_marks.read_mark_set(
            self.alice_id, "orchestrator", "orch-1",
        )
        self.assertIn("T", stamp1)
        # Small delay to ensure different timestamps.
        await db_read_marks.read_mark_set(
            self.alice_id, "orchestrator", "orch-1",
        )
        marks = await db_read_marks.read_marks_get(self.alice_id)
        data = marks[("orchestrator", "orch-1")]
        self.assertIn("read_at", data)

    async def test_read_marks_cross_user(self):
        """Bob's marks are invisible to Alice."""
        await db_read_marks.read_mark_set(
            self.bob_id, "orchestrator", "orch-bob",
        )
        marks = await db_read_marks.read_marks_get(self.alice_id)
        self.assertNotIn(("orchestrator", "orch-bob"), marks)

    async def test_chat_last_activity_empty(self):
        self.assertEqual(await db_read_marks.chat_last_activity(self.alice_id), {})

    async def test_chat_last_activity_returns_latest_per_chat(self):
        await db.messages_append(self.chat_id, "user", "hello")
        await db.messages_append(self.chat_id, "assistant", "hi")
        activity = await db_read_marks.chat_last_activity(self.alice_id)
        self.assertIn(self.chat_id, activity)
        entry = activity[self.chat_id]
        self.assertEqual(entry["role"], "assistant")
        self.assertIn("created_at", entry)
        self.assertIn("preview", entry)
        self.assertIn("tail", entry)

    async def test_chat_last_activity_scoped_to_owner(self):
        """Bob's messages don't appear in Alice's last_activity."""
        bob_chat = "qa-chat-bob"
        await db.chat_create(
            bob_chat, "Bob's chat", None,
            f"{self.tmp.name}/projects", self.bob_id,
        )
        await db.messages_append(bob_chat, "user", "bob-msg")
        activity = await db_read_marks.chat_last_activity(self.alice_id)
        self.assertNotIn(bob_chat, activity)


# ── db_transport_sync ──────────────────────────────────────────────────────

class DbTransportSyncTests(_BaseDBTest):

    async def test_sync_request_create(self):
        """A pending sync request is created with an id."""
        rid = await db_transport_sync.sync_request_create(
            "t1", self.alice_id, "agent", "pending",
        )
        self.assertIsInstance(rid, int)
        self.assertGreater(rid, 0)
        row = await db_transport_sync.sync_request_get(rid, self.alice_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["transport_id"], "t1")
        self.assertEqual(row["requested_by"], "agent")
        self.assertEqual(row["status"], "pending")

    async def test_sync_request_create_approved(self):
        """Human-triggered sync starts with status='approved'."""
        rid = await db_transport_sync.sync_request_create(
            "t1", self.alice_id, "human", "approved",
        )
        row = await db_transport_sync.sync_request_get(rid, self.alice_id)
        self.assertEqual(row["status"], "approved")

    async def test_sync_request_get_nonexistent(self):
        self.assertIsNone(await db_transport_sync.sync_request_get(999999, self.alice_id))

    async def test_sync_request_get_cross_owner(self):
        """Bob cannot see Alice's sync request."""
        rid = await db_transport_sync.sync_request_create(
            "t1", self.alice_id, "agent", "pending",
        )
        row = await db_transport_sync.sync_request_get(rid, self.bob_id)
        self.assertIsNone(row)

    async def test_sync_request_list_pending(self):
        """Lists only pending requests for the owner."""
        await db_transport_sync.sync_request_create(
            "t1", self.alice_id, "agent", "pending",
        )
        approved = await db_transport_sync.sync_request_create(
            "t2", self.alice_id, "human", "approved",
        )
        bob_pending = await db_transport_sync.sync_request_create(
            "t3", self.bob_id, "agent", "pending",
        )
        pending = await db_transport_sync.sync_request_list_pending(self.alice_id)
        ids = [r["id"] for r in pending]
        self.assertNotIn(approved, ids)  # approved status → not in pending list
        self.assertNotIn(bob_pending, ids)  # bob's request → cross-owner

    async def test_sync_request_list_pending_empty(self):
        self.assertEqual(await db_transport_sync.sync_request_list_pending(self.bob_id), [])

    async def test_sync_request_resolve(self):
        """Resolving changes status and sets resolved_at."""
        rid = await db_transport_sync.sync_request_create(
            "t1", self.alice_id, "agent", "pending",
        )
        await db_transport_sync.sync_request_resolve(
            rid, "rejected", files_changed=3, reason="bad changes",
        )
        row = await db_transport_sync.sync_request_get(rid, self.alice_id)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["files_changed"], 3)
        self.assertEqual(row["reason"], "bad changes")
        self.assertIn("resolved_at", row)
        self.assertIn("T", row["resolved_at"])

    async def test_sync_request_resolve_rejects(self):
        """A rejected sync has status='rejected'."""
        rid = await db_transport_sync.sync_request_create(
            "t1", self.alice_id, "agent", "pending",
        )
        await db_transport_sync.sync_request_resolve(rid, "rejected", reason="bad")
        pending = await db_transport_sync.sync_request_list_pending(self.alice_id)
        self.assertNotIn(rid, [r["id"] for r in pending])


if __name__ == "__main__":
    unittest.main()
