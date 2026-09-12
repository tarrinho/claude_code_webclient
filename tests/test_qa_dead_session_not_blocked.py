"""QA: a session whose process is gone cannot still be asking for a person.

`_cli_maps` builds the status map every poll from `db.read_claude_sessions()`,
which deliberately returns *ended* sessions too -- the session list is how you
reopen a finished conversation, so filtering them at the source would break
resume. Each entry carries `live`, set from `_pid_is_running` in
routes/db_sessions.py:651, and the sidebar already reads it
(`chat-list.js:557`, `session.live === false` renders as ended).

`_cli_maps` did not. It read `status` verbatim for every entry, so a session
that died while its last written status was `waiting` stayed in the blocked
list for ever: the console keeps reporting that a person is needed by a process
that no longer exists, and there is no way to satisfy it, because the thing
that would clear the status is gone. It also spends a `prompts.has_prompt`
subprocess on that dead pid on every poll, which is the cost the busy/idle
fast path exists to avoid.

Not reachable through standby since 598e7c4 -- that gate now refuses `waiting`
outright -- but a crash or a `kill -9` reaches it, and those are exactly the
cases where the session file keeps whatever it last held.

A dead session's status is treated as unknown rather than the entry being
dropped, because unknown is what it is: the empty string is already the
"absent status" case every consumer handles via the `if cli_status` guard.

The third test is the one that keeps the fix from over-reaching. Remote
sessions arrive from `_read_remote_sessions_sync` and carry no `live` key at
all, since a remote pid cannot be checked with a local `_pid_is_running`. Only
an explicit False may suppress a status; a missing key must not, or every
remote session silently stops being able to ask for help.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import app
import classification
import prompts


class DeadSessionNotBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def _maps(self, sessions):
        asked: list[str] = []
        with (
            patch.object(app.db, "read_claude_sessions",
                         AsyncMock(return_value=sessions)),
            patch.object(prompts, "has_prompt",
                         lambda sid: asked.append(sid) or True),
        ):
            status, _dismiss, _updated, prompting = await classification._cli_maps({})
        return status, prompting, asked

    async def test_a_dead_session_is_not_reported_as_waiting(self):
        """The defect: a status written before the process died outlives it."""
        status, prompting, asked = await self._maps([
            {"sessionId": "gone-1", "status": "waiting",
             "status_updated_at": "t", "live": False},
        ])
        self.assertEqual(status["gone-1"], "",
                         "a dead session's last status was taken as current")
        self.assertNotIn("gone-1", prompting)
        self.assertEqual(asked, [], "spent a subprocess on a dead pid")

    async def test_a_live_session_still_asks_for_a_person(self):
        """The behaviour being protected, not removed."""
        status, prompting, asked = await self._maps([
            {"sessionId": "here-1", "status": "waiting",
             "status_updated_at": "t", "live": True},
        ])
        self.assertEqual(status["here-1"], "waiting")
        self.assertEqual(asked, ["here-1"])
        self.assertTrue(prompting["here-1"])

    async def test_a_session_with_no_live_key_is_left_alone(self):
        """Remote sessions have no `live`. Unknown liveness is not death --
        suppressing those would silently stop every remote session from being
        able to ask for anything."""
        status, prompting, asked = await self._maps([
            {"sessionId": "remote-1", "status": "waiting", "status_updated_at": "t"},
        ])
        self.assertEqual(status["remote-1"], "waiting")
        self.assertEqual(asked, ["remote-1"])
        self.assertTrue(prompting["remote-1"])

    async def test_a_dead_busy_session_is_also_cleared(self):
        """Not only the blocked states. A dead session reported as `busy`
        renders as working for ever, which is the same lie in a calmer tone."""
        status, _prompting, _asked = await self._maps([
            {"sessionId": "gone-2", "status": "busy",
             "status_updated_at": "t", "live": False},
        ])
        self.assertEqual(status["gone-2"], "")


if __name__ == "__main__":
    unittest.main()
