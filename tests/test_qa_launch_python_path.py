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

    def test_it_finds_the_venv_from_any_working_directory(self):
        """Anchored to the resolver's own path, not to $PWD.

        The first version keyed off $PWD, so sourcing it from anywhere but the
        repo root fell back to the system interpreter *silently* -- the exact
        failure this file exists to end, reintroduced by the fix for it.
        Measured from bin/: /usr/bin/python3, with no warning that a venv sat
        one directory up. Both real callers happen to cd to the root first, so
        nothing would have caught it.
        """
        real_venv = ROOT / ".venv" / "bin" / "python"
        if not real_venv.is_file():
            self.skipTest("no .venv in this checkout to resolve to")
        for cwd in ("/tmp", str(ROOT / "bin")):
            with self.subTest(cwd=cwd):
                result = subprocess.run(
                    ["bash", "-c", f'cd {cwd} && . {RESOLVER} 2>/dev/null; '
                                   'echo "RESOLVED=$WC_PYTHON"'],
                    capture_output=True, text=True, timeout=30,
                    env={k: v for k, v in os.environ.items() if k != "WC_PYTHON"},
                )
                self.assertIn(f"RESOLVED={real_venv}", result.stdout)

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


class ProxyRunUsesTheResolvedInterpreterTests(unittest.TestCase):
    """The second caller, and the reason the resolver is a sourced file.

    bin/wc-proxy-run.sh ran `exec python3 claude_proxy.py`. That is harmless
    today only because claude_proxy.py imports nothing but stdlib and
    backend_env -- it becomes the same outage launch.sh had the day the proxy
    gains a dependency, and the proxy is on the turn hot path, so its failure
    takes every turn with it rather than the web UI alone.

    wc-resolve-claude-path.sh's own comment already names this shape: it was
    split out of this very script because "a second inline copy in launch.sh
    would have been a second place to forget". The python resolver gets the
    same treatment for the same reason, before the forgetting rather than
    after.
    """

    PROXY_RUN = ROOT / "bin" / "wc-proxy-run.sh"

    @staticmethod
    def _code_lines(text: str) -> list[str]:
        """Comment lines are excluded, and that is not tidiness: the header
        says "Run claude_proxy.py in the foreground", so a check that matches
        any line naming the script fails on prose describing it. The same trap
        passed a mutated command in test_qa_rules_preflight.py until the
        comments were stripped there too."""
        return [
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_it_does_not_exec_bare_python3(self):
        offenders = [
            line.strip()
            for line in self._code_lines(self.PROXY_RUN.read_text(encoding="utf-8"))
            if "claude_proxy.py" in line and "python3" in line
        ]
        self.assertEqual(offenders, [], f"bare python3 runs the proxy: {offenders}")

    def test_it_sources_the_resolver(self):
        self.assertIn(
            "wc-resolve-python.sh",
            self.PROXY_RUN.read_text(encoding="utf-8"))

    def test_the_proxy_is_started_with_the_resolved_interpreter(self):
        text = self.PROXY_RUN.read_text(encoding="utf-8")
        starts = [line for line in self._code_lines(text) if "claude_proxy.py" in line]
        self.assertTrue(starts, "wc-proxy-run.sh no longer starts the proxy")
        for line in starts:
            with self.subTest(line=line.strip()):
                self.assertIn("WC_PYTHON", line)

    def test_it_still_parses(self):
        result = subprocess.run(
            ["bash", "-n", str(self.PROXY_RUN)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class DeployedProxyUnitTests(unittest.TestCase):
    """The template is the durable instance: it *writes* the unit.

    bin/wc-deploy-proxy.sh generated
    `ExecStart=/usr/bin/env python3 claude_proxy.py`, which is exactly the
    ExecStart read off the pentester transport on 2026-09-13. That unit did not
    drift into bare python3 -- it was produced that way, and provisioning a new
    transport or re-running the deploy on an existing one recreates it. Fixing
    the local callers and hand-correcting one host leaves the generator intact.

    It has to degrade, not refuse: transports have no venv today (pentester has
    none), and claude_proxy.py imports only stdlib plus backend_env, so a bare
    python3 is *correct* there. The launcher therefore prefers a venv if one
    exists and falls back otherwise, which is the same "being able to work
    beats being routed" rule CLAUDE.md §8 applies to the CLI.
    """

    DEPLOY = ROOT / "bin" / "wc-deploy-proxy.sh"
    LAUNCHER = ROOT / "bin" / "wc-proxy-start.sh"

    def test_the_generated_unit_does_not_hardcode_bare_python3(self):
        text = self.DEPLOY.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith("ExecStart=") and "python3 claude_proxy" in line
        ]
        self.assertEqual(offenders, [], f"unit template hardcodes python3: {offenders}")

    def test_the_generated_unit_starts_through_the_launcher(self):
        text = self.DEPLOY.read_text(encoding="utf-8")
        execs = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("ExecStart=")]
        self.assertTrue(execs, "the deploy script no longer writes an ExecStart")
        for line in execs:
            with self.subTest(line=line):
                self.assertIn("wc-proxy-start.sh", line)

    def test_the_launcher_is_shipped_with_the_proxy(self):
        """A unit pointing at a file the deploy never copies is a host that
        will not start at all -- strictly worse than the bug being fixed."""
        text = self.DEPLOY.read_text(encoding="utf-8")
        copy_lines = [ln for ln in text.splitlines() if "scp " in ln]
        self.assertTrue(
            any("wc-proxy-start.sh" in ln for ln in copy_lines),
            f"launcher never copied to the transport: {copy_lines}",
        )

    def test_the_launcher_prefers_a_venv_but_runs_without_one(self):
        """Executed, not read -- the same reason the resolver block is."""
        self.assertTrue(self.LAUNCHER.is_file(), f"{self.LAUNCHER} does not exist")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "claude_proxy.py").write_text("")
            shutil.copy(self.LAUNCHER, root / "wc-proxy-start.sh")

            # No venv: must still choose an interpreter rather than give up.
            probe = 'WC_PROXY_PRINT_ONLY=1 sh ./wc-proxy-start.sh'
            bare = subprocess.run(["bash", "-c", f"cd {root} && {probe}"],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(bare.returncode, 0, bare.stderr)
            self.assertRegex(bare.stdout.strip(), r"python3?$")

            # With a venv: it must win.
            venv_bin = root / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
            (venv_bin / "python").chmod(0o755)
            withvenv = subprocess.run(["bash", "-c", f"cd {root} && {probe}"],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(withvenv.returncode, 0, withvenv.stderr)
            self.assertIn(".venv/bin/python", withvenv.stdout)

    def test_launch_does_not_print_bare_python3_instructions(self):
        """launch.sh:85 echoed manual start instructions telling a human to run
        `python3 claude_proxy.py` -- advice the codebase itself no longer
        follows."""
        text = LAUNCH.read_text(encoding="utf-8")
        offenders = [
            ln.strip() for ln in text.splitlines()
            if "echo" in ln and "python3 claude_proxy" in ln
        ]
        self.assertEqual(offenders, [], f"prints stale instructions: {offenders}")


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
