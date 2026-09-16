#!/usr/bin/env python3
"""Consolidate every owner identity for one person onto a single owner id.

Why this exists: this deployment accumulated four owner values for one human.
`users` holds two rows -- `pedro` (8e4af31c...) and `admin` (aaaa...) -- and
alongside those uuids the older rows carry the *login name* as the owner, from
before sessions switched from carrying a name to carrying a user id (the change
`shared.owner_of` exists to paper over). Measured 2026-09-16: 70 chats under the
pedro uuid, 24 under the admin uuid, 33,205 usage_events under the literal string
"admin", and one api_token under the literal string "pedro".

The visible damage was not only a split history. `handle_sessions_resume` guards
against opening a second chat for a CLI session already open, but it searches
`chat_list(owner)` -- only the current identity's chats -- so the guard cannot
see the other identity's. One live session, `voice-chat-app`, had three chats
pointing at it for exactly that reason.

Usage:
    wc-merge-owner.py --db PATH --into TARGET_OWNER --from SRC [--from SRC ...]
                      [--apply]

Dry-run by default: it prints what it would change and writes nothing. `--apply`
performs the update inside a single transaction, so either every table moves or
none does -- a half-migrated owner is worse than an unmigrated one, because the
split would then be invisible rather than merely wrong.

Deliberately NOT done here, both because they destroy information the operator
may still want and because neither is needed to fix the split:

* the redundant `users` row is left in place. Removing a user is a separate
  decision with its own consequences (an account that can still authenticate but
  owns nothing is confusing; an account that has vanished is worse if anything
  still references it).
* duplicate chats pointing at one CLI session are left alone. After the merge
  they become visible to the resume guard, but collapsing them means deleting
  conversations, and which of three to keep is a judgement this script has no
  basis to make.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

# Every table whose rows carry ownership, with the column that holds it.
# `api_tokens` is in here because a token minted under the old name grants
# access as an identity that would otherwise stop existing.
OWNER_TABLES: dict[str, str] = {
    "chats": "owner_id",
    "ai_machines": "owner_id",
    "usage_events": "owner_id",
    "read_marks": "owner_id",
    "turn_queue": "owner_id",
    "supervisors": "owner_id",
    "routed_requests": "owner_id",
    "api_tokens": "owner_id",
    "ssh_tunnels": "owner_id",
    "orchestrators": "owner_id",
    "ssh_transports": "owner_id",
    "agent_reply_log": "owner_id",
    "transport_sync_requests": "owner_id",
    "generated_images": "owner_id",
}


def _merge_read_marks(con: sqlite3.Connection, target: str, sources: list[str]) -> int:
    """Fold read_marks onto *target*, collapsing rows that would collide.

    `read_marks` is the one table a plain UPDATE cannot move, because it is
    keyed `PRIMARY KEY (owner_id, kind, ref_id)`: if both identities marked the
    same chat read, rewriting the owner produces two rows with the same key and
    the whole transaction aborts. Measured 2026-09-16, 10 rows collided.

    A rehearsal against a copy is what surfaced this, not inspection -- the
    constraint is declared inline in CREATE TABLE as a composite primary key,
    so a scan of `sqlite_master` for unique *indexes* reports nothing, and so
    does a grep for the word UNIQUE in the table's own SQL.

    The semantics are not in doubt, which is why collapsing is safe here and is
    not attempted for chats: a read mark asserts "this person has read X". Two
    identities of one person having each read X means the person has read it.
    The surviving row keeps the LATEST read_at and the latest dismissed_at,
    because "read more recently" and "dismissed at all" are both the stronger
    statement; MAX ignores NULLs, so a dismissal on either side survives a NULL
    on the other.

    Returns the number of rows that now belong to *target*.
    """
    placeholders = ",".join("?" * len(sources))
    owners = [target, *sources]
    all_placeholders = ",".join("?" * len(owners))

    # One winning row per (kind, ref_id) across every identity being merged.
    winners = con.execute(
        f"SELECT kind, ref_id, MAX(read_at), MAX(dismissed_at) "
        f"FROM read_marks WHERE owner_id IN ({all_placeholders}) "
        f"GROUP BY kind, ref_id",
        owners,
    ).fetchall()

    con.execute(
        f"DELETE FROM read_marks WHERE owner_id IN ({all_placeholders})", owners
    )
    con.executemany(
        "INSERT INTO read_marks (owner_id, kind, ref_id, read_at, dismissed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(target, kind, ref, read_at, dismissed) for kind, ref, read_at, dismissed in winners],
    )
    return len(winners)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def plan(con: sqlite3.Connection, target: str, sources: list[str]) -> list[tuple[str, int]]:
    """Rows that would move, per table. Read-only."""
    counts: list[tuple[str, int]] = []
    placeholders = ",".join("?" * len(sources))
    for table, column in OWNER_TABLES.items():
        if not _table_exists(con, table):
            continue
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})",
            sources,
        ).fetchone()[0]
        counts.append((table, n))
    return counts


def apply(con: sqlite3.Connection, target: str, sources: list[str]) -> list[tuple[str, int]]:
    """Move every source owner onto *target*, all tables in one transaction."""
    placeholders = ",".join("?" * len(sources))
    moved: list[tuple[str, int]] = []
    with con:  # commits on success, rolls back on any exception
        for table, column in OWNER_TABLES.items():
            if not _table_exists(con, table):
                continue
            if table == "read_marks":
                # Cannot be a plain UPDATE -- composite primary key. See
                # _merge_read_marks for why collapsing is correct here.
                moved.append((table, _merge_read_marks(con, target, sources)))
                continue
            cur = con.execute(
                f"UPDATE {table} SET {column} = ? "
                f"WHERE {column} IN ({placeholders})",
                [target, *sources],
            )
            moved.append((table, cur.rowcount))
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--into", required=True, help="owner id everything becomes")
    ap.add_argument("--from", dest="sources", action="append", required=True,
                    help="owner value to move (repeatable)")
    ap.add_argument("--apply", action="store_true",
                    help="write the change; omit for a dry run")
    args = ap.parse_args()

    if args.into in args.sources:
        print("refusing: --into is also listed in --from", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db)
    try:
        rows = apply(con, args.into, args.sources) if args.apply \
            else plan(con, args.into, args.sources)
    finally:
        con.close()

    verb = "moved" if args.apply else "would move"
    total = 0
    for table, n in rows:
        total += n
        if n:
            print(f"  {table:26} {verb} {n}")
    print(f"  {'TOTAL':26} {verb} {total}")
    if not args.apply:
        print("\ndry run -- nothing written. re-run with --apply to perform it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
