"""Tests for /api/supervisor-map endpoint (sync surface tests).

Most tests here exercise the pure functions that assemble the tree, because
each one answers a question with a single right answer: which transport a
conversation belongs to, what a group larger than the cap looks like, what a
backend's status is.

Three classes run the async supervisor_map() instead, and only for facts the
helpers cannot show on their own. CapacityOnMachineNodesTests: which nodes get
a capacity_* pair. MapAssemblyTests: which group a conversation is filed
under, what a task is typed as, whether terminal sessions appear at all, how
many queries one orchestrator costs -- and whether the assembly loop calls the
status helper at all, which is the gap that let `_aggregate_status([])` sit
there leaving every machine node idle while the helper's own unit tests
passed. SupervisorMapEndpointTests: what survives the route.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
import resource_guard
import transcripts
from routes.db_supervisor_map import (
    _MAX_CHILDREN, _aggregate_status, _capped, _chat_node, _chat_transport,
    _machine_status, _normalise, _task_status, supervisor_map,
)

HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
    )


class TestAggregateStatus:
    """Status aggregation rules."""

    def test_single_running(self):
        assert _aggregate_status([{"status": "running"}]) == "running"

    def test_single_busy(self):
        assert _aggregate_status([{"status": "busy"}]) == "running"

    def test_single_waiting(self):
        assert _aggregate_status([{"status": "waiting"}]) == "waiting"

    def test_single_idle(self):
        assert _aggregate_status([{"status": "idle"}]) == "idle"

    def test_single_error(self):
        assert _aggregate_status([{"status": "error"}]) == "error"

    def test_single_done(self):
        assert _aggregate_status([{"status": "done"}]) == "done"

    def test_empty(self):
        assert _aggregate_status([]) == "idle"

    def test_mixed_idle_and_busy(self):
        assert _aggregate_status([
            {"status": "idle"},
            {"status": "busy"},
        ]) == "running"

    def test_error_overrides_running(self):
        assert _aggregate_status([
            {"status": "running"},
            {"status": "error"},
        ]) == "error"

    def test_waiting_over_idle(self):
        assert _aggregate_status([
            {"status": "idle"},
            {"status": "waiting"},
        ]) == "waiting"

    def test_all_done(self):
        assert _aggregate_status([
            {"status": "done"},
            {"status": "done"},
        ]) == "done"

    def test_a_parents_status_covers_children_the_cap_will_hide(self):
        """Aggregation runs on the full group, before _capped trims it.

        Reversing those two would make the map's colours depend on how many
        siblings a node happens to have: a failing conversation in position
        13 of a group would leave its transport showing idle.
        """
        nodes = [{"id": f"c{i}", "label": f"Chat {i}", "status": "idle",
                  "type": "chat"} for i in range(20)]
        nodes[15]["status"] = "error"
        assert _aggregate_status(nodes) == "error"
        assert _capped(nodes, "t")[0]["status"] == "idle"


class TestCapped:
    """What happens to a group larger than the cap.

    This replaces a test that asserted the old `[:4]` slice was correct
    behaviour. It was not: three separate slices dropped nodes with nothing on
    screen to say so, so a host with six backends showed four and looked
    complete. The cap still exists -- one enormous group makes the whole radial
    tree unreadable -- but what it drops is now named in the tree.
    """

    def _nodes(self, count: int) -> list[dict]:
        return [{"id": f"n{i}", "label": f"N{i}", "status": "idle",
                 "type": "chat"} for i in range(count)]

    def test_a_small_group_is_returned_untouched(self):
        nodes = self._nodes(3)
        assert _capped(nodes, "t") == nodes

    def test_a_group_exactly_at_the_cap_gets_no_marker(self):
        """Off-by-one guard: at the cap nothing is hidden, so a "+0 more"
        node would be a lie about a complete group."""
        nodes = self._nodes(_MAX_CHILDREN)
        assert _capped(nodes, "t") == nodes

    def test_one_over_the_cap_names_the_one_it_hid(self):
        result = _capped(self._nodes(_MAX_CHILDREN + 1), "t")
        assert len(result) == _MAX_CHILDREN + 1
        assert result[-1]["type"] == "more"
        assert result[-1]["label"] == "+1 more"
        assert result[-1]["hidden_count"] == 1

    def test_the_marker_counts_every_hidden_node(self):
        result = _capped(self._nodes(_MAX_CHILDREN + 7), "t")
        assert result[-1]["hidden_count"] == 7
        assert result[-1]["label"] == "+7 more"

    def test_the_kept_nodes_keep_their_order(self):
        nodes = self._nodes(_MAX_CHILDREN + 5)
        result = _capped(nodes, "t")
        assert result[:_MAX_CHILDREN] == nodes[:_MAX_CHILDREN]

    def test_the_marker_id_is_scoped_to_its_group(self):
        """Two overflowing groups in one tree must not both emit the same id:
        d3's data join is keyed on it, so a duplicate makes one of the two
        markers vanish."""
        a = _capped(self._nodes(_MAX_CHILDREN + 1), "direct")[-1]
        b = _capped(self._nodes(_MAX_CHILDREN + 1), "t-2")[-1]
        assert a["id"] != b["id"]


class TestNormalise:
    """classify_chat status -> map vocabulary mapping."""

    def test_working(self):
        assert _normalise("working") == "running"

    def test_updated(self):
        assert _normalise("updated") == "running"

    def test_idle(self):
        assert _normalise("idle") == "idle"

    def test_waiting(self):
        assert _normalise("waiting") == "waiting"

    def test_running(self):
        assert _normalise("running") == "running"

    def test_busy(self):
        assert _normalise("busy") == "busy"

    def test_error(self):
        assert _normalise("error") == "error"

    def test_done(self):
        assert _normalise("done") == "done"

    def test_unknown(self):
        assert _normalise("unknown") == "idle"


class TestTaskStatus:
    """Orchestrator task row -> map status."""

    def test_pending(self):
        assert _task_status({"status": "pending"}) == "idle"

    def test_running(self):
        assert _task_status({"status": "running"}) == "busy"

    def test_error(self):
        assert _task_status({"status": "error"}) == "error"

    def test_done(self):
        assert _task_status({"status": "done"}) == "done"

    def test_planning(self):
        assert _task_status({"status": "planning"}) == "idle"

    def test_no_status(self):
        assert _task_status({}) == "idle"


class TestChatTransport:
    """Which transport group a conversation belongs in.

    The defect: this read ``chat["transport_id"]`` -- a column ``chats`` does
    not have. `.get` on a missing key is indistinguishable from a real NULL,
    so it returned None for every conversation ever created and nothing
    raised. Every direct chat therefore landed in the "Direct" group no matter
    which SSH-proxied backend was serving it, which is precisely the fact the
    map exists to show.
    """

    MACHINES = {
        "m-local": {"id": "m-local", "transport_id": None},
        "m-remote": {"id": "m-remote", "transport_id": "t-kali3"},
    }

    def test_a_chat_on_a_proxied_machine_follows_that_transport(self):
        chat = {"id": "c1", "ai_machine_id": "m-remote"}
        assert _chat_transport(chat, self.MACHINES) == "t-kali3"

    def test_a_chat_on_a_local_machine_stays_direct(self):
        chat = {"id": "c1", "ai_machine_id": "m-local"}
        assert _chat_transport(chat, self.MACHINES) is None

    def test_a_chat_with_no_machine_stays_direct(self):
        assert _chat_transport({"id": "c1"}, self.MACHINES) is None

    def test_a_chat_naming_a_deleted_machine_stays_direct(self):
        """A dangling ai_machine_id must not raise: the machine can be
        removed while its conversations are still listed."""
        chat = {"id": "c1", "ai_machine_id": "m-gone"}
        assert _chat_transport(chat, self.MACHINES) is None

    def test_a_chats_own_transport_id_is_ignored_if_one_ever_appears(self):
        """Guard against the original bug returning by a different route: the
        machine is the authority, so a stray key on the chat must not win."""
        chat = {"id": "c1", "ai_machine_id": "m-remote", "transport_id": "t-wrong"}
        assert _chat_transport(chat, self.MACHINES) == "t-kali3"


class TestChatNode:
    """The three states a chat node used to drop."""

    MACHINES = {"m1": {"id": "m1", "provider": "claude_code"}}

    def test_a_degraded_chat_is_an_error_whatever_else_it_looks_like(self):
        """classify_chat never looks at the degraded column, so without this
        a conversation the app had already given up on read as plain idle."""
        node = _chat_node(
            {"id": "c1", "title": "T", "degraded": 1,
             "degraded_reason": "backend refused"},
            {"c1": "idle"}, {}, self.MACHINES,
        )
        assert node["status"] == "error"
        assert node["degraded_reason"] == "backend refused"

    def test_a_healthy_chat_carries_no_degraded_keys(self):
        node = _chat_node({"id": "c1", "title": "T"}, {}, {}, self.MACHINES)
        assert node["status"] == "idle"
        assert "degraded_reason" not in node

    def test_queued_prompts_are_counted(self):
        node = _chat_node({"id": "c1", "title": "T"}, {}, {"c1": 3}, self.MACHINES)
        assert node["queued"] == 3

    def test_zero_queued_prompts_adds_no_key(self):
        """An absent key and a 0 must not render the same as "Queued: 0"."""
        node = _chat_node({"id": "c1", "title": "T"}, {}, {"c1": 0}, self.MACHINES)
        assert "queued" not in node

    def test_voice_mode_is_flagged(self):
        node = _chat_node(
            {"id": "c1", "title": "T", "voice_mode": 1}, {}, {}, self.MACHINES,
        )
        assert node["voice_mode"] is True

    def test_a_working_status_is_normalised(self):
        node = _chat_node({"id": "c1", "title": "T"}, {"c1": "working"}, {},
                          self.MACHINES)
        assert node["status"] == "running"


class TestMachineStatus:
    """A backend's status comes from the work routed to it.

    It used to be ``_aggregate_status([])`` -- unconditionally "idle" -- so a
    backend with a failing conversation on it looked exactly like an unused
    one.
    """

    def test_a_machine_with_no_conversations_is_idle(self):
        assert _machine_status({"id": "m1"}, {}, {}) == "idle"

    def test_a_failing_conversation_makes_its_machine_an_error(self):
        status = _machine_status(
            {"id": "m1"}, {"m1": ["c1", "c2"]}, {"c1": "idle", "c2": "error"},
        )
        assert status == "error"

    def test_a_working_conversation_makes_its_machine_running(self):
        status = _machine_status({"id": "m1"}, {"m1": ["c1"]}, {"c1": "working"})
        assert status == "running"

    def test_another_machines_conversations_do_not_count(self):
        status = _machine_status(
            {"id": "m1"}, {"m2": ["c1"]}, {"c1": "error"},
        )
        assert status == "idle"

    def test_an_unclassified_conversation_is_skipped_not_guessed(self):
        """chat_status only holds conversations with recorded activity. A
        missing entry must not be read as a status."""
        status = _machine_status({"id": "m1"}, {"m1": ["c1"]}, {})
        assert status == "idle"

    def test_a_disabled_machine_is_not_reported_as_a_fault(self):
        """"Switched off" is a configuration fact shown in Settings ->
        Backends. Colouring it as an error on the map would report a chosen
        state as a failure."""
        status = _machine_status({"id": "m1", "enabled": 0}, {}, {})
        assert status == "idle"


class CapacityOnMachineNodesTests(unittest.IsolatedAsyncioTestCase):
    """Existing vs total is attached to nodes backed by this host, and only
    those: an SSH-proxied machine is a different host's memory, which
    resource_guard was deliberately scoped to not claim to measure (see
    docs/superpowers/specs/2026-09-08-resource-guard-design.md and the
    brainstorming that scoped this feature to local nodes only).
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        # A capacity() this test controls, rather than the real host's
        # /proc -- the point under test is which nodes carry the numbers, not
        # what the numbers are.
        self.capacity_patch = patch.object(
            resource_guard, "capacity",
            return_value={"existing": 3, "total": 6, "cost_mb": 320,
                          "floor_mb": 400, "available_mb": 1360},
        )
        self.capacity_patch.start()
        self.addCleanup(self.capacity_patch.stop)

    async def test_a_direct_machine_node_carries_the_host_capacity(self):
        await db.ai_machine_create(
            "m-direct", "Anthropic API", "", 0, None, "claude-opus-5",
            None, None, "admin", provider="claude_code",
        )
        tree = await supervisor_map("admin")
        node = _machine_node(tree, "m-direct")
        self.assertEqual(node["capacity_existing"], 3)
        self.assertEqual(node["capacity_total"], 6)

    async def test_an_ssh_proxied_machine_node_carries_no_capacity(self):
        await db.ai_machine_create(
            "m-remote", "Kali3", "", 0, None, "claude-opus-5",
            None, None, "admin", provider="claude_code",
            transport_id="t-1",
        )
        tree = await supervisor_map("admin")
        node = _machine_node(tree, "m-remote")
        self.assertNotIn("capacity_existing", node)
        self.assertNotIn("capacity_total", node)

    async def test_two_direct_machines_share_the_same_reading(self):
        """One /proc scan for the whole map, not one per node -- both direct
        machines must report the identical pair from that single call."""
        await db.ai_machine_create(
            "m-a", "A", "", 0, None, "claude-opus-5", None, None, "admin",
            provider="claude_code",
        )
        await db.ai_machine_create(
            "m-b", "B", "", 0, None, "claude-sonnet-5", None, None, "admin",
            provider="claude_code",
        )
        tree = await supervisor_map("admin")
        a = _machine_node(tree, "m-a")
        b = _machine_node(tree, "m-b")
        self.assertEqual(a["capacity_existing"], b["capacity_existing"])
        self.assertEqual(a["capacity_total"], b["capacity_total"])
        resource_guard.capacity.assert_called_once()


class MapAssemblyTests(unittest.IsolatedAsyncioTestCase):
    """The assembly loop, against a real database.

    These four facts are only observable through supervisor_map() itself:
    which group a conversation is filed under, what a task node is typed as,
    whether terminal sessions appear at all, and how many queries one
    orchestrator costs.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.capacity_patch = patch.object(
            resource_guard, "capacity",
            return_value={"existing": 1, "total": 4, "cost_mb": 320,
                          "floor_mb": 400, "available_mb": 1680},
        )
        self.capacity_patch.start()
        self.addCleanup(self.capacity_patch.stop)
        # No terminal sessions unless a test asks for them: the real registry
        # is this host's own ~/.claude state, which would make these tests
        # depend on whatever the developer happens to have running.
        self.sessions_patch = patch.object(db, "read_claude_sessions", return_value=[])
        self.sessions_patch.start()
        self.addCleanup(self.sessions_patch.stop)

    async def _chat_on(self, chat_id: str, machine_id: str | None) -> None:
        await db.chat_create(chat_id, f"Chat {chat_id}", None, "/tmp", "admin")
        if machine_id:
            await db.db_conn.execute(
                "UPDATE chats SET ai_machine_id = ? WHERE id = ?",
                (machine_id, chat_id),
            )
            await db.db_conn.commit()

    def _group(self, tree: dict, key: str) -> dict:
        for child in tree.get("children", []):
            if child.get("id") == key:
                return child
        raise AssertionError(f"no transport group {key!r} in {tree!r}")

    async def test_a_chat_is_filed_under_its_machines_transport(self):
        """The headline defect: grouping read a column `chats` does not have,
        so every conversation landed in "Direct" regardless of its backend."""
        await db.ai_machine_create(
            "m-remote", "Kali3", "", 0, None, "claude-opus-5", None, None,
            "admin", provider="claude_code", transport_id="t-1",
        )
        await self._chat_on("c-remote", "m-remote")

        tree = await supervisor_map("admin")

        ids = [n["id"] for n in self._group(tree, "t-1")["children"]]
        self.assertIn("c-remote", ids)
        with self.assertRaises(AssertionError):
            self._group(tree, "direct")

    async def test_a_chat_with_no_machine_stays_in_the_direct_group(self):
        await db.ai_machine_create(
            "m-local", "Anthropic API", "", 0, None, "claude-opus-5", None,
            None, "admin", provider="claude_code",
        )
        await self._chat_on("c-local", None)

        tree = await supervisor_map("admin")

        ids = [n["id"] for n in self._group(tree, "direct")["children"]]
        self.assertIn("c-local", ids)

    async def test_an_orchestrator_task_is_typed_as_a_task(self):
        """Tasks were typed "chat", so the route's _enrich_messages queried
        messages_last() with a task id on every request -- always finding
        nothing -- and the drawer offered conversation actions for a row that
        has no conversation."""
        await db.orchestrator_create("o-1", "Build it", None, "admin")
        await db.orchestrator_task_create("o-1", "task-1", "Do the thing", None)

        tree = await supervisor_map("admin")

        orch = next(
            n for n in self._group(tree, "direct")["children"]
            if n["type"] == "orchestrator"
        )
        task = next(n for n in orch["children"] if n["id"] == "task-1")
        self.assertEqual(task["type"], "task")
        self.assertEqual(task["label"], "Do the thing")

    async def test_one_orchestrator_costs_one_members_query(self):
        """The members list was fetched twice for every orchestrator: once to
        build the chat -> orchestrator index, once again inside the assembly
        loop for the same id."""
        await db.orchestrator_create("o-1", "Build it", None, "admin")
        await db.orchestrator_task_create("o-1", "task-1", "T", None)
        with patch.object(
            db, "orchestrator_members_list", return_value=[],
        ) as members:
            await supervisor_map("admin")
        self.assertEqual(members.call_count, 1, members.call_args_list)

    async def test_a_terminal_session_appears_on_the_map(self):
        """read_claude_sessions() and _classify_cli_session were both already
        being fetched and imported here and then never used, so the map showed
        no terminal sessions at all while paying for the read."""
        self.sessions_patch.stop()
        self.addCleanup(self.sessions_patch.start)
        with patch.object(
            db, "read_claude_sessions",
            return_value=[{"sessionId": "s-1", "name": "cweb1", "status": "busy"}],
        ), patch.object(
            transcripts, "list_recent",
            return_value=[{"session_id": "s-1", "title": "cweb1",
                           "updated_at": 0}],
        ):
            tree = await supervisor_map("admin")

        node = next(
            n for n in self._group(tree, "direct")["children"]
            if n["type"] == "session"
        )
        self.assertEqual(node["id"], "s-1")
        self.assertEqual(node["status"], "running")

    async def test_a_webconsole_shadow_session_is_not_duplicated(self):
        """A shadow record describes a conversation that is already a node."""
        self.sessions_patch.stop()
        self.addCleanup(self.sessions_patch.start)
        with patch.object(
            db, "read_claude_sessions",
            return_value=[{"sessionId": "s-1", "status": "busy",
                           "entrypoint": "webconsole"}],
        ), patch.object(
            transcripts, "list_recent",
            return_value=[{"session_id": "s-1", "title": "x", "updated_at": 0}],
        ):
            tree = await supervisor_map("admin")

        for child in tree.get("children", []):
            for node in child.get("children", []):
                self.assertNotEqual(node.get("type"), "session")

    async def test_an_unreadable_session_registry_does_not_lose_the_tree(self):
        """One panel among many: a missing terminal session is better than a
        500 for the whole map."""
        self.sessions_patch.stop()
        self.addCleanup(self.sessions_patch.start)
        await self._chat_on("c-1", None)
        with patch.object(
            db, "read_claude_sessions", side_effect=OSError("registry gone"),
        ):
            tree = await supervisor_map("admin")

        ids = [n["id"] for n in self._group(tree, "direct")["children"]]
        self.assertIn("c-1", ids)

    async def test_a_machine_reports_the_state_of_the_work_on_it(self):
        """The assembly loop must actually call _machine_status. Reverting
        that one call to `_aggregate_status([])` left every machine node
        unconditionally idle and was caught by nothing: the helper's own unit
        tests pass whether or not anything uses it.
        """
        await db.ai_machine_create(
            "m-local", "Anthropic API", "", 0, None, "claude-opus-5", None,
            None, "admin", provider="claude_code",
        )
        await self._chat_on("c-bad", "m-local")
        await db.chat_mark_degraded("c-bad", "backend", "refused")

        tree = await supervisor_map("admin")

        self.assertEqual(_machine_node(tree, "m-local")["status"], "error")

    async def test_a_machine_with_only_healthy_work_is_not_an_error(self):
        """The other half of the pair: without it the test above would pass
        just as well against a machine hardcoded to "error"."""
        await db.ai_machine_create(
            "m-local", "Anthropic API", "", 0, None, "claude-opus-5", None,
            None, "admin", provider="claude_code",
        )
        await self._chat_on("c-ok", "m-local")

        tree = await supervisor_map("admin")

        self.assertEqual(_machine_node(tree, "m-local")["status"], "idle")

    async def test_no_group_silently_drops_a_node(self):
        """The three `[:4]` slices are gone. Above the cap the tree says how
        many it left out instead of looking complete."""
        for i in range(_MAX_CHILDREN + 3):
            await self._chat_on(f"c-{i:02d}", None)

        tree = await supervisor_map("admin")

        children = self._group(tree, "direct")["children"]
        self.assertEqual(len(children), _MAX_CHILDREN + 1)
        self.assertEqual(children[-1]["type"], "more")
        self.assertEqual(children[-1]["hidden_count"], 3)


def _machine_node(tree: dict, machine_id: str) -> dict:
    """Find a machine-type node by id, anywhere in the tree's children."""
    for child in tree.get("children", []):
        for node in child.get("children", []):
            if node.get("type") == "machine" and node.get("id") == machine_id:
                return node
    raise AssertionError(f"no machine node with id={machine_id!r} in {tree!r}")


class SupervisorMapEndpointTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/supervisor-map end to end -- session, route, and the tree
    the route actually returns to the browser.

    The plan this feature was built from (Task 2) called for exactly this and
    it was never written: every other test in this file, and in
    CapacityOnMachineNodesTests above, calls ``supervisor_map()`` directly,
    which never passes through the route's session handling or its
    ``_enrich_messages`` post-processing pass. That pass mutates every chat
    node's dict in place -- ``node["last_message"] = ...`` -- rather than
    rebuilding the tree, so it happens not to disturb a machine node's
    ``capacity_*`` keys, but nothing previously checked that this route
    delivers what the function produces rather than something
    ``_enrich_messages`` quietly reshaped or a stale session silently emptied.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.capacity_patch = patch.object(
            resource_guard, "capacity",
            return_value={"existing": 4, "total": 6, "cost_mb": 320,
                          "floor_mb": 400, "available_mb": 1040},
        )
        self.capacity_patch.start()
        self.addCleanup(self.capacity_patch.stop)

    def _login(self, who: str, password: str):
        client = _client()
        response = client.post("/login", json={"username": who, "password": password})
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    async def test_returns_200_with_the_expected_shape(self):
        password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(password))
        client = self._login("admin", password)

        response = client.get("/api/supervisor-map")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("center", body)
        self.assertIn("children", body)
        self.assertIsInstance(body["children"], list)

    async def test_an_unauthenticated_request_is_refused(self):
        response = _client().get("/api/supervisor-map")
        self.assertIn(response.status_code, (401, 303))

    async def test_a_direct_machines_capacity_survives_the_route(self):
        """The gap this class exists for: capacity_* reaching the client
        through _enrich_messages, not just through supervisor_map()."""
        password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(password))
        await db.ai_machine_create(
            "m-direct", "Anthropic API", "", 0, None, "claude-opus-5",
            None, None, "admin", provider="claude_code",
        )
        client = self._login("admin", password)

        body = client.get("/api/supervisor-map").json()

        node = _machine_node(body, "m-direct")
        self.assertEqual(node["capacity_existing"], 4)
        self.assertEqual(node["capacity_total"], 6)

    async def test_an_ssh_proxied_machine_has_no_capacity_over_the_route(self):
        password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(password))
        await db.ai_machine_create(
            "m-remote", "Kali3", "", 0, None, "claude-opus-5",
            None, None, "admin", provider="claude_code",
            transport_id="t-1",
        )
        client = self._login("admin", password)

        body = client.get("/api/supervisor-map").json()

        node = _machine_node(body, "m-remote")
        self.assertNotIn("capacity_existing", node)
        self.assertNotIn("capacity_total", node)
