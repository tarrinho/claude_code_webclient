# db_delegation.py -- the spec 2.6 benchmark table and the operational flags.
#
# Section 2.6 says this is a database table rather than a constant in source,
# and names the three things that touch it: the settings page (9.2) writes it,
# the re-benchmark job (10.2) writes it, and the ladder generator (3) reads it.
# The markdown table in the spec is a snapshot of this table's contents, not a
# second source of truth.
from __future__ import annotations

import json
from typing import Any

import db
from tiered_delegation import CapabilityRow

_COLUMNS = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context")

#: Provenance for `cost_per_1m_tokens`, kept OUT of `_COLUMNS` on purpose.
#: `_COLUMNS` is spec 2.6's five measured columns -- the set the editor
#: exposes, the set `_EDITABLE` mirrors, and the set this table's full-row
#: UPSERT nulls when omitted. `cost_basis` is a note about one of them, not a
#: sixth measurement, and folding it in would put it in front of an operator
#: as another number to measure.
_COST_BASIS = "cost_basis"


async def delegation_rows_all() -> list[dict[str, Any]]:
    """Every (model, task_type) row. NULL stays None."""
    cur = await db.db_conn.execute(
        "SELECT model, task_type, accuracy, n, cost_per_1m_tokens, "
        "median_latency_s, max_context, cost_basis FROM delegation_capability "
        "ORDER BY task_type, model"
    )
    return [dict(row) for row in await cur.fetchall()]


async def delegation_row_set(model: str, task_type: str, **columns: Any) -> bool:
    """Insert or update one row. Unknown column names are refused rather than
    silently dropped -- a typo in a column name would otherwise read as a
    successful write of nothing."""
    unknown = set(columns) - set(_COLUMNS) - {_COST_BASIS}
    if unknown:
        raise ValueError(f"unknown capability columns: {sorted(unknown)}")
    values = {c: columns.get(c) for c in _COLUMNS}
    # Written like the measured columns -- supplied or nulled -- because this
    # is a full-row UPSERT and a basis that survived a write it was not part
    # of would describe a number that is no longer there. The caller decides
    # what it should say; it is never carried over silently.
    basis = columns.get(_COST_BASIS)
    await db.db_conn.execute(
        "INSERT INTO delegation_capability "
        "(model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, "
        " max_context, cost_basis, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(model, task_type) DO UPDATE SET "
        "  accuracy = excluded.accuracy, n = excluded.n, "
        "  cost_per_1m_tokens = excluded.cost_per_1m_tokens, "
        "  median_latency_s = excluded.median_latency_s, "
        "  max_context = excluded.max_context, "
        "  cost_basis = excluded.cost_basis, updated_at = excluded.updated_at",
        (model, task_type, values["accuracy"], values["n"],
         values["cost_per_1m_tokens"], values["median_latency_s"],
         values["max_context"], basis, db._now()),
    )
    await db.db_conn.commit()
    return True


async def delegation_operational_all() -> set[str]:
    """Task types flagged routable. Absent means non-operational (1.1)."""
    cur = await db.db_conn.execute("SELECT task_type FROM delegation_operational")
    return {row["task_type"] for row in await cur.fetchall()}


async def delegation_operational_set(task_type: str, operational: bool) -> bool:
    if operational:
        await db.db_conn.execute(
            "INSERT INTO delegation_operational (task_type, updated_at) "
            "VALUES (?, ?) ON CONFLICT(task_type) DO UPDATE SET "
            "updated_at = excluded.updated_at",
            (task_type, db._now()),
        )
    else:
        await db.db_conn.execute(
            "DELETE FROM delegation_operational WHERE task_type = ?", (task_type,))
    await db.db_conn.commit()
    return True


_DECISION_COLUMNS = (
    "task_table", "task_id", "task_type", "score", "mutates", "source",
    "shadow_model", "actual_model", "ladder",
)


async def delegation_decision_record(**columns: Any) -> int:
    """Insert one shadow-mode routing decision; return its new id.

    Keyword-only, and unknown names are refused rather than dropped, for the
    same reason `delegation_row_set` refuses them: a typo in a column name
    would otherwise read as a successful write of nothing.

    `actual_model` and `ladder` are the only nullable columns -- a plan that
    named no model, and a task type that is not operational, respectively.
    """
    unknown = set(columns) - set(_DECISION_COLUMNS)
    if unknown:
        raise ValueError(f"unknown routing decision columns: {sorted(unknown)}")
    missing = [
        c for c in _DECISION_COLUMNS
        if c not in ("actual_model", "ladder") and columns.get(c) is None
    ]
    if missing:
        raise ValueError(f"missing routing decision columns: {missing}")
    cur = await db.db_conn.execute(
        "INSERT INTO delegation_routing_decision "
        "(task_table, task_id, task_type, score, mutates, source, "
        " shadow_model, actual_model, ladder, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(columns.get(c) for c in _DECISION_COLUMNS) + (db._now(),),
    )
    await db.db_conn.commit()
    return int(cur.lastrowid)


async def delegation_decision_note_ran_model(
    task_table: str, task_id: str, ran_model: str,
) -> int:
    """Record which model a recorded task actually executed on. Returns the
    number of decision rows updated.

    Separate from `delegation_decision_record` because the two facts are known
    at different times: the decision at plan materialisation, the model at
    execution. Folding them into one write would mean either delaying the
    record until the task finishes -- losing every task that never runs -- or
    holding state between the two, which a crash discards.

    Updates every row for the pair rather than one. A task can be re-planned
    and the schema deliberately allows a second decision for the same task id
    (there is no unique constraint), so "the" row is not a thing this can
    assume. Rows already carrying a `ran_model` are left alone: the first
    execution is the one the ladder was asked about.
    """
    cur = await db.db_conn.execute(
        "UPDATE delegation_routing_decision SET ran_model = ? "
        "WHERE task_table = ? AND task_id = ? AND ran_model IS NULL",
        (ran_model, task_table, task_id),
    )
    await db.db_conn.commit()
    return cur.rowcount


async def delegation_decisions_recent(
    task_type: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    """Recorded decisions, newest first, optionally for one task type.

    Ordered by `id` descending rather than `decided_at`: `db._now()` has
    second resolution and a plan creates its tasks inside one second, so
    ordering on the timestamp alone leaves the order within a plan undefined.
    """
    sql = (
        "SELECT id, task_table, task_id, task_type, score, mutates, source, "
        "shadow_model, actual_model, ladder, decided_at "
        "FROM delegation_routing_decision"
    )
    params: tuple[Any, ...] = ()
    if task_type:
        sql += " WHERE task_type = ?"
        params = (task_type,)
    sql += " ORDER BY id DESC LIMIT ?"
    cur = await db.db_conn.execute(sql, params + (int(limit),))
    return [dict(row) for row in await cur.fetchall()]


async def delegation_pin_all() -> dict[str, list[str]]:
    """Every pinned ladder, keyed by task_type. Absent means no pin."""
    cur = await db.db_conn.execute(
        "SELECT task_type, rungs FROM delegation_ladder_pin")
    result: dict[str, list[str]] = {}
    for row in await cur.fetchall():
        rungs = json.loads(row["rungs"]) if row["rungs"] else []
        result[row["task_type"]] = rungs
    return result


async def delegation_pin_set(
    task_type: str, rungs: list[str] | None,
) -> bool:
    """Set or clear one ladder pin. None clears."""
    if rungs is None:
        await db.db_conn.execute(
            "DELETE FROM delegation_ladder_pin WHERE task_type = ?",
            (task_type,))
    else:
        await db.db_conn.execute(
            "INSERT INTO delegation_ladder_pin "
            "(task_type, rungs, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(task_type) DO UPDATE SET "
            "  rungs = excluded.rungs, updated_at = excluded.updated_at",
            (task_type, json.dumps(rungs), db._now()),
        )
    await db.db_conn.commit()
    return True


def rows_to_capability(rows: list[dict[str, Any]]) -> list[CapabilityRow]:
    """Database rows -> the dataclass the ladder generator already takes."""
    return [
        CapabilityRow(
            model=row["model"], task_type=row["task_type"],
            accuracy=row["accuracy"], n=row["n"],
            cost_per_1m_tokens=row["cost_per_1m_tokens"],
            median_latency_s=row["median_latency_s"],
            max_context=row["max_context"],
        )
        for row in rows
    ]
