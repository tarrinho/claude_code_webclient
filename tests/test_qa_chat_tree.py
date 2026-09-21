"""QA: composing the sidebar tree from three unrelated sources.

`build_chat_tree` is deliberately pure -- no database, no DOM, no network.
The three relations have different shapes and different failure modes, and a
composer that fetched its own inputs could not be tested without standing up
all three.
"""
from __future__ import annotations

import unittest

from chat_tree import build_chat_tree


def _chat(chat_id, **kw):
    row = {"id": chat_id, "title": chat_id, "parent_chat_id": None}
    row.update(kw)
    return row


def _sub(tool_use_id, status="running"):
    return {"tool_use_id": tool_use_id, "agent_type": "x", "description": "d",
            "status": status, "started_at": "2026-09-21T10:00:00Z",
            "ended_at": None}


class BuildChatTreeTests(unittest.TestCase):

    def test_a_lone_chat_is_a_root_with_no_children(self):
        out = build_chat_tree([_chat("a")], {}, {})
        self.assertEqual([c["id"] for c in out], ["a"])
        self.assertEqual(out[0]["children"], [])

    def test_children_is_always_present(self):
        """The client renders what the server composed and never infers a
        relation, so the key must not be conditional."""
        out = build_chat_tree([_chat("a")], {}, {})
        self.assertIn("children", out[0])

    def test_a_subagent_becomes_a_child_of_its_chat(self):
        out = build_chat_tree([_chat("a")], {"a": [_sub("tu_1")]}, {})
        self.assertEqual(len(out[0]["children"]), 1)
        self.assertEqual(out[0]["children"][0]["kind"], "subagent")
        self.assertEqual(out[0]["children"][0]["tool_use_id"], "tu_1")

    def test_a_voice_child_nests_under_its_parent_and_leaves_the_roots(self):
        out = build_chat_tree(
            [_chat("parent"), _chat("kid", parent_chat_id="parent")], {}, {})
        self.assertEqual([c["id"] for c in out], ["parent"])
        kids = out[0]["children"]
        self.assertEqual([k["id"] for k in kids], ["kid"])
        self.assertEqual(kids[0]["kind"], "chat")

    def test_an_orchestrator_member_nests_under_the_orchestrator(self):
        out = build_chat_tree(
            [_chat("orch"), _chat("task1")], {}, {"task1": "orch"})
        self.assertEqual([c["id"] for c in out], ["orch"])
        self.assertEqual([k["id"] for k in out[0]["children"]], ["task1"])

    def test_orchestrator_membership_wins_over_a_voice_parent(self):
        """§8's precedence: orchestrator member -> voice parent -> root. A chat
        appears exactly once, so a tie must resolve, not duplicate."""
        out = build_chat_tree(
            [_chat("orch"), _chat("vparent"),
             _chat("both", parent_chat_id="vparent")],
            {}, {"both": "orch"})
        placed = {c["id"]: [k["id"] for k in c["children"]] for c in out}
        self.assertEqual(placed.get("orch"), ["both"])
        self.assertEqual(placed.get("vparent"), [])

    def test_a_chat_appears_exactly_once(self):
        out = build_chat_tree(
            [_chat("orch"), _chat("vparent"),
             _chat("both", parent_chat_id="vparent")],
            {}, {"both": "orch"})
        seen = [c["id"] for c in out] + [
            k["id"] for c in out for k in c["children"] if k["kind"] == "chat"]
        self.assertEqual(sorted(seen), ["both", "orch", "vparent"])

    def test_an_orphan_renders_as_a_root_rather_than_vanishing(self):
        """Losing a conversation because its parent was archived, deleted or
        filtered out by a search would be worse than showing it unnested."""
        out = build_chat_tree([_chat("kid", parent_chat_id="gone")], {}, {})
        self.assertEqual([c["id"] for c in out], ["kid"])

    def test_an_orphaned_orchestrator_member_also_renders_as_a_root(self):
        out = build_chat_tree([_chat("task1")], {}, {"task1": "missing"})
        self.assertEqual([c["id"] for c in out], ["task1"])

    def test_nesting_stops_at_one_level(self):
        """A grandchild attaches to the nearest present ancestor rather than
        creating a second indent level. A malformed parent_chat_id must not be
        able to make the sidebar recurse."""
        out = build_chat_tree(
            [_chat("a"), _chat("b", parent_chat_id="a"),
             _chat("c", parent_chat_id="b")], {}, {})
        self.assertEqual([c["id"] for c in out], ["a"])
        kids = {k["id"] for k in out[0]["children"]}
        self.assertEqual(kids, {"b", "c"})

    def test_a_self_referencing_parent_is_a_root_not_a_hang(self):
        out = build_chat_tree([_chat("a", parent_chat_id="a")], {}, {})
        self.assertEqual([c["id"] for c in out], ["a"])
        self.assertEqual(out[0]["children"], [])

    def test_a_two_chat_cycle_terminates(self):
        out = build_chat_tree(
            [_chat("a", parent_chat_id="b"), _chat("b", parent_chat_id="a")],
            {}, {})
        self.assertEqual([c["id"] for c in out], ["a", "b"])
        self.assertEqual(out[0]["children"], [])
        self.assertEqual(out[1]["children"], [])

    def test_a_two_chat_cycle_is_order_independent(self):
        """The up-front cycle pass exists precisely so this does not depend
        on which row comes first. An earlier draft attached as it walked, so
        for a two-chat cycle whichever row came first became the other's
        parent -- reversing the input here must still leave both as roots
        with no children, never one nested under the other."""
        out = build_chat_tree(
            [_chat("b", parent_chat_id="a"), _chat("a", parent_chat_id="b")],
            {}, {})
        self.assertEqual([c["id"] for c in out], ["b", "a"])
        self.assertEqual(out[0]["children"], [])
        self.assertEqual(out[1]["children"], [])

    def test_a_chain_deeper_than_the_bound_promotes_the_tail(self):
        """`_host_for`'s walk is bounded; a chain longer than that bound has
        its tail fall out of reach and come back as an extra root instead of
        flattening onto the chain's real root. This is also the only test
        that reaches the `host is None` promotion branch through a chat
        whose immediate parent IS present (as opposed to missing outright)."""
        out = build_chat_tree(
            [_chat("a"), _chat("b", parent_chat_id="a"),
             _chat("c", parent_chat_id="b"),
             _chat("d", parent_chat_id="c"),
             _chat("e", parent_chat_id="d")],
            {}, {})
        self.assertEqual([c["id"] for c in out], ["a", "e"])
        self.assertEqual({k["id"] for k in out[0]["children"]}, {"b", "c", "d"})
        self.assertEqual(out[1]["children"], [])

    def test_a_promoted_orphan_still_gets_its_subagents(self):
        """The subagent-attachment loop must cover chats promoted by the
        `host is None` branch too, not only the roots that existed before
        promotion -- otherwise a promoted chat silently loses its own
        subagent children."""
        out = build_chat_tree(
            [_chat("kid", parent_chat_id="gone")],
            {"kid": [_sub("tu_1")]}, {})
        self.assertEqual([c["id"] for c in out], ["kid"])
        self.assertEqual(len(out[0]["children"]), 1)
        self.assertEqual(out[0]["children"][0]["kind"], "subagent")

    def test_relation_is_orchestrator_for_a_normal_member(self):
        out = build_chat_tree(
            [_chat("orch"), _chat("task1")], {}, {"task1": "orch"})
        self.assertEqual(out[0]["children"][0]["relation"], "orchestrator")

    def test_relation_is_voice_when_the_named_orchestrator_is_absent(self):
        """member_of can name an orchestrator that is not in `chats`
        (archived, deleted, or filtered out by a search). _parent_of then
        falls through to the voice parent, so the label must say "voice" --
        deriving it from member_of alone (rather than from the parent that
        actually won) would mislabel this reachable case as "orchestrator"."""
        out = build_chat_tree(
            [_chat("vparent"), _chat("kid", parent_chat_id="vparent")],
            {}, {"kid": "missing_orch"})
        self.assertEqual([c["id"] for c in out], ["vparent"])
        self.assertEqual(out[0]["children"][0]["relation"], "voice")

    def test_subagents_and_chat_children_share_one_list(self):
        out = build_chat_tree(
            [_chat("a"), _chat("kid", parent_chat_id="a")],
            {"a": [_sub("tu_1")]}, {})
        kinds = sorted(k["kind"] for k in out[0]["children"])
        self.assertEqual(kinds, ["chat", "subagent"])

    def test_root_order_is_preserved(self):
        """The endpoint already sorted by favourites, placement and recency.
        The composer must not resort."""
        out = build_chat_tree([_chat("z"), _chat("a"), _chat("m")], {}, {})
        self.assertEqual([c["id"] for c in out], ["z", "a", "m"])

    def test_the_input_rows_are_not_mutated(self):
        rows = [_chat("a")]
        build_chat_tree(rows, {}, {})
        self.assertNotIn("children", rows[0])
