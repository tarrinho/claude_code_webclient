"""App-layer tests for conversation management APIs.

Covers: list (pinned fields), pin/unpin, delete 404, Markdown export,
title/description/archive mutations, workspace preservation.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import auth
import config
import db
import runner
import turns
from routes import chats as chat_routes
from routes import machines as machine_routes
from routes import misc as misc_routes

# ── Tests ──────────────────────────────────────────────────────────────────────

class ChatListTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/chats must include pinned / pinned_at in the response."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self._session = SimpleNamespace(
            user="admin",
        )

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_list_includes_pinned_and_pinned_at(self):
        chat_id = "a" * 32
        await db.chat_create(chat_id, "My Chat", None, f"{self.tmp.name}/proj", "admin")
        chats = await db.chat_list("admin")
        entry = chats[0]
        self.assertIn("pinned", entry)
        self.assertIn("pinned_at", entry)
        self.assertEqual(entry["pinned"], 0)
        self.assertIsNone(entry["pinned_at"])

    async def test_pinned_chat_has_pinned_at(self):
        chat_id = "b" * 32
        await db.chat_create(chat_id, "Pinned", None, f"{self.tmp.name}/proj2", "admin")
        await db.chat_update(chat_id, "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        chats = await db.chat_list("admin")
        self.assertEqual(chats[0]["pinned"], 1)
        self.assertEqual(chats[0]["pinned_at"], "2026-01-01T00:00:00Z")


class PinMutationTests(unittest.IsolatedAsyncioTestCase):
    """PATCH /api/chats/{id} with pinned=true/false."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_pin_sets_pinned_at(self):
        chat_id = "c" * 32
        await db.chat_create(chat_id, "To Pin", None, f"{self.tmp.name}/proj", "admin")
        patched = await db.chat_update(chat_id, "admin", pinned=1)
        self.assertTrue(patched)
        chat = await db.chat_get(chat_id, "admin")
        self.assertTrue(chat["pinned"])
        self.assertIsNotNone(chat["pinned_at"])

    async def test_unpin_cleared_pinned_at(self):
        chat_id = "d" * 32
        await db.chat_create(chat_id, "Unpin", None, f"{self.tmp.name}/proj", "admin")
        await db.chat_update(chat_id, "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        patched = await db.chat_update(chat_id, "admin", pinned=0, pinned_at=None)
        self.assertTrue(patched)
        chat = await db.chat_get(chat_id, "admin")
        self.assertFalse(chat["pinned"])
        self.assertIsNone(chat["pinned_at"])

    async def test_pin_unknown_chat_fails(self):
        result = await db.chat_update("nonexistent", "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        self.assertFalse(result)


class DeleteMutationTests(unittest.IsolatedAsyncioTestCase):
    """DELETE /api/chats/{id} returns 404 when chat doesn't exist,
    and actually removes records when it does."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_delete_fails_when_missing(self):
        result = await db.chat_delete("nonexistent", "admin")
        self.assertFalse(result)

    async def test_delete_removes_chat_and_messages_but_not_workspace(self):
        chat_id = "e" * 32
        work_dir = Path(self.tmp.name) / "projects" / "delete-me"
        work_dir.mkdir(parents=True)
        (work_dir / "test.txt").write_text("data")
        await db.chat_create(chat_id, "Delete Me", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "hello")
        self.assertTrue(await db.chat_delete(chat_id, "admin"))
        self.assertIsNone(await db.chat_get(chat_id, "admin", include_archived=True))
        self.assertEqual(await db.messages_get(chat_id), [])
        self.assertTrue(work_dir.exists())
        self.assertEqual((work_dir / "test.txt").read_text(), "data")

    async def test_delete_by_wrong_user_fails(self):
        chat_id = "f" * 32
        await db.chat_create(chat_id, "Mine", None, f"{self.tmp.name}/proj", "admin")
        self.assertFalse(await db.chat_delete(chat_id, "other-user"))


class ExportTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/chats/{id}/export returns a Markdown attachment."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_export_rendering(self):
        chat_id = "g" * 32
        await db.chat_create(chat_id, "Export Me", "A description", f"{self.tmp.name}/proj", "admin")
        await db.messages_append(chat_id, "user", "question")
        await db.messages_append(chat_id, "assistant", "answer")
        chat = await db.chat_get(chat_id, "admin", include_archived=True)
        messages = await db.messages_get(chat_id)
        md = chat_routes.render_chat_markdown(chat, messages)
        self.assertIn("# Export Me", md)
        self.assertIn("> A description", md)
        self.assertIn("question", md)
        self.assertIn("answer", md)
        self.assertIn("- Workspace:", md)
        self.assertIn("- Session:", md)

    async def test_export_404_for_missing(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_export(
                SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": "admin"})),
                "nonexistent",
            )
        self.assertEqual(ctx.exception.status_code, 404)


class ChatTitleDescriptionTests(unittest.IsolatedAsyncioTestCase):
    """PATCH /api/chats/{id} with title / description."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_title_update(self):
        chat_id = "h" * 32
        await db.chat_create(chat_id, "Old Title", None, f"{self.tmp.name}/proj", "admin")
        self.assertTrue(await db.chat_update(chat_id, "admin", title="New Title"))
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["title"], "New Title")

    async def test_description_update(self):
        chat_id = "i" * 32
        await db.chat_create(chat_id, "Chat", "first desc", f"{self.tmp.name}/proj", "admin")
        self.assertTrue(await db.chat_update(chat_id, "admin", description="second"))
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["description"], "second")

    async def test_archive_toggle(self):
        chat_id = "j" * 32
        await db.chat_create(chat_id, "Archive", None, f"{self.tmp.name}/proj", "admin")
        self.assertTrue(await db.chat_update(chat_id, "admin", archived=1))
        chat = await db.chat_get(chat_id, "admin", include_archived=True)
        self.assertTrue(chat["archived"])
        self.assertTrue(await db.chat_update(chat_id, "admin", archived=0))
        chat2 = await db.chat_get(chat_id, "admin")
        self.assertFalse(chat2["archived"])

    async def test_truncate_title_via_patch_handler(self):
        """The app handler (not the raw db) truncates titles to 200 chars."""
        chat_id = "k" * 32
        work_dir = Path(self.tmp.name) / "projects" / "trunc"
        work_dir.mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "Old", None, str(work_dir), "admin")

        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={"title": "x" * 300}),
        )
        response = await chat_routes.handle_chat_patch(request, chat_id)
        self.assertEqual(response.status_code, 200)

        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["title"], "x" * 200)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Full FastAPI route tests with mock auth/session to cover the handlers end-to-end."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_chat_delete_404_missing(self):
        from fastapi import HTTPException

        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_delete(request, "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_chat_delete_success(self):
        from fastapi import HTTPException

        chat_id = "l" * 32
        work_dir = Path(self.tmp.name) / "projects" / "ok"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Ok", None, str(work_dir), "admin")
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        )
        response = await chat_routes.handle_chat_delete(request, chat_id)
        self.assertEqual(response.status_code, 200)
        # Second call should raise 404
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_delete(request, chat_id)
        self.assertEqual(ctx.exception.status_code, 404)
        # Workspace still exists
        self.assertTrue(work_dir.exists())

    async def test_patch_rejects_non_boolean_flags(self):
        chat_id = "n" * 32
        work_dir = Path(self.tmp.name) / "projects" / "strict"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Strict", None, str(work_dir), "admin")
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={"pinned": "yes"}),
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_patch(request, chat_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_patch_rejects_blank_title_and_caps_description(self):
        chat_id = "o" * 32
        work_dir = Path(self.tmp.name) / "projects" / "validate"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Valid", None, str(work_dir), "admin")
        from fastapi import HTTPException
        blank = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={"title": "   "}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_patch(blank, chat_id)
        self.assertEqual(ctx.exception.status_code, 400)
        capped = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={"description": "d" * 600}),
        )
        await chat_routes.handle_chat_patch(capped, chat_id)
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(len(chat["description"]), 500)

    async def test_chat_export_returns_markdown(self):
        chat_id = "m" * 32
        work_dir = Path(self.tmp.name) / "projects" / "export-test"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Export Test", "desc here", str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "hi")
        await db.messages_append(chat_id, "assistant", "hello!")
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        )
        response = await chat_routes.handle_chat_export(request, chat_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/markdown; charset=utf-8")
        self.assertIn("Export Test", response.body.decode("utf-8"))
        disp = response.headers.get("content-disposition", "")
        self.assertIn("export-test.md", disp)


class StreamPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.work_dir = Path(config.PROJECTS_ROOT) / "stream"
        self.work_dir.mkdir(parents=True)
        self.chat_id = "s" * 32
        await db.chat_create(self.chat_id, "Stream", None, str(self.work_dir), "admin")
        self.request = SimpleNamespace(
            cookies={"wc_session": "valid"},
            json=AsyncMock(return_value={"content": "hello"}),
        )

    async def asyncTearDown(self):
        # Live turns are module state in turns.py, so one test's turn would
        # otherwise still be registered when the next one starts and every
        # subsequent send would queue behind a corpse.
        await turns.shutdown()
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _run_stream(self, events):
        async def fake_stream(*args, **kwargs):
            for event in events:
                yield event

        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(runner, "stream_turn", fake_stream):
            response = await chat_routes.stream_handler(self.request, self.chat_id)
            chunks = [chunk async for chunk in response.body_iterator]
            return "".join(
                chunk.decode() if isinstance(chunk, bytes) else chunk
                for chunk in chunks
            )

    async def test_oversized_stream_prompt_is_rejected_before_runner(self):
        self.request.json = AsyncMock(return_value={"content": "x" * (config.PROMPT_MAX_CHARS + 1)})
        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(runner, "stream_turn") as stream_turn:
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as ctx:
                await chat_routes.stream_handler(self.request, self.chat_id)
        self.assertEqual(ctx.exception.status_code, 400)
        stream_turn.assert_not_called()

    async def test_complete_stream_persists_pair_and_session(self):
        body = await self._run_stream([
            {"type": "session_id", "session_id": "session-new"},
            {"type": "text", "content": "hello back"},
            {"type": "done"},
        ])
        self.assertIn('\"type\": \"done\"', body)
        messages = await db.messages_get(self.chat_id)
        self.assertEqual(
            [(message["role"], message["content"]) for message in messages],
            [("user", "hello"), ("assistant", "hello back")],
        )
        chat = await db.chat_get(self.chat_id, "admin")
        self.assertEqual(chat["session_id"], "session-new")

    async def test_incomplete_stream_does_not_persist_partial_turn(self):
        body = await self._run_stream([
            {"type": "session_id", "session_id": "discard-me"},
            {"type": "text", "content": "partial"},
        ])
        self.assertIn("Stream ended before completion", body)
        self.assertEqual(await db.messages_get(self.chat_id), [])
        chat = await db.chat_get(self.chat_id, "admin")
        self.assertIsNone(chat["session_id"])

    async def test_failed_retry_does_not_duplicate_user_prompt(self):
        failed_body = await self._run_stream([
            {"type": "text", "content": "partial"},
            {"type": "error", "error": "failed"},
            {"type": "done"},
        ])
        self.assertNotIn('\"type\": \"done\"', failed_body)
        await self._run_stream([
            {"type": "text", "content": "complete"},
            {"type": "done"},
        ])
        messages = await db.messages_get(self.chat_id)
        self.assertEqual(
            [(message["role"], message["content"]) for message in messages],
            [("user", "hello"), ("assistant", "complete")],
        )

    async def test_closing_the_stream_leaves_the_turn_running(self):
        """A viewer leaving must not stop the turn. This is the whole feature.

        Closing the response used to cancel the runner and discard the answer
        -- while the usage row had already been written, because the tokens are
        spent as they arrive. So switching conversations mid-turn billed the
        user and returned nothing. The UI's refusal to switch was protecting
        the turn, not the interface.
        """
        release = asyncio.Event()

        async def fake_stream(*args, **kwargs):
            yield {"type": "text", "content": "partial"}
            await release.wait()
            yield {"type": "text", "content": " and the rest"}
            yield {"type": "done"}

        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(runner, "stream_turn", fake_stream):
            response = await chat_routes.stream_handler(self.request, self.chat_id)
            iterator = response.body_iterator
            await anext(iterator)          # start frame
            await anext(iterator)          # first token
            await iterator.aclose()        # the user switches conversation

            # Still alive with nobody watching.
            self.assertTrue(turns.is_running(self.chat_id))
            release.set()
            await turns.get(self.chat_id).task

        self.assertEqual(
            [(m["role"], m["content"])
             for m in await db.messages_get(self.chat_id)],
            [("user", "hello"), ("assistant", "partial and the rest")],
        )

    async def test_a_reattaching_client_is_replayed_what_it_missed(self):
        # The other half of switching away: coming back has to show the answer
        # built so far, not an empty conversation.
        release = asyncio.Event()

        async def fake_stream(*args, **kwargs):
            yield {"type": "text", "content": "first"}
            await release.wait()
            yield {"type": "text", "content": "second"}
            yield {"type": "done"}

        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(runner, "stream_turn", fake_stream):
            response = await chat_routes.stream_handler(self.request, self.chat_id)
            iterator = response.body_iterator
            await anext(iterator)
            await anext(iterator)
            await iterator.aclose()

            seen = []
            async def reattach():
                async for event in turns.follow(self.chat_id, 0):
                    if event.get("type") == "text":
                        seen.append(event["content"])
            follower = asyncio.create_task(reattach())
            await asyncio.sleep(0)
            release.set()
            await turns.get(self.chat_id).task
            await asyncio.wait_for(follower, timeout=1)

        # "first" was emitted before the reattach and still arrives.
        self.assertEqual(seen, ["first", "second"])

    async def test_a_second_prompt_while_running_is_queued(self):
        release = asyncio.Event()

        async def fake_stream(*args, **kwargs):
            yield {"type": "text", "content": "one"}
            await release.wait()
            yield {"type": "done"}

        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(runner, "stream_turn", fake_stream):
            first = await chat_routes.stream_handler(self.request, self.chat_id)
            iterator = first.body_iterator
            await anext(iterator)
            await anext(iterator)

            self.request.json = AsyncMock(return_value={"content": "second ask"})
            second = await chat_routes.stream_handler(self.request, self.chat_id)
            body = "".join([
                chunk.decode() if isinstance(chunk, bytes) else chunk
                async for chunk in second.body_iterator
            ])
            self.assertIn('"type": "queued"', body)
            self.assertIn('"position": 1', body)
            self.assertEqual(
                [row["prompt"] for row in await db.queue_list(self.chat_id, "admin")],
                ["second ask"],
            )
            await iterator.aclose()
            release.set()
            await turns.get(self.chat_id).task


class MachineTests(unittest.IsolatedAsyncioTestCase):
    """AI machine CRUD and activation endpoints."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, **extra):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": "admin"}), **extra)

    async def test_machine_list_seeds_anthropic_only(self):
        """A fresh account starts with the Anthropic API entry and nothing else.

        Claude Code's native backend is always on offer, so the list is never
        empty -- it was, before machines carried a provider.
        """
        resp = await machine_routes.handle_machines_list(self._make_request())
        data = json.loads(resp.body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(data["machines"]), 1)
        self.assertEqual(data["machines"][0]["provider"], "anthropic")

    async def test_machine_create_and_list(self):
        request = self._make_request(
            json=AsyncMock(return_value={
                "name": "GCP", "host": "10.0.1.5", "port": 9001,
                "model": "claude-sonnet-4-20250514",
            }),
        )
        resp = await machine_routes.handle_machine_create(request)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data["id"])
        # Verify it appears in the list (api key hidden), alongside the
        # seeded Anthropic entry every account gets.
        list_resp = await machine_routes.handle_machines_list(self._make_request())
        list_data = json.loads(list_resp.body)
        self.assertEqual(len(list_data["machines"]), 2)
        created = next(m for m in list_data["machines"] if m["name"] == "GCP")
        self.assertEqual(created["provider"], "proxy")
        self.assertNotIn("api_key", created)

    async def test_machine_create_rejects_empty_name(self):
        request = self._make_request(
            json=AsyncMock(return_value={"name": "", "host": "10.0.0.1", "port": 9000}),
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_create(request)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_create_rejects_bad_host(self):
        request = self._make_request(
            json=AsyncMock(return_value={"name": "X", "host": "not a host!", "port": 9000}),
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_create(request)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_get(self):
        request = self._make_request(
            json=AsyncMock(return_value={
                "name": "Local", "host": "127.0.0.1", "port": 9000,
            }),
        )
        create_resp = await machine_routes.handle_machine_create(request)
        mid = json.loads(create_resp.body)["id"]
        get_resp = await machine_routes.handle_machine_get(self._make_request(), mid)
        data = json.loads(get_resp.body)
        self.assertEqual(data["machine"]["name"], "Local")
        self.assertIsInstance(data["machine"]["has_api_key"], bool)

    async def test_machine_get_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_get(self._make_request(), "zz" * 32)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_activate(self):
        request = self._make_request(
            json=AsyncMock(return_value={"name": "A", "host": "10.0.0.1", "port": 9000}),
        )
        resp = await machine_routes.handle_machine_create(request)
        mid = (json.loads(resp.body))["id"]
        act_resp = await machine_routes.handle_machine_activate(self._make_request(), mid)
        data = json.loads(act_resp.body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["activated"])

    async def test_machine_patch(self):
        request = self._make_request(
            json=AsyncMock(return_value={"name": "Old", "host": "10.0.0.1", "port": 9000}),
        )
        resp = await machine_routes.handle_machine_create(request)
        mid = (json.loads(resp.body))["id"]
        patch_resp = await machine_routes.handle_machine_patch(
            self._make_request(json=AsyncMock(return_value={"name": "New"})), mid,
        )
        data = json.loads(patch_resp.body)
        self.assertTrue(data["ok"])

    async def test_machine_delete(self):
        request = self._make_request(
            json=AsyncMock(return_value={"name": "X", "host": "10.0.0.1", "port": 9000}),
        )
        resp = await machine_routes.handle_machine_create(request)
        mid = (json.loads(resp.body))["id"]
        del_resp = await machine_routes.handle_machine_delete(self._make_request(), mid)
        data = json.loads(del_resp.body)
        self.assertTrue(data["ok"])
        # Should now be gone
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_delete(self._make_request(), mid)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_list_no_api_keys(self):
        request = self._make_request(
            json=AsyncMock(return_value={
                "name": "Secret", "host": "10.0.0.2", "port": 9000,
                "api_key": "supersecret",
            }),
        )
        await machine_routes.handle_machine_create(request)
        list_resp = await machine_routes.handle_machines_list(self._make_request())
        list_data = json.loads(list_resp.body)
        self.assertNotIn("api_key", list_data["machines"][0])


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    """App settings (session TTL, turn timeout, prompt max) persistence."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, **extra):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": "admin"}), **extra)

    async def test_settings_get_includes_defaults(self):
        resp = await misc_routes.handle_settings_get(self._make_request())
        data = json.loads(resp.body)
        self.assertIn("session_ttl_s", data)
        self.assertIn("turn_timeout_s", data)
        self.assertIn("prompt_max", data)
        self.assertIn("version", data)

    async def test_settings_patch_updates_session_ttl(self):
        resp = await misc_routes.handle_settings_patch(
            self._make_request(json=AsyncMock(return_value={"session_ttl": 7200})),
        )
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])

    async def test_settings_patch_rejects_invalid_ttl(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_settings_patch(
                self._make_request(json=AsyncMock(return_value={"session_ttl": 5})),
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()