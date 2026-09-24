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
import shutil
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

        The shell branch that writes these files is covered separately, by
        `test_the_skip_branch_writes_not_run_artefacts` below.
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


@unittest.skipUnless(RUNNER.exists(), "runner not present")
class SkipBranchTests(unittest.TestCase):
    """Drive the skip decision itself, against a synthetic root.

    An earlier version of this file claimed this could not be done -- that the
    runner "resolves paths relative to the repository" and hangs elsewhere. It
    resolves them relative to its OWN location (`cd "$(dirname "$0")/.."`), so
    copying the script and the one module it imports into a temporary root
    with two stub test files drives the whole branch in about a second. The
    claim was wrong and was found in review; the gap it excused was cheap to
    close all along.

    This matters because the branch is otherwise covered by source assertions
    only, and a wrong variable name in it would skip the browser phase
    silently -- reporting a partial run as a complete one, which is the exact
    failure the rest of this file exists to prevent.
    """

    def _root(self, tmp: Path) -> Path:
        """A minimal tree the runner can execute in."""
        root = tmp / "root"
        (root / "bin").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "bin" / "run-suite-chunked.sh").write_text(RUNNER.read_text())
        # resource_guard is the only project module the script imports.
        shutil.copy(ROOT / "resource_guard.py", root / "resource_guard.py")
        (root / "tests" / "test_plain_probe.py").write_text(
            "def test_ok():\n    assert True\n")
        # Classified as a browser file by the runner's own grep.
        (root / "tests" / "test_browser_probe.py").write_text(
            "sync_playwright = None\n"
            "def test_never_runs():\n    assert True\n")
        (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
        return root

    def _run(self, root: Path, out: Path, **env_extra):
        env = dict(os.environ)
        env.pop("WC_RESOURCE_GUARD", None)
        env.update({
            "WC_PY": str(ROOT / ".venv" / "bin" / "python"),
            "WC_SUITE_OUT": str(out),
            "PYTHONPATH": str(root),
            **env_extra,
        })
        return subprocess.run(
            ["bash", str(root / "bin" / "run-suite-chunked.sh")],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=300,
        )

    def test_the_skip_branch_writes_not_run_artefacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            root = self._root(tmpp)
            out = tmpp / "out"
            result = self._run(root, out,
                               WC_SUITE_PLAIN_COST_MB="1",
                               WC_SUITE_COST_MB="99999999")

            combined = result.stdout + result.stderr
            log = out / "browser-test_browser_probe.log"
            rc = out / "browser-test_browser_probe.rc"
            self.assertTrue(log.exists(), f"no NOT RUN log written:\n{combined[-1200:]}")
            self.assertTrue(log.read_text().startswith("NOT RUN"))
            self.assertEqual(rc.read_text().strip(), "75")
            self.assertIn("NOT RUN (browser phase not admitted)", result.stdout)
            self.assertIn("NOT a full-suite result", result.stderr)
            self.assertNotEqual(result.returncode, 0,
                                "a run with a skipped phase must not exit 0")
            # The plain chunk still ran: a skipped browser phase must not cost
            # the reading the host could afford.
            self.assertTrue((out / "plain-01.log").exists())

    def test_an_affordable_browser_phase_actually_runs(self):
        """The complement, so the skip branch cannot be satisfied by always
        skipping -- which would pass every assertion above."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            root = self._root(tmpp)
            out = tmpp / "out"
            result = self._run(root, out,
                               WC_SUITE_PLAIN_COST_MB="1", WC_SUITE_COST_MB="1")

            log = out / "browser-test_browser_probe.log"
            self.assertTrue(log.exists())
            self.assertFalse(log.read_text().startswith("NOT RUN"),
                             "browser phase was skipped despite being affordable")
            self.assertNotIn("NOT a full-suite result", result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout[-1200:])

    def test_no_browser_files_means_no_skip_banner(self):
        """Finding 6: the banner announced an omission from a run that had
        none, because the verdict was taken for a phase with no members.

        Two independent guards now prevent this -- the verdict is not taken
        when `browser_count` is 0, and the banner checks it again -- so
        removing either ALONE leaves this test green. Verified: both single
        mutations pass, and restoring the original pre-fix state (both absent)
        fails here with "claimed a skipped phase with no browser files". That
        is defence in depth rather than a redundant assertion, and it is worth
        writing down, because a reader who mutates one guard and sees green
        would otherwise conclude this test is decorative.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            root = self._root(tmpp)
            (root / "tests" / "test_browser_probe.py").unlink()
            out = tmpp / "out"
            result = self._run(root, out,
                               WC_SUITE_PLAIN_COST_MB="1",
                               WC_SUITE_COST_MB="99999999")

            self.assertNotIn("NOT a full-suite result", result.stderr,
                             "claimed a skipped phase with no browser files")
            self.assertEqual(result.returncode, 0, result.stdout[-1200:])


if __name__ == "__main__":
    unittest.main()
