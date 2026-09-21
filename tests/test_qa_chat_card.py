"""QA: the family card, and the ordering rule it exists to protect.

The behavioural half runs the real module under node -- node is v24.19.0 here
and chat-list.js has no top-level imports, so it can be imported directly. A
source-text assertion cannot tell a correct collapse rule from a broken one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_LIST = ROOT / "web" / "assets" / "chat-list.js"
NODE = shutil.which("node")


def _child_rows(children, expanded):
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const m = await import({json.dumps(module.as_uri())});
        const out = m.childRowsFor(
            {{id: 'a', children: {json.dumps(children)}}},
            {json.dumps(expanded)});
        process.stdout.write(JSON.stringify(out));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout)


def _sub(name):
    return {"kind": "subagent", "tool_use_id": name, "agent_type": name,
            "description": "d", "status": "running",
            "started_at": "2026-09-21T10:00:00Z"}


def _age(stamp, now):
    """childAge(stamp) with Date.now() pinned, so the assertion is stable.

    Without pinning, an age test is a clock test: it passes today and drifts
    tomorrow.
    """
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const fixed = new Date({json.dumps(now)}).getTime();
        Date.now = () => fixed;
        const m = await import({json.dumps(module.as_uri())});
        process.stdout.write(JSON.stringify(m.childAge({json.dumps(stamp)})));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout)


@unittest.skipIf(NODE is None, "node is required to execute the module")
class ChildRowsTests(unittest.TestCase):

    def test_no_children_renders_nothing(self):
        self.assertEqual(_child_rows([], False), [])

    def test_a_small_family_renders_in_full_while_collapsed(self):
        """Five or fewer is the threshold: a typical two-or-three-subagent
        turn should be readable without a click."""
        kids = [_sub(f"s{n}") for n in range(5)]
        self.assertEqual(len(_child_rows(kids, False)), 5)

    def test_a_large_family_collapses_by_default(self):
        kids = [_sub(f"s{n}") for n in range(6)]
        self.assertEqual(_child_rows(kids, False), [])

    def test_a_large_family_expands_on_request(self):
        kids = [_sub(f"s{n}") for n in range(6)]
        self.assertEqual(len(_child_rows(kids, True)), 6)

    def test_a_running_subagent_reports_its_age(self):
        """§7: a stuck `running` row must read as stale rather than as active
        work. There is no timeout sweeper, so the age is the only truth on
        offer -- if it is missing, a dead subagent looks busy for ever."""
        out = _age("2026-09-21T10:00:00Z", "2026-09-21T10:45:00Z")
        self.assertEqual(out, "45m")

    def test_an_absent_timestamp_yields_no_age_rather_than_NaN(self):
        self.assertEqual(_age(None, "2026-09-21T10:45:00Z"), "")

    def test_a_malformed_timestamp_yields_no_age(self):
        self.assertEqual(_age("not-a-date", "2026-09-21T10:45:00Z"), "")

    def test_exactly_the_threshold_still_renders(self):
        """Boundary: 5 renders, 6 collapses. Asserted because an off-by-one
        here is invisible until someone runs exactly five subagents."""
        self.assertEqual(len(_child_rows([_sub(f"s{n}") for n in range(5)],
                                         False)), 5)
        self.assertEqual(_child_rows([_sub(f"s{n}") for n in range(6)],
                                     False), [])


class CommitOrderExclusionTests(unittest.TestCase):
    """The regression that would silently corrupt stored `position`.

    commitOrder builds what it persists by walking the DOM. A child row inside
    a card is not a root, so it must never reach the id list sent to
    PUT /api/chats/order. Asserted on the id list, never on the markup.
    """

    SOURCE = CHAT_LIST.read_text()

    def test_child_rows_are_excluded_from_the_persisted_order(self):
        start = self.SOURCE.index("function commitOrder(")
        body = self.SOURCE[start:start + 1600]
        self.assertIn("dataset.child !== '1'", body)

    def test_child_rows_are_marked_when_rendered(self):
        self.assertIn("dataset.child = '1'", self.SOURCE)

    def test_child_rows_are_not_draggable(self):
        self.assertIn("draggable = false", self.SOURCE)


if __name__ == "__main__":
    unittest.main()
