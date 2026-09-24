"""QA: qa_remote's per-transport lock and node selection.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §5.
"""
from __future__ import annotations

import os
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
        with self._tunnel_down(), self.assertRaises(qa_remote.QaRefusal) as ctx:
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

    async def test_resolve_transport_uses_the_config_default_when_unspecified(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))) as mocked,
        ):
            await qa_remote.resolve_transport("admin", "One")
        mocked.assert_awaited_once_with("m1", 700)


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
        with self._tunnel_down(), self.assertRaises(qa_remote.QaRefusal) as ctx:
            await qa_remote.resolve_transport("admin", None, floor_mb=700)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_never_falls_back_to_running_locally(self):
        """No transport qualifies -- refuse, and refuse only. There is no
        code path in resolve_transport that returns a local execution
        target; this test pins that absence."""
        with self.assertRaises(qa_remote.QaRefusal):
            await qa_remote.resolve_transport("admin", None, floor_mb=700)


class ConfigDefaultsTests(unittest.TestCase):
    def test_floor_defaults_to_700(self):
        import config

        self.assertEqual(config.QA_CAPACITY_FLOOR_MB, 700)

    def _floor_with(self, **env_extra) -> int:
        """QA_CAPACITY_FLOOR_MB as a freshly-imported config would compute it.

        A subprocess, deliberately, rather than importlib.reload(): reloading
        config in-process swaps the module object while every other module in
        the suite still holds a reference to the old one, and this codebase has
        already paid for that class of desync once. Reading the value out of a
        clean interpreter costs a fork and mutates nothing.
        """
        import subprocess
        import sys
        from pathlib import Path

        env = dict(os.environ)
        env.pop("WC_QA_CAPACITY_FLOOR_MB", None)
        env.pop("WC_SUITE_COST_MB", None)
        env.update(env_extra)
        out = subprocess.run(
            [sys.executable, "-c",
             "import config; print(config.QA_CAPACITY_FLOOR_MB)"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr[-600:])
        return int(out.stdout.strip())

    def test_the_remote_floor_has_its_own_variable(self):
        """The local runner's WC_SUITE_COST_MB now means the BROWSER phase's
        cost, not a whole run's floor. A remote run sends the entire suite to
        one host, so its floor is the heaviest phase's -- the same 700, for a
        different reason. Sharing one variable across the two meanings would
        let an operator raising it move the remote floor and the local browser
        bar while leaving local plain chunks alone."""
        self.assertEqual(self._floor_with(WC_QA_CAPACITY_FLOOR_MB="1234"), 1234)

    def test_the_old_variable_is_still_honoured(self):
        """Backward compatibility: an existing WC_SUITE_COST_MB override must
        keep moving the remote floor, or this split silently changes the
        behaviour of a deployment that had tuned it."""
        self.assertEqual(self._floor_with(WC_SUITE_COST_MB="999"), 999)

    def test_the_new_variable_wins_when_both_are_set(self):
        """Otherwise the fallback is unreachable in exactly the deployment
        most likely to set both while migrating."""
        self.assertEqual(
            self._floor_with(WC_QA_CAPACITY_FLOOR_MB="111",
                             WC_SUITE_COST_MB="999"), 111)


if __name__ == "__main__":
    unittest.main()
