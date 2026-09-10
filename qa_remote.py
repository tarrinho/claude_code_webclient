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
    "checked, and it's empty".

    Both the exec_command call and the blocking .read() are wrapped in
    asyncio.wait_for + asyncio.to_thread (mirroring
    routes/chats.py._remote_agent_reply's established pattern) -- exec_command
    is `async def` but its body, and file.read(), are synchronous paramiko
    calls that block the single event loop thread for as long as the remote
    command takes without this.
    """
    import tunnel_manager_ssh

    try:
        _, stdout, _ = await asyncio.wait_for(
            tunnel_manager_ssh.exec_command(
                machine_id, "free -m | awk '/Mem:/{print $7}'", timeout=5),
            timeout=5)
    except Exception:
        return None

    def _read() -> bytes:
        return stdout.read()

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_read), timeout=5)
        return int(raw.decode("utf-8", "replace").strip())
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
    all, since the checkout could have been wiped since. See _available_mb's
    docstring for why both the exec_command call and the read are wrapped."""
    import tunnel_manager_ssh

    cmd = f"test -x {QA_REMOTE_PATH}/.venv/bin/python && echo yes"
    try:
        _, stdout, _ = await asyncio.wait_for(
            tunnel_manager_ssh.exec_command(machine_id, cmd, timeout=5), timeout=5)
    except Exception:
        return False

    def _read() -> bytes:
        return stdout.read()

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_read), timeout=5)
        return raw.decode("utf-8", "replace").strip() == "yes"
    except Exception:
        return False


import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

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
    owner: str, name: str | None, floor_mb: int | None = None,
) -> Prepared:
    """Named: use that transport or raise QaRefusal with the specific
    reason. Unnamed: consider every owner's Active, unlocked, provisioned
    transport, measure each one's live memory, and pick the roomiest --
    never fall back to running locally (spec §5)."""
    import db
    import tunnel_manager

    if floor_mb is None:
        import config
        floor_mb = config.QA_CAPACITY_FLOOR_MB

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


async def _sync(prepared: Prepared) -> dict:
    """One call, no new sync engine code -- transport_sync.sync_transport is
    reused unmodified, pointed at the QA checkout and the QA pointer, never
    the production ones (spec §2).

    transport_sync's transfer runs over SFTP, which does not do bash-style
    '~' expansion the way every exec_command shell call in this module does
    -- so QA_REMOTE_PATH's tilde form would put/mkdir against a directory
    literally named '~' at the SFTP root, not bin/wc-provision-qa.sh's real
    checkout at $HOME/wc-qa-checkout. $HOME is resolved once, live, right
    here -- only for this SFTP-bound call, nowhere else in this module needs
    it. A failed $HOME read falls back to the tilde form (today's existing,
    already-broken-for-SFTP behavior) rather than hard-failing the whole run
    over an unrelated hiccup; transport_sync.sync_transport's own SyncError
    handling still applies from there.
    """
    import db
    import tunnel_manager_ssh
    import transport_sync

    home = ""
    try:
        _, stdout, _ = await asyncio.wait_for(
            tunnel_manager_ssh.exec_command(prepared.machine_id, "echo $HOME", timeout=5),
            timeout=5)

        def _read() -> bytes:
            return stdout.read()

        raw = await asyncio.wait_for(asyncio.to_thread(_read), timeout=5)
        home = raw.decode("utf-8", "replace").strip()
    except Exception:
        home = ""

    remote_dir = f"{home}/wc-qa-checkout" if home else QA_REMOTE_PATH

    result = await transport_sync.sync_transport(
        prepared.machine_id, remote_dir,
        prepared.transport.get("last_qa_synced_sha") or "")
    if result["ok"] and result["head_sha"]:
        await db.ssh_transport_set_last_qa_synced_sha(
            prepared.transport["id"], result["head_sha"])
    return result


# Kept as literal fragments, not re-derived from bin/run-suite-chunked.sh --
# the two files cannot share code across bash/Python, so parity is pinned
# by a test asserting both contain these same substrings (spec §6: "the
# two runners can never define 'a chunk' two different ways").
_COLLECT_CMD_FRAGMENT = "pytest --collect-only -q"
_BROWSER_GREP_FRAGMENT = "grep -ln 'playwright\\|sync_playwright'"
_CHUNK_GROUP_SIZE = 6


async def _collect_chunks(machine_id: str) -> tuple[list[list[str]], list[str]]:
    """(plain_file_groups, browser_files), discovered remotely so the list
    reflects what is actually on the synced checkout. Mirrors
    run-suite-chunked.sh's own rule exactly: the file list comes from
    pytest's own collection, never a glob; browser files run one at a time;
    everything else in groups of _CHUNK_GROUP_SIZE. Also mirrors its two
    anti-false-green guards: refuse on zero collected files, and cross-check
    the planned chunk count against what was actually collected -- a remote
    collection that comes back empty or partial must not silently report
    run-done ok:true with all-zero totals.

    Raises RuntimeError on either guard tripping. Uncaught here deliberately:
    it propagates up through execute()'s try/finally (lock still released)
    and out to routes/qa.py's event_stream(), which already converts any
    mid-run exception into a terminal run-done ok:false event -- the same
    path a transport dying mid-chunk already takes, so this needs no new
    event type.
    """
    import tunnel_manager_ssh

    collect_timeout = 60
    collect_cmd = (
        f"cd {QA_REMOTE_PATH} && timeout {collect_timeout}s .venv/bin/python "
        f"-m {_COLLECT_CMD_FRAGMENT} 2>/dev/null | grep -oE '^[^:]+\\.py' | sort -u"
    )
    try:
        _, stdout, _ = await asyncio.wait_for(
            tunnel_manager_ssh.exec_command(machine_id, collect_cmd, timeout=collect_timeout),
            timeout=collect_timeout)

        def _read_collect() -> bytes:
            return stdout.read()

        raw = await asyncio.wait_for(asyncio.to_thread(_read_collect), timeout=collect_timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("pytest --collect-only timed out on the remote checkout") from exc
    all_files = [
        line.strip() for line in raw.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]

    if not all_files:
        raise RuntimeError(
            "pytest collected no files on the remote checkout -- refusing "
            "to report a green run")

    browser_timeout = 30
    browser_cmd = (
        f"cd {QA_REMOTE_PATH} && timeout {browser_timeout}s {_BROWSER_GREP_FRAGMENT} "
        f"{' '.join(all_files)} 2>/dev/null | sort"
    )
    try:
        _, stdout, _ = await asyncio.wait_for(
            tunnel_manager_ssh.exec_command(machine_id, browser_cmd, timeout=browser_timeout),
            timeout=browser_timeout)

        def _read_browser() -> bytes:
            return stdout.read()

        raw = await asyncio.wait_for(asyncio.to_thread(_read_browser), timeout=browser_timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("browser-file detection timed out on the remote checkout") from exc
    browser_files = [
        line.strip() for line in raw.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]

    browser_set = set(browser_files)
    plain_files = [f for f in all_files if f not in browser_set]
    plain_chunks = [
        plain_files[i:i + _CHUNK_GROUP_SIZE]
        for i in range(0, len(plain_files), _CHUNK_GROUP_SIZE)
    ]

    planned = sum(len(c) for c in plain_chunks) + len(browser_files)
    if planned != len(all_files):
        raise RuntimeError(
            f"{len(all_files)} files collected but {planned} planned -- refusing to run")

    return plain_chunks, browser_files


@dataclass
class ChunkResult:
    name: str
    status: str  # "passed" | "test_failure" | "transport_error" | "capacity_refused"
    output: str
    returncode: int | None


async def _run_chunk(
    machine_id: str, name: str, files: list[str], timeout: int, floor_mb: int,
) -> ChunkResult:
    """Checked again immediately before running -- load can shift mid-run on
    a shared transport (spec §3). A capacity_refused chunk never reaches
    exec_command at all, so it can never be confused with a transport_error
    (SSH actually failing) or a test_failure (pytest actually ran).

    The remote command is prefixed with a shell `timeout` -- paramiko's own
    exec_command(timeout=) is a per-recv inactivity timeout, not a wall-clock
    cap, and pytest's continuous progress output means a genuinely stuck
    chunk would never trip it without this. See _available_mb's docstring
    for why the exec_command call and the reads are each wrapped in
    asyncio.wait_for/asyncio.to_thread.
    """
    import tunnel_manager_ssh

    ok, available = await _check_capacity(machine_id, floor_mb)
    if not ok:
        seen = f"{available} MB" if available is not None else "unknown"
        return ChunkResult(
            name, "capacity_refused",
            f"only {seen} available, floor is {floor_mb} MB", None)

    file_args = " ".join(files)
    cmd = (
        f"cd {QA_REMOTE_PATH} && timeout {timeout}s .venv/bin/python "
        f"-m pytest {file_args} -q --tb=short"
    )
    try:
        _, stdout, stderr = await asyncio.wait_for(
            tunnel_manager_ssh.exec_command(machine_id, cmd, timeout=timeout),
            timeout=timeout)

        def _read() -> tuple[bytes, bytes, int]:
            out = stdout.read()
            err = stderr.read()
            rc = stdout.channel.recv_exit_status()
            return out, err, rc

        out_bytes, err_bytes, rc = await asyncio.wait_for(
            asyncio.to_thread(_read), timeout=timeout)
    except Exception as exc:
        return ChunkResult(name, "transport_error", str(exc), None)
    out = out_bytes.decode("utf-8", "replace")
    err = err_bytes.decode("utf-8", "replace")
    status = "passed" if rc == 0 else "test_failure"
    return ChunkResult(name, status, out + err, rc)


async def execute(prepared: Prepared) -> AsyncIterator[dict]:
    """Sync, then every chunk, yielding one event per step. Holds
    _run_lock(prepared.machine_id) for the whole call -- released in
    `finally` whether the run finishes, fails, or the caller stops
    consuming early (an aborted HTTP connection cancels this generator,
    which still runs `finally`).

    Lock acquisition is check-then-acquire, not a blocking `await
    lock.acquire()`: Task 3's resolve_transport only checks lock.locked()
    before returning success, it never acquires the lock itself, so two
    concurrent requests can both pass that check for the same machine
    before either reaches here. asyncio is single-threaded and
    cooperative, so an uncontended lock.acquire() does not suspend --
    a .locked() check immediately followed by .acquire() with no
    intervening await is atomic against other coroutines. This keeps a
    second run targeting a locked transport refused immediately, per
    spec §5, rather than silently queued behind the first."""
    import config

    lock = _run_lock(prepared.machine_id)
    if lock.locked():
        yield {"type": "run-done", "ok": False,
               "reason": f"{prepared.transport['name']} already has a QA run in "
                         f"progress — wait for it or pick another transport"}
        return
    await lock.acquire()
    try:
        yield {"type": "sync-start", "transport": prepared.transport["name"]}
        sync_result = await _sync(prepared)
        if not sync_result["ok"]:
            yield {"type": "run-done", "ok": False, "reason": sync_result["reason"]}
            return
        yield {"type": "sync-done", "files_changed": sync_result["files_changed"]}

        plain_chunks, browser_files = await _collect_chunks(prepared.machine_id)
        totals = {"passed": 0, "test_failure": 0, "transport_error": 0,
                  "capacity_refused": 0}

        async def _run_and_report(name: str, files: list[str]):
            yield {"type": "chunk-start", "name": name, "files": files}
            result = await _run_chunk(
                prepared.machine_id, name, files, config.QA_CHUNK_TIMEOUT,
                prepared.floor_mb)
            totals[result.status] += 1
            yield {
                "type": "chunk-result", "name": result.name,
                "status": result.status, "output": result.output,
            }

        for i, files in enumerate(plain_chunks, start=1):
            async for event in _run_and_report(f"plain-{i:02d}", files):
                yield event
        for f in browser_files:
            name = f"browser-{f.rsplit('/', 1)[-1].removesuffix('.py')}"
            async for event in _run_and_report(name, [f]):
                yield event

        ok = totals["test_failure"] == 0 and totals["transport_error"] == 0
        yield {"type": "run-done", "ok": ok, "totals": totals}
    finally:
        lock.release()
