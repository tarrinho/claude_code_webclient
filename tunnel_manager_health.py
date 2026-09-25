"""Health probes and remote stats collection for tunnel manager.

Probe: TCP connection + claude_proxy.py handshake to confirm tunnel is live.
Stats: collect uptime/disk/load via SSH exec, store in system_samples.

Split boundary: tunnel state management (tunnel_manager) <-> SSH transport
(tunnel_manager_ssh) <-> health probe (this file).
"""
from __future__ import annotations

import logging
import re

_log = logging.getLogger("wc.tunnel.health")


async def probe_proxy(machine_id: str) -> bool:
    """Whether a turn can actually reach the proxy through the tunnel.

    Completes the real handshake against the forwarded local port --
    `{"type": "handshake", ...}` out, `{"type": "ack"}` back -- which is what
    `proxy_ok` is asked to mean everywhere it is consumed: runner routes turns
    on it, and machines.js renders the 'active' badge from it.

    This used to run `pgrep -f claude_proxy` over SSH and return whether
    anything matched, while its docstring described the handshake. Both halves
    were a problem, and the gap between them is what shipped:

    * a remote process matching that name proves the process exists, not that
      the path to it works. It may be wedged, bound to another port, or
      started against a different token;
    * an `ssh -L` forward accepts connections locally whether or not anything
      is listening at the far end -- it accepts, then immediately closes -- so
      a listening local port proves the SSH session and never the proxy.

    RETRACTION, 2026-09-25. An earlier version of this docstring claimed the
    change was forced by measurement: that all four of this deployment's
    transport backends answered EOF to a real handshake while being reported
    healthy. That was wrong, and the way it was wrong is worth more than the
    claim was.

    The probe making those measurements ran outside the console, where
    `config.PROXY_TOKEN` is empty. `claude_proxy.py` closes the connection
    without replying when the token does not match -- so every EOF was a
    correct authentication refusal, read as a dead tunnel. Re-run with the
    token from the settings table, all four answer `ack`. The transports were
    working the whole time.

    The reasoning above still holds on its own terms -- a remote process is not
    a reachable one, and an `ssh -L` forward listens either way -- so the check
    is stronger for testing the path rather than a process name, and the live
    manager now reports proxy_ok from a real handshake. But it was a design
    argument, not a bug report, and it should never have been dressed as one.

    The lesson sits closer to home than the check: a probe that cannot
    authenticate produces a symptom indistinguishable from the failure it is
    hunting. Verify the client before trusting what it measures.

    A failure logs the pgrep result too, because "no process over there" and
    "process running but nothing gets through" need different fixes and the
    verdict alone cannot tell them apart.
    """
    import asyncio
    import json

    import config

    # In-memory state first, the persisted row second. The manager owns the
    # live port while it is running; the row is what survives a restart, and
    # is consulted only when memory has nothing -- the same order
    # runner.get_proxy_target uses, so the two cannot disagree about where a
    # machine's tunnel is.
    import tunnel_manager

    port = (tunnel_manager._STATE.get(machine_id) or {}).get("local_port")
    if not port:
        import db
        if db.db_conn is None:
            return False
        row = await db.ssh_tunnel_get(machine_id)
        port = (row or {}).get("local_port")
    if not port:
        return False

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", int(port)), timeout=5)
        writer.write((json.dumps({
            "type": "handshake",
            "protocol": config.PROTOCOL,
            "token": config.PROXY_TOKEN,
        }) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=8)
        if not line:
            await _log_why_unreachable(machine_id, port, "forward returned EOF")
            return False
        try:
            frame = json.loads(line)
        except ValueError:
            await _log_why_unreachable(machine_id, port, "non-JSON reply")
            return False
        if frame.get("type") == "ack":
            return True
        await _log_why_unreachable(
            machine_id, port, f"handshake refused: {str(frame)[:80]}")
        return False
    except Exception as exc:
        await _log_why_unreachable(
            machine_id, port, f"{type(exc).__name__}: {exc}")
        return False
    finally:
        if writer is not None:
            writer.close()


async def _log_why_unreachable(machine_id: str, port, detail: str) -> None:
    """Say whether the remote process exists, so the verdict is actionable.

    "the proxy is unreachable" sends someone to the tunnel; "unreachable and
    no process is running there" sends them to the remote host. The check
    itself must not depend on this -- it is diagnosis, not evidence.
    """
    from tunnel_manager_ssh import exec_command

    remote = "unknown (ssh exec failed)"
    try:
        _, stdout, _ = await exec_command(
            machine_id, "pgrep -f 'claude_proxy' 2>/dev/null", timeout=5)
        found = stdout.read().decode("utf-8", errors="replace").strip()
        remote = "a claude_proxy process is running" if found else "no claude_proxy process"
    except Exception:
        pass
    _log.warning(
        "proxy_unreachable machine=%s port=%s: %s; on the remote host: %s",
        machine_id, port, detail, remote,
    )


def _one_number(raw: str) -> float | None:
    """The leading number in *raw*, or None when there is not one.

    `top` prints the CPU figure with a suffix on some hosts ("12.5%us"), `df`
    prints "45%", and a failed command lands here as "ERROR" or "". None means
    "no reading", which the caller must keep distinct from zero -- conflating
    them is the bug this helper exists to end: every transport sample was
    written as a row of zeros and read back as an idle host.
    """
    if not raw:
        return None
    text = raw.strip().rstrip("%")
    # Trim a trailing unit ("%us", "id") without accepting something that is
    # not a number at all.
    match = re.match(r"^[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover - re guarantees this parses
        return None


def parse_stats(raw: dict[str, str]) -> dict[str, float]:
    """Map `collect_stats`'s shell output onto system_samples' column names.

    The labels `collect_stats` collects ("cpu", "disk", "disk_bytes", "mem",
    "mem_bytes", "swap", "load", "cores", "uptime", "hostname", "kernel")
    are not the column names `system_sample_insert` writes ("cpu_pct",
    "disk_pct", "disk_used", "disk_total", "mem_used", "mem_total", "swap_pct",
    "load1", "load5", "load15", "uptime_s"), and that function defaults
    anything it is not given to 0. So the poller had been storing a zero for
    every metric of every connected transport, once per stats interval, since
    the day it was wired up.

    A field with no usable reading is *omitted*, not zeroed. The caller can
    then decline to store a sample that says nothing, rather than recording a
    transport as idle because its SSH exec failed.
    """
    parsed: dict[str, float] = {}
    # `cores` is deliberately excluded from the "did we read anything" test
    # below: it is a property of the host, not a measurement of it, so a
    # sample carrying nothing but a core count says nothing and must not be
    # stored as though it did.
    for label, column in (("cpu", "cpu_pct"), ("mem", "mem_pct"),
                          ("disk", "disk_pct"), ("cores", "cores")):
        value = _one_number(raw.get(label, ""))
        if value is not None:
            parsed[column] = value

    # Disk bytes: "used total" from `df -B1 /`.
    disk_bytes = _split_bytes(raw.get("disk_bytes"))
    if disk_bytes:
        parsed["disk_used"] = disk_bytes["used"]
        parsed["disk_total"] = disk_bytes["total"]

    # Memory bytes: "used total" from `free -b`.
    mem_bytes = _split_bytes(raw.get("mem_bytes"))
    if mem_bytes:
        parsed["mem_used"] = mem_bytes["used"]
        parsed["mem_total"] = mem_bytes["total"]

    # Swap percentage.
    swap_val = _one_number(raw.get("swap", ""))
    if swap_val is not None:
        parsed["swap_pct"] = swap_val

    # /proc/loadavg's first three fields, positionally: "0.52 0.41 0.38".
    load_parts = (raw.get("load") or "").split()
    for column, part in zip(("load1", "load5", "load15"), load_parts):
        value = _one_number(part)
        if value is not None:
            parsed[column] = value

    # Uptime in seconds.
    uptime = _one_number(raw.get("uptime", ""))
    if uptime is not None:
        parsed["uptime_s"] = uptime

    return parsed


def _split_bytes(raw: str | None) -> dict[str, float] | None:
    """'used total' from `df -B1 /` or `free -b`.

    Returns {"used": N, "total": M} or None when there is not one usable
    value in *raw*.
    """
    if not raw:
        return None
    parts = raw.strip().split()
    if len(parts) < 2:
        return None
    used = _one_number(parts[0])
    total = _one_number(parts[1])
    if used is None or total is None:
        return None
    return {"used": used, "total": total}


async def collect_stats(machine_id: str, store_fn=None) -> dict[str, str]:
    """Collect remote host stats via SSH exec and optionally publish them."""
    import tunnel_manager
    from tunnel_manager_ssh import exec_command

    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        return {}
    stats: dict[str, str] = {}
    for label, cmd in [
        ("cpu", "top -bn1 | grep 'Cpu(s)' | awk '{print $2}'"),
        ("disk", "df -h / 2>/dev/null | awk 'NR==2{print $5}'"),
        ("disk_bytes", "df -B1 / 2>/dev/null | awk 'NR==2{print $3\" \"$2}'"),
        ("mem", "free | awk '/Mem:/{printf \"%.1f\", $3/$2*100}'"),
        ("mem_bytes", "free -b 2>/dev/null | awk '/Mem:/{printf \"%s %s\", $3, $2}'"),
        ("swap", "free | awk '/Swap:/{printf \"%.1f\", $3/$2*100}'"),
        ("load", "cat /proc/loadavg 2>/dev/null | awk '{print $1,$2,$3}'"),
        # The denominator for load average. Collected per sample rather than
        # once per transport because there is nowhere to keep a per-transport
        # fact that the bucketed aggregate can reach, and it costs one more
        # exec on a connection that is already open and already running four.
        ("cores", "nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null"),
        # Uptime of the host, in seconds.
        ("uptime", "awk '{printf \"%.1f\", $1}' /proc/uptime 2>/dev/null"),
        # Hostname and kernel release for hardware-info style display.
        ("hostname", "hostname 2>/dev/null"),
        ("kernel", "uname -r 2>/dev/null"),
    ]:
        try:
            _, stdout, _ = await exec_command(machine_id, cmd, timeout=5)
            stats[label] = stdout.read().decode("utf-8", errors="replace").strip()
        except Exception:
            stats[label] = "ERROR"

    if store_fn:
        parsed = parse_stats(stats)
        if not {k for k in parsed if k != "cores"}:
            # Nothing readable came back. Storing this would write a row of
            # zeros that reads as a healthy idle host -- which is exactly what
            # the unmapped keys used to do.
            _log.warning(
                "remote stats unreadable for machine_id=%s; storing nothing "
                "(raw=%r)", machine_id, stats,
            )
        else:
            # Attributed to the *transport*, not the machine, because the
            # transport is the host these figures describe. Several machines
            # can share one transport_id (that is what _TRANSPORT_CONNECTIONS
            # exists for -- Kali3 currently has two), so keying on machine_id
            # would store the same host's readings twice per interval under
            # two names and show it twice in any per-host table.
            #
            # Falls back to machine_id when the state has no transport_id, so
            # a sample is still attributed to something rather than dropped.
            host_id = (state.get("transport_id") or machine_id)
            try:
                # The columns already existed and defaulted to 'local', so
                # every row written before this stays correct.
                await store_fn(parsed, host_type="transport", host_id=host_id)
            except Exception as exc:
                _log.error("store remote stats failed: %s", exc)
    return stats
