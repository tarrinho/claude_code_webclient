"""Unit tests for tunnel_manager_ssh — key check, port allocation, test SSH.

Covers: _check_key_permissions, _find_available_port, test_ssh_connection
(faked). Requires paramiko.
"""
import asyncio

import pytest


pytest.importorskip("paramiko")


def _get_or_create_loop():
    """See tests/test_ssh_tunnel_basic.py's copy of this for why: plain
    asyncio.get_event_loop() raises once nothing has set a "current" loop
    for this thread, which unittest.IsolatedAsyncioTestCase-based tests
    elsewhere in this suite leave as the state on their way out."""
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def test_key_check_ok(tmp_path):
    """A 0600 key passes permission check."""
    key = tmp_path / "key"
    key.write_text("fake key")
    key.chmod(0o600)
    from tunnel_manager_ssh import _check_key_permissions
    _check_key_permissions(str(key))  # no raise


def test_key_check_expands_a_tilde_path(tmp_path, monkeypatch):
    """`~/.ssh/id_ed25519` -- the exact placeholder text the init wizard's
    own form shows, and the natural way anyone would type this -- reached
    os.stat() unexpanded: it looked for a literal file named `~` relative
    to the service's cwd, not the real key, and failed "not found" even
    when the real key existed with correct permissions. Reproduced with a
    real key file under a fake HOME rather than the actual user's ~/.ssh,
    so this is exact tilde-expansion behaviour, not a path that merely
    happens to exist."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("fake key")
    key.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    from tunnel_manager_ssh import _check_key_permissions
    expanded = _check_key_permissions("~/.ssh/id_ed25519")
    assert expanded == str(key)


def test_key_check_too_permissive(tmp_path):
    """A 0644 key raises PermissionError."""
    key = tmp_path / "key"
    key.write_text("fake key")
    key.chmod(0o644)
    from tunnel_manager_ssh import _check_key_permissions
    with pytest.raises(PermissionError, match="mode"):
        _check_key_permissions(str(key))


def test_key_check_not_readable(tmp_path):
    """A key with mode 0000 raises PermissionError."""
    key = tmp_path / "key"
    key.write_text("fake key")
    key.chmod(0o000)
    from tunnel_manager_ssh import _check_key_permissions
    with pytest.raises(PermissionError):
        _check_key_permissions(str(key))
    key.chmod(0o600)  # restore for cleanup


def test_key_check_missing():
    """A nonexistent key raises FileNotFoundError."""
    from tunnel_manager_ssh import _check_key_permissions
    with pytest.raises(FileNotFoundError):
        _check_key_permissions("/nonexistent/path/key")


def test_find_available_port():
    """_find_available_port returns a port (9000 is likely free)."""
    from tunnel_manager_ssh import _find_available_port
    loop = _get_or_create_loop()
    port = loop.run_until_complete(_find_available_port(9000, 9100))
    assert 9000 <= port < 9100


def test_ssh_test_connection_no_host():
    """test_ssh_connection rejects empty ssh_host."""
    from tunnel_manager_ssh import test_ssh_connection
    loop = _get_or_create_loop()
    result = loop.run_until_complete(
        test_ssh_connection("", "kali", "/nonexistent")
    )
    assert result["ok"] is False
    assert "required" in result["error"].lower()


def test_ssh_test_connection_no_key():
    """test_ssh_connection rejects empty ssh_key_path."""
    from tunnel_manager_ssh import test_ssh_connection
    loop = _get_or_create_loop()
    result = loop.run_until_complete(
        test_ssh_connection("localhost", "kali", "")
    )
    assert result["ok"] is False
    assert "required" in result["error"].lower()


def test_ssh_test_connection_bad_key():
    """test_ssh_connection rejects nonexistent key."""
    from tunnel_manager_ssh import test_ssh_connection
    loop = _get_or_create_loop()
    result = loop.run_until_complete(
        test_ssh_connection("localhost", "kali", "/nonexistent/key")
    )
    assert result["ok"] is False
    err = result["error"].lower()
    assert any(w in err for w in ["not found", "no such", "mode"])
