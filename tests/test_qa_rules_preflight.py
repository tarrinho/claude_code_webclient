"""QA: rules.md's §0 preflight gate actually stops a run on a full box.

The gate this replaces could not work. It set `SKIP_TESTS=1` in §0b and tested
`${SKIP_TESTS:-}` in §14, two stages that never share a shell, so the variable
was empty at the point of use and the suite ran regardless -- on the very
low-memory box the gate existed to protect. Nothing detected that, because the
condition it guards against is invisible until the machine is already full.

So these tests **execute** the blocks, in the manner of
`test_qa_launch_reclaim`'s ItRunsAtAllTests and for the same reason recorded in
registry #49: nine tests that read a shell block passed a block that could
never succeed. Reading the source here would prove only that the words are
present.

The readings are injected through the `WC_PREFLIGHT_*` seam the block
documents, so a full box and an empty one are both reachable without waiting
for the machine to be in either state.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "rules.md"

GB_KB = 1024 * 1024


def _block(name: str) -> str:
    """Lift a fenced block out of rules.md by its markers."""
    source = RULES.read_text(encoding="utf-8")
    start = source.index(f"# >>> {name}") + len(f"# >>> {name}")
    return source[start:source.index(f"# <<< {name}")]


def _run(name: str, state: str, **env: str) -> subprocess.CompletedProcess:
    """Execute a block under the shell options a real run would use."""
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{_block(name)}"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "WC_PREFLIGHT_STATE": state, **env},
    )


class PreflightStopsAFullBoxTests(unittest.TestCase):
    """§0: the readings decide, and a bad one stops the run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / "verdict")

    def _run(self, **env):
        return _run("preflight-block", self.state, **env)

    def test_a_healthy_box_passes_and_records_ok(self):
        result = self._run(
            WC_PREFLIGHT_MEM_KB=str(3 * GB_KB),
            WC_PREFLIGHT_SWAP_TOTAL=str(3 * GB_KB),
            WC_PREFLIGHT_SWAP_FREE=str(3 * GB_KB),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PREFLIGHT OK", result.stdout)
        self.assertTrue(Path(self.state).read_text().startswith("OK"))

    def test_low_memory_aborts_with_a_nonzero_exit(self):
        """The exit code is the stop. Output alone can be read past."""
        result = self._run(
            WC_PREFLIGHT_MEM_KB=str(600 * 1024),   # 600 MB, the real reading
            WC_PREFLIGHT_SWAP_TOTAL=str(3 * GB_KB),
            WC_PREFLIGHT_SWAP_FREE=str(3 * GB_KB),
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("PREFLIGHT ABORT", result.stdout)
        self.assertIn("STOP.", result.stdout)
        self.assertTrue(Path(self.state).read_text().startswith("ABORT"))

    def test_high_swap_aborts_even_with_memory_free(self):
        result = self._run(
            WC_PREFLIGHT_MEM_KB=str(3 * GB_KB),
            WC_PREFLIGHT_SWAP_TOTAL=str(1000),
            WC_PREFLIGHT_SWAP_FREE=str(50),        # 95% used
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("swap at 95%", result.stdout)

    def test_a_box_with_no_swap_is_not_a_division_by_zero(self):
        result = self._run(
            WC_PREFLIGHT_MEM_KB=str(3 * GB_KB),
            WC_PREFLIGHT_SWAP_TOTAL="0",
            WC_PREFLIGHT_SWAP_FREE="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_verdict_survives_into_a_separate_shell(self):
        """The defect in one line: a variable does not cross stages, a file does.

        Each stage of a run is its own shell, so this asserts the property the
        old gate lacked rather than the words the new one uses.
        """
        self._run(
            WC_PREFLIGHT_MEM_KB=str(600 * 1024),
            WC_PREFLIGHT_SWAP_TOTAL=str(3 * GB_KB),
            WC_PREFLIGHT_SWAP_FREE=str(3 * GB_KB),
        )
        later = subprocess.run(
            ["bash", "-c", f'grep -q "^ABORT" "{self.state}" && echo seen'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(later.stdout.strip(), "seen")


class RecheckRefusesToAssumeTests(unittest.TestCase):
    """§0b: read the verdict back, and never read its absence as consent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / "verdict")

    def _run(self, **env):
        return _run("preflight-recheck-block", self.state, **env)

    def test_a_missing_verdict_stops_rather_than_proceeds(self):
        """An absent gate must not read as an open one."""
        result = self._run(WC_PREFLIGHT_MEM_KB=str(3 * GB_KB))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no preflight verdict", result.stdout)

    def test_an_abort_verdict_stops_the_suite(self):
        Path(self.state).write_text("ABORT\tonly 600 MB available\n")
        result = self._run(WC_PREFLIGHT_MEM_KB=str(3 * GB_KB))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("only 600 MB available", result.stdout)

    def test_an_ok_verdict_with_headroom_proceeds(self):
        Path(self.state).write_text("OK\t2026-09-08T02:00:00+01:00\n")
        result = self._run(WC_PREFLIGHT_MEM_KB=str(3 * GB_KB))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still OK", result.stdout)

    def test_memory_lost_since_the_preflight_stops_the_suite(self):
        """The case the recheck exists for: §0 passed, then a peer session
        started its own run and the headroom went."""
        Path(self.state).write_text("OK\t2026-09-08T02:00:00+01:00\n")
        result = self._run(WC_PREFLIGHT_MEM_KB=str(400 * 1024))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("memory fell to 400 MB", result.stdout)
        self.assertTrue(Path(self.state).read_text().startswith("ABORT"))


class CappedRunContainsTheDamageTests(unittest.TestCase):
    """§14: the suite runs inside a cgroup ceiling, and a kill says so.

    The point of the cap is not to make the suite fit -- it is that overshooting
    kills the run instead of the live server, which is the 860 MB process the
    kernel would otherwise choose. So these execute the block against a real
    scope rather than reading it: whether `systemd-run` actually confines
    anything is a property of this host's cgroup delegation, not of the words
    in the file.
    """

    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=64M",
             "/bin/true"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest(
                f"no delegated memory cgroup on this host: {probe.stderr.strip()}"
            )

    def _run(self, command: str, cap: str = "1G"):
        return subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{_block('capped-run-block')}"],
            capture_output=True, text=True, check=False,
            env={**os.environ, "WC_TEST_CMD": command, "WC_TEST_MEMORY_MAX": cap,
                 "PYTEST_SLICE": ""},
        )

    def test_a_passing_command_passes_through_the_cap(self):
        result = self._run("true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_real_failure_keeps_its_own_exit_code(self):
        """A cap must not flatten a genuine red run into something else."""
        result = self._run("exit 3")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_overshooting_the_cap_is_reported_as_not_a_test_result(self):
        """The failure mode this exists to prevent is a kill read as a verdict."""
        hog = (
            "python3 -c \"b=bytearray()\n"
            "for _ in range(40): b.extend(b'x'*(10<<20))\""
        )
        result = self._run(hog, cap="128M")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("KILLED BY THE CAP", result.stdout)
        self.assertIn("NOT a test result", result.stdout)

    def test_the_host_survives_a_run_that_overshoots(self):
        """The whole claim, measured: the cap contains the kill.

        Asserted against the live server's pid rather than against memory
        readings, which move on their own on a shared box.
        """
        server = subprocess.run(
            ["systemctl", "--user", "show", "webconsole.service",
             "-p", "MainPID", "--value"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if not server or server == "0":
            self.skipTest("no live server running to protect")
        hog = (
            "python3 -c \"b=bytearray()\n"
            "for _ in range(40): b.extend(b'x'*(10<<20))\""
        )
        self._run(hog, cap="128M")
        after = subprocess.run(
            ["systemctl", "--user", "show", "webconsole.service",
             "-p", "MainPID", "--value"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        self.assertEqual(
            server, after,
            "the live server was restarted while a capped run overshot -- the "
            "cap did not contain the kill",
        )


class SlicesCoverEveryTestFileTests(unittest.TestCase):
    """§14's slices must partition the suite: no file twice, none missed.

    Registry #51 is the reason this is a test and not a comment: 43 cases sat
    unrun inside a passing total for a whole session, because a total that
    hides unrun work reads exactly like a green one. A slice set that quietly
    omits a file reproduces that failure with extra steps.
    """

    def setUp(self):
        self.source = RULES.read_text(encoding="utf-8")
        self.files = {p.name for p in (ROOT / "tests").glob("test_*.py")}

    def _slice_globs(self) -> list[str]:
        stage = self.source.split("### The suite runs inside a memory cap", 1)[1]
        return [
            line.split('"', 2)[1]
            for line in stage.splitlines()
            if line.startswith('PYTEST_SLICE="tests/test_qa_')
        ]

    def test_the_qa_slices_are_declared(self):
        self.assertEqual(
            len(self._slice_globs()), 4,
            "expected four test_qa_ slices; the coverage check below depends "
            "on knowing how many there are",
        )

    def test_every_test_file_falls_in_exactly_one_slice(self):
        import fnmatch
        globs = [g.replace("tests/", "") for g in self._slice_globs()]
        # The fifth slice is every non-qa file, by construction.
        counts = {name: 0 for name in self.files}
        for name in self.files:
            if not name.startswith("test_qa_"):
                counts[name] += 1
                continue
            for glob in globs:
                if fnmatch.fnmatch(name, glob):
                    counts[name] += 1
        missed = sorted(n for n, c in counts.items() if c == 0)
        twice = sorted(n for n, c in counts.items() if c > 1)
        self.assertEqual(missed, [], f"these files are in no slice: {missed}")
        self.assertEqual(twice, [], f"these files are in two slices: {twice}")

    def test_the_browser_layer_is_split_out_of_the_default_command(self):
        """It is the largest consumer and what killed the whole-suite runs."""
        block = _block("capped-run-block")
        self.assertIn("--ignore=tests/test_frontend_browser.py", block)
        self.assertIn("--ignore=tests/test_qa_voice_conversation_browser.py", block)

    def test_the_fifth_slice_does_not_name_the_browser_file(self):
        """A listing names its files, and a named file defeats --ignore.

        This is not hypothetical: the first version of the fifth slice was
        `ls tests/test_*.py | grep -v '/test_qa_'`, which includes
        test_frontend_browser.py, so the slice silently became the browser run
        and was still going after ten minutes while its four predecessors took
        two to five. The default exclusion cannot save a slice that asks for
        the file by name.
        """
        stage = self.source.split("### The suite runs inside a memory cap", 1)[1]
        listing = [
            line for line in stage.splitlines()
            if line.startswith('PYTEST_SLICE="$(ls')
        ]
        self.assertEqual(len(listing), 1, "expected exactly one listing slice")
        self.assertIn(
            "grep -v 'test_frontend_browser'", listing[0],
            "the listing slice pulls in the browser layer, which the default "
            "--ignore cannot prevent for a named file",
        )

    def test_naming_a_file_overrides_the_default_ignore(self):
        """§14 tells the reader to run the browser layer by naming its files
        while the default command ignores them. That instruction is only true
        if pytest resolves it this way, and it is pytest's behaviour rather
        than ours -- so it is pinned here instead of trusted across upgrades.
        """
        browser = "tests/test_qa_voice_conversation_browser.py"
        named = subprocess.run(
            [sys.executable, "-m", "pytest", f"--ignore={browser}", browser,
             "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertIn("tests collected", named.stdout, named.stdout[-500:])
        self.assertNotIn("no tests collected", named.stdout)

        swept = subprocess.run(
            [sys.executable, "-m", "pytest", f"--ignore={browser}", "tests/",
             "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertNotIn(
            "voice_conversation_browser", swept.stdout,
            "--ignore stopped applying to a directory sweep, so the default "
            "command no longer excludes the browser layer",
        )


class TheRuleIsStatedTests(unittest.TestCase):
    """The prose has to carry the instruction; the code cannot say "stop" to a
    reader who is deciding whether to run the next stage."""

    def setUp(self):
        self.source = RULES.read_text(encoding="utf-8")

    def test_preflight_is_the_first_section(self):
        headings = [
            line for line in self.source.splitlines() if line.startswith("## ")
        ]
        self.assertTrue(
            headings[0].startswith("## 0. Preflight"),
            f"the first section is {headings[0]!r}, so the gate is not first",
        )

    def test_the_old_variable_gate_is_gone_from_the_test_stage(self):
        """Guarding pytest with a variable set in another shell is the defect."""
        stage = self.source.split("## 14.", 1)[1]
        self.assertNotIn(
            'if [ "${SKIP_TESTS:-}" = "1" ]', stage,
            "the suite is guarded again by a variable that cannot reach it",
        )


if __name__ == "__main__":
    unittest.main()
