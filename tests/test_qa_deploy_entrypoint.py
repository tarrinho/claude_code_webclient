"""QA: a deploy entry point exists, and nothing else claims to be one.

This file exists because the capability was deleted rather than moved. On
2026-09-09 `bin/wc-release.sh` -- until then the script that exported a commit
to its own directory, pointed `releases/current` at it and restarted the unit
-- was replaced wholesale by a session-branch merge tool of the same name. The
deploy logic did not move anywhere. For a day:

* nothing in `bin/` could put a commit live, and
* `bin/wc-release.sh` answered every invocation, `--list` and `--dry-run`
  included, with "release is already merged into main. Nothing to do." and
  **exit 0**.

So a session asking to send something live ran the release-shaped script, got a
calm message and a zero status, and had every reason to believe it had
deployed. The only reason it was caught is that someone read the symlink
afterwards. That is the failure mode this codebase keeps writing tests against
-- a status line that lies -- and it had escaped into the one operation where
being wrong is least recoverable.

What is asserted here is deliberately about *shape*, not behaviour: a real
deploy restarts the live service, so no test may perform one. Every check below
is a property of the files in the repository, which is exactly the class of
regression that occurred.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
DEPLOY = BIN / "wc-deploy.sh"


def _tracked_mode(rel_path: str) -> str:
    """The file mode git records, not the one the working tree happens to have.

    The distinction matters: a clone gets the tracked mode, so that is what a
    peer or a fresh checkout will actually be able to execute.
    """
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.split()[0]


class ADeployEntryPointExistsTests(unittest.TestCase):

    def test_the_script_is_present(self):
        self.assertTrue(
            DEPLOY.is_file(),
            "bin/wc-deploy.sh is gone. Deploying is the one operation this "
            "repository cannot do by hand safely -- the systemd unit serves "
            "the working tree, so a restart without this script ships "
            "whatever every session happened to have saved.",
        )

    def test_it_is_executable_where_it_counts(self):
        """In the index, so a clone can run it."""
        mode = _tracked_mode("bin/wc-deploy.sh")
        if not mode:
            self.skipTest("not a git checkout")
        self.assertEqual(
            mode, "100755",
            f"bin/wc-deploy.sh is tracked as {mode}; a clone would not be "
            f"able to execute it",
        )

    def test_it_deploys_a_commit_rather_than_the_working_tree(self):
        """The entire property the script was written to buy. `git archive`
        emits exactly what a commit contains, so another session's
        half-finished edit cannot travel into production."""
        source = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("git archive", source)
        self.assertNotIn("cp -r", source)

    def test_it_moves_the_current_symlink(self):
        """`releases/current` is what the unit follows, so an entry point that
        does not move it is not a deploy however it is named."""
        source = DEPLOY.read_text(encoding="utf-8")
        self.assertRegex(source, r"ln -sfn")
        self.assertIn("current", source)

    def test_it_verifies_before_activating(self):
        """A release whose modules do not import must not become `current`.
        Registry #51 was HEAD calling a function no committed file defined."""
        source = DEPLOY.read_text(encoding="utf-8")
        verify_at = source.index("verify()")
        activate_at = source.index("activate()")
        self.assertLess(verify_at, activate_at, "verify is defined after activate")
        self.assertIn("does not import; not activating", source)

    def test_it_records_a_rollback_target(self):
        source = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("--rollback", source)
        self.assertIn(".previous", source)

    def test_it_restarts_the_unit(self):
        source = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("systemctl --user restart", source)


class NothingElseClaimsToDeployTests(unittest.TestCase):
    """The other half. A deploy entry point existing is not enough if a
    differently-named script is what people reach for and it silently does
    nothing."""

    def test_the_release_script_says_it_does_not_deploy(self):
        """`wc-release.sh` merges branches. Its name is close enough to
        "release" that it will be reached for, so it has to say so itself --
        and the exit code cannot carry the message, because a no-op merge
        legitimately succeeds."""
        release = BIN / "wc-release.sh"
        if not release.is_file():
            self.skipTest("bin/wc-release.sh is not present")
        source = release.read_text(encoding="utf-8")
        self.assertIn("DOES NOT DEPLOY", source.upper())
        self.assertIn("wc-deploy.sh", source)

    def test_no_other_script_moves_the_current_symlink(self):
        """Two things able to move `current` is two things to keep correct,
        and the second one is always the one nobody tested."""
        movers = []
        for script in sorted(BIN.glob("*.sh")):
            if script.name == "wc-deploy.sh":
                continue
            text = script.read_text(encoding="utf-8", errors="replace")
            if re.search(r"ln -sfn.*current|releases/current", text):
                movers.append(script.name)
        self.assertEqual(
            movers, [],
            f"these also point releases/current somewhere: {movers}",
        )


if __name__ == "__main__":
    unittest.main()
