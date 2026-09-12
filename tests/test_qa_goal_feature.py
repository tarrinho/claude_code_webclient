"""QA tests for the goal column on chats and the hard-refresh endpoint.

Covers:
* Goal column exists in the chats table schema.
* Goal seeded from initial prompt on chat create (verified via db.chat_get).
* Goal returned in chat list response.
* Goal returned in chat get response (chat.get("goal")).
* Goal editable via PATCH (text, null, empty-string, truncation, type-check).
* Goal never leaks across owners.
* Hard-refresh endpoint returns 302 with no-cache headers.
* Hard-refresh does not require auth session.
* Auth middleware exempts /api/hard-refresh from session check.
* HTML/CSS — workspace goal bar, icon, and mobile media query exist.
"""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import auth
import config
import db
import middleware
from routes import chats as chat_routes
from routes import misc as misc_routes

# ── Helpers (mirror test_qa_coverage.py) ─────────────────────────────────────


def _make_request(
    method="POST", path="/api/chats", body=None, cookies=None, query=None,
):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies=cookies or {},
        headers={"accept": "*/*"},
        query_params=dict(query or {}),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session=None),
        json=AsyncMock(return_value=body or {}),
    )


async def _setup_db(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()


async def _teardown_db(tc):
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


# ── Schema: goal column ─────────────────────────────────────────────────────


class GoalSchemaTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_chats_table_has_goal_column(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        cols = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("goal", cols)

    async def test_goal_column_is_nullable(self):
        chat_id = "schema-test"
        wd = f"{self.tmpdir.name}/projects/{chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "No Goal Chat", None, wd, "admin")
        chat = await db.chat_get(chat_id, "admin")
        self.assertIsNone(chat.get("goal"))


# ── Chat create: goal seeded into DB ────────────────────────────────────────


class ChatCreateGoalTests(unittest.IsolatedAsyncioTestCase):
    """Goal is seeded in DB on create; create response has no goal field."""

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.sess = {"user": "admin", "role": "admin"}

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _create_req(self, body=None):
        r = _make_request(
            method="POST", path="/api/chats", body=body or {},
            cookies={"wc_session": self.sid},
        )
        r.state.session = self.sess
        return r

    async def test_prompt_becomes_goal(self):
        resp = await chat_routes.handle_chat_create(
            self._create_req({"title": "React Todo", "prompt": "Build a todo list app in React"})
        )
        data = json.loads(resp.body)
        chat_id = data["id"]
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["goal"], "Build a todo list app in React")

    async def test_title_fallback_goal(self):
        resp = await chat_routes.handle_chat_create(
            self._create_req({"title": "No Prompt Chat"})
        )
        chat_id = json.loads(resp.body)["id"]
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["goal"], "No Prompt Chat")

    async def test_goal_truncated_to_1000(self):
        resp = await chat_routes.handle_chat_create(
            self._create_req({"title": "Long", "prompt": "x" * 1500})
        )
        chat_id = json.loads(resp.body)["id"]
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(len(chat["goal"]), 1000)

    async def test_whitespace_prompt_no_goal(self):
        resp = await chat_routes.handle_chat_create(
            self._create_req({"title": "WSP", "prompt": "   "})
        )
        chat_id = json.loads(resp.body)["id"]
        chat = await db.chat_get(chat_id, "admin")
        self.assertIsNone(chat.get("goal"))


# ── Chat list: goal present ─────────────────────────────────────────────────


class ChatListGoalTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.sess = {"user": "admin", "role": "admin"}

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _list_req(self):
        r = _make_request(method="GET", path="/api/chats", cookies={"wc_session": self.sid})
        r.state.session = self.sess
        return r

    async def test_goal_in_list_response(self):
        chat_id = "list-goal"
        wd = f"{self.tmpdir.name}/projects/{chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "List Chat", "A description", wd, "admin")
        await db.chat_update(chat_id, "admin", goal="Goal from list")

        resp = await chat_routes.handle_chats_list(self._list_req())
        matching = [c for c in json.loads(resp.body)["chats"] if c["id"] == chat_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["goal"], "Goal from list")

    async def test_null_goal_in_list(self):
        chat_id = "list-no-goal"
        wd = f"{self.tmpdir.name}/projects/{chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "No Goal List", None, wd, "admin")

        resp = await chat_routes.handle_chats_list(self._list_req())
        matching = [c for c in json.loads(resp.body)["chats"] if c["id"] == chat_id]
        self.assertEqual(len(matching), 1)
        self.assertIsNone(matching[0].get("goal"))


# ── Chat get: goal present ──────────────────────────────────────────────────


class ChatGetGoalTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.sess = {"user": "admin", "role": "admin"}

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _get_req(self, chat_id):
        r = _make_request(method="GET", path=f"/api/chats/{chat_id}", cookies={"wc_session": self.sid})
        r.state.session = self.sess
        return r

    async def test_goal_in_get_response(self):
        # Use the same route to create the chat so owner_of translation works.
        create_r = _make_request(
            method="POST", path="/api/chats",
            body={"title": "Get Chat", "prompt": "Build a calculator"},
            cookies={"wc_session": self.sid},
        )
        create_r.state.session = self.sess
        resp = await chat_routes.handle_chat_create(create_r)
        chat_id = json.loads(resp.body)["id"]
        resp2 = await chat_routes.handle_chat_get(self._get_req(chat_id), chat_id)
        body = json.loads(resp2.body)
        self.assertIn("goal", body["chat"])
        self.assertEqual(body["chat"]["goal"], "Build a calculator")

    async def test_null_goal_in_get(self):
        chat_id = "get-null"
        wd = f"{self.tmpdir.name}/projects/{chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "Null Chat", None, wd, "admin")
        resp = await chat_routes.handle_chat_get(self._get_req(chat_id), chat_id)
        body = json.loads(resp.body)
        self.assertIsNone(body["chat"].get("goal"))

    async def test_404_on_missing_chat(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_get(self._get_req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)


# ── Chat patch: goal editable ──────────────────────────────────────────────


class ChatPatchGoalTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.sess = {"user": "admin", "role": "admin"}

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _patch_req(self, chat_id, body):
        r = _make_request(
            method="PATCH", path=f"/api/chats/{chat_id}", body=body,
            cookies={"wc_session": self.sid},
        )
        r.state.session = self.sess
        r.headers = {"X-CSRF-Token": "csrf"}
        return r

    async def _create(self, chat_id, extra=None):
        wd = f"{self.tmpdir.name}/projects/{chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "Test", None, wd, "admin")
        if extra:
            for k, v in extra.items():
                await db.db_conn.execute(f"UPDATE chats SET {k} = ? WHERE id = ?", [v, chat_id])
            await db.db_conn.commit()

    async def test_patch_sets_goal(self):
        await self._create("pg1")
        resp = await chat_routes.handle_chat_patch(self._patch_req("pg1", {"goal": "new goal"}), "pg1")
        self.assertEqual(resp.status_code, 200)
        # Verify goal was written to DB (patch response reads pre-update dict).
        chat = await db.chat_get("pg1", "admin")
        self.assertEqual(chat["goal"], "new goal")

    async def test_patch_clears_goal(self):
        await self._create("pg2", {"goal": "has one"})
        resp = await chat_routes.handle_chat_patch(self._patch_req("pg2", {"goal": None}), "pg2")
        # Verify goal was cleared in DB.
        chat = await db.chat_get("pg2", "admin")
        self.assertIsNone(chat["goal"])

    async def test_patch_empty_string(self):
        await self._create("pg3")
        resp = await chat_routes.handle_chat_patch(self._patch_req("pg3", {"goal": ""}), "pg3")
        chat = await db.chat_get("pg3", "admin")
        self.assertEqual(chat["goal"], "")

    async def test_patch_truncated_to_1000(self):
        await self._create("pg4")
        resp = await chat_routes.handle_chat_patch(self._patch_req("pg4", {"goal": "x" * 2000}), "pg4")
        chat = await db.chat_get("pg4", "admin")
        self.assertEqual(len(chat["goal"]), 1000)

    async def test_patch_rejects_non_string(self):
        await self._create("pg5")
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_patch(self._patch_req("pg5", {"goal": 12345}), "pg5")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_goal_in_allowed_fields(self):
        await self._create("pg6")
        resp = await chat_routes.handle_chat_patch(self._patch_req("pg6", {"goal": "ok"}), "pg6")
        self.assertEqual(resp.status_code, 200)

    async def test_patch_404_on_missing(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_patch(self._patch_req("nonexistent", {"goal": "x"}), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)


# ── Goal isolation ──────────────────────────────────────────────────────────


class GoalIsolationTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.admin_sid, _ = auth.session_new("admin")
        self.bob_sid, _ = auth.session_new("bob")

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _bob_req(self):
        r = _make_request(method="GET", path="/api/chats", cookies={"wc_session": self.bob_sid})
        r.state.session = {"user": "bob", "role": "user"}
        return r

    async def test_bob_cannot_see_admin_goal(self):
        chat_id = "isolation-admin"
        wd = f"{self.tmpdir.name}/projects/{chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "Admin Chat", "Admin desc", wd, "admin")
        await db.chat_update(chat_id, "admin", goal="Secret goal")
        resp = await chat_routes.handle_chats_list(self._bob_req())
        chats = [c for c in json.loads(resp.body)["chats"] if c["id"] == chat_id]
        self.assertEqual(len(chats), 0)


# ── Hard refresh ────────────────────────────────────────────────────────────


class HardRefreshTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, to_path="/"):
        return _make_request(method="GET", path=f"/api/hard-refresh?to={to_path}")

    async def test_returns_302(self):
        resp = await misc_routes._api_hard_refresh(self._req("/path"))
        self.assertEqual(resp.status_code, 302)

    async def test_redirects_to_to_param(self):
        r = _make_request(method="GET", path="/api/hard-refresh", query={"to": "/target"})
        resp = await misc_routes._api_hard_refresh(r)
        self.assertEqual(resp.headers["location"], "/target")

    async def test_no_cache_headers(self):
        cc = (await misc_routes._api_hard_refresh(self._req())).headers["cache-control"]
        self.assertIn("no-cache", cc)
        self.assertIn("no-store", cc)
        self.assertIn("must-revalidate", cc)

    async def test_no_session_required(self):
        resp = await misc_routes._api_hard_refresh(_make_request(method="GET", path="/api/hard-refresh?to=/"))
        self.assertEqual(resp.status_code, 302)

    async def test_default_target_root(self):
        resp = await misc_routes._api_hard_refresh(self._req("/"))
        self.assertEqual(resp.headers["location"], "/")


# ── Auth middleware exempts hard-refresh ────────────────────────────────────


class HardRefreshAuthExemptTests(unittest.TestCase):

    def test_exempted_in_middleware(self):
        source = inspect.getsource(middleware.AuthMiddleware.dispatch)
        self.assertIn("/api/hard-refresh", source)


# ── HTML / CSS ──────────────────────────────────────────────────────────────


class WorkspaceGoalBarTests(unittest.TestCase):

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "web"
        self.html = (base / "index.html").read_text()
        self.css = (base / "assets" / "styles.css").read_text()

    def test_workspace_goal_bar_exists(self):
        self.assertIn('id="workspaceGoal"', self.html)

    def test_goal_icon_present(self):
        self.assertIn("goal-icon", self.html)

    def test_goal_bar_hidden_by_default(self):
        chunk = self.html[self.html.find('id="workspaceGoal"'):self.html.find('id="workspaceGoal"') + 100]
        self.assertIn("hidden", chunk)

    def test_css_rule_exists(self):
        self.assertIn(".workspace-goal{", self.css)

    def test_mobile_media_query_has_flex(self):
        start = self.css.find("@media(max-width:620px)")
        section = self.css[start:start + 1500]
        self.assertIn("display:flex", section)


if __name__ == "__main__":
    unittest.main()
