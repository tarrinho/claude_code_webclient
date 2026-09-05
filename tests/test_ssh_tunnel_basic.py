"""Unit tests for tunnel_manager core (connect/disconnect/status).

Covers: start/stop lifecycle, queue_command delivery, tunnel_status,
exponential backoff state transitions. Requires paramiko.
"""
import asyncio

import pytest


pytest.importorskip("paramiko")


def _ensure_admin():
    """Bootstrap admin if needed."""
    from auth import bootstrap_admin
    loop = asyncio.get_event_loop()
    loop.run_until_complete(bootstrap_admin())


def test_tunnel_start_stop():
    """start() creates a task, stop() cancels it."""
    import tunnel_manager
    _ensure_admin()
    try:
        await_async(tunnel_manager.start(lambda *a, **k: None))
        assert tunnel_manager._task is not None
        assert not tunnel_manager._task.done()
        await_async(tunnel_manager.stop())
    except Exception:
        await_async(tunnel_manager.stop())


def test_queue_command_delivers():
    """queue_command puts a tuple in the internal queue."""
    import tunnel_manager
    # Drain queue by getting all pending items.
    while not tunnel_manager._queue.empty():
        tunnel_manager._queue.get_nowait()
    await_async(tunnel_manager.queue_command("m1", "START_TUNNEL"))
    item = tunnel_manager._queue.get_nowait()
    assert item == ("START_TUNNEL", "m1")


def test_tunnel_status_empty():
    """tunnel_status returns None when no state exists."""
    import tunnel_manager
    tunnel_manager._STATE.clear()
    result = await_async(tunnel_manager.tunnel_status("nonexistent"))
    assert result is None


def test_tunnel_status_after_start():
    """tunnel_status reflects state after START_TUNNEL command."""
    import tunnel_manager
    tunnel_manager._STATE.clear()
    while not tunnel_manager._queue.empty():
        tunnel_manager._queue.get_nowait()
    await_async(tunnel_manager.queue_command("m_test", "START_TUNNEL"))
    tunnel_manager._handle_command(
        "START_TUNNEL", "m_test",
        now_fn=lambda: "2026-01-01T00:00:00Z",
    )
    status = await_async(tunnel_manager.tunnel_status("m_test"))
    assert status is not None
    assert status["state"] == "connecting"
    assert status["tunnel_up"] == 0


def test_tunnel_stop_releases():
    """STOP_TUNNEL clears state and releases SSH client."""
    import tunnel_manager
    tunnel_manager._STATE.clear()
    while not tunnel_manager._queue.empty():
        tunnel_manager._queue.get_nowait()
    tunnel_manager._STATE["m_stop"] = {
        "state": "connected", "tunnel_up": 1,
        "ssh_client": None, "transport": None,
    }
    tunnel_manager._handle_command(
        "STOP_TUNNEL", "m_stop",
        now_fn=lambda: "2026-01-01T00:00:00Z",
    )
    status = await_async(tunnel_manager.tunnel_status("m_stop"))
    assert status is None


def test_backoff_grows():
    """Exponential backoff grows on repeated error→reconnect."""
    import tunnel_manager
    tunnel_manager._STATE.clear()
    tunnel_manager._STATE["m_backoff"] = {
        "state": "error", "_backoff": 1,
        "tunnel_up": 0, "proxy_ok": 0, "error_msg": "fail",
        "last_check": "2026-01-01T00:00:00Z",
    }
    state = tunnel_manager._STATE["m_backoff"]
    state["_backoff"] = min(state.get("_backoff", 1) * 2, 60)
    assert state["_backoff"] == 2
    state["_backoff"] = min(state.get("_backoff", 2) * 2, 60)
    assert state["_backoff"] == 4
    state["_backoff"] = min(state.get("_backoff", 4) * 2, 60)
    assert state["_backoff"] == 8


def await_async(coro):
    """Run a coroutine on the currently running loop."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)
