"""QA: auto-answer is no longer refused on a voice conversation.

Pedro hit this as a 400 on every turn in a voice chat:

    HTTP 400 path=/api/chats/1815edbaa.../stream
    detail=auto-answer cannot be used with voice conversations

Three gates in routes/chats.py enforced it -- the stream handler, the PUT
handler, and a GET handler that reported `enabled: False, voice_mode_blocked:
True` no matter what was stored. That last one is what made it hard to
diagnose: the knob rendered as OFF while being the thing blocking every turn,
and no file under web/assets reads `voice_mode_blocked`, so the cause was
invisible from the browser.

**The trap.** Arming auto-answer on a normal chat and then switching it to
voice mode was reachable and bricked the chat. The voice_mode branch of the
PATCH handler pins the voice backend and model but never cleared `auto_answer`,
and the PUT gate only stops you arming it on a chat that is *already* voice.
Measured on the production database before this change: 2 of 7 voice chats had
`voice_mode=1 AND auto_answer=1` and could not take a single turn --
`594d486cd1` and `1815edbaa1`, the latter being the id in the log line above.

So this removes the gates and clears `auto_answer` when voice mode is switched
on, which is what stops the trap recurring. Removing the gates is enough to
un-brick the two existing chats without touching their stored data.

**What this does NOT do, deliberately.** The knob is inert on a voice chat.
Auto-answer answers Claude Code CLI permission prompts, read by `auto_answer.py`
through `prompts.read_prompt` (GNU screen hardcopy) and the CLI's transcript
JSONL. A voice turn never spawns the CLI -- `routes/voice.py` calls
`AsyncOpenAI(...).chat.completions.create(...)` directly, so there is no screen
session, no transcript, no tool use and no prompt to answer. Pedro was told
that before approving. Storing the flag is therefore harmless rather than
useful, and the point of the change is that a stale flag must not break voice.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class _Fixture(unittest.IsolatedAsyncioTestCase):
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
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))
        await db.chat_create("voice", "Voice chat", None, f"{self.tmp.name}/p", "alice")
        await db.chat_create("plain", "Plain chat", None, f"{self.tmp.name}/p", "alice")
        await db.chat_update("voice", "alice", voice_mode=1)

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password})
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}


class ArmingItOnAVoiceChatTests(_Fixture):
    async def test_the_put_handler_accepts_it(self):
        """Previously 400 "auto-answer is not allowed for voice conversations"."""
        client, headers = self._login()
        r = client.put("/api/chats/voice/auto-answer", json={"enabled": True},
                       headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["enabled"])

    async def test_the_get_handler_reports_what_is_stored(self):
        """The gate that made this undiagnosable. It returned a hardcoded
        `enabled: False` for any voice chat, so the UI showed the knob off
        while a stored `true` was rejecting every turn."""
        client, headers = self._login()
        await db.chat_auto_answer_set("voice", "alice", True)
        r = client.get("/api/chats/voice/auto-answer", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            r.json()["enabled"],
            "a voice chat must report its real stored value, not a forced False")

    async def test_it_no_longer_claims_to_be_blocked(self):
        client, headers = self._login()
        r = client.get("/api/chats/voice/auto-answer", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("voice_mode_blocked", r.json())

    async def test_a_plain_chat_is_unaffected(self):
        """The change must not alter the non-voice path it shares."""
        client, headers = self._login()
        r = client.put("/api/chats/plain/auto-answer", json={"enabled": True},
                       headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        r = client.get("/api/chats/plain/auto-answer", headers=headers)
        self.assertTrue(r.json()["enabled"])


class TurningOnVoiceModeClearsItTests(_Fixture):
    """The trap, fixed at source.

    Without this, converting an armed chat to voice recreates exactly the state
    that bricked two of Pedro's chats -- and it is the only way to reach it,
    since the PUT gate never stopped this ordering.
    """

    async def test_enabling_voice_mode_disarms_auto_answer(self):
        await db.chat_auto_answer_set("plain", "alice", True)
        self.assertTrue(await db.chat_auto_answer_get("plain", "alice"))
        client, headers = self._login()
        r = client.patch("/api/chats/plain", json={"voice_mode": True},
                         headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(
            await db.chat_auto_answer_get("plain", "alice"),
            "converting an armed chat to voice must disarm it, or the flag is "
            "carried into a mode where it does nothing")

    async def test_disabling_voice_mode_does_not_arm_anything(self):
        """Only the enabling direction clears. Turning voice off must not
        invent a value -- a chat that was never armed stays unarmed."""
        client, headers = self._login()
        r = client.patch("/api/chats/voice", json={"voice_mode": False},
                         headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(await db.chat_auto_answer_get("voice", "alice"))

    async def test_the_accept_recommended_flag_is_cleared_too(self):
        """Both halves of the knob, or the second one survives into voice mode
        and re-arms as soon as the chat is switched back."""
        await db.chat_auto_answer_set("plain", "alice", True, True)
        client, headers = self._login()
        client.patch("/api/chats/plain", json={"voice_mode": True}, headers=headers)
        self.assertFalse(
            await db.chat_auto_answer_recommend_get("plain", "alice"))


class TheStreamNoLongerRefusesTests(_Fixture):
    """The 400 Pedro actually saw.

    The turn is not driven to completion here -- that needs a backend. What
    matters is that the request is not rejected before the stream opens, which
    is where the gate was.
    """

    async def test_a_voice_turn_with_auto_answer_armed_is_not_a_400(self):
        client, headers = self._login()
        await db.chat_auto_answer_set("voice", "alice", True)
        r = client.post("/api/chats/voice/stream", json={"content": "hello"},
                        headers=headers)
        self.assertNotEqual(
            r.status_code, 400,
            "the stream gate is still refusing an armed voice chat")
        if r.status_code == 400:      # pragma: no cover - message on failure
            self.assertNotIn("auto-answer", r.text)

    async def test_the_refusal_is_gone_from_the_code(self):
        """Belt and braces: the stream assertion above passes for any non-400,
        including a 500 from an unrelated fault. This pins the actual removal.

        Comment lines are stripped first. The removal is documented in comments
        that necessarily quote what was removed, and a scan of the raw file
        matches its own explanation -- which is a test that can only be made to
        pass by deleting the reasoning, exactly the wrong incentive.
        """
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "routes" / "chats.py"
                  ).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#"))
        for gone in ("auto-answer cannot be used with voice",
                     "auto-answer is not allowed for voice",
                     '"voice_mode_blocked"'):
            self.assertNotIn(gone, code, f"still live in routes/chats.py: {gone}")


if __name__ == "__main__":
    unittest.main()
