"""delegation_pipeline.py -- which of the five stages run, and in what order.

Spec section 4.8's precedence table, as code. Three rules can each subtract
stages -- one of them can also restore a stage an earlier rule removed --
and the order they resolve in is the whole behaviour:

  1. Blast radius (4.6) first. It is the only rule keyed on what the task
     DID rather than what its text predicted, so it overrides the trivial
     bypass: a score-1 task that touched more than MAX_FILES_TRIVIAL files
     runs the full pipeline regardless of what the classifier guessed.
  2. Trivial bypass (4.8) next, if blast radius did not override it. Its
     effect depends on `mutates`: a trivial WRITE (`mutates=True`) drops
     stages 3, 4 and 5; anything else -- `mutates=False` or
     `side_effecting_read` -- drops only 4 and 5. Stage 3 is never removed
     from either read variant by this rule: 4.8 scopes the bypass to cases
     where an oracle can still check the output, and that is a write
     argument, not a read/write-row-membership argument. A trivial write's
     output is code stage 2 can execute; a trivial read's -- side-effecting
     or not -- may be prose stage 2 cannot check at all (4.2, 4.7), so
     neither read variant may lose its only real gate here.
  3. Read-only (4.7) last, applied to whatever survives: mutates=False
     drops 4 and 5 (a no-op if step 2 already dropped them) and restores
     stage 3 if any earlier rule removed it. 4.8's invariant is that a
     read-only leaf never reaches the end without a reviewer gate, no
     matter how the earlier rules resolved.

`mutates` is the three-valued string from delegation_classifier, not a
bool. Only the literal MUTATES_FALSE value is "read-only" for this
module's read-only-restore purposes (step 3 above): a NON-trivial
MUTATES_SIDE_EFFECTING_READ still takes the same five stages as
MUTATES_TRUE, because it spends money or consumes an external rate limit
and 4.7 does not exempt it. But the TRIVIAL bypass (step 2) is scoped by
whether an oracle can still verify the output, not by that same
read/write split, so a trivial side_effecting_read keeps stage 3 -- this
reverses a 2026-09-16 ruling that had aligned it with MUTATES_TRUE under
the bypass specifically because 4.7 aligns it with MUTATES_TRUE elsewhere.
That analogy does not hold for the bypass: the bypass's own stated
justification is that a trivial write is cheap to verify by oracle, which
is an argument about executable output, and it does not transfer to a
read whose oracle may be vacuous. See this file's git history and spec
4.8 for the earlier reasoning and why it was reversed.

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

from dataclasses import dataclass
from typing import Final

from delegation_classifier import Classification, MUTATES_FALSE, MUTATES_TRUE
from tiered_delegation import (
    CapabilityTable,
    MAX_ATTEMPTS,
    SIZE_FACTOR,
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
    is_write = decision.mutates == MUTATES_TRUE
    over_blast_radius = files_changed > MAX_FILES_TRIVIAL
    trivial_bypass_applies = decision.score <= TRIVIAL_SCORE and not over_blast_radius

    # 2. Trivial bypass, scoped by whether an oracle can still verify the
    #    output (4.8), not by read/write-row membership: a trivial WRITE's
    #    output is code stage 2 can execute, so it loses its reviewer gate
    #    along with QA and security. Everything else -- mutates=False or
    #    side_effecting_read -- may produce prose stage 2 cannot check, so
    #    only QA and security are dropped; stage 3 is never removed here
    #    for either read variant.
    if trivial_bypass_applies:
        if is_write:
            stages -= {3, 4, 5}
        else:
            stages -= {4, 5}

    # 3. Read-only, last, applied to whatever survives (4.7): drop 4 and 5,
    #    and restore 3 if an earlier rule removed it -- a read-only leaf
    #    never ends up with no verification at all. This rule proper is
    #    mutates=False only; for side_effecting_read the trivial bypass
    #    above never removed 3 in the first place, so there is nothing
    #    left for this step to restore on that path.
    if is_read_only:
        stages -= {4, 5}
        stages.add(3)

    return sorted(stages)


# --- deadlines (spec 5.1) ----------------------------------------------------
#
# The per-type baseline, SIZE_FACTOR and LATENCY_CEILING_S already live in
# tiered_delegation, alongside the ladder generator they must never drift
# from -- see that module's header and spec 5.1's own argument for deriving
# the deadline and the ladder from one table. SIZE_FACTOR is imported above,
# not redefined here: a second copy under a second name is exactly how a
# re-benchmark updates one and not the other. The baseline is not imported at
# all, because it is no longer a constant -- `CapabilityTable` derives it from
# the same table and the same 1.0 reference the multipliers use.


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

    The baseline comes from `CapabilityTable.baseline_deadline_s`, which
    derives it against the same 1.0 reference `speed_multiplier` divides by --
    never from a fixed seconds map. A baseline fixed while the multiplier is
    derived makes the two disagree whenever the reference model changes; see
    that method's docstring and 5.1's 2026-09-17 subsection. An unknown task
    type (absent from `TIER0_BASELINE_CALIBRATION`) takes the LONGEST baseline,
    never the shortest: "we have not measured this" must not become a timeout.
    """
    baseline = table.baseline_deadline_s(task_type)
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
    baseline = table.baseline_deadline_s(task_type)
    size = SIZE_FACTOR.get(score, 1.0)
    return float(baseline) * size * (gate_latency / reference), None


# --- review gates (spec 4.3-4.5) ---------------------------------------------
#
# Nothing in this module calls any of the below yet -- it is the result type
# and the escalation rules stages 3-5 will run on, added ahead of the code
# that runs the stages. `MAX_ATTEMPTS` is imported from tiered_delegation
# above rather than redefined here: that module already carries it as the
# generation-attempts control from spec 5, and a second copy under a second
# name is exactly how a re-benchmark would update one and miss the other.
#
# None of the functions below can fail for a *data* reason -- each is a pure
# function of the ints/strings it is handed, with no missing-data case to
# report -- so none of them takes the `(value, reason)` shape that
# `gate_effective_deadline` and `CapabilityTable._worst_case` / `gate_latency_s`
# use for the functions that *can* fail that way. `GateResult.__post_init__`
# and `gate_exhaustion_outcome` do raise `ValueError` on an unrecognised
# `gate` name, but that is a caller-error check, not a missing-data one --
# see their docstrings -- so it stays a plain raise rather than borrowing the
# `(value, reason)` shape.


#: The three stage-3/4/5 gate names (4.3-4.5), following the same pattern as
#: `delegation_classifier`'s MUTATES_FALSE/SIDE_EFFECTING_READ/TRUE: a named
#: constant per value instead of a bare string, so a typo or a stray literal
#: ("Security", "security_gate", the stage number 5) cannot pass as a gate
#: name -- see `_VALID_GATES` and the validation in `GateResult.__post_init__`
#: and `gate_exhaustion_outcome` below, which is the fix for exactly that.
GATE_REVIEWER: Final[str] = "reviewer"
GATE_QA: Final[str] = "qa"
GATE_SECURITY: Final[str] = "security"

#: Every gate name this module recognises. Anything else is rejected rather
#: than defaulted -- see the docstrings below for why a silent default here
#: is the release's canonical cannot-be-noticed-until-wired defect: an
#: unrecognised gate used to compare unequal to "security" and fall through
#: to the reviewer/QA path (`LEAF_FAILED`), silently turning off 4.5's
#: human-escalation rule with no error and no failing test.
_VALID_GATES: Final[frozenset[str]] = frozenset({GATE_REVIEWER, GATE_QA, GATE_SECURITY})


@dataclass(frozen=True)
class GateResult:
    """One stage-3/4/5 gate's verdict on one generation attempt (4.3-4.5).

    `gate` must be one of `GATE_REVIEWER`, `GATE_QA` or `GATE_SECURITY` --
    each tagged with its own name so an escalation is traceable to which
    gate rejected it (spec 10), and so a security rejection can be told
    apart from a reviewer or QA one downstream (see `gate_exhaustion_outcome`)
    without inspecting `reason`.

    Construction raises `ValueError` for any other value. This is a
    programmer-error check, not a missing-data one -- unlike
    `gate_effective_deadline` or `CapabilityTable.gate_latency_s`, there is
    no legitimate case where a caller has a real gate and this module simply
    does not have data for it yet; an unrecognised `gate` string is always a
    mistake at the call site, so it is raised immediately at construction
    rather than threaded through as a `(value, reason)` pair for a caller to
    check later.
    """
    gate: str
    passed: bool
    reason: str

    def __post_init__(self) -> None:
        if self.gate not in _VALID_GATES:
            raise ValueError(
                f"unrecognised gate {self.gate!r}; expected one of "
                f"GATE_REVIEWER ({GATE_REVIEWER!r}), GATE_QA ({GATE_QA!r}), "
                f"GATE_SECURITY ({GATE_SECURITY!r})"
            )


#: 4.5: generation -> security review -> fix -> security review. After this
#: many completed cycles, a security rejection is escalated to a human
#: rather than recorded as a failed leaf -- see `gate_exhaustion_outcome`.
#: Not the tree's MAX_DEPTH or MAX_NODES: those do not track cycles, and a
#: security fix can introduce a new vulnerability, so something has to
#: bound the round-trip itself.
SECURITY_RERUN_CAP: Final[int] = 2


def next_generator_rung(current: int, gate: GateResult,
                        max_attempts: int = MAX_ATTEMPTS) -> int:
    """A rejection escalates the GENERATOR, never the reviewer.

    All three gates move the generator the same way: 4.3 sets the rule for
    the reviewer gate, 4.4 gives QA "the same path", and 4.5 explicitly
    keeps it "as before" for security too -- one rung on rejection, capped
    at `max_attempts - 1` so a leaf pinned at its top rung never invents a
    fourth. The security gate's extra behaviour from 4.5 -- routing back
    with the vulnerability flagged, the gate's own rung climbing, and
    eventually escalating to a human instead of failing the leaf -- lives
    on separate axes (`next_gate_rung`, `security_exhausted`,
    `gate_exhaustion_outcome`), not a different number returned from here.
    This function does not take the gate's name into account beyond
    `gate.passed`.
    """
    if gate.passed:
        return current
    return min(current + 1, max_attempts - 1)


def next_gate_rung(current: int, gate: GateResult, generator_rung: int,
                    generator_max_rung: int, max_rung: int) -> int:
    """The GATE's own rung (spec 4.3, 4.5's "the gate climbs too").

    Mirrors `next_generator_rung`'s shape -- current rung in, next rung
    out, capped -- but on a different condition. The gate does not climb on
    every rejection, only when it rejects output produced at the
    generator's own top rung (`generator_rung == generator_max_rung`):
    "rejecting the best generator available is evidence about the gate,
    not about the code" (4.5). An ordinary rejection at any lower generator
    rung leaves the gate exactly where it is, to re-review the escalated
    generator's next attempt -- the reviewer "stays at Luna" case 4.3
    already describes.

    `generator_max_rung` should be the same top-rung index the caller is
    passing as `max_attempts - 1` to `next_generator_rung` for this same
    leaf -- "top rung" has to mean the same generator position on both
    sides of the pipeline, or a gate could climb on a generator attempt
    that was not actually its last available one.

    `max_rung` bounds the gate's own climb and is a plain parameter, never
    a value looked up from a capability table here. Which concrete model
    sits at the gate's own top rung -- and how many rungs a gate ladder
    even has -- is exactly the `reviewer-gate` ladder-eligibility question
    section 12 defers as an open item; this function decides none of that,
    the same way `SECURITY_RERUN_CAP = 2` decides none of it either. A
    caller resolves the model-name question separately and passes the
    resulting integer in.
    """
    if gate.passed:
        return current
    if generator_rung < generator_max_rung:
        return current
    return min(current + 1, max_rung)


def security_exhausted(cycles: int) -> bool:
    """True once the security gate has been round-tripped past its cap
    (spec 4.5): generation -> security review -> fix -> security review,
    twice, with the same objection still standing on the third.
    """
    return cycles > SECURITY_RERUN_CAP


#: `gate_exhaustion_outcome`'s three possible answers, spec 4.5. Plain
#: string constants rather than a bool: `delegation_classifier`'s
#: three-valued `mutates` already sets the precedent in this codebase for
#: naming a multi-way outcome this way instead of overloading True/False.
#:
#: 4.5 names two different triggers for human involvement, in different
#: words, and this module reads the wording as two different outcomes
#: rather than one restated:
#:   - the 2-cycle cap (the pre-existing rule): "the leaf fails with a
#:     human-flag" -- still a FAILED leaf, a human is alerted alongside it.
#:   - the gate's own top rung, still rejecting after it climbed there
#:     ("the addition"): "escalated to a human, not recorded as a failed
#:     leaf" -- explicitly NOT a failure.
#: Collapsing the two into one constant would lose that "not recorded as a
#: failed leaf" is a stronger claim than "fails ... with a human-flag";
#: they are kept apart here so a caller cannot make that mistake silently.
LEAF_FAILED: Final[str] = "failed_leaf"
FAILED_HUMAN_FLAGGED: Final[str] = "failed_human_flagged"
ESCALATED_TO_HUMAN: Final[str] = "escalated_to_human"


def gate_exhaustion_outcome(gate: str, cycles: int, gate_rung: int,
                            gate_max_rung: int) -> str:
    """What happens to the leaf once a gate is exhausted (spec 4.5).

    Raises `ValueError` if `gate` is not one of `GATE_REVIEWER`, `GATE_QA` or
    `GATE_SECURITY`. This function takes a bare `gate: str`, not a
    `GateResult`, so `GateResult.__post_init__` validating its own field does
    not cover a caller that constructs the string by hand and passes it here
    directly -- this is the exact function the follow-up called out
    (`if gate != "security": return LEAF_FAILED`), where a typo'd or
    stage-numbered gate used to silently take the safe-looking reviewer/QA
    path instead of erroring, disabling 4.5's human-escalation rule with no
    failing test. Checked before anything else below, so an unrecognised
    name never reaches the "not security" fallthrough.

    A reviewer or QA gate that keeps rejecting the generator's top rung is
    an ordinary failed leaf -- 4.3 and 4.4 give neither of them a cap-based
    or rung-based escape hatch, so `gate` values other than `GATE_SECURITY`
    always return `LEAF_FAILED` regardless of the other arguments.

    The security gate has two distinct exhaustion conditions, checked in
    this order:

    1. **The gate's own top rung** (`gate_rung >= gate_max_rung`), still
       rejecting: 4.5's addition, `ESCALATED_TO_HUMAN` -- not a failed
       leaf, because a false reject at the gate's ceiling is indistinguish-
       able from a true one without judgement, and discarding correct work
       silently is the worse of the two errors. Checked first, and wins
       even if the cycle cap has also been reached: 4.5 introduces this
       rung-based rule as "the addition" layered on top of the pre-existing
       cycle cap, and states it in stronger terms ("not recorded as a
       failed leaf" versus "fails ... with a human-flag") -- the
       false-reject risk that motivates it does not go away just because
       the cycle count ran out at the same time.
    2. **The 2-cycle cap** (`security_exhausted(cycles)`), gate not yet at
       its own top rung: the pre-existing rule, `FAILED_HUMAN_FLAGGED` --
       still a failed leaf, with a human alerted alongside it.

    Neither check is `gate == GATE_SECURITY` alone: a security gate still
    inside both its own rung cap and its cycle cap is an ordinary
    `LEAF_FAILED`, same as reviewer or QA.
    """
    if gate not in _VALID_GATES:
        raise ValueError(
            f"unrecognised gate {gate!r}; expected one of "
            f"GATE_REVIEWER ({GATE_REVIEWER!r}), GATE_QA ({GATE_QA!r}), "
            f"GATE_SECURITY ({GATE_SECURITY!r})"
        )
    if gate != GATE_SECURITY:
        return LEAF_FAILED
    if gate_rung >= gate_max_rung:
        return ESCALATED_TO_HUMAN
    if security_exhausted(cycles):
        return FAILED_HUMAN_FLAGGED
    return LEAF_FAILED
