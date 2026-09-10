"""QA: db.py must not define a name its dispatch table delegates elsewhere.

`db.py` resolves most of its API through a `__getattr__` dispatch table that
maps a name to the `routes.db_*` module owning it. A module-level definition of
the same name in `db.py` silently wins -- `__getattr__` is only consulted for
names the module does not have -- so the dispatched copy becomes unreachable
code that still looks live, imports cleanly, and passes review.

That is not hypothetical. `_ensure_usage_columns` existed in both places, byte
-for-byte identical, until 2026-09-10. A migration adding `usage_events.
billing_route` was written into the dispatched copy, the suite passed, and the
column did not exist: every query naming it failed with "no such column" the
first time one was run against a real database. The two copies had been
identical for long enough that nothing distinguished the live one from the
dead one except which file it was in.

The same shape has cost this codebase twice before, both recorded in db.py's
own comments: `system_sample_insert` was defined here with a different
signature and shadowed the real one, so every local host sample crashed on
"type 'dict' is not supported" and none were written for days; and
`system_sample_list` was its stale read-side twin.

Static, and deliberately so: the failure is a *shape*, and detecting it at
runtime would mean calling every dispatched name and checking which module
answered.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PY = ROOT / "db.py"

# "name": "routes.db_something" -- the dispatch table's own syntax.
_DISPATCH_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)":\s*"(routes\.db_\w+)"')


def _module_level_names(source: str) -> set[str]:
    """Names db.py binds at module level: defs, async defs, and assignments.

    Only top-level bindings, because only those shadow `__getattr__`. A name
    assigned inside a function or a class is invisible to attribute lookup on
    the module.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _dispatched_names(source: str) -> dict[str, str]:
    return {name: module for name, module in _DISPATCH_RE.findall(source)}


class NoDispatchedNameIsShadowedTests(unittest.TestCase):

    def setUp(self):
        self.source = DB_PY.read_text(encoding="utf-8")

    def test_the_dispatch_table_was_found(self):
        """A regex that matches nothing would make the real assertion vacuous,
        and this file would then pass for ever while enforcing nothing."""
        self.assertGreater(len(_dispatched_names(self.source)), 100)

    def test_module_level_names_were_found(self):
        """The other half of the same guard."""
        self.assertGreater(len(_module_level_names(self.source)), 5)

    def test_no_dispatched_name_is_defined_in_db_py(self):
        dispatched = _dispatched_names(self.source)
        shadowed = sorted(set(dispatched) & _module_level_names(self.source))
        detail = "\n".join(
            f"  {name}: dispatched to {dispatched[name]}, but db.py defines it "
            f"at module level, so the dispatched copy never runs"
            for name in shadowed
        )
        self.assertEqual(
            shadowed, [],
            "db.py shadows a name its own dispatch table delegates. The local "
            "definition wins and the delegated one is dead code that still "
            "looks live:\n" + detail,
        )

    def test_the_detector_catches_a_shadow(self):
        """Run against the shape it exists to find, rather than trusted to
        work. The 2026-09-10 instance is reproduced verbatim in miniature."""
        source = (
            'async def _ensure_usage_columns() -> None:\n'
            '    pass\n'
            '\n'
            '_DISPATCH = {\n'
            '    "_ensure_usage_columns": "routes.db_usage",\n'
            '    "usage_record": "routes.db_usage",\n'
            '}\n'
        )
        dispatched = _dispatched_names(source)
        self.assertEqual(len(dispatched), 2)
        overlap = set(dispatched) & _module_level_names(source)
        self.assertEqual(overlap, {"_ensure_usage_columns"})

    def test_the_detector_ignores_a_nested_definition(self):
        """A name bound inside a function does not shadow `__getattr__`, and
        flagging it would make this test fire on correct code."""
        source = (
            'def init():\n'
            '    def usage_record():\n'
            '        pass\n'
            '    return usage_record\n'
            '\n'
            '_DISPATCH = {"usage_record": "routes.db_usage"}\n'
        )
        overlap = set(_dispatched_names(source)) & _module_level_names(source)
        self.assertEqual(overlap, set())


if __name__ == "__main__":
    unittest.main()
