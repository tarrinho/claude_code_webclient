"""QA: a session id names a conversation, a process names a session.

`_dedupe_sessions` collapsed records by `sessionId`. That is wrong, and the way
it is wrong is invisible until two terminals share a conversation:

* `claude --resume <id>` attaches a NEW process to an EXISTING conversation.
  So does entering a worktree, and `runner.py:808` passes `--resume` for every
  WebConsole turn.
* Renaming a session keeps its id -- one record on this host carries three
  `formerNames`, all with the same `sessionId`.

Measured 2026-09-18: `multiagent3 - testusage` (pid 3659449) and
`multiagent - benchmark plan` (pid 3763786) both served `599ee395-...`. The old
dedupe returned one of them, so the sessions list showed one row for two live
terminals, and the standby endpoint -- which SIGTERMs whatever it resolves --
aimed at a session nobody had named.

The collapse the function was actually written for, a CLI record and the
WebConsole shadow of the same conversation, still happens. These tests pin both
halves, because a fix that returned every record would "pass" the first half
while undoing the second.
"""
from __future__ import annotations

import unittest

from routes.db_sessions import _dedupe_sessions, _session_proc_key

DOMAIN = "linux:d8dcb62ef84e4205912f4c6e3fc695a5:pid:[4026531836]"


def _cli(pid: int, session_id: str, name: str, proc_start: int = 100, **over):
    record = {
        "entrypoint": "cli", "kind": "interactive", "pid": pid,
        "procStart": proc_start, "pidDomain": DOMAIN,
        "sessionId": session_id, "name": name, "live": True,
    }
    record.update(over)
    return record


def _shadow(session_id: str, server_pid: int = 999, **over):
    """What WebConsole writes: the SERVER's pid, not the session's."""
    record = {
        "entrypoint": "webconsole", "pid": server_pid, "procStart": 1,
        "pidDomain": DOMAIN, "sessionId": session_id, "name": session_id,
    }
    record.update(over)
    return record


class ProcKeyTests(unittest.TestCase):

    def test_a_cli_record_is_keyed_by_its_process(self):
        self.assertEqual(
            _session_proc_key(_cli(11, "sid-a", "one", proc_start=42)),
            (DOMAIN, "11", "42"))

    def test_a_webconsole_shadow_has_no_process_key(self):
        """Its pid is the server's, identical for every session it ever served.
        Keying on it would collapse unrelated conversations into one row."""
        self.assertIsNone(_session_proc_key(_shadow("sid-a")))

    def test_a_record_without_procstart_has_no_process_key(self):
        """procStart is what survives pid reuse. Without it the triple is not
        an identity, so the session id remains the only honest key."""
        rec = _cli(11, "sid-a", "one")
        del rec["procStart"]
        self.assertIsNone(_session_proc_key(rec))


class DedupeTests(unittest.TestCase):

    def test_two_live_processes_on_one_conversation_both_survive(self):
        """The defect, in its measured shape."""
        out = _dedupe_sessions([
            _cli(3659449, "599ee395", "multiagent3 - testusage"),
            _cli(3763786, "599ee395", "multiagent - benchmark plan", proc_start=200),
        ])
        self.assertEqual(len(out), 2, "a session id is not a process")
        self.assertEqual(
            {r["name"] for r in out},
            {"multiagent3 - testusage", "multiagent - benchmark plan"})

    def test_the_same_process_reported_twice_is_still_collapsed(self):
        """Same pid and start tick: one process, listed once."""
        out = _dedupe_sessions([
            _cli(11, "sid-a", "one"),
            _cli(11, "sid-a", "one"),
        ])
        self.assertEqual(len(out), 1)

    def test_a_shadow_is_dropped_when_the_real_process_is_present(self):
        """The collapse this function was written for. A fix that simply
        returned everything would break this."""
        out = _dedupe_sessions([_cli(11, "sid-a", "one"), _shadow("sid-a")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["entrypoint"], "cli")

    def test_a_shadow_survives_alone_when_no_process_record_exists(self):
        """A finished conversation still has to be listable."""
        out = _dedupe_sessions([_shadow("sid-gone")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sessionId"], "sid-gone")

    def test_shadows_for_different_conversations_are_not_merged(self):
        """They share the server's pid; only the session id tells them apart."""
        out = _dedupe_sessions([_shadow("sid-a"), _shadow("sid-b")])
        self.assertEqual(len(out), 2)

    def test_a_shadow_for_another_conversation_survives_beside_a_process(self):
        out = _dedupe_sessions([_cli(11, "sid-a", "one"), _shadow("sid-b")])
        self.assertEqual({r["sessionId"] for r in out}, {"sid-a", "sid-b"})

    def test_a_record_with_no_session_id_is_kept(self):
        """Unkeyed records passed through before and still must: dropping a row
        because it is malformed hides a session that exists."""
        out = _dedupe_sessions([{"entrypoint": "other", "name": "odd"}])
        self.assertEqual(len(out), 1)

    def test_one_pid_in_two_namespaces_is_two_sessions(self):
        """pidDomain separates namespaces -- a container's pid 42 and the
        host's pid 42 are different processes."""
        out = _dedupe_sessions([
            _cli(42, "sid-a", "host"),
            _cli(42, "sid-b", "container", pidDomain="linux:other:pid:[4026531999]"),
        ])
        self.assertEqual(len(out), 2)

    def test_a_reused_pid_with_a_new_start_tick_is_a_new_session(self):
        """Without procStart in the key, a recycled pid would silently shadow
        the session that held it before."""
        out = _dedupe_sessions([
            _cli(11, "sid-old", "old", proc_start=100),
            _cli(11, "sid-new", "new", proc_start=900),
        ])
        self.assertEqual(len(out), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
