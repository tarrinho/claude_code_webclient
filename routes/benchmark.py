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
from benchmark_sweep import _IN_FLIGHT, measure_one_cell
from routes.delegation import _require_admin, _require_json_object, _require_str_field

router = APIRouter()

#: cell_run_id -> {"status", "elapsed_s", "result"}
_CELL_RUNS: dict[str, dict[str, Any]] = {}


async def guard_cell(model: str, task_type: str) -> None:
    """Refuse only a collision on the same cell."""
    if (model, task_type) in _IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail=f"{model} / {task_type} is already being measured")


# `measure_one_cell` itself -- measure, write, clear dormancy, flag any
# reordering the write caused -- lives in benchmark_sweep.py so that
# bin/wc-benchmark.py's `--cell` can share this implementation exactly
# (spec 9) instead of reimplementing it and omitting the dormancy clear and
# the reorder flag, as it used to.


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
