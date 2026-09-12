"""Push this checkout's git-tracked files to a transport's remote checkout.

Design: docs/superpowers/specs/2026-09-09-transport-project-sync-design.md.

The manifest is always `git ls-files` (or a diff against it) on THIS host --
never anything a caller or a remote message supplies. That is the load-
bearing security property: a request can trigger *that* a sync happens, but
never *what* gets synced. Transfer rides tunnel_manager's already-live SSH
connection via SFTP; no new connection, no new credential.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger("wc.transport_sync")

# This file lives at the repo root alongside db.py/app.py -- the same
# anchor transport_readiness.py and friends assume implicitly by running
# from the project's own working directory.
_REPO_ROOT = Path(__file__).resolve().parent


@dataclass
class SyncPlan:
    to_push: list[str]     # relative paths to put
    to_delete: list[str]   # relative paths to remove
    head_sha: str          # what last_synced_sha becomes on full success


class SyncError(Exception):
    """A sync could not be computed or applied. .reason is user-facing."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _run_git_bytes(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(_REPO_ROOT), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {err.decode('utf-8', 'replace').strip()}")
    return out


async def _run_git(*args: str) -> str:
    return (await _run_git_bytes(*args)).decode("utf-8", "replace")


async def _committed_bytes(sha: str, rel_path: str) -> bytes:
    """The file's content *at that commit*, never from the working tree.

    `git show <sha>:<path>` rather than reading the path off disk. The
    manifest was always commit-derived -- that is this module's stated
    security property -- but the bytes were not, so a sync shipped whatever
    happened to be saved in a checkout six sessions edit concurrently, and
    then advanced last_synced_sha to head_sha, recording that the transport
    matched a commit it may never have received.

    Read as bytes and never decoded: the tree contains PNGs and other binary
    files, and a decode round trip would corrupt them.
    """
    return await _run_git_bytes("show", f"{sha}:{rel_path}")


def _safe_relative(path: str) -> str:
    """Reject anything that would escape the remote root.

    Defense in depth: git does not produce '..' or absolute paths for
    tracked files, but a path is never trusted to stay well-behaved just
    because of where it came from.
    """
    normalised = path.strip()
    if not normalised or normalised.startswith("/") or ".." in Path(normalised).parts:
        raise SyncError(f"unsafe path from git output: {path!r}")
    return normalised


async def _gitlink_paths() -> set[str]:
    """Tracked entries that are gitlinks (mode 160000) -- submodule-style
    references to another repo, not a regular file. Found by testing: this
    tree has one (.claude/worktrees/db-modularize, a committed worktree),
    and sftp.put() on a gitlink's path tries to open a directory as a file
    and fails. A flat file copy cannot meaningfully sync a separate repo
    anyway, so these are excluded rather than mishandled."""
    raw = await _run_git("ls-files", "-s")
    links = set()
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].split()[0] == "160000":
            links.add(parts[1].strip())
    return links


async def compute_plan(last_synced_sha: str) -> SyncPlan:
    """What would a sync push/delete right now, against *last_synced_sha*.

    Empty last_synced_sha (first sync for this transport) means a full
    manifest -- every tracked file is a push, nothing to delete.
    """
    head_sha = (await _run_git("rev-parse", "HEAD")).strip()

    # Checked before computing gitlinks: the common repeat-sync case where
    # nothing changed shouldn't pay for an O(repo size) ls-files -s call it
    # will never use.
    if last_synced_sha == head_sha:
        return SyncPlan(to_push=[], to_delete=[], head_sha=head_sha)

    gitlinks = await _gitlink_paths()

    if not last_synced_sha:
        raw = await _run_git("ls-files")
        files = [
            _safe_relative(line) for line in raw.splitlines()
            if line.strip() and line.strip() not in gitlinks
        ]
        return SyncPlan(to_push=files, to_delete=[], head_sha=head_sha)

    raw = await _run_git("diff", "--name-status", last_synced_sha, "HEAD")
    to_push: list[str] = []
    to_delete: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            # Rename: parts = [R100, old_path, new_path]. Simplest correct
            # handling -- remove the old path, push the new one -- rather
            # than a smarter single remote rename; renames are rare enough
            # in this repo that the complexity isn't worth it.
            if len(parts) >= 3:
                old_path, new_path = parts[1].strip(), parts[2].strip()
                if old_path not in gitlinks:
                    to_delete.append(_safe_relative(old_path))
                if new_path not in gitlinks:
                    to_push.append(_safe_relative(new_path))
            continue
        if len(parts) < 2:
            continue
        raw_path = parts[1].strip()
        if raw_path in gitlinks:
            continue
        path = _safe_relative(raw_path)
        if status.startswith("D"):
            to_delete.append(path)
        else:  # A, M, C (copy) -- all land as a push
            to_push.append(path)

    return SyncPlan(to_push=to_push, to_delete=to_delete, head_sha=head_sha)


async def apply_plan(
    machine_id: str, remote_root: str, plan: SyncPlan,
) -> int:
    """Push/delete every file in *plan* over the transport's live SFTP
    session. Returns the number of files changed. Raises SyncError on any
    failure -- callers must not advance last_synced_sha when this raises,
    or an unsynced diff is silently lost (see the design doc's 'no silent
    pointer advancement' rule)."""
    import tunnel_manager_ssh

    try:
        sftp = await tunnel_manager_ssh.open_sftp(machine_id)
    except RuntimeError as exc:
        raise SyncError(str(exc)) from exc

    changed = 0
    try:
        def _do_push(rel_path: str, blob: bytes) -> None:
            remote_path = f"{remote_root.rstrip('/')}/{rel_path}"
            remote_dir = remote_path.rsplit("/", 1)[0]
            _sftp_makedirs_sync(sftp, remote_dir)
            # putfo, not put: the bytes come from the commit (see
            # _committed_bytes), so there is no local path to read. A file
            # deleted or half-saved in the working tree no longer aborts a
            # sync partway through either.
            sftp.putfo(io.BytesIO(blob), remote_path)

        def _do_delete(rel_path: str) -> None:
            remote_path = f"{remote_root.rstrip('/')}/{rel_path}"
            try:
                sftp.remove(remote_path)
            except FileNotFoundError:
                pass  # already gone -- deleting it is still the goal met

        for rel_path in plan.to_push:
            blob = await _committed_bytes(plan.head_sha, rel_path)
            await asyncio.to_thread(_do_push, rel_path, blob)
            changed += 1
        for rel_path in plan.to_delete:
            await asyncio.to_thread(_do_delete, rel_path)
            changed += 1
    except OSError as exc:
        raise SyncError(f"transfer failed after {changed} file(s): {exc}") from exc
    finally:
        sftp.close()

    return changed


def _sftp_makedirs_sync(sftp: Any, remote_dir: str) -> None:
    """Synchronous mkdir -p over SFTP -- runs inside asyncio.to_thread.

    Found by testing against a real host: dropping the leading '/' on an
    absolute path (an earlier version did, via `current or part`) makes
    every stat/mkdir land relative to the SFTP session's own default
    directory instead of the intended absolute one -- it does not raise,
    it just silently creates nothing where you asked and possibly
    something unrelated elsewhere.
    """
    if not remote_dir or remote_dir in (".", "/"):
        return
    current = "/" if remote_dir.startswith("/") else ""
    for part in remote_dir.split("/"):
        if not part:
            continue
        current = current + part if current.endswith("/") else current + "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current)
            except OSError:
                pass


async def sync_transport(
    machine_id: str, remote_path: str, last_synced_sha: str,
) -> dict[str, Any]:
    """Compute and apply a sync. Returns {"ok", "files_changed", "reason",
    "head_sha"}. head_sha is only meaningful when ok is True -- callers
    advance ssh_transports.last_synced_sha to it only then."""
    try:
        plan = await compute_plan(last_synced_sha)
        if not plan.to_push and not plan.to_delete:
            return {
                "ok": True, "files_changed": 0,
                "reason": "already up to date", "head_sha": plan.head_sha,
            }
        changed = await apply_plan(machine_id, remote_path, plan)
        return {
            "ok": True, "files_changed": changed, "reason": "", "head_sha": plan.head_sha,
        }
    except SyncError as exc:
        _log.warning("transport_sync failed machine=%s: %s", machine_id, exc.reason)
        return {"ok": False, "files_changed": 0, "reason": exc.reason, "head_sha": ""}
