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
            host          TEXT NOT NULL,
            port          INTEGER NOT NULL DEFAULT 9000,
            api_key       TEXT,
            -- Keep in step with config.MODEL_NAME. A retired model id here
            -- makes every turn fail on a fresh database.
            model         TEXT NOT NULL DEFAULT 'claude-sonnet-5',
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
    """)
    await _ensure_chat_columns()
    await db_conn.commit()


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


def _fts_index_ids_sync(msg_ids: Sequence[int]) -> None:
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
    except Exception:  # noqa: BLE001 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


def _fts_forget_ids_sync(msg_ids: Sequence[int]) -> None:
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
    except Exception:  # noqa: BLE001 -- FTS5 may not exist, silent fail
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
    except Exception:  # noqa: BLE001 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


# ── Async wrappers: keep the blocking sqlite3 work off the event loop ──────────


async def _fts_index_ids(msg_ids: Sequence[int]) -> None:
    await asyncio.to_thread(_fts_index_ids_sync, list(msg_ids))


async def _fts_forget_ids(msg_ids: Sequence[int]) -> None:
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
        "SELECT id, name, host, port, model, base_url, description, active "
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machines_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        "SELECT id, name, host, port, model, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        "created_at, updated_at "
        "FROM ai_machines WHERE owner_id = ? ORDER BY active DESC, name ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def ai_machine_get(id: str, owner_id: str) -> dict[str, Any] | None:
    cur = await db_conn.execute(
        "SELECT id, name, host, port, model, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
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
) -> str:
    now = _now()
    await db_conn.execute(
        "INSERT INTO ai_machines "
        "(id, name, host, port, api_key, model, base_url, description, active, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            machine_id,
            name,
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
) -> bool:
    pairs: list[tuple[str, Any]] = [
        ("name", name),
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


async def db_restore(data: bytes) -> bool:
    """Replace the current database with the provided gzip-compressed data.

    The database is replaced atomically: write to a temp file, then rename.
    The connection is re-opened after the swap.
    """
    import gzip as _gzip

    try:
        decompressed = _gzip.decompress(data)
    except Exception:  # noqa: BLE001 -- silently reject bad input
        return False

    if not decompressed:
        return False

    db_path = Path(config.DB_PATH)
    tmp_path = Path(config.DB_PATH).with_suffix(".restore.tmp")

    try:
        # Write compressed data to temp file, then replace.
        tmp_path.write_bytes(decompressed)

        # Close existing connection before swapping files.
        await close()

        # Atomic rename.
        tmp_path.rename(db_path)

        # Re-open the database.
        global db_conn
        db_conn = await aiosqlite.connect(str(db_path))
        db_conn.row_factory = aiosqlite.Row
        await db_conn.execute("PRAGMA journal_mode=WAL")
        await db_conn.execute("PRAGMA foreign_keys=ON")

        return True
    except Exception:  # noqa: BLE001 -- recover best-effort on failure
        try:
            tmp_path.unlink()
        except OSError:
            pass
        # Re-open original DB if possible.
        try:
            db_conn = await aiosqlite.connect(str(db_path))
            db_conn.row_factory = aiosqlite.Row
            await db_conn.execute("PRAGMA journal_mode=WAL")
            await db_conn.execute("PRAGMA foreign_keys=ON")
        except Exception:  # noqa: BLE001 -- final fallback, connection may be broken
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
            }
        )

    return sessions


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
