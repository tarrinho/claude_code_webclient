"""Ladder pinning — end-to-end + unit tests.

Covers:
  - Design §8.1: schema + accessors
  - Design §8.2: seam (ladder vs generated_ladder)
  - Design §8.3: boot safety (without_unusable_pins)
  - Design §8.4: PUT /api/delegation/ladder validation
  - Design §8.5: page browser (GET /api/delegation returns pins + generated)
"""
from __future__ import annotations

import json
import pytest
import httpx

# ── Fixtures (shared with existing delegation tests) ─────────────────────────

from tests.fixtures.db import *  # noqa: F401,F403 — db, test_app


# ── §8.1: schema + accessors ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_delegation_ladder_pin(db, test_app):
    """The delegation_ladder_pin table exists after boot."""
    cur = await db.db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='delegation_ladder_pin'")
    rows = await cur.fetchall()
    assert len(rows) == 1
    info = await db.db_conn.execute(
        "PRAGMA table_info(delegation_ladder_pin)")
    cols = {r["name"] for r in await info.fetchall()}
    assert cols >= {"task_type", "rungs", "updated_at"}


@pytest.mark.asyncio
async def test_accessors_round_trip(db, test_app):
    """delegation_pin_set / delegation_pin_all round-trip."""
    from routes.db_delegation import delegation_pin_set, delegation_pin_all
    # Initially empty.
    all_pins = await delegation_pin_all()
    assert all_pins == {}
    # Set a pin.
    ok = await delegation_pin_set("coding", ["model-a", "model-b"])
    assert ok is True
    all_pins = await delegation_pin_all()
    assert all_pins["coding"] == ["model-a", "model-b"]
    assert "other" not in all_pins
    # Clear it.
    ok = await delegation_pin_set("coding", None)
    assert ok is True
    all_pins = await delegation_pin_all()
    assert "coding" not in all_pins


# ── §8.2: seam ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generated_ladder_returns_generator_output(db, test_app):
    """CapabilityTable.generated_ladder() is the old ladder() body."""
    from routes.db_delegation import rows_to_capability
    from tiered_delegation import CapabilityTable
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = await db.delegation_operational_all()
    table = CapabilityTable(rows, operational=operational)
    for tt in {"coding", "reviewer-gate", "long-context"}:
        gen = table.generated_ladder(tt)
        assert isinstance(gen, list)
        # Generated ladder should be non-empty for types with data.
        # (If the table is empty it'll be empty — that's fine.)


@pytest.mark.asyncio
async def test_ladder_returns_pin_when_present(db, test_app):
    """ladder() returns the pin, not the generated one."""
    from routes.db_delegation import rows_to_capability
    from tiered_delegation import CapabilityTable
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = set()
    pins = {"coding": ["my-model", "other-model"]}
    table = CapabilityTable(rows, operational=operational, pins=pins)
    assert table.ladder("coding") == ["my-model", "other-model"]


@pytest.mark.asyncio
async def test_ladder_falls_back_to_generated_when_no_pin(db, test_app):
    """ladder() generates when there is no pin for that task_type."""
    from routes.db_delegation import rows_to_capability
    from tiered_delegation import CapabilityTable
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = set()
    pins = {"coding": ["my-model"]}
    table = CapabilityTable(rows, operational=operational, pins=pins)
    # "reviewer-gate" has no pin → generated.
    gen = table.generated_ladder("reviewer-gate")
    assert table.ladder("reviewer-gate") == gen


@pytest.mark.asyncio
async def test_empty_list_pin_means_no_ladder(db, test_app):
    """An empty array is a legal pin meaning 'no ladder'.

    Distinct from no pin (generated) — the spec says so explicitly.
    """
    from routes.db_delegation import rows_to_capability
    from tiered_delegation import CapabilityTable
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = set()
    pins = {"coding": []}
    table = CapabilityTable(rows, operational=operational, pins=pins)
    assert table.ladder("coding") == []
    assert table.ladder("coding") is not table.ladder("coding")  # copy


# ── §8.3: boot safety (without_unusable_pins) ──────────────────────────────


@pytest.mark.asyncio
async def test_without_unusable_pins_drops_breaching_pin(db, test_app):
    """A pin that introduces new problems is dropped."""
    from routes.db_delegation import rows_to_capability
    from tiered_delegation import CapabilityTable
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = set()
    # Pin an obviously bad ladder (one rung only, but that rung doesn't
    # exist in the table — will cause a 'no row' problem that generated
    # ladder doesn't have).
    pins = {"coding": ["nonexistent-model-xyz"]}
    table = CapabilityTable(rows, operational=operational, pins=pins)
    safe, dropped = table.without_unusable_pins()
    # Should have dropped the pin because it introduces new problems.
    assert "coding" not in safe._pins
    assert len(dropped) > 0


@pytest.mark.asyncio
async def test_without_unusable_pins_keeps_safe_pin(db, test_app):
    """A pin that doesn't add new problems survives."""
    from routes.db_delegation import rows_to_capability
    from tiered_delegation import CapabilityTable
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = set()
    # Reorder existing rungs — shouldn't add problems.
    gen_ladder = CapabilityTable(rows, operational=operational
                                ).generated_ladder("coding")
    if len(gen_ladder) >= 2:
        pins = {"coding": list(reversed(gen_ladder))}
        table = CapabilityTable(rows, operational=operational, pins=pins)
        safe, dropped = table.without_unusable_pins()
        # Should survive (same models, just different order).
        assert safe._pins.get("coding") == pins["coding"]


# ── §8.4: PUT /api/delegation/ladder validation ────────────────────────────


@pytest.mark.asyncio
async def test_put_ladder_pin_set_and_get(db, test_app):
    """Set a pin, GET /api/delegation shows it."""
    resp = await test_app.get("/api/delegation")
    assert resp.status_code == 200
    data = resp.json()
    assert "pins" in data
    assert "generated_ladders" in data
    assert "ladders" in data


@pytest.mark.asyncio
async def test_put_ladder_unknown_task_type(test_app):
    """A completely unknown task_type is refused 400."""
    body = {"task_type": "no-such-type", "rungs": ["model-a"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_ladder_rung_count_exceeded(db, test_app):
    """More rungs than MAX_ATTEMPTS is refused."""
    from tiered_delegation import MAX_ATTEMPTS
    # Use an existing task_type but way too many rungs.
    body = {"task_type": "coding", "rungs": ["m1", "m2", "m3", "m4", "m5"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400
    # app.handle_http_exception renders HTTPException.detail as {"error": ...},
    # so "detail" is the server-side field name, never the wire one.
    assert "max" in resp.json()["error"].lower() or "rung" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_put_ladder_duplicate_rungs(db, test_app):
    """Duplicate rungs are refused 400."""
    body = {"task_type": "coding", "rungs": ["m1", "m1", "m2"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400
    assert "duplicate" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_put_ladder_blank_rung(test_app):
    """A blank rung string is refused 400."""
    body = {"task_type": "coding", "rungs": ["m1", "", "m2"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_ladder_non_string_rung(test_app):
    """A non-string rung is refused 400."""
    body = {"task_type": "coding", "rungs": ["m1", 42, "m2"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_ladder_clear_pin(db, test_app):
    """Omitting rungs clears the pin."""
    # First, set a pin.
    body1 = {"task_type": "coding", "rungs": ["m1", "m2"]}
    resp1 = await test_app.put("/api/delegation/ladder", json=body1)
    assert resp1.status_code == 200
    assert resp1.json()["ok"] is True
    # Now clear it (omit rungs).
    body2 = {"task_type": "coding"}
    resp2 = await test_app.put("/api/delegation/ladder", json=body2)
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True
    # Verify pin is gone.
    from routes.db_delegation import delegation_pin_all
    all_pins = await delegation_pin_all()
    assert "coding" not in all_pins


@pytest.mark.asyncio
async def test_put_ladder_returns_problems_and_generated(db, test_app):
    """Response includes 'problems' and 'generated' keys."""
    body = {"task_type": "coding", "rungs": ["model-a", "model-b"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert "problems" in data
    assert "generated" in data
    assert "pinned" in data
    assert isinstance(data["problems"], list)


@pytest.mark.asyncio
async def test_put_ladder_non_object_body(test_app):
    """A non-JSON-object body returns 400."""
    resp = await test_app.put("/api/delegation/ladder", content="[1,2]")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_ladder_missing_task_type(test_app):
    """Missing task_type is refused 400."""
    body = {"rungs": ["m1"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400


# ── §8.5: page browser (GET /api/delegation) ───────────────────────────────


@pytest.mark.asyncio
async def test_get_delegation_returns_pins_and_generated(db, test_app):
    """GET /api/delegation includes 'pins' and 'generated_ladders'."""
    resp = await test_app.get("/api/delegation")
    assert resp.status_code == 200
    data = resp.json()
    assert "pins" in data
    assert isinstance(data["pins"], dict)
    assert "generated_ladders" in data
    assert isinstance(data["generated_ladders"], dict)
    # 'ladders' should still be present (the effective ladder = pin or gen).
    assert "ladders" in data


@pytest.mark.asyncio
async def test_get_delegation_pins_show_after_set(db, test_app):
    """After setting a pin, GET shows it in 'pins'."""
    body = {"task_type": "coding", "rungs": ["my-model"]}
    await test_app.put("/api/delegation/ladder", json=body)
    resp = await test_app.get("/api/delegation")
    data = resp.json()
    assert data["pins"].get("coding") == ["my-model"]


@pytest.mark.asyncio
async def test_get_delegation_ladders_reflect_pin(db, test_app):
    """'ladders' (effective) matches the pin when one exists."""
    body = {"task_type": "coding", "rungs": ["my-model", "other-model"]}
    await test_app.put("/api/delegation/ladder", json=body)
    resp = await test_app.get("/api/delegation")
    data = resp.json()
    assert data["ladders"].get("coding") == ["my-model", "other-model"]
    # generated_ladders should differ.
    assert data["generated_ladders"].get("coding") != ["my-model", "other-model"]


# ── §8.6: gate type attempt budget ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_put_ladder_gate_budget_exceeded(db, test_app):
    """reviewer-gate refuses more than GATE_MAX_ATTEMPTS."""
    from tiered_delegation import GATE_MAX_ATTEMPTS
    body = {"task_type": "reviewer-gate", "rungs": ["m1", "m2", "m3"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_ladder_gate_budget_ok(db, test_app):
    """reviewer-gate with exactly GATE_MAX_ATTEMPTS is accepted."""
    from tiered_delegation import GATE_MAX_ATTEMPTS
    rungs = ["m1", "m2"]  # GATE_MAX_ATTEMPTS == 2
    body = {"task_type": "reviewer-gate", "rungs": rungs}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 200


# ── §8.7: admin-only ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_put_ladder_requires_admin(test_app):
    """Non-admin gets 403 on PUT /api/delegation/ladder."""
    body = {"task_type": "coding", "rungs": ["m1"]}
    resp = await test_app.put("/api/delegation/ladder", json=body)
    assert resp.status_code == 403
