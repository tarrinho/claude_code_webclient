"""QA: the suite does not leak a temp directory per `mkdtemp` call.

`tempfile.mkdtemp` has never cleaned up after itself -- the documentation says
so outright, and it is the caller's job. Seventeen test modules call it
directly and none of them removed the result, so every run left one directory
behind per call. Measured on 2026-09-18: 951 under /tmp, led by 154
`wc-testing-model` and 108 `wc-pins`.

That filled a 1.9 GB tmpfs mid-run. `git archive` failed with "No space left on
device", browser tests that write to temp directories went red for reasons
unrelated to the code under test, and a bisect built on those reds accused a
commit that touches one markdown file. Two operators cleared the space by hand
the same afternoon.

`tests/conftest.py` wraps `mkdtemp` and removes what it created at process
exit. These tests cover the wrapper rather than any call site, because the
whole point of fixing it there was to cover call sites that do not exist yet.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _conftest_namespace():
    """The namespace of the conftest that is actually installed.

    Not `import conftest`: `tests/` is not a package, the module is not on
    `sys.path`, and pytest registers it under a rewritten name that varies. A
    second import would also install a SECOND wrapper and then test that one,
    which is the opposite of what these tests are for.

    The installed wrapper's `__globals__` IS the conftest module's namespace,
    so it reaches the same `_created_tempdirs` list the suite is appending to.
    """
    return tempfile.mkdtemp.__globals__


class TrackedMkdtempTests(unittest.TestCase):
    def test_mkdtemp_is_the_wrapper_not_the_stdlib_function(self):
        """If conftest stops installing it, everything below passes anyway --
        the directories would simply never be registered. So assert the
        install itself."""
        namespace = _conftest_namespace()
        self.assertEqual(tempfile.mkdtemp.__name__, "_tracked_mkdtemp")
        self.assertIn("_created_tempdirs", namespace)
        self.assertIsNot(tempfile.mkdtemp, namespace["_real_mkdtemp"])

    def test_a_created_directory_is_registered_for_removal(self):
        registry = _conftest_namespace()["_created_tempdirs"]
        before = len(registry)
        path = tempfile.mkdtemp(prefix="wc-cleanup-probe-")
        self.addCleanup(lambda: os.path.isdir(path) and os.rmdir(path))
        self.assertEqual(len(registry), before + 1)
        self.assertEqual(registry[-1], path)

    def test_the_wrapper_returns_what_the_real_function_returns(self):
        """A wrapper that swallowed the return value would break every caller
        while still registering the directory."""
        path = tempfile.mkdtemp(prefix="wc-cleanup-probe-")
        self.addCleanup(lambda: os.path.isdir(path) and os.rmdir(path))
        self.assertTrue(os.path.isdir(path))
        self.assertIn("wc-cleanup-probe-", os.path.basename(path))

    def test_arguments_are_passed_through(self):
        """`prefix`, `suffix` and `dir` all have to survive the wrapper --
        several call sites depend on the prefix to find their own directories.
        """
        parent = tempfile.mkdtemp(prefix="wc-cleanup-parent-")
        path = tempfile.mkdtemp(prefix="pre-", suffix="-suf", dir=parent)
        base = os.path.basename(path)
        self.assertTrue(base.startswith("pre-"), base)
        self.assertTrue(base.endswith("-suf"), base)
        self.assertEqual(os.path.dirname(path), parent)


class RemovalAtExitTests(unittest.TestCase):
    """End to end, in a real subprocess, because `atexit` cannot be observed
    from inside the process that registers it."""

    def test_a_pytest_run_leaves_no_directory_behind(self):
        """The load-bearing test. Runs a throwaway test file under pytest --
        so conftest is loaded exactly as it is for the real suite -- and
        asserts the directory it made is gone once the process exits.

        Written so it can fail: the probe records the path it created into a
        file this process reads back, rather than guessing at a name, so a
        wrapper that registered nothing would leave a real path that still
        exists.
        """
        scratch = tempfile.mkdtemp(prefix="wc-cleanup-e2e-")
        record = Path(scratch) / "path.txt"
        # The probe file must live under `tests/`, not in the scratch
        # directory: pytest finds conftest.py by walking a collected file's
        # ANCESTORS, so a probe in /tmp runs without the wrapper installed and
        # the directory survives for a reason that has nothing to do with the
        # fix. The first version of this test did exactly that and failed
        # against working code.
        #
        # The name deliberately does not match `test_*.py`, so the ordinary
        # suite never collects it; naming it explicitly on the command line is
        # enough for the probe run. Removed in cleanup either way -- several
        # sessions share this checkout.
        probe = REPO_ROOT / "tests" / f"probe_leak_{os.getpid()}.py"
        self.addCleanup(lambda: probe.exists() and probe.unlink())
        probe.write_text(
            "import tempfile, pathlib\n"
            "def test_makes_a_tempdir():\n"
            "    p = tempfile.mkdtemp(prefix='wc-cleanup-inner-')\n"
            f"    pathlib.Path({str(record)!r}).write_text(p)\n"
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
             str(probe)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0,
                         f"probe run failed:\n{result.stdout}\n{result.stderr}")
        inner = record.read_text().strip()
        self.assertTrue(inner, "the probe never recorded a path")
        self.assertFalse(
            os.path.isdir(inner),
            f"{inner} survived the pytest process that created it -- "
            "conftest's mkdtemp cleanup did not run")


if __name__ == "__main__":
    unittest.main()
