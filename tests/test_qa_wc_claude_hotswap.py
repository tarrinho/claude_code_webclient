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


if __name__ == "__main__":
    unittest.main()
