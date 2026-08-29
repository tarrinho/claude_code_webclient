# db.py — SQLite bootstrap and queries for WebConsole.
#
# Single SQLite file, accessed via aiodlite (async). Schema created automatically on boot.
# All paths are resolved and validated against PROJECTS_ROOT before any filesystem op.
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import aiosqlite

import config

db_conn: aiosqlite.Connection | None = None
_messages_batch_lock: asyncio.Lock | None = None

# How long index maintenance waits for the SQLite writer lock before giving up.
_FTS_BUSY_TIMEOUT_MS: Final[int] = 5000

# Every SQLite database file starts with this. Used to reject non-DB uploads.
_SQLITE_MAGIC: Final[bytes] = b"SQLite format 3\x00"

# host/port are NOT NULL and describe the transport, so an Anthropic machine
# stores the API endpoint there. It also makes the reachability test meaningful.
_ANTHROPIC_HOST: Final[str] = "api.anthropic.com"


async def init() -> None:
    """Create the database and tables. Idempotent."""
    global db_conn, _messages_batch_lock
    _messages_batch_lock = asyncio.Lock()
    root = Path(config.PROJECTS_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)

    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db_conn = await aiosqlite.connect(str(db_path))
    db_conn.row_factory = aiosqlite.Row
    await db_conn.execute("PRAGMA journal_mode=WAL")
    await db_conn.execute("PRAGMA foreign_keys=ON")

    await db_conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            description   TEXT,
            session_id    TEXT,
            work_dir      TEXT NOT NULL,
            owner_id      TEXT NOT NULL DEFAULT 'admin',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL DEFAULT '',
            archived      INTEGER NOT NULL DEFAULT 0,
            pinned        INTEGER NOT NULL DEFAULT 0,
            pinned_at     TEXT,
            deleted_at    TEXT,
            model         TEXT,
            ai_machine_id TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_machines (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            -- 'anthropic' talks to the official API (what Claude Code uses by
            -- default); 'proxy' reaches a host running claude_proxy.py.
            provider      TEXT NOT NULL DEFAULT 'proxy',
            host          TEXT NOT NULL,
            port          INTEGER NOT NULL DEFAULT 9000,
            api_key       TEXT,
            -- The default model for turns on this machine. Keep in step with
            -- config.MODEL_NAME: a retired model id here makes every turn fail
            -- on a fresh database.
            model         TEXT NOT NULL DEFAULT 'claude-sonnet-5',
            -- JSON array of model ids offered in the picker. Empty means every
            -- model the backend serves is offered, so the feature is opt-in
            -- and an untouched machine can never present an empty picker.
            active_models TEXT NOT NULL DEFAULT '[]',
            base_url      TEXT,
            description   TEXT,
            active        INTEGER NOT NULL DEFAULT 0,
            owner_id      TEXT NOT NULL DEFAULT 'admin',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT NOT NULL REFERENCES chats(id),
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

        -- FTS5 index for full-text chat search on message bodies.
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content_rowid=id
        );
        -- Manually maintained via _refresh_fts_sync() — the
        -- content=auto-trigger path is known to be broken on some
        -- SQLite 3.46.x builds (MATCH returns 0 despite entries).
        -- Manually maintained via _refresh_fts_sync() — content=auto
        -- triggers don't work on some SQLite builds.

        CREATE TABLE IF NOT EXISTS users (
            id       TEXT PRIMARY KEY,
            email    TEXT,
            name     TEXT NOT NULL,
            password TEXT NOT NULL,
            role     TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- One row per model per completed turn, from Claude Code's `result`
        -- frame. provider is denormalised here so the cost-display rule
        -- survives the machine later being edited, renamed, or deleted.
        CREATE TABLE IF NOT EXISTS usage_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id               TEXT NOT NULL,
            owner_id              TEXT NOT NULL,
            model                 TEXT NOT NULL,
            provider              TEXT NOT NULL DEFAULT 'proxy',
            input_tokens          INTEGER NOT NULL DEFAULT 0,
            output_tokens         INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd              REAL,
            duration_ms           INTEGER,
            is_error              INTEGER NOT NULL DEFAULT 0,
            created_at            TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_owner_time
            ON usage_events(owner_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_usage_owner_model
            ON usage_events(owner_id, model);
    """)
    await _ensure_chat_columns()
    await db_conn.commit()
    # Bounded growth without a scheduler: one indexed DELETE per startup.
    await usage_prune(config.USAGE_RETENTION_DAYS)


async def _ensure_chat_columns() -> None:
    """Apply additive chat schema migrations for existing databases."""
    cursor = await db_conn.execute("PRAGMA table_info(chats)")
    columns = {row["name"] for row in await cursor.fetchall()}
    migrations = {
        "pinned": "ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        "pinned_at": "ALTER TABLE chats ADD COLUMN pinned_at TEXT",
        "deleted_at": "ALTER TABLE chats ADD COLUMN deleted_at TEXT",
        "model": "ALTER TABLE chats ADD COLUMN model TEXT",
        "ai_machine_id": "ALTER TABLE chats ADD COLUMN ai_machine_id TEXT",
    }
    for name, sql in migrations.items():
        if name not in columns:
            await db_conn.execute(sql)

    # Migrate ai_machines table for existing databases
    try:
        ma_cursor = await db_conn.execute("PRAGMA table_info(ai_machines)")
        ma_columns = {row["name"] for row in await ma_cursor.fetchall()}
    except Exception:  # noqa: BLE001 -- PRAGMA can fail on new tables
        ma_columns = set()
    if "owner_id" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'"
        )
    if ma_columns and "provider" not in ma_columns:
        # Existing rows are all claude_proxy hosts -- the default matches them.
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN provider TEXT NOT NULL DEFAULT 'proxy'"
        )
    if ma_columns and "active_models" not in ma_columns:
        # '[]' means "offer everything served", which is what existing rows did.
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN active_models TEXT NOT NULL DEFAULT '[]'"
        )

    await db_conn.commit()


async def close() -> None:
    global db_conn
    if db_conn:
        await db_conn.close()
        db_conn = None


# ── Chat CRUD ──────────────────────────────────────────────────────────────────────────


_CHAT_COLUMNS = (
    "id, title, description, session_id, work_dir, owner_id, created_at, "
    "updated_at, archived, pinned, pinned_at, deleted_at, model, ai_machine_id"
)
_ALLOWED_CHAT_FIELDS = {
    "title",
    "description",
    "archived",
    "pinned",
    "pinned_at",
    "model",
    "ai_machine_id",
}


async def chat_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        f"SELECT {_CHAT_COLUMNS} FROM chats "  # nosec B608: columns are static
        "WHERE owner_id = ? AND deleted_at IS NULL "
        "ORDER BY archived ASC, "
        "CASE WHEN archived = 0 THEN pinned ELSE 0 END DESC, "
        "CASE WHEN archived = 0 AND pinned = 1 THEN pinned_at END DESC, "
        "updated_at DESC",
        (owner_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def chat_get(
    chat_id: str,
    owner_id: str,
    include_archived: bool = False,
) -> dict[str, Any] | None:
    archived_filter = "" if include_archived else " AND archived = 0"
    cur = await db_conn.execute(
        f"SELECT {_CHAT_COLUMNS} FROM chats "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"
        + archived_filter,  # nosec B608: filter is static, values parameterized
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def chat_create(
    chat_id: str,
    title: str,
    description: str | None,
    work_dir: str,
    owner_id: str = "admin",
) -> str:
    now = _now()
    await db_conn.execute(
        "INSERT INTO chats (id, title, description, work_dir, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, title, description, work_dir, owner_id, now, now),
    )
    await db_conn.commit()
    return now


async def chat_update(chat_id: str, owner_id: str, **fields: Any) -> bool:
    if not fields or not set(fields).issubset(_ALLOWED_CHAT_FIELDS):
        return False
    if "pinned" in fields and "pinned_at" not in fields:
        fields["pinned_at"] = _now() if fields["pinned"] else None
    sets = ", ".join(k + " = ?" for k in fields)
    sets += ", updated_at = ?"
    sql = (
        "UPDATE chats SET "
        + sets
        + " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"
    )  # nosec B608: fields are allowlisted
    vals = list(fields.values()) + [_now(), chat_id, owner_id]
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
    updated = cur.rowcount > 0
    # Each indexed row carries the chat title, so a rename makes every entry
    # for this chat stale. Rebuild them.
    if updated and "title" in fields:
        await _fts_rebuild(chat_id)
    return updated


async def chat_archive(chat_id: str, owner_id: str, archived: int = 1) -> bool:
    return await chat_update(chat_id, owner_id, archived=archived)


async def chat_delete(chat_id: str, owner_id: str) -> bool:
    """Delete an owned conversation and messages while preserving its workspace."""
    cur = await db_conn.execute(
        "SELECT id FROM chats WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (chat_id, owner_id),
    )
    if await cur.fetchone() is None:
        return False
    # Capture the message ids first: once the rows are gone, an id lookup via
    # the messages table matches nothing and the index entries are orphaned.
    cur = await db_conn.execute(
        "SELECT id FROM messages WHERE chat_id = ?", (chat_id,)
    )
    msg_ids = [row["id"] for row in await cur.fetchall()]
    try:
        await db_conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db_conn.execute(
            "DELETE FROM chats WHERE id = ? AND owner_id = ?",
            (chat_id, owner_id),
        )
        await db_conn.commit()
    except Exception:
        await db_conn.rollback()
        raise
    await _fts_forget_ids(msg_ids)
    return True


async def chat_fork(
    src_chat_id: str,
    owner_id: str,
) -> dict[str, Any] | None:
    """Duplicate a chat's metadata and messages, returning the new chat dict.

    The forked chat gets a fresh UUID, an appended title suffix, and
    an empty work_dir under the same projects root.
    """
    src = await chat_get(src_chat_id, owner_id)
    if not src:
        return None

    new_chat_id = uuid.uuid4().hex
    now = _now()
    new_title = src["title"] + " (fork)"

    # Create a sibling workspace directory.
    work_dir = Path(src["work_dir"]).parent / f"{new_chat_id}_workspace"
    work_dir.mkdir(parents=True, exist_ok=True)

    await db_conn.execute(
        "INSERT INTO chats (id, title, description, work_dir, owner_id, "
        "created_at, updated_at, pinned, pinned_at, model, ai_machine_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
        (
            new_chat_id,
            new_title,
            src.get("description"),
            str(work_dir),
            owner_id,
            now,
            now,
            src.get("model"),
            src.get("ai_machine_id"),
        ),
    )

    # Copy messages in bulk.
    rows: list[tuple[str, str]] = []
    cur = await db_conn.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (src_chat_id,),
    )
    for row in await cur.fetchall():
        rows.append((row["role"], row["content"]))

    if rows:
        now2 = _now()
        for role, content in rows:
            await db_conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (new_chat_id, role, content, now2),
            )
        await db_conn.commit()
        # A fork is a bulk copy into a brand-new chat, so a per-chat rebuild
        # indexes exactly the rows just inserted.
        await _fts_rebuild(new_chat_id)

    return await chat_get(new_chat_id, owner_id)


async def chat_set_session(chat_id: str, session_id: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET session_id = ?, updated_at = ? WHERE id = ?",
        (session_id, _now(), chat_id),
    )
    await db_conn.commit()


async def chat_set_model(chat_id: str, model: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET model = ?, updated_at = ? WHERE id = ?",
        (model, _now(), chat_id),
    )
    await db_conn.commit()


async def chat_set_title(chat_id: str, title: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), chat_id),
    )
    await db_conn.commit()


# ── Chat FTS5 Search ──────────────────────────────────────────────────────────────────

async def chat_search(owner_id: str, query: str) -> list[dict[str, Any]]:
    """Search message bodies using FTS5.

    Returns a list of unique chats that match *query*, ordered by
    FTS5 rank (best match first).  Each entry is a chat dict with an
    added ``snippet`` key showing the matching fragment.
    """
    # FTS5 MATCH query — values are parameterized, the MATCH keyword is SQL.
    rows: list[dict[str, Any]] = []
    try:
        cur = await db_conn.execute(
            "SELECT rowid FROM messages_fts "  # nosec B608: MATCH is SQL keyword
            "WHERE content MATCH ?",
            (query,),
        )
        match_ids = [row["rowid"] for row in await cur.fetchall()]
    except Exception:  # noqa: BLE001 -- FTS5 may not exist on fresh DBs
        match_ids = []

    if not match_ids:
        return []

    # Fetch full chat details for matching rows, deduplicate by chat_id.
    _placeholders = ",".join("?" for _ in match_ids)
    cur = await db_conn.execute(
        f"SELECT c.id AS chat_id, c.title, c.description, c.session_id, "
        f"c.work_dir, c.owner_id, c.created_at, c.updated_at, "
        f"c.archived, c.pinned, c.pinned_at, c.deleted_at, c.model, "
        f"c.ai_machine_id, m.id AS msg_id, m.content AS msg_snippet "  # nosec B608: static SQL
        "FROM chats c "
        f"JOIN messages m ON m.chat_id = c.id AND m.id IN ({_placeholders}) "
        "WHERE c.owner_id = ? AND c.deleted_at IS NULL "
        "ORDER BY c.updated_at DESC",
        match_ids + [owner_id],
    )
    seen: set[str] = set()
    for row in await cur.fetchall():
        chat_id = row["chat_id"]
        if chat_id in seen:
            continue
        seen.add(chat_id)
        d = dict(row)
        d["id"] = d.pop("chat_id")  # keep key name consistent with other endpoints
        d["snippet"] = d.pop("msg_snippet", "") or ""
        rows.append(d)

    return rows


# ── Messages ───────────────────────────────────────────────────────────────────────────


async def messages_get(chat_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ── FTS5 index maintenance ──────────────────────────────────────────────────────────

def _fts_connect() -> sqlite3.Connection:
    """Open the dedicated synchronous connection used for index maintenance.

    A separate connection avoids aiosqlite transaction conflicts. busy_timeout
    makes it wait for the writer lock instead of failing with "database is
    locked" the moment a turn is writing concurrently.
    """
    sync = sqlite3.connect(str(Path(config.DB_PATH)), check_same_thread=False)
    sync.execute("PRAGMA journal_mode=WAL")
    sync.execute("PRAGMA foreign_keys=ON")
    sync.execute(f"PRAGMA busy_timeout={_FTS_BUSY_TIMEOUT_MS}")
    return sync


def _fts_index_ids_sync(msg_ids: Sequence[int | None]) -> None:
    """Index exactly *msg_ids*, replacing any existing entries for them.

    Cost is proportional to len(msg_ids), not to the size of the conversation.
    Each message is indexed with its chat title prefixed so that title-based
    searches also surface through the index.
    """
    ids = [i for i in msg_ids if i is not None]
    if not ids:
        return
    sync = None
    try:
        sync = _fts_connect()
        marks = ",".join("?" for _ in ids)
        sync.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ({marks})",  # nosec B608
            ids,
        )
        rows = sync.execute(
            "SELECT m.id, m.content, c.title FROM messages m "
            "JOIN chats c ON c.id = m.chat_id "
            f"WHERE m.id IN ({marks})",  # nosec B608: generated placeholders
            ids,
        ).fetchall()
        for msg_id, content, title in rows:
            if content:
                text = f"{title} {content}" if title else content
                sync.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                    (msg_id, text),
                )
        sync.commit()
    except Exception:  # noqa: BLE001,S110 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


def _fts_forget_ids_sync(msg_ids: Sequence[int | None]) -> None:
    """Drop *msg_ids* from the index.

    Callers must capture the ids **before** deleting the message rows: a purge
    that resolves ids via the messages table after the fact matches nothing and
    leaves the entries orphaned.
    """
    ids = [i for i in msg_ids if i is not None]
    if not ids:
        return
    sync = None
    try:
        sync = _fts_connect()
        marks = ",".join("?" for _ in ids)
        sync.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ({marks})",  # nosec B608
            ids,
        )
        sync.commit()
    except Exception:  # noqa: BLE001,S110 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


def _refresh_fts_sync(chat_id: str | None = None) -> None:
    """Full rebuild of the FTS5 index for one chat, or for every chat.

    Used for backfill and after a chat title changes (the title is baked into
    each indexed row). Prefer :func:`_fts_index_ids_sync` on the write path --
    this walks every message of the chat.
    """
    sync = None
    try:
        sync = _fts_connect()
        if chat_id:
            # Delete stale entries for this chat.
            sync.execute(
                "DELETE FROM messages_fts WHERE rowid IN "
                "(SELECT id FROM messages WHERE chat_id = ?)",
                (chat_id,),
            )
        else:
            sync.execute("DELETE FROM messages_fts")

        # Re-insert all message content, prefixed with chat title.
        rows = sync.execute(
            "SELECT m.id, m.content, c.title FROM messages m "
            "JOIN chats c ON c.id = m.chat_id"
            + (" AND m.chat_id = ?" if chat_id else "")
            + (" ORDER BY m.id ASC" if chat_id else ""),
            (chat_id,) if chat_id else (),
        ).fetchall()
        for msg_id, content, title in rows:
            if content:
                # Prefix with title so title searches also work.
                text = f"{title} {content}" if title else content
                sync.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                    (msg_id, text),
                )
        sync.commit()
    except Exception:  # noqa: BLE001,S110 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


# ── Async wrappers: keep the blocking sqlite3 work off the event loop ──────────


async def _fts_index_ids(msg_ids: Sequence[int | None]) -> None:
    await asyncio.to_thread(_fts_index_ids_sync, list(msg_ids))


async def _fts_forget_ids(msg_ids: Sequence[int | None]) -> None:
    await asyncio.to_thread(_fts_forget_ids_sync, list(msg_ids))


async def _fts_rebuild(chat_id: str | None = None) -> None:
    await asyncio.to_thread(_refresh_fts_sync, chat_id)


async def messages_append(chat_id: str, role: str, content: str) -> int:
    cur = await db_conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, _now()),
    )
    await db_conn.commit()
    last_id = cur.lastrowid
    await _fts_index_ids([last_id])
    return last_id


async def messages_batch(chat_id: str, rows: list[tuple[str, str]]) -> list[int]:
    """Insert multiple messages atomically and return their exact IDs."""
    global _messages_batch_lock
    if not rows:
        return []
    if _messages_batch_lock is None:
        _messages_batch_lock = asyncio.Lock()
    async with _messages_batch_lock:
        ids = []
        try:
            await db_conn.execute("BEGIN")
            now = _now()
            for role, content in rows:
                cur = await db_conn.execute(
                    "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                    (chat_id, role, content, now),
                )
                ids.append(cur.lastrowid)
            await db_conn.commit()
        except Exception:
            await db_conn.rollback()
            raise
        await _fts_index_ids(ids)
        return ids


# ── Users ──────────────────────────────────────────────────────────────────────────────


async def user_get_by_name(name: str) -> dict[str, Any] | None:
    cur = await db_conn.execute(
        "SELECT id, email, name, password, role, created_at FROM users WHERE name = ?",
        (name,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def user_create(
    name: str, email: str | None, password: str, role: str = "admin"
) -> None:
    user_id = uuid.uuid4().hex
    now = _now()
    await db_conn.execute(
        "INSERT INTO users (id, name, email, password, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name, email, password, role, now),
    )
    await db_conn.commit()


# ── Application settings ────────────────────────────────────────────────────────────────


async def setting_get(key: str) -> str | None:
    cur = await db_conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


async def setting_set(key: str, value: str) -> None:
    await db_conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _now()),
    )
    await db_conn.commit()


# ── AI Machines ────────────────────────────────────────────────────────────────────────


async def ai_machine_active(owner_id: str) -> dict[str, Any] | None:
    """Return the owner's active machine without exposing its API key."""
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, active "
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machines_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        "created_at, updated_at "
        "FROM ai_machines WHERE owner_id = ? ORDER BY active DESC, name ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def ai_machine_get(id: str, owner_id: str) -> dict[str, Any] | None:
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        # Derived in SQL so callers can report whether a key is configured
        # without the value ever leaving this layer. Computing it from an
        # api_key this query deliberately omits made it always false.
        "CASE WHEN api_key IS NOT NULL AND TRIM(api_key) <> '' THEN 1 ELSE 0 END "
        "AS has_api_key, "
        "created_at, updated_at "
        "FROM ai_machines WHERE id = ? AND owner_id = ?",
        (id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machine_create(
    machine_id: str,
    name: str,
    host: str,
    port: int,
    api_key: str | None,
    model: str,
    base_url: str | None,
    description: str | None,
    owner_id: str,
    provider: str = "proxy",
) -> str:
    now = _now()
    await db_conn.execute(
        "INSERT INTO ai_machines "
        "(id, name, provider, host, port, api_key, model, base_url, description, active, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            machine_id,
            name,
            provider,
            host,
            port,
            api_key,
            model,
            base_url,
            description,
            owner_id,
            now,
            now,
        ),
    )
    await db_conn.commit()
    return now


async def ai_machine_update(
    machine_id: str,
    owner_id: str,
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    description: str | None = None,
    provider: str | None = None,
) -> bool:
    pairs: list[tuple[str, Any]] = [
        ("name", name),
        ("provider", provider),
        ("host", host),
        ("port", port),
        ("api_key", api_key),
        ("model", model),
        ("base_url", base_url),
        ("description", description),
    ]
    sets: list[str] = []
    vals: list[Any] = []
    for field, value in pairs:
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.extend([machine_id, owner_id])
    sql = (
        "UPDATE ai_machines SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?"
    )  # nosec B608: fields are allowlisted
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_activate(machine_id: str, owner_id: str) -> bool:
    """Deactivate all machines and activate the one requested."""
    try:
        await db_conn.execute("BEGIN")
        await db_conn.execute(
            "UPDATE ai_machines SET active = 0 WHERE owner_id = ?",
            (owner_id,),
        )
        cur = await db_conn.execute(
            "UPDATE ai_machines SET active = 1, updated_at = ? WHERE id = ? AND owner_id = ?",
            (_now(), machine_id, owner_id),
        )
        await db_conn.commit()
        return cur.rowcount > 0
    except Exception:
        await db_conn.rollback()
        raise


async def ai_machine_delete(machine_id: str, owner_id: str) -> bool:
    cur = await db_conn.execute(
        "DELETE FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    await db_conn.commit()
    return cur.rowcount > 0


async def chat_owner(chat_id: str) -> str | None:
    """Return the owner of *chat_id*, so the runner can resolve its backend."""
    cur = await db_conn.execute("SELECT owner_id FROM chats WHERE id = ?", (chat_id,))
    row = await cur.fetchone()
    return row["owner_id"] if row else None


async def ai_machine_backend(owner_id: str) -> dict[str, Any] | None:
    """Return the active machine *including* its API key, for the runner only.

    Every other reader goes through ``ai_machine_active``/``ai_machines_list``,
    which omit ``api_key`` so it cannot reach an API response by accident.
    """
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, base_url, api_key "
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


def parse_active_models(raw: Any) -> list[str]:
    """Decode the active_models column into a list of ids.

    Anything unreadable decodes to empty, which means "offer everything the
    backend serves" -- the safe direction, because the alternative is a picker
    with nothing in it.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        entries = raw
    else:
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(entries, list):
        return []
    seen: set[str] = set()
    models: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip() and entry.strip() not in seen:
            seen.add(entry.strip())
            models.append(entry.strip()[:200])
    return models


async def ai_machine_set_models(
    machine_id: str, owner_id: str, active: list[str], default: str | None
) -> bool:
    """Set which models a machine offers, and which one it defaults to."""
    sets = ["active_models = ?", "updated_at = ?"]
    vals: list[Any] = [json.dumps(active), _now()]
    if default:
        sets.insert(1, "model = ?")
        vals.insert(1, default)
    cur = await db_conn.execute(
        "UPDATE ai_machines SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?",
        [*vals, machine_id, owner_id],
    )  # nosec B608: column names are literals, values parameterised
    await db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_api_key(machine_id: str, owner_id: str) -> str | None:
    """Return one machine's API key. Kept separate from ai_machine_get so the
    key is only ever fetched where it is deliberately needed."""
    cur = await db_conn.execute(
        "SELECT api_key FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    return row["api_key"] if row else None


async def ai_machine_seed_anthropic(owner_id: str) -> str | None:
    """Ensure the owner has an Anthropic API machine, and return its id.

    Claude Code's native backend is the official API, so every account gets an
    entry for it. Seeded inactive and with no API key: an unset key makes the
    CLI fall back to the host's own login, which is the normal setup.
    """
    cur = await db_conn.execute(
        "SELECT id FROM ai_machines WHERE owner_id = ? AND provider = 'anthropic' "
        "LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    if row:
        return row["id"]
    machine_id = uuid.uuid4().hex
    await ai_machine_create(
        machine_id,
        "Anthropic API",
        _ANTHROPIC_HOST,
        443,
        None,
        config.ANTHROPIC_MODEL,
        config.ANTHROPIC_BASE_URL,
        "Official Anthropic API — Claude Code's default backend.",
        owner_id,
        provider="anthropic",
    )
    return machine_id


# ── Usage accounting ───────────────────────────────────────────────────────────────────


def _cutoff(days: int) -> str:
    """Return the ISO timestamp *days* before now, matching _now()'s format."""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max(0, days) * 86400)
    )


async def usage_record(
    chat_id: str,
    owner_id: str,
    model: str,
    provider: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    is_error: bool = False,
) -> int | None:
    """Record one model's usage for a completed turn.

    Returns the row id, or None if the write failed. Accounting must never
    break a turn that has already succeeded, so failures are swallowed.
    """
    if not chat_id or not owner_id or not model:
        return None
    try:
        cur = await db_conn.execute(
            "INSERT INTO usage_events "
            "(chat_id, owner_id, model, provider, input_tokens, output_tokens, "
            " cache_read_tokens, cache_creation_tokens, cost_usd, duration_ms, "
            " is_error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                owner_id,
                model,
                provider or "proxy",
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(cache_read_tokens or 0),
                int(cache_creation_tokens or 0),
                cost_usd,
                duration_ms,
                1 if is_error else 0,
                _now(),
            ),
        )
        await db_conn.commit()
        return cur.lastrowid
    except Exception:  # noqa: BLE001 -- never fail a turn that already succeeded
        return None


async def usage_totals(owner_id: str, days: int | None = 30) -> list[dict[str, Any]]:
    """Per-model aggregates for *owner_id*. ``days=None`` means all time."""
    params: list[Any] = [owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db_conn.execute(
        "SELECT model, provider, COUNT(*) AS requests, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "SUM(COALESCE(cost_usd, 0)) AS cost_usd, "
        "SUM(is_error) AS errors, MAX(created_at) AS last_used "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY model, provider ORDER BY requests DESC, model ASC",
        params,
    )
    return [dict(row) for row in await cur.fetchall()]


async def usage_overall(owner_id: str, days: int | None = 30) -> dict[str, Any]:
    """Totals across every model, so the header does not re-sum in the client."""
    params: list[Any] = [owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db_conn.execute(
        "SELECT COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(is_error), 0) AS errors, "
        "COUNT(DISTINCT model) AS models "
        f"FROM usage_events WHERE {where}",  # nosec B608: clause is static
        params,
    )
    row = await cur.fetchone()
    return dict(row) if row else {}


async def usage_recent(owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent turns, with the conversation title joined in.

    LEFT JOIN so a deleted conversation still appears in the log rather than
    silently dropping the usage it accounted for.
    """
    cur = await db_conn.execute(
        "SELECT u.created_at, u.chat_id, u.model, u.provider, u.input_tokens, "
        "u.output_tokens, u.cost_usd, u.duration_ms, u.is_error, c.title AS chat_title "
        "FROM usage_events u LEFT JOIN chats c ON c.id = u.chat_id "
        "WHERE u.owner_id = ? ORDER BY u.id DESC LIMIT ?",
        # `limit or 50` would read 0 as "use the default", disagreeing with the
        # API layer which clamps 0 to 1. Only None means "unspecified".
        (owner_id, max(1, min(50 if limit is None else int(limit), 500))),
    )
    return [dict(row) for row in await cur.fetchall()]


async def usage_prune(days: int) -> int:
    """Delete rows older than *days*. Returns the number removed."""
    if not days or days <= 0:
        return 0
    try:
        cur = await db_conn.execute(
            "DELETE FROM usage_events WHERE created_at < ?", (_cutoff(days),)
        )
        await db_conn.commit()
        return cur.rowcount or 0
    except Exception:  # noqa: BLE001 -- pruning must never block startup
        return 0


# ── Helpers ─────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug_from_title(title: str) -> str:
    """Derive a slug from a chat title: lowercase, alphanumeric-hyphens, max 40 chars."""
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())
    slug = "-".join(p for p in slug.split("-") if p)
    return slug[:40] or "untitled"


def slug_pattern(slug: str) -> str | None:
    """Validate slug characters: [a-z0-9-], 3-40 chars.

    Returns the slug if valid, None otherwise.
    Slugs must not start or end with a dash.
    """
    if not re.fullmatch(r"[a-z0-9-]{3,40}", slug):
        return None
    if slug.startswith("-") or slug.endswith("-"):
        return None
    return slug


# ── Database backup / restore ────────────────────────────────────────────────────────


def _db_backup_sync(backup_path: str) -> bytes:
    """Copy the database and return it gzip-compressed. Blocking; call off-loop."""
    import gzip as _gzip

    sync_conn = sqlite3.connect(backup_path)
    db_conn_sync = sqlite3.connect(str(config.DB_PATH))
    try:
        db_conn_sync.backup(sync_conn)
    finally:
        db_conn_sync.close()
        sync_conn.close()
    return _gzip.compress(Path(backup_path).read_bytes())


async def db_backup() -> bytes:
    """Return a gzip-compressed SQLite backup of the entire database.

    sqlite3.backup(), the file read and the gzip pass are all blocking and
    scale with database size, so they run in a worker thread. On the event
    loop they would stall every other request, including live SSE streams.
    """
    backup_path = f"{config.DB_PATH}.backup.{int(time.time())}"
    try:
        return await asyncio.to_thread(_db_backup_sync, backup_path)
    finally:
        try:
            Path(backup_path).unlink()
        except OSError:
            pass


def _validate_sqlite_file(path: Path) -> bool:
    """Return True if *path* is a sound SQLite database with our schema.

    Runs before the live file is touched, so a corrupt or unrelated upload is
    rejected rather than swapped in.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        # A valid backup of *this* app, not just any SQLite file.
        return {"chats", "messages", "users"}.issubset(names)
    except sqlite3.DatabaseError:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


async def _reopen() -> None:
    """Reconnect and apply additive migrations. Never leaves db_conn as None."""
    await init()


async def db_restore(data: bytes) -> bool:
    """Replace the current database with the provided gzip-compressed data.

    The candidate is validated before the swap (gzip, SQLite magic, integrity
    check, expected tables), the swap itself is an atomic rename, and the
    reconnect goes through :func:`init` so schema migrations are applied -- a
    restored older backup would otherwise be missing columns the app expects.
    """
    import gzip as _gzip

    try:
        decompressed = _gzip.decompress(data)
    except Exception:  # noqa: BLE001 -- silently reject bad input
        return False

    # Reject anything that is not a SQLite database outright.
    if not decompressed.startswith(_SQLITE_MAGIC):
        return False

    db_path = Path(config.DB_PATH)
    tmp_path = db_path.with_suffix(".restore.tmp")

    try:
        tmp_path.write_bytes(decompressed)
        if not await asyncio.to_thread(_validate_sqlite_file, tmp_path):
            tmp_path.unlink(missing_ok=True)
            return False

        # Close existing connection before swapping files.
        await close()

        # Drop the old write-ahead log and shared-memory sidecars. Left in
        # place, SQLite can replay the previous database's WAL over the
        # restored file and corrupt it.
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

        # Atomic rename.
        tmp_path.replace(db_path)

        await _reopen()
        return True
    except Exception:  # noqa: BLE001 -- recover best-effort on failure
        tmp_path.unlink(missing_ok=True)
        # Leaving db_conn as None would 500 every later request until restart.
        try:
            await _reopen()
        except Exception:  # noqa: BLE001 -- final fallback, DB may be unusable
            global db_conn
            db_conn = None
        return False


# ── Claude Code session files ──────────────────────────────────────────────────────

_CLAUDE_SESSIONS_DIR: Final[Path] = Path.home() / ".claude" / "sessions"


async def read_claude_sessions() -> list[dict[str, Any]]:
    """Read ~/.claude/sessions/*.json files and return a list of session dicts.

    Returns sessions that are:
    - Not the current session (by PID match)
    - Have a non-empty name or sessionId
    - Are interactive kind

    Each dict has: id, name, cwd, kind, startedAt, updatedAt, sessionId
    """
    import os as _os

    sessions: list[dict[str, Any]] = []
    try:
        if not _CLAUDE_SESSIONS_DIR.is_dir():
            return []
    except (PermissionError, OSError):
        return []

    # Current PID to filter out the running session
    current_pid = _os.getpid()

    try:
        files = sorted(_CLAUDE_SESSIONS_DIR.glob("*.json"))
    except (PermissionError, OSError):
        return []

    for fpath in files:
        try:
            content = await asyncio.to_thread(fpath.read_text)
            data = json.loads(content)
        except (json.JSONDecodeError, OSError):
            continue
        # Valid JSON that isn't an object still has no .get(): a single file
        # holding a list or a bare string raised AttributeError out of the
        # loop and failed the whole listing, rather than being skipped like
        # every other unusable file here.
        if not isinstance(data, dict):
            continue

        # Skip the active CLI process, but retain WebConsole-created session files.
        # WebConsole writes these files with its own PID so the CLI can discover
        # them; filtering by PID alone would hide the bidirectional-sync record.
        pid = data.get("pid")
        try:
            same_process = bool(pid) and int(pid) == current_pid
        except (TypeError, ValueError):
            same_process = False
        if same_process and data.get("entrypoint") != "webconsole":
            continue

        name = data.get("name", "")
        kind = data.get("kind", "")
        session_id = data.get("sessionId", "")

        # Only include interactive sessions with a name
        if kind != "interactive":
            continue
        if not name and not session_id:
            continue

        started_at = _format_timestamp(data.get("startedAt"))
        updated_at = _format_timestamp(data.get("updatedAt"))
        if not started_at:
            started_at = ""
        if not updated_at:
            updated_at = ""

        model = data.get("model", "")
        if not model and session_id:
            # Transcript reads are blocking file I/O; keep them off the loop.
            model = await asyncio.to_thread(_lookup_session_model, session_id) or ""

        sessions.append(
            {
                "id": session_id if session_id else fpath.stem,
                "name": name or "Untitled",
                "cwd": data.get("cwd", ""),
                "kind": kind,
                "startedAt": started_at,
                "updatedAt": updated_at,
                "sessionId": session_id,
                "model": model,
                "entrypoint": data.get("entrypoint", ""),
                "live": _pid_is_running(pid),
                "file": fpath.name,
            }
        )

    return _dedupe_sessions(sessions)


def _pid_is_running(pid: Any) -> bool:
    """Return True if *pid* names a live process.

    Signal 0 performs the permission and existence checks without delivering
    anything, which is the cheapest liveness probe available.
    """
    import os as _os

    try:
        _os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def _dedupe_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse records that describe the same CLI session.

    Resuming a CLI session writes a second file, ~/.claude/sessions/<id>.json
    with entrypoint "webconsole", so the CLI can see the link. The real CLI
    already has its own PID-named file for that same sessionId, so listing the
    directory returned both and the sidebar showed every resumed session twice
    -- once under its real name ("cweb2") and once as "CLI: b198eb69...".
    Each resume added another, permanently.

    The CLI record wins: it carries the name the user actually chose and the
    live process. A "webconsole" record survives only when no CLI record
    claims that sessionId, which is how a session whose process has exited
    stays visible and removable.
    """
    best: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for item in sessions:
        key = item.get("sessionId")
        if not key:
            unkeyed.append(item)
            continue
        current = best.get(key)
        if current is None or _session_rank(item) > _session_rank(current):
            best[key] = item
    return [*best.values(), *unkeyed]


def _session_rank(item: dict[str, Any]) -> tuple[int, int]:
    """Order candidates for the same sessionId; highest wins."""
    return (
        1 if item.get("entrypoint") == "cli" else 0,
        1 if item.get("live") else 0,
    )


def _session_is_live(session_id: str) -> bool:
    """True if any record for *session_id* has a running process behind it."""
    try:
        files = list(_CLAUDE_SESSIONS_DIR.glob("*.json"))
    except (PermissionError, OSError):
        return False
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("sessionId") == session_id and _pid_is_running(data.get("pid")):
            return True
    return False


def delete_claude_session_file(session_id: str) -> bool:
    """Remove the WebConsole shadow record for *session_id*.

    Only ever deletes a file this application wrote. The CLI's own PID-named
    files belong to Claude Code and are left alone even when the process has
    exited -- reaping those is not WebConsole's business, and a live session
    must never lose its record because someone clicked a cross in a sidebar.

    Returns True if a file was removed.
    """
    import os as _os

    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Invalid session_id (contains path separators or ..)")

    path = _CLAUDE_SESSIONS_DIR / f"{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    if data.get("entrypoint") != "webconsole":
        raise ValueError("Refusing to delete a session file WebConsole did not write")

    # Liveness has to be judged across every record for this sessionId, not
    # from this file's own pid. A shadow record stores the pid of whichever
    # WebConsole process wrote it, which is long dead by the time anyone looks
    # -- so trusting it alone let a running CLI session be cleared from the
    # sidebar, because the session's real liveness lives in the CLI's separate
    # PID-named file.
    if _session_is_live(session_id):
        raise ValueError("Refusing to delete a session file whose process is running")

    try:
        _os.unlink(path)
    except OSError:
        return False
    return True


def _format_timestamp(ts: float | str | None) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    if ts is None:
        return ""
    try:
        ts_num = int(ts)
        if ts_num > 1e12:  # milliseconds
            ts_num = ts_num // 1000
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_num))
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


# ── Transcript-backed model discovery ───────────────────────────────────────────

_CLAUDE_PROJECTS_DIR: Final[Path] = Path.home() / ".claude" / "projects"

# Session ids are UUID-like. Restrict the charset before interpolating one into
# a glob pattern so it cannot traverse out of the projects directory.
_SESSION_ID_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Bytes read from the end of a transcript on the fast path.
_TRANSCRIPT_TAIL_BYTES: Final[int] = 1 << 20

# In-process cache keyed by session_id; refreshed each read_claude_sessions call
_model_cache: dict[str, str] = {}


def _session_transcript_paths(session_id: str) -> list[Path]:
    """Return the transcript files belonging to *session_id*.

    Claude stores each transcript as
    ``~/.claude/projects/<project-dir>/<session_id>.jsonl`` -- one level below
    the projects root, which is why a top-level ``*.jsonl`` glob matched
    nothing. Targeting the filename also avoids reading unrelated transcripts.
    """
    if not _SESSION_ID_SAFE_RE.fullmatch(session_id):
        return []
    try:
        if not _CLAUDE_PROJECTS_DIR.is_dir():
            return []
        return sorted(_CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    except (PermissionError, OSError):
        return []


def _model_from_lines(lines: Sequence[str], session_id: str) -> str | None:
    """Scan *lines* newest-first and return the first usable model."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record: dict = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Assistant message line carries the model in message.model
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        if record.get("sessionId") != session_id:
            continue
        model = message.get("model")
        if model and model != "<synthetic>":
            return model
    return None


def _extract_model_from_transcript(session_id: str) -> str | None:
    """Return the last non-synthetic model recorded for *session_id*.

    Reads only that session's transcript, and only its tail on the fast path --
    the newest assistant message is at the end, and transcripts reach tens of
    megabytes. Falls back to a full scan when the tail holds no assistant
    message.

    Returns None when no matching transcript line is found.
    """
    for fpath in _session_transcript_paths(session_id):
        try:
            size = fpath.stat().st_size
            with fpath.open("rb") as fh:
                if size > _TRANSCRIPT_TAIL_BYTES:
                    fh.seek(size - _TRANSCRIPT_TAIL_BYTES)
                    chunk = fh.read()
                    # Drop the leading partial line left by the seek.
                    newline = chunk.find(b"\n")
                    chunk = chunk[newline + 1 :] if newline >= 0 else b""
                else:
                    chunk = fh.read()
            model = _model_from_lines(
                chunk.decode("utf-8", errors="replace").splitlines(), session_id
            )
            if model:
                return model
            if size > _TRANSCRIPT_TAIL_BYTES:
                # Tail held no assistant message; pay for the whole file once.
                text = fpath.read_text(encoding="utf-8", errors="replace")
                model = _model_from_lines(text.splitlines(), session_id)
                if model:
                    return model
        except OSError:
            continue
    return None


def _lookup_session_model(session_id: str) -> str | None:
    """Resolve the model for *session_id* (session file or transcript).

    On first call for a session, the transcript is scanned and the result is
    cached in memory for the lifetime of the process.
    """
    if session_id in _model_cache:
        return _model_cache.get(session_id) or None

    model = _extract_model_from_transcript(session_id)
    if model:
        _model_cache[session_id] = model
    else:
        _model_cache[session_id] = ""

    return model or None


def write_claude_session_file(
    session_id: str, name: str, cwd: str, model: str = ""
) -> None:
    """Write a session file to ~/.claude/sessions/<session_id>.json so CLI can pick it up.

    This enables bidirectional sync: Web sessions become visible to CLI.
    Uses atomic write (write to temp then rename) to avoid partial reads.
    Validates session_id to prevent path traversal.
    """
    import os as _os

    # ── Security: reject path-traversal sequences ─────────────────────────────
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Invalid session_id (contains path separators or ..)")

    try:
        sessions_dir = _CLAUDE_SESSIONS_DIR
        sessions_dir.mkdir(parents=True, exist_ok=True)

        now_ms = int(time.time() * 1000)
        session_data = {
            "pid": _os.getpid(),
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": now_ms,
            "procStart": now_ms,
            "version": config.VERSION,
            "peerProtocol": "http",
            "peerFeatures": {
                "supports": ["text", "markdown"],
                "toolSupport": "complete",
            },
            "kind": "interactive",
            "entrypoint": "webconsole",
            "pidDomain": str(_os.getpid()),
            "messagingSocketPath": "",
            "name": name,
            "nameSource": "webconsole",
            "nameSince": now_ms,
            "updatedAt": now_ms,
            "model": model,
        }

        # Atomic write: write to temp, rename
        tmp_path = sessions_dir / f".tmp_{session_id}.json"
        with open(tmp_path, "w") as f:
            json.dump(session_data, f, indent=2)
        _os.rename(str(tmp_path), str(sessions_dir / f"{session_id}.json"))

    except (PermissionError, OSError):
        pass  # Silently fail — non-critical
