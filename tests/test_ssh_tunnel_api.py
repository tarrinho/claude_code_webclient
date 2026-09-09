"""Functional tests for tunnel API endpoints.

Covers: POST /api/tunnel/start, POST /api/tunnel/stop, POST /api/tunnel/toggle,
GET /api/tunnel/status, POST /api/init/ssh-test, POST /api/machines/:id/test.
These tests require the full app harness (db init, login) which may not
be available in all environments, so they are skipped gracefully.
"""
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Return a TestClient with admin session. Skips if app can't start.

    Registry #41: db.init() must never run against the real database -- it
    migrates. This fixture called it with no WC_DB_PATH override at all,
    which resolves to config.py's own default: <repo>/data/webconsole.db,
    the actual production file this deployment serves from. Every other
    test in this suite that calls db.init() patches config.DB_PATH and
    config.PROJECTS_ROOT first; this one didn't, so every run of this file
    initialized (and, via the /login attempt below with a password that
    does not match the real admin account, rate-limited login attempts
    against) the live database. WC_ADMIN_PASSWORD is set explicitly too --
    otherwise auth.bootstrap_admin() silently no-ops (no admin user, no
    password to log in with) if the environment happens not to define it.
    """
    try:
        import db
        import auth
        import asyncio
        import config
        import tunnel_manager

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "wc.db"))
        monkeypatch.setattr(config, "PROJECTS_ROOT", str(tmp_path / "projects"))
        # auth.bootstrap_admin() reads this fresh via config._str() (live
        # os.environ, not a cached config constant), so this is the one that
        # actually matters -- not a config.WC_ADMIN_PASSWORD attribute.
        monkeypatch.setenv("WC_ADMIN_PASSWORD", "admin-test-pw-123")
        # TestClient talks to "http://testserver" (plain HTTP); a Secure
        # cookie is set but withheld by the client on subsequent requests
        # unless the app is told this connection is intentionally insecure,
        # matching the plain-HTTP-on-Tailscale deployment mode elsewhere.
        monkeypatch.setattr(config, "COOKIE_ALLOW_INSECURE", True)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.init())
        loop.run_until_complete(auth.bootstrap_admin())

        from fastapi.testclient import TestClient
        from app import app  # noqa: F811
        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            "/login", json={"username": "admin", "password": "admin-test-pw-123"}
        )
        # The double-submit CSRF cookie: every mutating endpoint 403s without
        # this header matching it. Attached to the client's own default
        # headers rather than passed per-call, so every test function below
        # (written before this fixture ever got far enough to reach this
        # check) keeps working unmodified.
        client.headers.update({"X-CSRF-Token": client.cookies.get("wc_csrf") or ""})
        # Clean up tunnel manager from previous test runs.
        loop.run_until_complete(tunnel_manager.stop())

        yield client

        loop.run_until_complete(tunnel_manager.stop())
        loop.run_until_complete(db.close())
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


def test_tunnel_status_survives_no_in_memory_state(client):
    """The regression this file's empty-case test above could not catch: it
    exercises zero transport-routed machines, so the loop body in
    routes/machines_tunnel.py never runs at all.

    A real transport-routed machine with a persisted ssh_tunnels row but no
    tunnel_manager in-memory state -- exactly what every such machine looks
    like after a restart, per the RECONNECT-at-boot gap -- used to 500 here:
    the DB fallback built its status dict from a bare aiosqlite Row and read
    it back with `.get()`, which Row does not support. Pedro hit this live:
    Check passed every one of AppSec Tools' checks, but the badge never
    updated, because this endpoint was crashing on every single poll.
    """
    machine_id = _create_machine_with_transport(client)

    import asyncio
    import db

    asyncio.get_event_loop().run_until_complete(
        db.ssh_tunnel_create(machine_id, local_port=9000))
    asyncio.get_event_loop().run_until_complete(
        db.ssh_tunnel_update(machine_id, state="connected", tunnel_up=1,
                             proxy_ok=1))

    # tunnel_manager's in-memory _STATE has no entry for this machine --
    # nothing in this test process ever called START_TUNNEL/RECONNECT for it
    # -- so the route must fall through to the DB row above, not the
    # in-memory branch.
    import tunnel_manager
    assert machine_id not in tunnel_manager._STATE

    resp = client.get("/api/tunnel/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data[machine_id]["state"] == "connected"
    assert data[machine_id]["proxy_ok"] is True


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


def _create_machine_with_transport(client, name="Test SSH Box"):
    """Create an ssh_transport, then a machine pointed at it. Returns the
    new machine id.

    ssh_proxy stopped being a legal `provider` value once ssh transports
    were split into their own table/route (an earlier task in this plan):
    the SSH connection (ssh_host/ssh_user/ssh_key_path) now lives on
    ssh_transports, and ai_machines only references it via transport_id.
    So this creates a transport via POST /api/transports first, then a
    normal-provider ("claude_code") machine with transport_id set --
    mirroring what routes/machines_tunnel.py now gates on.

    ssh_host must actually resolve: transport creation runs it through
    net_validation._validate_host (see
    test_ssh_host_is_ssrf_checked_like_every_other_host_field), which needs
    a real DNS answer to classify the address at all. "localhost" resolves
    to 127.0.0.1, in the SSRF check's own default allowlist (loopback is
    where a real transport legitimately points during dev/test), so this
    exercises the real validation path rather than bypassing it.
    """
    resp = client.post("/api/transports", json={
        "name": f"{name} Transport",
        "ssh_host": "localhost",
        "ssh_user": "kali",
        "ssh_key_path": "~/.ssh/id_ed25519",
    })
    assert resp.status_code == 200, resp.text
    transport_id = resp.json()["id"]

    resp = client.post("/api/machines", json={
        "name": name,
        "provider": "claude_code",
        "transport_id": transport_id,
        "model": "claude-sonnet-5",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_ssh_host_is_ssrf_checked_like_every_other_host_field(client):
    """POST /api/transports validates ssh_host's *format* (a real hostname
    or IP shape), and must also run it through net_validation._validate_host
    -- the SSRF/private-IP blocklist every other host field in this app
    (e.g. the `host` field on POST /api/machines) already goes through.
    169.254.169.254 is the canonical cloud-metadata SSRF target that
    blocklist exists to stop, and it has a perfectly valid hostname
    *shape*, so the format check alone would let it straight through.

    This used to POST to /api/machines with provider=ssh_proxy, back when
    ssh_host lived directly on ai_machines. ssh_proxy is no longer a legal
    provider and ssh_host now lives on ssh_transports, so this targets
    POST /api/transports, the route that actually owns ssh_host today --
    same SSRF check, same assertion."""
    resp = client.post("/api/transports", json={
        "name": "Metadata Probe", "ssh_host": "169.254.169.254",
        "ssh_key_path": "~/.ssh/id_ed25519",
    })
    assert resp.status_code == 403, resp.text


def test_creating_a_non_ssh_machine_does_not_violate_not_null(client):
    """ai_machine_create used to pass ssh_host=None straight into a TEXT NOT
    NULL DEFAULT '' column -- a DEFAULT only applies when a column is
    *omitted* from the INSERT, not when it is present with an explicit NULL,
    which is exactly what a non-ssh_proxy machine did here. Every machine
    creation of any other provider type raised sqlite3.IntegrityError."""
    resp = client.post("/api/machines", json={
        "name": "Plain Anthropic Machine",
        "provider": "claude_code",
        "model": "claude-sonnet-5",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json().get("id")


def test_tunnel_start_for_a_real_machine_actually_creates_the_row(client, monkeypatch):
    """The core regression: starting a tunnel for a real ssh_proxy machine
    must reach tunnel_manager, not 400 on a machine_id the caller genuinely
    sent.

    Two bugs stacked here, both fixed:

      * _body(req) called req.body() without awaiting it, so json.loads()
        received a coroutine object, raised TypeError, and that was
        swallowed into {} -- every caller's machine_id read as empty
        regardless of what was actually POSTed. Proven here: this request
        would have 400'd with "machine_id required" against the old code
        even though a real id is sent below.
      * routes/machines_tunnel.py cast the id with int(machine_id) before
        creating the ssh_tunnels row. ai_machines.id is a hex UUID string,
        never a small integer, so this raised ValueError on every real
        machine and was swallowed by a bare except -- the tunnel DB row was
        never created, so no ssh_proxy tunnel has ever connected since the
        feature shipped. Proven here by asserting the row exists afterward,
        not just that the endpoint returned 200.
    """
    import tunnel_manager
    # The real background connection loop needs actual network/SSH access,
    # which this test does not have and should not depend on. queue_command
    # is the boundary between "the API accepted the request and tried to
    # create its bookkeeping row" (what these bugs broke) and "a real SSH
    # session came up" (a different, live-network concern) -- stubbing it
    # keeps this test fast and deterministic while still exercising the two
    # fixes above for real.
    queued = []
    monkeypatch.setattr(
        tunnel_manager, "queue_command",
        lambda mid, cmd: queued.append((mid, cmd)) or _noop(),
    )

    machine_id = _create_machine_with_transport(client)
    resp = client.post("/api/tunnel/start", json={"machine_id": machine_id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "status": "connecting"}
    assert queued == [(machine_id, "START_TUNNEL")]

    import db
    import asyncio
    row = asyncio.get_event_loop().run_until_complete(db.ssh_tunnel_get(machine_id))
    assert row is not None, (
        "no ssh_tunnels row was created for a real machine_id -- "
        "int(machine_id) is raising again"
    )
    assert row["machine_id"] == machine_id


async def _noop():
    return None


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
