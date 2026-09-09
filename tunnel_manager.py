"""Tunnel manager — background SSH tunnel lifecycle loop.

One event-loop task, one state dict, one command queue. Each active
ssh_proxy machine gets one entry in the state dict; the loop iterates
per-machine on each pass. Upgrading to per-machine threads is trivial
(a ``for`` loop spawning one coroutine per machine); the per-machine
interface is already ``(machine_id) -> state dict``.

Split boundary: tunnel state management (this file) <-> SSH transport
(tunnel_manager_ssh) <-> health probe (tunnel_manager_health).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Final

import config

_log = logging.getLogger("wc.tunnel_manager")

_STATE: dict[str, dict] = {}
# transport_id -> {"ssh_client", "transport", "refcount"}. Several machines
# can share one live SSH connection when they reference the same
# ai_machines.transport_id; this is the registry that makes the second (and
# later) connect() calls reuse it instead of opening a new handshake.
_TRANSPORT_CONNECTIONS: dict[str, dict] = {}
_TRANSPORT_LOCKS: dict[str, asyncio.Lock] = {}
# machine_ids with an in-flight _try_connect() whose _run() hasn't finished
# yet -- guards against two RECONNECT/START_TUNNEL actions for the same
# machine close together each spawning their own _run() task, which would
# both connect successfully and both increment the same transport's shared
# refcount for only one machine actually in use.
_CONNECTING: set[str] = set()
_queue: asyncio.Queue = asyncio.Queue()
_port_lock: asyncio.Lock = asyncio.Lock()
_task: asyncio.Task | None = None
_running: bool = False

# Exponential backoff for SSH connect retries (seconds).
_BACKOFF_BASE: Final[int] = 1
_BACKOFF_MAX: Final[int] = 60


def _default_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def start(store_fn, _now_fn=None) -> None:
    """Boot the loop on the event loop.

    Scans ssh_tunnels for active machines matching the boot-time
    owner context and attempts reconnects (backoff from step 1).
    """
    global _task, _running
    import db
    if _task and not _task.done():
        return  # idempotent
    _running = True
    # Auto-activate: read active ssh_proxy machines, attempt reconnect.
    # Use a plain sqlite3.Connection (not the shared async db.db_conn)
    # because db.db_conn is already checked out by the lifespan coroutine
    # that calls start(), so awaiting it here would deadlock.
    try:
        import sqlite3
        import config
        db_path = config.DB_PATH if config.DB_PATH else "/dev/null"
        scan_conn = sqlite3.connect(":memory:")
        scan_conn.execute("ATTACH DATABASE ? AS target", (db_path,))
        cursor = scan_conn.execute(
            "SELECT id, machine_id, state FROM target.ssh_tunnels "
            "WHERE tunnel_up = 1 ORDER BY id"
        )
        rows = cursor.fetchall()
        scan_conn.close()
        for row in rows:
            _queue.put_nowait(("RECONNECT", str(row[1])))
    except Exception:
        _log.exception("tunnel scan failed")
    _task = asyncio.create_task(_loop(store_fn, _now_fn or _default_now))


async def stop() -> None:
    """Set _running=False, drain queue, close all SSH clients."""
    global _running, _task, _queue
    _running = False
    if _task and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    for machine_id in list(_STATE):
        _release_machine(machine_id)
    # Otherwise a stranded entry (e.g. a connect that never got the chance
    # to hit _try_connect's finally before this stop() ran) would silently
    # disable _try_connect for that machine_id across a restart/reboot.
    _CONNECTING.clear()
    # A fresh Queue, not a drain of the old one. asyncio.Queue binds to
    # whichever event loop first awaits get()/put() on it, and this module
    # holds it at module scope -- every test file that starts the manager,
    # each under its own event loop (unittest.IsolatedAsyncioTestCase makes
    # a new one per test), left the *old* loop's binding on the queue after
    # a drain, so the next test to touch it hit
    # "RuntimeError: <Queue...> is bound to a different event loop" even
    # though it never held a live task at that point. Only surfaced once
    # tunnel_manager.start() actually ran for the first time anywhere (see
    # app.py's missing-await fix) -- before that nothing had ever awaited
    # the queue at all, so it had never bound to anything.
    _queue = asyncio.Queue()


async def queue_command(machine_id: str, action: str) -> None:
    """Enqueue START_TUNNEL / STOP_TUNNEL / RECONNECT."""
    await _queue.put((action, machine_id))


async def tunnel_status(machine_id: str) -> dict | None:
    """Return current tunnel state dict or None."""
    return _STATE.get(machine_id)


# ── Loop ────────────────────────────────────────────────────────────────────

async def _loop(store_fn, now_fn) -> None:
    """Main loop. Iterates per-machine each cycle.

    Command queue drains first. Then per-machine: connect if down,
    health-check if up, collect stats on interval. Sleep
    TUNNEL_HEALTH_INTERVAL_S or until a command arrives.
    """
    while _running:
        # Drain commands.
        while not _queue.empty():
            try:
                cmd, machine_id = _queue.get_nowait()
                _handle_command(cmd, machine_id, now_fn=now_fn)
            except asyncio.QueueEmpty:
                break
            except Exception:
                _log.exception("command error")

        try:
            # Per-machine health/stats.
            await _tick(store_fn=store_fn, now_fn=now_fn)
        except Exception:
            _log.exception("tick error")

        # Sleep, interruptible by queue events.
        await asyncio.sleep(config.TUNNEL_HEALTH_INTERVAL_S)


def _handle_command(action: str, machine_id: str, now_fn=None) -> None:
    """Process a queued command against *machine_id*."""
    now = now_fn() if now_fn else time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )

    if action == "START_TUNNEL":
        if machine_id not in _STATE:
            _STATE[machine_id] = {
                "state": "connecting",
                "tunnel_up": 0,
                "proxy_ok": 0,
                "error_msg": None,
                "connected_at": None,
                "last_check": now,
            }
        _queue.put_nowait(("RECONNECT", machine_id))

    elif action == "STOP_TUNNEL":
        if machine_id in _STATE:
            _STATE[machine_id]["state"] = "disconnected"
            _STATE[machine_id]["tunnel_up"] = 0
            _STATE[machine_id]["connected_at"] = None
            _STATE[machine_id]["error_msg"] = None
            _STATE[machine_id]["last_check"] = now
            _release_machine(machine_id)

    elif action == "RECONNECT":
        state = _STATE.get(machine_id)
        if not state:
            # Startup scan seeds RECONNECTs into an empty _STATE — create
            # the entry so _try_connect() can attempt the connection.
            _STATE[machine_id] = {
                "state": "connecting",
                "tunnel_up": 0,
                "proxy_ok": 0,
                "error_msg": None,
                "connected_at": None,
                "last_check": now,
            }
            _try_connect(machine_id)
            return
        # Release this machine's own prior claim on a shared transport (if
        # any) before reconnecting -- reconnecting without releasing first
        # double-counts the shared refcount every time a health-check retry
        # or a START_TUNNEL fires this, and the underlying connection is
        # then never actually closed (it outlives every machine still using
        # it, and survives stop()). Symmetric with STOP_TUNNEL's
        # release-before-clearing-state above.
        _release_machine(machine_id)
        _STATE[machine_id] = {
            "state": "connecting",
            "tunnel_up": 0,
            "proxy_ok": 0,
            "error_msg": None,
            "connected_at": None,
            "last_check": now,
        }
        _try_connect(machine_id)


def _release_machine(machine_id: str) -> None:
    """Release tunnel state dict entry and local port forward.

    The forward server is always this machine's own -- always stopped. The
    underlying SSH client/transport may be shared with other machines on the
    same transport_id; only actually closed when this was the last one still
    using it (refcount reaches zero).
    """
    state = _STATE.pop(machine_id, None)
    if not state:
        return
    forward_server = state.get("forward_server")
    if forward_server:
        # Before the transport it forwards over: closing the transport
        # first would just make every in-flight forwarded connection error
        # out through the transport instead of a clean local shutdown.
        import tunnel_manager_forward
        with contextlib.suppress(Exception):
            tunnel_manager_forward.stop_forward(forward_server)

    transport_id = state.get("transport_id")
    if not transport_id:
        # No shared registry entry (e.g. a machine with no transport_id at
        # all never went through it) -- close directly, same as before.
        _close_ssh(state.get("transport"), state.get("ssh_client"))
        return

    shared = _TRANSPORT_CONNECTIONS.get(transport_id)
    if not shared:
        _close_ssh(state.get("transport"), state.get("ssh_client"))
        return
    shared["refcount"] -= 1
    if shared["refcount"] <= 0:
        _close_ssh(shared.get("transport"), shared.get("ssh_client"))
        _TRANSPORT_CONNECTIONS.pop(transport_id, None)


def _close_ssh(transport, ssh_client) -> None:
    if transport:
        with contextlib.suppress(Exception):
            transport.close()
    if ssh_client:
        with contextlib.suppress(Exception):
            ssh_client.close()


def _transport_lock(transport_id: str) -> asyncio.Lock:
    """One lock per transport_id, created on first use. Guards the whole
    check-connect-store sequence in tunnel_manager_ssh.connect() against two
    machines racing to become "the first connection" on the same transport:
    without it, both could see no live entry, both perform a real paramiko
    handshake, and the second write clobbers (orphans, never closed) the
    first -- and releasing either machine could then drive the *other's*
    still-live connection to refcount zero and close it out from under it.
    Never cleaned up -- the number of transports a user configures is small
    and this is a cheap resource, not worth the complexity of tearing down.
    """
    lock = _TRANSPORT_LOCKS.get(transport_id)
    if lock is None:
        lock = asyncio.Lock()
        _TRANSPORT_LOCKS[transport_id] = lock
    return lock


async def _tick(store_fn, now_fn) -> None:
    """One cycle: health-check + stats for each machine."""
    from tunnel_manager_health import collect_stats, probe_proxy

    for machine_id, state in list(_STATE.items()):
        if state.get("state") == "connected":
            ok = await probe_proxy(machine_id)
            if ok:
                state["proxy_ok"] = 1
                state["state"] = "connected"
                state["last_check"] = now_fn()
                state["error_msg"] = None
            else:
                state["state"] = "error"
                state["last_check"] = now_fn()
                state["error_msg"] = "Proxy health probe failed"
                state["proxy_ok"] = 0

        elif state.get("state") == "error" or (
            state.get("state") == "connecting" and machine_id not in _CONNECTING
        ):
            # The second condition is a stranded machine: "connecting" but
            # nothing is actually running for it anymore -- e.g. a RECONNECT
            # drained while a just-finishing _try_connect() had already
            # written "connected" and was awaiting its DB update, still
            # holding _CONNECTING, so the RECONNECT's own _try_connect() call
            # no-op'd (guard still set from the *first* connect) and no task
            # was ever spawned for the fresh "connecting" state it wrote.
            # Nothing else drives a plain "connecting" state forward, so
            # without this it would sit there forever. Folding it into the
            # same retry path "error" already uses -- rather than a separate
            # branch -- means one attempt at recovery, not two copies of the
            # backoff logic.
            backoff = min(
                state.get("_backoff", _BACKOFF_BASE) * 2, _BACKOFF_MAX
            )
            await asyncio.sleep(backoff)
            # Release this machine's own prior claim on a shared transport
            # (if any) before retrying -- same reasoning as the RECONNECT
            # command: retrying without releasing first leaks the shared
            # refcount on every backoff cycle, and the underlying connection
            # is then never actually closed.
            _release_machine(machine_id)
            _STATE[machine_id] = {
                "state": "connecting",
                "tunnel_up": 0,
                "proxy_ok": 0,
                "error_msg": None,
                "connected_at": None,
                "last_check": now_fn(),
                "_backoff": backoff,
            }
            _try_connect(machine_id)

        elif state.get("state") == "connecting":
            state["last_check"] = now_fn()

        # Stats collection on interval.
        if state.get("state") == "connected":
            tick_count = state.get("_tick_count", 0) + 1
            state["_tick_count"] = tick_count
            stats_interval = config.TUNNEL_STATS_INTERVAL_S // config.TUNNEL_HEALTH_INTERVAL_S
            stats_interval = max(stats_interval, 1)
            if tick_count % stats_interval == 0:
                await collect_stats(machine_id, store_fn=store_fn)


def _try_connect(machine_id: str) -> None:
    """Attempt SSH connect and port forward. Calls into tunnel_manager_ssh."""
    import asyncio

    if machine_id in _CONNECTING:
        # Already connecting -- a second RECONNECT/START_TUNNEL for the same
        # machine while one is still in flight must not spawn a second
        # _run(): both would connect successfully and both increment the
        # same transport's shared refcount for only one machine actually in
        # use, leaving a phantom claim that never releases.
        return
    _CONNECTING.add(machine_id)

    async def _run():
        from tunnel_manager_ssh import connect as _connect

        try:
            ok, client, transport, local_port, ssh_port, forward_server, transport_id = (
                await _connect(machine_id)
            )
            if machine_id not in _STATE:
                # Stopped/removed while this connect was in flight (e.g.
                # STOP_TUNNEL fired during "connecting"). Nobody holds a
                # claim to release this result anymore -- release it
                # ourselves, or a successful connect leaks its own forward
                # server and registry claim forever, poisoning the
                # transport (refcount never reaches zero) for every future
                # machine that shares it.
                if ok:
                    if forward_server:
                        import tunnel_manager_forward
                        with contextlib.suppress(Exception):
                            tunnel_manager_forward.stop_forward(forward_server)
                    if transport_id:
                        shared = _TRANSPORT_CONNECTIONS.get(transport_id)
                        if shared:
                            shared["refcount"] -= 1
                            if shared["refcount"] <= 0:
                                _close_ssh(
                                    shared.get("transport"), shared.get("ssh_client")
                                )
                                _TRANSPORT_CONNECTIONS.pop(transport_id, None)
                return
            if ok:
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _STATE[machine_id].update({
                    "state": "connected",
                    "tunnel_up": 1,
                    "proxy_ok": 0,
                    "ssh_client": client,
                    "transport": transport,
                    "local_port": local_port,
                    "ssh_port": ssh_port,
                    "forward_server": forward_server,
                    "transport_id": transport_id,
                    "connected_at": now,
                    "last_check": now,
                    "error_msg": None,
                    "_backoff": _BACKOFF_BASE,
                })
                import db
                try:
                    await db.ssh_tunnel_update(
                        machine_id,
                        local_port=local_port,
                        ssh_port=ssh_port,
                        tunnel_up=1,
                        state="connected",
                    )
                except Exception:
                    _log.warning("tunnel update failed for %s", machine_id)
            else:
                _STATE[machine_id]["state"] = "error"
                _STATE[machine_id]["last_check"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
                _log.warning(
                    "tunnel_connect_failed machine=%s reason=%s",
                    machine_id,
                    _STATE[machine_id].get("error_msg", "unknown"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("connect_task error for %s", machine_id)
        finally:
            _CONNECTING.discard(machine_id)

    try:
        asyncio.create_task(_run())
    except Exception:
        # create_task itself failing (rare) means _run()'s own finally never
        # gets a chance to run -- without this, the guard added above would
        # outlive the task it was meant to guard, permanently no-op'ing
        # every future _try_connect() for this machine_id.
        _CONNECTING.discard(machine_id)
        raise
