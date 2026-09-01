"""QA: every test lives where the tooling looks for it.

`test_functional.py` sat at the repository root rather than under `tests/`, and
that one fact caused two separate defects — neither of them about tidiness.

**It was invisible to the chunked suite runner.** `bin/run-suite-chunked.sh`
globbed `tests/test_*.py`, so the file was never in a chunk, never in a total,
and a real failure inside it (`AppPersistenceTests` mocking four of the
handler's five database calls) sat there while run after run reported 0 failed.
0.9.4 was verified and released against that number. A glob cannot notice a file
it was never pointed at, so the runner was wrong in the reassuring direction.

**It escaped the log guard.** `tests/conftest.py` redirects `WC_LOG_FILE` before
anything imports config, because four test modules drive the app in-process and
would otherwise append to the production log. conftest applies to the directory
it sits in, so a test file outside `tests/` never sees it. Measured, not
assumed: running the file from the root grew `logs/webconsole.log` by 2008
bytes; from `tests/` it grows it by 0.

That second one is registry #42, which has now been "fixed" twice — once for the
servers the suite spawns, once for the in-process clients — and slipped through
a third route both times because the fix was attached to a mechanism rather than
to the property.

So this asserts the property: pytest collects no test file from outside
`tests/`. It is a source invariant in the same sense as the compile gate, and
unlike a paragraph in a runner's header it runs on every suite.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestLayoutTests(unittest.TestCase):
    """Where test files are allowed to live."""

    def test_pytest_collects_nothing_outside_the_tests_directory(self):
        """Asked of pytest, not of a glob.

        The whole point is that the runner's glob and pytest's collection had
        diverged, so asking a second glob whether they agree would reproduce the
        original mistake. This asks the collector itself.
        """
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=300,
        )
        files = {
            line.split("::", 1)[0]
            for line in result.stdout.splitlines()
            if "::" in line and line.strip().endswith(("]", ")")) is False
        }
        files = {f for f in files if f.endswith(".py")}
        self.assertTrue(files, f"collected nothing at all:\n{result.stdout[-800:]}")
        stray = sorted(f for f in files if not f.startswith("tests/"))
        self.assertEqual(
            stray, [],
            "these test files live outside tests/, so tests/conftest.py does not "
            "apply to them and any tooling scoped to tests/ cannot see them:\n  "
            + "\n  ".join(stray),
        )

    def test_no_test_file_sits_in_the_repository_root(self):
        """The cheap direct check, kept alongside the collector one.

        Collection is the property that matters, but it costs a subprocess and a
        few seconds. This one is instant and catches the common case, so a file
        added at the root fails fast and with an obvious message.
        """
        stray = sorted(p.name for p in ROOT.glob("test_*.py"))
        self.assertEqual(
            stray, [],
            "test files belong under tests/ — at the root they are outside "
            f"conftest's reach and outside tooling scoped to tests/: {stray}",
        )

    def test_the_conftest_that_guards_the_production_log_is_in_place(self):
        """The reason the location matters, pinned so it cannot quietly leave.

        Without this, moving the file into tests/ looks like tidiness and the
        actual protection — conftest redirecting WC_LOG_FILE before config is
        imported — could be deleted without anything objecting.
        """
        conftest = ROOT / "tests" / "conftest.py"
        self.assertTrue(conftest.is_file(), "tests/conftest.py has gone missing")
        source = conftest.read_text(encoding="utf-8")
        self.assertIn("WC_LOG_FILE", source,
                      "conftest no longer redirects the log file, so the suite "
                      "can append to logs/webconsole.log again (registry #42)")


if __name__ == "__main__":
    unittest.main()
