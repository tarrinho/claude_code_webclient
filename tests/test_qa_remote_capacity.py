"""QA: qa_remote's live capacity and provisioning checks.

Both read the transport directly over exec_command, never from stored
stats -- tunnel_manager_health's store_fn is separately broken (see
docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §3), so
nothing about remote memory can be trusted from the database.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import qa_remote


async def _hang(*args, **kwargs):
    """Never completes -- simulates exec_command's paramiko body blocking on
    a stuck remote read, so the surrounding asyncio.wait_for is what has to
    do the work of returning control."""
    await asyncio.sleep(999)


def _exec_returning(text: str):
    stdout = MagicMock()
    stdout.read.return_value = text.encode("utf-8")
    return AsyncMock(return_value=(MagicMock(), stdout, MagicMock()))


class AvailableMbTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_the_free_dash_m_output(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("2048\n")):
            self.assertEqual(await qa_remote._available_mb("m1"), 2048)

    async def test_a_raising_exec_command_is_none_not_zero(self):
        """None (could not read) and 0 (read, and it said zero) are different
        facts -- the capacity check below must not treat a broken SSH
        session as an empty host."""
        with patch("tunnel_manager_ssh.exec_command",
                    AsyncMock(side_effect=RuntimeError("tunnel not connected"))):
            self.assertIsNone(await qa_remote._available_mb("m1"))

    async def test_unparseable_output_is_none(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("garbage\n")):
            self.assertIsNone(await qa_remote._available_mb("m1"))

    async def test_a_hanging_exec_command_times_out_instead_of_blocking_forever(self):
        """Proves the asyncio.wait_for wrapping actually does something: a
        mocked exec_command that never returns must still make this
        function come back (with None) within its own 5s budget, not hang
        the caller (and, in production, the whole event loop) forever."""
        with patch("tunnel_manager_ssh.exec_command", AsyncMock(side_effect=_hang)):
            result = await qa_remote._available_mb("m1")
        self.assertIsNone(result)


class CheckCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_above_floor_is_ok(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("900\n")):
            ok, available = await qa_remote._check_capacity("m1", floor_mb=700)
        self.assertTrue(ok)
        self.assertEqual(available, 900)

    async def test_below_floor_refuses(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("400\n")):
            ok, available = await qa_remote._check_capacity("m1", floor_mb=700)
        self.assertFalse(ok)
        self.assertEqual(available, 400)

    async def test_unreadable_refuses(self):
        with patch("tunnel_manager_ssh.exec_command",
                    AsyncMock(side_effect=RuntimeError("gone"))):
            ok, available = await qa_remote._check_capacity("m1", floor_mb=700)
        self.assertFalse(ok)
        self.assertIsNone(available)


class IsProvisionedTests(unittest.IsolatedAsyncioTestCase):
    async def test_provisioned_when_the_venv_python_is_executable(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("yes\n")):
            self.assertTrue(await qa_remote._is_provisioned("m1"))

    async def test_not_provisioned_when_the_check_reports_nothing(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("\n")):
            self.assertFalse(await qa_remote._is_provisioned("m1"))

    async def test_not_provisioned_when_exec_command_raises(self):
        with patch("tunnel_manager_ssh.exec_command",
                    AsyncMock(side_effect=RuntimeError("tunnel not connected"))):
            self.assertFalse(await qa_remote._is_provisioned("m1"))

    async def test_a_hanging_exec_command_times_out_instead_of_blocking_forever(self):
        with patch("tunnel_manager_ssh.exec_command", AsyncMock(side_effect=_hang)):
            result = await qa_remote._is_provisioned("m1")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
