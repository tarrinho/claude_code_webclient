"""delegation_pipeline.py -- which of the five stages run, and in what order.

Spec section 4.8's precedence table, as code. Three rules can each subtract
stages -- one of them can also restore a stage an earlier rule removed --
and the order they resolve in is the whole behaviour:

  1. Blast radius (4.6) first. It is the only rule keyed on what the task
     DID rather than what its text predicted, so it overrides the trivial
     bypass: a score-1 task that touched more than MAX_FILES_TRIVIAL files
     runs the full pipeline regardless of what the classifier guessed.
  2. Trivial bypass (4.8) next, if blast radius did not override it. Its
     effect depends on `mutates`: a trivial WRITE drops stages 3, 4 and 5;
     a trivial READ drops only 4 and 5 -- stage 3 is never removed from a
     read here, because 4.7 makes it the read's only real gate and 4.8 is
     explicit that no rule may take it away.
  3. Read-only (4.7) last, applied to whatever survives: mutates=False
     drops 4 and 5 (a no-op if step 2 already dropped them) and restores
     stage 3 if any earlier rule removed it. 4.8's invariant is that a
     read-only leaf never reaches the end without a reviewer gate, no
     matter how the earlier rules resolved.

`mutates` is the three-valued string from delegation_classifier, not a
bool. Only the literal MUTATES_FALSE value is "read-only" for this module:
MUTATES_SIDE_EFFECTING_READ spends money or consumes an external rate
limit, so 4.7 explicitly holds it to the same five stages as MUTATES_TRUE,
including under the trivial bypass, which has no read-shaped row for it.

Stage 2 (the oracle) is a special case worth stating plainly. 4.2 and 4.7
both make it conditional on the output being executable -- parse, import,
compile, or pass-the-test -- and 4.8's net-effect table gives a read two
different rows for exactly this reason: "non-trivial read, executable
output" keeps stage 2, "non-trivial read, prose output" does not. This
function cannot make that call: `stages_for` runs before stage 1 has
produced anything, so whether the output will turn out to be executable
is not yet knowable here. Stage 2's presence or absence is therefore a
*runtime* decision belonging to the caller, made after generation, not a
*planning* decision this function can resolve in advance. Accordingly, 2
appears in every list this function returns whenever no rule here has
removed it -- it means "stage 2 is eligible," not "stage 2 will run." The
caller is expected to skip it at execution time if the generated output
turns out to be prose, per 4.2's "skipped rather than run" rule; a 2 in
the returned list is not a promise that the oracle executes.
"""
from __future__ import annotations

from typing import Final

from delegation_classifier import Classification, MUTATES_FALSE
from tiered_delegation import (
    CapabilityTable,
    SIZE_FACTOR,
    TIER0_DEADLINE,
    unknown_type_baseline_s,
)

#: Above this many changed files, a task gets the full pipeline whatever the
#: classifier's score said (4.6).
MAX_FILES_TRIVIAL: Final[int] = 3

#: At or below this score, a task is a candidate for the trivial bypass (4.8).
TRIVIAL_SCORE: Final[int] = 1

ALL_STAGES: Final[tuple[int, ...]] = (1, 2, 3, 4, 5)


def stages_for(decision: Classification, files_changed: int) -> list[int]:
    """The stages, in ascending order, that run for one leaf.

    Implements the precedence in section 4.8: blast radius, then the
    trivial bypass, then read-only -- read-only applied last so it can
    restore stage 3 no matter what the earlier two rules did.
    """
    stages = set(ALL_STAGES)

    is_read_only = decision.mutates == MUTATES_FALSE
    over_blast_radius = files_changed > MAX_FILES_TRIVIAL
    trivial_bypass_applies = decision.score <= TRIVIAL_SCORE and not over_blast_radius

    # 2. Trivial bypass, scoped by mutates (4.8): a trivial write loses its
    #    reviewer gate along with QA and security; a trivial read never
    #    loses the reviewer gate, only QA and security.
    if trivial_bypass_applies:
        if is_read_only:
            stages -= {4, 5}
        else:
            stages -= {3, 4, 5}

    # 3. Read-only, last, applied to whatever survives (4.7): drop 4 and 5,
    #    and restore 3 if an earlier rule removed it -- a read-only leaf
    #    never ends up with no verification at all.
    if is_read_only:
        stages -= {4, 5}
        stages.add(3)

    return sorted(stages)


# --- deadlines (spec 5.1) ----------------------------------------------------
#
# TIER0_DEADLINE, SIZE_FACTOR and LATENCY_CEILING_S already live in
# tiered_delegation, alongside the ladder generator they must never drift
# from -- see that module's header and spec 5.1's own argument for deriving
# the deadline and the ladder from one table. They are imported above, not
# redefined here: a second copy under a second name is exactly how a
# re-benchmark updates one and not the other.


def speed_multiplier(table: CapabilityTable, model: str, task_type: str) -> float:
    """5.1's model-speed multiplier: this model's own measured latency
    divided by the fastest **ladder-eligible** model's latency for
    `task_type` (`CapabilityTable.latency_reference_s`).

    The reference set is ladder-eligible rows only, never every row: a
    cost-excluded or unmeasured model can never be a rung, so letting it set
    the 1.0 reference would shrink the deadline of every model that can
    actually run one. `latency_reference_s` already enforces that filter;
    this function does not repeat it, it only divides by it.

    Falls back to 1.0 -- the reference model's own multiplier, never a
    fraction below it -- when the reference cannot be computed (an
    unmeasured task type) or when `model` has no measured latency under
    `task_type` at all. A multiplier manufactured out of missing data must
    not shrink a deadline.
    """
    reference = table.latency_reference_s(task_type)
    if reference is None or reference <= 0:
        return 1.0
    mine = next(
        (r.median_latency_s for r in table.rows_for(task_type)
         if r.model == model and r.median_latency_s is not None),
        None,
    )
    if mine is None:
        return 1.0
    return mine / reference


def effective_deadline(table: CapabilityTable, model: str, task_type: str,
                        score: int) -> float:
    """5.1: effective_deadline = per_type_baseline x size_factor x
    model_speed_multiplier, for one generation attempt.

    An unknown task type (absent from `TIER0_DEADLINE`) takes
    `unknown_type_baseline_s()` -- the LONGEST baseline, never the shortest:
    "we have not measured this" must not become a timeout.
    """
    baseline = TIER0_DEADLINE.get(task_type)
    if baseline is None:
        baseline = unknown_type_baseline_s()
    size = SIZE_FACTOR.get(score, 1.0)
    return float(baseline) * size * speed_multiplier(table, model, task_type)


def gate_effective_deadline(table: CapabilityTable, task_type: str,
                             score: int) -> tuple[float | None, str | None]:
    """5.1: a model-backed gate's deadline (stages 3-5, spec 4.3-4.5).

    Baseline and size factor come from the LEAF's own task type and score,
    same as `effective_deadline`, but the multiplier is the GATE model's --
    `CapabilityTable.gate_latency_s` (the floor `reviewer-gate` rung, or its
    fallback to the leaf task type's own row for that model) divided by the
    leaf task type's own reference. This is spec 5.1's worked example:
    reference 12.8 (luna, coding) against the gate row 11.1 (luna,
    reviewer-gate) gives a gate multiplier of 11.1/12.8, not a `coding`
    generation multiplier.

    Uses the table's public `gate_latency_s` accessor rather than
    re-deriving which model is the gate floor and which row times it --
    that logic already lives in `tiered_delegation.CapabilityTable` and
    duplicating it is how the two would drift apart.

    Returns `(value, None)` or `(None, reason)` -- the same `(value,
    reason)` shape as `CapabilityTable._worst_case`, `_gate_latency_s` and
    `gate_latency_s`, which this function is built directly on top of. A
    caller aggregating problems (spec 1.1's validator shape) can append the
    reason string it is handed rather than catching an exception and
    stringifying it. A gate deadline that silently defaulted instead would
    hide exactly the missing-data case that reason string exists to report.
    """
    gate_latency, reason = table.gate_latency_s(task_type)
    if gate_latency is None:
        return None, reason
    reference = table.latency_reference_s(task_type)
    if reference is None or reference <= 0:
        return None, (
            f"{task_type}: no ladder-eligible row has a usable "
            f"median_latency_s to be the 1.0 reference (spec 5.1)"
        )
    baseline = TIER0_DEADLINE.get(task_type)
    if baseline is None:
        baseline = unknown_type_baseline_s()
    size = SIZE_FACTOR.get(score, 1.0)
    return float(baseline) * size * (gate_latency / reference), None
