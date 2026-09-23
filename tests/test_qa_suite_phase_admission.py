"""QA: the suite runner admits each phase on its own cost.

`run-suite-chunked.sh` asked the resource guard once, declaring 700 MB -- the
price of a chunk running chromium, the heaviest tenant in the suite. The plain
chunks are not chromium: a six-file plain chunk was measured at 209 MB peak RSS
on 2026-09-23. So a host with no 700 MB window refused the 300-odd plain files
as well, and on 2026-09-22 and 23 that refused four consecutive attempts to get
any suite reading at all, on a box with ample room for the plain half.

Refusing work the host can afford is not the safe direction. It reads as
caution; the effect is that nobody gets a result, so changes ship on narrower
evidence. That is how the defective usage migration of 2026-09-23 reached
production.

The two properties below are the ones that make the split safe rather than
merely permissive:

* a host that cannot afford even the plain cost is still refused outright, so
  the 2026-09-07 incident this guard exists for stays prevented;
* a skipped browser phase is reported as NOT RUN and makes the run fail, never
  omitted. A partial run presenting its survivors as complete is registry #50,
  which this script has already been twice -- once in its file list and once in
  its aggregate parser.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bin" / "run-suite-chunked.sh"


@unittest.skipUnless(RUNNER.exists(), "runner not present")
class PhaseAdmissionSourceTests(unittest.TestCase):
    """Source-level, and honest about being weak -- the behavioural cases
    below are what actually pin this. These exist because the two costs must
    remain separately overridable for the behavioural cases to be writable at
    all."""

    SOURCE = RUNNER.read_text()

    def test_the_two_phases_declare_different_costs(self):
        self.assertIn("WC_SUITE_PLAIN_COST_MB", self.SOURCE)
        self.assertIn("WC_SUITE_COST_MB", self.SOURCE)

    def test_the_plain_cost_is_below_the_browser_cost(self):
        """If these ever invert, the split silently stops helping."""
        import re
        plain = re.search(r'PLAIN_COST_MB="\$\{WC_SUITE_PLAIN_COST_MB:-(\d+)\}"',
                          self.SOURCE)
        browser = re.search(r'BROWSER_COST_MB="\$\{WC_SUITE_COST_MB:-(\d+)\}"',
                            self.SOURCE)
        self.assertIsNotNone(plain, "plain cost default not found")
        self.assertIsNotNone(browser, "browser cost default not found")
        self.assertLess(int(plain.group(1)), int(browser.group(1)))


@unittest.skipUnless(RUNNER.exists(), "runner not present")
class PhaseAdmissionBehaviourTests(unittest.TestCase):
    """Run the real script against a throwaway tests directory."""

    def _run(self, env_extra, workdir):
        env = dict(os.environ)
        env.pop("WC_RESOURCE_GUARD", None)
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(RUNNER)], cwd=workdir, env=env,
            capture_output=True, text=True, timeout=600,
        )

    def test_an_unaffordable_plain_cost_refuses_the_whole_run(self):
        """The 2026-09-07 protection, unchanged."""
        with tempfile.TemporaryDirectory() as out:
            result = self._run(
                {"WC_SUITE_PLAIN_COST_MB": "99999999", "WC_SUITE_OUT": out},
                str(ROOT))
            self.assertEqual(result.returncode, 75, result.stderr[-800:])
            self.assertIn("refusing to start", result.stderr)
            self.assertIn("plain chunks", result.stderr)

    def test_the_aggregate_calls_a_skipped_phase_not_run_and_fails(self):
        """The half of the skip path that can be tested directly.

        The runner writes a `NOT RUN` log and an rc of 75 for each browser file
        it could not admit. This drives the aggregate over exactly those
        artefacts and asserts two things: that it is named as NOT RUN rather
        than as a crash -- "killed or crashed" sends the reader hunting a
        defect that does not exist -- and that the run does not exit 0, because
        a zero is what gets quoted as a full-suite result.

        WHAT THIS DOES NOT COVER, stated rather than implied: the shell branch
        that decides to write those files. Two routes to it were tried and
        neither is worth its cost. Driving the runner against a synthetic tests
        directory hangs -- the script resolves paths relative to the repository
        and is not built to run elsewhere -- and a test that hangs for ten
        minutes is worse than an honest gap. Exercising it in the real
        repository means running the entire plain phase first, because the
        browser loop is last, which is the whole suite.

        So the decision branch is covered by source assertions only, and that
        is weaker than it sounds: a wrong variable name there would skip
        silently. What IS verified end to end, by hand in this repository on
        2026-09-23, is the plain refusal -- WC_SUITE_PLAIN_COST_MB=99999999
        produces "refusing to start ... plain chunks (declared 99999999 MB)"
        and exit 75, so the 2026-09-07 protection is intact.
        """
        import re
        source = RUNNER.read_text()
        body = source[source.index('"$PY" - "$OUT" <<\'PY\''):]
        script = body[body.index("\n") + 1:body.index("\nPY\n")]

        with tempfile.TemporaryDirectory() as out:
            outp = Path(out)
            (outp / "browser-test_frontend_browser.log").write_text(
                "NOT RUN -- host not admitted for the browser phase\n"
                "declared cost: 700 MB\n")
            (outp / "browser-test_frontend_browser.rc").write_text("75")
            (outp / "plain-01.log").write_text("12 passed in 3.0s\n")
            (outp / "plain-01.rc").write_text("0")

            result = subprocess.run(
                [str(ROOT / ".venv" / "bin" / "python"), "-c", script, out],
                capture_output=True, text=True, timeout=120,
            )
            self.assertIn("NOT RUN", result.stdout,
                          f"skipped phase not named as such:\n{result.stdout}")
            self.assertNotIn("killed or crashed", result.stdout,
                             "a phase that was never admitted is not a crash")
            self.assertIn("12 passed", result.stdout,
                          "the plain chunks that did run must still be counted")
            self.assertNotEqual(
                result.returncode, 0,
                "an aggregate containing a skipped phase must not exit 0 -- "
                "a zero is what gets quoted as a complete result")


if __name__ == "__main__":
    unittest.main()
