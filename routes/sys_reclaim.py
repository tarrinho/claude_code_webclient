"""Scratch-space reclaim for the Server settings panel.

Deletes stale throwaway files and directories from the host's tmpfs mounts.
On this host ``/tmp`` and ``/dev/shm`` are tmpfs, so their contents occupy
RAM (and swap once the kernel pages them out). Test runs, DAST scratch
databases and abandoned build directories accumulate there and are never
reclaimed by anything else -- 1.2 GB of swap came back from a hand cleanup
on 2026-09-22, which is why this exists as a button instead of a habit.

This module never touches a process. Process reclaim lives in
``sys_cleanup``; the two are deliberately separate buttons, because killing
another session's agent and deleting a stale file need different amounts of
thought from whoever is clicking.

The safety argument, in full, because "without impacting the service" is the
whole requirement:

* Only paths directly inside an allowed tmpfs root are ever considered, and
  the root must actually be a tmpfs mount. Nothing recurses out of it: the
  real path is re-derived and re-checked against the root, so a symlink
  pointing at the database cannot smuggle itself in.
* Only entries owned by this uid.
* Only regular files and directories. Sockets, FIFOs and devices are how
  running programs talk to each other (screen, ssh-agent, dbus) and are
  never candidates.
* Only entries whose newest mtime anywhere inside them is older than
  ``MIN_AGE_S``. A directory is as new as the newest thing in it, so an
  active scratch tree cannot look stale because its top level is old.
* Never an entry any live process holds open, has as its working directory,
  or has mapped. That is what keeps a peer's in-flight pytest run, the live
  service and every editor out of the list.
* Never a name matching ``_PROTECTED_RE`` -- the infrastructure sockets and
  private trees that satisfy every rule above and still must not go.

``execute`` re-derives the whole preview and accepts only paths that appear
in it, so a stale page cannot delete something the scan never offered.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

import config

_log = logging.getLogger("wc.app")

_PROC = "/proc"

# tmpfs mounts whose top level is scratch space. Both are checked against
# /proc/mounts before use: on a host where /tmp is a real disk, deleting
# from it frees no memory, and this panel would be claiming something untrue.
ALLOWED_ROOTS = ("/tmp", "/dev/shm")

# Two hours. Long enough that a suite which started before you opened the
# settings page is still protected by age alone, even if it currently holds
# no descriptor open (pytest closes and reopens its temp files freely).
MIN_AGE_S = 7200

# Names that pass every other test and must still never be deleted. Hidden
# entries are X11/ICE sockets and lock directories; the named prefixes are
# session multiplexers, agents and buses whose sockets live in tmpfs for the
# life of the machine.
_PROTECTED_RE = re.compile(
    r"^(?:\.|"
    r"systemd-private|snap-private|snap\.|"
    r"screen|uscreens|S-|tmux-|"
    r"ssh-|gpg-|dbus-|pulse|wayland-|"
    r"\.X11-unix|\.ICE-unix"
    r")"
)

# How entries are grouped in the panel. `webconsole` is this project's own
# scratch, which is the bulk of it and the safest to remove; `other` is
# everything else that qualifies, shown separately and unselected by default
# so nobody sweeps away a stranger's files without reading the list.
_WEBCONSOLE_RE = re.compile(r"^(?:wc|wcval|wcg|dast_wc_|pytest-of-|webconsole)")


def _own_uid() -> int:
    return os.getuid()


def _tmpfs_roots() -> list[str]:
    """ALLOWED_ROOTS that exist and are really tmpfs on this host."""
    mounted: set[str] = set()
    try:
        with open(f"{_PROC}/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in ("tmpfs", "ramfs"):
                    mounted.add(parts[1])
    except OSError:
        return []
    return [root for root in ALLOWED_ROOTS if root in mounted and os.path.isdir(root)]


def _protected_paths() -> set[str]:
    """Every real path a live process holds open, maps, or sits in.

    Descriptors, the working directory and the executable each count. Reading
    another uid's /proc entries fails with EACCES; those are skipped rather
    than raised, so the set is a lower bound for processes we cannot see --
    which is safe, because a path we cannot prove is free is still refused by
    the uid check below.
    """
    busy: set[str] = set()
    try:
        pids = [name for name in os.listdir(_PROC) if name.isdigit()]
    except OSError:
        return busy
    for pid in pids:
        base = f"{_PROC}/{pid}"
        for link in ("cwd", "exe"):
            try:
                busy.add(os.path.realpath(os.readlink(f"{base}/{link}")))
            except OSError:
                pass
        try:
            fds = os.listdir(f"{base}/fd")
        except OSError:
            fds = []
        for fd in fds:
            try:
                busy.add(os.path.realpath(os.readlink(f"{base}/fd/{fd}")))
            except OSError:
                pass
        # Memory maps catch a file that is mapped but no longer open by
        # descriptor -- a running binary's own tmpfs copy, for instance.
        try:
            with open(f"{base}/maps", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if " /" in line:
                        busy.add(os.path.realpath(line.split(" /", 1)[1].strip()))
        except OSError:
            pass
    return busy


def _never_delete() -> set[str]:
    """Paths this deployment owns, whatever else they satisfy.

    A test run can point ``WC_DB_PATH`` at tmpfs, and the release directory
    is configurable; both would otherwise be ordinary stale files.
    """
    keep: set[str] = set()
    for value in (config.DB_PATH, config.SESSION_DB_PATH):
        if value:
            keep.add(os.path.realpath(str(value)))
    return keep


def _tree_stats(path: str) -> tuple[int, float]:
    """(bytes, newest mtime) for a file or a whole directory tree."""
    try:
        stat = os.lstat(path)
    except OSError:
        return (0, 0.0)
    if not os.path.isdir(path) or os.path.islink(path):
        return (stat.st_size, stat.st_mtime)
    total = stat.st_size
    newest = stat.st_mtime
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in dirnames + filenames:
            try:
                sub = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += sub.st_size
            newest = max(newest, sub.st_mtime)
    return (total, newest)


def _mem_snapshot() -> dict[str, float]:
    """MemAvailable and SwapFree in MB, so the panel can show what changed."""
    values: dict[str, int] = {}
    try:
        with open(f"{_PROC}/meminfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0])
    except OSError:
        return {}
    return {
        "mem_available_mb": round(values.get("MemAvailable", 0) / 1024, 1),
        "swap_free_mb": round(values.get("SwapFree", 0) / 1024, 1),
    }


def _classify(name: str) -> str:
    return "webconsole" if _WEBCONSOLE_RE.match(name) else "other"


def preview(min_age_s: int | None = None) -> dict:
    """Return what could be deleted without deleting anything.

    ``{"entries": [...], "skipped": [...], "roots": [...],
       "total_mb": N, "counts": {...}, "min_age_s": N, "mem": {...}}``

    ``skipped`` carries a reason per rejected entry. It is not diagnostic
    noise: it is the evidence that the busy paths were seen and left alone,
    and it is what the panel shows when someone asks why their directory is
    not on the list.
    """
    age_floor = MIN_AGE_S if min_age_s is None else max(0, int(min_age_s))
    uid = _own_uid()
    now = time.time()
    busy = _protected_paths()
    keep = _never_delete()
    roots = _tmpfs_roots()

    entries: list[dict] = []
    skipped: list[dict] = []

    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError as exc:
            skipped.append({"path": root, "reason": f"unreadable: {exc}"})
            continue
        for name in names:
            path = os.path.join(root, name)
            real = os.path.realpath(path)
            if _PROTECTED_RE.match(name):
                continue  # infrastructure; not worth listing as a skip
            # A symlink whose target is outside the root, or any path whose
            # real location escapes it, is refused rather than followed.
            if real != path and not real.startswith(root + os.sep):
                skipped.append({"path": path, "reason": "points outside the root"})
                continue
            try:
                stat = os.lstat(path)
            except OSError:
                continue
            if stat.st_uid != uid:
                continue  # another user's; never ours to judge
            if not (os.path.isdir(path) or os.path.isfile(path)) or os.path.islink(path):
                skipped.append({"path": path, "reason": "socket, link or device"})
                continue
            if real in keep:
                skipped.append({"path": path, "reason": "this deployment's database"})
                continue
            size_bytes, newest = _tree_stats(path)
            age_s = max(0.0, now - newest)
            if age_s < age_floor:
                skipped.append({
                    "path": path,
                    "reason": f"changed {int(age_s / 60)} min ago",
                })
                continue
            held = [p for p in busy if p == real or p.startswith(real + os.sep)]
            if held:
                skipped.append({"path": path, "reason": "a running process is using it"})
                continue
            entries.append({
                "path": path,
                "name": name,
                "root": root,
                "kind": _classify(name),
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "age_s": int(age_s),
                "is_dir": os.path.isdir(path),
            })

    entries.sort(key=lambda row: -row["size_mb"])
    counts = {
        "webconsole": sum(1 for row in entries if row["kind"] == "webconsole"),
        "other": sum(1 for row in entries if row["kind"] == "other"),
    }
    return {
        "entries": entries,
        "skipped": skipped,
        "roots": roots,
        "counts": counts,
        "total_mb": round(sum(row["size_mb"] for row in entries), 1),
        "min_age_s": age_floor,
        "mem": _mem_snapshot(),
    }


def execute(paths: list[str]) -> dict:
    """Delete the named paths. Every one must be in a fresh preview.

    Returns ``{"deleted": [...], "failed": [...], "refused": [...],
    "freed_mb": N, "mem_before": {...}, "mem_after": {...}}``.

    Re-deriving the preview here is not belt-and-braces. It is the only
    check that runs against the state at deletion time: the page that
    listed a directory may have been open for an hour, and a suite may have
    claimed it since.
    """
    before = _mem_snapshot()
    current = preview()
    offered = {row["path"]: row for row in current["entries"]}

    deleted: list[dict] = []
    failed: list[dict] = []
    refused: list[str] = []
    freed_mb = 0.0

    for path in paths:
        row = offered.get(path)
        if row is None:
            refused.append(path)
            continue
        try:
            if row["is_dir"]:
                shutil.rmtree(path)
            else:
                Path(path).unlink()
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})
            continue
        freed_mb += row["size_mb"]
        deleted.append({"path": path, "size_mb": row["size_mb"]})

    if deleted:
        _log.info("reclaim: deleted %d paths, %.1f MB", len(deleted), freed_mb)
    return {
        "deleted": deleted,
        "failed": failed,
        "refused": refused,
        "freed_mb": round(freed_mb, 1),
        "mem_before": before,
        "mem_after": _mem_snapshot(),
    }
