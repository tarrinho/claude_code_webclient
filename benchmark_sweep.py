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
from datetime import datetime, timedelta, timezone
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


#: Spec 8.3. A turn that has just finished leaves the gateway still draining,
#: so "no turn in flight" is not the same as idle. Chosen rather than measured,
#: and labelled as such.
IDLE_MARGIN_MINUTES = 10

#: How long to wait before asking again once the box went busy.
BUSY_RECHECK_SECONDS = 60


async def box_is_busy() -> tuple[bool, str]:
    """Is the box in use? Returns (busy, reason).

    The reason is returned rather than logged so `--status` can say why a
    sweep stopped, instead of leaving an operator to guess between "finished
    for the night" and "crashed".

    The idle cutoff is computed in Python, not via SQLite's datetime('now').
    db._now() writes 'YYYY-MM-DDTHH:MM:SSZ'; datetime('now') produces
    'YYYY-MM-DD HH:MM:SS' (space, no Z). Compared as strings the two forms
    first differ at the date/time separator, where 'T' sorts above ' ', so
    every message written today would satisfy
    `created_at >= datetime('now', '-N minutes')` regardless of the hour --
    the box would read as busy all day.

    Voice sessions are deliberately NOT a signal. routes/voice.py speaks to an
    OpenAI-compatible endpoint directly (CLAUDE.md section 0), so a voice
    conversation does not contend with the CLI transport this harness measures
    over -- counting it would stop sweeps for load that does not exist.
    """
    import runner
    if runner.slots_busy():
        return True, "a turn is in flight"

    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=IDLE_MARGIN_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = await db.db_conn.execute(
        "SELECT created_at FROM messages WHERE created_at >= ? LIMIT 1",
        (cutoff,))
    if await cur.fetchone():
        return True, f"a message was written in the last {IDLE_MARGIN_MINUTES} minutes"

    return False, "idle"
