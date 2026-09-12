# db_sessions.py — Claude Code session files and transcript-backed model discovery.
#
# Extracted from db.py so that the session route layer (``routes/misc.py``) can
# import session helpers without pulling the whole database module.

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

_log = logging.getLogger(__name__)

# ── In-memory TTL cache for remote session data ────────────────────────────
# Populated by a background loop every 3 s; read_claude_sessions() serves
# the cache when it is <4 s old, avoiding SSH on every request.

_CACHE_TTL: Final[float] = 4.0  # seconds before cache is stale
_CACHE_REFRESH: Final[float] = 3.0  # background loop interval
_sessions_cache: list[dict[str, Any]] | None = None
_cache_timestamp: float = 0.0
_cache_lock = threading.Lock()

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
                    chunk = chunk[newline + 1 :] if newline >= 0 else b""
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


def _read_remote_sessions_sync() -> list[dict[str, Any]]:
    """Read sessions from all SSH transports via direct SSH.

    Reads ``~/.claude/sessions/*.json`` on each remote host and returns
    them in the same format as local sessions.  Best-effort: failures
    on individual hosts don't block the others.
    """
    sessions: list[dict[str, Any]] = []
    import sqlite3 as _sqlite3
    import config
    try:
        conn = _sqlite3.connect(os.environ.get("WC_DB_PATH", config.DB_PATH))
        conn.row_factory = _sqlite3.Row
        rows = list(conn.execute(
            "SELECT t.ssh_host, t.ssh_user, t.ssh_key_path, id "
            "FROM ssh_transports t"
        ).fetchall())
        conn.close()
    except Exception:
        return []

    for row in rows:
        ssh_host = row["ssh_host"]
        ssh_user = row["ssh_user"]
        ssh_key_path = row["ssh_key_path"]
        if not ssh_host or not ssh_key_path:
            continue

        remote_hostname = ssh_host.split(":")[0]  # strip port if present

        try:
            import paramiko
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh_client.connect(
                ssh_host,
                username=ssh_user,
                key_filename=os.path.expanduser(ssh_key_path),
                timeout=10,
            )

            try:
                _, stdout, _ = ssh_client.exec_command(
                    "ls ~/.claude/sessions/*.json 2>/dev/null",
                    timeout=5,
                )
                file_list = stdout.read().decode("utf-8", errors="replace").strip()
                if file_list:
                    for fpath in file_list.splitlines():
                        fpath = fpath.strip()
                        if not fpath or fpath.endswith(".key"):
                            continue
                        try:
                            _, content, _ = ssh_client.exec_command(
                                f"cat '{fpath}' 2>/dev/null",
                                timeout=5,
                            )
                            raw = content.read().decode("utf-8", errors="replace")
                            if not raw:
                                continue
                            data = json.loads(raw)
                            session = _remote_session_parse(data, remote_hostname)
                            if session:
                                sessions.append(session)
                        except Exception:
                            continue
            finally:
                try:
                    ssh_client.close()
                except Exception:
                    pass

        except Exception:
            continue

    return sessions


def _remote_session_parse(data: dict, remote_hostname: str) -> dict[str, Any] | None:
    """Build a session dict from a remote host's session JSON."""
    try:
        pid = data.get("pid")
        try:
            pid_int = int(pid) if pid else 0
        except (TypeError, ValueError):
            return None

        if pid_int == 0:
            return None

        started_at = _format_timestamp(data.get("startedAt"))
        updated_at = _format_timestamp(data.get("updatedAt"))
        if not started_at:
            started_at = ""
        if not updated_at:
            updated_at = ""

        name = data.get("name") or ""
        session_id = data.get("sessionId", "")
        kind = data.get("kind", "")
        status = data.get("status") or "exited"

        return {
            "id": session_id if session_id else f"{remote_hostname}:{pid_int}",
            "name": name or "Untitled",
            "cwd": data.get("cwd", ""),
            "kind": kind,
            "startedAt": started_at,
            "updatedAt": updated_at,
            "sessionId": session_id,
            "model": data.get("model", ""),
            "entrypoint": data.get("entrypoint", ""),
            "status": status,
            "status_updated_at": _format_timestamp(data.get("statusUpdatedAt")) or "",
            "live": status != "exited",
            "file": f"{remote_hostname}:{pid_int}",
            "remote_hostname": remote_hostname,
            "remote_pid": pid_int,
        }
    except Exception:
        return None


async def update_sessions_cache() -> None:
    """Refresh the in-memory cache by reading remote sessions in parallel.

    Uses ``asyncio.gather`` to fan out SSH connections so all transports
    are queried concurrently rather than one after the other.
    """
    try:
        import sqlite3 as _sqlite3
        import config

        def _read_transports() -> list[Any]:
            conn = _sqlite3.connect(os.environ.get("WC_DB_PATH", config.DB_PATH))
            conn.row_factory = _sqlite3.Row
            try:
                return list(conn.execute(
                    "SELECT t.ssh_host, t.ssh_user, t.ssh_key_path, id "
                    "FROM ssh_transports t"
                ).fetchall())
            finally:
                conn.close()

        try:
            # Also off the loop: a synchronous sqlite3 open and query, on a
            # 78 MB database, on every refresh.
            rows = await asyncio.to_thread(_read_transports)
        except Exception:
            _log.warning("update_sessions_cache: failed to read transports")
            return

        if not rows:
            _log.info("update_sessions_cache: no transports found")
            return

        def _read_one_host_blocking(
            host: str, user: str, key_path: str
        ) -> list[dict[str, Any]]:
            """Every call in here blocks: paramiko's connect, and one
            exec_command plus read per session file. It is a plain `def` to
            say so."""
            import paramiko

            remote_hostname = host.split(":")[0]
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    host,
                    username=user,
                    key_filename=os.path.expanduser(key_path),
                    timeout=10,
                )
                try:
                    _, stdout, _ = ssh.exec_command(
                        "ls ~/.claude/sessions/*.json 2>/dev/null", timeout=5
                    )
                    file_list = stdout.read().decode("utf-8", errors="replace").strip()
                    if not file_list:
                        return []
                    results: list[dict[str, Any]] = []
                    for fpath in file_list.splitlines():
                        fpath = fpath.strip()
                        if not fpath or fpath.endswith(".key"):
                            continue
                        try:
                            _, content, _ = ssh.exec_command(
                                f"cat '{fpath}' 2>/dev/null", timeout=5
                            )
                            raw = content.read().decode("utf-8", errors="replace")
                            if not raw:
                                continue
                            data = json.loads(raw)
                            session = _remote_session_parse(data, remote_hostname)
                            if session:
                                results.append(session)
                        except Exception:
                            continue
                    return results
                finally:
                    try:
                        ssh.close()
                    except Exception:
                        pass
            except Exception:
                return []

        # to_thread, not a bare call. The docstring above claimed gather made
        # these concurrent, and a comment below claimed the remote read "goes
        # through a thread" -- neither was true. The bodies are synchronous
        # paramiko, so gather ran them one after another *on the event loop*:
        # four SSH handshakes plus a `cat` per session file, every refresh,
        # while the loop was meant to be serving HTTP. Measured 2026-09-10 with
        # 36 sessions across four transports: /login took 75 seconds.
        async def _read_one_host(
            host: str, user: str, key_path: str
        ) -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                _read_one_host_blocking, host, user, key_path)

        coros = [
            _read_one_host(
                row["ssh_host"], row["ssh_user"], row["ssh_key_path"]
            )
            for row in rows
            if row["ssh_host"] and row["ssh_key_path"]
        ]
        if coros:
            results = await asyncio.gather(*coros)
            merged: list[dict[str, Any]] = []
            for chunk in results:
                merged.extend(chunk)

            # Also merge local sessions, off the event loop: this globs the
            # sessions directory, reads every JSON file, and runs a SQLite
            # lookup plus a PID check per session. All blocking, and the loop
            # is serving every other session while it runs -- the same reason
            # the remote read above now goes through a thread, which this
            # comment previously asserted it already did.
            local = await asyncio.to_thread(_read_claude_sessions_sync)
            merged.extend(local)
            # Deduped once, here. An earlier _dedupe_sessions(merged) sat above
            # the local merge and its result was discarded, so it was a full
            # pass over the remote list for nothing.
            final = _dedupe_sessions(merged)

            with _cache_lock:
                global _sessions_cache, _cache_timestamp  # noqa: PLW0603
                _sessions_cache = final
                _cache_timestamp = time.time()
            _log.info("update_sessions_cache: refreshed with %d sessions", len(final))
        else:
            _log.info("update_sessions_cache: no valid hosts to refresh")

    except Exception:
        _log.exception("update_sessions_cache: refresh failed")


async def read_claude_sessions() -> list[dict[str, Any]]:
    """Read CLI sessions from both local host and connected SSH transports.

    Serves cached sessions when available and fresh (less than 4 s old),
    avoiding SSH on most requests.
    """
    # ── cache check ──────────────────────────────────────────────────
    with _cache_lock:
        cached = _sessions_cache
        if cached is not None and (time.time() - _cache_timestamp) < _CACHE_TTL:
            return list(cached)  # defensive copy

    # Cache miss or stale — read local sessions.  Remote discovery is optional
    # (env var WC_REMOTE_SESSIONS=1 / config.py::REMOTE_SESSIONS); it is off by
    # default so that tests which mock a local sessions dir do not reach the
    # real network.
    sessions: list[dict[str, Any]] = await asyncio.to_thread(
        _read_claude_sessions_sync)
    import config as _config  # noqa: local import to keep the flag lazy
    if _config.REMOTE_SESSIONS:
        try:
            remote = await asyncio.to_thread(_read_remote_sessions_sync)
            sessions.extend(remote)
        except Exception:
            pass  # Remote discovery is best-effort

    return _dedupe_sessions(sessions)


def read_claude_sessions_sync() -> list[dict[str, Any]]:
    """Synchronous version of read_claude_sessions for callers that cannot await.

    Serves cached sessions when fresh; falls back to full SSH when stale or
    unavailable.
    """
    with _cache_lock:
        cached = _sessions_cache
        if cached is not None and (time.time() - _cache_timestamp) < _CACHE_TTL:
            return list(cached)

    # Remote discovery is best-effort and blocks the calling thread; gate it
    # behind the same flag so a sync caller does not reach the network unless
    # explicitly enabled.
    import config as _config
    remote: list[dict[str, Any]] = []
    if _config.REMOTE_SESSIONS:
        remote = _read_remote_sessions_sync()
    return _dedupe_sessions(_read_claude_sessions_sync() + remote)


def _read_claude_sessions_sync() -> list[dict[str, Any]]:
    """Synchronous version of read_claude_sessions — local sessions only."""
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
            content = fpath.read_text()
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
            model = db._lookup_session_model(session_id) or ""

        # A process that has exited has no *current* status, only the last one
        # it happened to write, and this is the one place that knows which of
        # the two it is -- so the rule belongs here rather than in each reader.
        #
        # Three readers took the stale value as current: classification's
        # `_cli_maps` (so a session that died while `waiting` asked for a
        # person for ever, and cost a `prompts.has_prompt` subprocess per poll
        # on a dead pid), routes/chats.py's busy set (so its linked chat showed
        # as working for ever), and db_supervisor_map's `_cli_session_nodes`,
        # which passes the raw entry to `_classify_cli_session`. Fixing readers
        # one at a time is how CLAUDE.md's "this rule used to exist three
        # times" happens; this fixes the other two without touching them.
        #
        # `status_updated_at` is left alone on purpose: a timestamp does not
        # stop being true when the process dies, and chat-list.js:548 greys a
        # stale row with it, which is what an ended session should look like.
        running = db._pid_is_running(pid)
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
                "status": (data.get("status") or "") if running else "",
                "status_updated_at": _format_timestamp(data.get("statusUpdatedAt")) or "",
                "live": running,
                "file": fpath.name,
            }
        )

    return _dedupe_sessions(sessions)
