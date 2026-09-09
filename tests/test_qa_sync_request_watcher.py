"""QA: sync_request_watcher.py -- the marker match, and the two ways it
must not misfire.

Design: docs/superpowers/specs/2026-09-09-transport-project-sync-design.md.
auto_answer.py's own docstring states this codebase's rule against
heuristic text matching; these pin the same discipline for a trigger that
ends in a file-writing operation queued for a human to approve.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
import sync_request_watcher as watcher


class MarkerRegexTests(unittest.TestCase):
    def test_matches_the_literal_prefix_followed_by_a_name(self):
        m = watcher._MARKER_RE.match("SYNC_REQUEST Kali3")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "Kali3")

    def test_must_be_a_prefix_not_merely_present(self):
        """A message that only mentions the phrase mid-sentence must not
        match -- otherwise ordinary conversation about sync requests would
        queue one."""
        self.assertIsNone(
            watcher._MARKER_RE.match("can you send a SYNC_REQUEST Kali3 please"))

    def test_case_sensitive(self):
        """Deliberately case-sensitive: this is a literal marker, not
        natural language, so there is no "did they mean..." to guess at."""
        self.assertIsNone(watcher._MARKER_RE.match("sync_request Kali3"))

    def test_requires_a_transport_reference(self):
        self.assertIsNone(watcher._MARKER_RE.match("SYNC_REQUEST"))
        self.assertIsNone(watcher._MARKER_RE.match("SYNC_REQUEST   "))

    def test_extra_words_after_the_reference_are_ignored(self):
        """Only the first token after the marker is the transport
        reference -- anything past it (a reason, a note) is not parsed as
        part of the name, so a chatty message doesn't fail to match."""
        m = watcher._MARKER_RE.match("SYNC_REQUEST Kali3 -- my checkout is stale")
        self.assertEqual(m.group(1), "Kali3")


class PassTests(unittest.IsolatedAsyncioTestCase):
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

        await db.ssh_transport_create(
            "t1", "Kali3", "admin", "kali-3.example.net", "kali", "~/.ssh/id_ed25519")

    async def _run_pass(self, events):
        fake_traffic = AsyncMock(return_value=events)
        with patch("transcripts.agent_traffic", fake_traffic):
            await watcher._pass()

    async def test_a_matching_message_queues_a_pending_request(self):
        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST Kali3"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["transport_id"], "t1")
        self.assertEqual(pending[0]["requested_by"], "cweb-remote")

    async def test_resolves_by_transport_id_too(self):
        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST t1"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(len(pending), 1)

    async def test_matching_is_case_insensitive_for_the_transport_name(self):
        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST kali3"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(len(pending), 1)

    async def test_outgoing_messages_are_ignored(self):
        """Only incoming messages can trigger this -- an outgoing SendMessage
        that happens to quote the marker (e.g. echoing it back) must not."""
        await self._run_pass([
            {"direction": "out", "sender": "us", "text": "SYNC_REQUEST Kali3"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(pending, [])

    async def test_unresolvable_transport_reference_creates_nothing(self):
        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST NoSuchHost"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(pending, [])

    async def test_a_second_matching_message_does_not_duplicate_the_request(self):
        """One pending request per transport at a time -- two different
        senders (or the same one, retried) asking to sync the same
        transport collapse to one approval, not two."""
        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST Kali3"},
            {"direction": "in", "sender": "cweb-other", "text": "SYNC_REQUEST Kali3"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(len(pending), 1)

    async def test_a_resolved_request_can_be_re_requested(self):
        """The dedup must be against *pending* requests, not "ever
        created" -- otherwise a legitimate re-request after the first sync
        completed would be silently swallowed forever."""
        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST Kali3"},
        ])
        first = (await db.sync_request_list_pending("admin"))[0]
        await db.sync_request_resolve(first["id"], "done", files_changed=1)

        await self._run_pass([
            {"direction": "in", "sender": "cweb-remote", "text": "SYNC_REQUEST Kali3"},
        ])
        pending = await db.sync_request_list_pending("admin")
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
