"""Shadow-mode recorder: what the delegation subsystem *would* have chosen.

Design: docs/superpowers/specs/2026-09-18-routing-decision-record-design.md

For every task the orchestrator creates, this module classifies the task,
resolves the model the delegation subsystem would have picked, and stores that
beside the model the task was actually given. It changes no routing outcome --
the task still runs on whatever the plan named. What it produces is a labelled
disagreement set on real work: classifier verdict versus the operator's own
choice, gathered before any decision to switch routing on.

Why a recorder at all, rather than routing: as of 2026-09-18 the delegation
ladder is unreachable end-to-end. `ModelRouter.assign_model` has no production
caller, `app.state.capability_table` is written at startup and read nowhere,
and an ordinary turn's model comes from `runner.get_default_model`. A recorder
hooked into `assign_model` would have recorded zero rows for as long as it
existed, and that silence would have read as "no tasks were routed" rather than
"the hook is dead" -- so the hook is at `OrchestratorEngine._materialise_plan`,
where a task's model is actually decided.

The three-branch precedence below duplicates `ModelRouter.assign_model`, and
the duplication is deliberate: `assign_model` returns only the model, and a
record needs to know *which branch produced it*. Changing that method's return
type would break three test files asserting a signature that release 0.19.0
published. `tests/test_qa_delegation_recorder.py` runs a shared table of inputs
through both and asserts they agree on the chosen model; that test is the
safety net this duplication is allowed on.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import config

_log = logging.getLogger("wc.app")

#: Which branch produced `shadow_model`.
SOURCE_RULE: str = "rule"
SOURCE_LADDER: str = "ladder"
SOURCE_FALLBACK: str = "fallback"

#: The only task table this records for today. A column rather than a
#: constant in the schema, because the table has been renamed once already.
ORCHESTRATOR_TASK_TABLE: str = "orchestrator_tasks"


def resolve_shadow(
    combined: str,
    *,
    rules: list[dict[str, str]] | None,
    table: Any | None,
) -> tuple[str, str, list[str] | None]:
    """Return (source, shadow_model, ladder) for already-lowercased *combined*.

    Same precedence as `ModelRouter.assign_model`: an operator's explicit rule,
    then the measured ladder for an operational task type, then the fallback.
    Split out from `record_decision` so the agreement test can drive it with no
    database at all.

    `ladder` is the full list of rungs when one was consulted, and None
    otherwise -- including for a rule match, where no ladder was looked at.
    """
    for rule in rules or []:
        pattern = rule.get("pattern", "")
        model = rule.get("model", "")
        if pattern and model:
            try:
                if re.search(pattern, combined):
                    return SOURCE_RULE, model, None
            except re.error:
                # Matches assign_model: an operator's bad regex is skipped,
                # not raised on. Logged there too, so this stays silent rather
                # than logging the same broken pattern twice per task.
                continue

    if table is not None:
        from delegation_classifier import classify

        decision = classify(combined)
        if table.is_operational(decision.task_type):
            rungs = list(table.ladder(decision.task_type))
            if rungs:
                return SOURCE_LADDER, rungs[0], rungs
            # An operational type with an empty ladder is refused at startup
            # (spec 1.1), so reaching here means a table built some other way.
            # assign_model falls back rather than raising; so does this.
            return SOURCE_FALLBACK, config.ANTHROPIC_MODEL, rungs

    return SOURCE_FALLBACK, config.ANTHROPIC_MODEL, None


async def record_decision(
    *,
    task_table: str,
    task_id: str,
    title: str,
    description: str,
    actual_model: str | None,
    router: Any | None = None,
) -> int | None:
    """Record one shadow decision. Returns the new row id, or None if nothing
    was written.

    Raises rather than swallowing: the caller owns failure isolation, because
    only the caller knows which task it was recording for, and a warning that
    does not name the task is what hid a silent failure in this same loop once
    already (see `_materialise_plan`).
    """
    import db
    from delegation_classifier import classify
    from delegation_startup import load_capability_table

    combined = (title + " " + description).lower()
    table = await load_capability_table()
    decision = classify(combined)
    source, shadow_model, ladder = resolve_shadow(
        combined, rules=getattr(router, "rules", None), table=table,
    )
    return await db.delegation_decision_record(
        task_table=task_table,
        task_id=task_id,
        task_type=decision.task_type,
        score=decision.score,
        mutates=decision.mutates,
        source=source,
        shadow_model=shadow_model,
        actual_model=actual_model,
        ladder=None if ladder is None else json.dumps(ladder),
    )
