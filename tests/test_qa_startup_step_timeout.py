"""QA: a startup step that hangs must end the process, not the port.

Startup runs inside the lifespan, before uvicorn binds the socket. A step that
never returns therefore leaves a process systemd considers perfectly healthy
with nothing listening on 443 -- a state indistinguishable from the outside
from a wedged server, which is why wc-health.sh restarts it. That kills the
boot, which starts again, and hangs again.

Measured on 2026-09-07: six consecutive boots between 12:25 and 12:31, a
restart roughly every 70 seconds, the site unreachable throughout. Each of
those boots logged exactly two lines -- "WebConsole starting" and
"PROJECTS_ROOT=..." -- and then nothing. That named the window but not the step
inside it, and the window held five candidates.

Two properties matter, and they are what these tests pin:

* a hanging step raises rather than hanging forever, and does so inside
  wc-health.sh's 45-second boot grace, so the process ends itself before the
  health check can race it -- one restarter (systemd's Restart=always), not
  two;
* every step is timed and logged *by name*, so the next occurrence says which
  one it was instead of leaving a silent gap to bisect.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import app as app_module


class StartupStepTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_hanging_step_raises_instead_of_hanging(self):
        async def never():
            await asyncio.sleep(3600)

        with patch.object(app_module, "_STARTUP_STEP_TIMEOUT_S", 0.05):
            with self.assertRaises(asyncio.TimeoutError):
                await app_module._startup_step("wedged", never())

    async def test_the_timeout_is_inside_the_health_check_boot_grace(self):
        """The whole point of the number: fail before the health check acts.

        wc-health.sh waits WC_HEALTH_BOOT_GRACE (45s by default) before it will
        restart anything. A startup step allowed to exceed that hands the
        outage to a second restarter, which is the loop this exists to break.
        """
        self.assertLess(
            app_module._STARTUP_STEP_TIMEOUT_S, 45,
            "a step may not outlive wc-health.sh's boot grace, or the health "
            "check restarts the boot instead of the process ending itself",
        )

    async def test_the_timeout_names_the_step_that_hung(self):
        async def never():
            await asyncio.sleep(3600)

        with patch.object(app_module, "_STARTUP_STEP_TIMEOUT_S", 0.05):
            with self.assertLogs(app_module._log, level="ERROR") as caught:
                with self.assertRaises(asyncio.TimeoutError):
                    await app_module._startup_step("migrate_sessions", never())
        self.assertTrue(
            any("startup_step_timeout" in line and "migrate_sessions" in line
                for line in caught.output),
            f"the step name must be greppable in the log, got {caught.output}",
        )

    async def test_a_healthy_step_returns_its_value_and_is_timed(self):
        async def work():
            return "done"

        with self.assertLogs(app_module._log, level="INFO") as caught:
            result = await app_module._startup_step("db.init", work())
        self.assertEqual(result, "done")
        self.assertTrue(
            any("startup_step" in line and "db.init" in line and "took=" in line
                for line in caught.output),
            f"every step must record what it cost, got {caught.output}",
        )

    async def test_a_slow_but_successful_step_warns(self):
        """The early warning: the hang, seen before it becomes one."""
        async def slow():
            await asyncio.sleep(0.05)

        with patch.object(app_module, "_STARTUP_STEP_SLOW_S", 0.01):
            with self.assertLogs(app_module._log, level="WARNING") as caught:
                await app_module._startup_step("load_settings", slow())
        self.assertTrue(
            any("startup_step_slow" in line and "load_settings" in line
                for line in caught.output),
            f"expected a slow-step warning, got {caught.output}",
        )


if __name__ == "__main__":
    unittest.main()
