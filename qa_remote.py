"""Orchestrate a QA suite run on a remote transport.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.

Runs *inside* the app process, never as a standalone script: exec_command,
open_sftp and therefore sync_transport are all bound to
tunnel_manager._STATE, the running webconsole.service process's own
in-memory live SSH connections (spec §4). routes/qa.py is the only caller
that should ever import this module from outside a request handler.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("wc.qa_remote")

# Deliberately not ~/wc-proxy (the transport's own remote_path, where
# claude_proxy.py actually runs from) -- see spec §2 for the blast-radius
# and cadence reasons this checkout is kept separate.
QA_REMOTE_PATH = "~/wc-qa-checkout"


class QaRefusal(Exception):
    """A QA run cannot proceed. .status_code is the HTTP status routes/qa.py
    should answer with; .reason is the user-facing message."""

    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


async def _available_mb(machine_id: str) -> int | None:
    """Live remote available memory in MB, read fresh every call -- never
    trusted from storage (spec §3: tunnel_manager_health's store_fn is
    separately broken, so nothing here can rely on persisted stats).
    Returns None on any failure to read or parse, distinct from a
    successfully-read 0 -- callers must not conflate "could not check" with
    "checked, and it's empty"."""
    import tunnel_manager_ssh

    try:
        _, stdout, _ = await tunnel_manager_ssh.exec_command(
            machine_id, "free -m | awk '/Mem:/{print $7}'", timeout=5)
        return int(stdout.read().decode("utf-8", "replace").strip())
    except Exception:
        return None


async def _check_capacity(machine_id: str, floor_mb: int) -> tuple[bool, int | None]:
    """(ok, available_mb). ok is False both when available is below floor_mb
    and when available could not be read at all."""
    available = await _available_mb(machine_id)
    if available is None:
        return False, None
    return available >= floor_mb, available


async def _is_provisioned(machine_id: str) -> bool:
    """Has bin/wc-provision-qa.sh ever run here? Checked live, the same way
    as capacity -- a stale "provisioned" flag would be worse than no flag at
    all, since the checkout could have been wiped since."""
    import tunnel_manager_ssh

    cmd = f"test -x {QA_REMOTE_PATH}/.venv/bin/python && echo yes"
    try:
        _, stdout, _ = await tunnel_manager_ssh.exec_command(machine_id, cmd, timeout=5)
        return stdout.read().decode("utf-8", "replace").strip() == "yes"
    except Exception:
        return False


import asyncio
from dataclasses import dataclass

_RUN_LOCKS: dict[str, asyncio.Lock] = {}


def _run_lock(machine_id: str) -> asyncio.Lock:
    """One lock per machine_id, held for a QA run's whole lifetime (spec
    §5) -- sync through the last chunk. Never cleaned up, matching
    tunnel_manager._transport_lock's own reasoning: the number of machines
    a user configures is small and this is a cheap resource."""
    lock = _RUN_LOCKS.get(machine_id)
    if lock is None:
        lock = asyncio.Lock()
        _RUN_LOCKS[machine_id] = lock
    return lock


async def _machine_for_transport(transport_id: str, owner: str) -> str | None:
    """The machine tunnel_status/exec_command/open_sftp key on for this
    transport. A deliberate copy of routes.transports._machine_for_transport
    (module-private there, so not imported) -- one shared tunnel per
    transport, so any machine on it addresses the same connection."""
    import db

    machines = [
        m for m in await db.ai_machines_list(owner)
        if m.get("transport_id") == transport_id
    ]
    return machines[0]["id"] if machines else None


@dataclass
class Prepared:
    transport: dict
    machine_id: str
    floor_mb: int


async def _check_named(transport: dict, machine_id: str, floor_mb: int) -> None:
    """Raises QaRefusal on the first precondition that fails, in the order
    spec §4/§5 states them: liveness, provisioning, lock, capacity."""
    import tunnel_manager

    name = transport["name"]
    status = await tunnel_manager.tunnel_status(machine_id)
    if not status or not status.get("tunnel_up"):
        raise QaRefusal(409, f"{name} has no live tunnel — Check or Init it first")
    if not await _is_provisioned(machine_id):
        raise QaRefusal(
            409, f"{name} has not been provisioned for QA — "
                 f"run bin/wc-provision-qa.sh {name} first")
    if _run_lock(machine_id).locked():
        raise QaRefusal(
            409, f"{name} already has a QA run in progress — "
                 f"wait for it or pick another transport")
    ok, available = await _check_capacity(machine_id, floor_mb)
    if not ok:
        seen = f"{available} MB" if available is not None else "unknown (read failed)"
        raise QaRefusal(
            503, f"{name} does not have enough free memory for a QA run "
                 f"(available: {seen}, floor: {floor_mb} MB)")


async def resolve_transport(
    owner: str, name: str | None, floor_mb: int = 700,
) -> Prepared:
    """Named: use that transport or raise QaRefusal with the specific
    reason. Unnamed: consider every owner's Active, unlocked, provisioned
    transport, measure each one's live memory, and pick the roomiest --
    never fall back to running locally (spec §5)."""
    import db
    import tunnel_manager

    if name:
        transport = next(
            (t for t in await db.ssh_transports_list(owner) if t["name"] == name),
            None,
        )
        if not transport:
            raise QaRefusal(404, f"no transport named {name!r}")
        machine_id = await _machine_for_transport(transport["id"], owner)
        if not machine_id:
            raise QaRefusal(400, f"{name} has no backend assigned")
        await _check_named(transport, machine_id, floor_mb)
        return Prepared(transport=transport, machine_id=machine_id, floor_mb=floor_mb)

    best: Prepared | None = None
    best_available = -1
    for transport in await db.ssh_transports_list(owner):
        machine_id = await _machine_for_transport(transport["id"], owner)
        if not machine_id or _run_lock(machine_id).locked():
            continue
        status = await tunnel_manager.tunnel_status(machine_id)
        if not status or not status.get("tunnel_up"):
            continue
        if not await _is_provisioned(machine_id):
            continue
        ok, available = await _check_capacity(machine_id, floor_mb)
        if not ok or available is None:
            continue
        if available > best_available:
            best = Prepared(transport=transport, machine_id=machine_id, floor_mb=floor_mb)
            best_available = available
    if best is None:
        raise QaRefusal(503, "no transport is Active, provisioned and has enough "
                              "free memory for a QA run right now")
    return best
