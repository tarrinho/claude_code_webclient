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
            # Registry #81: resolve_claude_bin checks $WC_CLAUDE_PATH before it
            # ever falls back to PATH, and tries the newest installed
            # ~/.local/share/claude/versions/* ahead of PATH too -- so on a
            # host with a real CLI installed (every dev box), the fake claude
            # on PATH was never actually reached. Pin it through the seam the
            # script already provides for exactly this.
            "WC_CLAUDE_PATH": str(fake_claude / "claude"),
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
        _make_db(db, machines=[{"name": "idle", "provider": "claude_code",
                                "active": False}])
        result = self._run(db, "--resume", "test1")
        self.assertIn("FAKE-CLAUDE-RAN", result.stdout)
        self.assertIn("no active machine", result.stderr)

    def test_no_active_machine_leaves_the_callers_own_credentials_alone(self):
        """Regression guard: the startup "no active machine" guard is a bare
        exec, not routed through handoff_unmanaged. handoff_unmanaged calls
        apply_env, which is correct at its actual (mid-loop) call site --
        stripping this same script's own earlier export for a machine that
        just went inactive -- but there is nothing of this script's own to
        strip here, at the very first thing the script does. Routing this
        guard through it too would silently unset whatever ANTHROPIC_*/
        CLAUDE_CODE_SIMPLE the caller's own shell had already set, while the
        banner still (falsely) says "unchanged"."""
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{"name": "idle", "provider": "claude_code",
                                "active": False}])
        env_reporter = Path(self.tmp.name) / "bin" / "claude"
        env_reporter.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "print('ANTHROPIC_API_KEY=' + os.environ.get('ANTHROPIC_API_KEY', '<unset>'))\n"
            "print('CLAUDE_CODE_SIMPLE=' + os.environ.get('CLAUDE_CODE_SIMPLE', '<unset>'))\n"
        )
        env_reporter.chmod(0o755)
        env = dict(self.env)
        env.pop("WC_CLAUDE_DRY_RUN", None)
        env["WC_DB_PATH"] = db
        env["ANTHROPIC_API_KEY"] = "callers-own-key"
        env["CLAUDE_CODE_SIMPLE"] = "1"
        result = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                                env=env, check=False)
        self.assertIn("ANTHROPIC_API_KEY=callers-own-key", result.stdout)
        self.assertIn("CLAUDE_CODE_SIMPLE=1", result.stdout)

    def test_anthropic_machine_with_key_resolves(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Anthropic API", "provider": "claude_code",
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
            "name": "Anthropic API", "provider": "claude_code",
            "api_key": "sk-test", "model": "claude-opus-5", "active": True,
        }])
        result = self._run(db, "--resume", "test1", "--model", "claude-haiku-4-5")
        # Registry #81: "would exec:" always names the fully-resolved binary
        # path (resolve_claude_bin never execs a bare name), and that path is
        # a property of the box -- here it is the fake claude's tempdir, on a
        # real machine it is whichever ~/.local/share/claude/versions/* is
        # newest. Assert on the argument shape, not the resolved executable.
        self.assertIn("--resume test1 --model claude-haiku-4-5",
                     result.stdout.replace("\n", " "))
        self.assertIn("would exec:", result.stdout)
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
            f'{functions}\n'
            # The spliced text above re-derives HERE from ${BASH_SOURCE[0]},
            # which names this bash -c string itself when run this way, not
            # the real wc-claude.sh -- so query_backend's `$HERE/bin/...`
            # resolved to a path under "/" and every poll tick failed to find
            # the file, silently (the poller swallows a transient failure by
            # design). Re-pin it to the real repo root after sourcing.
            f'HERE="{ROOT}"\n'
            f'{body}\n'
        )

    def _run_bash(self, body: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", self._script(body)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def test_a_real_change_signals_the_target(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            # Registry #81: same as DryRunResolutionTests -- the real installed
            # CLI wins over PATH in resolve_claude_bin's order, so the fake
            # claude must be pinned through $WC_CLAUDE_PATH, its documented
            # override seam, not injected via PATH alone.
            "WC_CLAUDE_PATH": str(claude),
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        result = subprocess.run(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_the_poller_and_child_are_cleaned_up_on_exit(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "claude_code", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        # Find the fake-claude child before killing the wrapper.
        children = subprocess.run(
            ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True, check=False,
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
            "name": "one", "provider": "claude_code", "api_key": "key-one",
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
            ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True, check=False,
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
                               capture_output=True, check=False).returncode != 0
                for pid in before_children
            )),
            "poller or the pre-hand-off child survived handoff_unmanaged",
        )


class ScreenSurvivalTests(unittest.TestCase):
    """Automates as much of the plan's Task 3 Step 7 manual acceptance check
    as a unit test can reach: a real `screen` session, real pty, the wrapper
    supervising a hot-swap inside it. Fake claude + a throwaway WC_DB_PATH --
    no production database, no live WebConsole Settings flip, no real
    Anthropic spend. What this cannot cover is the one thing that needs a
    human: the live WebConsole UI actually writing the `active` flag through
    its own Settings page rather than a direct SQL UPDATE here.

    Proves the two things Step 7 was written to check: the banner reprints
    with the new machine's name inside the same window, and the screen
    session's pid is unchanged across the restart -- the pty, not the
    wrapper's own pid, is what must survive (see the design doc's core
    insight)."""

    def setUp(self):
        if subprocess.run(["which", "screen"],
                          capture_output=True, check=False).returncode != 0:
            self.skipTest("screen not installed")
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
        self.hardcopy = str(Path(self.tmp.name) / "hardcopy.txt")
        self.session = f"wc-qa-{os.getpid()}-{id(self)}"
        self.addCleanup(self._quit_session)
        self.overrides = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            # Registry #81: same seam as the other two fixtures above.
            "WC_CLAUDE_PATH": str(claude),
            "WC_DB_PATH": self.db,
            "FAKE_CLAUDE_LOG": self.log,
            "WC_CLAUDE_HOTSWAP": "1",
            "WC_CLAUDE_POLL_S": "1",
            "WC_CLAUDE_NO_REPAIR": "1",
        }

    def _quit_session(self):
        subprocess.run(["screen", "-S", self.session, "-X", "quit"],
                       capture_output=True, check=False)

    def _screen_pid(self) -> str | None:
        out = subprocess.run(["screen", "-list"],
                             capture_output=True, text=True, check=False).stdout
        for line in out.splitlines():
            token = line.strip().split()[:1]
            if not token or "." not in token[0]:
                continue
            pid, _, name = token[0].partition(".")
            if name == self.session:
                return pid
        return None

    def _hardcopy_text(self) -> str:
        subprocess.run(
            ["screen", "-S", self.session, "-X", "hardcopy", self.hardcopy],
            capture_output=True, check=False,
        )
        return Path(self.hardcopy).read_text(errors="replace") \
            if Path(self.hardcopy).exists() else ""

    def _wait_for(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.3)
        return False

    def test_the_banner_reprints_in_the_same_screen_session_on_a_hot_swap(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "claude_code", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        env_pairs = [f"{k}={v}" for k, v in self.overrides.items()]
        subprocess.run(
            ["screen", "-dmS", self.session, "env", *env_pairs,
             str(SCRIPT), "--resume", "test-session"],
            check=True,
        )
        self.assertTrue(self._wait_for(lambda: self._screen_pid() is not None),
                        "screen session never started")
        pid_before = self._screen_pid()

        self.assertTrue(
            self._wait_for(lambda: "wc-claude: one" in self._hardcopy_text()),
            "banner for the first machine never appeared in the screen "
            f"session; last hardcopy:\n{self._hardcopy_text()}",
        )

        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, "
            "active) VALUES ('two', 'anthropic', 'key-two', "
            "'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()

        self.assertTrue(
            self._wait_for(lambda: "wc-claude: two" in self._hardcopy_text()),
            "banner did not reprint for the second machine within the "
            f"same screen session; last hardcopy:\n{self._hardcopy_text()}",
        )
        self.assertEqual(
            self._screen_pid(), pid_before,
            "the screen session's pid changed across the hot-swap -- the "
            "pty was not preserved, which is the one property this whole "
            "feature exists to guarantee",
        )


if __name__ == "__main__":
    unittest.main()
