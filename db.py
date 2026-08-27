# db.py — SQLite bootstrap and queries for WebConsole.
#
# Single SQLite file, accessed via aiodlite (async). Schema created automatically on boot.
# All paths are resolved and validated against PROJECTS_ROOT before any filesystem op.
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Final

import aiosqlite

import config


db_conn: aiosqlite.Connection | None = None
_messages_batch_lock: asyncio.Lock | None = None


async def init() -> None:
    """Create the database and tables. Idempotent."""
    global db_conn
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
            deleted_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT NOT NULL REFERENCES chats(id),
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

        CREATE TABLE IF NOT EXISTS users (
            id       TEXT PRIMARY KEY,
            email    TEXT,
            name     TEXT NOT NULL,
            password TEXT NOT NULL,
            role     TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL DEFAULT ''
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
    }
    for name, sql in migrations.items():
        if name not in columns:
            await db_conn.execute(sql)


async def close() -> None:
    global db_conn
    if db_conn:
        await db_conn.close()
        db_conn = None


# ── Chat CRUD ──────────────────────────────────────────────────────────────────────────


_CHAT_COLUMNS = (
    "id, title, description, session_id, work_dir, owner_id, created_at, "
    "updated_at, archived, pinned, pinned_at, deleted_at"
)
_ALLOWED_CHAT_FIELDS = {"title", "description", "archived", "pinned", "pinned_at"}


async def chat_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        f"SELECT {_CHAT_COLUMNS} FROM chats WHERE owner_id = ? AND deleted_at IS NULL "  # nosec B608: columns are static
        "ORDER BY archived ASC, "
        "CASE WHEN archived = 0 THEN pinned ELSE 0 END DESC, "
        "CASE WHEN archived = 0 AND pinned = 1 THEN pinned_at END DESC, "
        "updated_at DESC",
        (owner_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def chat_get(
    chat_id: str, owner_id: str, include_archived: bool = False,
) -> dict[str, Any] | None:
    archived_filter = "" if include_archived else " AND archived = 0"
    cur = await db_conn.execute(
        f"SELECT {_CHAT_COLUMNS} FROM chats "
        f"WHERE id = ? AND owner_id = ? AND deleted_at IS NULL{archived_filter}",  # nosec B608: filter is static
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
    sql = "UPDATE chats SET " + sets + " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"  # nosec B608: fields are allowlisted
    vals = list(fields.values()) + [_now(), chat_id, owner_id]
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
    return cur.rowcount > 0


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
    try:
        await db_conn.execute("BEGIN")
        await db_conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db_conn.execute(
            "DELETE FROM chats WHERE id = ? AND owner_id = ?", (chat_id, owner_id),
        )
        await db_conn.commit()
    except Exception:
        await db_conn.rollback()
        raise
    return True


async def chat_set_session(chat_id: str, session_id: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET session_id = ?, updated_at = ? WHERE id = ?",
        (session_id, _now(), chat_id),
    )
    await db_conn.commit()


async def chat_set_title(chat_id: str, title: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), chat_id),
    )
    await db_conn.commit()


# ── Messages ───────────────────────────────────────────────────────────────────────────


async def messages_get(chat_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def messages_append(chat_id: str, role: str, content: str) -> int:
    cur = await db_conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, _now()),
    )
    await db_conn.commit()
    return cur.lastrowid


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
        return ids


# ── Users ──────────────────────────────────────────────────────────────────────────────


async def user_get_by_name(name: str) -> dict[str, Any] | None:
    cur = await db_conn.execute(
        "SELECT id, email, name, password, role, created_at FROM users WHERE name = ?",
        (name,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def user_create(name: str, email: str | None, password: str, role: str = "admin") -> None:
    user_id = uuid.uuid4().hex
    now = _now()
    await db_conn.execute(
        "INSERT INTO users (id, name, email, password, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name, email, password, role, now),
    )
    await db_conn.commit()


# ── Helpers ─────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug_from_title(title: str) -> str:
    """Derive a slug from a chat title: lowercase, alphanumeric-hyphens, max 40 chars."""
    import re
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())
    slug = "-".join(p for p in slug.split("-") if p)
    return slug[:40] or "untitled"


def slug_pattern(slug: str) -> str | None:
    """Validate slug characters: [a-z0-9-], 3-40 chars.

    Returns the slug if valid, None otherwise.
    Slugs must not start or end with a dash.
    """
    import re
    if not re.fullmatch(r"[a-z0-9-]{3,40}", slug):
        return None
    if slug.startswith("-") or slug.endswith("-"):
        return None
    return slug


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
            with open(fpath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Skip the active CLI process, but retain WebConsole-created session files.
        # WebConsole writes these files with its own PID so the CLI can discover
        # them; filtering by PID alone would hide the bidirectional-sync record.
        pid = data.get("pid")
        if pid and int(pid) == current_pid and data.get("entrypoint") != "webconsole":
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

        sessions.append({
            "id": session_id if session_id else fpath.stem,
            "name": name or "Untitled",
            "cwd": data.get("cwd", ""),
            "kind": kind,
            "startedAt": started_at,
            "updatedAt": updated_at,
            "sessionId": session_id,
        })

    return sessions


def _format_timestamp(ts: int | float | str | None) -> str:
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


def write_claude_session_file(session_id: str, name: str, cwd: str) -> None:
    """Write a session file to ~/.claude/sessions/<session_id>.json so CLI can pick it up.

    This enables bidirectional sync: Web sessions become visible to CLI.
    Uses atomic write (write to temp then rename) to avoid partial reads.
    """
    import os as _os

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
        }

        # Atomic write: write to temp, rename
        tmp_path = sessions_dir / f".tmp_{session_id}.json"
        with open(tmp_path, "w") as f:
            json.dump(session_data, f, indent=2)
        _os.rename(str(tmp_path), str(sessions_dir / f"{session_id}.json"))

    except (PermissionError, OSError):
        pass  # Silently fail — non-critical