"""QA: an unreadable backend database must not stop a terminal starting.

`bin/wc-backend-env.py` already said this about itself, above the very query
that broke it: "This tool must not be the reason a terminal fails to launch."
It guarded a missing *column* -- `active_models` was added after first release
-- and did not guard a missing *table*.

The gap is `Path.is_file()`, which is satisfied by a file that is not a
database: an empty one, a truncated one, or one another process created and has
not populated. That is not hypothetical. `config.DB_PATH` resolves relative to
the working directory, so every git worktree has its own, and a full suite run
leaves a zero-byte `data/webconsole.db` behind in whichever worktree it ran in.
The next run there failed `test_an_impossible_floor_still_starts_the_cli` with
`sqlite3.OperationalError: no such table: ai_machines` -- the wrapper correctly
refusing to launch unrouted against a backend lookup that had died.

Two runs of the same commit on the same host gave opposite results, decided by
a file an earlier chunk had created underneath the later one. The failure named
the resource floor, which was not involved at all.

Unreadable is therefore treated exactly as absent: no machines, and the caller
falls back to its default backend. The lookup is an optimisation over that
default, never a precondition for it -- refusing to start is a worse answer to
"the lookup did not work" than starting unoptimised.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-backend-env.py"


def _load():
    """Import the script by path -- its filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("wc_backend_env", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.exists(), "wc-backend-env.py not present")
class UnreadableBackendDbQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mod = _load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_zero_byte_database_behaves_as_no_database(self):
        """The exact artefact a suite run leaves in a worktree."""
        db = self.dir / "webconsole.db"
        db.touch()
        self.assertTrue(db.is_file(), "the fixture must pass the is_file gate")
        self.assertEqual(db.stat().st_size, 0)
        self.assertEqual(self.mod.machine_for(db), {})

    def test_a_missing_database_still_behaves_the_same(self):
        """The branch that always worked, pinned so the two cannot diverge."""
        self.assertEqual(self.mod.machine_for(self.dir / "absent.db"), {})

    def test_a_database_without_the_table_behaves_as_no_database(self):
        """A real SQLite file that simply predates the schema."""
        db = self.dir / "other.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()
        self.assertEqual(self.mod.machine_for(db), {})

    def test_garbage_that_is_not_sqlite_at_all(self):
        db = self.dir / "junk.db"
        db.write_bytes(b"this is not a database")
        self.assertEqual(self.mod.machine_for(db), {})

    def test_model_resolution_survives_the_same_files(self):
        """_resolve_model reads the same table through its own connection, so
        it needed its own guard. It returns an exit status, not a dict."""
        empty = self.dir / "empty2.db"
        empty.touch()
        junk = self.dir / "junk2.db"
        junk.write_bytes(b"nope")
        for path in (empty, junk, self.dir / "absent2.db"):
            with self.subTest(db=path.name):
                self.assertEqual(self.mod._resolve_model(path, "some/model"), 0)

    def test_a_populated_database_is_still_read(self):
        """The guard must not swallow the working case.

        Without this, returning {} unconditionally would pass every case above
        while disabling backend selection entirely.
        """
        db = self.dir / "real.db"
        con = sqlite3.connect(db)
        # `active`, not `is_active`: both _owner_scope and the no-profile
        # branch of machine_for read `row.get("active")`. Naming it wrong makes
        # this fixture return {} and look exactly like the guard swallowing the
        # working case -- which is how this test earned its place.
        con.execute(
            "CREATE TABLE ai_machines (id INTEGER PRIMARY KEY, name TEXT, "
            " slug TEXT, model TEXT, enabled INTEGER, active INTEGER, "
            " owner_id TEXT)"
        )
        con.execute(
            "INSERT INTO ai_machines (name, slug, model, enabled, active, "
            " owner_id) VALUES ('box', 'box', 'some/model', 1, 1, 'admin')"
        )
        con.commit()
        con.close()
        found = self.mod.machine_for(db)
        self.assertTrue(found, "a populated database must still resolve")
        self.assertEqual(found.get("model"), "some/model")


if __name__ == "__main__":
    unittest.main()
