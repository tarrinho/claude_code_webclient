"""QA: no test may turn an unexpected failure into a skip.

A skip reads as "not applicable here", which is indistinguishable from "ran
and passed" in a total. That is registry #50, and this file exists because the
suite had a live instance of it that survived for days.

`tests/test_ssh_tunnel_api.py`'s fixture wrapped its whole setup in
`try: ... except Exception: pytest.skip("app harness unavailable")`. Its
`asyncio.get_event_loop()` call raises once any IsolatedAsyncioTestCase file
has run first -- and it always had, in a full run -- so eleven of that file's
fourteen tests reported as skipped in every full-suite pass while passing when
the file was invoked alone. Nobody was misled by a wrong answer; they were
misled by an absence, which is harder to notice. Meanwhile the tunnel code
those tests cover was being rewritten daily.

The rule this enforces: a skip must be decided by a *narrow* exception -- one
that names the condition that genuinely makes a test inapplicable, such as
ImportError for a missing dependency -- or by an explicit check. `except
Exception` around a skip cannot distinguish "this machine lacks the
dependency" from "the code under test is broken", and it resolves both to
silence.

Static, deliberately: the failure being prevented is a *shape*, and a runtime
check would have to provoke every fixture's failure path to find it.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# Exception types narrow enough to mean "genuinely not applicable here".
# ImportError is the honest one: the dependency is absent, so there is nothing
# to test. Add to this list only with a reason -- each entry is a class of
# failure that will be reported as a skip forever after.
NARROW = {"ImportError", "ModuleNotFoundError", "FileNotFoundError", "OSError"}

BROAD = {"Exception", "BaseException"}


def _skips(node: ast.AST) -> bool:
    """Does this subtree call pytest.skip / self.skipTest / unittest.skip?"""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = ""
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name in {"skip", "skipTest", "skip_test"}:
            return True
    return False


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """The exception names a handler catches. Empty means a bare `except:`."""
    if handler.type is None:
        return set()
    types = (
        handler.type.elts
        if isinstance(handler.type, ast.Tuple)
        else [handler.type]
    )
    names = set()
    for entry in types:
        if isinstance(entry, ast.Name):
            names.add(entry.id)
        elif isinstance(entry, ast.Attribute):
            names.add(entry.attr)
    return names


def _offenders() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - test_frontend_syntax's job
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _skips(node):
                continue
            names = _handler_names(node)
            if not names:
                found.append((path.name, node.lineno, "bare except"))
            elif names & BROAD:
                found.append((path.name, node.lineno, " ".join(sorted(names))))
            elif not names <= NARROW:
                found.append((path.name, node.lineno, " ".join(sorted(names))))
    return found


class SkipsAreDecidedNarrowlyTests(unittest.TestCase):

    def test_there_are_test_files_to_check(self):
        """A find-nothing bug here would make the assertion below vacuous."""
        self.assertGreater(len(list(TESTS.glob("test_*.py"))), 50)

    def test_no_test_converts_an_unexpected_failure_into_a_skip(self):
        offenders = _offenders()
        detail = "\n".join(
            f"  {name}:{line} catches {caught} and skips" for name, line, caught in offenders
        )
        self.assertEqual(
            offenders, [],
            "a skip decided by a broad except cannot tell 'not applicable "
            "here' from 'the code is broken', and reports both as silence:\n"
            + detail,
        )

    def test_the_check_recognises_a_broad_except(self):
        """The detector has to work, not merely be present -- so it is run
        against the shape it exists to catch."""
        source = (
            "import pytest\n"
            "def fixture():\n"
            "    try:\n"
            "        setup()\n"
            "    except Exception:\n"
            "        pytest.skip('unavailable')\n"
        )
        tree = ast.parse(source)
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertTrue(_skips(handlers[0]))
        self.assertEqual(_handler_names(handlers[0]), {"Exception"})

    def test_the_check_allows_a_narrow_one(self):
        source = (
            "import pytest\n"
            "try:\n"
            "    import quickjs\n"
            "except ImportError:\n"
            "    pytest.skip('quickjs not installed')\n"
        )
        tree = ast.parse(source)
        handler = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)][0]
        self.assertTrue(_skips(handler))
        self.assertTrue(_handler_names(handler) <= NARROW)


if __name__ == "__main__":
    unittest.main()
