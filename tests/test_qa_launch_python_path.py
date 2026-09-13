"""QA: the app is started by the interpreter that has its dependencies.

launch.sh ran `python3 -m uvicorn app:app` on both its branches. The venv is
where this project's dependencies live, and the system python3 is not it, so
the app started only for as long as the two happened to agree.

They stopped agreeing on 2026-09-12: the Design Specs Gallery added nh3 and
Markdown to requirements.txt, they were installed into .venv, and
`python3 -c 'import app'` began failing at `specs_gallery.py:19` with
ModuleNotFoundError. webconsole.service crashed on every start, systemd hit its
restart limit and gave up, and the console was down for about two and a half
hours before anyone noticed -- because a service that never starts logs nothing
a person is watching.

CLAUDE.md §9 already says a run under any other interpreter is untrustworthy.
This makes the *startup path* honour that, the same way
bin/wc-resolve-claude-path.sh makes it honour the CLI's location: resolve
explicitly, prefer the venv, and fall back with a warning rather than refusing
to run -- "routing is worth a lot, and not more than being able to work."

The block markers are copied from that file for the reason its own comment
gives, which this suite is not going to relearn: the block is *executed* here,
not read. Registry #48 is nine tests that read a shell block and never ran one
of them, so a block that could not succeed passed all nine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "bin" / "wc-resolve-python.sh"
LAUNCH = ROOT / "launch.sh"


def _block(path: Path, name: str) -> str:
    """Lift the marked block so it is run rather than described."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"# >>> {name}") + len(f"# >>> {name}")
    end = text.index(f"# <<< {name}")
    return text[start:end]


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run_block(self, *, with_venv: bool) -> subprocess.CompletedProcess:
        if with_venv:
            venv_bin = self.root / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            python = venv_bin / "python"
            python.write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n")
            python.chmod(0o755)
        script = f"set -u\ncd {self.root}\n{_block(RESOLVER, 'python-path-block')}\necho \"RESOLVED=$WC_PYTHON\"\n"
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30,
            env={k: v for k, v in os.environ.items() if k != "WC_PYTHON"},
        )

    def test_the_venv_interpreter_wins_when_present(self):
        result = self._run_block(with_venv=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"RESOLVED={self.root}/.venv/bin/python", result.stdout)

    def test_it_falls_back_rather_than_refusing_to_start(self):
        """A host with no venv must still come up. Losing the console entirely
        is worse than running it on an interpreter that may be missing a
        dependency -- and the warning says which happened."""
        result = self._run_block(with_venv=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"RESOLVED=\S*python3?\b")
        self.assertIn("WARNING", result.stderr)

    def test_an_existing_choice_is_respected(self):
        """So a deployment that already knows its interpreter is not overridden
        -- the same contract WC_CLAUDE_PATH has."""
        script = (
            f"set -u\ncd {self.root}\nexport WC_PYTHON=/usr/bin/python3\n"
            f"{_block(RESOLVER, 'python-path-block')}\necho \"RESOLVED=$WC_PYTHON\"\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIn("RESOLVED=/usr/bin/python3", result.stdout)


class LaunchUsesTheResolvedInterpreterTests(unittest.TestCase):
    def test_launch_does_not_start_uvicorn_with_bare_python3(self):
        """The defect. Both branches -- the WC_EXEC=1 exec and the backgrounded
        one -- have to be changed, or the service still comes up under the
        wrong interpreter in whichever mode systemd happens to use."""
        text = LAUNCH.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in text.splitlines()
            if "uvicorn" in line and "python3 -m uvicorn" in line
        ]
        self.assertEqual(offenders, [], f"bare python3 starts uvicorn: {offenders}")

    def test_launch_sources_the_resolver(self):
        text = LAUNCH.read_text(encoding="utf-8")
        self.assertIn("wc-resolve-python.sh", text)

    def test_every_uvicorn_start_uses_the_resolved_interpreter(self):
        text = LAUNCH.read_text(encoding="utf-8")
        starts = [line for line in text.splitlines() if "-m uvicorn" in line]
        self.assertTrue(starts, "launch.sh no longer starts uvicorn at all")
        for line in starts:
            with self.subTest(line=line.strip()):
                self.assertIn("WC_PYTHON", line)

    def test_the_resolver_is_executable_shell(self):
        self.assertTrue(RESOLVER.is_file(), f"{RESOLVER} does not exist")
        result = subprocess.run(
            ["bash", "-n", str(RESOLVER)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestDastBootsUnderTheTestInterpreterTests(unittest.TestCase):
    """tests/test_dast.py is the same defect with a quieter failure.

    It boots the app with the literal "python3", so on a host whose system
    interpreter lacks the dependencies the server never answers and all 16
    tests report *skipped* -- the suite goes greener while covering less.
    Measured on the QA node 2026-09-13: 16 skipped in 9.27s, and
    `python3 -c 'import app'` there fails on `import nh3` while the venv
    imports cleanly.
    """

    def test_it_boots_with_the_running_interpreter(self):
        text = (ROOT / "tests" / "test_dast.py").read_text(encoding="utf-8")
        self.assertNotIn('["python3", "-m", "uvicorn"', text)
        self.assertIn("sys.executable", text)


if __name__ == "__main__":
    unittest.main()
