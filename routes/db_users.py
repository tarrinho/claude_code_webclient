# db_users.py — Users, application settings, and API tokens.
#
# Extracted from db.py so the auth route path does not need the full
# database module.

import logging
import uuid
from typing import Any

import db

_log = logging.getLogger("wc.db.users")


async def user_get_by_name(name: str) -> dict[str, Any] | None:
    cur = await db.db_conn.execute(
        "SELECT id, email, name, password, role, created_at FROM users WHERE name = ?",
        (name,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def user_create(
    name: str, email: str | None, password: str, role: str = "admin"
) -> None:
    user_id = uuid.uuid4().hex
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO users (id, name, email, password, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name, email, password, role, now),
    )
    await db.db_conn.commit()


async def setting_get(key: str) -> str | None:
    cur = await db.db_conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


async def setting_set(key: str, value: str) -> None:
    await db.db_conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, db._now()),
    )
    await db.db_conn.commit()


async def api_token_create(
    token_id: str,
    name: str,
    token_hash: str,
    owner_id: str,
    role: str,
    expires_at: str | None = None,
) -> None:
    """Store a new token. The plaintext is the caller's to show once and drop."""
    await db.db_conn.execute(
        "INSERT INTO api_tokens "
        "(id, name, token_hash, owner_id, role, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_id, name, token_hash, owner_id, role, db._now(), expires_at),
    )
    await db.db_conn.commit()


async def api_token_by_hash(token_hash: str) -> dict[str, Any] | None:
    """Look up a live token by hash, or None."""
    cur = await db.db_conn.execute(
        "SELECT * FROM api_tokens "
        "WHERE token_hash = ? AND revoked_at IS NULL "
        "  AND (expires_at IS NULL OR expires_at > ?)",
        (token_hash, db._now()),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def api_token_touch(token_id: str) -> None:
    """Record that a token was used. Called at most once a minute per token."""
    await db.db_conn.execute(
        "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (db._now(), token_id)
    )
    await db.db_conn.commit()


async def api_token_list(owner_id: str, include_revoked: bool = False) -> list[dict[str, Any]]:
    """Every token this owner has, newest first and **without the hash**."""
    clause = "" if include_revoked else " AND revoked_at IS NULL"
    cur = await db.db_conn.execute(
        "SELECT id, name, owner_id, role, created_at, expires_at, last_used_at, "
        "       revoked_at "
        f"FROM api_tokens WHERE owner_id = ?{clause} "  # nosec B608: clause is static
        "ORDER BY created_at DESC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def api_token_revoke(token_id: str, owner_id: str) -> bool:
    """Revoke one token. True if it was live and belonged to *owner_id*."""
    cur = await db.db_conn.execute(
        "UPDATE api_tokens SET revoked_at = ? "
        "WHERE id = ? AND owner_id = ? AND revoked_at IS NULL",
        (db._now(), token_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def admin_action_record(
    user_id: str, action: str, detail: str | None = None,
) -> None:
    """Log an admin action to the persistent audit trail."""
    await db.db_conn.execute(
        "INSERT INTO admin_actions (user_id, action, detail, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, action, detail, db._now()),
    )
    await db.db_conn.commit()
