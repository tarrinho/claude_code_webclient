"""QA: wc-claude.sh resolves the active backend correctly, with and without
the hot-swap feature engaged.

Task 1 establishes that refactoring the script into reusable functions changes
nothing observable: the same four DB fixtures must resolve to the same
WC_CLAUDE_DRY_RUN report, byte for byte, before and after the refactor. Tasks 2
and 3 build the poller and the supervised loop on top of these functions.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
