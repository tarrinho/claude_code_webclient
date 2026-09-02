"""LIVE: switch backends inside one conversation, both directions.

This is the scenario that broke five terminal sessions on 2026-09-01. A session
holds one transcript, and the transcript is replayed on every later turn. Move
that session to a different provider and the replay has to survive blocks the
first provider wrote and the second one refuses.

Two turns per test, same chat, same session id, different machine:

  * `test_gateway_then_anthropic` -- start on the gateway, finish on Anthropic
  * `test_anthropic_then_gateway` -- the reverse

Both directions on purpose. They are not symmetrical: only one of them puts a
strict API in front of a permissive gateway's output, and that is the direction
that fails.

The turns go through `chat_routes._prepare_transcript_for_backend` first, exactly as a
real turn does, so this measures the guard the product actually has rather than
a guard the test supplies for it.

Opt-in, like the other live tests -- real tokens, real endpoints:

    WC_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_live_backend_switch.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import config
import db
import runner
import transcripts
from routes import chats as chat_routes

# The suite has no convention for importing one test module from another, and
# the alternatives are worse: a tests/__init__.py would change collection
# semantics for two thousand tests, and copying the machine lookup would give
# the two live files separate ideas of which machines exist. Scoped to this
# file, so nothing else inherits it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_live_backends import (
    DIRECT,
    GATEWAY,
    LIVE,
    _machine,
    _proxy_token,
)

FIRST = "Reply with exactly one word: alpha"
SECOND = "Reply with exactly one word: beta"


def _strict_api_would_refuse(session_id: str) -> list[str]:
    """Blocks the Anthropic API rejects, by the same rule the API applies.

    Two kinds:

    * empty text -- `{"type":"text","text":""}`. Found by a byte marker.
    * foreign thinking -- a `thinking` block whose `signature` is absent or
      blank. A signature is provider-specific; a block produced elsewhere cannot
      be replayed and the API answers `400 ... each thinking block must contain
      non-whitespace thinking`.

    When this test was written the product handled only the first. The repair
    ran on a switched conversation, removed the empty text, logged success, and
    left the thinking blocks -- so the check below failed in both directions
    while the turns themselves still passed. That is the shape worth keeping in
    mind: the conversation worked, and was accumulating the state that makes
    every later turn fail at once.

    Native thinking is deliberately not counted: a signed block is the
    provider's own and replays correctly.
    """
    path = transcripts.transcript_path(session_id)
    if path is None:
        return []
    problems: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "thinking" and not str(block.get("signature") or "").strip():
                problems.append("foreign thinking")
            elif kind == "text" and not str(block.get("text") or "").strip():
                problems.append("empty text")
    return problems


@unittest.skipUnless(LIVE, "live backend test; set WC_LIVE_TESTS=1 to run")
class BackendSwitchTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        token = _proxy_token()
        if not token:
            self.skipTest("no proxy token available")
        self.tmp = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(config, "DB_PATH", f"{self.tmp.name}/switch.db"),
            patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"),
            patch.object(config, "PROXY_TOKEN", token),
        ]
        for p in self._patches:
            p.start()
        Path(config.PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
        await db.init()

        self.machines = {}
        for label in (GATEWAY, DIRECT):
            row = _machine(label)
            if row is None:
                self.skipTest(f"no machine named {label!r} is configured")
            self.machines[label] = row
        self.owner = self.machines[GATEWAY]["owner_id"] or "admin"
        for row in self.machines.values():
            await db.ai_machine_create(
                row["id"], row["name"], "", 0, row["api_key"], row["model"],
                row["base_url"], "", self.owner, provider=row["provider"],
            )

        self.chat_id = f"switch-{uuid.uuid4().hex[:8]}"
        work_dir = Path(config.PROJECTS_ROOT) / self.chat_id
        work_dir.mkdir(parents=True, exist_ok=True)
        await db.chat_create(self.chat_id, "backend switch", None,
                             str(work_dir), self.owner)

    async def asyncTearDown(self):
        await db.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def _turn(self, machine_label: str, prompt: str) -> tuple[str, list[dict]]:
        """Pin the chat to a machine and run one real turn on it."""
        row = self.machines[machine_label]
        await db.chat_update(self.chat_id, self.owner, ai_machine_id=row["id"])
        chat = await db.chat_get(self.chat_id, self.owner)
        # The app does this before every turn; without it this test would be
        # measuring a guard the product does not actually run.
        await chat_routes._prepare_transcript_for_backend(chat)

        text, events = [], []
        try:
            async with asyncio.timeout(config.TURN_TIMEOUT_S):
                async for event in runner.stream_turn(
                    prompt, chat.get("session_id"), chat["work_dir"],
                    self.chat_id, row["model"],
                ):
                    events.append(event)
                    if event.get("type") == "text" and event.get("content"):
                        text.append(str(event["content"]))
                    elif event.get("type") == "session_id":
                        await db.chat_set_session(self.chat_id, event["session_id"])
        except TimeoutError:
            self.fail(f"{machine_label}: no answer within {config.TURN_TIMEOUT_S}s")
        return "".join(text), events

    def _answered(self, label: str, word: str, text: str, events: list[dict]):
        errors = [e.get("message") for e in events if e.get("type") == "error"]
        self.assertFalse(errors, f"{label} returned an error: {errors[:2]}")
        self.assertIn(word, text.lower(),
                      f"{label}: asked for {word!r}, got {text.strip()[:200]!r}")

    async def _switch(self, first: str, second: str):
        text, events = await self._turn(first, FIRST)
        self._answered(first, "alpha", text, events)

        chat = await db.chat_get(self.chat_id, self.owner)
        session_id = chat.get("session_id")
        self.assertTrue(session_id, "first turn recorded no session to resume")

        text, events = await self._turn(second, SECOND)
        self._answered(second, "beta", text, events)
        return session_id

    async def test_gateway_then_anthropic(self):
        """Gateway first, then Anthropic replays what the gateway wrote."""
        session_id = await self._switch(GATEWAY, DIRECT)
        self.assertEqual(
            _strict_api_would_refuse(session_id), [],
            "the transcript still holds blocks a strict API refuses",
        )

    async def test_anthropic_then_gateway(self):
        """The reverse: a permissive backend replaying Anthropic's output.

        No transcript assertion here, and the absence is the point. The repair
        runs *before* a turn, not after, so in this direction the gateway writes
        its empty text and unsigned thinking in the final turn and nothing has
        run since. Asserting cleanliness here failed, and the failure was mine:
        it demanded the product clean state it has not yet had a reason to look
        at. The contract is that a strict backend never *replays* poison, which
        is what test_gateway_then_anthropic and the round trip below assert.

        What this direction is worth testing is that it works at all: Anthropic
        writes signed thinking, and the gateway has to accept its own replay.
        """
        await self._switch(DIRECT, GATEWAY)

    async def test_a_third_turn_still_works_after_switching_back(self):
        """One switch can be survived by luck; a round trip is the real test.

        Poison is permanent once written -- every later turn replays it -- so a
        conversation that survives A->B->A is the claim that actually matters.
        """
        await self._switch(DIRECT, GATEWAY)
        # Back to the strict backend, which must replay everything the gateway
        # just wrote. This is where the repair has to have done its job.
        chat = await db.chat_get(self.chat_id, self.owner)
        session_id = chat["session_id"]
        self.assertTrue(
            _strict_api_would_refuse(session_id),
            "the gateway wrote nothing a strict API would refuse, so this "
            "round trip proves nothing -- check the gateway still emits it",
        )
        text, events = await self._turn(DIRECT, "Reply with exactly one word: gamma")
        self._answered(DIRECT, "gamma", text, events)
        self.assertEqual(
            _strict_api_would_refuse(session_id), [],
            "the repair did not clean the gateway's output before the replay",
        )


if __name__ == "__main__":
    unittest.main()
