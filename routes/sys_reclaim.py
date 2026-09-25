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
* **Only entries belonging to a named family in ``_FAMILIES``.** This is the
  newest rule and the one that decides what the panel is for. The first
  version offered every entry that passed the checks above, which on this
  host was 452 of them totalling 170.8 MB -- and 109.4 MB of that sat in 39
  files from a single family, while ~370 were sub-megabyte logs and diffs.
  Nobody can judge 452 rows, and the operator who tried said so: *"I'm seeing
  a lot of files and I won't be able to know if I can delete any, I don't
  have context."* That was the right complaint. The checks above had already
  made the judgment; the list was asking the reader to make it again, without
  the information the scan had. So an entry now has to be something this
  service can *name* -- its own test and scan litter -- and everything else
  is reported as left alone rather than offered as a decision.

``execute`` re-derives the whole preview and accepts only paths that appear
in it, so a stale page cannot delete something the scan never offered.

``sweep`` is the same deletion with nobody watching, run on a timer from
``app.lifespan``. It is safe to run unattended for exactly one reason: the
named families are files this service created, so no judgment is delegated
to it that the preview rules do not already make.
"""
from __future__ import annotations

import asyncio
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

# The families this service can name, in the order the panel lists them.
#
# A family is a claim: "this service made these, and it knows what they are
# for". That claim is what makes both the grouped panel and the unattended
# sweep safe, so adding a pattern here is a real decision -- it must match
# something *this* codebase creates, never a name that merely looks like
# scratch. Anything unmatched is reported as left alone; it is not a
# candidate, which is the whole point of the list existing.
#
# Ordered most-specific first: `_family` takes the first match, and
# `dast_wc_<pid>.db-wal` must land in the database family rather than in a
# generic `wc` one.
_FAMILIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "dast_db",
        "Security-scan databases",
        re.compile(r"^dast_wc_\d+\.db(?:-wal|-shm)?$"),
    ),
    (
        "dast_projects",
        "Security-scan project directories",
        re.compile(r"^dast_projects_\d+$"),
    ),
    (
        "pytest",
        "pytest temporary directories",
        re.compile(r"^pytest-of-"),
    ),
    (
        "wc_scratch",
        "Test-run and validation scratch",
        re.compile(r"^(?:wc[-_]|wcval|wcg|webconsole[-_.])"),
    ),
)

_FAMILY_LABELS = {fid: label for fid, label, _ in _FAMILIES}


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


def _family(name: str) -> str | None:
    """The family *name* belongs to, or None if this service cannot name it."""
    for family_id, _label, pattern in _FAMILIES:
        if pattern.match(name):
            return family_id
    return None


def preview(min_age_s: int | None = None) -> dict:
    """Return what could be deleted without deleting anything.

    ``{"families": [...], "entries": [...], "skipped": [...], "roots": [...],
       "total_mb": N, "min_age_s": N, "mem": {...}, "last_sweep": {...}}``

    ``families`` is what the panel renders: one row per named family, with
    its label, count, size and member paths. ``entries`` is the same set
    flattened, kept because ``execute`` validates against it and because the
    panel offers the file-by-file detail behind a disclosure -- available to
    anyone who wants it, in front of nobody who does not.

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
            # Last, because it is the cheapest check and the least alarming
            # skip: an unnamed entry is not a problem, it is simply not this
            # service's to delete.
            family_id = _family(name)
            if family_id is None:
                skipped.append({
                    "path": path,
                    "reason": "not one of this service's own scratch families",
                })
                continue
            entries.append({
                "path": path,
                "name": name,
                "root": root,
                "family": family_id,
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "age_s": int(age_s),
                "is_dir": os.path.isdir(path),
            })

    entries.sort(key=lambda row: -row["size_mb"])
    families = []
    for family_id, label, _pattern in _FAMILIES:
        members = [row for row in entries if row["family"] == family_id]
        if not members:
            continue
        families.append({
            "id": family_id,
            "label": label,
            "count": len(members),
            "size_mb": round(sum(row["size_mb"] for row in members), 1),
            # The oldest member, because "untouched for N" is the reassurance
            # that matters and the newest member is the one that could still
            # be argued about.
            "age_s": min(row["age_s"] for row in members),
            "paths": [row["path"] for row in members],
            "entries": members,
        })
    return {
        "families": families,
        "entries": entries,
        "skipped": skipped,
        "roots": roots,
        "total_mb": round(sum(row["size_mb"] for row in entries), 1),
        "min_age_s": age_floor,
        "mem": _mem_snapshot(),
        "last_sweep": dict(_last_sweep) if _last_sweep else None,
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


# ── The unattended sweep ──────────────────────────────────────────────────────
#
# Same deletion, nobody watching. It is only safe because of the family rule:
# every path it can reach is one this service created and can name, so the
# sweep makes no judgment the preview rules have not already made.
#
# It deliberately does not persist its result. The panel shows the last sweep
# so the timer is visible rather than silent, and an in-memory record that
# resets on restart is the honest version of that -- a stored row would
# outlive the process that wrote it and claim a sweep happened in a release
# that may no longer be running.

_last_sweep: dict | None = None
_sweep_task = None


def last_sweep() -> dict | None:
    return dict(_last_sweep) if _last_sweep else None


def sweep() -> dict:
    """Delete every currently-reclaimable path. Records the result."""
    global _last_sweep
    current = preview()
    result = execute([row["path"] for row in current["entries"]])
    _last_sweep = {
        "at": time.time(),
        "deleted": len(result["deleted"]),
        "freed_mb": result["freed_mb"],
        "failed": len(result["failed"]),
    }
    _log.info(
        "reclaim sweep: deleted %d paths, %.1f MB",
        _last_sweep["deleted"], _last_sweep["freed_mb"],
    )
    return result


async def _sweep_loop(interval_s: int, delay_s: int) -> None:
    await asyncio.sleep(delay_s)
    while True:
        try:
            # to_thread, not inline: the scan reads every PID's descriptors
            # and walks whole directory trees, and the event loop is serving
            # other people's turns while it does.
            await asyncio.to_thread(sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("reclaim sweep failed")
        await asyncio.sleep(interval_s)


def start_sweeper(interval_s: int | None = None, delay_s: int | None = None) -> None:
    """Begin the periodic sweep. Idempotent, and a no-op when disabled."""
    global _sweep_task
    if not config.RECLAIM_SWEEP_ENABLED:
        return
    if _sweep_task and not _sweep_task.done():
        return  # a second lifespan must not double the sweep rate
    every = config.RECLAIM_SWEEP_INTERVAL_S if interval_s is None else interval_s
    wait = config.RECLAIM_SWEEP_DELAY_S if delay_s is None else delay_s
    _sweep_task = asyncio.create_task(_sweep_loop(every, wait))


async def stop_sweeper() -> None:
    """Cancel the sweep. Safe when it was never started."""
    global _sweep_task
    if not _sweep_task:
        return
    _sweep_task.cancel()
    try:
        await _sweep_task
    except asyncio.CancelledError:
        pass
    except Exception:
        _log.exception("reclaim sweep failed during shutdown")
    _sweep_task = None
