"""QA: the memory admission guard decides, and fails open when it cannot.

Design: docs/superpowers/specs/2026-09-08-resource-guard-design.md

`check()` takes its inputs, so these are a table rather than a mock of the
operating system. Two cases earn their place beyond the arithmetic:

* **fail open** — a guard that blocks work because it could not measure turns a
  monitoring bug into an outage at every caller simultaneously. It is also the
  case a future reader is most likely to "fix" into fail-closed, so it is
  asserted rather than left to the docstring.
* **the override logs** — an override that stops being visible stops being an
  override and becomes a default nobody chose.

The boundary is pinned exactly, so a later `<` / `<=` slip fails a test instead
of quietly shifting when the host starts refusing work.
"""
from __future__ import annotations

import logging
import unittest
import unittest.mock

import resource_guard


def _meminfo(available_mb: int, *, swap_total_mb: int = 3151,
             swap_free_mb: int = 3151) -> dict[str, int]:
    """A /proc/meminfo shaped dict, in kilobytes as the kernel reports it."""
    return {
        "MemTotal": 3816 * 1024,
        "MemAvailable": available_mb * 1024,
        "MemFree": 213 * 1024,
        "SwapTotal": swap_total_mb * 1024,
        "SwapFree": swap_free_mb * 1024,
    }


# An environment with no override set, so a real WC_RESOURCE_GUARD in the
# developer's shell cannot make these tests pass for the wrong reason.
_CLEAN: dict[str, str] = {}


class HeadroomRuleTests(unittest.TestCase):
    def test_todays_real_numbers_refuse_an_eighth_agent(self):
        """635 MB available, 320 MB agent, 400 MB floor -- the measurement
        this whole design was built from."""
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(635), env=_CLEAN)
        self.assertFalse(verdict.ok, verdict.reason)
        self.assertIn("315 MB would remain", verdict.reason)

    def test_a_healthy_box_allows(self):
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(2000), env=_CLEAN)
        self.assertTrue(verdict.ok, verdict.reason)

    def test_the_boundary_is_pinned(self):
        """Exactly at the floor is allowed; one megabyte under is not.

        Pinned explicitly because `<` vs `<=` here is the difference between
        refusing and allowing at the exact point the host is most marginal, and
        a silent shift there would be invisible until it mattered.
        """
        at_floor = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(720), env=_CLEAN)
        self.assertTrue(at_floor.ok, "400 remaining == floor must be allowed")

        under = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(719), env=_CLEAN)
        self.assertFalse(under.ok, "399 remaining < floor must be refused")

    def test_the_verdict_carries_the_numbers_it_decided_on(self):
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(635), env=_CLEAN)
        self.assertEqual(verdict.available_mb, 635)
        self.assertEqual(verdict.cost_mb, 320)
        self.assertEqual(verdict.floor_mb, 400)

    def test_a_bigger_declared_cost_refuses_sooner(self):
        """run-suite-chunked.sh declares more than an agent costs."""
        room = _meminfo(1000)
        self.assertTrue(resource_guard.check(
            320, floor_mb=400, meminfo=room, env=_CLEAN).ok)
        self.assertFalse(resource_guard.check(
            700, floor_mb=400, meminfo=room, env=_CLEAN).ok)


class SwapRuleTests(unittest.TestCase):
    def test_a_thrashing_host_is_refused_even_with_memory_available(self):
        """Adding load to a swapping box is how slow becomes stopped."""
        verdict = resource_guard.check(
            320, floor_mb=400,
            meminfo=_meminfo(2000, swap_total_mb=3151, swap_free_mb=600),
            env=_CLEAN,
        )
        self.assertFalse(verdict.ok, verdict.reason)
        self.assertIn("swap", verdict.reason)

    def test_todays_swap_level_does_not_fire(self):
        """1681/3151 = 53%, above the 40% minimum: the slower-moving guard."""
        verdict = resource_guard.check(
            320, floor_mb=400,
            meminfo=_meminfo(2000, swap_total_mb=3151, swap_free_mb=1681),
            env=_CLEAN,
        )
        self.assertTrue(verdict.ok, verdict.reason)

    def test_a_host_with_no_swap_is_not_penalised(self):
        """SwapTotal 0 must not divide by zero or read as 0% free."""
        verdict = resource_guard.check(
            320, floor_mb=400,
            meminfo=_meminfo(2000, swap_total_mb=0, swap_free_mb=0),
            env=_CLEAN,
        )
        self.assertTrue(verdict.ok, verdict.reason)


class FailsOpenTests(unittest.TestCase):
    def test_missing_memavailable_allows_rather_than_blocks(self):
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo={"MemTotal": 1}, env=_CLEAN)
        self.assertTrue(verdict.ok)
        self.assertFalse(verdict.measured)

    def test_an_unreadable_proc_allows(self):
        def boom():
            raise OSError("no /proc here")

        original = resource_guard.read_meminfo
        resource_guard.read_meminfo = boom  # type: ignore[assignment]
        try:
            verdict = resource_guard.check(320, floor_mb=400, env=_CLEAN)
        finally:
            resource_guard.read_meminfo = original  # type: ignore[assignment]
        self.assertTrue(verdict.ok, "an unmeasurable host must not be blocked")
        self.assertFalse(verdict.measured)

    def test_being_unmeasurable_is_recorded_not_silent(self):
        with self.assertLogs(resource_guard._log, level="WARNING") as caught:
            resource_guard.check(
                320, floor_mb=400, meminfo={"MemTotal": 1}, env=_CLEAN)
        self.assertTrue(
            any("unmeasurable" in line for line in caught.output),
            f"failing open must say so, got {caught.output}",
        )


class OverrideTests(unittest.TestCase):
    def test_the_override_allows_work_that_would_be_refused(self):
        refused = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(635), env=_CLEAN)
        self.assertFalse(refused.ok)

        allowed = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(635),
            env={"WC_RESOURCE_GUARD": "off"},
        )
        self.assertTrue(allowed.ok)

    def test_the_override_is_logged_at_warning(self):
        with self.assertLogs(resource_guard._log, level="WARNING") as caught:
            resource_guard.check(
                320, floor_mb=400, meminfo=_meminfo(635),
                env={"WC_RESOURCE_GUARD": "off"},
            )
        self.assertTrue(
            any("resource_guard_overridden" in line for line in caught.output),
            f"an unlogged override is an invisible default, got {caught.output}",
        )

    def test_an_unrelated_value_does_not_disable_the_guard(self):
        """Only the documented off-values count; WC_RESOURCE_GUARD=on must not
        read as 'set, therefore off'."""
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(635),
            env={"WC_RESOURCE_GUARD": "on"},
        )
        self.assertFalse(verdict.ok)


class ReportTests(unittest.TestCase):
    def test_report_runs_against_the_real_host_without_raising(self):
        """It scans /proc; the numbers vary, that it survives the scan does not."""
        load = resource_guard.report()
        self.assertGreaterEqual(load.interactive_count, 0)
        self.assertGreaterEqual(load.interactive_mb, 0)
        self.assertEqual(load.errors, [])

    def test_the_summary_names_what_is_holding_memory(self):
        load = resource_guard.Load(
            interactive_count=7, interactive_mb=1884, console_mb=633)
        summary = load.summary()
        self.assertIn("7 interactive agents", summary)
        self.assertIn("1884 MB", summary)
        self.assertIn("633 MB", summary)

    def test_explain_tells_a_refused_caller_what_to_do(self):
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(635), env=_CLEAN)
        text = resource_guard.explain(verdict, resource_guard.Load())
        self.assertIn("WC_RESOURCE_GUARD=off", text)

    def test_explain_does_not_offer_the_override_when_allowing(self):
        verdict = resource_guard.check(
            320, floor_mb=400, meminfo=_meminfo(2000), env=_CLEAN)
        text = resource_guard.explain(verdict, resource_guard.Load())
        self.assertNotIn("WC_RESOURCE_GUARD=off", text)


class CliTests(unittest.TestCase):
    """The exit code is the whole interface for two of the three callers.

    A guard that reaches the right verdict and returns the wrong status blocks
    nothing, and would do so silently.
    """

    def test_exit_zero_when_there_is_room(self):
        import os

        os.environ["WC_RESOURCE_FLOOR_MB"] = "0"
        try:
            self.assertEqual(resource_guard.main(["--cost-mb", "0", "--quiet"]), 0)
        finally:
            os.environ.pop("WC_RESOURCE_FLOOR_MB", None)

    def test_the_floor_can_be_set_on_the_command_line(self):
        """--floor-mb exists so two callers can want different reserves.

        Added when the agent-spawn check in bin/wc-claude.sh was found
        commented out: the only way to move the floor was an environment
        variable, so that path could not ask for a lower reserve than the
        suite runner and was switched off instead of tuned. A flag that is
        parsed but not passed to check() would leave that exactly as it was,
        so this asserts the value actually reaches the verdict.
        """
        import os

        previous = os.environ.pop("WC_RESOURCE_GUARD", None)
        try:
            self.assertEqual(
                resource_guard.main(["--cost-mb", "0", "--floor-mb", "0", "--quiet"]), 0)
            self.assertEqual(
                resource_guard.main(
                    ["--cost-mb", "0", "--floor-mb", "99999999", "--quiet"]), 1,
                "--floor-mb was accepted but never reached check()")
        finally:
            if previous is not None:
                os.environ["WC_RESOURCE_GUARD"] = previous

    def test_the_flag_beats_the_environment_variable(self):
        """An explicit flag is a caller stating its own requirement; an env var
        is ambient. If the variable won, a shell that had exported a permissive
        floor would silently relax every caller that had chosen a strict one.
        """
        import os

        previous = os.environ.pop("WC_RESOURCE_GUARD", None)
        os.environ["WC_RESOURCE_FLOOR_MB"] = "0"
        try:
            self.assertEqual(
                resource_guard.main(
                    ["--cost-mb", "0", "--floor-mb", "99999999", "--quiet"]), 1)
        finally:
            os.environ.pop("WC_RESOURCE_FLOOR_MB", None)
            if previous is not None:
                os.environ["WC_RESOURCE_GUARD"] = previous

    def test_exit_one_when_refused(self):
        import os

        # A floor larger than any real host has: refusal is guaranteed without
        # depending on what this machine happens to have free right now.
        os.environ["WC_RESOURCE_FLOOR_MB"] = "99999999"
        # And the override cleared for the duration. `main()` reads os.environ
        # rather than taking an `env=` dict, so unlike the tests above it is
        # NOT covered by _CLEAN: any ambient WC_RESOURCE_GUARD=off turns the
        # refusal this asserts into an admission and the exit code into 0.
        # conftest.py sets exactly that for the suite (so unrelated tests do
        # not depend on the host's free memory), and a developer with it in
        # their shell would have defeated this test even before that -- the
        # same hazard _CLEAN exists to prevent, one env-reading path over.
        previous = os.environ.pop("WC_RESOURCE_GUARD", None)
        try:
            self.assertEqual(resource_guard.main(["--cost-mb", "1", "--quiet"]), 1)
        finally:
            os.environ.pop("WC_RESOURCE_FLOOR_MB", None)
            if previous is not None:
                os.environ["WC_RESOURCE_GUARD"] = previous





class CapacityTests(unittest.TestCase):
    """existing vs total, the pair the supervisor map shows per node.

    Deliberately built on the same injection points as HeadroomRuleTests
    (meminfo=, env=) plus one more (load=), so a real host is never read here
    -- report()'s own docstring says it belongs on a refusal path, and a test
    suite calling it for real on every run is exactly "frequent".
    """

    def _load(self, interactive=0, turns=0):
        return resource_guard.Load(interactive_count=interactive, turn_count=turns)

    def test_todays_real_numbers(self):
        """The numbers this feature exists to show, pinned as a regression:
        six interactive agents, 686 MB available, nothing more fits."""
        result = resource_guard.capacity(
            320, floor_mb=400, meminfo=_meminfo(686), env=_CLEAN,
            load=self._load(interactive=6),
        )
        self.assertEqual(result, {
            "existing": 6, "total": 6,
            "cost_mb": 320, "floor_mb": 400, "available_mb": 686,
        })

    def test_room_for_more_extends_the_total_past_existing(self):
        result = resource_guard.capacity(
            320, floor_mb=400, meminfo=_meminfo(2000), env=_CLEAN,
            load=self._load(interactive=2),
        )
        # (2000 - 400) // 320 = 5 more fit on top of the 2 already running.
        self.assertEqual(result["existing"], 2)
        self.assertEqual(result["total"], 7)

    def test_interactive_and_turn_agents_both_count_as_existing(self):
        result = resource_guard.capacity(
            320, floor_mb=400, meminfo=_meminfo(2000), env=_CLEAN,
            load=self._load(interactive=2, turns=3),
        )
        self.assertEqual(result["existing"], 5)

    def test_an_empty_host_still_reports_zero_existing(self):
        result = resource_guard.capacity(
            320, floor_mb=400, meminfo=_meminfo(2000), env=_CLEAN, load=self._load(),
        )
        self.assertEqual(result["existing"], 0)
        self.assertEqual(result["total"], 5)

    def test_unmeasurable_reports_a_total_of_none_not_a_guess(self):
        """The fails-open case check() has, carried through rather than
        papered over with a number nothing backs up."""
        result = resource_guard.capacity(
            320, floor_mb=400, meminfo={"MemTotal": 1}, env=_CLEAN,
            load=self._load(interactive=6),
        )
        self.assertIsNone(result["total"])
        self.assertIsNone(result["available_mb"])
        # Existing is a live process count, not a projection -- it is not
        # unmeasurable just because meminfo could not be read.
        self.assertEqual(result["existing"], 6)

    def test_the_override_also_withholds_a_total(self):
        """WC_RESOURCE_GUARD=off marks check()'s verdict unmeasured, and
        capacity() must not quietly disagree by reporting a total anyway."""
        result = resource_guard.capacity(
            320, floor_mb=400, meminfo=_meminfo(2000),
            env={"WC_RESOURCE_GUARD": "off"}, load=self._load(interactive=1),
        )
        self.assertIsNone(result["total"])

    def test_report_is_not_called_when_load_is_supplied(self):
        """The /proc scan is the expensive half; a caller that already has a
        Load must not pay for a second one."""
        def _boom():
            raise AssertionError("report() was called despite load= being given")
        with unittest.mock.patch.object(resource_guard, "report", _boom):
            resource_guard.capacity(
                320, floor_mb=400, meminfo=_meminfo(2000), env=_CLEAN,
                load=self._load(interactive=1),
            )


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
