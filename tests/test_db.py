import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/webconsole.db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_schema_and_default_chat_fields(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"pinned", "pinned_at", "deleted_at"}.issubset(columns))
        await db.chat_create("one", "One", None, f"{self.tmp.name}/projects/one", "admin")
        chat = await db.chat_get("one", "admin")
        self.assertEqual(chat["pinned"], 0)
        self.assertIsNone(chat["pinned_at"])
        self.assertIsNone(chat["deleted_at"])

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        names = [row["name"] for row in await cursor.fetchall()]
        self.assertEqual(names.count("pinned"), 1)

    async def test_chat_list_groups_pinned_recent_and_archived(self):
        for chat_id in ("recent", "pinned-old", "pinned-new", "archived"):
            await db.chat_create(chat_id, chat_id, None, f"{self.tmp.name}/{chat_id}", "admin")
        await db.db_conn.execute("UPDATE chats SET updated_at = '2026-01-01T00:00:00Z' WHERE id = 'recent'")
        await db.chat_update("pinned-old", "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        await db.chat_update("pinned-new", "admin", pinned=1, pinned_at="2026-02-01T00:00:00Z")
        await db.chat_update("archived", "admin", archived=1)
        chats = await db.chat_list("admin")
        self.assertEqual([c["id"] for c in chats], ["pinned-new", "pinned-old", "recent", "archived"])

    async def test_last_model_used_single_chat_matches_the_batched_form(self):
        """`db.last_model_used(chat_id, owner)` is the single-chat form of
        `last_models_used` used by GET /api/chats/{id} -- the two must never
        disagree about the same conversation."""
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.usage_record("c1", "admin", "claude-sonnet-5", "claude_code")
        await db.usage_record("c1", "admin", "claude-opus-5", "claude_code")
        single = await db.last_model_used("c1", "admin")
        batched = (await db.last_models_used("admin"))["c1"]
        self.assertEqual(single, "claude-opus-5")
        self.assertEqual(single, batched)

    async def test_last_model_used_is_empty_for_a_chat_with_no_turns(self):
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        self.assertEqual(await db.last_model_used("c1", "admin"), "")

    async def test_last_model_used_is_owner_scoped(self):
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.usage_record("c1", "someone-else", "claude-opus-5", "claude_code")
        self.assertEqual(await db.last_model_used("c1", "admin"), "")

    async def test_last_models_used_is_the_newest_row_per_chat(self):
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.chat_create("c2", "C2", None, f"{self.tmp.name}/c2", "admin")
        # c1 switches model mid-conversation; the newest row must win, not the
        # first or an arbitrary one -- this is the whole point of the query.
        await db.usage_record("c1", "admin", "claude-sonnet-5", "claude_code")
        await db.usage_record("c1", "admin", "claude-opus-5", "claude_code")
        await db.usage_record("c2", "admin", "vllm/Qwen3.6-35B-A3B-NVFP4", "anthropic-compatible")
        last = await db.last_models_used("admin")
        self.assertEqual(last["c1"], "claude-opus-5")
        self.assertEqual(last["c2"], "vllm/Qwen3.6-35B-A3B-NVFP4")

    async def test_last_models_used_ties_break_on_row_id_not_timestamp(self):
        # Two rows landing in the same turn (multi-model usage, or two writes
        # in the same clock tick) must not make "newest" ambiguous. MAX(id) is
        # exact where MAX(created_at) is not.
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("c1", "admin", "claude-sonnet-5", "anthropic", "2026-01-01T00:00:00Z"),
        )
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("c1", "admin", "claude-opus-5", "anthropic", "2026-01-01T00:00:00Z"),
        )
        await db.db_conn.commit()
        last = await db.last_models_used("admin")
        self.assertEqual(last["c1"], "claude-opus-5")

    async def test_last_models_used_is_owner_scoped_and_excludes_terminal_rows(self):
        await db.chat_create("mine", "Mine", None, f"{self.tmp.name}/mine", "admin")
        await db.usage_record("mine", "admin", "claude-opus-5", "claude_code")
        # A row from a different owner must not leak into admin's view.
        await db.usage_record("theirs", "someone-else", "claude-opus-5", "claude_code")
        # chat_id='' is how a terminal-origin turn is recorded (db.py comment
        # on usage_events.session_id) and must not surface as a "chat".
        await db.usage_record("terminal-session-id", "admin", "claude-opus-5",
                              "claude_code", origin="terminal")
        await db.db_conn.execute(
            "UPDATE usage_events SET chat_id = '' WHERE origin = 'terminal'"
        )
        await db.db_conn.commit()
        last = await db.last_models_used("admin")
        self.assertEqual(set(last), {"mine"})

    async def test_last_model_used_is_independent_of_the_routing_override(self):
        """`chats.model` and last-used must never collide.

        `chats.model` is the user's routing override (runner.get_default_model
        reads it before the backend/global default); it is set explicitly, not
        derived from usage. The two must be able to disagree -- that disagreement
        is exactly what lets a chat serve on model A while still being pinned to
        model B for its next turn.
        """
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.chat_set_model("c1", "claude-opus-5")
        await db.usage_record("c1", "admin", "vllm/Qwen3.6-35B-A3B-NVFP4",
                              "anthropic-compatible")
        chat = await db.chat_get("c1", "admin")
        last = await db.last_models_used("admin")
        self.assertEqual(chat["model"], "claude-opus-5")
        self.assertEqual(last["c1"], "vllm/Qwen3.6-35B-A3B-NVFP4")

    async def test_archived_chat_requires_explicit_lookup(self):
        await db.chat_create("archived", "Archived", None, f"{self.tmp.name}/archived", "admin")
        await db.chat_update("archived", "admin", archived=1)
        self.assertIsNone(await db.chat_get("archived", "admin"))
        self.assertIsNotNone(await db.chat_get("archived", "admin", include_archived=True))
        self.assertIsNone(await db.chat_get("archived", "other", include_archived=True))

    async def test_delete_removes_records_but_preserves_workspace(self):
        work_dir = Path(self.tmp.name) / "projects" / "kept"
        work_dir.mkdir(parents=True)
        await db.chat_create("delete", "Delete", None, str(work_dir), "admin")
        await db.messages_append("delete", "user", "hello")
        await db.messages_append("delete", "assistant", "hi")
        self.assertTrue(await db.chat_delete("delete", "admin"))
        self.assertIsNone(await db.chat_get("delete", "admin", include_archived=True))
        self.assertEqual(await db.messages_get("delete"), [])
        self.assertTrue(work_dir.exists())
        self.assertFalse(await db.chat_delete("missing", "admin"))

    async def test_chat_update_rejects_unknown_columns(self):
        await db.chat_create("one", "One", None, f"{self.tmp.name}/one", "admin")
        self.assertFalse(await db.chat_update("one", "admin", session_id="unsafe"))
        self.assertFalse(await db.chat_update("one", "other", title="No"))

    async def test_concurrent_message_batches_return_their_exact_ids(self):
        import asyncio

        await db.chat_create("one", "One", None, f"{self.tmp.name}/one", "admin")
        first, second = await asyncio.gather(
            db.messages_batch("one", [("user", "first-u"), ("assistant", "first-a")]),
            db.messages_batch("one", [("user", "second-u"), ("assistant", "second-a")]),
        )
        self.assertEqual(len(set(first + second)), 4)
        messages = {message["id"]: message["content"] for message in await db.messages_get("one")}
        self.assertEqual([messages[row_id] for row_id in first], ["first-u", "first-a"])
        self.assertEqual([messages[row_id] for row_id in second], ["second-u", "second-a"])


class AiMachinesTransportIdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_ai_machines_has_transport_id_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(ai_machines)")
        columns = {row["name"] for row in await cur.fetchall()}
        self.assertIn("transport_id", columns)

    async def test_create_with_transport_id_round_trips(self):
        await db.ai_machine_create(
            "m1", "CF AI Machine (via Kali3)", "llm.ai-machine.cfappsecurity.com",
            443, None, "vllm/Qwen3.6-35B-A3B-NVFP4",
            "https://llm.ai-machine.cfappsecurity.com", None, "admin",
            provider="claude_code", transport_id="t1",
        )
        row = await db.ai_machine_get("m1", "admin")
        self.assertEqual(row["transport_id"], "t1")

    async def test_create_without_transport_id_defaults_to_null(self):
        await db.ai_machine_create(
            "m2", "Anthropic API", "api.anthropic.com", 443, None,
            "claude-sonnet-5", "https://api.anthropic.com", None, "admin",
            provider="claude_code",
        )
        row = await db.ai_machine_get("m2", "admin")
        self.assertIsNone(row["transport_id"])

    async def test_update_can_set_and_clear_transport_id(self):
        await db.ai_machine_create(
            "m3", "CF AI Machine", "llm.ai-machine.cfappsecurity.com", 443,
            None, "vllm/Qwen3.6-35B-A3B-NVFP4", None, None, "admin",
            provider="claude_code",
        )
        await db.ai_machine_update("m3", "admin", transport_id="t1")
        self.assertEqual((await db.ai_machine_get("m3", "admin"))["transport_id"], "t1")
        await db.ai_machine_clear_transport("m3", "admin")
        # NOTE: ai_machine_update's existing pairs-building only sets a field
        # when `value is not None` (see routes/db_machines.py) -- clearing
        # transport_id back to NULL needs its own explicit path (Step 4 below
        # adds one), not a bare None kwarg. This test documents that: passing
        # None must not silently no-op.
        self.assertIsNone((await db.ai_machine_get("m3", "admin"))["transport_id"])

    async def test_backend_columns_include_transport_id(self):
        await db.ai_machine_create(
            "m4", "CF AI Machine", "h", 443, None, "vllm/x", None, None,
            "admin", provider="claude_code", transport_id="t1",
        )
        backend = await db.ai_machine_backend_by_id("m4", "admin")
        self.assertEqual(backend["transport_id"], "t1")

    async def test_machines_list_includes_transport_id(self):
        """ai_machines_list must expose transport_id -- routes/machines_tunnel.py's
        tunnel_status_endpoint (and the frontend's transport grouping) rely on
        db.ai_machines_list(...) returning it directly, not a bare id lookup."""
        await db.ai_machine_create(
            "m5", "CF AI Machine (via Kali3)", "h", 443, None, "vllm/x", None,
            None, "admin", provider="claude_code", transport_id="t1",
        )
        machines = await db.ai_machines_list("admin")
        machine = next(m for m in machines if m["id"] == "m5")
        self.assertEqual(machine["transport_id"], "t1")


class SshProxyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)

    async def test_existing_ssh_proxy_row_becomes_transport_plus_backend(self):
        # Simulate a pre-migration database: init() once to get every OTHER
        # table, then hand-insert an old-shape ssh_proxy row directly (bypasses
        # ai_machine_create, which no longer accepts ssh_host/etc, since this
        # models data that predates this migration).
        await db.init()
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at, "
            " ssh_host, ssh_user, ssh_key_path, ssh_host_key_fingerprint) "
            "VALUES ('old1', 'Kali3', 'ssh_proxy', '', 9000, NULL, "
            "        'vllm/Qwen3.6-35B-A3B-NVFP4', NULL, NULL, 0, 'admin', "
            "        '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z', "
            "        'kali-3.tail850c40.ts.net', 'kali', '~/.ssh/id_ed25519', '')"
        )
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at) "
            "VALUES ('cfai', 'CF AI Machine', 'claude_code', "
            "        'llm.ai-machine.cfappsecurity.com', 443, 'real-key', "
            "        'vllm/Qwen3.6-35B-A3B-NVFP4', "
            "        'https://llm.ai-machine.cfappsecurity.com', NULL, 1, "
            "        'admin', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
        )
        await db.db_conn.commit()
        await db.close()

        # Re-run init() -- this is where the migration must fire.
        await db.init()
        self.addAsyncCleanup(db.close)

        # Old row gone.
        old = await db.ai_machine_get("old1", "admin")
        self.assertIsNone(old)

        # A transport now exists carrying Kali3's SSH details.
        transports = await db.ssh_transports_list("admin")
        self.assertEqual(len(transports), 1)
        transport = transports[0]
        self.assertEqual(transport["name"], "Kali3")
        self.assertEqual(transport["ssh_host"], "kali-3.tail850c40.ts.net")
        self.assertEqual(transport["ssh_key_path"], "~/.ssh/id_ed25519")

        # A new backend exists, copying CF AI Machine's own fields, pointed
        # at the new transport.
        machines = await db.ai_machines_list("admin")
        migrated = [m for m in machines if m.get("transport_id") == transport["id"]]
        self.assertEqual(len(migrated), 1)
        new_backend = await db.ai_machine_backend_by_id(migrated[0]["id"], "admin")
        self.assertEqual(new_backend["model"], "vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(new_backend["base_url"], "https://llm.ai-machine.cfappsecurity.com")
        self.assertEqual(new_backend["api_key"], "real-key")
        self.assertIn("Kali3", migrated[0]["name"])

    async def test_migration_is_idempotent(self):
        """Running init() a second time must not create duplicate transports
        or backends.

        An active claude_code machine is inserted alongside the ssh_proxy
        row (mirroring test_existing_ssh_proxy_row_becomes_transport_plus_backend
        above) so the migration actually takes its "copy the active backend"
        branch and creates a migrated ai_machines row -- without one, this
        is the one production data migration in the whole branch, and
        asserting only the transport count would miss a duplicated backend
        entirely."""
        await db.init()
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at) "
            "VALUES ('cfai2', 'CF AI Machine', 'claude_code', "
            "        'llm.ai-machine.cfappsecurity.com', 443, 'real-key', "
            "        'vllm/Qwen3.6-35B-A3B-NVFP4', "
            "        'https://llm.ai-machine.cfappsecurity.com', NULL, 1, "
            "        'admin', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
        )
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at, "
            " ssh_host, ssh_user, ssh_key_path, ssh_host_key_fingerprint) "
            "VALUES ('old2', 'Pentester-Kali_Mac', 'ssh_proxy', '', 9000, "
            "        NULL, 'vllm/Qwen3.6-35B-A3B-NVFP4', NULL, NULL, 0, "
            "        'admin', '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z', "
            "        'pentester.tail850c40.ts.net', 'claude-ai-machine', "
            "        '~/.ssh/id_ed25519', '')"
        )
        await db.db_conn.commit()
        await db.close()
        await db.init()
        await db.close()
        await db.init()  # second run
        self.addAsyncCleanup(db.close)
        transports = await db.ssh_transports_list("admin")
        self.assertEqual(len(transports), 1)
        transport_id = transports[0]["id"]

        machines = await db.ai_machines_list("admin")
        migrated = [m for m in machines if m.get("transport_id") == transport_id]
        self.assertEqual(
            len(migrated), 1,
            "the migrated backend was duplicated by re-running init()",
        )


if __name__ == "__main__":
    unittest.main()
