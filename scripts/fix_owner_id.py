"""Migrate all rows with owner_id='admin' to the real admin user UUID."""
import asyncio
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "webconsole.db"

async def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchall()
    con.close()

    if not rows:
        print("No admin user found")
        sys.exit(1)

    admin_id = rows[0][0]
    print(f"Migrating to admin_id={admin_id[:16]}...")

    # Tables to migrate
    tables = [
        "ai_machines", "chats", "orchestrators", "usage_events", "ssh_transports"
    ]

    con2 = sqlite3.connect(DB_PATH)
    for table in tables:
        # Check if table has owner_id column
        info = con2.execute(f"PRAGMA table_info({table})").fetchall()
        has_owner = any(col[1] == 'owner_id' for col in info)
        if not has_owner:
            print(f"  {table}: no owner_id column, skipping")
            continue

        # Count before
        count = con2.execute(
            f"SELECT count(*) FROM {table} WHERE owner_id = ?", ("admin",)
        ).fetchone()[0]

        if count == 0:
            print(f"  {table}: 0 rows with owner_id='admin', skipping")
            continue

        # Migrate
        con2.execute(
            f"UPDATE {table} SET owner_id = ? WHERE owner_id = 'admin'",
            (admin_id,)
        )
        con2.commit()
        print(f"  {table}: {con2.total_changes} rows migrated")

    con2.close()
    print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
