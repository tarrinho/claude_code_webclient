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

import benchmark_cell
import benchmark_reorder
import benchmark_writer
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store
from routes.db_delegation import delegation_rows_all

#: Spec 12. Three consecutive failed sweeps take a cell out of rotation.
#: azure_ai/gpt-5.4-mini-copilot cannot be reached by this harness at all
#: (CLAUDE.md 0.1) and would otherwise burn 7 cells at up to the 900s cap on
#: every sweep -- about 10% of the matrix spent re-confirming a known failure.
DORMANCY_THRESHOLD = 3

#: (model, task_type) pairs currently being measured, by anyone. Shared by
#: the Delegation page's Re-measure button (routes/benchmark.py) and
#: `wc-benchmark.py --cell` -- both go through `measure_one_cell` below, so a
#: single set guards a collision between them regardless of which one holds
#: a cell.
_IN_FLIGHT: set[tuple[str, str]] = set()


async def measure_one_cell(model: str, task_type: str,
                           on_progress=None) -> dict[str, Any]:
    """Measure, write, clear dormancy, and flag any reordering the write
    caused. The one implementation of a forced re-measure, shared by the
    Delegation page and `wc-benchmark.py --cell` (spec 9): a caller that
    reimplemented this by hand used to omit both the dormancy clear on a
    failed force and the reorder flag, since those run only after
    record_success/record_failure, not inside either of them.

    Dormancy is cleared whether the measurement succeeds or fails: a forced
    re-measure is a human deciding the underlying problem is fixed, and
    that judgment is what the failure counter cannot make on its own.
    """
    before = await delegation_rows_all()
    _IN_FLIGHT.add((model, task_type))
    try:
        result = await benchmark_cell.run_cell(
            model, task_type, on_progress=on_progress)
        if result.status == "ok":
            await record_success(
                "manual", model, task_type, result,
                trigger="manual", under_load=True)
        else:
            await record_failure("manual", model, task_type, result)
        await store.capability_meta_set(
            model, task_type, consecutive_failures=0, dormant=0)
        after = await delegation_rows_all()
        await benchmark_reorder.flag_reorderings(before, after, [task_type])
        return {"status": result.status, "elapsed_s": result.elapsed_s,
                "error": result.error}
    finally:
        _IN_FLIGHT.discard((model, task_type))


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

    Two outcomes, per spec 8.1: advance the current sweep, or start a new
    one. "cooling" is the advance branch finding nothing to advance -- kept
    as a distinct label so a caller can tell it apart from "stopped because
    busy", which is a different kind of nothing. It is not a third
    scheduling branch.
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
        run_id=run_id, started_at=_stamp(now),
        expires_at=_stamp(now + timedelta(days=EXPIRY_DAYS)),
        models=list(models), task_types=list(task_types), repeats=repeats,
        cells_total=len(models) * len(task_types), trigger=trigger)
    return await store.run_get(run_id)


async def _finish(run: dict[str, Any]) -> None:
    """A sweep with nothing pending is done, whether that is because every
    cell measured or because the remainder went dormant (spec 12.2) -- a
    dormant cell can never become pending again, so there is nothing to wait
    for."""
    now = _now()
    groups = await classify_cells(run)
    await store.run_set_dormant_count(run["id"], len(groups["dormant"]))
    await store.run_finish(run["id"], _stamp(now),
                           _stamp(now + timedelta(days=COOLING_DAYS)))


async def run_night(run: dict[str, Any], *, now: datetime | None = None) -> str:
    """Measure until the box is busy, every cell has had its one attempt for
    tonight, the sweep completes, or it expires.

    Returns "complete", "stopped", or "expired". Each cell writes as it
    finishes (spec 6), so stopping at any point keeps everything measured so
    far.

    Each cell gets at most one attempt per call, tracked in `attempted`
    below. Spec 12 counts THREE CONSECUTIVE SWEEPS, not three attempts in
    one night: `classify_cells` recomputes `pending` every iteration, and a
    failure writes no capability row (spec 6), so a cell that stayed at
    index 0 without this guard would be retried immediately, burning the
    whole dormancy allowance -- and the ten-day expiry check below it -- on
    one night instead of three. Once every still-pending cell has had its
    attempt for tonight, the night is done: pending cells remain, so this
    returns "stopped", not "complete" -- only a later night, or a later
    iteration once dormancy clears a name from `pending`, can attempt them
    again.

    Busy means stop for the night (spec 8.3): finish nothing further, leave
    the sweep current, and let the next night's timer continue it. There is
    no re-check loop within a single call -- once busy, this returns.

    Expiry is checked against `run["expires_at"]` as stored at `start_sweep`
    and never recomputed (rule 2) -- but checked before EVERY cell, not only
    at entry, so a night that crosses the ten-day boundary mid-run stops
    there instead of measuring on into the next invocation. An expired sweep
    is by definition one that failed to stay current, so it is abandoned
    rather than measured further, and every cell it already wrote stays in
    delegation_capability (rule 4).

    Reorder detection (spec 13) runs ONCE, after the loop ends, over every
    task type this call actually measured -- not once per cell. A ladder is
    a property of a task type, not of one row, so comparing after every
    cell would flag intermediate states of a ladder that was never finished
    at that point in the loop. `measure_one_cell` (the page's and --cell's
    shared per-cell path) is the one place a single-write before/after
    comparison is correct, because there the snapshots bracket exactly one
    write; here they bracket the whole night's.
    """
    before = await delegation_rows_all()
    attempted: set[tuple[str, str]] = set()
    measured_task_types: set[str] = set()
    outcome: str

    while True:
        moment = now or _now()
        if _stamp(moment) >= run["expires_at"]:
            await store.run_expire(run["id"])
            outcome = "expired"
            break

        if await sweep_is_complete(run):
            await _finish(run)
            outcome = "complete"
            break

        groups = await classify_cells(run)
        remaining = [key for key in groups["pending"] if key not in attempted]
        if not remaining:
            outcome = "stopped"
            break

        busy, _reason = await box_is_busy()
        if busy:
            outcome = "stopped"
            break

        model, task_type = remaining[0]
        attempted.add((model, task_type))
        result = await benchmark_cell.run_cell(
            model, task_type, repeats=int(run["repeats"]))
        if result.status == "ok":
            await record_success(run["id"], model, task_type, result,
                                 trigger="scheduled", under_load=False)
        else:
            await record_failure(run["id"], model, task_type, result)
        measured_task_types.add(task_type)

    if measured_task_types:
        after = await delegation_rows_all()
        await benchmark_reorder.flag_reorderings(
            before, after, sorted(measured_task_types))
    return outcome
