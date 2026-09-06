"""Seed an isolated WebConsole instance for a manual layout check.

Creates one user, one orchestrator and four tasks in whatever database
WC_DB_PATH points at, so the orchestrator page has something to render and a
task with a long result to click. Intended for a throwaway instance on
loopback; it is not part of the test suite and touches nothing in the real
deployment.

Usage:
    WC_DB_PATH=/path/to/throwaway.db python bin/livecheck_seed.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import db

USER = "livecheck"
PASSWORD = "livecheck-throwaway-pw"

LONG_RESULT = (
    "Traced the failure through the scheduler and confirmed the ordering is "
    "deterministic.\n\n"
    "The row counts match the source table exactly, both new indexes were "
    "created without locking, and the foreign key constraint validated on the "
    "first pass.\n\n"
    + "\n".join(f"  step {i:02d}: verified" for i in range(1, 41))
    + "\n\nThis text exists to prove the detail panel shows the whole result "
      "rather than the 3000-character slice the chat carries."
)


async def main() -> None:
    await db.init()

    # owner_id is the user NAME, not the users.id. app.py builds the session
    # with auth.session_new(user["name"], ...) and every orchestrator endpoint
    # scopes on session["user"], so seeding the uuid here makes the rows
    # invisible to the API while looking perfectly correct in the database.
    cur = await db.db_conn.execute("SELECT name FROM users WHERE name = ?", (USER,))
    if await cur.fetchone():
        print(f"reusing existing user {USER!r}")
    else:
        await db.user_create(USER, None, auth.hash_password(PASSWORD))
        print(f"created user {USER!r} / {PASSWORD!r}")
    owner_id = USER

    sup_id = "livecheck00000000000000000000000"
    await db.db_conn.execute("DELETE FROM supervisor_tasks WHERE supervisor_id = ?", (sup_id,))
    await db.db_conn.execute("DELETE FROM supervisor_messages WHERE supervisor_id = ?", (sup_id,))
    await db.db_conn.execute("DELETE FROM supervisors WHERE id = ?", (sup_id,))
    await db.db_conn.commit()

    await db.orchestrator_create(
        sup_id, "Layout live check", "Seeded for the manual panel check", owner_id
    )
    print(f"created orchestrator id={sup_id}")

    tasks = [
        ("t001", "Reattach the detail panel", "done", 100.0, LONG_RESULT),
        ("t002", "Event Log spans the width", "running", 45.0, ""),
        ("t003", "Clear the log on switch", "pending", 0.0, ""),
        ("t004", "Wire the bottom handle", "failed", 0.0, "could not reach the proxy"),
    ]
    for task_id, title, status, pct, result in tasks:
        await db.orchestrator_task_create(
            sup_id, task_id, title, f"description for {title}", model="claude-opus-5"
        )
        await db.orchestrator_task_update(
            supervisor_id=sup_id, task_id=task_id, owner_id=owner_id,
            status=status, result=result or None, progress_pct=pct,
        )
    print(f"created {len(tasks)} tasks")

    await db.orchestrator_messages_append(
        sup_id, "user", "Check the orchestrator page layout.", {"kind": "goal"}
    )
    await db.orchestrator_messages_append(
        sup_id, "orchestrator",
        "Task 'Reattach the detail panel' completed "
        f"({len(LONG_RESULT)} chars)\n\n{LONG_RESULT[:3000]}",
        {"kind": "task_result", "task_id": "t001"},
    )

    # A second orchestrator, so the log-clearing behaviour has somewhere to
    # switch to.
    other = "livecheck11111111111111111111111"
    await db.db_conn.execute("DELETE FROM supervisors WHERE id = ?", (other,))
    await db.db_conn.commit()
    await db.orchestrator_create(other, "Second orchestrator", None, owner_id)
    print(f"created orchestrator id={other}")

    # Without this the aiosqlite worker thread keeps the loop alive and the
    # script never exits, even though every write above has committed.
    await db.db_conn.close()
    print("seed complete")


if __name__ == "__main__":
    asyncio.run(main())
