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
    """Probe the proxy through the tunnel.

    Sends handshake NDJSON frame then a turn probe to claude_proxy.py
    on 127.0.0.1:<local_port>. Returns True on success.
    """
    from tunnel_manager_ssh import exec_command

    try:
        _, stdout, _ = await exec_command(
            machine_id,
            "pgrep -f 'claude_proxy' 2>/dev/null",
            timeout=5,
        )
        result = stdout.read().decode("utf-8", errors="replace").strip()
        return bool(result)
    except Exception:
        return False


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

    The labels `collect_stats` collects under ("cpu", "disk", "mem", "load")
    are not the column names `system_sample_insert` writes ("cpu_pct",
    "disk_pct", "mem_pct", "load1"/"load5"/"load15"), and that function
    defaults anything it is not given to 0. So the poller had been storing a
    zero for every metric of every connected transport, once per stats
    interval, since the day it was wired up -- 146 such rows by the time it
    was noticed, mixed into the local host's own series.

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
    # /proc/loadavg's first three fields, positionally: "0.52 0.41 0.38".
    load_parts = (raw.get("load") or "").split()
    for column, part in zip(("load1", "load5", "load15"), load_parts):
        value = _one_number(part)
        if value is not None:
            parsed[column] = value
    return parsed


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
        ("mem", "free | awk '/Mem:/{printf \"%.1f\", $3/$2*100}'"),
        ("load", "cat /proc/loadavg 2>/dev/null | awk '{print $1,$2,$3}'"),
        # The denominator for load average. Collected per sample rather than
        # once per transport because there is nowhere to keep a per-transport
        # fact that the bucketed aggregate can reach, and it costs one more
        # exec on a connection that is already open and already running four.
        ("cores", "nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null"),
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
