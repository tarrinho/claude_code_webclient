"""QA: every asset cache-buster equals a hash of that asset's content.

The gap this closes
-------------------
`test_qa_asset_module_versions.py` asserts the references *agree with each
other* -- that `index.html` and every importing module name the same `?v=` for
a given file -- which catches the ES-module identity split. It cannot catch
staleness: content changing while the number stands still leaves every
reference in agreement and the browser running an old copy.

That happened three times on 2026-09-10. `app.js` changed at `?v=55` and
`styles.css` at `?v=41`, both found by hand while fixing the Backends panel.
Then `machines.js` gained a four-state transport status and was never bumped,
because its version had already moved from 9 to 10 earlier that day -- so the
sweep that bumped everything referencing `?v=55` did not touch it, and the
agreement test stayed green. A scan that afternoon found five modules in that
state at once: machines.js, device-alerts.js, orchestrator.js, skills.js and
usage.js.

Staleness is a fact about *history*, so no test that reads only the current
files can detect it -- which is why the numbers are now derived rather than
maintained. `bin/wc-asset-versions.py` computes each one from the file's own
content, and this asserts that derivation still holds. Edit a module without
re-running the script and this fails, instead of a browser silently keeping
the old module.

Asserted against HEAD, not the working tree, and the scope is deliberate. The
invariant is "every committed reference matches committed content" -- which is
what the machines.js bug violated, and what a deploy serves. A dirty working
tree is not a violation: another session part-way through editing an asset has
not done anything wrong yet, and failing the suite for everyone because of it
would make this gate the thing people switch off. `--check` without `--ref`
covers that case and is what a pre-commit hook should call.

Deliberately not a git-*history* check. An earlier version of this idea
compared "last commit touching the file" against "last commit changing its
`?v=`", and it is the wrong instrument twice over: it cannot see uncommitted
work, and it reported ten false positives because a commit that bumps one
module's version edits every file importing it, which counts as that file
changing too.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-asset-versions.py"


def _load_script():
    """Import the script as a module, so the test uses the same hash function
    the rewriter does rather than a second copy of the rule."""
    spec = importlib.util.spec_from_file_location("wc_asset_versions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_git_checkout() -> bool:
    """A tar-exported tree (the QA node) has no .git, and `git show HEAD:` in
    one falls back to working-tree content -- which would make this test assert
    a tautology rather than skip. Same reason test_qa_head_consistency.py and
    test_qa_deploy_entrypoint.py guard themselves."""
    return (ROOT / ".git").exists()


@unittest.skipUnless(_is_git_checkout(), "not a git checkout")
class AssetVersionsMatchContentTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), f"{SCRIPT} is missing")
        self.mod = _load_script()
        # Committed content is the subject; see the module docstring.
        self.mod._REF = "HEAD"

    def test_every_reference_matches_its_target_content(self):
        """The gate. A failure names the file and both numbers."""
        problems, seen = self.mod.scan()
        self.assertGreater(seen, 20, "the scan found almost no references; it "
                                     "is probably looking in the wrong place")
        self.assertEqual(
            problems, [],
            "asset cache-busters disagree with their content -- run "
            "bin/wc-asset-versions.py:\n  " + "\n  ".join(problems),
        )

    def test_the_check_flag_agrees_with_the_scan(self):
        """The script is what a person or a hook runs; the scan is what this
        test calls. If they could disagree, a green suite would not mean a
        green `--check`."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check", "--ref", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, timeout=120,
        )
        self.assertEqual(
            result.returncode, 0,
            f"--check failed while the scan passed:\n{result.stdout}\n{result.stderr}",
        )

    def test_rewriting_is_idempotent(self):
        """A second pass must find nothing to do.

        This is the property that makes the scheme terminate, and it is not
        free: the hash is taken over each file with its own `?v=` querystrings
        stripped, precisely so that rewriting a reference cannot change the
        holder's hash. Hash the raw bytes instead and bumping `app.js` forces
        every importer to be rewritten, which changes their hashes, which
        forces `index.html` to be rewritten, with no fixed point to reach.
        """
        first = {p: self.mod.version_for(p) for p in self._targets()}
        # Recompute after no edits: the same input must give the same number.
        second = {p: self.mod.version_for(p) for p in self._targets()}
        self.assertEqual(first, second, "version_for is not deterministic")
        problems, _ = self.mod.scan()
        self.assertEqual(problems, [], "the tree is not at a fixed point")

    def test_the_hash_ignores_cache_busters_in_the_content(self):
        """The strip rule, asserted directly rather than inferred from
        idempotence.

        Two files differing *only* in the `?v=` they point at must hash the
        same, or a dependency's bump would cascade into its importers.
        """
        a = "import {state} from './app.js?v=111';\nconst x = 1;\n"
        b = "import {state} from './app.js?v=999';\nconst x = 1;\n"
        self.assertEqual(
            self.mod._STRIP_RE.sub(r"\1", a),
            self.mod._STRIP_RE.sub(r"\1", b),
            "the strip leaves the querystring in, so hashes will cascade",
        )

    def test_a_content_change_moves_the_version(self):
        """The other half: the hash must actually respond to content.

        A strip too greedy -- one that removed the import line, say -- would
        make unrelated modules hash identically and the whole scheme would
        stop busting anything.
        """
        real = ROOT / "web" / "assets" / "app.js"
        # Working-tree content for this one: the point is that the hash
        # responds to bytes, and HEAD content cannot be edited.
        self.mod._REF = None
        original = self.mod.version_for(real)
        text = real.read_text(encoding="utf-8")
        try:
            real.write_text(text + "\n// probe\n", encoding="utf-8")
            changed = self.mod.version_for(real)
        finally:
            real.write_text(text, encoding="utf-8")
        self.assertNotEqual(
            original, changed,
            "appending a line did not change the version, so content changes "
            "would ship behind an unchanged cache-buster",
        )
        self.assertEqual(
            self.mod.version_for(real), original,
            "the file was not restored, or the hash is not a pure function of "
            "content",
        )

    def _targets(self) -> list[Path]:
        return sorted(
            p for p in (ROOT / "web" / "assets").rglob("*.js")
        ) + sorted((ROOT / "web" / "assets").glob("*.css"))


if __name__ == "__main__":
    unittest.main()
