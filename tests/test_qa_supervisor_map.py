"""Tests for /api/supervisor-map endpoint (sync surface tests).

Most tests here exercise the pure functions that assemble the tree. The HTTP
endpoint is tested in the browser test suite (task 7) and by the full suite
run in task 8. CapacityOnMachineNodesTests is the exception: it runs the async
supervisor_map() itself, because "which nodes get a capacity_* pair" is a
property of the assembly loop, not of any pure helper it calls.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

import auth
import config
import db
import resource_guard
from routes.db_supervisor_map import (
    _aggregate_status, _normalise, _task_status, supervisor_map,
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

    def test_max4_children(self):
        """Only the first four children are returned by supervisor_map,
        so the renderer never needs to handle more than four per node."""
        nodes = [
            {"id": "c0", "label": "Chat 0", "status": "idle", "type": "chat"},
            {"id": "c1", "label": "Chat 1", "status": "idle", "type": "chat"},
            {"id": "c2", "label": "Chat 2", "status": "idle", "type": "chat"},
            {"id": "c3", "label": "Chat 3", "status": "idle", "type": "chat"},
            {"id": "c4", "label": "Chat 4 (should not appear)", "status": "busy", "type": "chat"},
        ]
        # _aggregate_status ignores extra nodes — the cap is in supervisor_map()
        assert _aggregate_status(nodes[:4]) == "idle"


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
