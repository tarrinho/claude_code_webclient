"""QA: two usage imports at once must not take the request down with them.

The defect, measured on the live service on 2026-09-15. Opening Settings shows
"Could not load statistics", and the reason is in logs/uvicorn.out.log rather
than logs/webconsole.log, which is why it stayed invisible:

    GET /api/usage/series?days=30&bucket=day HTTP/1.1" 500 Internal Server Error
    sqlite3.OperationalError: cannot start a transaction within a transaction
      routes/misc.py:555      in handle_usage_get
      routes/misc.py:534      in _import_cli_usage
      routes/db_usage.py:152  in usage_import

``_import_cli_usage`` runs at the top of *both* usage handlers -- the report at
``/api/usage`` and the charts at ``/api/usage/series``. It reaches
``usage_import``, which opens an explicit ``BEGIN`` on ``db.db_conn``, the one
connection every request in the process shares, and then keeps awaiting inside
that transaction: a ``to_thread`` call to read the session's launched model,
then a row-by-row insert loop. Each of those awaits is a point where the event
loop can hand control to another request, and the next request through either
handler issues its own ``BEGIN`` on the same connection. SQLite refuses, the
handler raises, and the client turns any non-2xx into "Could not load
statistics" (web/assets/app.js:509).

Nothing serialised it. The window is wide in practice because the series
endpoint runs four full aggregations over every usage row -- 169,087 of them
on this deployment, measured at 6.4 to 19.1 seconds per request -- and opening
Settings asks for the report and the charts together.

What these tests pin, and what they deliberately do not:

* That two concurrent imports both succeed and write their rows. That is the
  bug, and it fails without the lock with exactly the error above.
* That the import still writes the cursor with the rows, because the fix must
  not turn a correctness property into a casualty of serialising: if the rows
  landed and the cursor did not, the next run would count the same turns
  again.

They do not assert anything about ordering or fairness between the two
callers. Either may win; only "neither raises, and both are recorded" matters.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path


def _rows(session: str, count: int, start_offset: int = 0) -> list[dict]:
    """Enough rows to cross USAGE_IMPORT_BATCH, so the loop runs more than one
    transaction and the interleaving has somewhere to happen."""
    return [
        {
            "offset": start_offset + i + 1,
            "timestamp": f"2026-09-15T10:{i % 60:02d}:00Z",
            "model": "claude-opus-5",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": 12,
            "is_error": 0,
            "after_prompt": "",
        }
        for i in range(count)
    ]


class ConcurrentUsageImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-usageimport-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()

    async def asyncTearDown(self):
        await self.db.close()

    async def test_two_imports_at_once_both_succeed(self):
        """The regression. Without serialisation the loser raises
        OperationalError("cannot start a transaction within a transaction")
        and its request answers 500."""
        results = await asyncio.gather(
            self.db.usage_import("admin", "sess-a", _rows("sess-a", 60), 60),
            self.db.usage_import("admin", "sess-b", _rows("sess-b", 60), 60),
            return_exceptions=True,
        )
        raised = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(
            raised, [],
            f"a concurrent import raised instead of waiting its turn: {raised}",
        )
        self.assertEqual(results, [60, 60])

    async def test_every_row_from_both_imports_is_recorded(self):
        """Serialising must not quietly drop the loser's work -- a fix that
        swallowed the second import would satisfy the test above."""
        await asyncio.gather(
            self.db.usage_import("admin", "sess-a", _rows("sess-a", 60), 60),
            self.db.usage_import("admin", "sess-b", _rows("sess-b", 60), 60),
        )
        cur = await self.db.db_conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM usage_events "
            "WHERE session_id IN ('sess-a','sess-b') GROUP BY session_id"
        )
        counts = {row["session_id"]: row["n"] for row in await cur.fetchall()}
        self.assertEqual(counts, {"sess-a": 60, "sess-b": 60})

    async def test_the_cursor_advances_with_the_rows(self):
        """The property the transaction exists for, asserted under
        concurrency: rows and cursor move together or not at all, because a
        cursor left behind makes the next run count the same turns again."""
        await asyncio.gather(
            self.db.usage_import("admin", "sess-a", _rows("sess-a", 60), 60),
            self.db.usage_import("admin", "sess-b", _rows("sess-b", 60), 60),
        )
        self.assertEqual(await self.db.usage_cursor_get("sess-a"), 60)
        self.assertEqual(await self.db.usage_cursor_get("sess-b"), 60)


if __name__ == "__main__":
    unittest.main()
