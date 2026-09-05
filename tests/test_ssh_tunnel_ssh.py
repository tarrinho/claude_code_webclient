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


class _FakeKey:
    """Stands in for a paramiko PKey -- only .asbytes() is used by
    _fingerprint()."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def asbytes(self) -> bytes:
        return self._raw


def test_fingerprint_is_stable_and_key_dependent():
    """Same key bytes -> same fingerprint every time; different key bytes ->
    a different one. If this weren't true, TOFU pinning would either never
    match (every connection looks like a changed key) or always match
    (every key looks like every other one)."""
    from tunnel_manager_ssh import _fingerprint

    key_a = _FakeKey(b"fake-key-bytes-a")
    key_a_again = _FakeKey(b"fake-key-bytes-a")
    key_b = _FakeKey(b"fake-key-bytes-b")

    fp_a = _fingerprint(key_a)
    assert fp_a.startswith("SHA256:")
    assert _fingerprint(key_a_again) == fp_a
    assert _fingerprint(key_b) != fp_a


def test_pinned_policy_accepts_and_records_the_first_key_seen():
    """No stored_fingerprint yet (a brand-new machine's first connection):
    the policy must accept the key -- raising here would mean an ssh_proxy
    machine could never connect even once -- and record it on itself for
    the caller to persist."""
    from tunnel_manager_ssh import _PinnedHostKeyPolicy

    policy = _PinnedHostKeyPolicy("")
    policy.missing_host_key(None, "host.example", _FakeKey(b"first-key"))

    assert policy.new_fingerprint is not None
    assert policy.mismatch is None


def test_pinned_policy_accepts_a_key_matching_the_stored_fingerprint():
    """A later connection presenting the *same* key the machine was pinned
    to on first connect must be silently accepted -- this is the normal,
    expected case on every connection after the first, so it must not
    raise or record a "new" fingerprint (nothing changed, nothing to
    persist again)."""
    from tunnel_manager_ssh import _PinnedHostKeyPolicy, _fingerprint

    key = _FakeKey(b"the-real-key")
    stored = _fingerprint(key)

    policy = _PinnedHostKeyPolicy(stored)
    policy.missing_host_key(None, "host.example", key)  # must not raise

    assert policy.mismatch is None
    assert policy.new_fingerprint is None


def test_pinned_policy_rejects_a_key_that_does_not_match_the_pin():
    """The actual point of pinning: a later connection presenting a
    *different* key than the one accepted on first connect -- a MITM, or
    the host being reinstalled with a new key -- must be rejected, not
    silently trusted the way AutoAddPolicy always did."""
    from tunnel_manager_ssh import _PinnedHostKeyPolicy, _fingerprint

    original = _fingerprint(_FakeKey(b"original-key"))
    policy = _PinnedHostKeyPolicy(original)

    with pytest.raises(ConnectionError):
        policy.missing_host_key(None, "host.example", _FakeKey(b"different-key"))

    assert policy.mismatch == (original, _fingerprint(_FakeKey(b"different-key")))
