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
_queue: asyncio.Queue = asyncio.Queue()
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
    try:
        cursor = await db.db_conn.execute(
            "SELECT id, machine_id, state FROM ssh_tunnels "
            "WHERE tunnel_up = 1 ORDER BY id"
        )
        # aiosqlite.Cursor has __aiter__ but not __iter__ -- a plain `for`
        # over the cursor itself raises TypeError immediately, silently
        # caught by the except below (same shape as db.ssh_tunnel_get's
        # missing-await bug found alongside this one). Harmless while
        # ssh_tunnels has no tunnel_up=1 rows yet, since nothing was there
        # to reconnect either way, but would have blocked every reconnect
        # attempt after a restart once one existed.
        rows = await cursor.fetchall()
        for row in rows:
            _queue.put_nowait(("RECONNECT", str(row["machine_id"])))
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
            return
        state["state"] = "connecting"
        state["tunnel_up"] = 0
        state["error_msg"] = None
        state["last_check"] = now
        _try_connect(machine_id)


def _release_machine(machine_id: str) -> None:
    """Release tunnel state dict entry and local port forward."""
    state = _STATE.pop(machine_id, None)
    if not state:
        return
    ssh_client = state.get("ssh_client")
    transport = state.get("transport")
    if transport:
        with contextlib.suppress(Exception):
            transport.close()
    if ssh_client:
        with contextlib.suppress(Exception):
            ssh_client.close()


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

        elif state.get("state") in ("connecting", "error"):
            if state.get("state") == "error":
                state["_backoff"] = min(
                    state.get("_backoff", _BACKOFF_BASE) * 2, _BACKOFF_MAX
                )
                await asyncio.sleep(state["_backoff"])
                _try_connect(machine_id)
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

    async def _run():
        from tunnel_manager_ssh import connect as _connect

        try:
            ok, client, transport, local_port, ssh_port = await _connect(
                machine_id
            )
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

    asyncio.create_task(_run())
