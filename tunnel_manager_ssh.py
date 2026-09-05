"""SSH transport layer for tunnel manager.

Handles SSH client lifecycle, key file validation, port allocation,
and exec_command wrapper. Imported by tunnel_manager.py.

Split boundary: tunnel state management (tunnel_manager) <-> SSH transport
(this file) <-> health probe (tunnel_manager_health).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time

import tunnel_manager
import tunnel_manager_forward

_log = logging.getLogger("wc.tunnel.ssh")


def _fingerprint(key) -> str:
    """SHA256 fingerprint in the same format `ssh-keygen -lf` prints, so a
    value stored here is directly comparable to what a human checks by
    hand."""
    import base64
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class _PinnedHostKeyPolicy:
    """Trust-on-first-use, not blanket trust. paramiko.AutoAddPolicy (bandit
    B507, CWE-295) accepted any host key on every connection with no
    verification at all -- a MITM between this host and ssh_host was
    undetectable. This pins the key on the first successful connection and
    rejects any later connection whose key has changed, which is what
    actually catches a MITM or a reinstalled host. Deliberately not
    RejectPolicy against the account's own ~/.ssh/known_hosts: that would
    require every configured ssh_proxy host to already be manually
    SSH'd-to once from this account, which none of them are yet, and would
    re-break the connection this session's other fixes just got working.

    Runs inside paramiko's synchronous connect() (itself run via
    asyncio.to_thread), so it cannot await the DB write that persists a new
    fingerprint -- it only records what happened onto itself, in
    `new_fingerprint` / `mismatch`, for the async caller to act on once the
    (still synchronous) connect() call returns.
    """

    def __init__(self, stored_fingerprint: str):
        self.stored_fingerprint = stored_fingerprint or None
        self.new_fingerprint: str | None = None
        self.mismatch: tuple[str, str] | None = None

    def missing_host_key(self, client, hostname, key):
        fp = _fingerprint(key)
        if self.stored_fingerprint is None:
            self.new_fingerprint = fp
            return
        if fp != self.stored_fingerprint:
            self.mismatch = (self.stored_fingerprint, fp)
            # Not paramiko.SSHException: this class deliberately does not
            # import paramiko at module scope (this whole file defers that
            # import into the functions that need it, so the rest of it
            # stays usable without paramiko installed). Any exception here
            # aborts ssh_client.connect() the same way; ConnectionError
            # reads correctly to whatever catches it either way.
            raise ConnectionError(
                f"host key for {hostname} changed: expected "
                f"{self.stored_fingerprint}, got {fp} -- possible MITM, or "
                f"the host was reinstalled"
            )
        # Matches the pinned value: accepting means doing nothing here.


async def connect(machine_id: str):
    """Attempt SSH connect + port forward for *machine_id*.

    Returns (ok, ssh_client, transport, local_port, ssh_port, forward_server).
    forward_server is the tunnel_manager_forward server actually bridging
    127.0.0.1:local_port to claude_proxy.py on the remote side; the caller
    must hold onto it and pass it to tunnel_manager_forward.stop_forward()
    on disconnect, or the forwarding thread and its bound port both leak.
    """
    import paramiko

    import db

    # ssh_tunnels only carries tunnel bookkeeping (state, ports, owner_id) --
    # ssh_host/ssh_user/ssh_key_path live on ai_machines, the table the
    # Settings form actually writes them to. Reading them from the tunnel
    # row (a sqlite3.Row, whose .get() doesn't exist either -- indexing or
    # dict() is what it supports) meant every field defaulted to "" and
    # connect() always failed with "ssh_host is empty" -- when it managed
    # to run at all, which needed the four other bugs found alongside this
    # one already fixed first (the tunnel row wasn't even being created
    # until then). ai_machine_get needs owner_id for its own scoping check,
    # which the tunnel row does carry (ssh_tunnels.owner_id, set at
    # creation from the authenticated request that started the tunnel).
    try:
        tunnel_row = await db.ssh_tunnel_get(machine_id)
    except Exception:
        return _fail(machine_id, "no tunnel row")

    if not tunnel_row:
        return _fail(machine_id, "no tunnel row")

    try:
        machine = await db.ai_machine_get(machine_id, tunnel_row["owner_id"])
    except Exception:
        return _fail(machine_id, "no machine row")

    if not machine:
        return _fail(machine_id, "no machine row")

    ssh_host = machine.get("ssh_host", "")
    ssh_user = machine.get("ssh_user", "kali")
    ssh_key_path = machine.get("ssh_key_path", "")
    ssh_port = tunnel_row["ssh_port"] if "ssh_port" in tunnel_row.keys() else 22

    if not ssh_host:
        return _fail(machine_id, "ssh_host is empty")
    if not ssh_key_path:
        return _fail(machine_id, "ssh_key_path is empty")

    try:
        ssh_key_path = _check_key_permissions(ssh_key_path)
    except Exception as exc:
        return _fail(machine_id, str(exc))

    try:
        local_port = await _find_available_port()
    except Exception as exc:
        _fail(machine_id, str(exc))
        return (False, None, None, 0, 0, None)

    stored_fingerprint = machine.get("ssh_host_key_fingerprint", "")
    policy = _PinnedHostKeyPolicy(stored_fingerprint)
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(policy)
    try:
        await asyncio.to_thread(
            ssh_client.connect,
            hostname=ssh_host,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=10,
        )
    except Exception as exc:
        ssh_client.close()
        if policy.mismatch:
            _log.warning(
                "ssh_host_key_mismatch machine=%s expected=%s got=%s",
                machine_id, policy.mismatch[0], policy.mismatch[1],
            )
        return _fail(machine_id, str(exc)[:200])

    if policy.new_fingerprint:
        # First-ever connection to this machine: pin what we just accepted
        # so the *next* connection has something to compare against. A
        # write failure here must not fail an otherwise-successful
        # connect -- it only means TOFU has to happen again next time,
        # not that anything is actually wrong right now.
        try:
            await db.ai_machine_set_ssh_host_key_fingerprint(
                machine_id, policy.new_fingerprint
            )
        except Exception:
            _log.exception(
                "could not persist ssh host key fingerprint for %s", machine_id
            )

    try:
        transport = ssh_client.get_transport()
        if not transport:
            ssh_client.close()
            return _fail(machine_id, "no transport after connect")
        # This is `-L <local_port>:127.0.0.1:<config.PROXY_PORT>` -- forward
        # a local port to claude_proxy.py's own default bind address on the
        # *remote* side. Assumes the remote machine's claude_proxy.py binds
        # the same default port this codebase does everywhere else, since
        # there is no per-machine "remote proxy port" setting to read
        # instead (confirmed live against the one ssh_proxy machine
        # configured today: claude_proxy.py was already running there,
        # `127.0.0.1:9000`, matching config.PROXY_PORT's own default).
        #
        # transport.request_port_forward(...) used to sit here instead --
        # paramiko's *remote* forwarding request (`-R`), the wrong
        # direction, and never given a handler either, so it did nothing
        # at all: nothing was ever bound to listen on 127.0.0.1:local_port
        # on this host, which is what runner.get_proxy_target() actually
        # connects to. A real turn through a real, fully-connected tunnel
        # failed immediately with "Cannot connect to proxy at
        # 127.0.0.1:<port>" -- confirmed live, tunnel state="connected"
        # the whole time.
        import config

        forward_server = tunnel_manager_forward.start_forward(
            transport, local_port, "127.0.0.1", config.PROXY_PORT,
        )
    except Exception as exc:
        ssh_client.close()
        return _fail(machine_id, str(exc)[:200])

    return (True, ssh_client, transport, local_port, ssh_port, forward_server)


def _check_key_permissions(key_path: str) -> str:
    """Verify SSH key file permissions (must be <= 0o600). Returns the
    expanded path, since a `~` a user typed into the Settings form (the
    natural way to write it, and what the init wizard's own placeholder
    text -- `~/.ssh/id_ed25519` -- suggests) was never expanded before
    reaching os.stat(): it looked for a literal file named `~` relative to
    the service's cwd, not the real key, and failed "not found" even when
    the key existed with correct permissions. Every caller must use this
    return value for the actual paramiko connection too, not the original
    string, or the check and the connection attempt look at two different
    paths."""
    expanded = os.path.expanduser(key_path)
    try:
        st = os.stat(expanded)
    except OSError as exc:
        raise FileNotFoundError(f"SSH key not found: {key_path}") from exc
    mode = st.st_mode & 0o777
    if mode > 0o600:
        raise PermissionError(
            f"SSH key {key_path} mode {oct(mode)} "
            f"(expected 0o600 or stricter)"
        )
    if not os.access(expanded, os.R_OK):
        raise PermissionError(f"SSH key {key_path} is not readable")
    return expanded


async def _find_available_port(low: int = 9000, high: int = 10000) -> int:
    """Find first available local port in range [low, high]."""
    for port in range(low, high):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
        except ConnectionRefusedError:
            return port
        except TimeoutError:
            continue
        except OSError:
            continue
    raise OSError(f"No available port in range {low}-{high}")


async def disconnect(machine_id: str) -> None:
    """Close SSH client and release port forward for *machine_id*."""
    state = tunnel_manager._STATE.get(machine_id)
    if not state:
        return
    transport = state.get("transport")
    ssh_client = state.get("ssh_client")
    if transport:
        with contextlib.suppress(Exception):
            transport.close()
    if ssh_client:
        with contextlib.suppress(Exception):
            ssh_client.close()
    state["ssh_client"] = None
    state["transport"] = None


async def exec_command(machine_id: str, cmd: str, timeout: int = 10):
    """Run *cmd* over the SSH tunnel. Returns (stdin, stdout, stderr)."""
    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        raise RuntimeError("tunnel not connected")
    return state["ssh_client"].exec_command(cmd, timeout=timeout)


def _fail(machine_id: str, error_msg: str):
    """Return failure tuple and set error_msg on state."""
    state = tunnel_manager._STATE.get(machine_id)
    if state:
        state["state"] = "error"
        state["error_msg"] = error_msg
        state["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return (False, None, None, 0, 0, None)


async def test_ssh_connection(ssh_host: str, ssh_user: str, ssh_key_path: str):
    """Test SSH connectivity without creating a tunnel.

    Used by the initialization wizard.
    Returns {"ok": bool, "error": str | None}.
    """
    import paramiko

    if not ssh_host:
        return {"ok": False, "error": "SSH host is required"}
    if not ssh_key_path:
        return {"ok": False, "error": "SSH key path is required"}

    try:
        ssh_key_path = _check_key_permissions(ssh_key_path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    ssh = paramiko.SSHClient()
    # AutoAddPolicy here, unlike connect()'s _PinnedHostKeyPolicy above: this
    # runs before a machine even exists (the init wizard's connectivity
    # check, called with raw form fields, no machine_id), so there is
    # nowhere yet to persist a pinned fingerprint against. The connection is
    # torn down immediately after and nothing sensitive flows over it --
    # only the health-of-connectivity boolean this returns. The real,
    # standing SSH session (connect(), above) is what actually needs and
    # gets the pin; this one-shot check is a narrower, lower-value target,
    # and pinning starts from its very first real connection either way.
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507
    try:
        await asyncio.to_thread(
            ssh.connect,
            hostname=ssh_host,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=5,
        )
        ssh.close()
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def probe_remote(machine_id: str):
    """Probe remote host info via existing tunnel.

    Returns dict with probe results for each check.
    """
    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        return {"ok": False, "error": "tunnel not connected"}

    results = {}
    for label, cmd in [
        ("claude", "which claude 2>/dev/null || echo NOT_FOUND"),
        ("python3", "python3 --version 2>&1 || echo NOT_FOUND"),
        ("disk", "df -h / 2>/dev/null | awk 'NR==2{print $5}'"),
        ("proxy", "pgrep -f claude_proxy.py 2>/dev/null || echo NOT_FOUND"),
    ]:
        try:
            _, stdout, _ = state["ssh_client"].exec_command(cmd, timeout=5)
            decoded = stdout.read().decode("utf-8", errors="replace")
            results[label] = decoded.strip()
        except Exception:
            results[label] = "ERROR"

    return {"ok": True, "results": results}
