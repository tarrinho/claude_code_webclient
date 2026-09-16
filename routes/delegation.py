"""Routes for /api/delegation -- the tiered-delegation settings matrix.

Spec 9.2. The benchmark table is live-editable and the ladders regenerate from
it at runtime, so every write is validated before it is stored, not only the
operational flip: a table checked once at startup can be broken at any time
and will not say so until the next restart.

Scoping (spec 1.1): "a write is rejected only against the invariants of task
types that are already operational, plus the type being written. Editing a
non-operational type's rows stays free -- that is the bootstrap path, and
gating it would make the table impossible to fill in."

For a row write (`handle_row_put`), the *check set* -- which task types have
their six invariants evaluated -- is exactly the task types already flagged
operational in the database. The type being written joins that set only if it
is itself already one of them; a not-yet-operational type is never added to
the check set on its own account, or an operator filling in one column at a
time would be blocked by the very check the edit exists to satisfy (this is
also why `test_a_row_can_be_written_and_read_back` -- a first, incomplete row
for a brand-new, non-operational task type -- must succeed). What *does* carry
the write regardless of which type it targets is the **data**: validation
always runs against the full merged table (every row, with the pending write
applied), never just the rows of the type being written. That is what makes a
write to a non-operational type still able to break something: a `coding` row
can be operational while its worst-case path depends on the cheapest
`reviewer-gate` row (spec 5.1), and `reviewer-gate` itself need not be
operational for that dependency to exist. Feeding the merged table into every
already-operational type's check is how that edit gets caught without adding
`reviewer-gate` to the check set.

For the operational flip (`handle_operational_put`), spec 1.1 says flipping
*is* the act that submits a type to validation -- "at that moment every
ladder-eligible row must be complete and every rung must resolve, or the
system refuses to start" -- so the type being flipped is unconditionally added
to the check set for that one call, regardless of whether it is already
operational.
"""
from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from delegation_startup import load_capability_table
from routes.db_delegation import rows_to_capability
from tiered_delegation import (
    BUDGET_USD,
    EXCLUDED_MODELS,
    LATENCY_CEILING_S,
    LEAVES_PER_TREE,
    MAX_ATTEMPTS,
    CapabilityTable,
)

_log = logging.getLogger("wc.app")

router = APIRouter()

_EDITABLE = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context")

# Spec 12: "until [the gate-type validation question] is decided, do not flip
# `coding` to operational" -- and 1.2 measures `coding` as already clearing
# every one of 1.1's six invariants, so nothing else in this codebase stops
# the flip. Same pattern 2.7 uses for excluding `azure_ai/gpt-5.4-mini`: a
# decision, not a derived value, so nothing recomputes it and re-admitting
# `coding` means deleting this line -- once section 12's reviewer-gate item
# resolves, not before.
_OPERATIONAL_FLIP_BLOCKED: Final[frozenset[str]] = frozenset({"coding"})


def _require_admin(request: Request) -> dict:
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return session


def _require_json_object(data: Any) -> dict:
    """A PUT body that isn't a JSON object (a bare list, string, number...)
    must get a 400, not an `AttributeError` from the first `.get()` call
    escaping as an unhandled 500."""
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400, detail="request body must be a JSON object")
    return data


def _coerce_measured_value(column: str, value: Any) -> float | int | None:
    """Coerce one of the five measured columns to number-or-`None`, refusing
    anything else with a 400.

    `db.delegation_row_set` stores whatever it is given -- SQLite's REAL
    affinity does not coerce text, so a string (or a dict) written into one
    of these columns is accepted silently and only breaks on the *next*
    read: `CapabilityTable.ladder`'s sort key divides by
    `cost_per_1m_tokens`, so a non-numeric value there turns every
    subsequent `GET /api/delegation` into a 500 -- and the settings page is
    the only normal way an operator would reach the cell to fix it, so a bad
    write would brick the one tool that could repair it.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(
            status_code=400,
            detail=f"{column} must be a number or null, got {value!r}")
    return value


def _config_overview() -> dict[str, Any]:
    """Spec 9.2's first sentence: the page also surfaces the kill switch
    (9.1), the tunables from 5 and 10, and the cost ceiling (2.7) -- read
    only, per the operator ruling on this task (the matrix cells are the
    only editable part; nothing here is versioned config storage).

    Every entry names the section it comes from. Where this release holds no
    single source for a value -- nothing has been built yet, or the spec
    states a number in prose with no backing constant -- that is said
    outright rather than inventing a number to show. See the task 9 fix-1
    report for the full list of what has no source yet.
    """
    return {
        "kill_switch": {
            "section": "9.1",
            "available": False,
            "note": "not implemented in this release -- nothing routes "
                     "through this design yet, so there is no switch to read",
        },
        "attempts_and_caps": {
            "section": "5",
            "max_attempts_generation": {
                "value": MAX_ATTEMPTS,
                "source": "tiered_delegation.MAX_ATTEMPTS",
            },
            "max_attempts_per_gate": {
                "value": None,
                "note": "not modeled as its own constant; each gate runs "
                        "once by construction (spec 4.3)",
            },
            "max_nodes_per_tree": {
                "value": LEAVES_PER_TREE,
                "source": "tiered_delegation.LEAVES_PER_TREE",
            },
            "max_depth": {
                "value": None,
                "note": "not implemented in this release",
            },
            "max_children_per_node": {
                "value": None,
                "note": "not implemented in this release",
            },
            "max_subagents_per_leaf": {
                "value": None,
                "note": "not implemented in this release",
            },
            "combined_latency_ceiling_s": {
                "value": LATENCY_CEILING_S,
                "source": "tiered_delegation.LATENCY_CEILING_S",
            },
        },
        "cost_ceiling": {
            "section": "2.7",
            "budget_usd_per_tree": {
                "value": BUDGET_USD,
                "source": "tiered_delegation.BUDGET_USD",
            },
            "excluded_models": {
                "value": sorted(EXCLUDED_MODELS),
                "source": "tiered_delegation.EXCLUDED_MODELS",
            },
        },
        "observability": {
            "section": "10",
            "circuit_breaker_threshold": {
                "value": None,
                "note": "not implemented in this release -- spec text names "
                        "60% of the last 20 leaves per gate, with no backing "
                        "constant yet",
            },
            "human_spot_check_rate": {
                "value": None,
                "note": "not implemented in this release -- spec text names "
                        "2% of leaves clearing every gate, with no backing "
                        "constant yet",
            },
            "free_tier_target": {
                "value": None,
                "note": "open item (spec 12) -- the >=70% target's scope is "
                        "not decided, so no value is shown",
            },
        },
    }


async def handle_delegation_get(request: Request):
    """GET /api/delegation -- the matrix, the flags, and the derived ladders."""
    rows = await db.delegation_rows_all()
    operational = sorted(await db.delegation_operational_all())
    table = await load_capability_table()
    task_types = sorted({row["task_type"] for row in rows})
    return JSONResponse({
        "rows": rows,
        "operational": operational,
        # Derived, never stored: the ladder is the output of walking 2.6, so
        # sending a stored copy would let the page show a ladder the generator
        # does not produce.
        "ladders": {t: table.ladder(t) for t in task_types},
        "editable_columns": list(_EDITABLE),
        "config": _config_overview(),
    })


async def handle_row_put(request: Request):
    """PUT /api/delegation/row -- write one cell set for one (model, task).

    `db.delegation_row_set` (routes/db_delegation.py, already committed) is a
    full-row UPSERT: every one of the five columns is written from its
    keyword arguments, and an omitted column is stored as `None` -- it has no
    partial-column mode. Spec 9.2's page edits one cell at a time
    (`web/assets/delegation.js`'s `_saveRow` sends exactly one column per
    PUT), so this handler has to do the merge `delegation_row_set` does not:
    an edited cell keeps the row's other four values as they were stored,
    rather than blanking them. Skipping this merge would make every single-
    cell edit silently null the other four columns on that row.
    """
    _require_admin(request)
    data = _require_json_object(await request.json())
    model = (data.get("model") or "").strip()
    task_type = (data.get("task_type") or "").strip()
    if not model or not task_type:
        raise HTTPException(status_code=400, detail="model and task_type are required")

    # Unknown column names are refused outright, not silently dropped -- a
    # typo in a column name (or a client probing the endpoint) must not read
    # as a successful write of the columns it did recognise.
    unknown = set(data) - {"model", "task_type"} - set(_EDITABLE)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown capability columns: {sorted(unknown)}")

    rows = await db.delegation_rows_all()
    existing = next(
        (r for r in rows if r["model"] == model and r["task_type"] == task_type),
        None)
    merged_columns = {
        c: (_coerce_measured_value(c, data[c]) if c in data
            else (existing.get(c) if existing else None))
        for c in _EDITABLE
    }

    # Validate the resulting table before storing it -- see the module
    # docstring for the scoping rule. The check set is the task types already
    # operational; the data is the full table with this write merged in, so a
    # write to a non-operational type (e.g. a reviewer-gate row) can still be
    # caught if it breaks an already-operational type that depends on it.
    operational = await db.delegation_operational_all()
    if operational:
        merged_rows = [r for r in rows
                       if not (r["model"] == model and r["task_type"] == task_type)]
        merged_rows.append({"model": model, "task_type": task_type, **merged_columns})
        table = CapabilityTable(rows_to_capability(merged_rows), operational=operational)
        problems = table.validate()
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))

    await db.delegation_row_set(model, task_type, **merged_columns)
    _log.info("delegation_row_set model=%s task_type=%s", model, task_type)
    return JSONResponse({"ok": True})


async def handle_operational_put(request: Request):
    """PUT /api/delegation/operational -- flip a task type routable.

    This is the act that submits a task type to 1.1's validation. A refused
    flip leaves the stored state exactly as it was.
    """
    _require_admin(request)
    data = _require_json_object(await request.json())
    task_type = (data.get("task_type") or "").strip()
    operational = bool(data.get("operational"))
    if not task_type:
        raise HTTPException(status_code=400, detail="task_type is required")

    if operational and task_type in _OPERATIONAL_FLIP_BLOCKED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{task_type} cannot be flipped operational: spec section 12 "
                "leaves the gate-type validation question open and forbids "
                f"flipping {task_type} until it is decided"
            ))

    if operational:
        rows = rows_to_capability(await db.delegation_rows_all())
        current = await db.delegation_operational_all()
        table = CapabilityTable(rows, operational=current | {task_type})
        problems = table.validate()
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))

    await db.delegation_operational_set(task_type, operational)
    _log.info("delegation_operational_set task_type=%s operational=%s",
              task_type, operational)
    return JSONResponse({"ok": True, "operational": operational})


@router.get("/api/delegation")
async def _api_delegation_get(request: Request):
    return await handle_delegation_get(request)


@router.put("/api/delegation/row")
async def _api_delegation_row_put(request: Request):
    return await handle_row_put(request)


@router.put("/api/delegation/operational")
async def _api_delegation_operational_put(request: Request):
    return await handle_operational_put(request)
