"""QA: no module references a name that does not exist.

Two bugs found by a rules.md §9 pass on 2026-09-10, and neither was visible
from the outside, because in both cases a broad `except` clause was standing
between the defect and anyone noticing:

* `routes/voice.py` called `runner.get_default_model()` and never imported
  `runner`. The surrounding `except Exception: return None` caught the
  `NameError`, so voice handoff summarization silently produced nothing for
  every conversation without a pinned model -- which is precisely the fallback
  path that code exists to provide. Nothing logged, nothing raised, and the
  feature simply did not work in the one case it was written for.
* `bench/judge_delegation.py` printed `{e if 'e' in dir() else 'parse failed'}`
  after an `except Exception:` that never bound an `e`. The guard was therefore
  always false, so every grading fallback reported "parse failed" and a proxy
  error was indistinguishable from unparseable model output.

A unit test per site would not have caught either one: the first needs a chat
with no pinned model *and* an assertion that the fallback produced something,
and the second only shows up in a diagnostic nobody asserts on. What they have
in common is cheap to check statically, so this gate checks the class instead
of the instances.

Why pyflakes directly rather than shelling out to flake8: no dependency on
setup.cfg/tox.ini being present or on a particular flake8 version's default
select list, and it stays fast enough to run on every suite.

Deliberately narrow: **undefined names only.** F811 (a redefinition shadowing
an earlier one) is a real defect of the same family and is *not* asserted here,
because at the time this was written a peer session had one in uncommitted
working-tree changes -- HEAD was clean. A gate that fails on another session's
in-flight edit trains people to skip the suite, which costs more than the gate
gains. Add it when the tree is quiet.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

try:
    from pyflakes import checker as pyflakes_checker
    from pyflakes.messages import UndefinedLocal, UndefinedName
except ImportError:  # pragma: no cover - reported as a skip, not an error
    # pyflakes arrives here as a flake8 dependency locally and is installed
    # explicitly on the QA node by rules.md §14. Guarded rather than assumed:
    # a bare import failure at collection time takes the whole file out with
    # an error, which reads as a broken test rather than a missing tool.
    pyflakes_checker = None
    UndefinedLocal = UndefinedName = None

ROOT = Path(__file__).resolve().parents[1]

# .claude holds git worktrees -- other checkouts of this same project. Their
# contents are not this tree's responsibility and scanning them double-reports
# every file.
_SKIP_PARTS = {".venv", "__pycache__", ".claude", "node_modules"}

_UNDEFINED = (UndefinedName, UndefinedLocal) if UndefinedName else ()


def _python_files() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.py")
        if not _SKIP_PARTS & set(p.relative_to(ROOT).parts)
    )


def _undefined_names(path: Path) -> list[str]:
    """Return "file:line: name" for every undefined name in *path*."""
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # A syntax error is a different failure with its own louder signal
        # (py_compile in rules.md §1); reporting it here as "no undefined
        # names" would be wrong, but so would reporting it as one.
        return []
    rel = path.relative_to(ROOT)
    return [
        f"{rel}:{m.lineno}: {m.message % m.message_args}"
        for m in pyflakes_checker.Checker(tree, filename=str(path)).messages
        if isinstance(m, _UNDEFINED)
    ]


@unittest.skipUnless(pyflakes_checker is not None,
                     "needs pyflakes (rules.md \u00a714 installs it on the QA node)")
class NoUndefinedNamesTests(unittest.TestCase):
    def test_no_module_references_an_undefined_name(self):
        """The gate. A hit here is a NameError waiting for the right input --
        and, as both bugs above show, quite possibly one already happening
        inside an except clause that hides it."""
        found: list[str] = []
        for path in _python_files():
            found.extend(_undefined_names(path))
        self.assertEqual(
            found, [],
            "undefined names found; each is a NameError on the path that "
            "reaches it:\n  " + "\n  ".join(found),
        )

    def test_the_checker_actually_reports_an_undefined_name(self):
        """Positive control, and the reason this file is trustworthy.

        Without it, a wrong import path, a renamed pyflakes message class, or
        an isinstance filter that matches nothing would make the gate above
        pass on every input -- including the two real defects it was written
        for. That failure mode is silent and permanent, so it gets its own
        assertion rather than an assumption.

        The two shapes are checked separately because they are different
        pyflakes messages: a name never bound anywhere (UndefinedName) and a
        local read before its own assignment (UndefinedLocal).
        """
        cases = {
            "never bound": "def f():\n    return runner.thing()\n",
            "read before assignment": (
                "def f():\n"
                "    def g():\n"
                "        print(x)\n"
                "        x = 1\n"
                "    return g\n"
            ),
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                tree = ast.parse(source)
                messages = [
                    m for m in pyflakes_checker.Checker(tree, filename="<t>").messages
                    if isinstance(m, _UNDEFINED)
                ]
                self.assertTrue(
                    messages,
                    f"the checker reported nothing for {label!r}; the gate "
                    f"above is not actually checking anything",
                )

    def test_clean_source_is_not_reported(self):
        """The other half of the control: a gate that flags everything is as
        useless as one that flags nothing, and would be "fixed" by deleting
        it."""
        tree = ast.parse("import os\n\n\ndef f():\n    return os.getcwd()\n")
        messages = [
            m for m in pyflakes_checker.Checker(tree, filename="<t>").messages
            if isinstance(m, _UNDEFINED)
        ]
        self.assertEqual([str(m) for m in messages], [])

    def test_it_scans_a_meaningful_number_of_files(self):
        """A path or filter mistake that emptied the file list would make the
        gate pass by scanning nothing. Asserted as a floor, not a count, so
        adding or removing a module does not fail the suite."""
        files = _python_files()
        self.assertGreater(
            len(files), 100,
            f"only {len(files)} Python files found under {ROOT}; the scan is "
            f"probably rooted or filtered wrongly",
        )

    def test_the_scan_covers_routes_and_the_repo_root(self):
        """Names the two directories the found bugs lived in, so a future
        filter change cannot quietly drop them."""
        scanned = {str(p.relative_to(ROOT)) for p in _python_files()}
        self.assertIn("routes/voice.py", scanned)
        self.assertIn("bench/judge_delegation.py", scanned)
        self.assertTrue(
            any(s == "app.py" for s in scanned),
            "the repo root is not being scanned",
        )

    def test_worktrees_are_excluded(self):
        """`.claude/worktrees/` holds other checkouts of this project. Left in,
        every file is reported twice and an unrelated branch's work can fail
        this tree's suite."""
        scanned = [str(p.relative_to(ROOT)) for p in _python_files()]
        self.assertFalse(
            [s for s in scanned if s.startswith(".claude/")],
            "worktree checkouts are being scanned as if they were this tree",
        )


if __name__ == "__main__":
    unittest.main()
