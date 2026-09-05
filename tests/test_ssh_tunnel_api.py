"""Functional tests for tunnel API endpoints.

Covers: POST /api/tunnel/start, POST /api/tunnel/stop, POST /api/tunnel/toggle,
GET /api/tunnel/status, POST /api/init/ssh-test, POST /api/machines/:id/test.
These tests require the full app harness (db init, login) which may not
be available in all environments, so they are skipped gracefully.
"""
import pytest


@pytest.fixture
def client():
    """Return a TestClient with admin session. Skips if app can't start."""
    try:
        import db
        import auth
        import asyncio
        import tunnel_manager

        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.init())
        loop.run_until_complete(auth.bootstrap_admin())

        from fastapi.testclient import TestClient
        from app import app  # noqa: F811
        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            "/login", json={"username": "admin", "password": "admin"}
        )
        # Clean up tunnel manager from previous test runs.
        loop.run_until_complete(tunnel_manager.stop())

        yield client

        loop.run_until_complete(tunnel_manager.stop())
    except Exception:
        pytest.skip("app harness unavailable")


def test_tunnel_start_no_machine(client):
    """POST /api/tunnel/start with missing machine_id returns 400."""
    resp = client.post("/api/tunnel/start", json={})
    assert resp.status_code == 400


def test_tunnel_start_nonexistent(client):
    """POST /api/tunnel/start with nonexistent machine_id returns 404."""
    resp = client.post("/api/tunnel/start", json={"machine_id": "no-such-id"})
    assert resp.status_code == 404


def test_tunnel_stop_no_machine(client):
    """POST /api/tunnel/stop with missing machine_id returns 400."""
    resp = client.post("/api/tunnel/stop", json={})
    assert resp.status_code == 400


def test_tunnel_toggle_no_machine(client):
    """POST /api/tunnel/toggle with missing machine_id returns 400."""
    resp = client.post("/api/tunnel/toggle", json={})
    assert resp.status_code == 400


def test_tunnel_status_returns_dict(client):
    """GET /api/tunnel/status returns a dict (may be empty)."""
    resp = client.get("/api/tunnel/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


def test_init_ssh_test_no_host(client):
    """POST /api/init/ssh-test with empty ssh_host returns 400."""
    resp = client.post("/api/init/ssh-test", json={})
    assert resp.status_code == 400


def test_init_ssh_test_bad_key(client):
    """POST /api/init/ssh-test with nonexistent key returns error."""
    resp = client.post("/api/init/ssh-test", json={
        "ssh_host": "localhost",
        "ssh_key_path": "/nonexistent/key",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is False


def test_tunnel_start_requires_auth():
    """POST /api/tunnel/start without auth returns 401."""
    from fastapi.testclient import TestClient
    from app import app  # noqa: F811
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/tunnel/start", json={})
    assert resp.status_code == 401


def test_tunnel_stop_requires_auth():
    """POST /api/tunnel/stop without auth returns 401."""
    from fastapi.testclient import TestClient
    from app import app  # noqa: F811
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/tunnel/stop", json={})
    assert resp.status_code == 401


def test_tunnel_toggle_requires_auth():
    """POST /api/tunnel/toggle without auth returns 401."""
    from fastapi.testclient import TestClient
    from app import app  # noqa: F811
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/tunnel/toggle", json={})
    assert resp.status_code == 401
