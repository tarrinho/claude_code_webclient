# db_delegation.py -- the spec 2.6 benchmark table and the operational flags.
#
# Section 2.6 says this is a database table rather than a constant in source,
# and names the three things that touch it: the settings page (9.2) writes it,
# the re-benchmark job (10.2) writes it, and the ladder generator (3) reads it.
# The markdown table in the spec is a snapshot of this table's contents, not a
# second source of truth.
from __future__ import annotations

from typing import Any

import db
from tiered_delegation import CapabilityRow

_COLUMNS = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context")


async def delegation_rows_all() -> list[dict[str, Any]]:
    """Every (model, task_type) row. NULL stays None."""
    cur = await db.db_conn.execute(
        "SELECT model, task_type, accuracy, n, cost_per_1m_tokens, "
        "median_latency_s, max_context FROM delegation_capability "
        "ORDER BY task_type, model"
    )
    return [dict(row) for row in await cur.fetchall()]


async def delegation_row_set(model: str, task_type: str, **columns: Any) -> bool:
    """Insert or update one row. Unknown column names are refused rather than
    silently dropped -- a typo in a column name would otherwise read as a
    successful write of nothing."""
    unknown = set(columns) - set(_COLUMNS)
    if unknown:
        raise ValueError(f"unknown capability columns: {sorted(unknown)}")
    values = {c: columns.get(c) for c in _COLUMNS}
    await db.db_conn.execute(
        "INSERT INTO delegation_capability "
        "(model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, "
        " max_context, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(model, task_type) DO UPDATE SET "
        "  accuracy = excluded.accuracy, n = excluded.n, "
        "  cost_per_1m_tokens = excluded.cost_per_1m_tokens, "
        "  median_latency_s = excluded.median_latency_s, "
        "  max_context = excluded.max_context, updated_at = excluded.updated_at",
        (model, task_type, values["accuracy"], values["n"],
         values["cost_per_1m_tokens"], values["median_latency_s"],
         values["max_context"], db._now()),
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
