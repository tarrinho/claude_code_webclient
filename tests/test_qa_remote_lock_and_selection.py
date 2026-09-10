"""QA: qa_remote's per-transport lock and node selection.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §5.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
import qa_remote


class _WithTransportsDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        qa_remote._RUN_LOCKS.clear()
        self.addCleanup(qa_remote._RUN_LOCKS.clear)

    async def _make_transport(self, tid, name, machine_id):
        await db.ssh_transport_create(
            tid, name, "admin", f"{name}.example.net", "kali", "~/.ssh/id_ed25519")
        await db.ai_machine_create(
            machine_id, f"{name} backend", "", 0, None, "", None, None, "admin",
            transport_id=tid)

    def _tunnel_up(self, **extra):
        return patch("tunnel_manager.tunnel_status",
                     AsyncMock(return_value={"tunnel_up": True, **extra}))

    def _tunnel_down(self):
        return patch("tunnel_manager.tunnel_status", AsyncMock(return_value=None))


class RunLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        qa_remote._RUN_LOCKS.clear()
        self.addCleanup(qa_remote._RUN_LOCKS.clear)

    async def test_same_machine_id_returns_the_same_lock(self):
        self.assertIs(qa_remote._run_lock("m1"), qa_remote._run_lock("m1"))

    async def test_different_machine_ids_get_different_locks(self):
        self.assertIsNot(qa_remote._run_lock("m1"), qa_remote._run_lock("m2"))


class MachineForTransportTests(_WithTransportsDb):
    async def test_finds_the_assigned_machine(self):
        await self._make_transport("t1", "One", "m1")
        self.assertEqual(await qa_remote._machine_for_transport("t1", "admin"), "m1")

    async def test_none_when_no_machine_assigned(self):
        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        self.assertIsNone(await qa_remote._machine_for_transport("t1", "admin"))


class ResolveNamedTransportTests(_WithTransportsDb):
    async def test_refuses_404_when_transport_does_not_exist(self):
        with self.assertRaises(qa_remote.QaRefusal) as ctx:
            await qa_remote.resolve_transport("admin", "nope")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_refuses_409_when_not_active(self):
        await self._make_transport("t1", "One", "m1")
        with self._tunnel_down():
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", "One")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Check or Init it first", ctx.exception.reason)

    async def test_refuses_409_when_not_provisioned(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=False)),
        ):
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", "One")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("wc-provision-qa.sh", ctx.exception.reason)

    async def test_refuses_409_when_already_locked(self):
        await self._make_transport("t1", "One", "m1")
        await qa_remote._run_lock("m1").acquire()
        try:
            with (
                self._tunnel_up(),
                patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            ):
                with self.assertRaises(qa_remote.QaRefusal) as ctx:
                    await qa_remote.resolve_transport("admin", "One")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("already has a QA run in progress", ctx.exception.reason)
        finally:
            qa_remote._run_lock("m1").release()

    async def test_refuses_503_below_capacity_floor(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", AsyncMock(return_value=(False, 400))),
        ):
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", "One", floor_mb=700)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_succeeds_when_every_precondition_passes(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
        ):
            prepared = await qa_remote.resolve_transport("admin", "One", floor_mb=700)
        self.assertEqual(prepared.transport["id"], "t1")
        self.assertEqual(prepared.machine_id, "m1")
        self.assertEqual(prepared.floor_mb, 700)


class ResolveUnnamedTransportTests(_WithTransportsDb):
    async def test_picks_the_roomiest_active_candidate(self):
        await self._make_transport("t1", "Small", "m1")
        await self._make_transport("t2", "Big", "m2")

        async def fake_capacity(machine_id, floor_mb):
            return (True, 900) if machine_id == "m2" else (True, 750)

        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", side_effect=fake_capacity),
        ):
            prepared = await qa_remote.resolve_transport("admin", None, floor_mb=700)
        self.assertEqual(prepared.transport["id"], "t2")

    async def test_excludes_locked_transports_from_selection(self):
        await self._make_transport("t1", "Locked", "m1")
        await self._make_transport("t2", "Free", "m2")
        await qa_remote._run_lock("m1").acquire()
        try:
            with (
                self._tunnel_up(),
                patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
                patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            ):
                prepared = await qa_remote.resolve_transport("admin", None, floor_mb=700)
            self.assertEqual(prepared.transport["id"], "t2")
        finally:
            qa_remote._run_lock("m1").release()

    async def test_refuses_503_when_none_qualify(self):
        await self._make_transport("t1", "Dead", "m1")
        with self._tunnel_down():
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", None, floor_mb=700)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_never_falls_back_to_running_locally(self):
        """No transport qualifies -- refuse, and refuse only. There is no
        code path in resolve_transport that returns a local execution
        target; this test pins that absence."""
        with self.assertRaises(qa_remote.QaRefusal):
            await qa_remote.resolve_transport("admin", None, floor_mb=700)


if __name__ == "__main__":
    unittest.main()
