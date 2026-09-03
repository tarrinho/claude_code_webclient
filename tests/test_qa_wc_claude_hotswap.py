"""QA: wc-claude.sh resolves the active backend correctly, with and without
the hot-swap feature engaged.

Task 1 establishes that refactoring the script into reusable functions changes
nothing observable: the same four DB fixtures must resolve to the same
WC_CLAUDE_DRY_RUN report, byte for byte, before and after the refactor. Tasks 2
and 3 build the poller and the supervised loop on top of these functions.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-claude.sh"


def _make_db(path: str, machines: list[dict], default_model: str = "") -> None:
    """A throwaway DB holding only what wc-claude.sh reads: ai_machines and
    settings. Deliberately not the full app schema -- the script only ever
    touches these two tables and these columns."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ai_machines (name TEXT, provider TEXT, base_url TEXT, "
        "api_key TEXT, model TEXT, active INTEGER)"
    )
    con.execute("CREATE TABLE settings (key TEXT, value TEXT)")
    for m in machines:
        con.execute(
            "INSERT INTO ai_machines (name, provider, base_url, api_key, "
            "model, active) VALUES (?, ?, ?, ?, ?, ?)",
            (m["name"], m["provider"], m.get("base_url"), m.get("api_key"),
             m.get("model"), 1 if m.get("active") else 0),
        )
    if default_model:
        con.execute(
            "INSERT INTO settings (key, value) VALUES ('default_model', ?)",
            (default_model,),
        )
    con.commit()
    con.close()


class DryRunResolutionTests(unittest.TestCase):
    """Four fixtures covering every branch query_backend/resolve_backend take.

    Run via WC_CLAUDE_DRY_RUN=1 against a fake `claude` on PATH, so nothing
    ever actually starts a session or spends a token.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake_claude = Path(self.tmp.name) / "bin"
        fake_claude.mkdir()
        (fake_claude / "claude").write_text("#!/bin/sh\necho FAKE-CLAUDE-RAN\n")
        (fake_claude / "claude").chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{fake_claude}:{os.environ.get('PATH', '')}",
            "WC_CLAUDE_DRY_RUN": "1",
        }

    def _run(self, db_path: str | None, *args: str) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if db_path is not None:
            env["WC_DB_PATH"] = db_path
        else:
            env.pop("WC_DB_PATH", None)
        return subprocess.run(
            [str(SCRIPT), *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_no_database_file_starts_claude_unchanged(self):
        missing = str(Path(self.tmp.name) / "does-not-exist.db")
        result = self._run(missing, "--resume", "test1")
        # WC_CLAUDE_DRY_RUN never triggers here: the no-DB branch execs before
        # the dry-run check exists in the script, exactly as it does today.
        self.assertIn("FAKE-CLAUDE-RAN", result.stdout)
        self.assertIn("no WebConsole database", result.stderr)

    def test_no_active_machine_starts_claude_unchanged(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{"name": "idle", "provider": "anthropic",
                                "active": False}])
        result = self._run(db, "--resume", "test1")
        self.assertIn("FAKE-CLAUDE-RAN", result.stdout)
        self.assertIn("no active machine", result.stderr)

    def test_anthropic_machine_with_key_resolves(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Anthropic API", "provider": "anthropic",
            "base_url": "", "api_key": "sk-test-key-value",
            "model": "claude-opus-5", "active": True,
        }])
        result = self._run(db, "--resume", "test1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ANTHROPIC_API_KEY = <set, 17 chars>", result.stdout)
        self.assertNotIn("FAKE-CLAUDE-RAN", result.stdout,
                         "dry run must not start claude")
        self.assertIn("--model claude-opus-5", result.stdout)

    def test_gateway_machine_unsets_anthropic_vars(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Gateway", "provider": "openai",
            "base_url": "https://gateway.example/v1", "api_key": "gw-key",
            "model": "gpt-5.6-luna", "active": True,
        }])
        result = self._run(db, "--resume", "test1")
        self.assertIn("ANTHROPIC_BASE_URL = (unset)", result.stdout)
        self.assertIn("ANTHROPIC_API_KEY = (unset)", result.stdout)

    def test_explicit_model_flag_always_wins(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Anthropic API", "provider": "anthropic",
            "api_key": "sk-test", "model": "claude-opus-5", "active": True,
        }])
        result = self._run(db, "--resume", "test1", "--model", "claude-haiku-4-5")
        self.assertIn("would exec: claude  --resume test1 --model claude-haiku-4-5",
                     result.stdout.replace("\n", " "))
        self.assertNotIn("--model claude-opus-5", result.stdout)


class PollerTests(unittest.TestCase):
    """The poller in isolation: no claude, no loop, just detect-and-signal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "db.sqlite")
        self.state = str(Path(self.tmp.name) / "state")

    def _script(self, body: str) -> str:
        # Sources the functions from the real script (everything up to, but
        # not including, its main body) so the poller under test is the real
        # implementation, not a re-typed copy.
        functions = SCRIPT.read_text(encoding="utf-8").split(
            'detect_resume_name "$@"', 1)[0]
        return (
            f'export WC_DB_PATH="{self.db}"\n'
            f'DB="{self.db}"\n'
            f'{functions}\n{body}\n'
        )

    def _run_bash(self, body: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", self._script(body)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def test_a_real_change_signals_the_target(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        # write_backend_state needs PROVIDER/BASE_URL/API_KEY/MODEL set, as
        # resolve_backend would set them at startup.
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 2
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("{self.db}")
con.execute("UPDATE ai_machines SET api_key = 'key-two' WHERE name = 'one'")
con.commit()
con.close()
PY
sleep 3
stop_poller
'''
        self._run_bash(body)
        self.assertTrue(Path(f"{self.state}.hit").exists(),
                        "poller did not signal after a real backend change")

    def test_no_change_does_not_signal(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 4
stop_poller
'''
        self._run_bash(body)
        self.assertFalse(Path(f"{self.state}.hit").exists(),
                         "poller signalled with no actual backend change")

    def test_no_active_machine_does_not_signal(self):
        """A machine being deactivated with nothing else active must not
        trigger a restart into a broken state."""
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 2
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("{self.db}")
con.execute("UPDATE ai_machines SET active = 0")
con.commit()
con.close()
PY
sleep 3
stop_poller
'''
        self._run_bash(body)
        self.assertFalse(Path(f"{self.state}.hit").exists(),
                         "poller signalled a transition to no active machine")

    def test_a_transient_query_failure_does_not_kill_the_poller(self):
        """query_backend can fail transiently -- the DB replaced or briefly
        unreadable under a long session. A bare command substitution under
        `set -e` would take the whole poller subshell down with it, going
        silently dead for the rest of the session with nothing left to
        notice a real change ever again."""
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 1
rm -f "{self.db}"
sleep 2
if kill -0 "$POLLER_PID" 2>/dev/null; then
    echo ALIVE > "{self.state}.alive"
fi
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("{self.db}")
con.execute(
    "CREATE TABLE ai_machines (name TEXT, provider TEXT, base_url TEXT, "
    "api_key TEXT, model TEXT, active INTEGER)"
)
con.execute("CREATE TABLE settings (key TEXT, value TEXT)")
con.execute(
    "INSERT INTO ai_machines (name, provider, api_key, model, active) "
    "VALUES ('one', 'anthropic', 'key-two', 'claude-opus-5', 1)")
con.commit()
con.close()
PY
sleep 3
stop_poller
'''
        self._run_bash(body)
        self.assertTrue(Path(f"{self.state}.alive").exists(),
                        "poller died on a transient query_backend failure "
                        "(missing DB) instead of surviving it")
        self.assertTrue(Path(f"{self.state}.hit").exists(),
                        "poller should still detect a real change once the "
                        "DB comes back")

    def test_stop_poller_leaves_no_process_behind(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
start_poller "{self.state}" "$$"
echo "POLLER_PID=$POLLER_PID"
stop_poller
sleep 1
if kill -0 "$POLLER_PID" 2>/dev/null; then
    echo STILL_ALIVE
else
    echo GONE
fi
'''
        result = self._run_bash(body)
        self.assertIn("GONE", result.stdout, result.stdout)


FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Fake claude for hot-swap integration tests.

Logs one line per invocation (its env + argv) to LOG, then sleeps until
SIGTERM, at which point it exits 0 -- mimicking a real claude session
shutting down cleanly on the signal the loop sends it.
"""
import json, os, signal, sys, time

LOG = os.environ["FAKE_CLAUDE_LOG"]

def _term(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, _term)

with open(LOG, "a") as f:
    f.write(json.dumps({
        "argv": sys.argv[1:],
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    }) + "\\n")

time.sleep(60)
'''


class HotswapLoopTests(unittest.TestCase):
    """End to end: real script, fake claude, real throwaway DB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        claude = bindir / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.db = str(Path(self.tmp.name) / "db.sqlite")
        self.log = str(Path(self.tmp.name) / "claude.log")
        Path(self.log).touch()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "WC_DB_PATH": self.db,
            "FAKE_CLAUDE_LOG": self.log,
            "WC_CLAUDE_HOTSWAP": "1",
            "WC_CLAUDE_POLL_S": "1",
            "WC_CLAUDE_NO_REPAIR": "1",
        }

    def _invocations(self) -> list[dict]:
        import json
        lines = [ln for ln in Path(self.log).read_text().splitlines() if ln]
        return [json.loads(ln) for ln in lines]

    def _wait_for(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_a_backend_change_restarts_with_the_same_resume_name(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1),
                        "first claude invocation never happened")

        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, active) "
            "VALUES ('two', 'anthropic', 'key-two', 'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()

        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 2),
                        "no second invocation after the active machine changed")
        first, second = self._invocations()[:2]
        # Both machines set a `model`, so build_model_args (Task 1, already
        # covered by DryRunResolutionTests) injects `--model <name>` ahead of
        # the passed-through args -- that is correct, established behaviour,
        # not something this test is about. What this test asserts is the
        # resume target itself: it must survive the hot-swap unchanged.
        self.assertEqual(first["argv"][-2:], ["--resume", "test-session"])
        self.assertEqual(second["argv"][-2:], ["--resume", "test-session"],
                         "resume target must be identical across a hot-swap")
        self.assertEqual(first["ANTHROPIC_API_KEY"], "key-one")
        self.assertEqual(second["ANTHROPIC_API_KEY"], "key-two")
        # The model follows the swap too, not just the environment.
        self.assertIn("claude-sonnet-5", second["argv"])

    def test_no_change_means_no_restart(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        time.sleep(4)  # several poll ticks at WC_CLAUDE_POLL_S=1
        self.assertEqual(len(self._invocations()), 1,
                         "an unrelated poll tick caused a restart")

    def test_without_the_flag_behaves_like_a_single_exec(self):
        env = dict(self.env)
        env.pop("WC_CLAUDE_HOTSWAP")
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, active) "
            "VALUES ('two', 'anthropic', 'key-two', 'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()
        time.sleep(3)
        self.assertEqual(len(self._invocations()), 1,
                         "a change restarted the session with WC_CLAUDE_HOTSWAP unset")

    def test_without_resume_behaves_like_a_single_exec(self):
        env = dict(self.env)
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT)], env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, active) "
            "VALUES ('two', 'anthropic', 'key-two', 'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()
        time.sleep(3)
        self.assertEqual(len(self._invocations()), 1,
                         "a change restarted a session with no --resume name")

    def test_the_child_exiting_on_its_own_ends_the_wrapper(self):
        """The everyday case: the user runs /exit or Ctrl-D. The wrapper must
        not treat that as something to restart from."""
        exiting_claude = '''#!/usr/bin/env python3
import sys
sys.exit(0)
'''
        bindir = Path(self.tmp.name) / "bin"
        (bindir / "claude").write_text(exiting_claude)
        (bindir / "claude").chmod(0o755)
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        result = subprocess.run(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0)

    def test_the_poller_and_child_are_cleaned_up_on_exit(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        # Find the fake-claude child before killing the wrapper.
        children = subprocess.run(
            ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True,
        ).stdout.split()
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(1)
        for pid in children:
            with self.assertRaises(subprocess.CalledProcessError,
                                   msg=f"child {pid} survived the wrapper"):
                subprocess.run(["kill", "-0", pid], check=True,
                              capture_output=True)

    def test_a_mid_session_deactivation_hands_off_unmanaged(self):
        """handoff_unmanaged's mid-loop call site (PROVIDER="-" after a
        confirmed change) is unreachable via a single clean poll tick --
        PollerTests::test_no_active_machine_does_not_signal already proves
        the poller itself never signals a transition into "no active
        machine". It is only reachable via a race: two DB writes landing
        within roughly one poll interval (active -> a different active ->
        deactivated), where the poller signals for the first, real change
        but resolve_backend re-reads the second by the time this process
        reacts. Sending the same signal the poller would have sent makes
        that already-real branch reachable deterministically here, instead
        of depending on hitting a microsecond window between two commits.
        """
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))

        # The poller and the first fake-claude child are both direct
        # children of the wrapper at this point -- capture them before
        # triggering the hand-off so we can prove neither survives it.
        before_children = subprocess.run(
            ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True,
        ).stdout.split()
        self.assertEqual(len(before_children), 2,
                         "expected exactly the poller and the fake-claude "
                         "child as the wrapper's own direct children")

        con = sqlite3.connect(self.db)
        con.execute("UPDATE ai_machines SET active = 0")
        con.commit()
        con.close()
        proc.send_signal(signal.SIGUSR1)

        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 2),
                        "no hand-off invocation after a confirmed deactivation")
        second = self._invocations()[1]
        self.assertEqual(second["ANTHROPIC_API_KEY"], "",
                         "handoff_unmanaged must not leak the deactivated "
                         "machine's key into the unmanaged child")
        self.assertEqual(second["ANTHROPIC_BASE_URL"], "")

        # exec replaces the wrapper's own process image (same pid, new
        # program) -- its former children, the poller and the pre-hand-off
        # claude child (already brought down inside the loop before
        # handoff_unmanaged runs), must not still be around afterwards.
        self.assertTrue(
            self._wait_for(lambda: all(
                subprocess.run(["kill", "-0", pid],
                               capture_output=True).returncode != 0
                for pid in before_children
            )),
            "poller or the pre-hand-off child survived handoff_unmanaged",
        )


if __name__ == "__main__":
    unittest.main()
