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
from delegation_startup import (budget_enforcement_enabled,
                                ceiling_enforcement_enabled,
                                delegation_enabled,
                                live_known_models,
                                problems_with)
from routes.db_benchmark import capability_meta_all
from routes.db_delegation import rows_to_capability, delegation_pin_all, delegation_pin_set
from routes.db_users import setting_set
from tiered_delegation import (
    BUDGET_USD,
    DELEGATION_ENABLED_DEFAULT,
    DELEGATION_ENABLED_SETTING,
    GATE_CALLS,
    BUDGET_ENFORCEMENT_DEFAULT,
    BUDGET_ENFORCEMENT_SETTING,
    CEILING_ENFORCEMENT_DEFAULT,
    CEILING_ENFORCEMENT_SETTING,
    EXCLUDED_MODELS,
    GATE_MAX_ATTEMPTS,
    LATENCY_CEILING_S,
    LEAVES_PER_TREE,
    MAX_ATTEMPTS,
    is_gate_task_type,
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
    # `coding` is NOT here any more. Its hold was lifted by operator decision
    # on 2026-09-18, after spec 12's gate-type question closed and the
    # worst-case formula was corrected to charge each gate per generation
    # attempt. It clears all six of 1.1's invariants -- $0.657 against a $1.00
    # budget, 1,726s against a 2,900s ceiling -- with the ceiling ENFORCED,
    # not merely with it switched off.
    #
    # Flipping it is what makes something route for the first time:
    # orchestrator.assign_model stops returning config.ANTHROPIC_MODEL for
    # coding-classified tasks and returns the ladder's rung 0 instead, which
    # is the free local model at 66% measured accuracy. That is the cost
    # thesis working as designed, and it is a real change in which model
    # answers, not just a flag.
    # `reasoning` is NOT here any more either. Amendment b782e4d held it
    # "until its 75% n=2 accuracy figure is either re-measured or enforced in
    # code" -- and it was re-measured: 2.6 now carries reasoning at n=6 for
    # five models. The hold was blocking on a condition that had already been
    # met, which is the worst state for a guard to be in, because it reads as
    # a live objection and is not one. Lifted by operator decision 2026-09-18.
    #
    # The map is now EMPTY. That is deliberate and not a sign the mechanism
    # was removed: a policy hold is a decision recorded in code, and there is
    # currently no undecided one. `_blockers_by_task_type` and the settings
    # page still read this map, and a future hold is one entry away.
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
            "available": True,
            "setting": DELEGATION_ENABLED_SETTING,
            "default": DELEGATION_ENABLED_DEFAULT,
            "note": "one global switch, on by default. Off means the design "
                    "is not in play at all: the capability table is not "
                    "validated at startup and the shadow recorder writes "
                    "nothing. Its live state is in the payload's `enabled` "
                    "field -- this block is read from constants and cannot "
                    "report a setting.",
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
    enforce_budget: bool = BUDGET_ENFORCEMENT_DEFAULT,
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

    A task type blocked neither way reports `{"policy": None, "data": [],
    "warnings": []}` -- never an invented problem.

    **An already-operational type is validated too, and that is deliberate.**
    It used to short-circuit to an empty result on the reasoning that a type
    which cleared the flip has nothing to report. That is only true while
    nothing changes underneath it, and things do: turning a gate type OFF is
    not validated (spec 12's coverage rule applies to flipping ON), so an
    operational type can become invalid without any action against it. The
    short-circuit meant the page showed such a type as clean and the only
    symptom was a refused restart later, with nothing connecting the two
    events. A live type that has gone invalid is the single most urgent thing
    this endpoint can say.
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
        # The budget overrun travels the same road: computed for every type
        # whatever the knob says, and appearing in `warnings` exactly when it
        # is not also blocking. The settings page reads `over_budget` rather
        # than pattern-matching this string, so the two never drift.
        over_budget = candidate.budget_breaches().get(task_type)
        if over_budget is not None and not enforce_budget:
            warnings.append(over_budget)
        problems = candidate.validate(
            known_models=known_models,
            enforce_latency_ceiling=enforce_latency_ceiling,
            enforce_budget=enforce_budget)
        # `validate()` is scoped to every type in the candidate's operational
        # set, not only the one being asked about here -- filter down to the
        # problems that actually name this task type.
        data_problems = [p for p in problems if p.startswith(f"{task_type}:")]
        # A policy hold is about FLIPPING, so it does not apply to a type
        # that is already operational -- reporting one there would tell an
        # operator to undo something the hold never stopped.
        policy = (None if task_type in operational_set
                  else _OPERATIONAL_FLIP_BLOCKED.get(task_type))
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
            # An explicit flag, not something the page infers from the text of
            # a warning. The dollar marker has to survive the message being
            # reworded, and a client grepping for "BUDGET_USD" would not.
            "over_budget": over_budget is not None,
        }
    return blockers


async def handle_delegation_get(request: Request):
    """GET /api/delegation -- the matrix, the flags, the derived ladders, and
    why each non-operational type cannot flip yet (see
    `_blockers_by_task_type`)."""
    rows = await db.delegation_rows_all()
    # Task 10: merge the benchmark subsystem's provenance columns into each
    # row so the Delegation page can render dormant markers, reorder
    # highlights and the Re-measure control without a second round trip.
    meta = {(r["model"], r["task_type"]): r
            for r in await capability_meta_all()}
    for row in rows:
        extra = meta.get((row["model"], row["task_type"])) or {}
        row["dormant"] = int(extra.get("dormant") or 0)
        row["reorder_flagged"] = int(extra.get("reorder_flagged") or 0)
        row["measured_at"] = extra.get("measured_at")
    operational = sorted(await db.delegation_operational_all())
    operational_set = set(operational)
    capability_rows = rows_to_capability(rows)
    table = CapabilityTable(capability_rows, operational=operational_set)
    task_types = sorted({row["task_type"] for row in rows})
    pins = await delegation_pin_all()
    table_with_pins = CapabilityTable(
        capability_rows, operational=operational_set, pins=pins)
    # Same live list, same fallback, as every other call site that validates
    # this table (see live_known_models's docstring) -- the blocker computed
    # here must agree with what a real flip attempt would say.
    known_models = await live_known_models()
    enforce_ceiling = await ceiling_enforcement_enabled()
    enforce_budget = await budget_enforcement_enabled()
    blockers = _blockers_by_task_type(
        capability_rows, operational_set, known_models, enforce_ceiling,
        enforce_budget)
    return JSONResponse({
        "rows": rows,
        "operational": operational,
        "ladders": {t: table_with_pins.ladder(t) for t in task_types},
        "generated_ladders": {
            t: table.generated_ladder(t) for t in task_types
        },
        "pins": pins,
        "editable_columns": list(_EDITABLE),
        "config": _config_overview(),
        "blockers": blockers,
        "enabled": {
            "enabled": await delegation_enabled(),
            "default": DELEGATION_ENABLED_DEFAULT,
            "setting": DELEGATION_ENABLED_SETTING,
        },
        "budget_enforcement": {
            "enabled": enforce_budget,
            "default": BUDGET_ENFORCEMENT_DEFAULT,
            "setting": BUDGET_ENFORCEMENT_SETTING,
        },
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
    unknown = set(data) - {"model", "task_type", "cost_basis"} - set(_EDITABLE)
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

    # The basis follows the number it describes. Stated explicitly, it is
    # stored. Not stated, it survives only while the cost it annotates is
    # unchanged -- a new rate arriving without a word about where it came from
    # makes the old provenance a false claim, and a false claim here is worse
    # than the silence this column was added to end. This is the whole reason
    # for the column: gpt-5.6-terra still carries a rate assumed from luna
    # eight times ago, and nothing in the table can say so.
    old_cost = existing.get("cost_per_1m_tokens") if existing else None
    new_cost = merged_columns.get("cost_per_1m_tokens")
    if "cost_basis" in data:
        raw_basis = data["cost_basis"]
        cost_basis = None if raw_basis is None else str(raw_basis).strip()[:200] or None
    elif existing and old_cost == new_cost:
        cost_basis = existing.get("cost_basis")
    else:
        cost_basis = None

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
        # `problems_with` is the single place that pairs a table with the
        # live settings of both enforcement knobs, and it carries the same
        # live-model list and the same fallback as
        # delegation_startup.validate_or_die. Spec 1.1 requires the same code
        # path and the same error text at every moment a table is validated:
        # if the startup check, this endpoint and the seed script ever
        # disagreed, a table would pass through one and be refused by another,
        # which is how an operator ends up with a deployment that will not
        # boot after its next restart.
        problems = await problems_with(table)
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))

    await db.delegation_row_set(
        model, task_type, cost_basis=cost_basis, **merged_columns)
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

    # Turning a GATE type off is validated too, and this half was missing.
    #
    # Spec 12's coverage rule says an ordinary type may not route through a
    # gate type that has not cleared 1.1 -- and it was enforced only when
    # flipping something ON. So switching `reviewer-gate` off under a live
    # `long-context` was accepted, and left a stored state that 1.1 refuses:
    # the console kept running and then failed to start on its next restart,
    # with nothing connecting the two events.
    #
    # Checked against the dependents rather than by revalidating the table,
    # because the message has to name what is in the way. "long-context is
    # operational and runs its gates on this type" tells an operator what to
    # do; a generic invariant failure does not.
    if not operational and task_type in GATE_CALLS:
        current = await db.delegation_operational_all()
        dependents = sorted(t for t in current if t not in GATE_CALLS)
        if dependents:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{task_type} cannot be turned off while "
                    f"{', '.join(dependents)} "
                    f"{'is' if len(dependents) == 1 else 'are'} operational: "
                    f"stages 3-5 of {'that type' if len(dependents) == 1 else 'those types'} "
                    f"run on it (spec 4.3-4.5, 12). Turn "
                    f"{'it' if len(dependents) == 1 else 'them'} off first, or "
                    f"this deployment will refuse to start on its next restart"
                ))

    if operational:
        rows = rows_to_capability(await db.delegation_rows_all())
        current = await db.delegation_operational_all()
        table = CapabilityTable(rows, operational=current | {task_type})
        # Same single definition as handle_row_put above -- see the comment
        # there for why these three callers must never disagree.
        problems = await problems_with(table)
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


async def handle_budget_enforcement_put(request: Request):
    """PUT /api/delegation/budget-enforcement -- 9.2's knob for 2.7's budget.

    Off by default (`BUDGET_ENFORCEMENT_DEFAULT`). See
    `tiered_delegation.BUDGET_ENFORCEMENT_SETTING` for why: the tree cost is
    computed from `LEAVES_PER_TREE`, the one input that is assumed rather than
    measured, and an over-estimated leaf count refuses task types that would
    in fact fit.

    Turning it ON is validated first, exactly as the ceiling's is, and for the
    same reason: storing the flag first would leave a deployment that refuses
    to start on its next restart, and a settings write that bricks the boot
    path is what 1.1's checks exist to prevent. Turning it OFF is never
    validated -- relaxing a blocking invariant cannot break another one.
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
                                  enforce_budget=True)
        if problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "enforcing the budget would invalidate an "
                    "already-operational task type, and this deployment would "
                    "refuse to start on its next restart: "
                    + "; ".join(problems)
                ))

    await setting_set(BUDGET_ENFORCEMENT_SETTING, "1" if enabled else "0")
    _log.info("delegation_budget_enforcement enabled=%s", enabled)
    return JSONResponse({"ok": True, "enabled": enabled})


async def handle_enabled_put(request: Request):
    """PUT /api/delegation/enabled -- spec 9.1's one global kill switch.

    On by default (`DELEGATION_ENABLED_DEFAULT`), because on is what this
    deployment did before the switch existed. Off means the design is not in
    play at all: `validate_or_die` does not run at startup, so a capability
    table nobody has fixed cannot stop the console booting, and the shadow
    recorder writes nothing.

    Turning it ON is validated first, for the same reason the two enforcement
    knobs are: switching on means `validate_or_die` WILL run at the next boot,
    so storing the flag against a table that breaks an invariant would leave a
    deployment that refuses to start hours later, with nothing to connect the
    failure to this click. Turning it OFF is never validated -- an off switch
    that can be refused is not an off switch, and 9.1 requires rollback to be
    one clean action.
    """
    _require_admin(request)
    data = _require_json_object(await request.json())
    if "enabled" not in data:
        raise HTTPException(status_code=400, detail="enabled is required")
    enabled = bool(data["enabled"])

    if enabled:
        rows = rows_to_capability(await db.delegation_rows_all())
        operational = await db.delegation_operational_all()
        problems = await problems_with(
            CapabilityTable(rows, operational=operational))
        if problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "turning delegation on would leave a table that fails spec "
                    "1.1, and this deployment would refuse to start on its "
                    "next restart: " + "; ".join(problems)
                ))

    await setting_set(DELEGATION_ENABLED_SETTING, "1" if enabled else "0")
    _log.info("delegation_enabled enabled=%s", enabled)
    return JSONResponse({"ok": True, "enabled": enabled})


@router.put("/api/delegation/enabled")
async def _api_delegation_enabled_put(request: Request):
    return await handle_enabled_put(request)


@router.put("/api/delegation/budget-enforcement")
async def _api_delegation_budget_enforcement_put(request: Request):
    return await handle_budget_enforcement_put(request)


@router.put("/api/delegation/operational")
async def _api_delegation_operational_put(request: Request):
    return await handle_operational_put(request)


@router.put("/api/delegation/ceiling-enforcement")
async def _api_delegation_ceiling_enforcement_put(request: Request):
    return await handle_ceiling_enforcement_put(request)


async def handle_ladder_pin_put(request: Request):
    """PUT /api/delegation/ladder -- set or clear one task type's ladder pin.

    Accepts {"task_type": "...", "rungs": ["model/one", ...]}.
    rungs=[] clears the pin (back to generated).
    rungs omitted → clears the pin.

    Validation:
      - Unknown task_type (not in delegation_capability rows): 400
      - Rung count > MAX_ATTEMPTS (or GATE_MAX_ATTEMPTS for gate types): 400
      - Duplicate rungs: 400
      - Model ID shape via ModelRouter.validate_model: 400
      Not refused: breaching pin, pin naming unmeasured model.

    Returns {"ok": true, "problems": [...]} -- problems are warnings,
    not refusals (a pin that breaches invariants is allowed to survive).
    """
    _require_admin(request)
    data = _require_json_object(await request.json())
    task_type = _require_str_field(data, "task_type")
    if not task_type:
        raise HTTPException(status_code=400, detail="task_type is required")

    raw_rungs = data.get("rungs")
    if raw_rungs is None:
        # No rungs key → clear the pin
        await delegation_pin_set(task_type, None)
        return JSONResponse({"ok": True, "problems": []})

    if not isinstance(raw_rungs, list):
        raise HTTPException(status_code=400, detail="rungs must be an array")

    rungs: list[str] = []
    for r in raw_rungs:
        if not isinstance(r, str):
            raise HTTPException(
                status_code=400, detail="each rung must be a string")
        s = r.strip()
        if not s:
            raise HTTPException(
                status_code=400, detail="rung string must not be blank")
        rungs.append(s)

    # Budget check: rung count must not exceed the attempt limit.
    budget = (GATE_MAX_ATTEMPTS if is_gate_task_type(task_type)
              else MAX_ATTEMPTS)
    if len(rungs) > budget:
        raise HTTPException(
            status_code=400,
            detail=f"{task_type}: pin has {len(rungs)} rungs, "
                   f"max {budget} (attempt budget exceeded)")

    # Duplicate check.
    if len(rungs) != len(set(rungs)):
        seen: set[str] = set()
        dups: list[str] = []
        for r in rungs:
            if r in seen:
                dups.append(r)
            seen.add(r)
        raise HTTPException(
            status_code=400,
            detail=f"{task_type}: duplicate rungs: {', '.join(sorted(set(dups)))}")

    # All rows for this task type.
    rows = await db.delegation_rows_all()
    task_rows = [r for r in rows if r["task_type"] == task_type]
    if not task_rows:
        raise HTTPException(
            status_code=400,
            detail=f"{task_type}: no delegation_capability rows for this task type")

    # Model ID shape check for each rung: bare string or backend/name with
    # both parts non-empty (same logic as _resolution_problems in the table).
    for model_id in rungs:
        backend, sep, name = model_id.partition("/")
        if sep and (not backend or not name):
            raise HTTPException(
                status_code=400,
                detail=f"{task_type}: rung {model_id!r} is not a valid "
                       "backend-and-model pair")

    # Now actually write the pin and surface any problems (warnings, not
    # refusals).
    await delegation_pin_set(task_type, rungs)

    # Compute what the generated ladder would look like, so the caller can
    # surface the delta on the page.
    capability_rows = rows_to_capability(rows)
    operational = await db.delegation_operational_all()
    pins = await delegation_pin_all()
    pins[task_type] = rungs  # the pin just written
    table = CapabilityTable(
        capability_rows, operational=operational, pins=pins)
    generated = table.generated_ladder(task_type)
    generated_pins = dict(pins)
    del generated_pins[task_type]
    gen_without_pin = CapabilityTable(
        capability_rows, operational=operational, pins=generated_pins)

    # Boot-safety: drop pins that would prevent startup. Log what is dropped.
    safe_table, dropped = table.without_unusable_pins()
    if dropped:
        for reason in dropped:
            _log.warning("delegation: pin dropped at boot: %s", reason)

    # Problems are warnings, not refusals: a pin that breaches invariants is
    # allowed to survive (operator decision 2026-09-18).
    known_models = await live_known_models()
    enforce_ceiling = await ceiling_enforcement_enabled()
    enforce_budget = await budget_enforcement_enabled()
    problems = safe_table.validate(
        known_models=known_models,
        enforce_latency_ceiling=enforce_ceiling,
        enforce_budget=enforce_budget)
    # Filter to only problems involving this task type.
    type_problems = [p for p in problems if p.startswith(f"{task_type}:")]

    return JSONResponse({
        "ok": True,
        "problems": type_problems,
        "generated": generated,
        "pinned": list(table.ladder(task_type)),
    })


@router.put("/api/delegation/ladder")
async def _api_delegation_ladder_pin_put(request: Request):
    return await handle_ladder_pin_put(request)
