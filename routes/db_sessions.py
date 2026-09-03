# db_sessions.py — Claude Code session files and transcript-backed model discovery.
#
# Extracted from db.py so that the session route layer (``routes/misc.py``) can
# import session helpers without pulling the whole database module.

import asyncio
import json
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import db

# These resolve through db.__getattr__ so that test patches of db._CLAUDE_SESSIONS_DIR
# propagate into the imported functions.  A plain module-level Path() would read a
# different value after patch.object(db, "_CLAUDE_SESSIONS_DIR", ...).


def _resolve_sessions_dir() -> Path:
    """Return the sessions directory, overridable by patching db._CLAUDE_SESSIONS_DIR."""
    v = getattr(db, "_CLAUDE_SESSIONS_DIR", None)
    if v is not None:
        return Path(v)
    return Path.home() / ".claude" / "sessions"


def _resolve_projects_dir() -> Path:
    """Return the projects directory, overridable by patching db._CLAUDE_PROJECTS_DIR."""
    v = getattr(db, "_CLAUDE_PROJECTS_DIR", None)
    if v is not None:
        return Path(v)
    return Path.home() / ".claude" / "projects"


# ── Transcript-backed model discovery ───────────────────────────────────────────

_SESSION_ID_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TRANSCRIPT_TAIL_BYTES: Final[int] = 1 << 20
_model_cache: dict[str, str] = {}


def _session_transcript_paths(session_id: str) -> list[Path]:
    """Return the transcript files belonging to *session_id*."""
    if not _SESSION_ID_SAFE_RE.fullmatch(session_id):
        return []
    try:
        if not _resolve_projects_dir().is_dir():
            return []
        return sorted(_resolve_projects_dir().glob(f"*/{session_id}.jsonl"))
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
    """Return the last non-synthetic model recorded for *session_id*."""
    for fpath in _session_transcript_paths(session_id):
        try:
            size = fpath.stat().st_size
            with fpath.open("rb") as fh:
                if size > _TRANSCRIPT_TAIL_BYTES:
                    fh.seek(size - _TRANSCRIPT_TAIL_BYTES)
                    chunk = fh.read()
                    newline = chunk.find(b"\n")
                    chunk = chunk[newline + 1 :] if newline >= 0 else b""  # noqa: E203
                else:
                    chunk = fh.read()
            model = _model_from_lines(
                chunk.decode("utf-8", errors="replace").splitlines(), session_id
            )
            if model:
                return model
            if size > _TRANSCRIPT_TAIL_BYTES:
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


# ── Session file management ──────────────────────────────────────────────────────


def _pid_is_running(pid: Any) -> bool:
    """Return True if *pid* names a live process."""
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _dedupe_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse records that describe the same CLI session."""
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
        files = list(_resolve_sessions_dir().glob("*.json"))
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


def _format_timestamp(ts: float | str | None) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    if ts is None:
        return ""
    try:
        ts_num = int(ts)
        if ts_num > 1e12:
            ts_num = ts_num // 1000
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_num))
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


async def read_claude_sessions() -> list[dict[str, Any]]:
    """Read ~/.claude/sessions/*.json files and return a list of session dicts.

    Returns sessions that are:
    - Not the current session (by PID match)
    - Have a non-empty name or sessionId
    - Are interactive kind
    """
    sessions: list[dict[str, Any]] = []
    try:
        if not _resolve_sessions_dir().is_dir():
            return []
    except (PermissionError, OSError):
        return []

    current_pid = os.getpid()

    try:
        files = sorted(_resolve_sessions_dir().glob("*.json"))
    except (PermissionError, OSError):
        return []

    for fpath in files:
        try:
            content = await asyncio.to_thread(fpath.read_text)
            data = json.loads(content)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue

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
            model = await asyncio.to_thread(db._lookup_session_model, session_id) or ""

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
                "status": data.get("status") or "",
                "status_updated_at": _format_timestamp(data.get("statusUpdatedAt")) or "",
                "live": db._pid_is_running(pid),
                "file": fpath.name,
            }
        )

    return _dedupe_sessions(sessions)


def delete_claude_session_file(session_id: str) -> bool:
    """Remove the WebConsole shadow record for *session_id*."""
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Invalid session_id (contains path separators or ..)")

    path = _resolve_sessions_dir() / f"{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    if data.get("entrypoint") != "webconsole":
        raise ValueError("Refusing to delete a session file WebConsole did not write")

    if _session_is_live(session_id):
        raise ValueError("Refusing to delete a session file whose process is running")

    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def write_claude_session_file(
    session_id: str, name: str, cwd: str, model: str = ""
) -> None:
    """Write a session file to ~/.claude/sessions/<session_id>.json so CLI can pick it up."""
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Invalid session_id (contains path separators or ..)")

    try:
        sessions_dir = _resolve_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)

        now_ms = int(time.time() * 1000)
        session_data = {
            "pid": os.getpid(),
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": now_ms,
            "procStart": now_ms,
            "version": db.config.VERSION,
            "peerProtocol": "http",
            "peerFeatures": {
                "supports": ["text", "markdown"],
                "toolSupport": "complete",
            },
            "kind": "interactive",
            "entrypoint": "webconsole",
            "pidDomain": str(os.getpid()),
            "messagingSocketPath": "",
            "name": name,
            "nameSource": "webconsole",
            "nameSince": now_ms,
            "updatedAt": now_ms,
            "model": model,
        }

        tmp_path = sessions_dir / f".tmp_{session_id}.json"
        with open(tmp_path, "w") as f:
            json.dump(session_data, f, indent=2)
        os.rename(str(tmp_path), str(sessions_dir / f"{session_id}.json"))

    except (PermissionError, OSError):
        pass  # Silently fail — non-critical
