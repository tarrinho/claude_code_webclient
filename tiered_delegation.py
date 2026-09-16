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

    def _gate_latency_s(self, task_type: str) -> tuple[float | None, str | None]:
        """The `median_latency_s` the three model gates are timed against.

        Two choices live here, both of which 5.1 pins and neither of which is
        free to get wrong -- it says the gate-row question "moves the worst
        case by well over a hundred seconds, so it is not a detail".

        *Which model.* The gates run on the `reviewer-gate` task type (3, 4.3),
        and 4.3 names their entry rung as the floor of that type, with a per-
        gate `MAX_ATTEMPTS` of 1 (section 5) -- so in the worst case each gate
        runs once, at the floor. The floor is taken here as the cheapest priced
        `reviewer-gate` row that is not cost-excluded. Note what it deliberately
        does **not** require: a measured accuracy. Requiring one would make
        every operational task type depend on `reviewer-gate` being
        ladder-eligible, which is precisely the gate-type dependency section 12
        records as an open question and forbids resolving in passing. This
        invariant asks only whether the leaf's own worst case can be computed
        and fits.

        *Which row.* 5.1: "A gate uses the `reviewer-gate` row when one exists
        for that model, falling back to the leaf's task-type row when it does
        not" -- the gate row is the direct measurement of the call being timed.

        Returns `(latency, None)` or `(None, reason)`.
        """
        gate_rows = self.rows_for(GATE_TASK_TYPE)
        candidates = [r for r in gate_rows
                      if r.model not in EXCLUDED_MODELS
                      and r.cost_per_1m_tokens is not None]
        if not candidates:
            # Two different repairs, so two different sentences. Telling an
            # operator who is looking at two gate rows that the table holds
            # none sends them to add rows they can already see; what they
            # actually have to fix is the blank rate or the exclusion.
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
            return None, (
                f"{why}, so the model its three gates run on (spec 4.3) "
                f"cannot be named"
            )
        candidates.sort(key=lambda r: (r.cost_per_1m_tokens, r.model))
        gate = candidates[0]
        if gate.median_latency_s is not None:
            return gate.median_latency_s, None
        for row in self.rows_for(task_type):          # 5.1's fallback
            if row.model == gate.model and row.median_latency_s is not None:
                return row.median_latency_s, None
        return None, (
            f"gate model {gate.model} has no measured median_latency_s under "
            f"{GATE_TASK_TYPE} or {task_type}"
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

            worst_case = baseline x size_factor x [ sum(m_rung) over MAX_ATTEMPTS
                                                  + sum(m_gate) over the 3 gates ]

        Every generation attempt runs to its deadline, then every gate runs to
        its own. Rungs past `MAX_ATTEMPTS` are not summed because no leaf can
        reach them.

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

        gate_latency, reason = self._gate_latency_s(task_type)
        if gate_latency is None:
            problems.append(f"{prefix} -- {reason}")
        else:
            multiplier_sum += MODEL_GATE_COUNT * gate_latency / reference

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

    def _tree_cost(self, task_type: str) -> tuple[float | None, list[str]]:
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
        """
        problems: list[str] = []
        prefix = f"{task_type}: expected tree cost (spec 2.7) cannot be computed"
        rungs = self.ladder(task_type)
        if not rungs:
            return None, []

        rate_of = {r.model: r.cost_per_1m_tokens for r in self.rows_for(task_type)}
        total = 0.0
        for rung, model in enumerate(rungs):
            rate = rate_of.get(model)
            if rate is None:                    # unpriced, not free (2.7)
                problems.append(
                    f"{prefix} -- rung {model} has no cost_per_1m_tokens"
                )
                continue
            try:
                total += rung_cost_usd(rung, rate)
            except ValueError as exc:
                problems.append(
                    f"{prefix} -- its ladder has {len(rungs)} rungs and {exc}"
                )
                break
        if problems:
            return None, problems
        return total, []

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
            tree_cost, why_not = self._tree_cost(task_type)
            problems.extend(why_not)
            if tree_cost is not None and tree_cost > BUDGET_USD:
                problems.append(
                    f"{task_type}: its ladder's expected tree cost is "
                    f"${tree_cost:.3f}, above BUDGET_USD (${BUDGET_USD:.2f}) "
                    f"(spec 2.7)"
                )
        return problems
