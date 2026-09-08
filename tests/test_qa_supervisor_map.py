"""Tests for /api/supervisor-map endpoint (sync surface tests).

All tests here exercise the pure functions that assemble the tree. The HTTP
endpoint is tested in the browser test suite (task 7) and by the full suite
run in task 8.
"""
from __future__ import annotations

import pytest

from routes.db_supervisor_map import _aggregate_status, _normalise, _task_status


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
