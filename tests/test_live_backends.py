"""LIVE: create a chat on each backend and actually ask it a question.

Two turns, end to end, through the real path: `runner.stream_turn` resolves the
chat's pinned machine, sends the turn to the proxy on 127.0.0.1:9000, the proxy
spawns `claude` with `--model` and the machine's credentials in the child's
environment, and the model answers.

  * `test_the_ai_machine_answers`  -- the gateway machine (base_url set, key set)
  * `test_anthropic_answers`       -- the direct machine (api.anthropic.com)

These spend real tokens, so they are opt-in: set WC_LIVE_TESTS=1 to run them.
Without it they skip, and a skip says so rather than passing quietly. They are
also the only tests here that can fail because of somebody else's outage, which
is the other reason they do not run by default -- a red suite should mean the
code is wrong.

    WC_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_live_backends.py -v

Machine settings are copied from the real database at runtime, read-only, and
never printed: the gateway's key is a credential and this file must stay safe to
commit. If a machine is missing the test skips naming which one, so the reason
is actionable without disclosing anything.

The prompt asks for one word so the assertion is about reachability rather than
model quality, and so a turn costs as little as possible.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import config
import db
import runner

ROOT = Path(__file__).resolve().parents[1]
PROD_DB = ROOT / "data" / "webconsole.db"
# launch.sh writes the proxy token here with umask 077 and exports it into the
# service environment. A test process started from a shell has neither, so the
# handshake is refused and the proxy closes the socket -- which surfaces as
# `IncompleteReadError: 0 bytes read`, an unhelpful way to be told "no token".
PROXY_TOKEN_FILE = ROOT / "data" / "proxy_token.txt"
LIVE = os.environ.get("WC_LIVE_TESTS") == "1"


def _proxy_token() -> str | None:
    """The token launch.sh generated, read the same way launch.sh reads it.

    Never logged or asserted on: this is a credential, and the file is 0600 for
    a reason. It is loaded into the environment so config picks it up, and the
    test only ever reports whether one was found.
    """
    if os.environ.get("WC_PROXY_TOKEN"):
        return os.environ["WC_PROXY_TOKEN"]
    try:
        token = PROXY_TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return token or None

# One word back. Long enough to be unambiguous, short enough to be cheap.
PROMPT = "Reply with exactly one word: pong"
EXPECT = "pong"

GATEWAY = "Current AI Machine"
DIRECT = "Anthropic API"


def _machine(name: str) -> dict | None:
    """A machine row from the real database, or None. Read-only, never printed."""
    if not PROD_DB.is_file():
        return None
    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, name, provider, base_url, api_key, model, owner_id "
            "FROM ai_machines WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


@unittest.skipUnless(LIVE, "live backend test; set WC_LIVE_TESTS=1 to run")
class LiveBackendTests(unittest.IsolatedAsyncioTestCase):
    """Each test pins a chat to one machine and asks it a question."""

    async def asyncSetUp(self):
        token = _proxy_token()
        if not token:
            self.skipTest(
                "no proxy token: run with the service environment, or start the "
                f"proxy once so {PROXY_TOKEN_FILE.name} exists"
            )
        self.tmp = tempfile.TemporaryDirectory()
        # A throwaway database. Never the real one: db.init() takes a write
        # transaction, and pointing it at the live file once stopped the running
        # server from writing for 37 minutes.
        self._patches = [
            patch.object(config, "DB_PATH", f"{self.tmp.name}/live.db"),
            patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"),
            patch.object(config, "PROXY_TOKEN", token),
        ]
        for p in self._patches:
            p.start()
        Path(config.PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def _chat_on(self, machine_name: str) -> tuple[str, dict, str]:
        """Create a chat pinned to *machine_name*, copied from the real config."""
        machine = _machine(machine_name)
        if machine is None:
            self.skipTest(f"no machine named {machine_name!r} is configured")

        owner = machine["owner_id"] or "admin"
        await db.ai_machine_create(
            machine["id"], machine["name"], "", 0, machine["api_key"],
            machine["model"], machine["base_url"], "", owner,
            provider=machine["provider"],
        )
        chat_id = f"live-{uuid.uuid4().hex[:8]}"
        work_dir = Path(config.PROJECTS_ROOT) / chat_id
        work_dir.mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, f"live {machine_name}", None,
                             str(work_dir), owner)
        await db.chat_update(chat_id, owner, ai_machine_id=machine["id"])
        return chat_id, machine, owner

    async def _ask(self, chat_id: str, owner: str,
                   model: str | None) -> tuple[str, list[dict]]:
        """Run one real turn and return (text, events)."""
        chat = await db.chat_get(chat_id, owner)
        events: list[dict] = []
        text = []
        try:
            async with asyncio.timeout(config.TURN_TIMEOUT_S):
                async for event in runner.stream_turn(
                    PROMPT, None, chat["work_dir"], chat_id, model
                ):
                    events.append(event)
                    # "content", not "text": the event type is `text` and the
                    # payload key is `content`. Reading event["text"] collected
                    # nothing and the failure read as "the backend said nothing"
                    # when the backend had answered fine.
                    if event.get("type") == "text" and event.get("content"):
                        text.append(str(event["content"]))
        except TimeoutError:
            self.fail(f"no answer within {config.TURN_TIMEOUT_S}s")
        return "".join(text), events

    def _assert_answered(self, text: str, events: list[dict], machine: dict):
        errors = [e for e in events if e.get("type") == "error"]
        self.assertFalse(
            errors,
            f"backend returned an error: {[e.get('message') for e in errors][:2]}",
        )
        self.assertTrue(text.strip(), "the turn produced no text at all")
        self.assertIn(EXPECT, text.lower(),
                      f"asked for {EXPECT!r}, got: {text.strip()[:200]!r}")

    async def test_the_ai_machine_answers(self):
        """The gateway: base_url and key come from the machine record."""
        chat_id, machine, owner = await self._chat_on(GATEWAY)
        text, events = await self._ask(chat_id, owner, machine["model"])
        self._assert_answered(text, events, machine)

    async def test_anthropic_answers(self):
        """The direct machine, which is also the regression this pairs with.

        This machine stores no api_key, so the CLI must fall back to the host's
        own login -- and its base_url is api.anthropic.com, not the gateway.
        Before the ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN leak was fixed in
        claude_proxy._backend_env, a proxy started from a shell pointed at the
        gateway would send this turn there instead, and the only symptom was an
        answer from the wrong model.
        """
        chat_id, machine, owner = await self._chat_on(DIRECT)
        text, events = await self._ask(chat_id, owner, machine["model"])
        self._assert_answered(text, events, machine)

    async def test_the_two_backends_are_actually_different(self):
        """Guards against both tests passing while pointed at the same place.

        Two green turns prove reachability, not routing: if the gateway leaked
        into the direct machine's environment, both would answer and both would
        pass. The models differ, and the CLI reports which one served the turn,
        so the answer to "were these the same backend" is in the events.
        """
        gateway, direct = _machine(GATEWAY), _machine(DIRECT)
        if not gateway or not direct:
            self.skipTest("both machines must be configured to compare them")
        self.assertNotEqual(
            (gateway["base_url"] or "").strip(), (direct["base_url"] or "").strip(),
            "the two machines point at the same endpoint, so nothing is compared",
        )

        # Both machines live in the same throwaway database; they have distinct
        # ids, so one chat pinned to each is enough and no reset is needed.
        chat_g, _, owner_g = await self._chat_on(GATEWAY)
        _, events_g = await self._ask(chat_g, owner_g, gateway["model"])
        models_g = {e.get("model") for e in events_g if e.get("type") == "model"}

        chat_d, _, owner_d = await self._chat_on(DIRECT)
        _, events_d = await self._ask(chat_d, owner_d, direct["model"])
        models_d = {e.get("model") for e in events_d if e.get("type") == "model"}

        # Asserted, not guarded. This read `if models_g and models_d:` and so
        # passed silently whenever neither turn reported a model -- including
        # when both turns failed outright, which is the exact case it exists to
        # catch. A comparison that cannot run is a failure, not a pass.
        self.assertTrue(models_g, "the gateway turn reported no model")
        self.assertTrue(models_d, "the direct turn reported no model")
        self.assertNotEqual(
            models_g, models_d,
            f"both turns were served by the same model {models_g}; the "
            "backends are not isolated",
        )


if __name__ == "__main__":
    unittest.main()
