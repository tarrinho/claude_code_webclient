"""QA coverage for the committed tree being able to run at all.

A green test suite proves the *working tree*. It says nothing about what was
committed, and the two drift the moment a commit is staged by pathspec.

That drift shipped: ae11c72 landed the session-durability work without staging
auth.py. The tests went in, the app.py call site went in, the implementation did
not, so HEAD called ``auth.load_sessions()`` against an auth.py with no such
function. A clone raised AttributeError at startup and the 17 tests in
test_qa_session_durability.py could not pass -- while every one of them passed
locally, against a tree HEAD did not describe. It survived a full rules.md run
and a push, and was only found two days later by chasing a missing changelog
heading.

Nothing in the pipeline compared the two. This does.

These tests read git, not the filesystem, so they fail on exactly the condition
no other test can see: a repository that cannot start from a fresh clone.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def in_a_git_repo() -> bool:
    return git("rev-parse", "--git-dir").strip() != ""


def head_file(path: str) -> str | None:
    """The committed contents of *path*, or None if HEAD does not track it."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _merge_getattr_symbols(existing: set[str], source: str) -> set[str]:
    """Merge names from a module's ``__getattr__`` ``_SYMBOLS`` dict.

    The cross-module reference test only walks ``def``/``class``/``import``
    nodes.  A lazy-``__getattr__`` mapping (nested inside a function body) is
    not captured by that walk, so we read it separately.
    """
    merged = set(existing)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return merged

    # _SYMBOLS lives inside the __getattr__ function body, not at module level.
    # Walk all statements (including those nested in function bodies).
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id != "_SYMBOLS":
            continue
        value = node.value if isinstance(node, ast.AnnAssign) else node.value
        if not isinstance(value, ast.Dict):
            continue
        for key in value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                merged.add(key.value)
    return merged


def module_names(source: str) -> set[str]:
    """Every name a module binds at module scope: def, class, assignment, import.

    Descends into ``if`` / ``try`` / ``with`` / loop bodies, because a name bound
    there is still bound at module scope. auth.py defines ``verify_password``
    twice inside a try/except — Argon2id with an scrypt fallback — and a
    collector that only read ``tree.body`` reported it as undefined, which is a
    false alarm on a file that is entirely correct.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names

    def collect(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(a.asname or a.name.split(".")[0] for a in node.names)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith,
                                   ast.For, ast.AsyncFor, ast.While)):
                collect(node.body)
                collect(getattr(node, "orelse", []))
                collect(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    collect(handler.body)

    collect(tree.body)
    return names


@unittest.skipUnless(in_a_git_repo(), "not a git checkout")
class CrossModuleReferencesResolveTests(unittest.TestCase):
    """Every ``module.name`` in HEAD must be defined by that module in HEAD.

    Static, not an import, and that distinction is the whole point. The first
    version of this file materialised HEAD and ran ``import app``, which passes
    against the very bug it was written for: ``auth.load_sessions()`` is called
    inside ``lifespan``, at request time, so importing the module never reaches
    it. An import check only sees module-level references and would have waved
    the broken release through exactly as the real pipeline did.
    """

    def test_no_module_calls_a_name_its_target_does_not_define(self):
        tracked = git("ls-tree", "-r", "--name-only", "HEAD").split()
        local = {Path(f).stem: f for f in tracked
                 if f.endswith(".py") and "/" not in f}
        defined = {stem: module_names(head_file(path) or "")
                   for stem, path in local.items()}

        # ``__getattr__`` resolves names lazily.  Collect them so the
        # static walk does not flag legitimate cross-module references as
        # missing.  This keeps the test honest: a name removed from the
        # mapping is still caught by the runtime.
        db_defined = defined.get("db", set())
        db_source = head_file(local.get("db")) or ""
        db_defined = _merge_getattr_symbols(db_defined, db_source)
        defined["db"] = db_defined

        missing: list[str] = []
        for path in local.values():
            source = head_file(path)
            if source is None:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                self.fail(f"HEAD:{path} does not parse")
            # Only modules this file actually imports, so a local variable that
            # happens to share a module's name cannot raise a false alarm.
            imported = {n.name for node in ast.walk(tree)
                        if isinstance(node, ast.Import) for n in node.names}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in imported
                        and node.value.id in defined
                        and node.attr not in defined[node.value.id]):
                    missing.append(
                        f"HEAD:{path}:{node.lineno} calls "
                        f"{node.value.id}.{node.attr}, which HEAD:{local[node.value.id]} "
                        f"does not define"
                    )
        self.assertEqual(
            missing, [],
            "the committed tree references names it does not contain, so a "
            "fresh clone would fail at runtime:\n  " + "\n  ".join(missing),
        )

    def test_the_check_sees_a_name_that_is_absent(self):
        """Guards the guard: the walk must actually resolve attributes.

        Without this, a bug in the AST walk would leave the test above passing
        on an empty list forever -- green, and blind.
        """
        caller = "import auth\n\n\ndef go():\n    return auth.load_sessions()\n"
        target = "def session_new():\n    pass\n"
        tree = ast.parse(caller)
        imported = {n.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for n in node.names}
        found = [
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in imported
            and node.attr not in module_names(target)
        ]
        self.assertEqual(found, ["load_sessions"])


@unittest.skipUnless(in_a_git_repo(), "not a git checkout")
class CommittedTreeImportsTests(unittest.TestCase):
    """Materialise HEAD and import it -- catches module-level breakage only."""

    def test_head_can_be_imported(self):
        tracked = [f for f in git("ls-tree", "-r", "--name-only", "HEAD").split()
                   if f.endswith(".py")]
        self.assertTrue(tracked, "HEAD tracks no Python files; git read failed")

        with tempfile.TemporaryDirectory() as tmp:
            for name in tracked:
                content = head_file(name)
                if content is None:
                    continue
                target = Path(tmp) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-c", "import app"],
                cwd=tmp, capture_output=True, text=True, timeout=180, check=False,
                # A clean environment: inheriting the caller's would let the
                # working tree satisfy an import that HEAD cannot.
                env={"PATH": "/usr/bin:/bin", "HOME": tmp, "PYTHONPATH": tmp,
                     "VIRTUAL_ENV": str(REPO / ".venv")},
            )
        detail = result.stderr.strip().splitlines()[-1] if result.stderr else ""
        self.assertEqual(
            result.returncode, 0,
            f"the committed tree cannot import app.py, so a fresh clone cannot "
            f"start: {detail}",
        )


@unittest.skipUnless(in_a_git_repo(), "not a git checkout")
class NoTestIsStrandedFromItsSubjectTests(unittest.TestCase):
    """A committed test that imports something HEAD lacks can never pass."""

    def test_every_committed_test_can_resolve_its_local_imports(self):
        tracked = git("ls-tree", "-r", "--name-only", "HEAD").split()
        local = {Path(f).stem for f in tracked
                 if f.endswith(".py") and "/" not in f}
        stranded = []
        for name in tracked:
            if not name.startswith("tests/") or not name.endswith(".py"):
                continue
            source = head_file(name)
            if source is None:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                stranded.append(f"{name}: does not parse ({exc.msg})")
                continue
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    targets = [(node.module or "").split(".")[0]]
                for module in targets:
                    # Only project modules: a missing third-party package is
                    # §5's business, and this must not fail on the environment.
                    if module and module in local and f"{module}.py" not in tracked:
                        stranded.append(f"{name} imports {module}, not in HEAD")
        self.assertEqual(stranded, [], "committed tests reference uncommitted modules")


@unittest.skipUnless(in_a_git_repo(), "not a git checkout")
class WorkingTreeMatchesHeadTests(unittest.TestCase):
    """Advisory: names the source files that differ from what was committed.

    Not a failure. Several sessions share this checkout and uncommitted work is
    the normal state; a test that failed on it would be red all day and quickly
    ignored. The value is that a run prints the list, so "the suite is green"
    is never mistaken for "the commit is complete".
    """

    def test_report_uncommitted_source_files(self):
        dirty = [line[3:] for line in git("status", "--porcelain").splitlines()
                 if line[3:].endswith(".py") and not line.startswith("??")]
        if dirty:
            print(f"\n  NOTE: {len(dirty)} committed Python file(s) differ from HEAD: "
                  f"{', '.join(sorted(dirty))}")
            print("  A green suite proves this tree, not HEAD. Check what is staged "
                  "before releasing.")
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
