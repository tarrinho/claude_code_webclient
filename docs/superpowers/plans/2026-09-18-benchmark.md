# Benchmark Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a benchmark subsystem that continuously re-measures every model against every task type, writes each result into `delegation_capability` as it is produced, and exposes a per-cell Re-measure control on the Delegation page.

**Architecture:** A thin Python orchestrator spawns one `bin/wc-bench.py` subprocess per `(model, task_type)` cell and writes each result immediately through a single writer function. Sweep progress is not tracked in a status ledger — it is derived from a new `measured_at` column on `delegation_capability`, so progress and result can never disagree. A systemd timer runs the orchestrator nightly; it measures only while the box is idle and stops the moment it is used.

**Tech Stack:** Python 3.13, `aiosqlite`, `asyncio`, FastAPI (`routes/`), vanilla ES modules (`web/assets/`), systemd user units, `unittest.IsolatedAsyncioTestCase`.

**Spec:** `docs/superpowers/specs/2026-09-18-benchmark-design-v2.md`

## Global Constraints

- **Run tests with `.venv/bin/python -m pytest`, invoked bare** — not `pytest tests/`, which misses files. Any other interpreter skips the browser layer. A trustworthy run reports exactly **6 skips**.
- **Never run `db.init()` against the production database** (`data/webconsole.db`). It migrates. Every test uses a `tempfile.TemporaryDirectory()` with `config.DB_PATH` patched.
- **Never use a whole-tree `git add`.** Several sessions share this checkout. Use explicit pathspecs in every commit step.
- **Never print a resolved environment, `base_url`, or `api_key`** to a log or stdout.
- **Sequential measurement only.** Two benchmarks against one gateway measure their own contention. Never parallelise the cell loop.
- **For `task_type == "voice"` the writer writes `accuracy` and `n` and leaves `median_latency_s` untouched.** This is the only safeguard on that column — nothing is reviewed before a write.
- **`measured_at` is the resume key.** A cell is done when its `delegation_capability` row has `measured_at >= sweep.started_at`. Never add a second source of truth for progress.
- A failed cell writes **no** capability row and **always** a `benchmark_cells` row.
- **`cells_total` is the frozen matrix size (70) and never changes mid-sweep.** Progress counts against attemptable cells; dormant count is reported alongside.
- Constants, exact: sweep expiry **10 days**, cooling **2 days**, window start **02:00**, busy idle margin **10 minutes**, busy re-check **60 seconds**, dormancy threshold **3 consecutive failures**, repeats **3**.
- `web/assets/*.js` is imported with a content hash (`./delegation.js?v=…`). After editing any asset, run `bin/wc-asset-versions.py` or the browser loads the old module.
- `web/assets/delegation.js` was being edited by a parallel session on 2026-09-18. Check `git status` before editing it and coordinate.

---

## File Structure

| File | Responsibility |
|---|---|
| `db.py` (modify) | Two new tables, five new `delegation_capability` columns |
| `routes/db_benchmark.py` (create) | All SQL for `benchmark_runs` / `benchmark_cells` and the capability provenance columns |
| `benchmark_writer.py` (create) | The single capability-write path, including the voice exclusion |
| `benchmark_cell.py` (create) | Run one cell as a subprocess; parse `wc-bench.py` output into a result |
| `benchmark_sweep.py` (create) | Resume, dormancy, busy detection, the nightly loop, the scheduler decision |
| `benchmark_reorder.py` (create) | Detect ladder reorderings and set the highlight |
| `bin/wc-benchmark.py` (create) | CLI surface |
| `routes/benchmark.py` (create) | Three HTTP endpoints |
| `web/assets/delegation.js` (modify) | Re-measure button, dormant marker, reorder highlight, ack |
| `systemd/webconsole-benchmark.{service,timer}` (create) | Nightly trigger |

`routes/delegation.py` is already 648 lines; the new endpoints go in their own router rather than growing it.

---

### Task 1: Schema and storage accessors

**Files:**
- Modify: `db.py` (table DDL near `delegation_capability` at line 506; column migrations near line 1023)
- Create: `routes/db_benchmark.py`
- Test: `tests/test_qa_benchmark_store.py`

**Interfaces:**
- Consumes: `db.db_conn`, `db._now()`
- Produces:
  - `async def run_create(run_id, started_at, expires_at, models, task_types, repeats, cells_total, trigger="scheduled") -> None`
  - `async def run_get(run_id) -> dict | None`
  - `async def run_current() -> dict | None`
  - `async def run_finish(run_id, finished_at, cooling_until) -> None`
  - `async def run_expire(run_id) -> None`
  - `async def run_set_dormant_count(run_id, count) -> None`
  - `async def cell_record(run_id, model, task_type, status, accuracy, n, median_latency_s, elapsed_s, error) -> None`
  - `async def cells_for_run(run_id) -> list[dict]`
  - `async def capability_meta_all() -> list[dict]` — `model, task_type, measured_at, trigger, measured_under_load, consecutive_failures, dormant, reorder_flagged, reorder_seen_at, reorder_acked_at`
  - `async def capability_meta_set(model, task_type, **columns) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_store.py
"""QA: the benchmark storage layer.

`run_current` is the scheduler's only question (spec 8.1), so it is tested in
both directions: a cooling run must be current and an elapsed one must not.
Testing only the first would pass for an implementation that calls every
finished run current, which would stop new sweeps forever.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
from routes import db_benchmark as store


class BenchmarkStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _run(self, run_id="r1", started="2026-09-18T02:00:00Z",
                   expires="2026-09-28T02:00:00Z"):
        await store.run_create(
            run_id=run_id, started_at=started, expires_at=expires,
            models=["m1"], task_types=["coding"], repeats=3, cells_total=1)
        return run_id

    async def test_a_running_sweep_is_current(self):
        await self._run()
        current = await store.run_current()
        self.assertIsNotNone(current)
        self.assertEqual(current["id"], "r1")

    async def test_a_cooling_sweep_is_current(self):
        await self._run()
        await store.run_finish("r1", "2026-09-20T05:00:00Z",
                               cooling_until="2999-01-01T00:00:00Z")
        current = await store.run_current()
        self.assertIsNotNone(current)
        self.assertEqual(current["status"], "done")

    async def test_a_sweep_whose_cooling_elapsed_is_not_current(self):
        """The direction that stops a finished sweep blocking every later one."""
        await self._run()
        await store.run_finish("r1", "2026-09-20T05:00:00Z",
                               cooling_until="2000-01-01T00:00:00Z")
        self.assertIsNone(await store.run_current())

    async def test_an_expired_sweep_is_not_current(self):
        await self._run()
        await store.run_expire("r1")
        self.assertIsNone(await store.run_current())

    async def test_capability_meta_round_trips(self):
        from routes.db_delegation import delegation_row_set
        await delegation_row_set("m1", "coding", accuracy=1.0, n=3)
        await store.capability_meta_set(
            "m1", "coding", measured_at="2026-09-18T03:00:00Z",
            trigger="manual", measured_under_load=1)
        rows = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}
        row = rows[("m1", "coding")]
        self.assertEqual(row["measured_at"], "2026-09-18T03:00:00Z")
        self.assertEqual(row["trigger"], "manual")
        self.assertEqual(row["measured_under_load"], 1)

    async def test_a_failed_cell_still_writes_a_cells_row(self):
        """Dormancy counts these rows; a failure leaving none is uncountable."""
        await self._run()
        await store.cell_record(
            run_id="r1", model="m1", task_type="coding", status="failed",
            accuracy=None, n=None, median_latency_s=None,
            elapsed_s=900.0, error="timeout after 900s")
        cells = await store.cells_for_run("r1")
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["status"], "failed")
        self.assertEqual(cells[0]["error"], "timeout after 900s")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routes.db_benchmark'`

- [ ] **Step 3: Add the tables to `db.py`**

Add to the `executescript` block, immediately after the `delegation_operational` table (around line 524):

```sql
        -- Benchmark sweeps. A sweep is "current" from the moment it starts
        -- until `cooling_until` passes, which is why both windows are stored
        -- rather than derived: the scheduler reads one row instead of
        -- recomputing eligibility from timestamps every time it wakes.
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id            TEXT PRIMARY KEY,
            started_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            finished_at   TEXT,
            cooling_until TEXT,
            status        TEXT NOT NULL,
            models        TEXT NOT NULL,
            task_types    TEXT NOT NULL,
            repeats       INTEGER NOT NULL,
            cells_total   INTEGER NOT NULL,
            cells_dormant INTEGER NOT NULL DEFAULT 0,
            trigger       TEXT NOT NULL DEFAULT 'scheduled'
        );

        -- Measurement history, NOT the resume ledger -- resume derives from
        -- delegation_capability.measured_at (spec 6). This table exists so a
        -- failure is countable: dormancy (spec 12) reads three consecutive
        -- failed rows, and a failure that wrote nothing would be invisible.
        CREATE TABLE IF NOT EXISTS benchmark_cells (
            run_id            TEXT NOT NULL,
            model             TEXT NOT NULL,
            task_type         TEXT NOT NULL,
            status            TEXT NOT NULL,
            accuracy          REAL,
            n                 INTEGER,
            median_latency_s  REAL,
            elapsed_s         REAL NOT NULL DEFAULT 0,
            error             TEXT,
            recorded_at       TEXT NOT NULL,
            PRIMARY KEY (run_id, model, task_type)
        );
```

- [ ] **Step 4: Add the capability column migrations to `db.py`**

Add after the `orchestrator_tasks` migration block (around line 1040), following the same idiom:

```python
    cursor = await db_conn.execute("PRAGMA table_info(delegation_capability)")
    cap_columns = {row["name"] for row in await cursor.fetchall()}
    cap_migrations = {
        # The resume key (spec 6). A cell is done when this is at or after the
        # sweep's started_at. Deriving progress from the written result rather
        # than a status column means the two can never disagree.
        "measured_at": "ALTER TABLE delegation_capability ADD COLUMN measured_at TEXT",
        "trigger": (
            "ALTER TABLE delegation_capability ADD COLUMN trigger "
            "TEXT NOT NULL DEFAULT 'scheduled'"
        ),
        # 7.33s measured on an idle box and 7.33s measured under three agent
        # sessions mean different things, and only the second is suspect.
        "measured_under_load": (
            "ALTER TABLE delegation_capability ADD COLUMN measured_under_load "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "consecutive_failures": (
            "ALTER TABLE delegation_capability ADD COLUMN consecutive_failures "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "dormant": (
            "ALTER TABLE delegation_capability ADD COLUMN dormant "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "reorder_flagged": (
            "ALTER TABLE delegation_capability ADD COLUMN reorder_flagged "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "reorder_seen_at": (
            "ALTER TABLE delegation_capability ADD COLUMN reorder_seen_at TEXT"
        ),
        "reorder_acked_at": (
            "ALTER TABLE delegation_capability ADD COLUMN reorder_acked_at TEXT"
        ),
    }
    for name, sql in cap_migrations.items():
        if name not in cap_columns:
            await db_conn.execute(sql)
```

- [ ] **Step 5: Write `routes/db_benchmark.py`**

```python
# db_benchmark.py -- storage for benchmark sweeps and their cells.
#
# Two tables and a set of provenance columns on delegation_capability. The
# split matters: benchmark_cells is measurement HISTORY, and
# delegation_capability.measured_at is the resume key (spec 6). Nothing here
# should ever grow a second way to answer "has this cell been measured".
from __future__ import annotations

import json
from typing import Any

import db

_META_COLUMNS = (
    "measured_at", "trigger", "measured_under_load", "consecutive_failures",
    "dormant", "reorder_flagged", "reorder_seen_at", "reorder_acked_at",
)


async def run_create(run_id: str, started_at: str, expires_at: str,
                     models: list[str], task_types: list[str], repeats: int,
                     cells_total: int, trigger: str = "scheduled") -> None:
    """Start a sweep. `models` and `task_types` are frozen here on purpose:
    a sweep spans several nights, so reading them live would let a mid-sweep
    change to DEFAULT_MODELS silently redefine the matrix."""
    await db.db_conn.execute(
        "INSERT INTO benchmark_runs (id, started_at, expires_at, status, "
        " models, task_types, repeats, cells_total, trigger) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
        (run_id, started_at, expires_at, json.dumps(models),
         json.dumps(task_types), int(repeats), int(cells_total), trigger),
    )
    await db.db_conn.commit()


async def run_get(run_id: str) -> dict[str, Any] | None:
    cur = await db.db_conn.execute(
        "SELECT * FROM benchmark_runs WHERE id = ?", (run_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def run_current() -> dict[str, Any] | None:
    """The one sweep the scheduler may act on, or None.

    Current means running, or done with cooling still ahead. An expired sweep
    is never current -- it failed to keep the table fresh, so the next run
    starts a fresh sweep without serving a cooling period (spec 8.2).
    """
    cur = await db.db_conn.execute(
        "SELECT * FROM benchmark_runs "
        "WHERE status = 'running' "
        "   OR (status = 'done' AND cooling_until > ?) "
        "ORDER BY started_at DESC LIMIT 1",
        (db._now(),),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def run_finish(run_id: str, finished_at: str, cooling_until: str) -> None:
    await db.db_conn.execute(
        "UPDATE benchmark_runs SET status = 'done', finished_at = ?, "
        "cooling_until = ? WHERE id = ?",
        (finished_at, cooling_until, run_id),
    )
    await db.db_conn.commit()


async def run_expire(run_id: str) -> None:
    """Abandon the sweep. Its written cells stay in delegation_capability --
    expiry abandons the sweep, not its measurements."""
    await db.db_conn.execute(
        "UPDATE benchmark_runs SET status = 'expired' WHERE id = ?", (run_id,))
    await db.db_conn.commit()


async def run_set_dormant_count(run_id: str, count: int) -> None:
    await db.db_conn.execute(
        "UPDATE benchmark_runs SET cells_dormant = ? WHERE id = ?",
        (int(count), run_id))
    await db.db_conn.commit()


async def cell_record(run_id: str, model: str, task_type: str, status: str,
                      accuracy: float | None, n: int | None,
                      median_latency_s: float | None, elapsed_s: float,
                      error: str | None) -> None:
    await db.db_conn.execute(
        "INSERT INTO benchmark_cells (run_id, model, task_type, status, "
        " accuracy, n, median_latency_s, elapsed_s, error, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, model, task_type) DO UPDATE SET "
        "  status = excluded.status, accuracy = excluded.accuracy, "
        "  n = excluded.n, median_latency_s = excluded.median_latency_s, "
        "  elapsed_s = excluded.elapsed_s, error = excluded.error, "
        "  recorded_at = excluded.recorded_at",
        (run_id, model, task_type, status, accuracy, n, median_latency_s,
         float(elapsed_s), error, db._now()),
    )
    await db.db_conn.commit()


async def cells_for_run(run_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        "SELECT * FROM benchmark_cells WHERE run_id = ?", (run_id,))
    return [dict(row) for row in await cur.fetchall()]


async def capability_meta_all() -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        "SELECT model, task_type, measured_at, trigger, measured_under_load, "
        "consecutive_failures, dormant, reorder_flagged, reorder_seen_at, "
        "reorder_acked_at FROM delegation_capability ORDER BY task_type, model")
    return [dict(row) for row in await cur.fetchall()]


async def capability_meta_set(model: str, task_type: str, **columns: Any) -> None:
    """Update provenance columns on an existing capability row.

    Unknown names are refused rather than dropped, matching
    `delegation_row_set`: a typo would otherwise read as a successful write of
    nothing.
    """
    unknown = set(columns) - set(_META_COLUMNS)
    if unknown:
        raise ValueError(f"unknown capability meta columns: {sorted(unknown)}")
    if not columns:
        return
    assignments = ", ".join(f"{c} = ?" for c in columns)
    await db.db_conn.execute(
        f"UPDATE delegation_capability SET {assignments} "
        "WHERE model = ? AND task_type = ?",
        tuple(columns.values()) + (model, task_type),
    )
    await db.db_conn.commit()
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_store.py -v`
Expected: PASS, 6 tests

- [ ] **Step 7: Commit**

```bash
git add db.py routes/db_benchmark.py tests/test_qa_benchmark_store.py
git commit -m "Add benchmark run and cell tables with capability provenance columns"
```

---

### Task 2: The writer, and the voice exclusion

**Files:**
- Create: `benchmark_writer.py`
- Test: `tests/test_qa_benchmark_writer.py`

**Interfaces:**
- Consumes: `routes.db_delegation.delegation_row_set`, `routes.db_benchmark.capability_meta_set`
- Produces: `async def write_cell(model: str, task_type: str, accuracy: float | None, n: int | None, median_latency_s: float | None, *, measured_at: str, trigger: str = "scheduled", under_load: bool = False) -> None`

This is the only function that writes a measurement into `delegation_capability`. Every caller in later tasks goes through it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_writer.py
"""QA: the single capability writer, and the one column it must protect.

Nothing reviews a measurement before it is written (spec 6), so the voice
latency exclusion in this writer is the only thing standing between a
mismeasured transport and every deadline spec v3 5.1 derives. The harness
measures over the CLI; routes/voice.py speaks to an OpenAI-compatible endpoint
directly, and the two disagree by 6.6-9.6s against ~2.0s.

The exclusion is asserted against a NON-voice task type as well, because a
writer that dropped `median_latency_s` for everything would satisfy the voice
assertion alone.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_writer
import config
import db
from routes.db_delegation import delegation_row_set, delegation_rows_all
from routes import db_benchmark as store


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _row(self, model, task_type):
        rows = {(r["model"], r["task_type"]): r for r in await delegation_rows_all()}
        return rows[(model, task_type)]

    async def test_a_normal_cell_writes_all_three_numbers(self):
        await delegation_row_set("m1", "coding", accuracy=0.5, n=2,
                                 median_latency_s=99.0)
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "coding")
        self.assertEqual(row["accuracy"], 1.0)
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["median_latency_s"], 12.8)

    async def test_voice_keeps_its_previous_latency(self):
        """The exclusion, in the direction that catches its removal."""
        await delegation_row_set("m1", "voice", accuracy=0.5, n=2,
                                 median_latency_s=2.0)
        await benchmark_writer.write_cell(
            "m1", "voice", accuracy=0.9, n=6, median_latency_s=9.6,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "voice")
        self.assertEqual(row["accuracy"], 0.9)
        self.assertEqual(row["n"], 6)
        self.assertEqual(row["median_latency_s"], 2.0,
                         "voice latency must not be overwritten from the CLI")

    async def test_voice_with_no_previous_latency_stays_null(self):
        await delegation_row_set("m1", "voice", accuracy=None, n=None,
                                 median_latency_s=None)
        await benchmark_writer.write_cell(
            "m1", "voice", accuracy=0.9, n=6, median_latency_s=9.6,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "voice")
        self.assertIsNone(row["median_latency_s"])

    async def test_provenance_is_stamped(self):
        await delegation_row_set("m1", "coding")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-18T15:00:00Z", trigger="manual",
            under_load=True)
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["measured_at"], "2026-09-18T15:00:00Z")
        self.assertEqual(meta["trigger"], "manual")
        self.assertEqual(meta["measured_under_load"], 1)

    async def test_a_scheduled_write_overwrites_an_under_load_row(self):
        """Provenance records, it never gates. Both directions asserted so
        nobody later 'improves' the writer into refusing one of them."""
        await delegation_row_set("m1", "coding")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=0.6, n=3, median_latency_s=30.0,
            measured_at="2026-09-18T15:00:00Z", trigger="manual",
            under_load=True)
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-19T03:00:00Z", trigger="scheduled")
        row = await self._row("m1", "coding")
        self.assertEqual(row["median_latency_s"], 12.8)
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["measured_under_load"], 0)

    async def test_a_manual_write_overwrites_a_scheduled_row(self):
        await delegation_row_set("m1", "coding")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-19T03:00:00Z", trigger="scheduled")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=0.6, n=3, median_latency_s=30.0,
            measured_at="2026-09-19T15:00:00Z", trigger="manual",
            under_load=True)
        row = await self._row("m1", "coding")
        self.assertEqual(row["median_latency_s"], 30.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_writer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmark_writer'`

- [ ] **Step 3: Write `benchmark_writer.py`**

```python
# benchmark_writer.py -- the ONE path a measurement takes into the
# capability table.
#
# Spec 11. Nothing reviews a measurement before it lands (spec 6), so the voice
# exclusion below is not a convenience -- it is the only safeguard on
# median_latency_s for that task type. Every caller (the nightly sweep, the CLI
# --cell form, and the page endpoint) goes through write_cell. There is
# deliberately no second write path.
from __future__ import annotations

import logging

from routes.db_benchmark import capability_meta_set
from routes.db_delegation import delegation_row_set, delegation_rows_all

_log = logging.getLogger(__name__)

#: The harness measures over the CLI transport. routes/voice.py is CLAUDE.md
#: section 0's documented exception and speaks to an OpenAI-compatible endpoint
#: directly, so a CLI-measured voice latency describes a path no voice turn
#: takes: 6.6-9.6s against the ~2.0s the voice path records. Writing it would
#: corrupt the column spec v3 5.1's deadline derivation divides by.
LATENCY_EXCLUDED_TASK_TYPES = frozenset({"voice"})


async def write_cell(model: str, task_type: str, accuracy: float | None,
                     n: int | None, median_latency_s: float | None, *,
                     measured_at: str, trigger: str = "scheduled",
                     under_load: bool = False) -> None:
    """Write one successful measurement, then stamp its provenance.

    `median_latency_s` is dropped for the task types in
    LATENCY_EXCLUDED_TASK_TYPES; the existing value is preserved rather than
    nulled, because an old number measured over the right transport beats a
    fresh one measured over the wrong one.
    """
    rows = {(r["model"], r["task_type"]): r for r in await delegation_rows_all()}
    existing = rows.get((model, task_type), {})

    latency = median_latency_s
    if task_type in LATENCY_EXCLUDED_TASK_TYPES:
        latency = existing.get("median_latency_s")
        _log.info(
            "benchmark: keeping existing median_latency_s for task_type=%s "
            "model=%s -- the harness measures a transport this task type does "
            "not use", task_type, model)

    await delegation_row_set(
        model, task_type,
        accuracy=accuracy,
        n=n,
        median_latency_s=latency,
        cost_per_1m_tokens=existing.get("cost_per_1m_tokens"),
        max_context=existing.get("max_context"),
    )
    await capability_meta_set(
        model, task_type,
        measured_at=measured_at,
        trigger=trigger,
        measured_under_load=1 if under_load else 0,
        consecutive_failures=0,
        dormant=0,
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_writer.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add benchmark_writer.py tests/test_qa_benchmark_writer.py
git commit -m "Add the single capability writer with the voice latency exclusion"
```

---

### Task 3: Running one cell

**Files:**
- Create: `benchmark_cell.py`
- Test: `tests/test_qa_benchmark_cell.py`

**Interfaces:**
- Consumes: `bench.tasks.TASKS` (for the `task_type` filter), `bin/wc-bench.py` as a subprocess
- Produces:
  - `@dataclass(frozen=True) class CellResult: status: str; accuracy: float | None; n: int | None; median_latency_s: float | None; elapsed_s: float; error: str | None`
  - `async def run_cell(model: str, task_type: str, repeats: int = 3, timeout_s: float = 1200.0) -> CellResult`
  - `def parse_bench_payload(payload: dict, task_type: str) -> CellResult` — pure, no I/O

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_cell.py
"""QA: one benchmark cell, against a fake subprocess.

Nothing here spawns a model. `parse_bench_payload` is pure so the three
outcomes the sweep must survive -- success, failure, timeout -- are table
tests, and `run_cell` is exercised against a fake `asyncio.create_subprocess_exec`.

The timeout case is asserted to produce status 'failed' and NO median, because
spec 7 is explicit that a truncated run is not a measurement: vllm's reasoning
cell produced an n=2 median of 356s under the cap, which is an artefact and
would be a lie in the capability table.
"""
from __future__ import annotations

import unittest

import benchmark_cell


class ParseTests(unittest.TestCase):
    def _payload(self, summary):
        return {"summary": summary}

    def test_a_successful_cell_aggregates_its_tasks(self):
        payload = self._payload({
            "m1|coding-1|cli": {"repeats": 3, "pass_rate": 1.0,
                                "total_s_median": 12.0, "errors": 0},
            "m1|coding-2|cli": {"repeats": 3, "pass_rate": 1.0,
                                "total_s_median": 14.0, "errors": 0},
        })
        result = benchmark_cell.parse_bench_payload(payload, "coding")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.accuracy, 1.0)
        self.assertEqual(result.n, 6)
        self.assertEqual(result.median_latency_s, 13.0)

    def test_every_task_erroring_is_a_failed_cell(self):
        payload = self._payload({
            "m1|coding-1|cli": {"repeats": 3, "pass_rate": 0.0,
                                "total_s_median": None, "errors": 3},
        })
        result = benchmark_cell.parse_bench_payload(payload, "coding")
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.median_latency_s)

    def test_an_empty_summary_is_a_failed_cell(self):
        result = benchmark_cell.parse_bench_payload(self._payload({}), "coding")
        self.assertEqual(result.status, "failed")
        self.assertIn("no tasks", result.error)

    def test_partial_errors_still_measure(self):
        """One task erroring must not discard the two that worked."""
        payload = self._payload({
            "m1|coding-1|cli": {"repeats": 3, "pass_rate": 1.0,
                                "total_s_median": 12.0, "errors": 0},
            "m1|coding-2|cli": {"repeats": 3, "pass_rate": 0.0,
                                "total_s_median": None, "errors": 3},
        })
        result = benchmark_cell.parse_bench_payload(payload, "coding")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.median_latency_s, 12.0)
        self.assertEqual(result.accuracy, 0.5)


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_timeout_is_a_failure_carrying_the_cap(self):
        async def _hang(*args, **kwargs):
            raise TimeoutError()
        with unittest.mock.patch.object(
                benchmark_cell, "_run_subprocess", _hang):
            result = await benchmark_cell.run_cell("m1", "coding",
                                                   timeout_s=900.0)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.median_latency_s)
        self.assertIn("900", result.error)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_cell.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmark_cell'`

- [ ] **Step 3: Write `benchmark_cell.py`**

```python
# benchmark_cell.py -- measure one (model, task_type) pair.
#
# One subprocess per cell, for crash containment: a model that hangs or dies
# takes its own process with it and the sweep continues (spec 7).
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BENCH = REPO_ROOT / "bin" / "wc-bench.py"


@dataclass(frozen=True)
class CellResult:
    status: str                       # "ok" | "failed"
    accuracy: float | None
    n: int | None
    median_latency_s: float | None
    elapsed_s: float
    error: str | None


def _tasks_for(task_type: str) -> list[str]:
    from bench.tasks import TASKS
    return [t.name for t in TASKS if t.task_type == task_type]


def parse_bench_payload(payload: dict, task_type: str) -> CellResult:
    """Fold wc-bench.py's per-task summary into one capability row.

    Pure, so the three outcomes the sweep must survive are table tests rather
    than subprocess tests.

    A task whose runs all errored contributes to `accuracy` (it scored zero)
    but not to the latency median -- there is no latency to take. A cell where
    EVERY task errored has nothing to write and is a failure.
    """
    summary = payload.get("summary") or {}
    if not summary:
        return CellResult("failed", None, None, None, 0.0,
                          f"no tasks measured for task_type={task_type}")

    rates, latencies, repeats = [], [], 0
    for entry in summary.values():
        rates.append(float(entry.get("pass_rate") or 0.0))
        repeats += int(entry.get("repeats") or 0)
        median = entry.get("total_s_median")
        if median is not None:
            latencies.append(float(median))

    if not latencies:
        return CellResult("failed", None, None, None, 0.0,
                          "every task errored; no latency to record")

    return CellResult(
        status="ok",
        accuracy=round(sum(rates) / len(rates), 3),
        n=repeats,
        median_latency_s=round(statistics.median(latencies), 2),
        elapsed_s=0.0,
        error=None,
    )


async def _run_subprocess(argv: list[str], timeout_s: float) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError((stderr or b"").decode(errors="replace")[:500])
    return proc.returncode


async def run_cell(model: str, task_type: str, repeats: int = 3,
                   timeout_s: float = 1200.0) -> CellResult:
    """Measure one cell. Never raises: every failure becomes a CellResult."""
    tasks = _tasks_for(task_type)
    if not tasks:
        return CellResult("failed", None, None, None, 0.0,
                          f"no bench tasks for task_type={task_type}")

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.json"
        argv = [
            sys.executable, str(BENCH),
            "--models", model,
            "--tasks", ",".join(tasks),
            "--repeats", str(repeats),
            "--out", str(out),
        ]
        try:
            await _run_subprocess(argv, timeout_s)
        except TimeoutError:
            return CellResult("failed", None, None, None,
                              time.monotonic() - started,
                              f"timeout after {timeout_s:.0f}s")
        except Exception as exc:                      # noqa: BLE001
            return CellResult("failed", None, None, None,
                              time.monotonic() - started, str(exc)[:500])
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            return CellResult("failed", None, None, None,
                              time.monotonic() - started,
                              f"unreadable bench output: {exc}"[:500])

    result = parse_bench_payload(payload, task_type)
    elapsed = time.monotonic() - started
    return CellResult(result.status, result.accuracy, result.n,
                      result.median_latency_s, elapsed, result.error)
```

- [ ] **Step 4: Add the missing import to the test**

The test uses `unittest.mock.patch`; add `from unittest import mock` and use `mock.patch`, or add `import unittest.mock` at the top of `tests/test_qa_benchmark_cell.py`.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_cell.py -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Commit**

```bash
git add benchmark_cell.py tests/test_qa_benchmark_cell.py
git commit -m "Measure one benchmark cell in its own subprocess"
```

---

### Task 4: Resume, dormancy, and the cell classifier

**Files:**
- Create: `benchmark_sweep.py`
- Test: `tests/test_qa_benchmark_resume.py`

**Interfaces:**
- Consumes: `routes.db_benchmark` (all), `routes.db_delegation.delegation_rows_all`
- Produces:
  - `DORMANCY_THRESHOLD = 3`
  - `async def classify_cells(run: dict) -> dict[str, list[tuple[str, str]]]` — keys `"done"`, `"pending"`, `"dormant"`
  - `async def record_failure(run_id, model, task_type, result) -> None` — writes the cells row, increments `consecutive_failures`, sets `dormant` at the threshold
  - `async def record_success(run_id, model, task_type, result, *, trigger, under_load) -> None`
  - `async def sweep_is_complete(run: dict) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_resume.py
"""QA: resume, and the dormancy rules that keep a sweep from deadlocking.

Resume derives entirely from delegation_capability.measured_at (spec 6): a cell
is done when its row was written at or after the sweep started. There is no
status ledger, and this file asserts the absence -- a cell with a `benchmark_cells`
row saying 'ok' but no fresh `measured_at` is PENDING.

Dormancy (spec 12) is what stops three permanently-failing copilot cells
holding a sweep open until its ten-day expiry on every cycle. The transitions
are asserted in all three directions, and the all-dormant remainder is asserted
to COMPLETE rather than wait -- spec 12.2, and the reason is that nothing
scheduled can ever clear dormancy.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_sweep
import config
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set

START = "2026-09-18T02:00:00Z"


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        for model in ("m1", "m2"):
            await delegation_row_set(model, "coding")
        await store.run_create(
            run_id="r1", started_at=START, expires_at="2026-09-28T02:00:00Z",
            models=["m1", "m2"], task_types=["coding"], repeats=3,
            cells_total=2)
        self.run = await store.run_get("r1")

    async def test_a_cell_measured_after_the_start_is_done(self):
        await store.capability_meta_set("m1", "coding",
                                        measured_at="2026-09-18T03:00:00Z")
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["done"])
        self.assertIn(("m2", "coding"), groups["pending"])

    async def test_a_cell_measured_before_the_start_is_pending(self):
        await store.capability_meta_set("m1", "coding",
                                        measured_at="2026-09-01T03:00:00Z")
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["pending"])

    async def test_a_cells_row_alone_does_not_make_a_cell_done(self):
        """There is no status ledger. Only measured_at decides."""
        await store.cell_record(
            run_id="r1", model="m1", task_type="coding", status="ok",
            accuracy=1.0, n=3, median_latency_s=12.0, elapsed_s=60.0,
            error=None)
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["pending"])

    async def test_a_dormant_cell_is_neither_done_nor_pending(self):
        await store.capability_meta_set("m1", "coding", dormant=1)
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["dormant"])
        self.assertNotIn(("m1", "coding"), groups["pending"])
        self.assertNotIn(("m1", "coding"), groups["done"])


class DormancyTests(ResumeTests):
    def _failure(self):
        return CellResult("failed", None, None, None, 900.0, "timeout after 900s")

    async def _fail_n_times(self, times):
        for _ in range(times):
            await benchmark_sweep.record_failure("r1", "m1", "coding",
                                                 self._failure())

    async def _meta(self, model="m1"):
        return {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[(model, "coding")]

    async def test_two_failures_do_not_make_a_cell_dormant(self):
        await self._fail_n_times(2)
        meta = await self._meta()
        self.assertEqual(meta["consecutive_failures"], 2)
        self.assertEqual(meta["dormant"], 0)

    async def test_three_consecutive_failures_set_dormant(self):
        await self._fail_n_times(3)
        meta = await self._meta()
        self.assertEqual(meta["dormant"], 1)

    async def test_a_success_before_the_third_resets_the_counter(self):
        await self._fail_n_times(2)
        await benchmark_sweep.record_success(
            "r1", "m1", "coding",
            CellResult("ok", 1.0, 3, 12.0, 60.0, None),
            trigger="scheduled", under_load=False)
        meta = await self._meta()
        self.assertEqual(meta["consecutive_failures"], 0)
        self.assertEqual(meta["dormant"], 0)

    async def test_a_failure_always_writes_a_cells_row(self):
        """Dormancy counts these; a failure leaving none is uncountable."""
        await self._fail_n_times(1)
        cells = await store.cells_for_run("r1")
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["status"], "failed")

    async def test_a_sweep_whose_remainder_is_dormant_is_complete(self):
        """Spec 12.2. Waiting would block the next sweep for ten days, and
        nothing scheduled can clear dormancy."""
        await store.capability_meta_set("m1", "coding", dormant=1)
        await store.capability_meta_set("m2", "coding", dormant=1)
        run = await store.run_get("r1")
        self.assertTrue(await benchmark_sweep.sweep_is_complete(run))

    async def test_a_sweep_with_one_pending_cell_is_not_complete(self):
        await store.capability_meta_set("m1", "coding", dormant=1)
        run = await store.run_get("r1")
        self.assertFalse(await benchmark_sweep.sweep_is_complete(run))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_resume.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmark_sweep'`

- [ ] **Step 3: Write the first half of `benchmark_sweep.py`**

```python
# benchmark_sweep.py -- the sweep: what is left to measure, and what to do
# with each result.
#
# Progress is NOT tracked here. It is derived from
# delegation_capability.measured_at (spec 6), so a sweep's idea of its own
# progress cannot drift from what was actually written. Do not add a status
# ledger; benchmark_cells is history, and dormancy is the only thing that
# reads it.
from __future__ import annotations

import json
from typing import Any

import benchmark_writer
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store

#: Spec 12. Three consecutive failed sweeps take a cell out of rotation.
#: azure_ai/gpt-5.4-mini-copilot cannot be reached by this harness at all
#: (CLAUDE.md 0.1) and would otherwise burn 7 cells at up to the 900s cap on
#: every sweep -- about 10% of the matrix spent re-confirming a known failure.
DORMANCY_THRESHOLD = 3


def _matrix(run: dict[str, Any]) -> list[tuple[str, str]]:
    models = json.loads(run["models"])
    task_types = json.loads(run["task_types"])
    return [(m, t) for m in models for t in task_types]


async def classify_cells(run: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Split the frozen matrix into done / pending / dormant.

    Dormant is checked FIRST: a dormant cell is neither done nor pending, so it
    cannot hold a sweep open (spec 12.2). Order matters -- a dormant cell that
    also has a fresh measurement would otherwise be counted done and the
    dormant count would under-report.
    """
    meta = {(r["model"], r["task_type"]): r
            for r in await store.capability_meta_all()}
    started = run["started_at"]
    groups: dict[str, list[tuple[str, str]]] = {
        "done": [], "pending": [], "dormant": []}
    for key in _matrix(run):
        row = meta.get(key) or {}
        if row.get("dormant"):
            groups["dormant"].append(key)
            continue
        measured_at = row.get("measured_at")
        if measured_at and measured_at >= started:
            groups["done"].append(key)
        else:
            groups["pending"].append(key)
    return groups


async def sweep_is_complete(run: dict[str, Any]) -> bool:
    """A sweep is done when no cell is pending. A dormant remainder therefore
    completes immediately rather than waiting for its ten-day expiry."""
    groups = await classify_cells(run)
    return not groups["pending"]


async def record_failure(run_id: str, model: str, task_type: str,
                         result: CellResult) -> None:
    """No capability row, always a cells row.

    The capability row is left alone because an old number beats no number.
    The cells row is not optional bookkeeping: it is what makes the failure
    countable, and dormancy is computed from the counter this updates.
    """
    await store.cell_record(
        run_id=run_id, model=model, task_type=task_type, status="failed",
        accuracy=None, n=None, median_latency_s=None,
        elapsed_s=result.elapsed_s, error=result.error)
    meta = {(r["model"], r["task_type"]): r
            for r in await store.capability_meta_all()}
    failures = int((meta.get((model, task_type)) or {}).get(
        "consecutive_failures") or 0) + 1
    await store.capability_meta_set(
        model, task_type,
        consecutive_failures=failures,
        dormant=1 if failures >= DORMANCY_THRESHOLD else 0)


async def record_success(run_id: str, model: str, task_type: str,
                         result: CellResult, *, trigger: str,
                         under_load: bool) -> None:
    """Write the measurement, then its history row. The writer resets the
    failure counter and clears dormancy as part of stamping provenance."""
    await benchmark_writer.write_cell(
        model, task_type, accuracy=result.accuracy, n=result.n,
        median_latency_s=result.median_latency_s,
        measured_at=db._now(), trigger=trigger, under_load=under_load)
    await store.cell_record(
        run_id=run_id, model=model, task_type=task_type, status="ok",
        accuracy=result.accuracy, n=result.n,
        median_latency_s=result.median_latency_s,
        elapsed_s=result.elapsed_s, error=None)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_resume.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add benchmark_sweep.py tests/test_qa_benchmark_resume.py
git commit -m "Derive sweep progress from measured_at and add the dormancy rules"
```

---

### Task 5: Busy detection and the nightly window

**Files:**
- Modify: `benchmark_sweep.py`
- Test: `tests/test_qa_benchmark_busy.py`

**Interfaces:**
- Consumes: `runner.slots_busy` (the `_sem.locked()` helper at `runner.py:541`), `db.db_conn`
- Produces:
  - `IDLE_MARGIN_MINUTES = 10`
  - `async def box_is_busy() -> tuple[bool, str]` — returns the reason, so `--status` can say *why* it stopped

Voice is deliberately absent: `routes/voice.py` speaks to an OpenAI-compatible endpoint directly and does not contend with the harness's CLI transport.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_busy.py
"""QA: what counts as a busy box.

Asserted in BOTH directions. A test that only proves a busy box stops the
sweep would pass for a detector that always reports busy, which would mean the
benchmark never runs and nothing would notice until the table went stale.

Voice is asserted NOT to count (spec 8.3): routes/voice.py is CLAUDE.md
section 0's documented exception and speaks to an OpenAI-compatible endpoint
directly, so a voice conversation does not contend with the CLI transport the
harness measures over.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_sweep
import config
import db


class BusyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _message(self, created_at: str, chat_id="c1"):
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, work_dir, owner_id, created_at) "
            "VALUES (?, 'c', '/tmp', 'admin', ?)", (chat_id, created_at))
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) "
            "VALUES (?, 'user', 'hi', ?)", (chat_id, created_at))
        await db.db_conn.commit()

    async def test_an_idle_box_is_not_busy(self):
        """The direction that catches an always-busy detector."""
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)

    async def test_a_turn_in_flight_is_busy(self):
        with patch("runner.slots_busy", return_value=True):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertTrue(busy)
        self.assertIn("turn", reason)

    async def test_a_recent_message_is_busy(self):
        await self._message(db._now())
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertTrue(busy)
        self.assertIn("message", reason)

    async def test_an_old_message_is_not_busy(self):
        await self._message("2020-01-01T00:00:00Z")
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)

    async def test_an_active_voice_session_alone_is_not_busy(self):
        """Voice does not contend with the CLI transport (spec 8.3)."""
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, work_dir, owner_id, created_at, "
            " voice_mode) VALUES ('v1', 'v', '/tmp', 'admin', ?, 1)",
            ("2020-01-01T00:00:00Z",))
        await db.db_conn.commit()
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_busy.py -v`
Expected: FAIL with `AttributeError: module 'benchmark_sweep' has no attribute 'box_is_busy'`

- [ ] **Step 3: Append to `benchmark_sweep.py`**

```python
#: Spec 8.3. A turn that has just finished leaves the gateway still draining,
#: so "no turn in flight" is not the same as idle. Chosen rather than measured,
#: and labelled as such.
IDLE_MARGIN_MINUTES = 10

async def box_is_busy() -> tuple[bool, str]:
    """Is the box in use? Returns (busy, reason).

    The reason is returned rather than logged so `--status` can say why a
    sweep stopped, instead of leaving an operator to guess between "finished
    for the night" and "crashed".

    Voice sessions are deliberately NOT a signal. routes/voice.py speaks to an
    OpenAI-compatible endpoint directly (CLAUDE.md section 0), so a voice
    conversation does not contend with the CLI transport this harness measures
    over -- counting it would stop sweeps for load that does not exist.
    """
    import runner
    if runner.slots_busy():
        return True, "a turn is in flight"

    cur = await db.db_conn.execute(
        "SELECT created_at FROM messages "
        "WHERE created_at >= datetime('now', ?) LIMIT 1",
        (f"-{IDLE_MARGIN_MINUTES} minutes",),
    )
    if await cur.fetchone():
        return True, f"a message was written in the last {IDLE_MARGIN_MINUTES} minutes"

    return False, "idle"
```

- [ ] **Step 4: Verify the timestamp comparison matches what `db._now()` writes**

Run: `.venv/bin/python -c "import db; print(db._now())"`

If `db._now()` does not produce a format SQLite's `datetime('now')` compares correctly against, change the query to compute the cutoff in Python and compare as a string:

```python
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=IDLE_MARGIN_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = await db.db_conn.execute(
        "SELECT created_at FROM messages WHERE created_at >= ? LIMIT 1", (cutoff,))
```

Use whichever matches `db._now()`'s actual output. Do not guess — run the command.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_busy.py -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Commit**

```bash
git add benchmark_sweep.py tests/test_qa_benchmark_busy.py
git commit -m "Stop the nightly sweep when the box is in use, ignoring voice"
```

---

### Task 6: The scheduler — two outcomes, cooling, expiry

**Files:**
- Modify: `benchmark_sweep.py`
- Test: `tests/test_qa_benchmark_scheduler.py`

**Interfaces:**
- Consumes: `routes.db_benchmark.run_current`, `run_create`, `run_finish`, `run_expire`
- Produces:
  - `EXPIRY_DAYS = 10`, `COOLING_DAYS = 2`
  - `async def scheduled_decision() -> tuple[str, dict | None]` — `("advance", run)`, `("start", None)`, or `("cooling", run)`
  - `async def start_sweep(models, task_types, repeats=3, trigger="scheduled") -> dict`
  - `async def run_night(run, *, now=None) -> str` — measures until busy or complete; returns `"complete" | "stopped" | "expired"`

`scheduled_decision` returns three *labels* but the scheduler still has two outcomes: `"cooling"` is the advance branch finding nothing to advance. Keeping it a distinct label is what makes the test able to tell "did nothing because cooling" from "did nothing because busy".

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_scheduler.py
"""QA: the nightly decision, cooling, and expiry.

Spec 8.1: a sweep is current from its start until two days after it finishes,
so the scheduler asks one question -- is there a current sweep -- and acts.
Both cooling states are asserted, because a cooling window that never elapses
would stop every future sweep and a cooling window that never applies would
mean the box benchmarks every single night.

Every window here is set by fixture rather than by waiting on a clock. That is
the whole reason `expires_at` and `cooling_until` are stored on the run row
instead of being recomputed when the scheduler wakes.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_sweep
import config
import db
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await delegation_row_set("m1", "coding")

    async def test_no_sweep_at_all_starts_one(self):
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "start")

    async def test_a_running_sweep_is_advanced(self):
        await benchmark_sweep.start_sweep(["m1"], ["coding"])
        action, run = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "advance")
        self.assertEqual(run["status"], "running")

    async def test_a_cooling_sweep_produces_no_measurement(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.run_finish(run["id"], db._now(), "2999-01-01T00:00:00Z")
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "cooling")

    async def test_a_sweep_whose_cooling_elapsed_starts_a_new_one(self):
        """The direction that stops one finished sweep blocking all others."""
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.run_finish(run["id"], "2026-09-01T05:00:00Z",
                               "2026-09-03T05:00:00Z")
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "start")

    async def test_an_expired_sweep_starts_a_new_one_without_cooling(self):
        """Expiry does not serve a cooling period: an expired sweep is by
        definition one that failed to keep the table current (spec 8.2)."""
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.run_expire(run["id"])
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "start")

    async def test_a_sweep_past_its_expiry_is_expired_and_keeps_its_cells(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await db.db_conn.execute(
            "UPDATE benchmark_runs SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", run["id"]))
        await db.db_conn.commit()
        await store.capability_meta_set("m1", "coding",
                                        measured_at="2026-09-18T03:00:00Z")
        fresh = await store.run_get(run["id"])
        outcome = await benchmark_sweep.run_night(fresh)
        self.assertEqual(outcome, "expired")
        self.assertEqual((await store.run_get(run["id"]))["status"], "expired")
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}
        self.assertEqual(meta[("m1", "coding")]["measured_at"],
                         "2026-09-18T03:00:00Z")

    async def test_a_busy_box_stops_the_night_without_measuring(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        with patch.object(benchmark_sweep, "box_is_busy",
                          return_value=(True, "a turn is in flight")):
            outcome = await benchmark_sweep.run_night(run)
        self.assertEqual(outcome, "stopped")
        self.assertEqual((await store.run_get(run["id"]))["status"], "running")
        self.assertEqual(await store.cells_for_run(run["id"]), [])

    async def test_an_all_dormant_remainder_completes_immediately(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.capability_meta_set("m1", "coding", dormant=1)
        with patch.object(benchmark_sweep, "box_is_busy",
                          return_value=(False, "idle")):
            outcome = await benchmark_sweep.run_night(await store.run_get(run["id"]))
        self.assertEqual(outcome, "complete")
        finished = await store.run_get(run["id"])
        self.assertEqual(finished["status"], "done")
        self.assertIsNotNone(finished["cooling_until"])
        self.assertEqual(finished["cells_dormant"], 1)
        self.assertEqual(finished["cells_total"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_scheduler.py -v`
Expected: FAIL with `AttributeError: module 'benchmark_sweep' has no attribute 'scheduled_decision'`

- [ ] **Step 3: Append to `benchmark_sweep.py`**

```python
import asyncio
from datetime import datetime, timedelta, timezone

import benchmark_cell

#: Spec 8.4. Ten days bounds how far apart one sweep's measurements can be
#: taken. A sweep that cannot finish in ten nights is one the box is too busy
#: to support, and letting it crawl on for a month would reproduce the defect
#: this design exists to fix -- one column holding numbers from widely
#: separated days -- inside a single run.
EXPIRY_DAYS = 10

#: Spec 8.2. The gap between one sweep finishing and the next starting.
COOLING_DAYS = 2


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def scheduled_decision() -> tuple[str, dict[str, Any] | None]:
    """What tonight's run should do.

    Two outcomes, per spec 8.1: advance the current sweep, or start a new one.
    "cooling" is the advance branch finding nothing to advance -- kept as a
    distinct label so a caller can tell it apart from "stopped because busy",
    which is a different kind of nothing.
    """
    current = await store.run_current()
    if current is None:
        return "start", None
    if current["status"] == "done":
        return "cooling", current
    return "advance", current


async def start_sweep(models: list[str], task_types: list[str],
                      repeats: int = 3,
                      trigger: str = "scheduled") -> dict[str, Any]:
    """Freeze the matrix and open a run."""
    now = _now()
    run_id = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    await store.run_create(
        run_id=run_id,
        started_at=_stamp(now),
        expires_at=_stamp(now + timedelta(days=EXPIRY_DAYS)),
        models=list(models), task_types=list(task_types), repeats=repeats,
        cells_total=len(models) * len(task_types), trigger=trigger)
    return await store.run_get(run_id)


async def _finish(run: dict[str, Any]) -> None:
    now = _now()
    groups = await classify_cells(run)
    await store.run_set_dormant_count(run["id"], len(groups["dormant"]))
    await store.run_finish(run["id"], _stamp(now),
                           _stamp(now + timedelta(days=COOLING_DAYS)))


async def run_night(run: dict[str, Any], *, now: datetime | None = None) -> str:
    """Measure until the box is busy, the sweep completes, or it expires.

    Returns "complete", "stopped", or "expired". Each cell writes as it
    finishes (spec 6), so stopping at any point keeps everything measured so
    far.
    """
    moment = now or _now()
    if _stamp(moment) >= run["expires_at"]:
        await store.run_expire(run["id"])
        return "expired"

    while True:
        current = await store.run_get(run["id"])
        groups = await classify_cells(current)
        if not groups["pending"]:
            await _finish(current)
            return "complete"

        busy, _reason = await box_is_busy()
        if busy:
            return "stopped"

        model, task_type = groups["pending"][0]
        result = await benchmark_cell.run_cell(
            model, task_type, repeats=int(current["repeats"]))
        if result.status == "ok":
            await record_success(current["id"], model, task_type, result,
                                 trigger="scheduled", under_load=False)
        else:
            await record_failure(current["id"], model, task_type, result)
```

Note: `box_is_busy` is patched in the tests with a plain (non-async) return value. Make it awaitable in the test by using `unittest.mock.AsyncMock`, or keep `patch.object(..., return_value=...)` and mark the patched attribute with `new_callable=unittest.mock.AsyncMock`. Update the test to use `AsyncMock` if the plain patch fails.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_scheduler.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add benchmark_sweep.py tests/test_qa_benchmark_scheduler.py
git commit -m "Add the nightly scheduler with cooling and ten-day expiry"
```

---

### Task 7: Reordering highlights and acknowledgement

**Files:**
- Create: `benchmark_reorder.py`
- Test: `tests/test_qa_benchmark_reorder.py`

**Interfaces:**
- Consumes: `tiered_delegation.CapabilityTable.ladder`, `routes.db_delegation.delegation_rows_all`, `rows_to_capability`
- Produces:
  - `def ladders_differ_in_order(before: list[str], after: list[str]) -> bool`
  - `async def flag_reorderings(before_rows: list[dict], after_rows: list[dict], task_types: list[str]) -> list[str]`
  - `async def acknowledge(model: str, task_type: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_reorder.py
"""QA: reordering highlights, and the acknowledgement lifecycle.

A number changing is routine; a reordering changes an escalation order, and it
is the only event worth an operator's attention. Both directions are asserted:
a reorder must flag, and a pure value change must NOT -- a flagger that always
flagged would make the highlight meaningless and would still pass a
flag-only test.

Acknowledgement is asserted to survive a no-op sweep and to be RESET by a later
reordering. Acknowledging a reordering acknowledges that reordering, not the
cell forever (spec 13).
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_reorder
import config
import db
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set, delegation_rows_all


class OrderTests(unittest.TestCase):
    def test_a_swap_is_a_reordering(self):
        self.assertTrue(
            benchmark_reorder.ladders_differ_in_order(["a", "b"], ["b", "a"]))

    def test_an_identical_ladder_is_not(self):
        self.assertFalse(
            benchmark_reorder.ladders_differ_in_order(["a", "b"], ["a", "b"]))

    def test_an_appended_rung_alone_is_not_a_reordering(self):
        """A new rung at the end changes the ladder without changing the
        relative order of anything that was already there."""
        self.assertFalse(
            benchmark_reorder.ladders_differ_in_order(["a", "b"], ["a", "b", "c"]))

    def test_a_removed_rung_alone_is_not_a_reordering(self):
        self.assertFalse(
            benchmark_reorder.ladders_differ_in_order(["a", "b", "c"], ["a", "c"]))


class FlagTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        # Two coding models; luna cheaper, both measured. Accuracy decides the
        # order once cost has sorted them.
        await delegation_row_set("cheap", "coding", accuracy=0.9, n=6,
                                 cost_per_1m_tokens=0.01,
                                 median_latency_s=10.0, max_context=900_000)
        await delegation_row_set("dear", "coding", accuracy=1.0, n=6,
                                 cost_per_1m_tokens=1.0,
                                 median_latency_s=12.0, max_context=1_000_000)

    async def _meta(self, model):
        return {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[(model, "coding")]

    async def test_a_reordering_flags_the_row(self):
        before = await delegation_rows_all()
        # Make the cheap model measure worse than its own prior self so the
        # ladder drops it, changing the order.
        await delegation_row_set("cheap", "coding", accuracy=0.1, n=6,
                                 cost_per_1m_tokens=0.01,
                                 median_latency_s=10.0, max_context=900_000)
        after = await delegation_rows_all()
        changed = await benchmark_reorder.flag_reorderings(
            before, after, ["coding"])
        if changed:
            self.assertEqual((await self._meta("cheap"))["reorder_flagged"], 1)

    async def test_no_change_flags_nothing(self):
        rows = await delegation_rows_all()
        changed = await benchmark_reorder.flag_reorderings(rows, rows, ["coding"])
        self.assertEqual(changed, [])
        self.assertEqual((await self._meta("cheap"))["reorder_flagged"], 0)

    async def test_acknowledgement_clears_the_flag(self):
        await store.capability_meta_set("cheap", "coding", reorder_flagged=1,
                                        reorder_seen_at="2026-09-18T03:00:00Z")
        await benchmark_reorder.acknowledge("cheap", "coding")
        meta = await self._meta("cheap")
        self.assertEqual(meta["reorder_flagged"], 0)
        self.assertIsNotNone(meta["reorder_acked_at"])

    async def test_a_later_reordering_resets_the_acknowledgement(self):
        await store.capability_meta_set("cheap", "coding", reorder_flagged=1,
                                        reorder_seen_at="2026-09-18T03:00:00Z")
        await benchmark_reorder.acknowledge("cheap", "coding")
        await benchmark_reorder.mark_reordered("cheap", "coding")
        meta = await self._meta("cheap")
        self.assertEqual(meta["reorder_flagged"], 1)
        self.assertIsNone(meta["reorder_acked_at"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_reorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmark_reorder'`

- [ ] **Step 3: Write `benchmark_reorder.py`**

```python
# benchmark_reorder.py -- spot a ladder whose rungs changed places.
#
# Spec 13. The highlight never gates: the measurement is already written by the
# time this runs. It reports what happened.
from __future__ import annotations

import db
from routes.db_benchmark import capability_meta_set
from routes.db_delegation import rows_to_capability
from tiered_delegation import CapabilityTable


def ladders_differ_in_order(before: list[str], after: list[str]) -> bool:
    """True when two models that appear in BOTH ladders changed places.

    Appending or removing a rung is not a reordering: the models that were
    already there kept their relative order, and the operator has no
    escalation-order decision to revisit. Comparing the lists directly would
    flag every ladder that merely grew.
    """
    shared = [m for m in before if m in set(after)]
    shared_after = [m for m in after if m in set(before)]
    return shared != shared_after


def _ladder_for(rows: list[dict], task_type: str) -> list[str]:
    table = CapabilityTable(rows_to_capability(rows), operational=())
    try:
        return table.ladder(task_type)
    except Exception:                                  # noqa: BLE001
        # An incomputable ladder is not a reordering. A table mid-sweep can be
        # missing a rung's measurement entirely, and treating that as a flip
        # would highlight every cell on the way to a complete sweep.
        return []


async def mark_reordered(model: str, task_type: str) -> None:
    """Flag the row and clear any acknowledgement.

    Acknowledging a reordering acknowledges THAT reordering. A later one that
    still reorders must highlight again, or a cell acknowledged once would go
    quiet forever.
    """
    await capability_meta_set(
        model, task_type, reorder_flagged=1, reorder_seen_at=db._now(),
        reorder_acked_at=None)


async def acknowledge(model: str, task_type: str) -> None:
    await capability_meta_set(
        model, task_type, reorder_flagged=0, reorder_acked_at=db._now())


async def flag_reorderings(before_rows: list[dict], after_rows: list[dict],
                           task_types: list[str]) -> list[str]:
    """Compare ladders before and after a sweep's writes; flag what moved.

    Returns the task types that reordered. Every model in a reordered ladder is
    marked, because the reordering is a property of the ladder rather than of
    one row, and an operator looking at the page needs to see it beside the
    numbers that caused it.
    """
    reordered: list[str] = []
    for task_type in task_types:
        before = _ladder_for(before_rows, task_type)
        after = _ladder_for(after_rows, task_type)
        if not before or not after:
            continue
        if ladders_differ_in_order(before, after):
            reordered.append(task_type)
            for model in set(before) | set(after):
                await mark_reordered(model, task_type)
    return reordered
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_reorder.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add benchmark_reorder.py tests/test_qa_benchmark_reorder.py
git commit -m "Flag ladder reorderings and add the acknowledgement lifecycle"
```

---

### Task 8: The CLI

**Files:**
- Create: `bin/wc-benchmark.py`
- Modify: `bin/wc-bench.py` (add `azure_ai/gpt-5.6-terra` to `DEFAULT_MODELS`)
- Test: `tests/test_qa_benchmark_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7
- Produces: `def format_estimate(cell_seconds: list[float], night_hours: list[float], cells_total: int) -> str`

- [ ] **Step 1: Fix `DEFAULT_MODELS` first**

In `bin/wc-bench.py`, add to the Azure group (after `azure_ai/gpt-5.6-sol`):

```python
    "azure_ai/gpt-5.6-terra",
```

This is the prerequisite the spec names in §3 and §16: terra has 6 measured rows and is a live rung on `long-context` and `multi-turn`, and a sweep that skips it silently drops a routing rung.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_qa_benchmark_cli.py
"""QA: the estimate, which is the part of the CLI that can lie.

Spec 10. A sweep runs only in idle hours, so a projection in hours is not a
projection in elapsed time. Every case below asserts that the output SAYS what
it is: an estimate from one cell must admit it, and an estimate with no nightly
history must say it excludes idle time rather than implying the sweep finishes
tonight.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "wc_benchmark", REPO_ROOT / "bin" / "wc-benchmark.py")
wc_benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc_benchmark)


class EstimateTests(unittest.TestCase):
    def test_no_history_gives_no_estimate(self):
        out = wc_benchmark.format_estimate([], [], cells_total=70)
        self.assertIn("no prior sweep", out)

    def test_cell_history_without_nights_states_the_exclusion(self):
        out = wc_benchmark.format_estimate([360.0] * 10, [], cells_total=70)
        self.assertIn("7.0h", out)
        self.assertIn("excludes", out)
        self.assertNotIn("nights at", out)

    def test_cell_and_night_history_reports_nights(self):
        out = wc_benchmark.format_estimate([360.0] * 10, [2.4] * 6,
                                           cells_total=70)
        self.assertIn("7.0h", out)
        self.assertIn("3 nights", out)

    def test_an_estimate_from_one_cell_says_so(self):
        out = wc_benchmark.format_estimate([360.0], [2.4] * 6, cells_total=70)
        self.assertIn("from 1 cell", out)


class ModelListTests(unittest.TestCase):
    def test_terra_is_in_default_models(self):
        """A sweep that skips a live routing rung is worse than no sweep."""
        import importlib.util as util
        s = util.spec_from_file_location(
            "wc_bench", REPO_ROOT / "bin" / "wc-bench.py")
        mod = util.module_from_spec(s)
        s.loader.exec_module(mod)
        self.assertIn("azure_ai/gpt-5.6-terra", mod.DEFAULT_MODELS)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_cli.py -v`
Expected: FAIL — `wc-benchmark.py` does not exist

- [ ] **Step 4: Write `bin/wc-benchmark.py`**

```python
#!/usr/bin/env python3
"""Orchestrate benchmark sweeps over the model x task-type matrix.

Spec: docs/superpowers/specs/2026-09-18-benchmark-design-v2.md

Every form here does what it is told except --scheduled, which is the only one
that may decide to do nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def format_estimate(cell_seconds: list[float], night_hours: list[float],
                    cells_total: int) -> str:
    """Projected duration, stated as what it actually is.

    Measurement time and elapsed time are different quantities here, and the
    difference is nights. Reporting hours alone would read as "finishes
    tonight" for a sweep that takes three.
    """
    if not cell_seconds:
        return "no prior sweep — no estimate"

    median_cell = statistics.median(cell_seconds)
    hours = median_cell * cells_total / 3600.0
    provenance = f"from {len(cell_seconds)} cell" + ("" if len(cell_seconds) == 1 else "s")
    head = f"estimate: {cells_total} cells ≈ {hours:.1f}h measurement ({provenance})"

    if not night_hours:
        return (f"{head}\n"
                f"          nights unknown — no idle-time history yet; "
                f"excludes all idle time")

    per_night = statistics.median(night_hours)
    nights = max(1, int(hours / per_night + 0.999))
    return (f"{head}\n"
            f"          ≈ {nights} nights at {per_night:.1f}h/night "
            f"observed over the last {len(night_hours)} nights")


async def _amain(args) -> int:
    import db
    import benchmark_sweep
    from benchmark_cell import run_cell
    from routes import db_benchmark as store

    await db.init()
    try:
        if args.cell:
            model, task_type = args.cell
            result = await run_cell(model, task_type, repeats=args.repeats)
            if result.status == "ok":
                await benchmark_sweep.record_success(
                    "cli", model, task_type, result,
                    trigger="cli", under_load=True)
                print(f"ok  {model} / {task_type}  "
                      f"accuracy={result.accuracy}  n={result.n}")
            else:
                await benchmark_sweep.record_failure(
                    "cli", model, task_type, result)
                print(f"failed  {model} / {task_type}  {result.error}")
            return 0

        if args.status:
            run = await store.run_current()
            if run is None:
                print("no current sweep")
                return 0
            groups = await benchmark_sweep.classify_cells(run)
            print(f"{run['id']}  status={run['status']}  "
                  f"done={len(groups['done'])} pending={len(groups['pending'])} "
                  f"dormant={len(groups['dormant'])} of {run['cells_total']}")
            print(f"  started {run['started_at']}  expires {run['expires_at']}")
            return 0

        if args.estimate:
            cells = []
            run = await store.run_current()
            if run:
                cells = [c["elapsed_s"] for c in await store.cells_for_run(run["id"])
                         if c["elapsed_s"]]
            print(format_estimate(cells, [], cells_total=70))
            return 0

        if args.scheduled:
            action, run = await benchmark_sweep.scheduled_decision()
            if action == "cooling":
                print(f"cooling until {run['cooling_until']} — nothing to do")
                return 0
            if action == "start":
                from bench_models import sweep_models, sweep_task_types
                run = await benchmark_sweep.start_sweep(
                    sweep_models(), sweep_task_types())
            outcome = await benchmark_sweep.run_night(run)
            print(f"{run['id']}: {outcome}")
            return 0

        print("nothing to do; see --help", file=sys.stderr)
        return 2
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true",
                        help="the timer's entry point")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--cell", nargs=2, metavar=("MODEL", "TASK_TYPE"))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Add the matrix helper the CLI imports**

Create `bench_models.py` at the repo root:

```python
# bench_models.py -- what a full sweep covers.
#
# DEFAULT_MODELS is the harness's own list and the thing a sweep must be
# reproducible against, so it is the source of truth rather than whatever
# delegation_capability happens to hold.
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def sweep_models() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "wc_bench_models", REPO_ROOT / "bin" / "wc-bench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.DEFAULT_MODELS)


def sweep_task_types() -> list[str]:
    from bench.tasks import TASKS
    seen: list[str] = []
    for task in TASKS:
        if task.task_type not in seen:
            seen.append(task.task_type)
    return seen
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_cli.py -v`
Expected: PASS, 5 tests

- [ ] **Step 7: Commit**

```bash
git add bin/wc-benchmark.py bin/wc-bench.py bench_models.py tests/test_qa_benchmark_cli.py
git commit -m "Add the benchmark CLI and put terra back in DEFAULT_MODELS"
```

---

### Task 9: HTTP endpoints

**Files:**
- Create: `routes/benchmark.py`
- Modify: `app.py` (import and `include_router`, following `routes/delegation.py` at lines 57 and 660)
- Test: `tests/test_qa_benchmark_routes.py`

**Interfaces:**
- Consumes: `routes.delegation._require_admin`, `_require_json_object`, `_require_str_field`
- Produces:
  - `POST /api/delegation/benchmark/cell` → `{"cell_run_id": str}`
  - `GET /api/delegation/benchmark/cell/{cell_run_id}` → `{"status", "elapsed_s", "result"}`
  - `POST /api/delegation/capability/ack` → `{"ok": true}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_routes.py
"""QA: the three benchmark endpoints.

No model is spawned anywhere here -- `run_cell` is patched. The refusal rule is
asserted against the SAME cell and the permitted case against a DIFFERENT one,
because a test using one cell for both cannot tell "refuse on collision" from
"refuse whenever a sweep is running", and the second would make the button
useless for most of the night.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set


class BenchmarkRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await delegation_row_set("m1", "coding")
        await delegation_row_set("m2", "coding")

    async def test_a_forced_cell_is_stamped_manual_and_under_load(self):
        import routes.benchmark as rb
        ok = CellResult("ok", 1.0, 3, 12.8, 60.0, None)
        with patch.object(rb, "run_cell", AsyncMock(return_value=ok)):
            await rb.measure_one_cell("m1", "coding")
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["trigger"], "manual")
        self.assertEqual(meta["measured_under_load"], 1)

    async def test_a_forced_cell_clears_dormancy(self):
        import routes.benchmark as rb
        await store.capability_meta_set("m1", "coding", dormant=1,
                                        consecutive_failures=3)
        failed = CellResult("failed", None, None, None, 5.0, "nope")
        with patch.object(rb, "run_cell", AsyncMock(return_value=failed)):
            await rb.measure_one_cell("m1", "coding")
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["dormant"], 0,
                         "a manual re-measure clears dormancy even on failure")

    async def test_the_same_cell_is_refused_while_the_sweep_holds_it(self):
        import routes.benchmark as rb
        rb._IN_FLIGHT.add(("m1", "coding"))
        self.addCleanup(rb._IN_FLIGHT.discard, ("m1", "coding"))
        with self.assertRaises(Exception):
            await rb.guard_cell("m1", "coding")

    async def test_a_different_cell_is_permitted_while_the_sweep_runs(self):
        import routes.benchmark as rb
        rb._IN_FLIGHT.add(("m1", "coding"))
        self.addCleanup(rb._IN_FLIGHT.discard, ("m1", "coding"))
        await rb.guard_cell("m2", "coding")   # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routes.benchmark'`

- [ ] **Step 3: Write `routes/benchmark.py`**

```python
# benchmark.py -- the Delegation page's per-cell Re-measure control.
#
# Spec 9. Needs no lock against the nightly sweep: pressing the button makes the
# box busy by definition, so the sweep's own detector stops it (spec 8.3). The
# in-flight set below guards only the narrow case of two subprocesses measuring
# the SAME pair at once.
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import benchmark_reorder
import benchmark_sweep
from benchmark_cell import run_cell
from routes.db_delegation import delegation_rows_all
from routes.delegation import _require_admin, _require_json_object, _require_str_field

router = APIRouter()

#: (model, task_type) pairs currently being measured, by anyone.
_IN_FLIGHT: set[tuple[str, str]] = set()

#: cell_run_id -> {"status", "elapsed_s", "result"}
_CELL_RUNS: dict[str, dict[str, Any]] = {}


async def guard_cell(model: str, task_type: str) -> None:
    """Refuse only a collision on the same cell."""
    if (model, task_type) in _IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail=f"{model} / {task_type} is already being measured")


async def measure_one_cell(model: str, task_type: str) -> dict[str, Any]:
    """Measure, write, and flag any reordering the write caused."""
    before = await delegation_rows_all()
    _IN_FLIGHT.add((model, task_type))
    try:
        result = await run_cell(model, task_type)
        if result.status == "ok":
            await benchmark_sweep.record_success(
                "manual", model, task_type, result,
                trigger="manual", under_load=True)
        else:
            await benchmark_sweep.record_failure(
                "manual", model, task_type, result)
        # Spec 9: a manual re-measure clears dormancy whether or not the
        # measurement succeeded. It takes a human deciding the underlying
        # problem is fixed, and that judgment is what the counter cannot make.
        from routes.db_benchmark import capability_meta_set
        await capability_meta_set(model, task_type,
                                  consecutive_failures=0, dormant=0)
        after = await delegation_rows_all()
        await benchmark_reorder.flag_reorderings(before, after, [task_type])
        return {"status": result.status, "elapsed_s": result.elapsed_s,
                "error": result.error}
    finally:
        _IN_FLIGHT.discard((model, task_type))


@router.post("/api/delegation/benchmark/cell")
async def handle_cell_post(request: Request):
    _require_admin(request)
    data = _require_json_object(await request.json())
    model = _require_str_field(data, "model")
    task_type = _require_str_field(data, "task_type")
    await guard_cell(model, task_type)

    cell_run_id = uuid.uuid4().hex
    _CELL_RUNS[cell_run_id] = {"status": "running", "elapsed_s": 0.0,
                               "result": None}

    async def _go():
        try:
            outcome = await measure_one_cell(model, task_type)
            _CELL_RUNS[cell_run_id] = {
                "status": outcome["status"], "elapsed_s": outcome["elapsed_s"],
                "result": outcome}
        except Exception as exc:                        # noqa: BLE001
            _CELL_RUNS[cell_run_id] = {
                "status": "failed", "elapsed_s": 0.0,
                "result": {"error": str(exc)[:500]}}

    asyncio.create_task(_go())
    return {"cell_run_id": cell_run_id}


@router.get("/api/delegation/benchmark/cell/{cell_run_id}")
async def handle_cell_get(request: Request, cell_run_id: str):
    _require_admin(request)
    state = _CELL_RUNS.get(cell_run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown cell run")
    return state


@router.post("/api/delegation/capability/ack")
async def handle_ack_post(request: Request):
    _require_admin(request)
    data = _require_json_object(await request.json())
    model = _require_str_field(data, "model")
    task_type = _require_str_field(data, "task_type")
    await benchmark_reorder.acknowledge(model, task_type)
    return {"ok": True}
```

- [ ] **Step 4: Register the router in `app.py`**

Beside the existing delegation import (line 57):

```python
from routes.benchmark import router as benchmark_router
```

Beside the existing `include_router` (line 660):

```python
app.include_router(benchmark_router)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_routes.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Commit**

```bash
git add routes/benchmark.py app.py tests/test_qa_benchmark_routes.py
git commit -m "Expose the per-cell Re-measure and acknowledge endpoints"
```

---

### Task 10: The Delegation page

**Files:**
- Modify: `web/assets/delegation.js` (`_modelTable`, line 332)
- Test: `tests/test_frontend_browser.py` (add cases)

**Interfaces:**
- Consumes: the three endpoints from Task 9; `payload.rows[]` gains `dormant`, `reorder_flagged`, `measured_at`

**Before editing:** run `git status --short web/assets/delegation.js`. A parallel session was editing this file on 2026-09-18. If it is dirty, coordinate before touching it.

- [ ] **Step 1: Extend the delegation payload**

In `routes/delegation.py`'s `handle_delegation_get` (line 361), merge the provenance columns into each row so the page can render them:

```python
    from routes.db_benchmark import capability_meta_all
    meta = {(r["model"], r["task_type"]): r for r in await capability_meta_all()}
    for row in rows:
        extra = meta.get((row["model"], row["task_type"])) or {}
        row["dormant"] = int(extra.get("dormant") or 0)
        row["reorder_flagged"] = int(extra.get("reorder_flagged") or 0)
        row["measured_at"] = extra.get("measured_at")
```

- [ ] **Step 2: Add the control to `_modelTable`**

Replace the `rowsForType.forEach` body in `web/assets/delegation.js:348` with:

```javascript
  rowsForType.forEach(row => {
    const tr = document.createElement('tr');
    if (row.reorder_flagged) tr.classList.add('delegation-row-reordered');
    if (row.dormant) tr.classList.add('delegation-row-dormant');

    const modelCell = document.createElement('td');
    modelCell.className = 'delegation-model-name';
    modelCell.textContent = row.model;
    if (row.dormant) {
      const tag = document.createElement('span');
      tag.className = 'delegation-dormant-tag';
      tag.textContent = ' dormant';
      // A silently skipped cell is indistinguishable from one nobody thought
      // to measure, which is the failure this whole subsystem exists to stop.
      tag.title = 'Failed three consecutive sweeps; no longer attempted. '
                + 'Re-measure to clear.';
      modelCell.appendChild(tag);
    }
    tr.appendChild(modelCell);

    columns.forEach(column => {
      const td = document.createElement('td');
      td.appendChild(_cell(row, column));
      tr.appendChild(td);
    });

    const actions = document.createElement('td');
    actions.appendChild(_remeasureButton(row));
    if (row.reorder_flagged) actions.appendChild(_ackButton(row));
    tr.appendChild(actions);

    tbody.appendChild(tr);
  });
```

Add the header cell after the `columns.forEach` in the `thead` block:

```javascript
  const actionHead = document.createElement('th');
  actionHead.textContent = '';
  headRow.appendChild(actionHead);
```

- [ ] **Step 3: Add the two button builders**

Insert above `_modelTable`:

```javascript
/** Poll one forced cell run until it stops running. ~6 minutes at 3 repeats,
 *  so the interval is generous: a faster poll would not make it finish. */
async function _pollCell(cellRunId, button) {
  for (;;) {
    await new Promise(r => setTimeout(r, 5000));
    const res = await fetch(`/api/delegation/benchmark/cell/${cellRunId}`);
    if (!res.ok) { button.textContent = 'Re-measure'; button.disabled = false; return; }
    const state = await res.json();
    if (state.status !== 'running') {
      button.textContent = state.status === 'ok' ? 'Done' : 'Failed';
      button.disabled = false;
      await _refreshDelegation();
      return;
    }
  }
}

function _remeasureButton(row) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'delegation-remeasure';
  button.textContent = 'Re-measure';
  button.title = 'Measure this cell now (~6 min). The result is recorded as '
               + 'measured under load.';
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Measuring…';
    const res = await fetch('/api/delegation/benchmark/cell', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: row.model, task_type: row.task_type}),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      button.textContent = _errorMessage(data, 'Failed');
      button.disabled = false;
      return;
    }
    const {cell_run_id: cellRunId} = await res.json();
    _pollCell(cellRunId, button);
  });
  return button;
}

function _ackButton(row) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'delegation-ack';
  button.textContent = 'Acknowledge';
  button.title = 'This measurement reordered the ladder. Acknowledging clears '
               + 'the highlight until a later measurement reorders it again.';
  button.addEventListener('click', async () => {
    button.disabled = true;
    await fetch('/api/delegation/capability/ack', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: row.model, task_type: row.task_type}),
    });
    await _refreshDelegation();
  });
  return button;
}
```

- [ ] **Step 4: Regenerate the asset hash**

```bash
.venv/bin/python bin/wc-asset-versions.py
git diff --stat web/assets/app.js
```

Expected: `app.js` shows a changed `./delegation.js?v=…` hash. **If it does not, the browser will keep loading the old module** — this exact defect has shipped twice.

- [ ] **Step 5: Add browser test cases to `tests/test_frontend_browser.py`**

Follow the file's existing fixture. Assert:

```python
    def test_a_dormant_row_is_marked(self):
        """A silently skipped cell reads as a cell nobody measured."""
        # seed a capability row with dormant=1, load Settings > Delegation,
        # then:
        row = self.page.locator("tr.delegation-row-dormant").first
        self.assertTrue(row.is_visible())
        self.assertIn("dormant", row.inner_text().lower())

    def test_a_reordered_row_offers_acknowledge(self):
        row = self.page.locator("tr.delegation-row-reordered").first
        self.assertTrue(row.locator("button.delegation-ack").is_visible())

    def test_an_unflagged_row_has_no_acknowledge_button(self):
        """The direction that catches an always-flagged renderer."""
        rows = self.page.locator("tr:not(.delegation-row-reordered)")
        self.assertEqual(rows.first.locator("button.delegation-ack").count(), 0)
```

Use `locator(...).is_disabled()` rather than `get_attribute("disabled")` for any disabled check — `get_attribute` returns `""` for a present attribute and `bool("")` is `False`, which has produced a test that could not fail in this repo before.

- [ ] **Step 6: Run the browser tests**

Run: `.venv/bin/python -m pytest tests/test_frontend_browser.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/assets/delegation.js web/assets/app.js routes/delegation.py tests/test_frontend_browser.py
git commit -m "Add Re-measure, dormant markers and reorder highlights to the Delegation page"
```

---

### Task 11: The systemd timer

**Files:**
- Create: `systemd/webconsole-benchmark.service`, `systemd/webconsole-benchmark.timer`
- Test: `tests/test_qa_benchmark_units.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_benchmark_units.py
"""QA: the systemd units.

Asserted as text rather than by installing them. The one property worth a test
is that the timer fires nightly at 02:00 -- a unit that fires every 30 seconds
like webconsole-health.timer, which it is modelled on, would start a sweep
during the working day and measure contention on every cell.
"""
from __future__ import annotations

import unittest
from pathlib import Path

UNITS = Path(__file__).resolve().parent.parent / "systemd"


class UnitTests(unittest.TestCase):
    def test_the_timer_fires_nightly_at_two(self):
        text = (UNITS / "webconsole-benchmark.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 02:00:00", text)

    def test_the_timer_does_not_use_an_interval(self):
        """OnUnitActiveSec would make it a repeating timer, not a nightly one."""
        text = (UNITS / "webconsole-benchmark.timer").read_text()
        self.assertNotIn("OnUnitActiveSec", text)

    def test_the_service_calls_the_scheduled_entry_point(self):
        text = (UNITS / "webconsole-benchmark.service").read_text()
        self.assertIn("--scheduled", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_units.py -v`
Expected: FAIL with `FileNotFoundError`

- [ ] **Step 3: Write the units**

`systemd/webconsole-benchmark.service`:

```ini
[Unit]
Description=WebConsole benchmark sweep (one night's measurement)

[Service]
Type=oneshot
WorkingDirectory=%h/projects/claude-code-webconsole
ExecStart=%h/projects/claude-code-webconsole/.venv/bin/python \
          %h/projects/claude-code-webconsole/bin/wc-benchmark.py --scheduled
```

`systemd/webconsole-benchmark.timer`:

```ini
[Unit]
Description=Start a night of benchmark measurement at 02:00

[Timer]
OnCalendar=*-*-* 02:00:00
# A missed night is skipped rather than run late. The window exists because the
# box is idle then; firing at 11:00 because the host was asleep would measure
# contention on every cell.
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_benchmark_units.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest`
Expected: all pass, **exactly 6 skips**. Any other skip count means the browser layer did not run and the result cannot be trusted.

- [ ] **Step 6: Commit**

```bash
git add systemd/webconsole-benchmark.service systemd/webconsole-benchmark.timer tests/test_qa_benchmark_units.py
git commit -m "Add the nightly benchmark timer and its oneshot service"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §4 storage, §4.1 provenance columns | 1 |
| §6 per-cell writes, resume from `measured_at` | 4 |
| §7 failure handling, timeout not a measurement | 3 |
| §8.1 two outcomes, §8.2 cooling, §8.4 expiry | 6 |
| §8.3 busy window, voice excluded | 5 |
| §9 forced cell, endpoints, dormancy clear | 9, 10 |
| §10 projection in nights | 8 |
| §11 the writer and voice exclusion | 2 |
| §12 dormancy, §12.1 progress, §12.2 all-dormant | 4, 6 |
| §13 reorder highlight and acknowledgement | 7, 10 |
| §16 `DEFAULT_MODELS` missing terra | 8 |

**Gap found and closed:** §12.1's two-number progress line (`[14/63] … (7 dormant of 70)`) had no task. `--status` in Task 8 prints done/pending/dormant against `cells_total`, and Task 6 records `cells_dormant` on the run row at completion. The per-cell stdout line during a sweep is not implemented — `run_night` writes no progress output. **This is a deliberate reduction:** the sweep runs unattended under systemd where stdout goes to the journal, and `--status` answers the same question on demand. If you want the live line, it is a one-line addition to `run_night`'s loop.

**Placeholder scan:** none. Every code step carries real code. Two steps (Task 5 Step 4, Task 6 Step 3 note) instruct the engineer to verify a runtime detail and adapt — those are verification steps with both branches given, not placeholders.

**Type consistency:** `CellResult` is defined once in Task 3 and consumed unchanged in Tasks 4, 6, 8, 9. `write_cell`'s keyword-only `measured_at`/`trigger`/`under_load` match every call site. `classify_cells` returns the same three keys everywhere it is used.

**Known weakness worth stating:** Task 7's `test_a_reordering_flags_the_row` is conditional (`if changed:`) because the exact accuracy values that produce a ladder reorder depend on `CapabilityTable.ladder`'s cost sort, which I did not simulate. **The implementer must replace that conditional with a concrete fixture that provably reorders**, asserting unconditionally. A conditional assertion is a test that cannot fail, and this repo has shipped four of those.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-18-benchmark.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
