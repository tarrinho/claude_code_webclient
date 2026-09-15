"""Capability table and escalation-ladder generation.

Stage 1 of docs/superpowers/specs/2026-09-14-tiered-agent-delegation-spec-v3.md
-- sections 1.1, 2.6, 2.7 and 3.

Scope, stated up front because a passing suite here means less than it looks
like it means. This module decides *which model would be tried in what order*
for a task type. It does not route anything. `ModelRouter.assign_model` is
untouched, every task type ships non-operational, and a non-operational type
falls back to today's routing. Section 12 of the spec carries three open items,
each with an explicit "do not", and the third forbids flipping `coding` to
operational until gate-type validation is decided. This is the machinery those
decisions get applied to.

Deliberately pure: no database, no config read, no import from `app`. The table
is passed in. That keeps the generator testable against a constructed table
rather than against production data, which matters because section 2.6 ships
mostly unmeasured -- a module that read the live table would encode today's
measurement state into its own tests.
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


def expected_tokens(task_type: str) -> int:
    """The task type's expected call size, for the cost ceiling."""
    return EXPECTED_TOKENS.get(task_type, DEFAULT_EXPECTED_TOKENS)


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

    # ── Validation (spec 1.1) ───────────────────────────────────────────────

    def validate(self) -> list[str]:
        """The subset of 1.1's six invariants this stage can answer.

        Returns every problem rather than the first, because 1.1 requires the
        error to list all broken invariants so an operator can fix the data in
        one pass instead of discovering them one restart at a time.

        Scoped to operational task types. A non-operational type may hold TBD
        in any column -- without that, the system could never start for the
        first time, and editing a non-operational type's rows would be gated by
        the very check the edit exists to satisfy.

        Three of the six invariants are NOT checked here and are named rather
        than silently skipped: model resolution needs the backend combo box,
        and the two budget invariants need the latency ceiling and tree model
        from sections 5.1 and 2.7. They belong to stage 2. This function must
        not be read as "the spec's validation", only as the part of it that a
        pure table can answer.
        """
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
        return problems
