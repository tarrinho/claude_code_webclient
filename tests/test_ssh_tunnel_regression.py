"""Regression and security tests for SSH proxy feature.

Lightweight checks that don't require the full app/test harness.
"""
import re


def test_version_bumped():
    """VERSION is 0.12.0 or higher."""
    import config
    version = config.VERSION
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match, f"VERSION {version} has no parseable semver"
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (0, 12), f"Version {version} < {config.VERSION}"


def test_paramiko_in_requirements():
    """paramiko is listed in requirements.txt."""
    with open("requirements.txt") as f:
        content = f.read()
    assert "paramiko" in content.lower()


def test_backend_kind_ssh_proxy():
    """backend_kind returns 'ssh-proxy' for ssh_proxy provider."""
    from shared import backend_kind
    machine = {"provider": "ssh_proxy"}
    assert backend_kind(machine) == "ssh-proxy"
