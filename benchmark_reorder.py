# benchmark_reorder.py -- spot a ladder whose rungs changed places.
#
# Spec 13. The highlight never gates: the measurement is already written by the
# time this runs. It reports what happened.
from __future__ import annotations

import db
from routes.db_benchmark import capability_meta_set
from routes.db_delegation import rows_to_capability
from tiered_delegation import CapabilityTable


def ladders_differ_in_order(before: list[str], after: list[str]) -> bool:
    """True when two models that appear in BOTH ladders changed places.

    Appending or removing a rung is not a reordering: the models that were
    already there kept their relative order, and the operator has no
    escalation-order decision to revisit. Comparing the lists directly would
    flag every ladder that merely grew.
    """
    shared = [m for m in before if m in set(after)]
    shared_after = [m for m in after if m in set(before)]
    return shared != shared_after


def _ladder_for(rows: list[dict], task_type: str) -> list[str]:
    table = CapabilityTable(rows_to_capability(rows), operational=())
    try:
        return table.ladder(task_type)
    except Exception:                                  # noqa: BLE001
        # An incomputable ladder is not a reordering. A table mid-sweep can be
        # missing a rung's measurement entirely, and treating that as a flip
        # would highlight every cell on the way to a complete sweep.
        return []


async def mark_reordered(model: str, task_type: str) -> None:
    """Flag the row and clear any acknowledgement.

    Acknowledging a reordering acknowledges THAT reordering. A later one that
    still reorders must highlight again, or a cell acknowledged once would go
    quiet forever.
    """
    await capability_meta_set(
        model, task_type, reorder_flagged=1, reorder_seen_at=db._now(),
        reorder_acked_at=None)


async def acknowledge(model: str, task_type: str) -> None:
    await capability_meta_set(
        model, task_type, reorder_flagged=0, reorder_acked_at=db._now())


async def flag_reorderings(before_rows: list[dict], after_rows: list[dict],
                           task_types: list[str]) -> list[str]:
    """Compare ladders before and after a sweep's writes; flag what moved.

    Returns the task types that reordered. Every model in a reordered ladder is
    marked, because the reordering is a property of the ladder rather than of
    one row, and an operator looking at the page needs to see it beside the
    numbers that caused it.
    """
    reordered: list[str] = []
    for task_type in task_types:
        before = _ladder_for(before_rows, task_type)
        after = _ladder_for(after_rows, task_type)
        if not before or not after:
            continue
        if ladders_differ_in_order(before, after):
            reordered.append(task_type)
            for model in set(before) | set(after):
                await mark_reordered(model, task_type)
    return reordered
