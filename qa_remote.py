"""Orchestrate a QA suite run on a remote transport.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.

Runs *inside* the app process, never as a standalone script: exec_command,
open_sftp and therefore sync_transport are all bound to
tunnel_manager._STATE, the running webconsole.service process's own
in-memory live SSH connections (spec §4). routes/qa.py is the only caller
that should ever import this module from outside a request handler.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("wc.qa_remote")

# Deliberately not ~/wc-proxy (the transport's own remote_path, where
# claude_proxy.py actually runs from) -- see spec §2 for the blast-radius
# and cadence reasons this checkout is kept separate.
QA_REMOTE_PATH = "~/wc-qa-checkout"


class QaRefusal(Exception):
    """A QA run cannot proceed. .status_code is the HTTP status routes/qa.py
    should answer with; .reason is the user-facing message."""

    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


async def _available_mb(machine_id: str) -> int | None:
    """Live remote available memory in MB, read fresh every call -- never
    trusted from storage (spec §3: tunnel_manager_health's store_fn is
    separately broken, so nothing here can rely on persisted stats).
    Returns None on any failure to read or parse, distinct from a
    successfully-read 0 -- callers must not conflate "could not check" with
    "checked, and it's empty"."""
    import tunnel_manager_ssh

    try:
        _, stdout, _ = await tunnel_manager_ssh.exec_command(
            machine_id, "free -m | awk '/Mem:/{print $7}'", timeout=5)
        return int(stdout.read().decode("utf-8", "replace").strip())
    except Exception:
        return None


async def _check_capacity(machine_id: str, floor_mb: int) -> tuple[bool, int | None]:
    """(ok, available_mb). ok is False both when available is below floor_mb
    and when available could not be read at all."""
    available = await _available_mb(machine_id)
    if available is None:
        return False, None
    return available >= floor_mb, available


async def _is_provisioned(machine_id: str) -> bool:
    """Has bin/wc-provision-qa.sh ever run here? Checked live, the same way
    as capacity -- a stale "provisioned" flag would be worse than no flag at
    all, since the checkout could have been wiped since."""
    import tunnel_manager_ssh

    cmd = f"test -x {QA_REMOTE_PATH}/.venv/bin/python && echo yes"
    try:
        _, stdout, _ = await tunnel_manager_ssh.exec_command(machine_id, cmd, timeout=5)
        return stdout.read().decode("utf-8", "replace").strip() == "yes"
    except Exception:
        return False
