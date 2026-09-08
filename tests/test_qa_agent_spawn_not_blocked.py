"""QA: a short-of-memory host warns before spawning an agent, never blocks.

This is a decision test, not a logic test. `bin/wc-claude.sh` shipped with a
hard refusal (`exit 75`) when `resource_guard` said the host had no room.
Pedro commented the check out; it was restored with a lower floor; he then
asked for the blockage removed outright, and reaffirmed that after the
trade-off was put to him explicitly. So the non-blocking behaviour is a settled
choice, and the reason it needs a test is that it looks exactly like a bug:
someone reading `resource_guard.py`, seeing a guard whose whole purpose is to
refuse work, will reasonably "restore" the `exit` and reintroduce the blockage.

The reasoning worth preserving, because it is not obvious from the code: a
refusal here stops the operator from starting the very session they need in
order to fix an overloaded host. It fails in the worst direction -- the fuller
the box gets, the more certainly it locks you out of the only tool that could
free it. A warning carries identical information and leaves the judgement with
the human, who can see which sessions are disposable and which are mid-task.
The script cannot.

What that costs is also real: nothing now prevents an OOM kill of
webconsole.service, which has happened on this host before. That is the
accepted trade, not an oversight.

The module's own verdict logic is unchanged and still tested by
tests/test_qa_resource_guard.py -- `check()` still refuses, `main()` still
exits 1. Only this one caller declines to act on it.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "wc-claude.sh"


def _guard_block() -> str:
    """The guard invocation and the branch that acts on it."""
    source = WRAPPER.read_text(encoding="utf-8")
    match = re.search(
        r"if ! guard_output=.*?\nfi", source, re.DOTALL)
    assert match, "the resource_guard call is gone from bin/wc-claude.sh"
    return match.group(0)


class TheGuardStillRunsTests(unittest.TestCase):
    """Removing the blockage is not the same as removing the check.

    Deleting the call outright would also be a way to stop it blocking, and it
    would throw away the warning -- the only signal that tells an operator the
    host is the reason everything just got slow.
    """

    def test_the_wrapper_still_asks_the_guard(self):
        block = _guard_block()
        self.assertIn("resource_guard", block)
        self.assertIn("--cost-mb", block)

    def test_it_still_passes_an_honest_cost(self):
        """350, not the original 320. Re-measured across 6 live agents on
        2026-09-08: mean RSS 337 MB, median 349 MB. The old figure was an
        under-estimate, so the projection was optimistic -- which matters more
        now, not less, because the number's only remaining job is to make the
        warning worth reading."""
        self.assertIn('--cost-mb "${WC_AGENT_COST_MB:-350}"', _guard_block())

    def test_the_floor_is_tunable_per_invocation(self):
        self.assertIn('--floor-mb "${WC_AGENT_FLOOR_MB:-250}"', _guard_block())


class ItDoesNotBlockTests(unittest.TestCase):
    def test_the_branch_does_not_exit(self):
        """The regression this file exists for."""
        block = _guard_block()
        self.assertNotIn("exit 75", block)
        self.assertNotRegex(
            block, r"\bexit\b",
            "the guard branch exits, so a short-of-memory host is blocked from "
            "starting an agent again -- see this file's docstring before "
            "changing it back")

    def test_the_branch_says_it_is_continuing(self):
        """A bare warning reads like a refusal to anyone who has seen the old
        behaviour. The line has to say the session is starting anyway, or the
        operator waits for something that already happened."""
        block = _guard_block()
        self.assertIn("starting anyway", block)
        self.assertNotIn("refused to start", block)

    def test_the_warning_goes_to_stderr(self):
        """stdout on this path is whatever the caller is piping the CLI into.
        A warning there corrupts it -- the same reason resource_guard's own CLI
        reports on stderr."""
        block = _guard_block()
        for line in block.splitlines():
            if "echo" in line or "printf" in line:
                self.assertIn(">&2", line, f"not redirected to stderr: {line}")


class LiveBehaviourTests(unittest.TestCase):
    """Run the real wrapper with a floor no host can satisfy.

    The source assertions above can all pass while the script still fails to
    run -- a stray `set -e`, or a non-zero status leaking from the branch,
    would block just as effectively as an explicit exit.
    """

    @unittest.skipUnless(WRAPPER.exists(), "wrapper not present")
    def test_an_impossible_floor_still_starts_the_cli(self):
        env = dict(os.environ)
        env.pop("WC_RESOURCE_GUARD", None)   # the suite sets this to "off"
        env["WC_AGENT_FLOOR_MB"] = "99999999"
        result = subprocess.run(
            [str(WRAPPER), "--version"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"the wrapper refused to run. stderr:\n{result.stderr}")
        self.assertIn("Claude Code", result.stdout)
        self.assertIn("starting anyway", result.stderr,
                      "the warning was not shown, so the operator has no idea "
                      "the host is short of memory")


if __name__ == "__main__":
    unittest.main()
