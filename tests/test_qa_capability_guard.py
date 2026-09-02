"""QA: the guard that refuses a run which would silently skip a layer.

`tests/capabilities.py` plus the `pytest_collection_modifyitems` hook in
`conftest.py` exist because rules.md #50 -- 43 tests absent from every total for
an evening -- was remedied with a paragraph in §14, and that paragraph has since
failed **twice** against readers who had read it. #64 is a session calling a
tree "green and settled" eleven times while the browser layer never ran.

The guard has four properties that matter, and each has an obvious wrong
version, so each gets a test:

1. It fires when a capability is missing *here* and present in the venv.
2. It does **not** fire when the capability is missing from both, because that
   is a fact about the machine and the per-file `skipUnless` guards state it
   correctly. Escalating it would stop a laptop without Chromium running the
   suite at all.
3. It does **not** fire when nothing collected needs the capability, because a
   session-wide refusal trains everyone to set the opt-out permanently -- after
   which the guard is gone and the variable stays.
4. The opt-out reports what it is giving up. An escape hatch that silences the
   count is the original defect with an extra step.

The end-to-end behaviour is verified by running pytest in a subprocess under
both interpreters, because the property is about interpreters and cannot be
observed from inside one of them.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from . import capabilities

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PYTHON = shutil.which("python3")


def _system_python_has(module: str) -> bool:
    """Whether the SYSTEM interpreter can import *module*.

    Asked of that interpreter, in a subprocess. The first version of this asked
    `capabilities._available_here()`, which answers for the interpreter running
    the test -- the venv -- and so reported "system python3 has quickjs" while
    skipping the three tests that exist to prove it does not. Measuring the
    wrong subject is the exact failure this whole file guards against, and it
    got in here first.
    """
    if SYSTEM_PYTHON is None:
        return False
    return subprocess.run(
        [SYSTEM_PYTHON, "-c", f"import {module}"],
        capture_output=True, timeout=60, check=False,
    ).returncode == 0



def both(result) -> str:
    """stdout and stderr together, since the guard uses both channels."""
    return (result.stdout or "") + (result.stderr or "")


class CapabilityDetectionTests(unittest.TestCase):
    """The two-question shape: available here, and available there."""

    def test_the_venv_probe_compares_prefixes_not_executables(self):
        """The bug this had on its first run, kept as a test.

        A venv built on the system interpreter symlinks `bin/python` straight at
        it, so comparing *resolved executable paths* concludes "we are already
        the venv" for both interpreters -- and the probe that would catch the
        wrong one never runs. Measured on this machine: the two resolve to the
        same binary; their prefixes are `/usr` and the project's `.venv`.
        """
        source = (ROOT / "tests" / "capabilities.py").read_text(encoding="utf-8")
        self.assertIn("sys.prefix", source)
        self.assertNotIn(
            "Path(sys.executable).resolve() == VENV_PYTHON.resolve()", source,
            "comparing executables is the proxy that silently disabled this",
        )

    def test_running_under_the_venv_reports_no_wrong_interpreter(self):
        """Whatever else is true, the correct invocation must be silent."""
        if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
            self.skipTest("this test only means something under the venv")
        self.assertEqual(capabilities.wrong_interpreter(), [])

    def test_an_unaskable_venv_is_not_an_accusation(self):
        """No evidence must never become "absent".

        If the venv cannot be probed -- missing, unreadable, timing out -- the
        mapping comes back empty, and an empty mapping must not make every
        capability look like a wrong-interpreter error. Otherwise the guard
        blocks every run on a machine with no venv at all.
        """
        # BOTH have to be forced. Under the venv `_available_here` is True for
        # every capability, so the loop `continue`s before it ever consults the
        # venv evidence -- and a first version of this test, patching only the
        # probe, passed against a mutation that dropped the evidence check
        # entirely. A test that cannot reach the line it names is not a test.
        original_venv = capabilities._available_in_venv
        original_here = capabilities._available_here
        capabilities._available_in_venv = dict          # no evidence at all
        capabilities._available_here = lambda _cap: False   # missing here
        try:
            self.assertEqual(
                capabilities.wrong_interpreter(), [],
                "with no evidence that another interpreter has it, a missing "
                "capability is a fact about the machine, not an accusation",
            )
            # And the contrast: evidence present means it *is* reported.
            capabilities._available_in_venv = lambda: {
                cap.name: True for cap in capabilities.CAPABILITIES
            }
            self.assertEqual(
                len(capabilities.wrong_interpreter()),
                len(capabilities.CAPABILITIES),
                "with evidence, every missing capability should be reported",
            )
        finally:
            capabilities._available_in_venv = original_venv
            capabilities._available_here = original_here

    def test_chromium_is_deliberately_not_a_guarded_capability(self):
        """Its absence is a real property of a machine.

        Adding it here would mean a laptop without a browser cannot run the
        suite, which is a worse failure than the one being prevented -- and the
        `skipUnless(CHROMIUM)` guards already report it honestly.
        """
        names = {cap.name for cap in capabilities.CAPABILITIES}
        self.assertNotIn("chromium", names)
        self.assertEqual(names, {"quickjs", "playwright-driver"})


@unittest.skipIf(SYSTEM_PYTHON is None, "no system python3 to contrast with")
class GuardBehaviourTests(unittest.TestCase):
    """End to end, in subprocesses, because the subject is interpreters."""

    def _run(self, python: str, target: str, env_extra: dict | None = None):
        """Returns the completed process. Callers read `both(result)`.

        `pytest.UsageError` writes to **stderr** while the informational
        notes go to stdout, so a test that inspects only one stream reads
        a refusal as silence -- which is how this file first passed the
        wrong way round.
        """
        import os
        env = {**os.environ, "WC_LOG_FILE": "/tmp/wc-capguard-test.log"}  # nosec B108
        env.pop("WC_ALLOW_PARTIAL_SUITE", None)
        env.update(env_extra or {})
        return subprocess.run(
            [python, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, timeout=300, check=False, env=env,
        )

    def test_the_wrong_interpreter_is_refused_for_a_test_that_needs_quickjs(self):
        if _system_python_has("quickjs"):
            self.skipTest("system python3 has quickjs here; nothing to contrast")
        result = self._run(SYSTEM_PYTHON, "tests/test_frontend_syntax.py")
        self.assertIn("missing here, present in .venv", both(result))
        self.assertIn(".venv/bin/python -m pytest", both(result),
                      "the message must name the command that works")
        self.assertIn("no tests ran", both(result))

    def test_the_refusal_is_not_an_internal_error(self):
        """It used to raise SystemExit, which pytest reports as INTERNALERROR
        with a traceback -- burying the actionable line under noise that reads
        like a bug in the guard."""
        if _system_python_has("quickjs"):
            self.skipTest("system python3 has quickjs here; nothing to contrast")
        result = self._run(SYSTEM_PYTHON, "tests/test_frontend_syntax.py")
        self.assertNotIn("INTERNALERROR", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_a_run_that_needs_nothing_is_not_blocked(self):
        """Property 3. The guard fires on what was collected, not on what
        exists, so an unrelated file runs fine under any interpreter."""
        result = self._run(SYSTEM_PYTHON, "tests/test_qa_test_layout.py")
        self.assertNotIn("missing here", both(result))
        self.assertIn("passed", both(result))

    def test_the_opt_out_runs_but_names_the_cost(self):
        """Property 4. Silencing the count would be #50 with an extra step."""
        if _system_python_has("quickjs"):
            self.skipTest("system python3 has quickjs here; nothing to contrast")
        result = self._run(SYSTEM_PYTHON, "tests/test_frontend_syntax.py",
                           {"WC_ALLOW_PARTIAL_SUITE": "1"})
        self.assertIn("continuing without the tests listed above", both(result))
        self.assertIn("collected test(s) covering", both(result),
                      "the opt-out must still report what is being skipped")
        self.assertIn("skipped", both(result))

    def test_the_venv_runs_the_same_file_for_real(self):
        """The contrast that makes the whole thing meaningful: the tests the
        other interpreter skipped actually execute here."""
        result = self._run(str(ROOT / ".venv" / "bin" / "python"),
                           "tests/test_frontend_syntax.py")
        self.assertNotIn("missing here", both(result))
        self.assertIn("passed", both(result))
        self.assertNotIn("skipped", both(result).strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
