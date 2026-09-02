"""QA: the two surfaces that classify conversations must agree on the *data*.

`classify_chat` is called from two places — the sidebar feed and the supervisor
members panel — and centralising it was the right move. But centralising a
decision does not centralise its arguments, and that is where this went wrong
once already: the members panel passed `{}, {}, {}` for the three CLI lookups,
so it could not see that a linked terminal was still working and promoted
running work to "finished", while the sidebar held the same rule with better
inputs and correctly kept quiet. Two surfaces, one function, and still a
disagreement — because the shared thing was the logic and not the data.

cweb3's formulation is the one worth keeping: **when a decision is centralised,
its arguments become the duplication.**

It is fixed: both callers now take their maps from `await _cli_maps(marks)`.
Nothing enforces that, though, and the 0.10.0 reorganisation is precisely the
change that would let it drift — once the two callers live in different modules,
"they both use the same helper" stops being visible on one screen.

So this file guards the property in two ways, because neither alone is enough:

* **Structurally**, that every caller of `classify_chat` obtains its maps from
  `_cli_maps` and none builds them inline. This survives the callers moving to
  separate files, which is the point.
* **Behaviourally**, that the two surfaces agree when a CLI session is actually
  in play. The existing agreement test in `test_qa_supervisor_members.py` uses
  web chats only, so the maps are empty on both sides — and passing `{}` is
  indistinguishable from passing the real thing. That is the exact blind spot
  the original bug shipped through.
"""
from __future__ import annotations

import ast
import pathlib
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import app
import config
import db

ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class ClassifierInputSourceTests(unittest.TestCase):
    """Structural: nobody assembles the classifier's inputs by hand."""

    @staticmethod
    def _callers_of_classify_chat():
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        found = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "classify_chat"):
                    found.append((fn, node))
        return found

    def test_there_are_callers_to_check(self):
        """Guards the guard: if `classify_chat` is renamed or the callers move
        out of app.py, every assertion below passes vacuously."""
        callers = self._callers_of_classify_chat()
        self.assertGreaterEqual(
            len(callers), 2,
            "expected the sidebar and the members panel to both classify; if "
            "this dropped to one, either a surface stopped classifying or this "
            "scan stopped finding it",
        )

    def test_every_caller_takes_its_maps_from_the_shared_helper(self):
        offenders = []
        for fn, _call in self._callers_of_classify_chat():
            body = ast.dump(fn)
            if "_cli_maps" not in body:
                offenders.append(fn.name)
        self.assertEqual(
            offenders, [],
            "these classify conversations without taking the CLI lookups from "
            "_cli_maps, so they can disagree with the other surface about the "
            "data while agreeing about the rule:\n  " + "\n  ".join(offenders),
        )

    def test_no_caller_passes_empty_maps(self):
        """The literal shape of the original defect.

        `classify_chat(chat, last, live_ids, queued, marks, {}, {}, {})` is
        syntactically fine, reads as a caller that has nothing to say, and is
        how a surface came to report finished work as running.
        """
        offenders = []
        for fn, call in self._callers_of_classify_chat():
            empties = [a for a in call.args
                       if isinstance(a, ast.Dict) and not a.keys]
            if empties:
                offenders.append(f"{fn.name} passes {len(empties)} empty dict(s)")
        self.assertEqual(offenders, [], "\n  ".join(offenders))


class SurfacesAgreeWithACliSessionTests(unittest.IsolatedAsyncioTestCase):
    """Behavioural: the two surfaces agree when the maps are not empty.

    Patching `_cli_maps` is deliberate and is what makes this discriminating. If
    either surface built its maps inline instead of calling the helper, the
    patch would not reach it, the two would see different data, and the
    assertion would fail — which is precisely the divergence being guarded.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.sup = uuid.uuid4().hex
        await db.supervisor_create(self.sup, "Release", "", "alice")
        self.chat = uuid.uuid4().hex
        await db.chat_create(self.chat, "linked agent", "", f"{self.tmp.name}/p",
                             "alice")
        await db.chat_set_session(self.chat, SESSION_ID)
        await db.messages_batch(self.chat, [("user", "go"), ("assistant", "on it")])
        await db.supervisor_member_add(self.sup, self.chat)

    def _request(self):
        return types.SimpleNamespace(
            method="GET",
            url=types.SimpleNamespace(path="/api/supervisor"),
            cookies={}, headers={}, query_params={},
            client=types.SimpleNamespace(host="127.0.0.1"),
            state=types.SimpleNamespace(session={"user": "alice", "role": "admin"}),
            json=AsyncMock(return_value={}),
            is_disconnected=AsyncMock(return_value=False),
        )

    async def _both_surfaces(self, cli_status: str):
        """The status each surface reports for the same chat, same inputs."""
        import json
        # Fourth map: whether the session is showing a prompt on its terminal.
        # Empty here on purpose. This case is about the two surfaces agreeing
        # from the session's *status*, so leaving it empty keeps a screen read
        # from supplying an answer that status alone should decide.
        maps = ({SESSION_ID: cli_status}, {}, {SESSION_ID: "2026-09-01T00:00:00Z"},
                {})
        with patch.object(app, "_cli_maps", AsyncMock(return_value=maps)):
            feed = json.loads(bytes(
                (await app.handle_supervisor(self._request())).body))
            members = json.loads(bytes(
                (await app.handle_supervisor_members_get(
                    self._request(), self.sup)).body))["members"]
        sidebar = None
        for bucket in ("waiting", "working", "updated"):
            for entry in feed.get(bucket, []):
                if entry.get("id") == self.chat:
                    sidebar = entry.get("status")
        panel = next((m.get("status") for m in members
                      if m.get("id") == self.chat), None)
        return sidebar, panel

    async def test_a_busy_terminal_reads_the_same_on_both_surfaces(self):
        """The case the original bug got wrong: work still running.

        The members panel could not see the terminal was busy, so it announced
        the agent as finished while the sidebar kept it quiet.
        """
        sidebar, panel = await self._both_surfaces("busy")
        self.assertEqual(
            panel, sidebar,
            f"the members panel says {panel!r} and the sidebar says {sidebar!r} "
            "for the same conversation with the same inputs",
        )

    async def test_an_idle_terminal_also_reads_the_same(self):
        """The other side of the branch, so a fix that hardcoded one answer
        cannot pass."""
        sidebar, panel = await self._both_surfaces("idle")
        self.assertEqual(panel, sidebar)

    async def test_the_maps_actually_reach_the_classifier(self):
        """Without this the two tests above pass when both surfaces ignore the
        maps entirely — agreeing on nothing is still agreement."""
        busy, _ = await self._both_surfaces("busy")
        idle, _ = await self._both_surfaces("idle")
        self.assertNotEqual(
            busy, idle,
            "the CLI status made no difference to the classification, so these "
            "tests would agree whatever the maps contained",
        )


if __name__ == "__main__":
    unittest.main()
