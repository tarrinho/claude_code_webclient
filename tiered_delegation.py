"""Capability table and escalation-ladder generation.

Stage 1 of docs/superpowers/specs/2026-09-14-tiered-agent-delegation-spec-v3.md
-- sections 1.1, 2.6, 2.7, 3 and 5.1.

Scope, stated up front because a passing suite here means less than it looks
like it means. This module decides *which model would be tried in what order*
for a task type. It does not route anything. `ModelRouter.assign_model` is
untouched, every task type ships non-operational, and a non-operational type
falls back to today's routing. Section 12 of the spec carries three open items,
each with an explicit "do not", and the third forbids flipping `coding` to
operational until gate-type validation is decided. This is the machinery those
decisions get applied to.

Deliberately pure: no database, no import from `app`. The table is passed in.
That keeps the generator testable against a constructed table rather than
against production data, which matters because section 2.6 ships mostly
unmeasured -- a module that read the live table would encode today's
measurement state into its own tests.

One qualification to that purity, added with 1.1's model-resolution invariant:
`validate()` takes the model combo box (9.3) as an argument, and falls back to
`config.KNOWN_MODELS` when the caller passes nothing. The import is inside the
function, so importing this module still reads no configuration, and every test
supplies the set explicitly. See `default_known_models`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable

# ── Cost ceiling (spec 2.7) ──────────────────────────────────────────────────
#
# Excluded from every ladder by operator decision, recorded 2026-09-15.
#
# This is NOT derived from the numbers, and the distinction is the whole point.
# Measured per token, mini (0.5261/1M) is 2.99x cheaper than sonnet (1.5709/1M),
# which the ladders keep as a rung on six task types -- so no threshold that
# admits sonnet can exclude mini. The earlier per-request ordering that did
# exclude it was an artifact of who sent what: mini's historical requests
# average 28,538 tokens against sonnet's 928, so mini looked dear because it was
# handed large jobs, not because it charges more.
#
# A cost-derived exclusion would reverse itself the moment rates moved, and the
# ceiling would have to be re-tuned to keep mini out. A decision does not move
# when the numbers do. Nothing recomputes this; re-admitting mini means editing
# this line.
EXCLUDED_MODELS: Final[frozenset[str]] = frozenset({
    "azure_ai/gpt-5.4-mini",
})

# Per-task-type expected size, in tokens, for the cost ceiling (spec 2.7:
# "the model's rate times the task type's expected size").
#
# The spec names `expected_tokens(task_type)` but never fixes a number for it.
# Resolved here rather than left ambiguous: it is a per-task-type constant, so
# it multiplies every candidate's cost by the same factor and therefore cannot
# reorder a ladder. It matters for the two budget invariants in 1.1 and not at
# all for the generator -- a property pinned by
# test_ladder_order_does_not_depend_on_expected_tokens.
#
# The default is the one measured figure the spec offers for a leaf-sized call
# (2.7's worked example uses a 12,000-token call). Task types with a measured
# size belong in this map; everything else takes the default, and a wrong
# default costs accuracy in the budget check, never correctness in the ladder.
DEFAULT_EXPECTED_TOKENS: Final[int] = 12_000
EXPECTED_TOKENS: Final[dict[str, int]] = {}

# The five columns 1.1 requires on a ladder-eligible row of an operational type.
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context",
)

# ── Latency (spec 5.1) ───────────────────────────────────────────────────────
#
# Every constant below is quoted from the spec beside its citation. None of
# them is tuned here; 5.1 says outright that the ceiling is derived and "not an
# independent constant to be tuned on its own", and that the arithmetic behind
# it has been redone five times without the ceiling ever moving.

#: 5.1's per-type baseline: what the task type costs on a mid-sized instance of
#: that task, on the fastest model measured for it.
TIER0_DEADLINE: Final[dict[str, int]] = {
    "long-context": 45,     # seconds
    "coding": 90,
}

#: 5.1: "An unknown type must receive the **longest** deadline, never the
#: shortest." Taken as the maximum of the map rather than written out, so
#: adding a slower type cannot leave this behind pointing at the old maximum.
def unknown_type_baseline_s() -> int:
    return max(TIER0_DEADLINE.values())


#: 5.1's size factor, keyed by the classifier's complexity score (section 2).
SIZE_FACTOR: Final[dict[int, float]] = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5, 5: 2.0}

#: The binding case for the 1.1 check is the highest score the type can reach,
#: and that is 5 for every type, not 4: section 2.1 takes the maximum score
#: across all matched patterns, and the score-5 pattern
#: (`implement.*multiple|coordinate.*agent|orchestrate`) can match alongside any
#: other type's pattern. 5.1 makes exactly this argument for `coding` --
#: "Budgeting to score 4 would leave the ceiling below the worst case for a task
#: the classifier produces by ordinary means" -- and it generalises, so the check
#: never budgets a type below a score it can actually be given.
BINDING_SIZE_FACTOR: Final[float] = SIZE_FACTOR[5]

#: Section 5's control table. MAX_ATTEMPTS exists nowhere in this codebase yet,
#: so the value comes from the spec rather than from an import.
MAX_ATTEMPTS: Final[int] = 3            # generation attempts, per leaf (5)
MODEL_GATE_COUNT: Final[int] = 3        # stages 3-5, one model call each (4.3-4.5)

#: 5 ("combined latency ceiling | 1,500 -- derived, see 5.1"), per leaf, all
#: five stages.
LATENCY_CEILING_S: Final[int] = 1_500

#: 3 and 4.3: all three review gates run on one task type, so there is no
#: separate security-gate type and the table keeps nine rows.
GATE_TASK_TYPE: Final[str] = "reviewer-gate"

# ── Tree cost (spec 2.7) ─────────────────────────────────────────────────────

#: 2.7, measured 2026-09-15 (`bench/pipeline_ab.py`, 6 leaves).
TOKENS_PER_LEAF: Final[int] = 59_460

#: 2.7, and the one input on this list that is **assumed, never measured**: it
#: is `MAX_NODES` (section 5) used as an upper bound. 1.2 explains why it cannot
#: be measured from this deployment -- no orchestrator task has ever decomposed,
#: so no tree has ever been formed. Being an upper bound makes it the
#: conservative choice: the true figure is almost certainly smaller, and a
#: smaller figure admits more models, so nothing is excluded here in error.
LEAVES_PER_TREE: Final[int] = 40

#: 2.7's reach probabilities, indexed by rung. Rung 0 runs on every leaf;
#: rungs 1 and 2 are measured at n=6.
#:
#: Rung 2 is carried as the exact 1/6, which 2.7 now states outright ("one leaf
#: of six. The cost table below is computed from the exact `1/6`, not from a
#: rounded 0.17; at 0.17 every cell of it disagrees"). An earlier revision of
#: that input table printed 0.17, and it was corrected on the evidence this
#: check produced: at 0.17, `claude-sonnet-5` at rung 2 comes out at $0.635
#: against the published $0.623, and `azure_ai/gpt-5.4-mini` at $0.213 against
#: the published $0.209. The published costs are the arithmetic this invariant
#: has to agree with, so the probability is carried unrounded.
REACH_PROBABILITY: Final[tuple[float, ...]] = (1.0, 0.5, 1.0 / 6.0)

#: Section 5: per **tree**. All leaves share one pool -- it is not per goal and
#: not per leaf.
BUDGET_USD: Final[float] = 1.00


def expected_tokens(task_type: str) -> int:
    """The task type's expected call size, for the cost ceiling."""
    return EXPECTED_TOKENS.get(task_type, DEFAULT_EXPECTED_TOKENS)


def rung_cost_usd(rung: int, cost_per_1m_tokens: float) -> float:
    """What one rung contributes to the expected tree cost (spec 2.7).

        leaves_per_tree x tokens_per_leaf x P(reach rung) x rate

    This is the per-cell arithmetic behind 2.7's "cost of placing each model at
    each rung" table, and it reproduces every cell of it.

    Raises `ValueError` for a rung 2.7 publishes no reach probability for. The
    caller turns that into a 1.1 problem string; it is never treated as zero,
    which is the one failure mode a deeper ladder must not have.
    """
    if rung < 0 or rung >= len(REACH_PROBABILITY):
        raise ValueError(
            f"spec 2.7 publishes reach probabilities for rungs "
            f"0-{len(REACH_PROBABILITY) - 1} only; rung {rung} cannot be priced"
        )
    return (LEAVES_PER_TREE * TOKENS_PER_LEAF
            * REACH_PROBABILITY[rung] * cost_per_1m_tokens / 1_000_000)


def default_known_models() -> frozenset[str]:
    """The model combo box's built-in contents (spec 9.3).

    `config.KNOWN_MODELS` is the list the models page falls back to when the
    active machine cannot be asked what it serves, and it holds Anthropic model
    ids only. It is therefore a partial answer to 1.1's model-resolution
    invariant, and `validate()` treats it as one: see `_resolution_problems`.

    Imported here rather than at module scope so that importing this module
    still reads no configuration.
    """
    import config
    return frozenset(config.KNOWN_MODELS)


# Private alias. `ladder()` takes a parameter called `expected_tokens` so the
# keyword reads the same at both call sites, which shadows the function inside
# that body; reaching the module-level name through globals() there worked but
# breaks silently under any rename. The alias is bound once, here.
_default_expected_tokens = expected_tokens


@dataclass(frozen=True)
class CapabilityRow:
    """One (model, task_type) pair from spec 2.6.

    `accuracy` is None for TBD. `cost_per_1m_tokens` is None for *unpriced*,
    which is not the same as 0.0: zero is a real rate for the self-hosted model
    and means free, while None means no rate from any source and makes the row
    ineligible (2.7: "still not treated as free").
    """
    model: str
    task_type: str
    accuracy: float | None
    n: int | None
    cost_per_1m_tokens: float | None
    median_latency_s: float | None
    max_context: int | None

    def missing_columns(self) -> list[str]:
        return [c for c in _REQUIRED_COLUMNS if getattr(self, c) is None]


class CapabilityTable:
    """The benchmark table, plus the ladder generator that walks it."""

    def __init__(self, rows: Iterable[CapabilityRow],
                 operational: Iterable[str] = ()) -> None:
        self._rows = list(rows)
        # Default false, per 1.1's bootstrap exemption. A type is submitted to
        # validation by being flipped here, and that is the only way it becomes
        # routable -- so no type can go live on unmeasured data.
        self._operational = frozenset(operational)

    # ── Rows and eligibility (spec 2.6) ─────────────────────────────────────

    def rows_for(self, task_type: str) -> list[CapabilityRow]:
        return [r for r in self._rows if r.task_type == task_type]

    def is_operational(self, task_type: str) -> bool:
        return task_type in self._operational

    def is_ladder_eligible(self, row: CapabilityRow) -> bool:
        """Spec 2.6's one predicate. There is no second notion of "candidate".

        A row exists (the caller found it), its accuracy is measured, and it
        survives the cost ceiling. An unmeasured accuracy removes the row
        outright: TBD is not "measured worse", it is not measured, and
        conflating the two would let cheapest-first put an unmeasured model at
        rung 0.
        """
        if row.accuracy is None:
            return False
        if row.model in EXCLUDED_MODELS:
            return False
        if row.cost_per_1m_tokens is None:      # unpriced, not free
            return False
        return True

    def ladder_eligible(self, task_type: str) -> list[CapabilityRow]:
        return [r for r in self.rows_for(task_type) if self.is_ladder_eligible(r)]

    # ── Cost (spec 2.7) ─────────────────────────────────────────────────────

    def effective_cost_per_task(self, model: str, task_type: str,
                                expected: int | None = None) -> float | None:
        """Rate x size. Never a per-request figure -- that unit is what made
        mini look dearer than sonnet when it is 2.99x cheaper per token."""
        size = expected_tokens(task_type) if expected is None else expected
        for row in self.rows_for(task_type):
            if row.model == model:
                if row.cost_per_1m_tokens is None:
                    return None
                return row.cost_per_1m_tokens * size / 1_000_000
        return None

    # ── Generation (spec 3) ─────────────────────────────────────────────────

    def ladder(self, task_type: str, expected_tokens: int | None = None) -> list[str]:
        """The three steps of spec 3, in order.

        1. take the ladder-eligible models for the task type;
        2. sort cheapest-first on effective cost per task;
        3. walk that order, skipping any model measured worse than the
           **current rung**.

        Step 1 is what makes step 3 well defined: by the time accuracies are
        compared, every candidate has one, so "measured worse" is never asked
        of a TBD.

        Step 3 compares against the current rung rather than the best accuracy
        seen. Those differ once a model has been skipped, and comparing against
        the best-seen would make the ladder depend on models it rejected.

        Strictly worse is skipped; equal is kept. Luna and sonnet both measure
        100% on coding, and a `<=` here would drop every tied rung -- which is
        what keeps sonnet at rung 2 (spec 3.1).
        """
        candidates = self.ladder_eligible(task_type)
        size = (_default_expected_tokens(task_type)
                if expected_tokens is None else expected_tokens)
        candidates.sort(
            key=lambda r: (r.cost_per_1m_tokens * size / 1_000_000, r.model))

        rungs: list[str] = []
        current: float | None = None
        for row in candidates:
            if current is not None and row.accuracy < current:
                continue
            rungs.append(row.model)
            current = row.accuracy
        return rungs

    # ── Latency (spec 5.1) ──────────────────────────────────────────────────

    def baseline_deadline_s(self, task_type: str) -> int:
        """5.1's per-type baseline, with the unknown type taking the longest."""
        return TIER0_DEADLINE.get(task_type, unknown_type_baseline_s())

    def latency_reference_s(self, task_type: str) -> float | None:
        """The 1.0 reference: the fastest **ladder-eligible** model (5.1).

        The reference set is ladder-eligible rows, not every row. A model that
        is cost-excluded or unmeasured can never be a rung, so letting it set
        the reference would shrink the deadline of every model that can be one.
        """
        measured = [r.median_latency_s for r in self.ladder_eligible(task_type)
                    if r.median_latency_s is not None]
        return min(measured) if measured else None

    def _gate_rung0_row(
        self, task_type: str
    ) -> tuple[CapabilityRow | None, str, str | None]:
        """Which row is the gate's real entry rung right now, and which of
        two paths named it (ruling F5, 2026-09-16).

        `_gate_latency_s` used to pick the cheapest priced, non-excluded
        `reviewer-gate` row unconditionally, deliberately not requiring a
        measured accuracy so the invariant would not pre-decide section 12's
        open gate-type question. That is correct only while `reviewer-gate`
        has no ladder-eligible row at all. Once it does,
        `ladder(GATE_TASK_TYPE)[0]` **is** the model that actually runs first
        -- the ladder is what "runs first" means for every other task type,
        and `reviewer-gate` is not a special case of that. Timing the
        invariant against a different, cheaper row at that point would be
        timing it against a model the pipeline does not run.

        So: try the ladder first. `ladder(GATE_TASK_TYPE)` is non-empty only
        once some `reviewer-gate` row has a measured accuracy and survives
        the cost ceiling -- exactly the condition under which its first rung
        stops being a guess. Fall back to the pre-existing cheapest-priced
        pick (`_gate_candidates`, no accuracy required) only when it is
        empty, which is today's real, shipped state: `reviewer-gate` has two
        rows and neither carries an accuracy, so it is not ladder-eligible at
        all and this fallback is live code, not a dead branch kept "just in
        case". This never asks whether gate types *must* be ladder-eligible
        -- section 12's open item -- it only uses the ladder's answer when
        the ladder has one.

        Returns `(row, "ladder", None)`, `(row, "fallback", None)`, or
        `(None, "", why)` when neither path can name a model at all -- `why`
        is `_gate_candidates`'s reason, reused rather than duplicated.
        """
        ladder_rungs = self.ladder(GATE_TASK_TYPE)
        if ladder_rungs:
            top = ladder_rungs[0]
            row = next(
                (r for r in self.rows_for(GATE_TASK_TYPE) if r.model == top),
                None,
            )
            if row is not None:
                return row, "ladder", None
        candidates, why = self._gate_candidates(task_type)
        if not candidates:
            return None, "", why
        return candidates[0], "fallback", None

    def gate_rung0(self, task_type: str) -> tuple[str | None, str | None]:
        """Which model is the gate's entry rung (5.1) right now, and which of
        F5's two paths named it: `"ladder"` when `reviewer-gate` is
        ladder-eligible and its real rung 0 was used, `"fallback"` when it
        fell back to the pre-existing cheapest-priced pick instead, or
        `(None, None)` when neither path can name a model at all.

        Not used by `_gate_latency_s` for its own arithmetic -- that calls
        `_gate_rung0_row` directly, because it also needs the row's
        `median_latency_s`, not just its model id -- but exists for a caller
        that only wants to *say* which claim a downstream figure rests on. A
        latency computed against the ladder's rung 0 is a different claim
        from one computed against the fallback, and 5.1's worst-case ceiling
        depends on knowing which one it got.
        """
        row, source, _ = self._gate_rung0_row(task_type)
        return (row.model if row is not None else None), (source or None)

    def _gate_latency_s(self, task_type: str) -> tuple[float | None, str | None]:
        """The `median_latency_s` the three model gates are timed against.

        Two choices live here, both of which 5.1 pins and neither of which is
        free to get wrong -- it says the gate-row question "moves the worst
        case by well over a hundred seconds, so it is not a detail".

        *Which model.* The gates run on the `reviewer-gate` task type (3, 4.3),
        and 4.3 names their entry rung as the floor of that type, with a per-
        gate `MAX_ATTEMPTS` of 1 (section 5) -- so in the worst case each gate
        runs once, at the floor. `_gate_rung0_row` (F5) picks that floor: the
        ladder's own rung 0 when `reviewer-gate` is ladder-eligible, falling
        back to the cheapest priced, non-excluded row (no accuracy required)
        only when it is not -- see that method for why. Requiring an accuracy
        unconditionally would make every operational task type depend on
        `reviewer-gate` being ladder-eligible, which is precisely the gate-type
        dependency section 12 records as an open question and forbids
        resolving in passing; using the ladder's answer *when it has one*
        does not do that, because it never asserts gate types must have one.

        *Which row.* 5.1: "A gate uses the `reviewer-gate` row when one exists
        for that model, falling back to the leaf's task-type row when it does
        not" -- the gate row is the direct measurement of the call being timed.

        Returns `(latency, None)` or `(None, reason)`. The reason, when there
        is one, names which of the two paths was tried so the two failure
        modes ("no gate model at all" vs. "the picked gate model has no
        latency") are never printed identically -- see `gate_rung0` for the
        same fact when the call instead succeeds.
        """
        gate, source, why = self._gate_rung0_row(task_type)
        if gate is None:
            return None, (
                f"{why}, so the model its three gates run on (spec 4.3) "
                f"cannot be named"
            )
        if gate.median_latency_s is not None:
            return gate.median_latency_s, None
        for row in self.rows_for(task_type):          # 5.1's fallback
            if row.model == gate.model and row.median_latency_s is not None:
                return row.median_latency_s, None
        return None, (
            f"gate model {gate.model} (the reviewer-gate {source} pick) has "
            f"no measured median_latency_s under {GATE_TASK_TYPE} or "
            f"{task_type}"
        )

    def _gate_candidates(self, task_type: str) -> tuple[list[CapabilityRow], str | None]:
        """Usable `reviewer-gate` rows, cheapest first (spec 4.3's floor
        pick and 4.3/4.5's climb target).

        Shared by `_gate_latency_s` (entry rung, `candidates[0]`) and
        `_gate_climb_latency_s` (the climb rung, `candidates[1]`) so the two
        can never pick "the gate model" two different ways. `task_type` is
        accepted for symmetry with the two callers and 5.1's fallback (which
        needs the leaf's own task type), but the candidate set itself is
        always drawn from `GATE_TASK_TYPE` -- the gate model is the same one
        for every leaf task type by construction (spec 3, 4.3).

        Returns `([], reason)` when no row is usable as a gate model at all;
        `(candidates, None)` otherwise. Two different repairs, so two
        different sentences in the reason -- telling an operator who is
        looking at two gate rows that the table holds none sends them to add
        rows they can already see; what they actually have to fix is the
        blank rate or the exclusion.
        """
        gate_rows = self.rows_for(GATE_TASK_TYPE)
        candidates = [r for r in gate_rows
                      if r.model not in EXCLUDED_MODELS
                      and r.cost_per_1m_tokens is not None]
        if not candidates:
            if not gate_rows:
                why = (f"the table holds no {GATE_TASK_TYPE} row at all")
            else:
                unusable = ", ".join(
                    f"{r.model} ("
                    + ("cost-excluded from every ladder"
                       if r.model in EXCLUDED_MODELS
                       else "no cost_per_1m_tokens")
                    + ")"
                    for r in sorted(gate_rows, key=lambda r: r.model)
                )
                why = (f"every {GATE_TASK_TYPE} row is unusable as a gate "
                       f"model: {unusable}")
            return [], why
        candidates.sort(key=lambda r: (r.cost_per_1m_tokens, r.model))
        return candidates, None

    def _gate_climb_latency_s(self, task_type: str) -> tuple[float | None, str | None]:
        """The gate's second call, at the rung above its entry (spec 4.3:
        "The reviewer itself only climbs (`luna -> sonnet`) if it keeps
        rejecting output from the generator's top rung"; 4.5 gives the
        security gate the same climb). The worst-case path assumes the
        climb happens, because it is a worst case.

        The climb rung is `_gate_candidates`'s second entry -- the same
        cheapest-first order `_gate_latency_s` picks its floor from, so the
        two can never disagree about which model is one rung above the
        floor.

        Unlike the entry rung, there is **no** fallback to the leaf task
        type's own row here. 5.1's fallback quote ("falling back to the
        leaf's task-type row when it [the reviewer-gate row] does not"
        exist) is stated for "a gate", i.e. for picking the entry rung's
        latency; nothing in 4.3/4.5 extends it to the climbed call, and
        assuming it would let an unmeasured `reviewer-gate` climb hide
        behind a measured leaf-type row for the same model -- exactly the
        kind of invented rule 5.1's "derived, not chosen" principle rules
        out. The climb rung's latency must be measured directly on
        `reviewer-gate`.

        Returns `(latency, None)` when a climb rung exists and is measured;
        `(None, None)` when the gate table has only one usable model, so
        there is nothing to climb to and the term is correctly zero rather
        than missing; `(None, reason)` when a climb rung exists but its
        `median_latency_s` is not measured -- the climb is possible, the
        worst case must assume it happens, and it cannot be priced.
        """
        candidates, _ = self._gate_candidates(task_type)
        if len(candidates) < 2:
            return None, None
        climb = candidates[1]
        if climb.median_latency_s is not None:
            return climb.median_latency_s, None
        return None, (
            f"gate climb model {climb.model} has no measured median_latency_s "
            f"under {GATE_TASK_TYPE}"
        )

    def gate_latency_s(self, task_type: str) -> tuple[float | None, str | None]:
        """Public accessor for `_gate_latency_s` (spec 5.1's per-gate
        deadline: `per_type_baseline x size_factor x model_speed_multiplier`
        computed with the gate model's own latency). Added for
        `delegation_pipeline.gate_effective_deadline`, which needs this
        table's gate-model-and-row selection but must not duplicate it --
        two copies of "which model, which row" is how the two would drift
        apart. No logic lives here; it only exposes the private method.
        """
        return self._gate_latency_s(task_type)

    def _worst_case(self, task_type: str) -> tuple[float | None, list[str]]:
        """5.1's worst-case path, and why it could not be computed.

        Amended 2026-09-16 (spec 5.1, "the formula above is incomplete, and
        1,243.125s is a lower bound"): the original formula summed three
        gate calls, each run once, at the gate's entry rung. Release 0.19.0
        implemented both that arithmetic and 4.3/4.5's gate machinery for
        the first time, and they disagreed -- 4.3/4.5 let a gate climb one
        rung (`_gate_climb_latency_s`) if it keeps rejecting the generator's
        top rung, and the original sum had no term for the second call that
        climb makes. 4.3/4.5 win, because 5.1's own decision is that the
        ceiling is *derived* from the worst-case path, not chosen, and a
        formula modelling less than the pipeline does is not a derivation
        of it:

            worst_case = baseline x size_factor x [ sum(m_rung) over MAX_ATTEMPTS
                                                  + MODEL_GATE_COUNT
                                                    x (m_gate_entry + m_gate_climb) ]

        Every generation attempt runs to its deadline, then every one of the
        three gates runs its entry call and, if the reviewer-gate table
        holds a second usable model to climb to, its climb call too. Rungs
        past `MAX_ATTEMPTS` are not summed because no leaf can reach them.

        `m_gate_climb` is 0, not missing, when there is only one usable
        `reviewer-gate` model: a gate cannot climb to a rung that does not
        exist, so the term is correctly absent rather than unknown. It is a
        **problem**, not 0 and not silently dropped, when a second model
        exists but its own `reviewer-gate` `median_latency_s` is unmeasured
        -- the climb is possible, the worst case has to assume it happens
        (this is a worst case), and a figure that skipped an assumed-to-
        happen call would be exactly the "models less than the pipeline"
        failure 5.1 now names. See `_gate_climb_latency_s` for why there is
        no fallback to the leaf task type's row here the way there is for
        the entry rung.

        **What this still does not cover: spec 4.5's security re-run
        cycles.** `delegation_pipeline.SECURITY_RERUN_CAP` (2) permits two
        additional generation ("fix") -> security-review cycles after the
        base security gate rejects, and each cycle's security-review call
        would reuse this same gate machinery -- but its *generation* call
        has no rung this module, or any other in this codebase, currently
        names. 4.5 rules out escalating that call ("a targeted fix, not a
        blind rewrite from a larger model"), which rules out the obvious
        guess, and assigns no model in its place; `delegation_pipeline`'s
        own docstring for `next_gate_rung` says as much -- "which concrete
        model sits at the gate's own top rung -- and how many rungs a gate
        ladder even has -- is exactly the `reviewer-gate` ladder-eligibility
        question section 12 defers as an open item; this function decides
        none of that, the same way `SECURITY_RERUN_CAP = 2` decides none of
        it either." Inventing a rung for it here would resolve that open
        item in passing, which section 12 explicitly forbids. So the term
        is left out rather than guessed at, and this paragraph is the
        comment naming what is missing: a spec rule for which model backs a
        security-rerun cycle's fix-generation call, and a table that can
        answer it once that rule exists.

        An empty ladder yields `(None, [])`: "no empty ladder" already reports
        that, and repeating it as a second problem would make one broken
        invariant look like two.
        """
        problems: list[str] = []
        prefix = f"{task_type}: worst-case path (spec 5.1) cannot be computed"
        rungs = self.ladder(task_type)[:MAX_ATTEMPTS]
        if not rungs:
            return None, []

        reference = self.latency_reference_s(task_type)
        if reference is None or reference <= 0:
            return None, [
                f"{prefix} -- no ladder-eligible row has a usable measured "
                f"median_latency_s to be the 1.0 reference"
            ]

        latency_of = {r.model: r.median_latency_s for r in self.rows_for(task_type)}
        multiplier_sum = 0.0
        for model in rungs:
            latency = latency_of.get(model)
            if latency is None:
                problems.append(
                    f"{prefix} -- rung {model} has no measured median_latency_s"
                )
            else:
                multiplier_sum += latency / reference

        entry_latency, entry_reason = self._gate_latency_s(task_type)
        if entry_reason is not None:
            problems.append(f"{prefix} -- {entry_reason}")

        climb_latency, climb_reason = self._gate_climb_latency_s(task_type)
        if climb_reason is not None:
            problems.append(f"{prefix} -- {climb_reason}")

        if entry_latency is not None:
            gate_multiplier = entry_latency / reference
            if climb_latency is not None:
                gate_multiplier += climb_latency / reference
            multiplier_sum += MODEL_GATE_COUNT * gate_multiplier

        if problems:
            return None, problems
        return (self.baseline_deadline_s(task_type) * BINDING_SIZE_FACTOR
                * multiplier_sum), []

    def worst_case_path_s(self, task_type: str) -> float | None:
        """5.1's worst-case path in seconds, or None when it cannot be
        computed. The reason is only produced by `validate()`, which is where
        an incomputable path becomes a refusal to start."""
        return self._worst_case(task_type)[0]

    # ── Tree cost (spec 2.7) ────────────────────────────────────────────────

    def _tree_cost(
        self, task_type: str
    ) -> tuple[float | None, list[str], str | None]:
        """2.7's expected tree cost for a task type's ladder, and why not.

            tree_cost = leaves_per_tree x tokens_per_leaf
                      x SUM over rungs of [ P(reach rung) x rate ]

        A ladder deeper than 2.7's published reach probabilities is **refused**
        rather than truncated. Truncating would price the fourth rung at zero,
        and an unpriced rung and a free one must not look the same to this
        check -- it is the one failure mode 2.7 cannot tolerate, because the
        whole point of the section is that a model's contribution depends on
        how often it is reached. The alternative reading, that rungs past
        `MAX_ATTEMPTS` are unreachable and so genuinely cost nothing, is
        defensible but silent; if a ladder ever does exceed three rungs, an
        operator should be told rather than have the extra rung disappear.

        The third element is the rung whose own cost contributes the most to
        the total, or None when the total could not be computed. Spec 11
        requires the budget-overrun problem string to name this rung -- a
        total on its own tells an operator the ladder is too expensive, but
        not which cell in 2.7's table to fix, and this is that cell.
        """
        problems: list[str] = []
        prefix = f"{task_type}: expected tree cost (spec 2.7) cannot be computed"
        rungs = self.ladder(task_type)
        if not rungs:
            return None, [], None

        rate_of = {r.model: r.cost_per_1m_tokens for r in self.rows_for(task_type)}
        total = 0.0
        costliest_rung: str | None = None
        costliest_cost = -1.0
        for rung, model in enumerate(rungs):
            rate = rate_of.get(model)
            if rate is None:                    # unpriced, not free (2.7)
                problems.append(
                    f"{prefix} -- rung {model} has no cost_per_1m_tokens"
                )
                continue
            try:
                rung_cost = rung_cost_usd(rung, rate)
            except ValueError as exc:
                problems.append(
                    f"{prefix} -- its ladder has {len(rungs)} rungs and {exc}"
                )
                break
            total += rung_cost
            if rung_cost > costliest_cost:
                costliest_cost = rung_cost
                costliest_rung = model
        if problems:
            return None, problems, None
        return total, [], costliest_rung

    def tree_cost_usd(self, task_type: str) -> float | None:
        """2.7's expected tree cost in dollars, or None when it cannot be
        computed."""
        return self._tree_cost(task_type)[0]

    # ── Validation (spec 1.1) ───────────────────────────────────────────────

    def _resolution_problems(self, task_type: str,
                             known: frozenset[str] | None) -> list[str]:
        """1.1: every rung resolves to a backend-and-model pair in the combo
        box (9.3).

        What the built-in list can and cannot adjudicate, stated plainly
        because the gap is the whole design of this check. `config.KNOWN_MODELS`
        is the *fallback* the models page shows when the active machine cannot
        be asked what it serves, and it holds Anthropic ids only -- the live
        combo box is whatever the machine reports. So:

        * a **bare** id names an Anthropic-backend model and is checked against
          the known set. This catches the defect that actually occurs: a typo or
          a retired id in a ladder rung;
        * a **backend-qualified** `backend/model` id (`azure_ai/gpt-5.6-luna`,
          `vllm/Qwen3.6-35B-A3B-NVFP4`) carries its backend with it and cannot
          be adjudicated against a list that does not enumerate gateway
          catalogues. It is accepted as resolvable, and only its shape is
          checked. 1.2 records all three `coding` rungs as resolving, which a
          membership-only check would contradict.

        A caller holding the live list closes that gap by passing it: when
        `known_models` is supplied, every rung must be a member of it.
        """
        problems: list[str] = []
        builtin: frozenset[str] | None = None
        for model in self.ladder(task_type):
            backend, sep, name = model.partition("/")
            if sep:
                if not backend or not name:
                    problems.append(
                        f"{task_type}: rung {model!r} is not a valid "
                        f"backend-and-model pair (spec 9.3)"
                    )
                elif known is not None and model not in known:
                    problems.append(
                        f"{task_type}: rung {model} is not in the model combo "
                        f"box (spec 9.3)"
                    )
                continue
            if known is None and builtin is None:
                builtin = default_known_models()
            resolved_against = known if known is not None else builtin
            if model not in resolved_against:
                problems.append(
                    f"{task_type}: rung {model!r} does not resolve to a model "
                    f"in the model combo box (spec 9.3)"
                )
        return problems

    def validate(self, known_models: Iterable[str] | None = None) -> list[str]:
        """All six of 1.1's invariants.

        Returns every problem rather than the first, because 1.1 requires the
        error to list all broken invariants so an operator can fix the data in
        one pass instead of discovering them one restart at a time.

        Scoped to operational task types. A non-operational type may hold TBD
        in any column -- without that, the system could never start for the
        first time, and editing a non-operational type's rows would be gated by
        the very check the edit exists to satisfy.

        `known_models` is the model combo box (9.3). Omitted, it falls back to
        `config.KNOWN_MODELS`, which covers Anthropic ids only -- see
        `_resolution_problems` for exactly what that can and cannot decide. A
        caller holding the live per-machine list should pass it.

        Nothing here makes anything operational, and nothing routes: this
        function only ever reports. What each invariant costs when it cannot be
        computed is uniform -- a problem string naming the task type and the
        missing data, never a crash and never a silent pass.
        """
        known = frozenset(known_models) if known_models is not None else None
        problems: list[str] = []
        for task_type in sorted(self._operational):
            eligible = self.ladder_eligible(task_type)

            # "no blank fields", scoped to ladder-eligible rows (2026-09-15).
            for row in eligible:
                missing = row.missing_columns()
                if missing:
                    problems.append(
                        f"{task_type}: {row.model} is ladder-eligible but has "
                        f"no value for {', '.join(missing)}"
                    )

            # "no empty ladder"
            if not self.ladder(task_type):
                problems.append(
                    f"{task_type}: is operational but its ladder is empty -- "
                    "no row has a measured accuracy that survives the cost "
                    "ceiling"
                )

            # "every rung is backed by a row" -- trivially true when the ladder
            # is generated rather than written, and asserted so that it stays
            # that way if a hand-written snapshot is ever reintroduced.
            backed = {r.model for r in self.rows_for(task_type)}
            for model in self.ladder(task_type):
                if model not in backed:
                    problems.append(
                        f"{task_type}: rung {model} has no row in the table"
                    )

            # "model resolution" (1.1, 9.3)
            problems.extend(self._resolution_problems(task_type, known))

            # "the ceiling fits the budget" (1.1, 5.1)
            worst_case, why_not = self._worst_case(task_type)
            problems.extend(why_not)
            if worst_case is not None and worst_case > LATENCY_CEILING_S:
                problems.append(
                    f"{task_type}: its worst-case path is {worst_case:.0f}s, "
                    f"above the {LATENCY_CEILING_S}s combined latency ceiling "
                    f"(spec 5.1)"
                )

            # "the ladder fits the budget" (1.1, 2.7)
            tree_cost, why_not, costliest_rung = self._tree_cost(task_type)
            problems.extend(why_not)
            if tree_cost is not None and tree_cost > BUDGET_USD:
                problems.append(
                    f"{task_type}: its ladder's expected tree cost is "
                    f"${tree_cost:.3f}, above BUDGET_USD (${BUDGET_USD:.2f}) "
                    f"-- rung {costliest_rung} is the costliest (spec 2.7)"
                )
        return problems
