"""QA: db.system_sample_insert must resolve to the real, current implementation.

Found in production logs while investigating an unrelated sidebar glitch:
`logs/webconsole.log` showed `sqlite3.ProgrammingError: Error binding
parameter 1: type 'dict' is not supported`, raised from inside
`db.system_sample_insert`, on every single interval of the background
sysstats loop (192 times in one day) -- local host stats had not been
recorded at all.

Root cause: the db.py -> routes/db_usage.py module split (see db.py's
`__getattr__`) moved `system_sample_insert` to take one `values: dict`
argument, matching the flat `SYSTEM_FIELDS` schema and matching how
sysstats.py already called it (`store(to_row(snapshot))`, one positional
dict). But an old three-arg duplicate (`host_type, host_id, data`) was left
behind in db.py itself. `__getattr__` only resolves a name that Python
cannot find any other way -- a real module-level definition always wins --
so every caller of `db.system_sample_insert` (sysstats.py's loop, spawned
via `sysstats.start(db.system_sample_insert)`, and anyone else who imports
`db` rather than `routes.db_usage` directly) got the dead duplicate instead
of the one actually wired to the current table shape. It crashed on every
call, silently, in a background task nobody was watching. It also
intermittently broke *unrelated* concurrent requests sharing the same
aiosqlite worker thread -- confirmed live: a probe script's own
`POST /api/chats` 500'd until this was fixed, then passed cleanly.

`system_sample_list` (host_type, limit), the read-side twin of the same
stale block, was removed alongside it: same incomplete-split shape, no
callers left anywhere in the codebase (checked via grep before removing).
"""
from __future__ import annotations

import inspect
import unittest

import db
import sysstats


class SystemSampleInsertShadowQA(unittest.TestCase):
    def test_db_system_sample_insert_is_not_shadowed_by_a_stale_duplicate(self):
        # A local module-level def in db.py -- even a dead one nobody calls
        # on purpose -- wins over the __getattr__ forward to routes.db_usage.
        # Asserting the real module and signature is the only way to catch
        # that shadowing; asserting behaviour alone (e.g. "does it accept a
        # dict") would not explain *why* a fix regressed if it ever did.
        self.assertEqual(
            db.system_sample_insert.__module__, "routes.db_usage",
            "db.system_sample_insert resolved outside routes.db_usage -- a "
            "module-level definition in db.py is shadowing the __getattr__ "
            "forward again",
        )
        params = list(inspect.signature(db.system_sample_insert).parameters)
        self.assertEqual(
            params, ["values"],
            "db.system_sample_insert's signature changed shape -- sysstats.py "
            "calls it with one positional dict (`store(to_row(snapshot))`); "
            "anything else reproduces the parameter-binding crash this test "
            "exists to catch",
        )

    def test_sysstats_calls_it_with_the_shape_it_actually_takes(self):
        # Ties the two halves of the bug together: to_row()'s output shape
        # must match what db.system_sample_insert (whichever one resolves)
        # is prepared to bind. Constructs the call the real background loop
        # makes without touching a database, so this stays a fast unit test.
        snapshot = {
            "cpu_pct": 12.5, "mem_pct": 40.0, "mem_used": 100, "mem_total": 200,
            "swap_pct": 0.0, "disk": {"pct": 50.0, "used": 1, "total": 2},
            "load": [0.1, 0.2, 0.3], "proc": {"rss": 1000, "cpu_pct": 1.0},
        }
        row = sysstats.to_row(snapshot)
        self.assertIsInstance(row, dict)
        sig = inspect.signature(db.system_sample_insert)
        # Binding against the real signature raises TypeError for exactly the
        # shape mismatch that broke this in production (three scalar params
        # expecting host_type/host_id/data, fed one dict) -- this is the
        # regression check without needing a live DB connection.
        sig.bind(row)

    def test_system_sample_list_is_gone(self):
        # Same stale block, same incomplete split, zero callers left anywhere
        # in the codebase when removed. Asserting it now raises through
        # __getattr__ (rather than resolving to a second dead duplicate)
        # catches the same shadowing class of bug if it is ever reintroduced.
        with self.assertRaises(AttributeError):
            db.system_sample_list


if __name__ == "__main__":
    unittest.main()
