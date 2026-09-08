"""QA: a shelved backend is not offered anywhere.

machine_for() is the load-bearing one. It is the terminal path: every
interactive `claude` on this host goes through bin/wc-claude.sh, which asks
bin/wc-backend-env.py which backend to use. If that returns a disabled
machine, the wrapper launches a session against a backend the operator
shelved -- silently, and possibly against the wrong endpoint. That is the
class of failure the wrapper exists to prevent (CLAUDE.md 0.1).

Design: docs/superpowers/specs/2026-09-08-default-and-enabled-backends-design.md
"""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "wc-backend-env.py"


def _db_with(machines: list[tuple[str, str, int, int]]) -> Path:
    """machines: (id, name, active, enabled)."""
    path = Path(tempfile.mkdtemp(prefix="wc-offered-")) / "wc.db"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE ai_machines (id TEXT PRIMARY KEY, name TEXT, provider TEXT, "
        "host TEXT, port INTEGER, api_key TEXT, model TEXT, base_url TEXT, "
        "description TEXT, active INTEGER, enabled INTEGER NOT NULL DEFAULT 1, "
        "owner_id TEXT, created_at TEXT, updated_at TEXT, active_models TEXT)"
    )
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    for mid, name, active, enabled in machines:
        con.execute(
            "INSERT INTO ai_machines (id,name,provider,host,port,api_key,model,"
            "base_url,description,active,enabled,owner_id,created_at,updated_at,"
            "active_models) VALUES (?,?,'claude_code','api.anthropic.com',443,"
            "NULL,'claude-opus-5','https://api.anthropic.com',NULL,?,?,'admin',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','[]')",
            (mid, name, active, enabled),
        )
    con.commit()
    con.close()
    return path


def _helper_module(tag: str):
    """Load bin/wc-backend-env.py as a module. It is a script, not a package
    member, so importlib is the only way to reach machine_for directly."""
    spec = importlib.util.spec_from_file_location(f"wcbe_{tag}", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MachineForTests(unittest.TestCase):
    def test_a_disabled_active_machine_is_not_returned(self):
        """The terminal path. A disabled backend still flagged active must not
        reach bin/wc-claude.sh."""
        wcbe = _helper_module("disabled")
        db = _db_with([("m-1", "Shelved", 1, 0)])
        self.assertEqual(
            wcbe.machine_for(db), {},
            "a disabled backend must never be resolved as active")

    def test_an_enabled_active_machine_is_returned(self):
        wcbe = _helper_module("enabled")
        db = _db_with([("m-1", "Live", 1, 1)])
        self.assertEqual(wcbe.machine_for(db).get("name"), "Live")

    def test_a_disabled_machine_is_skipped_in_favour_of_nothing(self):
        """Only one machine is active; disabling it leaves no default rather
        than promoting an arbitrary other one."""
        wcbe = _helper_module("skip")
        db = _db_with([("m-1", "Shelved", 1, 0), ("m-2", "Other", 0, 1)])
        self.assertEqual(wcbe.machine_for(db), {})

    def test_naming_a_disabled_profile_is_refused(self):
        wcbe = _helper_module("profile")
        db = _db_with([("m-1", "Shelved", 0, 0), ("m-2", "Live", 1, 1)])
        with self.assertRaises(SystemExit) as caught:
            wcbe.machine_for(db, "shelved")
        self.assertEqual(caught.exception.code, 2)

    def test_a_database_without_the_column_still_resolves(self):
        """SELECT * plus .get('enabled', 1): this helper must keep working
        against a database that predates the column, the same way it does for
        active_models."""
        wcbe = _helper_module("legacy")
        path = Path(tempfile.mkdtemp(prefix="wc-legacy-")) / "wc.db"
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE ai_machines (id TEXT PRIMARY KEY, name TEXT, "
            "provider TEXT, host TEXT, port INTEGER, api_key TEXT, model TEXT, "
            "base_url TEXT, description TEXT, active INTEGER, owner_id TEXT, "
            "created_at TEXT, updated_at TEXT)"
        )
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "INSERT INTO ai_machines VALUES ('m-1','Old','claude_code',"
            "'api.anthropic.com',443,NULL,'claude-opus-5',"
            "'https://api.anthropic.com',NULL,1,'admin',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        con.commit()
        con.close()
        self.assertEqual(wcbe.machine_for(path).get("name"), "Old")


if __name__ == "__main__":
    unittest.main()
