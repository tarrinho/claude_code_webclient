"""QA: the task tree must not crash on the real shape of `depends_on`.

Found while double-checking an unrelated feature (the per-task progress
estimate) against real data rather than idealised fixtures. `depends_on` is
stored via `json.dumps(depends_on or [])` (routes/db_orchestrators.py) and
handed to the client untransformed (`state.tasks = data.tasks || []` in
`loadTasks`) -- so every task's `depends_on` field is a JSON-encoded *string*,
never an array. Confirmed against the live database, read-only:

    9cbf264d_t001|[]|text
    fa583523_t001|[]|text
    fa583523_t002|[]|text

`renderTaskTree` (tasks.js) and `computeLayers` (rail.js, added concurrently by
another session while this was being investigated) both called
`.map`/`.join`/`.length`/`.every` on that value as if it were already an array.
`"[]".length` is 2 (truthy) and `"[]" || []` is `"[]"` (also truthy), so neither
guard skipped the string case -- both walked straight into an array method a
string does not have, and threw. That is not a rare path: since the column is
never SQL NULL, this fired for *every* task, on every render, which is what
made this a live-blocking bug for the progress feature living in the very same
per-task callback in tasks.js -- that code never ran either, because the whole
`.map()` over `state.tasks` throws before reaching a later task's turn.

Every existing fixture that exercised this code (test_qa_supervisor_ux_shortcuts.py)
fed an idealised `"depends_on": []`/`["t1"]` -- a real array -- which is why
this went unnoticed: nothing had tested the shape the backend actually sends.

Both files independently gained the same small parser, `_dependsOnList`,
rather than one importing it from the other -- tasks.js already explains why it
keeps its own copy of things rail.js/dom.js also need: ES modules do not share
top-level scope, and the two files do not have a direction that avoids a
cross-import cycle. These tests lift each copy by source (the same technique
tests/test_qa_series_continuity_js.py uses) and drive it directly, rather than
booting the full six-module render pipeline through a browser -- lighter, and
not sensitive to how much memory the browser happens to have free on a box
shared by several concurrent agent sessions, which the full-render version of
this test was.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR_DIR = REPO / "web" / "assets" / "orchestrator"
TASKS_JS = SUPERVISOR_DIR / "tasks.js"
RAIL_JS = SUPERVISOR_DIR / "rail.js"
DB_SUPERVISORS = REPO / "routes" / "db_orchestrators.py"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def lift(path: Path, name: str) -> str:
    """The full text of `function <name>(...)` (exported or not), braces matched.

    Matched by depth, not by the first `}`: `_dependsOnList`'s own try/catch
    has one nested inside it.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"(?:export\s+)?function\s+{re.escape(name)}\s*\(", source)
    if match is None:
        raise AssertionError(
            f"{name}() not found in {path.name}. If it was renamed or inlined, "
            "update this harness rather than letting it skip"
        )
    start = match.start()
    open_brace = source.index("{", match.end() - 1)
    depth, pos = 1, open_brace + 1
    while depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos].replace("export function", "function", 1)


# (label, JS expression) pairs, run against each file's own copy of the parser.
CHECKS: list[tuple[str, str]] = [
    ("the real shape: the string \"[]\"",
     "JSON.stringify(_dependsOnList('[]')) === '[]'"),
    ("a real dependency list, still JSON-encoded",
     "JSON.stringify(_dependsOnList('[\"t1\",\"t0\"]')) === '[\"t1\",\"t0\"]'"),
    ("malformed JSON degrades to no dependencies, not a throw",
     "JSON.stringify(_dependsOnList('not json')) === '[]'"),
    ("a real array passes through unchanged",
     "JSON.stringify(_dependsOnList(['t1'])) === '[\"t1\"]'"),
    ("null becomes no dependencies",
     "JSON.stringify(_dependsOnList(null)) === '[]'"),
    ("undefined becomes no dependencies",
     "JSON.stringify(_dependsOnList(undefined)) === '[]'"),
    ("empty string becomes no dependencies",
     "JSON.stringify(_dependsOnList('')) === '[]'"),
    ("a JSON string that is not an array becomes no dependencies",
     "JSON.stringify(_dependsOnList('{\"a\":1}')) === '[]'"),
]

PAGE = """<!doctype html><meta charset="utf-8"><title>pending</title>
<script>
%(function)s
const checks = [
%(checks)s
];
const failed = [];
for (const [label, check] of checks) {
  let ok = false;
  try { ok = check(); } catch (e) { ok = false; }
  if (!ok) failed.push(label);
}
document.title = failed.length ? 'FAIL: ' + failed.join(' | ') : 'OK';
</script>
"""


def _run_checks(source_file: Path) -> None:
    """Assert every CHECKS entry passes against `_dependsOnList` in `source_file`.

    Raises AssertionError naming exactly which checks failed, or if the page
    never finished at all (chromium crashed, module syntax error).
    """
    function_src = lift(source_file, "_dependsOnList")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(PAGE % {
            "function": function_src,
            "checks": ",\n".join(
                f"  [{json.dumps(label)}, () => ({expr})]" for label, expr in CHECKS
            ),
        })
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             # Chromium writes a ~126 MB profile per launch and leaves it
             # behind if killed rather than exited; pointed at the page
             # directory so this test's own cleanup removes it either way.
             f"--user-data-dir={page.parent}/chrome-profile",
             "--virtual-time-budget=5000", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>([^<]*)</title>", result.stdout)
    title = match.group(1) if match else ""
    if title == "pending":
        raise AssertionError(
            f"the page never ran its script against {source_file.name} -- "
            f"chromium said: {result.stderr[-300:]}"
        )
    if title != "OK":
        raise AssertionError(f"{source_file.name}: {title}")


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class DependsOnParsingTests(unittest.TestCase):
    """Both files' copies of the parser, against the shapes that matter.

    One browser launch per file rather than per case: launching is the slow,
    memory-hungry part, and CHECKS already reports which case failed if one
    does.
    """

    def test_tasks_js_parses_every_real_shape(self):
        _run_checks(TASKS_JS)

    def test_rail_js_parses_every_real_shape(self):
        """Added because rail.js hit the identical bug independently: its own
        `(t.depends_on || []).every(...)` does not skip a truthy string
        either, and it landed in a fresh file while this defect was already
        being investigated."""
        _run_checks(RAIL_JS)


class TheColumnIsAlwaysAJsonStringTests(unittest.TestCase):
    """Confirms the fact the fix and the tests above are built on, so it stops
    being an assertion made once in a chat and starts being checked in CI."""

    def test_supervisor_task_create_always_json_encodes_depends_on(self):
        source = DB_SUPERVISORS.read_text(encoding="utf-8")
        self.assertIn(
            "json.dumps(depends_on or [])", source,
            "if this changed to store NULL or a real list, the frontend's "
            "string-shape assumption in _dependsOnList needs re-checking, "
            "not just this test",
        )


if __name__ == "__main__":
    unittest.main()
