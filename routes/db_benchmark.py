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


async def measured_hours_per_night(limit: int = 10) -> list[float]:
    """Hours of measurement recorded per calendar date, newest night first.

    Spec 10's estimate needs a per-night rate, and there is no per-night
    table -- `benchmark_cells.recorded_at` is the only record of when a cell
    was measured. `db._now()` writes it as `%Y-%m-%dT%H:%M:%SZ`, so the
    calendar date is the first 10 characters of the string, taken with
    `substr` rather than SQLite's `date()`: `date()` expects a decimal-point
    timestamp and returns NULL on this `T`-separated one, which would make
    every night disappear silently rather than raise. Grouping by that slice
    and summing `elapsed_s` gives measured hours per night.
    """
    cur = await db.db_conn.execute(
        "SELECT substr(recorded_at, 1, 10) AS night, SUM(elapsed_s) AS secs "
        "FROM benchmark_cells "
        "GROUP BY night "
        "ORDER BY night DESC "
        "LIMIT ?",
        (int(limit),),
    )
    rows = await cur.fetchall()
    return [row["secs"] / 3600.0 for row in rows]


async def capability_meta_all() -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        "SELECT model, task_type, measured_at, trigger, measured_under_load, "
        "consecutive_failures, dormant, reorder_flagged, reorder_seen_at, "
        "reorder_acked_at FROM delegation_capability ORDER BY task_type, model")
    return [dict(row) for row in await cur.fetchall()]


async def capability_meta_set(model: str, task_type: str, **columns: Any) -> None:
    """Insert-or-update provenance columns on a capability row.

    Unknown names are refused rather than dropped, matching
    `delegation_row_set`: a typo would otherwise read as a successful write of
    nothing.

    A cell that has never been measured successfully has no
    delegation_capability row -- `write_cell` (benchmark_writer.py) is the
    only inserter, and a failed cell never calls it (spec: the capability
    row is left alone on failure so an old number beats no number). A bare
    UPDATE against a row that does not exist yet matches nothing, so
    `consecutive_failures`/`dormant` silently never advance for a cell that
    has never once succeeded. INSERT ... ON CONFLICT DO UPDATE fixes that
    without changing behaviour for a row that already exists.

    `updated_at` is NOT NULL on this table, so the INSERT branch must supply
    it; it is set from `db._now()` only for that branch and is never part of
    the UPDATE SET list below, matching the old UPDATE-only behaviour where
    this function never touched `updated_at`. `accuracy`, `n`,
    `cost_per_1m_tokens`, `median_latency_s` and `max_context` are never
    written here on either branch -- only `write_cell` may invent those,
    because this function's callers are provenance-only (failure counters,
    dormancy, reorder flags), never a measurement.
    """
    unknown = set(columns) - set(_META_COLUMNS)
    if unknown:
        raise ValueError(f"unknown capability meta columns: {sorted(unknown)}")
    if not columns:
        return
    cols = list(columns)
    insert_cols = ", ".join(["model", "task_type", "updated_at"] + cols)
    insert_placeholders = ", ".join(["?"] * (3 + len(cols)))
    update_assignments = ", ".join(f"{c} = excluded.{c}" for c in cols)
    await db.db_conn.execute(
        f"INSERT INTO delegation_capability ({insert_cols}) "
        f"VALUES ({insert_placeholders}) "
        f"ON CONFLICT(model, task_type) DO UPDATE SET {update_assignments}",
        (model, task_type, db._now(), *columns.values()),
    )
    await db.db_conn.commit()
