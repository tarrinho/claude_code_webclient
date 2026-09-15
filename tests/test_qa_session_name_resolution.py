"""QA: `_find_session_name` must return a name the standby script can match.

The bug this covers, measured on this host 2026-09-15: clicking Standby on a
chat returned

    Standby script failed: no running session found matching
    'api.anthropic.com : 39 : Status' (checked pid and name)

while the session was running the whole time, under the name `multi-agent`.

`~/.claude/sessions` holds two kinds of file that both carry a `sessionId`:

* `<pid>.json` -- describes a process. Its `name` is what
  `bin/wc-session-standby.sh` matches on.
* `<uuid>.json` -- a display label like `'api.anthropic.com : 39 : Status'`.
  The script skips these; it refuses every non-numeric basename because they
  name no signalable process.

The resolver returned the first `glob` hit with no preference between them, so
when the filesystem yielded the uuid file first the route handed the script a
value it is structurally incapable of matching. Three files claimed one
sessionId on this host -- two live processes and one stale uuid record naming a
pid that had exited -- and glob returned the stale one.

It looked intermittent for a reason worth keeping in a test: a chat whose label
happened to equal a real session name (`cweb4`) matched and worked, so the
failure depended on the chat rather than on the code.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import routes.chats as chats


def _a_dead_pid() -> int:
    """A pid with no live process, or skip -- liveness is the point here."""
    for candidate in range(4_000_000, 4_000_200):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise unittest.SkipTest("no free pid found to stand in for a dead process")


class FindSessionNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        self.home_patch = patch.object(Path, "home", staticmethod(lambda: self.home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def _write(self, basename: str, **fields):
        p = self.home / ".claude" / "sessions" / f"{basename}.json"
        p.write_text(json.dumps(fields), encoding="utf-8")

    def test_prefers_the_live_process_over_a_uuid_label(self):
        """The exact shape of the reported failure, with the order forced.

        The uuid file is returned to the resolver *first*, which is what
        happened on this host. Left to the filesystem the order is incidental
        -- in a fresh temp directory glob yields the pid file first and the old
        code passed by luck, so asserting without controlling the order would
        be a test that agrees with whichever implementation it is given.
        """
        sid = "599ee395-f8f6-46bd-8b63-cbd3d7cc213c"
        self._write(sid, sessionId=sid, name="api.anthropic.com : 39 : Status",
                    pid=782802, updatedAt=1789459652666)
        self._write(str(os.getpid()), sessionId=sid, name="multi-agent",
                    pid=os.getpid(), updatedAt=1789495645000)

        sessions = self.home / ".claude" / "sessions"
        uuid_first = [str(sessions / f"{sid}.json"), str(sessions / f"{os.getpid()}.json")]
        with patch("glob.glob", return_value=uuid_first):
            self.assertEqual(chats._find_session_name(sid), "multi-agent")

    def test_a_uuid_record_alone_resolves_to_nothing(self):
        """Returning its label would send the script after something it skips.

        None becomes a 400 naming the real problem, rather than a 500 from a
        script asked to find a process that was never described.
        """
        sid = "sid-only-a-label"
        self._write(sid, sessionId=sid, name="api.anthropic.com : 39 : Status",
                    pid=782802, updatedAt=1789459652666)
        self.assertIsNone(chats._find_session_name(sid))

    def test_a_dead_pid_file_is_not_offered(self):
        """A pid file outlives its process; its name is equally unmatchable."""
        sid = "sid-stale-pid"
        dead = _a_dead_pid()
        self._write(str(dead), sessionId=sid, name="long-gone",
                    pid=dead, updatedAt=1789495645000)
        self.assertIsNone(chats._find_session_name(sid))

    def test_newest_live_process_wins(self):
        """Two live processes claimed one sessionId on this host, so the
        tiebreak is load-bearing rather than defensive."""
        sid = "sid-two-live"
        self._write(str(os.getpid()), sessionId=sid, name="newer",
                    pid=os.getpid(), updatedAt=2_000_000_000_000)
        self._write(str(os.getppid()), sessionId=sid, name="older",
                    pid=os.getppid(), updatedAt=1_000_000_000_000)
        self.assertEqual(chats._find_session_name(sid), "newer")

    def test_another_session_is_not_returned(self):
        sid = "sid-wanted"
        self._write(str(os.getpid()), sessionId="sid-different", name="someone-else",
                    pid=os.getpid(), updatedAt=1789495645000)
        self.assertIsNone(chats._find_session_name(sid))


if __name__ == "__main__":
    unittest.main()
