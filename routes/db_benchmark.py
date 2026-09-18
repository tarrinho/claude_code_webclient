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
