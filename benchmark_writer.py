# benchmark_writer.py -- the ONE path a measurement takes into the
# capability table.
#
# Spec 11. Nothing reviews a measurement before it lands (spec 6), so the voice
# exclusion below is not a convenience -- it is the only safeguard on
# median_latency_s for that task type. Every caller (the nightly sweep, the CLI
# --cell form, and the page endpoint) goes through write_cell. There is
# deliberately no second write path.
from __future__ import annotations

import logging

from routes.db_benchmark import capability_meta_set
from routes.db_delegation import delegation_row_set, delegation_rows_all

_log = logging.getLogger(__name__)

#: The harness measures over the CLI transport. routes/voice.py is CLAUDE.md
#: section 0's documented exception and speaks to an OpenAI-compatible endpoint
#: directly, so a CLI-measured voice latency describes a path no voice turn
#: takes: 6.6-9.6s against the ~2.0s the voice path records. Writing it would
#: corrupt the column spec v3 5.1's deadline derivation divides by.
LATENCY_EXCLUDED_TASK_TYPES = frozenset({"voice"})


async def write_cell(model: str, task_type: str, accuracy: float | None,
                     n: int | None, median_latency_s: float | None, *,
                     measured_at: str, trigger: str = "scheduled",
                     under_load: bool = False) -> None:
    """Write one successful measurement, then stamp its provenance.

    `median_latency_s` is dropped for the task types in
    LATENCY_EXCLUDED_TASK_TYPES; the existing value is preserved rather than
    nulled, because an old number measured over the right transport beats a
    fresh one measured over the wrong one.
    """
    rows = {(r["model"], r["task_type"]): r for r in await delegation_rows_all()}
    existing = rows.get((model, task_type), {})

    latency = median_latency_s
    if task_type in LATENCY_EXCLUDED_TASK_TYPES:
        latency = existing.get("median_latency_s")
        _log.info(
            "benchmark: keeping existing median_latency_s for task_type=%s "
            "model=%s -- the harness measures a transport this task type does "
            "not use", task_type, model)

    await delegation_row_set(
        model, task_type,
        accuracy=accuracy,
        n=n,
        median_latency_s=latency,
        cost_per_1m_tokens=existing.get("cost_per_1m_tokens"),
        max_context=existing.get("max_context"),
    )
    await capability_meta_set(
        model, task_type,
        measured_at=measured_at,
        trigger=trigger,
        measured_under_load=1 if under_load else 0,
        consecutive_failures=0,
        dormant=0,
    )
