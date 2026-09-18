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
