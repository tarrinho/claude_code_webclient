"""Host resource sampling for the Server statistics page.

Pure stdlib: `/proc` plus `os.statvfs`. Deliberately no psutil, for two
reasons. §5 of rules.md requires every third-party import to appear in
`requirements.txt`, and adding a C-extension to a pinned dependency set buys
nothing here -- the four files this reads are the same four psutil reads. The
cost is that this module is Linux-only, which the deployment already is.

CPU percentage is a *delta* between two samples, so the first call after
import cannot produce one and returns 0.0. The sampler below takes its first
reading at startup precisely so the first reading a user sees is real.

Nothing in here raises. A host that does not expose a file returns partial
data rather than failing the page: a missing swap figure is worth less than
a Server tab that 500s because this box has no swap.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import os
import time
from typing import Any, Final

import config

_PROC = "/proc"

# Previous (total, idle) jiffies. Module state because a percentage over an
# interval needs the previous end of that interval.
_prev_cpu: tuple[int, int] | None = None
_prev_proc: tuple[float, float] | None = None  # (cpu_seconds, wall_clock)


def _read_cpu() -> tuple[int, int] | None:
    """Aggregate (total, idle) jiffies from the first line of /proc/stat."""
    try:
        with open(f"{_PROC}/stat") as handle:
            nums = [int(value) for value in handle.readline().split()[1:]]
        # idle + iowait: a process blocked on disk is not the CPU being busy.
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return sum(nums), idle
    except (OSError, ValueError, IndexError):
        return None


def _read_meminfo() -> dict[str, int]:
    """/proc/meminfo as bytes. Values there are kB regardless of the label."""
    out: dict[str, int] = {}
    try:
        with open(f"{_PROC}/meminfo") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    out[key.strip()] = int(parts[0]) * 1024
    except (OSError, ValueError):
        pass
    return out


def _disk(path: str) -> dict[str, Any]:
    """Usage of the filesystem holding *path*.

    `f_bavail` rather than `f_bfree`: the difference is the reserved blocks
    only root may use, and reporting those as free tells an operator they have
    space that their own service cannot allocate.
    """
    try:
        stat = os.statvfs(path)
    except OSError:
        return {}
    total = stat.f_frsize * stat.f_blocks
    avail = stat.f_frsize * stat.f_bavail
    used = total - avail
    return {
        "path": path,
        "total": total,
        "used": used,
        "avail": avail,
        "pct": round(used / total * 100, 1) if total else 0.0,
    }


def _loadavg() -> list[float]:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        return [0.0, 0.0, 0.0]


def _uptime_s() -> float:
    try:
        with open(f"{_PROC}/uptime") as handle:
            return round(float(handle.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return 0.0


def _proc_self() -> dict[str, Any]:
    """This server's own footprint -- the "for this project" half of the page.

    Host CPU and memory say whether the machine is in trouble; these say
    whether WebConsole is the reason. A leak here is invisible in the host
    figures until it is already severe.
    """
    global _prev_proc
    out: dict[str, Any] = {"pid": os.getpid()}
    try:
        with open(f"{_PROC}/self/status") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if not parts:
                    continue
                if key == "VmRSS":
                    out["rss"] = int(parts[0]) * 1024
                elif key == "Threads":
                    out["threads"] = int(parts[0])
    except (OSError, ValueError):
        pass
    try:
        out["fds"] = len(os.listdir(f"{_PROC}/self/fd"))
    except OSError:
        pass
    try:
        times = os.times()
        cpu_s = times.user + times.system
        now = time.monotonic()
        out["cpu_s"] = round(cpu_s, 2)
        if _prev_proc:
            d_cpu = cpu_s - _prev_proc[0]
            d_wall = now - _prev_proc[1]
            if d_wall > 0:
                out["cpu_pct"] = round(max(0.0, d_cpu / d_wall * 100), 1)
        _prev_proc = (cpu_s, now)
    except OSError:
        pass
    return out


_hw: dict[str, Any] | None = None


def _cpu_model() -> str | None:
    try:
        with open(f"{_PROC}/cpuinfo") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        pass
    try:  # ARM has no "model name"; the device tree carries the board instead.
        with open("/sys/firmware/devicetree/base/model", "rb") as handle:
            model = handle.read().decode(errors="ignore").strip("\x00 ").strip()
            return model or None
    except OSError:
        return None


def hw_info() -> dict[str, Any]:
    """Static facts about the box. Read once -- none of this changes."""
    global _hw
    if _hw is not None:
        return _hw
    info: dict[str, Any] = {}
    try:
        uname = os.uname()
        info["kernel"] = uname.release
        info["arch"] = uname.machine
        info["hostname"] = uname.nodename
    except OSError:
        pass
    if (model := _cpu_model()):
        info["cpu_model"] = model[:90]
    info["cpu_threads"] = os.cpu_count() or 0
    try:
        with open(f"{_PROC}/cpuinfo") as handle:
            for line in handle:
                if line.lower().startswith("cpu cores"):
                    info["cpu_cores"] = int(line.split(":")[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    _hw = info
    return info


def sample(disk_path: str | None = None) -> dict[str, Any]:
    """One host snapshot. Never raises; degrades to partial data.

    Blocking, but only just: these are memory-backed pseudo-files measured in
    microseconds. `os.statvfs` is the exception -- it can block on a wedged
    mount -- which is why callers on the event loop go through
    `sample_async()` rather than calling this directly.
    """
    global _prev_cpu

    cpu_pct = 0.0
    current = _read_cpu()
    if current and _prev_cpu:
        d_total = current[0] - _prev_cpu[0]
        d_idle = current[1] - _prev_cpu[1]
        if d_total > 0:
            cpu_pct = max(0.0, min(100.0, (1 - d_idle / d_total) * 100))
    if current:
        _prev_cpu = current

    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0)
    # MemAvailable, not MemFree: the kernel's own estimate of what a new
    # allocation could get, which counts reclaimable cache. MemFree on a
    # healthy Linux box reads as almost nothing and alarms people for no
    # reason.
    mem_avail = mem.get("MemAvailable", 0)
    mem_used = mem_total - mem_avail if mem_total else 0
    swap_total = mem.get("SwapTotal", 0)
    swap_used = swap_total - mem.get("SwapFree", 0) if swap_total else 0

    path = disk_path or str(config.PROJECTS_ROOT)
    return {
        "available": bool(current or mem),
        "at": time.time(),
        "cpu_pct": round(cpu_pct, 1),
        "ncpu": os.cpu_count() or 0,
        "load": _loadavg(),
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_avail": mem_avail,
        "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_pct": round(swap_used / swap_total * 100, 1) if swap_total else 0.0,
        "disk": _disk(path),
        "uptime_s": _uptime_s(),
        "proc": _proc_self(),
        "info": hw_info(),
    }


async def sample_async(disk_path: str | None = None) -> dict[str, Any]:
    """`sample()` off the event loop, because statvfs can block."""
    return await asyncio.to_thread(sample, disk_path)


def to_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Flatten a snapshot into the columns `system_samples` stores.

    The snapshot is nested because that is the shape the page wants to read;
    the table is flat because that is the shape SQL wants to aggregate. This
    is the one place the two meet, so a column added to the table has exactly
    one place to be filled from.
    """
    disk = snapshot.get("disk") or {}
    proc = snapshot.get("proc") or {}
    load = snapshot.get("load") or [0.0, 0.0, 0.0]
    return {
        "cpu_pct": snapshot.get("cpu_pct", 0.0),
        "mem_pct": snapshot.get("mem_pct", 0.0),
        "mem_used": snapshot.get("mem_used", 0),
        "mem_total": snapshot.get("mem_total", 0),
        "swap_pct": snapshot.get("swap_pct", 0.0),
        "disk_pct": disk.get("pct", 0.0),
        "disk_used": disk.get("used", 0),
        "disk_total": disk.get("total", 0),
        "load1": load[0] if len(load) > 0 else 0.0,
        "load5": load[1] if len(load) > 1 else 0.0,
        "load15": load[2] if len(load) > 2 else 0.0,
        "proc_rss": proc.get("rss", 0),
        "proc_cpu_pct": proc.get("cpu_pct", 0.0),
    }


# ── Background sampler ────────────────────────────────────────────────────────
#
# History has to be collected while nobody is looking, or the charts only ever
# show the moments someone happened to have the tab open.

_task: asyncio.Task | None = None


async def _loop(store, interval_s: int) -> None:
    # Prime the CPU delta immediately so the first stored sample is a real
    # percentage rather than the 0.0 that any first reading must produce.
    await sample_async()
    while True:
        try:
            await asyncio.sleep(interval_s)
            snapshot = await sample_async()
            if snapshot.get("available"):
                await store(to_row(snapshot))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a sampler must outlive one bad read
            _log_sampler_error()


def _log_sampler_error() -> None:
    import logging

    logging.getLogger("wc.sysstats").exception("system sample failed")


def start(store, interval_s: int | None = None) -> None:
    """Begin sampling into *store*, an async callable taking one snapshot."""
    global _task
    if _task and not _task.done():
        return  # Idempotent: a second lifespan must not double the sample rate.
    # `is None`, not `or`: an explicit interval of 0 means no delay, and
    # `or` would silently turn it into the default.
    every = config.SYSTEM_SAMPLE_S if interval_s is None else interval_s
    _task = asyncio.create_task(_loop(store, every))


# ── Write-path liveness ───────────────────────────────────────────────────────
#
# Registry #41: the server served HTTP 200 for 37 minutes while writing
# nothing. Its aiosqlite connection held a read transaction opened before
# another process wrote, so in WAL it could never upgrade to a writer -- every
# write failed with "database is locked", permanently, on that connection
# alone. A fresh connection took the lock instantly, which is why it looked
# healthy from outside.
#
# A read-only probe cannot see that: /login returns 200 either way. This table
# can, because it is the only one written unconditionally on a timer --
# `messages` writes only when somebody talks, `usage_events` only when a turn
# completes, so silence in either is normal. Silence here is not.
#
# Three states rather than a boolean, because the expensive mistake is the
# false positive. Registry #34 was a health check that restarted a *healthy*
# server every 30 seconds -- strictly worse than having none. Only STALE means
# "restart"; a server that has not been up long enough to have sampled yet is
# WARMING, and a database that has never been sampled is UNKNOWN.
OK: Final = "ok"
WARMING: Final = "warming"
STALE: Final = "stale"
UNKNOWN: Final = "unknown"


def newest_sample_at(db_path: str) -> str | None:
    """The newest stored sample stamp, read without taking any lock.

    `mode=ro` is the point: a plain connection can run a migration or take a
    write lock, and doing that against the live file is what caused #41 in the
    first place. A read-only URI connection physically cannot.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT MAX(created_at) FROM system_samples").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def process_uptime_s(pid: int) -> float | None:
    """Seconds since *pid* started, from /proc. None if it cannot be read."""
    try:
        with open(f"{_PROC}/{pid}/stat") as handle:
            # Field 22 is starttime in clock ticks. The comm field can contain
            # spaces and brackets, so everything up to the last ')' is skipped
            # rather than split on -- a process named "(my app) x" would
            # otherwise shift every field after it.
            fields = handle.read().rpartition(")")[2].split()
        started_ticks = int(fields[19])
        with open(f"{_PROC}/uptime") as handle:
            up = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return max(0.0, up - started_ticks / os.sysconf("SC_CLK_TCK"))


def write_health(
    newest: str | None,
    uptime_s: float | None,
    interval_s: int,
    now: float | None = None,
) -> tuple[str, str]:
    """Classify the write path from the newest sample. Pure, so it is testable.

    The tolerance is three sampling intervals with a 180s floor: one missed
    sample is a busy machine, three in a row is a write path that has stopped.
    The floor keeps a very short configured interval from making the check
    hair-triggered.
    """
    limit = max(3 * interval_s, 180)
    if newest is None:
        return UNKNOWN, "no samples recorded"
    now = time.time() if now is None else now
    try:
        stamp = _dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return UNKNOWN, f"unparseable stamp {newest!r}"
    age = now - stamp.timestamp()
    if age <= limit:
        return OK, f"last write {age:.0f}s ago"
    # The row is old, but a just-restarted server has not had time to write
    # one yet -- and the stale rows it inherited are from before the restart.
    # Without this branch every restart looks like a dead write path.
    if uptime_s is not None and uptime_s <= limit:
        return WARMING, f"up {uptime_s:.0f}s, first sample not due yet"
    return STALE, f"last write {age:.0f}s ago, over the {limit}s limit"


def main(argv: list[str] | None = None) -> int:
    """CLI for a health check: `python3 -m sysstats --pid <server pid>`.

    Reads `config.DB_PATH`, which already resolves `WC_DB_PATH` and the
    project-local fallback. Re-implementing either half here would get the
    answer right only when the env var is unset -- true on a developer's
    machine, false on a real deployment -- and a probe pointed at the wrong
    file reports `unknown` for ever, so it looks installed while doing
    nothing.

    Exit status is 1 only for STALE. `warming` and `unknown` exit 0: they mean
    "no verdict yet", and a caller that restarts on them restart-loops a
    server that is merely young.
    """
    import argparse

    parser = argparse.ArgumentParser(description="WebConsole write-path liveness")
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="server pid, for the just-restarted grace period; "
        "without it a young server is reported STALE rather than WARMING",
    )
    args = parser.parse_args(argv)

    newest = newest_sample_at(config.DB_PATH)
    uptime = process_uptime_s(args.pid) if args.pid else None
    state, why = write_health(newest, uptime, config.SYSTEM_SAMPLE_S)
    print(f"{state}: {why} (db={config.DB_PATH})")
    return 1 if state == STALE else 0


async def stop() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    # suppress rather than try/except/pass: the cancellation is expected, and
    # a bare pass here is indistinguishable from a swallowed real error.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _task
    _task = None


if __name__ == "__main__":
    raise SystemExit(main())
