"""QA: disabling a backend is refused while anything depends on it.

The refusal names the conversations. The alternative -- letting pinned chats
fall back to the default -- was considered and rejected in the spec: this
deployment has chats pinned across backends serving disjoint model ids, so a
fallback would turn one click into several conversations answering 429 "No
deployments available for selected model", which reads as capacity and is
really routing (CLAUDE.md 0.1).

Design: docs/superpowers/specs/2026-09-08-default-and-enabled-backends-design.md
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import machines as mr


class _Request:
    def __init__(self, body: dict, user: str = "admin"):
        self._body = body
        self.state = type("S", (), {"session": {"user": user}})()

    async def json(self):
        return self._body


_MACHINE = {"id": "m-1", "name": "Gateway", "provider": "claude_code",
            "host": "gw.example.com", "port": 443, "active": 0, "enabled": 1}

_NO_PINS = {"total": 0, "titles": [], "ids": []}


def _body(response) -> dict:
    return json.loads(bytes(response.body))


def _patched(machine, pins, setter):
    """Patch the three db calls the enabled branch makes."""
    return (
        patch.object(mr.db, "ai_machine_get", AsyncMock(return_value=machine)),
        patch.object(mr.db, "chats_pinned_to_machine", AsyncMock(return_value=pins)),
        patch.object(mr.db, "ai_machine_set_enabled", setter),
    )


class DisableIsAllowedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_plain_backend_can_be_disabled(self):
        setter = AsyncMock(return_value=True)
        a, b, c = _patched(dict(_MACHINE), _NO_PINS, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        self.assertEqual(response.status_code, 200)
        setter.assert_awaited_once_with("m-1", "admin", False)

    async def test_enabling_is_never_refused(self):
        """A disabled, pinned, formerly-default backend can always come back:
        a backend returning to service breaks nothing that depends on it."""
        setter = AsyncMock(return_value=True)
        machine = {**_MACHINE, "enabled": 0, "active": 1}
        pins = {"total": 8, "titles": ["a"] * 8, "ids": ["x"] * 8}
        a, b, c = _patched(machine, pins, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": True}), "m-1")
        self.assertEqual(response.status_code, 200)
        setter.assert_awaited_once_with("m-1", "admin", True)


class DisableIsRefusedTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_default_cannot_be_disabled(self):
        setter = AsyncMock()
        a, b, c = _patched({**_MACHINE, "active": 1}, _NO_PINS, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(_body(response)["is_default"])
        setter.assert_not_awaited()

    async def test_pinned_conversations_are_named_not_counted(self):
        pins = {"total": 3, "titles": ["cweb2", "voice test", "kali3 test"],
                "ids": ["c1", "c2", "c3"]}
        setter = AsyncMock()
        a, b, c = _patched(dict(_MACHINE), pins, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        self.assertEqual(response.status_code, 409)
        body = _body(response)
        self.assertEqual(body["pinned_total"], 3)
        self.assertEqual([c["title"] for c in body["pinned_chats"]],
                         ["cweb2", "voice test", "kali3 test"])
        self.assertIn("cweb2", body["error"])
        setter.assert_not_awaited()

    async def test_the_message_reports_the_full_total_not_the_capped_list(self):
        pins = {"total": 40, "titles": [f"chat {i}" for i in range(8)],
                "ids": [f"c{i}" for i in range(8)]}
        a, b, c = _patched(dict(_MACHINE), pins, AsyncMock())
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        body = _body(response)
        self.assertEqual(body["pinned_total"], 40)
        self.assertEqual(len(body["pinned_chats"]), 8)
        self.assertIn("40", body["error"])
        self.assertIn("32 more", body["error"])

    async def test_an_unknown_machine_is_404_not_409(self):
        a, b, c = _patched(None, _NO_PINS, AsyncMock())
        with a, b, c, self.assertRaises(HTTPException) as caught:
            await mr.handle_machine_patch(_Request({"enabled": False}), "m-1")
        self.assertEqual(caught.exception.status_code, 404)


class FieldValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_must_be_a_boolean(self):
        a, b, c = _patched(dict(_MACHINE), _NO_PINS, AsyncMock())
        with a, b, c, self.assertRaises(HTTPException) as caught:
            await mr.handle_machine_patch(
                _Request({"enabled": "yes"}), "m-1")
        self.assertEqual(caught.exception.status_code, 400)

    async def test_enabled_is_an_accepted_field(self):
        """It must be in _MACHINE_ALLOWED_FIELDS or the handler 400s with
        'No valid fields to update' before reaching any of the logic above."""
        self.assertIn("enabled", mr._MACHINE_ALLOWED_FIELDS)


if __name__ == "__main__":
    unittest.main()
