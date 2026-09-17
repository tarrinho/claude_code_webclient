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
from delegation_startup import ceiling_enforcement_enabled, live_known_models
from routes.db_delegation import rows_to_capability
from routes.db_users import setting_set
from tiered_delegation import (
    BUDGET_USD,
    CEILING_ENFORCEMENT_DEFAULT,
    CEILING_ENFORCEMENT_SETTING,
    EXCLUDED_MODELS,
    LATENCY_CEILING_S,
    LEAVES_PER_TREE,
    MAX_ATTEMPTS,
    CapabilityRow,
    CapabilityTable,
)

_log = logging.getLogger("wc.app")

router = APIRouter()

_EDITABLE = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context")

# Spec 12: "until [the gate-type validation question] is decided, do not flip
# `coding` to operational" -- and 1.2 measures `coding` as already clearing
# every one of 1.1's six invariants, so nothing else in this codebase stops
# the flip. Same pattern 2.7 uses for excluding `azure_ai/gpt-5.4-mini`: a
# decision, not a derived value, so nothing recomputes it.
#
# Spec amendment b782e4d added, of the `reasoning` ladder: "Until it is either
# enforced in code or lifted by measurement, `reasoning` must stay
# non-operational (spec 1.1)." That amendment landed after `coding`'s guard
# was written, so nobody re-applied the same reasoning to `reasoning` -- the
# ruling behind `coding`'s entry was that the constraint "was being held only
# by nobody having clicked", and that argument is identical here.
#
# Today's data would refuse the `reasoning` flip on its own regardless of this
# guard: `claude-sonnet-5`'s reasoning row carries no `median_latency_s`, and
# its one-rung tree already costs $3.736 against a `BUDGET_USD` of 1.00. So
# this entry does not close an open door -- it closes the gap between a
# decision and data that happens, today, to agree with it. The hold itself is
# about a 75% n=2 accuracy figure being re-measured; a latency-and-cost
# measurement alone would not lift it.
#
# Each entry is a task type mapped to *why* it is held -- the two types are
# blocked for different reasons (a still-open spec-12 gate-type question for
# `coding`, an unmeasured accuracy figure for `reasoning`), so a single
# generic refusal message would not tell an operator which blocker applies to
# them. Still a named constant, and still removable per task type: deleting
# `coding`'s entry needs section 12's reviewer-gate item resolved, and deleting
# `reasoning`'s entry needs its accuracy figure re-measured above the bar --
# neither should be deleted for the other's reason.
_OPERATIONAL_FLIP_BLOCKED: Final[dict[str, str]] = {
    "coding": (
        "spec section 12 leaves the gate-type validation question open and "
        "forbids flipping coding until it is decided"
    ),
    "reasoning": (
        "spec amendment b782e4d holds reasoning non-operational until its "
        "75% n=2 accuracy figure is either re-measured or enforced in code "
        "(spec 1.1); today's data would refuse this flip anyway -- "
        "claude-sonnet-5's reasoning row has no median_latency_s and its "
        "one-rung tree costs $3.736 against a BUDGET_USD of 1.00 -- but this "
        "guard is what stops that data gap from silently becoming the only "
        "thing enforcing the hold"
    ),
}


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


def _require_str_field(data: dict, key: str) -> str:
    """Coerce one field of an already-validated JSON object to a stripped
    string, refusing anything that isn't a string (or missing/None) with a
    400. `_require_json_object` stops a non-object body from reaching
    `.get()`; this stops a non-string *field* -- a body like
    `{"model": 5, ...}` -- from reaching `.strip()` and escaping as an
    unhandled `AttributeError` (a 500) one level down."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400, detail=f"{key} must be a string")
    return value.strip()


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


def _blockers_by_task_type(
    capability_rows: list[CapabilityRow], operational_set: set[str],
    known_models: frozenset[str] | None,
    enforce_latency_ceiling: bool = CEILING_ENFORCEMENT_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """Why each task type cannot go operational, for the settings page.

    Two independent kinds of blocker, and both are surfaced:

    * policy -- `_OPERATIONAL_FLIP_BLOCKED`, a decision recorded in code
      (spec 12 holds `coding`, amendment b782e4d holds `reasoning`).
    * data -- 1.1's six invariants. `handle_operational_put` already computes
      exactly this when it validates a real flip: build a `CapabilityTable`
      with the candidate type added to the operational set and collect
      `validate()`'s problems. Doing the same here, once per non-operational
      task type, is nine in-memory calls on a GET -- no new storage, no new
      endpoint.

    A third channel, `warnings`, carries what is WRONG but not BLOCKING: with
    9.2's ceiling knob off (`CEILING_ENFORCEMENT_SETTING`, the default), a
    worst-case path over 5.1's ceiling appears here instead of in `data`. It
    is deliberately not merged into `data`: the page has to be able to say
    "this is why you cannot flip" and "this is over the ceiling and we are not
    stopping you" as different sentences, or turning the knob off would read
    as the breach having gone away.

    An already-operational task type, or one blocked neither way, reports
    `{"policy": None, "data": [], "warnings": []}` -- never an invented
    problem.
    """
    blockers: dict[str, dict[str, Any]] = {}
    task_types = sorted({row.task_type for row in capability_rows})
    for task_type in task_types:
        candidate = CapabilityTable(
            capability_rows, operational=operational_set | {task_type})
        # The breach is computed for every type, operational or not, and
        # whatever the knob says -- the knob decides whether it also appears
        # in `data` below, never whether it is measured.
        breach = candidate.latency_ceiling_breaches().get(task_type)
        warnings = [breach] if breach is not None and not enforce_latency_ceiling else []
        if task_type in operational_set:
            blockers[task_type] = {
                "policy": None, "data": [], "warnings": warnings}
            continue
        problems = candidate.validate(
            known_models=known_models,
            enforce_latency_ceiling=enforce_latency_ceiling)
        # `validate()` is scoped to every type in the candidate's operational
        # set, not only the one being asked about here -- filter down to the
        # problems that actually name this task type.
        data_problems = [p for p in problems if p.startswith(f"{task_type}:")]
        policy = _OPERATIONAL_FLIP_BLOCKED.get(task_type)
        blockers[task_type] = {
            # Prefixed with the task type, like every string `validate()`
            # produces. The stored reason is deliberately unprefixed --
            # `handle_operational_put` interpolates it into a sentence that
            # already names the type -- but on the settings page a policy line
            # sits directly beside data lines that ARE prefixed, and the
            # mismatch read as two different kinds of message about two
            # different things.
            "policy": None if policy is None else f"{task_type}: {policy}",
            "data": data_problems,
            "warnings": warnings,
        }
    return blockers


async def handle_delegation_get(request: Request):
    """GET /api/delegation -- the matrix, the flags, the derived ladders, and
    why each non-operational type cannot flip yet (see
    `_blockers_by_task_type`)."""
    rows = await db.delegation_rows_all()
    operational = sorted(await db.delegation_operational_all())
    operational_set = set(operational)
    capability_rows = rows_to_capability(rows)
    table = CapabilityTable(capability_rows, operational=operational_set)
    task_types = sorted({row["task_type"] for row in rows})
    # Same live list, same fallback, as every other call site that validates
    # this table (see live_known_models's docstring) -- the blocker computed
    # here must agree with what a real flip attempt would say.
    known_models = await live_known_models()
    enforce_ceiling = await ceiling_enforcement_enabled()
    blockers = _blockers_by_task_type(
        capability_rows, operational_set, known_models, enforce_ceiling)
    return JSONResponse({
        "rows": rows,
        "operational": operational,
        # Derived, never stored: the ladder is the output of walking 2.6, so
        # sending a stored copy would let the page show a ladder the generator
        # does not produce.
        "ladders": {t: table.ladder(t) for t in task_types},
        "editable_columns": list(_EDITABLE),
        "config": _config_overview(),
        "blockers": blockers,
        "ceiling_enforcement": {
            "enabled": enforce_ceiling,
            "default": CEILING_ENFORCEMENT_DEFAULT,
            "setting": CEILING_ENFORCEMENT_SETTING,
            "ceiling_s": LATENCY_CEILING_S,
        },
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
    model = _require_str_field(data, "model")
    task_type = _require_str_field(data, "task_type")
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
        # Same live list, same fallback, as delegation_startup.validate_or_die
        # -- spec 1.1 requires the same code path and the same error text at
        # every moment a table is validated, and a write endpoint must not
        # start refusing writes because a backend happens to be unreachable.
        known_models = await live_known_models()
        # Same knob the startup check and the settings page read. If these
        # ever disagreed, a table would pass through one path and be refused
        # by another -- which is how an operator ends up with a deployment
        # that will not boot after its next restart.
        problems = table.validate(
            known_models=known_models,
            enforce_latency_ceiling=await ceiling_enforcement_enabled())
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
    task_type = _require_str_field(data, "task_type")
    operational = bool(data.get("operational"))
    if not task_type:
        raise HTTPException(status_code=400, detail="task_type is required")

    if operational and task_type in _OPERATIONAL_FLIP_BLOCKED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{task_type} cannot be flipped operational: "
                f"{_OPERATIONAL_FLIP_BLOCKED[task_type]}"
            ))

    if operational:
        rows = rows_to_capability(await db.delegation_rows_all())
        current = await db.delegation_operational_all()
        table = CapabilityTable(rows, operational=current | {task_type})
        # Same live list, same fallback, as handle_row_put above and as
        # delegation_startup.validate_or_die -- see the comment there.
        known_models = await live_known_models()
        # Same knob the startup check and the settings page read. If these
        # ever disagreed, a table would pass through one path and be refused
        # by another -- which is how an operator ends up with a deployment
        # that will not boot after its next restart.
        problems = table.validate(
            known_models=known_models,
            enforce_latency_ceiling=await ceiling_enforcement_enabled())
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


async def handle_ceiling_enforcement_put(request: Request):
    """PUT /api/delegation/ceiling-enforcement -- make 5.1's combined latency
    ceiling a blocking invariant, or back off again.

    Off by default (`CEILING_ENFORCEMENT_DEFAULT`). See
    `tiered_delegation.CEILING_ENFORCEMENT_SETTING` for why: the ceiling is
    derived from the worst case of the most expensive *operational* task type,
    nothing is operational, and every type currently over it is over because
    it is uncalibrated rather than because it is slow.

    Turning it ON is validated before it is stored, because enabling
    enforcement can invalidate a task type that is ALREADY operational.
    Storing the flag first would leave a deployment that refuses to start on
    its next restart, and a settings write that bricks the boot path is
    exactly what 1.1's checks exist to prevent. Turning it OFF is never
    validated: relaxing a blocking invariant cannot break another one.
    """
    _require_admin(request)
    data = _require_json_object(await request.json())
    if "enabled" not in data:
        raise HTTPException(status_code=400, detail="enabled is required")
    enabled = bool(data["enabled"])

    if enabled:
        rows = rows_to_capability(await db.delegation_rows_all())
        operational = await db.delegation_operational_all()
        table = CapabilityTable(rows, operational=operational)
        known_models = await live_known_models()
        problems = table.validate(known_models=known_models,
                                  enforce_latency_ceiling=True)
        if problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "enforcing the latency ceiling would invalidate an "
                    "already-operational task type, and this deployment would "
                    "refuse to start on its next restart: "
                    + "; ".join(problems)
                ))

    await setting_set(CEILING_ENFORCEMENT_SETTING, "1" if enabled else "0")
    _log.info("delegation_ceiling_enforcement enabled=%s", enabled)
    return JSONResponse({"ok": True, "enabled": enabled})


@router.put("/api/delegation/operational")
async def _api_delegation_operational_put(request: Request):
    return await handle_operational_put(request)


@router.put("/api/delegation/ceiling-enforcement")
async def _api_delegation_ceiling_enforcement_put(request: Request):
    return await handle_ceiling_enforcement_put(request)
