"""Fix all existing rows whose owner_id is the literal 'admin' string to point
to the actual admin user UUID.

The DB was created with owner_id='admin' as the schema default, so every row
inserted before UUID scoping was properly enforced got the literal string.
Real requests now pass session["user"] (a UUID), but the old data never moved.
"""
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("migrate_owner_id")

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "webconsole.db"


async def main() -> None:
    # 1. Find the real admin user ID
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
    ).fetchall()
    con.close()

    if not rows:
        log.error("No admin user found — aborting to avoid orphaning data")
        sys.exit(1)

    admin_id = rows[0][0]
    log.info("Admin user UUID: %s", admin_id)

    # 2. Migrate every table that can have stale owner_id='admin' rows
    tables = [
        ("ai_machines", "id"),
        ("chats", "id"),
        ("orchestrators", "id"),
        ("usage_events", "id"),
        ("ssh_transports", "id"),
    ]

    for table, id_col in tables:
        # Count before
        con2 = sqlite3.connect(DB_PATH)
        count = con2.execute(
            f"SELECT count(*) as c FROM {table} WHERE owner_id = ?",
            ("admin",),
        ).fetchone()[0]
        con2.close()

        if count == 0:
            log.info("%s: 0 rows to migrate", table)
            continue

        log.info("Updating %d rows in %s …", count, table)
        con2 = sqlite3.connect(DB_PATH)
        con2.execute(
            f"UPDATE {table} SET owner_id = ? WHERE owner_id = 'admin'",
            (admin_id,),
        )
        con2.commit()
        changed = con2.total_changes
        con2.close()

        if changed:
            log.info("  → %d rows migrated to owner_id=%s", changed, admin_id)
        else:
            log.warning("  → no rows changed (possible race)")

    log.info("Done. All rows now use UUID %s", admin_id)


if __name__ == "__main__":
    asyncio.run(main())
