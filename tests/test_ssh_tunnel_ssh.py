"""Unit tests for tunnel_manager_ssh — key check, port allocation, test SSH.

Covers: _check_key_permissions, _find_available_port, test_ssh_connection
(faked). Requires paramiko.
"""
import asyncio

import pytest


pytest.importorskip("paramiko")


def test_key_check_ok(tmp_path):
    """A 0600 key passes permission check."""
    key = tmp_path / "key"
    key.write_text("fake key")
    key.chmod(0o600)
    from tunnel_manager_ssh import _check_key_permissions
    _check_key_permissions(str(key))  # no raise


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
    loop = asyncio.get_event_loop()
    port = loop.run_until_complete(_find_available_port(9000, 9100))
    assert 9000 <= port < 9100


def test_ssh_test_connection_no_host():
    """test_ssh_connection rejects empty ssh_host."""
    from tunnel_manager_ssh import test_ssh_connection
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(
        test_ssh_connection("", "kali", "/nonexistent")
    )
    assert result["ok"] is False
    assert "required" in result["error"].lower()


def test_ssh_test_connection_no_key():
    """test_ssh_connection rejects empty ssh_key_path."""
    from tunnel_manager_ssh import test_ssh_connection
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(
        test_ssh_connection("localhost", "kali", "")
    )
    assert result["ok"] is False
    assert "required" in result["error"].lower()


def test_ssh_test_connection_bad_key():
    """test_ssh_connection rejects nonexistent key."""
    from tunnel_manager_ssh import test_ssh_connection
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(
        test_ssh_connection("localhost", "kali", "/nonexistent/key")
    )
    assert result["ok"] is False
    err = result["error"].lower()
    assert any(w in err for w in ["not found", "no such", "mode"])
