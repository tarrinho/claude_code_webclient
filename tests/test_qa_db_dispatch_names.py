"""QA: every `db.<name>` a module calls must be a name `db` can resolve.

`db` resolves its public surface through `__getattr__` against a dispatch
table mapping helper name -> the `routes/db_*` module that defines it. That
indirection is what keeps `db.py` from importing every route module at import
time, and it has one cost: a call to a name the table does not carry is not a
NameError at import, or a lint error, or anything at all until the line runs.

That is not hypothetical. The supervisor -> orchestrator rename moved every
helper to `orchestrator_*` and rewrote the dispatch table, but left the call
sites saying `db.supervisor_list`, `db.supervisor_get`, `db.supervisor_update`
and thirteen more. `routes/orchestrators.py` alone held 33 of them, so
`GET /api/orchestrators` answered 500 and the orchestrator page rendered
nothing -- with the only visible symptom a console error in a browser nobody
had open. 2946 tests passed either side of it.

So this test reads the source rather than the behaviour: any `db.<something>`
attribute reference in the tree, checked against what `db` actually exposes.
It is deliberately static -- importing every module to check would need a
database, and the failure it guards against is precisely one that import
alone does not surface.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

import db

ROOT = Path(__file__).resolve().parents[1]

# Directories that are not this application's live source.
_SKIP = ("__pycache__", ".venv", ".claude", "docs", "node_modules")

# Attributes that are legitimately not dispatch helpers: module-level objects
# and submodules `db` genuinely owns.
_NOT_HELPERS = {
    "db_conn", "config", "init", "close", "transcript_path",
}

# Files whose `db.<name>` references are deliberate and must not resolve.
# One entry, and it earns it: that test asserts a helper was *removed*, so
# naming it is the assertion. Anything added here needs the same kind of
# reason -- "it fails otherwise" is how a guard test becomes decoration.
_INTENTIONAL = {
    "tests/test_qa_system_sample_insert_shadow.py",
}


def _uses_db_module(tree: ast.AST) -> bool:
    """True when `db` in this file means the module and nothing else.

    Two scripts under `bin/` bind a local `db = Path(...)/"webconsole.db"`
    and then call `db.exists()` / `db.is_file()`. Those are Path methods on a
    local variable, and reading them as dispatch-table misses reported two
    findings that were not findings -- which is worse than reporting none,
    because a guard test that cries wolf is one somebody switches off.

    So: the file must import `db` as a module, and must never rebind the name
    (assignment, loop target, parameter, comprehension). Either condition
    failing means this file is not one this test can reason about, and it is
    skipped rather than guessed at.
    """
    imports = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "db" and a.asname is None for a in node.names):
                imports = True
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id == "db":
                return False
        elif isinstance(node, ast.arg) and node.arg == "db":
            return False
    return imports


def _db_attribute_names(path: Path) -> set[str]:
    """Every `db.<name>` referenced in *path*, or empty when not applicable."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    if not _uses_db_module(tree):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "db"
        ):
            found.add(node.attr)
    return found


def _source_files() -> list[Path]:
    return [
        p for p in ROOT.rglob("*.py")
        if not any(part in _SKIP for part in p.parts)
        and str(p.relative_to(ROOT)) not in _INTENTIONAL
    ]


class DbDispatchNameTests(unittest.TestCase):
    def test_every_db_attribute_referenced_in_the_tree_resolves(self):
        unresolved: dict[str, set[str]] = {}
        for path in _source_files():
            for name in _db_attribute_names(path):
                if name.startswith("_") or name in _NOT_HELPERS:
                    continue
                try:
                    getattr(db, name)
                except AttributeError:
                    unresolved.setdefault(
                        str(path.relative_to(ROOT)), set()
                    ).add(name)
        self.assertEqual(
            unresolved, {},
            "these modules call db helpers that db cannot resolve — they "
            "raise AttributeError at run time, not import time:\n"
            + "\n".join(
                f"  {f}: {', '.join(sorted(names))}"
                for f, names in sorted(unresolved.items())
            ),
        )

    def test_no_module_still_calls_the_pre_rename_supervisor_helpers(self):
        """Named separately from the check above so the report says *why*.

        The generic check would flag these too, but only as "cannot resolve".
        This one states the actual situation -- a rename that moved the
        definitions and not the callers -- which is the thing a reader needs
        in order to fix it correctly rather than by adding an alias.
        """
        stale: dict[str, set[str]] = {}
        for path in _source_files():
            names = {
                n for n in _db_attribute_names(path)
                if n.startswith("supervisor_")
            }
            if names:
                stale[str(path.relative_to(ROOT))] = names
        self.assertEqual(
            stale, {},
            "the supervisor -> orchestrator rename left these call sites "
            "behind; each has an orchestrator_* equivalent:\n"
            + "\n".join(
                f"  {f}: {', '.join(sorted(names))}"
                for f, names in sorted(stale.items())
            ),
        )
