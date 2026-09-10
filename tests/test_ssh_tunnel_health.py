"""Unit tests for tunnel_manager_health — probe and stats collection.

Covers: probe_proxy (faked), collect_stats. Requires paramiko.

Every test here drives a coroutine through `_run`, which creates a loop rather
than fetching one. `asyncio.get_event_loop()` raises once any
IsolatedAsyncioTestCase file has run before this one, and it always had in a
full-suite pass -- all five tests failed with "There is no current event loop"
purely because of what ran before them. The same defect cost
tests/test_ssh_tunnel_api.py eleven silently-skipped tests; here it failed
loudly, which was luckier.
"""
import asyncio

import pytest


pytest.importorskip("paramiko")


def _run(coro):
    """Drive *coro* on a loop of this test's own, and clean it up."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_probe_proxy_no_tunnel():
    """probe_proxy returns False when no tunnel exists."""
    import tunnel_manager

    tunnel_manager._STATE.clear()
    from tunnel_manager_health import probe_proxy
    result = _run(probe_proxy("no_such_machine"))
    assert result is False


def test_probe_proxy_no_port():
    """probe_proxy returns False when tunnel_up but no local_port."""
    import tunnel_manager

    tunnel_manager._STATE.clear()
    tunnel_manager._STATE["m_probe"] = {
        "tunnel_up": 1, "state": "connected",
    }
    from tunnel_manager_health import probe_proxy
    result = _run(probe_proxy("m_probe"))
    assert result is False


def test_probe_proxy_refused():
    """probe_proxy returns False when TCP connection is refused."""
    import tunnel_manager

    tunnel_manager._STATE.clear()
    tunnel_manager._STATE["m_probe2"] = {
        "tunnel_up": 1, "state": "connected",
        "local_port": 19999,
    }
    from tunnel_manager_health import probe_proxy
    result = _run(probe_proxy("m_probe2"))
    assert result is False


def test_collect_stats_no_tunnel():
    """collect_stats returns empty dict when no tunnel exists."""
    import tunnel_manager

    tunnel_manager._STATE.clear()
    from tunnel_manager_health import collect_stats
    result = _run(collect_stats("no_such_machine"))
    assert result == {}


def test_collect_stats_no_ssh_client():
    """collect_stats returns empty dict when no ssh_client in state."""
    import tunnel_manager

    tunnel_manager._STATE.clear()
    tunnel_manager._STATE["m_stats"] = {
        "tunnel_up": 1, "state": "connected",
    }
    from tunnel_manager_health import collect_stats
    result = _run(collect_stats("m_stats"))
    assert result == {}
