"""QA: the systemd units.

Asserted as text rather than by installing them. The one property worth a test
is that the timer fires nightly at 02:00 -- a unit that fires every 30 seconds
like webconsole-health.timer, which it is modelled on, would start a sweep
during the working day and measure contention on every cell.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "systemd"


class UnitTests(unittest.TestCase):
    def test_the_timer_fires_nightly_at_two(self):
        text = (UNITS / "webconsole-benchmark.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 02:00:00", text)

    def test_the_timer_does_not_use_an_interval(self):
        """OnUnitActiveSec would make it a repeating timer, not a nightly one."""
        text = (UNITS / "webconsole-benchmark.timer").read_text()
        self.assertNotIn("OnUnitActiveSec", text)

    def test_the_service_calls_the_scheduled_entry_point(self):
        text = (UNITS / "webconsole-benchmark.service").read_text()
        self.assertIn("--scheduled", text)

    def test_the_service_points_at_a_script_that_exists(self):
        """A unit naming a renamed or never-created script installs cleanly and
        fails only at 02:00, in the dark, with the failure visible nowhere but
        the journal. Resolve ExecStart's path against the repo root, with %h
        substituted for it, and require the target to exist on disk.
        """
        text = (UNITS / "webconsole-benchmark.service").read_text()
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
        exec_start = lines[start]
        i = start
        while lines[i].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            exec_start += " " + lines[i].strip()
        match = re.search(r"%h/projects/claude-code-webconsole/(\S+\.py)", exec_start)
        self.assertIsNotNone(
            match, "ExecStart did not reference a .py script under the repo root"
        )
        script = ROOT / match.group(1)
        self.assertTrue(script.exists(), f"{script} does not exist")


if __name__ == "__main__":
    unittest.main()
