"""QA: who may read and close another session's pending question.

The three question routes reach into a live terminal -- GET reads what it is
displaying, POST moves the selection and presses Enter, DELETE presses Escape.
Every one of them is therefore worth an access test, and none of them had one:
`tests/test_qa_question_dismiss.py` and `tests/test_qa_prompt_answer.py` both
call the handlers directly, which is the right level for their subject and
skips the entire middleware stack.

These go through the real stack instead, and the ownership case is the one that
matters most. ``db.chat_get(chat_id, owner)`` is what stops one account
answering another account's question, and it is a single argument in a single
call -- exactly the shape that survives a refactor while quietly losing its
meaning. Here it is asserted from the outside, where losing it shows up as a
200 instead of a 404.

A note on the harness, because it decides whether any of this is real. The
session cookie is set ``Secure`` unless ``COOKIE_ALLOW_INSECURE``, and httpx
will not send a Secure cookie to an ``http://`` URL -- so a TestClient on the
default ``http://testserver`` logs in successfully, receives the cookie, and is
then anonymous for every subsequent request. The committed API-level tests in
``tests/test_qa_chats.py`` are in that state: they assert
``status_code in [200, 401]`` and branch on which arrived, so they pass while
authenticating nothing. ``base_url="https://testserver"`` is what makes the
cookie travel, and every test below would go 401 without it -- which is why the
first two assert 401 explicitly rather than "not 200": the difference between
"refused because anonymous" and "refused because the harness is broken" has to
stay visible.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"


def _client():
    """A TestClient that can actually hold a session.

    No ``with`` block on purpose: entering one runs the app's lifespan, which
    starts the host sampler and the background sweeps. This suite wants the
    routing and middleware, not a running server.
    """
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class QuestionAccessTests(unittest.IsolatedAsyncioTestCase):
    """The middleware stack and the ownership check, from outside."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        # Registered here rather than in asyncTearDown, and immediately after
        # init: aiosqlite's connection runs on a non-daemon thread, so a setup
        # that raises after this line would otherwise leave the interpreter
        # hanging at exit instead of reporting the failure. A wrong function
        # name below cost a 120s hang before this was in place.
        self.addAsyncCleanup(db.close)

        # Generated, never written down: a fixture password that is checked
        # against a real hash, so nothing here is a credential for anything.
        self.passwords = {
            "alice": secrets.token_urlsafe(16),
            "bob": secrets.token_urlsafe(16),
        }
        for name, password in self.passwords.items():
            await db.user_create(name, None, auth.hash_password(password))

        work = f"{self.tmp.name}/p"
        # Alice has one conversation linked to a terminal session and one that
        # is not; Bob has one, which Alice must not be able to touch.
        await db.chat_create("a-linked", "Alice linked", None, work, "alice")
        await db.chat_set_session("a-linked", "sess-alice")
        await db.chat_create("a-loose", "Alice unlinked", None, work, "alice")
        await db.chat_create("b-linked", "Bob's", None, work, "bob")
        await db.chat_set_session("b-linked", "sess-bob")

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        csrf = client.cookies.get("wc_csrf")
        self.assertTrue(csrf, "login must issue a CSRF token")
        return client, {"X-CSRF-Token": csrf}

    # ── Anonymous ────────────────────────────────────────────────────────────

    def test_an_anonymous_read_is_refused(self):
        response = _client().get("/api/chats/a-linked/question")
        self.assertEqual(response.status_code, 401)

    def test_an_anonymous_dismissal_is_refused(self):
        """DELETE is newer than the other two and had no coverage here at all.

        401 rather than 404 or 405 is the point: the refusal comes from the
        auth middleware, ahead of routing, so the handler is never entered and
        no keystroke can reach a terminal.
        """
        response = _client().delete("/api/chats/a-linked/question")
        self.assertEqual(response.status_code, 401)

    def test_an_anonymous_answer_is_refused(self):
        response = _client().post("/api/chats/a-linked/question", json={"index": 1})
        self.assertEqual(response.status_code, 401)

    # ── Cross-site ───────────────────────────────────────────────────────────

    def test_a_dismissal_without_the_csrf_header_is_refused(self):
        """A prompt closed by a cross-site request is a session unblocked by
        somebody who is not the user. Asserted through the middleware rather
        than by reading its method set, which is what
        test_qa_question_dismiss.py does -- that spots a narrowed set and this
        spots a route that stops going through the middleware at all."""
        client, _headers = self._login("alice")
        response = client.delete("/api/chats/a-linked/question")
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["error"])

    def test_an_answer_without_the_csrf_header_is_refused(self):
        client, _headers = self._login("alice")
        response = client.post("/api/chats/a-linked/question", json={"index": 1})
        self.assertEqual(response.status_code, 403)

    # ── Ownership ────────────────────────────────────────────────────────────

    def test_one_account_cannot_read_anothers_question(self):
        client, _headers = self._login("alice")
        response = client.get("/api/chats/b-linked/question")
        self.assertEqual(response.status_code, 404)

    def test_one_account_cannot_dismiss_anothers_question(self):
        """The worst of the three if it were missing: Escape lands in a
        terminal the caller has no claim on, and the owner sees a prompt they
        were waiting on close by itself."""
        client, headers = self._login("alice")
        response = client.delete("/api/chats/b-linked/question", headers=headers)
        self.assertEqual(response.status_code, 404)

    def test_one_account_cannot_answer_anothers_question(self):
        client, headers = self._login("alice")
        response = client.post(
            "/api/chats/b-linked/question", json={"index": 1}, headers=headers
        )
        self.assertEqual(response.status_code, 404)

    def test_a_missing_chat_is_a_404_not_a_500(self):
        client, headers = self._login("alice")
        response = client.delete("/api/chats/nope/question", headers=headers)
        self.assertEqual(response.status_code, 404)

    # ── Reaching the handler ─────────────────────────────────────────────────
    #
    # Without these the class could pass with the route unregistered: every
    # assertion above is a refusal, and a 404 from an absent route is
    # indistinguishable from a 404 from the ownership check.

    def test_an_owner_reaches_the_handler_on_an_unlinked_chat(self):
        """400 is the handler speaking: it looked the chat up, found it, and
        found no session behind it."""
        client, headers = self._login("alice")
        response = client.delete("/api/chats/a-loose/question", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not linked", response.json()["error"])

    def test_an_owner_dismissing_a_settled_question_gets_a_success(self):
        """Nothing pending is the state the caller asked for, so it is a 200 --
        and reaching that answer means the request passed auth, CSRF, routing
        and ownership on the way in."""
        client, headers = self._login("alice")
        with patch("transcripts.pending_question", return_value=None):
            response = client.delete("/api/chats/a-linked/question", headers=headers)
        self.assertEqual(response.status_code, 200)
        body: dict[str, Any] = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["already_closed"])

    def test_an_owner_reading_a_settled_question_sees_nothing_pending(self):
        client, _headers = self._login("alice")
        with patch("transcripts.pending_question", return_value=None):
            response = client.get("/api/chats/a-linked/question")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["pending"])


if __name__ == "__main__":
    unittest.main()
