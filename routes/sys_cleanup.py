"""Process cleanup for the Server settings panel.

Scans for reclaimable processes, returns what could be killed, how much
memory it would free, and what types are present. A separate call performs
the actual SIGTERM/SIGKILL so it stays an explicit two-step choice.

Only kills processes owned by the uid that this Python process runs as.
Never targets uvicorn, caddy, or the python process that is running this
module itself. Best-effort -- failures are recorded, not raised.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time

_log = logging.getLogger("wc.app")

_CWD = "/proc"
_KNOWN_SERVICES = {"uvicorn", "caddy"}
_CLAUDE_SHELL_RE = re.compile(r"wc-claude\.sh")
_CLAUDE_CLI_RE = re.compile(r"(2\.1\.\d+|claude).*--(dangerously-skip-permissions|--resume)")
_CHROME_RE = re.compile(r"(chrome|chromium)")
_PYTEST_RE = re.compile(r"python.*(?:pytest|test|unittest)", re.IGNORECASE)


def _own_pid():
    return os.getpid()


def _own_uid():
    return os.getuid()


def _pid_alive(pid):
    return os.path.exists(f"{_CWD}/{pid}")


def _proc_read(pid, filename):
    try:
        with open(f"{_CWD}/{pid}/{filename}", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _proc_read_int(pid, filename, field, default=0):
    """Read a specific field from /proc/pid/<filename>.

    Handles both space-delimited files (stat, status) and tab-delimited
    (status).  `field` is either an index (int) for space-delimited files
    or a colon-prefix ("VmRSS:") for line-based files.
    """
    raw = _proc_read(pid, filename)
    if not raw:
        return default

    if isinstance(field, str):
        # Line-based: look for "FieldName:\tVALUE"
        for line in raw.splitlines():
            if line.startswith(field):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1])
                    except (ValueError, TypeError):
                        return default
        return default

    # Index-based: space-delimited, 0-indexed after split
    try:
        tokens = raw.split()
        return int(tokens[field])
    except (IndexError, ValueError, AttributeError):
        return default


def _proc_status_name(pid):
    raw = _proc_read(pid, "status")
    for line in raw.splitlines():
        if line.startswith("Name:"):
            return line.split(":", 1)[1].strip()
    return ""


def _proc_status_state(pid):
    raw = _proc_read(pid, "status")
    for line in raw.splitlines():
        if line.startswith("State:"):
            return line.split(":", 1)[1].split()[0] if line.split(":", 1)[1].strip() else ""
    return ""


def _proc_cmdline(pid):
    raw = _proc_read(pid, "cmdline")
    if not raw:
        return ""
    return raw.replace("\x00", " ").strip()[:200]


def _proc_rss(pid):
    return _proc_read_int(pid, "status", "VmRSS:")


def _proc_create_time(pid):
    try:
        return os.stat(f"{_CWD}/{pid}").st_ctime
    except OSError:
        return 0


def _proc_uids(pid):
    raw = _proc_read(pid, "status")
    for line in raw.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()[1:]
            if len(parts) >= 1:
                try:
                    return int(parts[0])
                except ValueError:
                    pass
    return -1


def _proc_pgid(pid):
    try:
        return os.getpgid(pid)
    except Exception:
        return -1


def _collect_all_processes():
    """Collect all processes owned by this uid with full info."""
    now = time.time()
    raw = {}
    caller_pgid = _proc_pgid(_own_pid())
    caller_uid = _own_uid()
    # Walk up our parent chain — exclude every ancestor PID and their PGIDs.
    # The Claude CLI that spawned this uvicorn lives in an ancestor PGID.
    # Limit to ~5 levels up so we don't accidentally protect the whole
    # system tree (the walk reaches PID 1 / init quickly).
    _protect_pids: set[int] = {_own_pid()}
    _protect_pgids: set[int] = {caller_pgid}
    pid = _own_pid()
    for _depth in range(8):
        _protect_pids.add(pid)
        try:
            # /proc/pid/stat field 19 (0-indexed) is ppid; skip the
            # comm field (fields 1-10) which may contain parens.
            stat_raw = _proc_read(pid, "stat")
            # Find the last "(" and match the following ")" — everything
            # after it is space-delimited numeric fields.
            last_paren = stat_raw.rfind(")")
            if last_paren == -1:
                raise ValueError("no ')' in stat")
            fields = stat_raw[last_paren + 2:].split()
            ppid_val = int(fields[1])  # field index 0 = state, 1 = ppid
        except Exception:
            ppid_val = 0
        if ppid_val < 1:
            break
        try:
            ppid_pg = os.getpgid(ppid_val)
            _protect_pgids.add(ppid_pg)
        except (OSError, ProcessLookupError):
            pass
        pid = ppid_val

    for entry in os.listdir(_CWD):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid < 1:
            continue
        # Protect: this Python process, ancestors, and any process in our
        # PGID tree — killing them would kill ourselves.
        if pid in _protect_pids:
            continue
        try:
            if os.getpgid(pid) in _protect_pgids:
                continue
        except (OSError, ProcessLookupError):
            continue
        try:
            euid = _proc_uids(pid)
            if euid != caller_uid:
                continue
        except Exception:
            continue

        name = _proc_status_name(pid)
        if name.lower() in _KNOWN_SERVICES:
            continue
        cmdline = _proc_cmdline(pid)

        kind = _detect_kind(pid, name, cmdline)

        try:
            rss_bytes = _proc_rss(pid)  # VmRSS is in kB
            rss_mb = round(rss_bytes / 1024, 1)  # kB -> MB
        except Exception:
            rss_mb = 0.0
        try:
            ct = _proc_create_time(pid)
            age_s = int(now - ct)
        except Exception:
            age_s = 0
        try:
            pgid = _proc_pgid(pid)
        except Exception:
            pgid = -1

        raw[pid] = {
            "pid": pid, "name": name, "kind": kind,
            "rss_mb": rss_mb, "age_s": age_s,
            "cmdline": cmdline, "pgid": pgid,
        }
    return raw


def _detect_kind(pid, name, cmdline):
    """Return one of 'zombie' | 'chrome' | 'claude' | 'python' | None."""
    state = _proc_status_state(pid)
    if state == "Z":
        return "zombie"

    if _CHROME_RE.search(name) or _CHROME_RE.search(cmdline):
        return "chrome"

    if _CLAUDE_SHELL_RE.search(cmdline) or _CLAUDE_CLI_RE.search(cmdline):
        return "claude"

    if name and "python" in name:
        if _PYTEST_RE.search(cmdline):
            return "python"

    return None


def _cpu_is_idle(pid):
    """Check if a process is using < 2% CPU via a single sampling."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu="],
            capture_output=True, text=True, timeout=2,
        )
        cpu = float(out.stdout.strip()) if out.stdout.strip() else 100
        return cpu < 2.0
    except Exception:
        return False


def preview() -> dict:
    """Return what could be killed without doing anything.

    Returns {"claude": [...], "chrome": [...], "python": [...],
             "zombie": [...], "total_estimated_mb": N, "counts": {...}}.
    """
    all_procs = _collect_all_processes()
    results = {}
    counts = {}
    total_mb = 0.0

    # --- zombies ---
    zombies = [p for p in all_procs.values() if p["kind"] == "zombie"]
    results["zombie"] = sorted(zombies, key=lambda x: -x["rss_mb"])
    counts["zombie"] = len(zombies)
    total_mb += sum(p["rss_mb"] for p in zombies)

    # --- claude ---
    claude_candidates = [p for p in all_procs.values() if p["kind"] == "claude"]
    # Filter: age > 30 min. Don't filter by CPU — we want to surface ALL
    # long-running agents so the user can see what's active vs idle.
    claude_candidates = [p for p in claude_candidates if p["age_s"] > 1800]

    results["claude"] = sorted(claude_candidates, key=lambda x: -x["rss_mb"])
    counts["claude"] = len(claude_candidates)
    total_mb += sum(p["rss_mb"] for p in claude_candidates)

    # --- chrome ---
    chrome = [p for p in all_procs.values() if p["kind"] == "chrome"]
    chrome = [p for p in chrome if p["rss_mb"] > 50 and p["age_s"] > 60]
    results["chrome"] = sorted(chrome, key=lambda x: -x["rss_mb"])
    counts["chrome"] = len(chrome)
    total_mb += sum(p["rss_mb"] for p in chrome)

    # --- python (pytest/test) ---
    py_tests = [p for p in all_procs.values() if p["kind"] == "python"]
    results["python"] = sorted(py_tests, key=lambda x: -x["rss_mb"])
    counts["python"] = len(py_tests)
    total_mb += sum(p["rss_mb"] for p in py_tests)

    results["total_estimated_mb"] = round(total_mb, 1)
    results["counts"] = counts
    return results


def _kill_group(pgid):
    """Kill an entire process group. Returns (success, note)."""
    try:
        os.killpg(pgid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            if _pgid_dead(pgid):
                return True, ""
        os.killpg(pgid, signal.SIGKILL)
        for _ in range(4):
            time.sleep(0.5)
            if _pgid_dead(pgid):
                return True, "SIGKILL"
        return True, "pg still alive after SIGTERM+SIGKILL"
    except ProcessLookupError:
        return True, "pg already gone"
    except PermissionError:
        return False, "permission denied"
    except Exception as exc:
        return False, str(exc)


def _pgid_dead(pgid):
    try:
        os.getpgid(pgid)
        return False
    except ProcessLookupError:
        return True


def _kill_single(pid):
    """Kill a single process. Returns (success, note)."""
    try:
        if _pid_alive(pid):
            state = _proc_status_state(pid)
            if state == "Z":
                return True, "zombie (awaiting parent)"
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                if not _pid_alive(pid):
                    return True, ""
            os.kill(pid, signal.SIGKILL)
            for _ in range(4):
                time.sleep(0.5)
                if not _pid_alive(pid):
                    return True, ""
            return True, "SIGKILL"
        return True, "already gone"
    except ProcessLookupError:
        return True, "already gone"
    except PermissionError:
        return False, "permission denied"
    except Exception as exc:
        return False, str(exc)


def execute() -> dict:
    """Kill reclaimable processes. Returns killed[], failed[], freed_mb."""
    stats = preview()
    killed = []
    failed = []
    freed = 0.0
    processed_pids = set()

    # --- zombie ---
    for p in stats.get("zombie", []):
        pid = p["pid"]
        if pid in processed_pids:
            continue
        processed_pids.add(pid)
        rss = p["rss_mb"]
        ok, note = _kill_single(pid)
        if ok:
            killed.append({"pid": pid, "kind": "zombie", "rss_mb": rss, "note": note})
            freed += rss
        else:
            failed.append({"pid": pid, "kind": "zombie", "error": note})

    # --- claude: kill by PGID to get the whole group ---
    claude_candidates = stats.get("claude", [])
    if claude_candidates:
        # Group by PGID (each PGID is one logical session)
        groups = {}
        for p in claude_candidates:
            pgid = p["pgid"]
            if pgid < 0:
                pgid = p["pid"]  # fallback: unique key
            groups.setdefault(pgid, []).append(p)

        for pgid, group_procs in groups.items():
            for p in group_procs:
                processed_pids.add(p["pid"])
            # Total RSS for this group
            group_rss = sum(p["rss_mb"] for p in group_procs)

            ok, note = _kill_group(pgid)
            if ok:
                killed.append({"pid": pgid, "kind": "claude", "rss_mb": round(group_rss, 1), "note": note})
                freed += group_rss
            else:
                for p in group_procs:
                    failed.append({"pid": p["pid"], "kind": "claude", "error": note})

    # --- chrome ---
    for p in stats.get("chrome", []):
        pid = p["pid"]
        if pid in processed_pids:
            continue
        processed_pids.add(pid)
        rss = p["rss_mb"]
        ok, note = _kill_single(pid)
        if ok:
            killed.append({"pid": pid, "kind": "chrome", "rss_mb": rss, "note": note})
            freed += rss
        else:
            failed.append({"pid": pid, "kind": "chrome", "error": note})

    # --- python (pytest/test) ---
    for p in stats.get("python", []):
        pid = p["pid"]
        if pid in processed_pids:
            continue
        processed_pids.add(pid)
        rss = p["rss_mb"]
        ok, note = _kill_single(pid)
        if ok:
            killed.append({"pid": pid, "kind": "python", "rss_mb": rss, "note": note})
            freed += rss
        else:
            failed.append({"pid": pid, "kind": "python", "error": note})

    return {
        "killed": killed,
        "failed": failed,
        "freed_mb": round(freed, 1),
        "counts": {k: v for k, v in stats.get("counts", {}).items()},
    }
