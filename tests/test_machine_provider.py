"""Coverage for the Anthropic API provider on AI machines.

`ai_machines` used to model only a claude_proxy host, so there was no way to
select the API Claude Code natively talks to. Machines now carry a provider,
and an 'anthropic' one carries its endpoint and key to the CLI as
ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY.

Covers:
* Schema — provider column, default for pre-existing rows, migration path.
* Seeding — the Anthropic entry is materialised once per owner.
* handle_machine_create — provider validation, endpoint/port/model defaults.
* handle_machine_patch — provider validation, switching provider.
* API responses — api_key never echoed back for either provider.
* runner.get_backend — active-machine resolution and the empty cases.
* runner._build_env — ANTHROPIC_* set, OPENAI_* shim excluded.
* claude_proxy._backend_env — inherits os.environ, never truncates it.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import auth
import claude_proxy
import config
import db
import runner


def _make_admin_session():
    return {"user": "admin", "role": "admin"}


def _make_request(body=None):
    return SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/api/machines"),
        cookies={},
        headers={"accept": "*/*"},
        query_params={},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session=_make_admin_session()),
        json=AsyncMock(return_value=body or {}),
    )


async def _setup_db(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()


async def _teardown_db(tc):
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


class MachineSchemaTests(unittest.IsolatedAsyncioTestCase):
    """The provider column exists and defaults to the pre-existing behaviour."""

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_provider_column_exists(self):
        cur = await db.db_conn.execute("PRAGMA table_info(ai_machines)")
        columns = {row["name"] for row in await cur.fetchall()}
        self.assertIn("provider", columns)

    async def test_created_machine_defaults_to_proxy(self):
        await db.ai_machine_create(
            "m1", "Box", "10.0.0.9", 9000, None, "claude-sonnet-5", None, None, "admin"
        )
        machine = await db.ai_machine_get("m1", "admin")
        self.assertEqual(machine["provider"], "proxy")

    async def test_migration_adds_provider_to_legacy_rows(self):
        """A database written before the column must gain it, defaulting to proxy."""
        await db.close()
        path = config.DB_PATH
        # Rebuild the table without `provider`, as an older release wrote it.
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            DROP TABLE ai_machines;
            CREATE TABLE ai_machines (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 9000, api_key TEXT,
                model TEXT NOT NULL DEFAULT 'claude-sonnet-5', base_url TEXT,
                description TEXT, active INTEGER NOT NULL DEFAULT 0,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO ai_machines
                (id, name, host, port, model, active, owner_id, created_at, updated_at)
            VALUES ('old', 'Legacy', '10.0.0.1', 9000, 'claude-sonnet-5', 1, 'admin',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )
        legacy.commit()
        legacy.close()

        await db.init()
        machine = await db.ai_machine_get("old", "admin")
        self.assertEqual(machine["provider"], "proxy")


class MachineSeedTests(unittest.IsolatedAsyncioTestCase):
    """Every owner gets an Anthropic entry, created exactly once."""

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_seed_creates_anthropic_machine(self):
        machine_id = await db.ai_machine_seed_anthropic("admin")
        machine = await db.ai_machine_get(machine_id, "admin")
        self.assertEqual(machine["provider"], "anthropic")
        self.assertEqual(machine["host"], "api.anthropic.com")
        self.assertEqual(machine["port"], 443)
        self.assertEqual(machine["base_url"], "https://api.anthropic.com")

    async def test_seed_is_idempotent(self):
        first = await db.ai_machine_seed_anthropic("admin")
        second = await db.ai_machine_seed_anthropic("admin")
        self.assertEqual(first, second)
        machines = await db.ai_machines_list("admin")
        anthropic = [m for m in machines if m["provider"] == "anthropic"]
        self.assertEqual(len(anthropic), 1)

    async def test_seed_carries_no_api_key(self):
        """A blank key is what makes the CLI fall back to the host's login."""
        machine_id = await db.ai_machine_seed_anthropic("admin")
        await db.ai_machine_activate(machine_id, "admin")
        backend = await db.ai_machine_backend("admin")
        self.assertIsNone(backend["api_key"])

    async def test_listing_seeds_for_existing_accounts(self):
        request = _make_request()
        response = await app.handle_machines_list(request)
        import json as _json

        machines = _json.loads(response.body)["machines"]
        self.assertTrue(any(m["provider"] == "anthropic" for m in machines))

    async def test_listing_never_exposes_api_key(self):
        await db.ai_machine_create(
            "m1", "Box", "10.0.0.9", 9000, "sk-secret", "claude-sonnet-5",
            None, None, "admin",
        )
        request = _make_request()
        response = await app.handle_machines_list(request)
        self.assertNotIn(b"sk-secret", response.body)


class MachineCreateProviderTests(unittest.IsolatedAsyncioTestCase):
    """handle_machine_create understands the provider field."""

    async def asyncSetUp(self):
        await _setup_db(self)
        # Creation resolves the host for the SSRF blocklist; keep it offline.
        self._resolve = patch.object(app, "_validate_host", return_value="160.79.104.10")
        self._resolve.start()

    async def asyncTearDown(self):
        self._resolve.stop()
        await _teardown_db(self)

    async def test_anthropic_defaults_endpoint_and_port(self):
        request = _make_request({"name": "Anthropic", "provider": "anthropic"})
        await app.handle_machine_create(request)
        machines = await db.ai_machines_list("admin")
        created = next(m for m in machines if m["name"] == "Anthropic")
        self.assertEqual(created["provider"], "anthropic")
        self.assertEqual(created["base_url"], "https://api.anthropic.com")
        self.assertEqual(created["host"], "api.anthropic.com")
        self.assertEqual(created["port"], 443)

    async def test_anthropic_defaults_model(self):
        request = _make_request({"name": "Anthropic", "provider": "anthropic"})
        await app.handle_machine_create(request)
        machines = await db.ai_machines_list("admin")
        created = next(m for m in machines if m["name"] == "Anthropic")
        self.assertEqual(created["model"], config.ANTHROPIC_MODEL)

    async def test_anthropic_accepts_custom_base_url(self):
        request = _make_request(
            {
                "name": "Gateway",
                "provider": "anthropic",
                "base_url": "https://gateway.example.com/v1",
            }
        )
        await app.handle_machine_create(request)
        machines = await db.ai_machines_list("admin")
        created = next(m for m in machines if m["name"] == "Gateway")
        # The whole URL must survive -- scheme and path included.
        self.assertEqual(created["base_url"], "https://gateway.example.com/v1")
        self.assertEqual(created["host"], "gateway.example.com")

    async def test_proxy_provider_still_requires_host(self):
        request = _make_request({"name": "Box", "provider": "proxy"})
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_machine_create(request)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Host", ctx.exception.detail)

    async def test_unknown_provider_rejected(self):
        request = _make_request(
            {"name": "Nope", "provider": "openai", "host": "10.0.0.1"}
        )
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_machine_create(request)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Unknown provider")

    async def test_omitted_provider_defaults_to_proxy(self):
        request = _make_request({"name": "Box", "host": "10.0.0.9"})
        await app.handle_machine_create(request)
        machines = await db.ai_machines_list("admin")
        created = next(m for m in machines if m["name"] == "Box")
        self.assertEqual(created["provider"], "proxy")


class MachinePatchProviderTests(unittest.IsolatedAsyncioTestCase):
    """handle_machine_patch validates and applies the provider field."""

    async def asyncSetUp(self):
        await _setup_db(self)
        await db.ai_machine_create(
            "m1", "Box", "10.0.0.9", 9000, None, "claude-sonnet-5", None, None, "admin"
        )

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_switch_to_anthropic(self):
        request = _make_request(
            {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
        )
        await app.handle_machine_patch(request, "m1")
        machine = await db.ai_machine_get("m1", "admin")
        self.assertEqual(machine["provider"], "anthropic")
        self.assertEqual(machine["base_url"], "https://api.anthropic.com")

    async def test_unknown_provider_rejected(self):
        request = _make_request({"provider": "bedrock"})
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_machine_patch(request, "m1")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Unknown provider")

    async def test_non_string_provider_rejected(self):
        """Reaches the type guard rather than blowing up in .strip()."""
        request = _make_request({"provider": 7})
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_machine_patch(request, "m1")
        self.assertEqual(ctx.exception.status_code, 400)


class BaseUrlNormaliseTests(unittest.TestCase):
    """Legacy rows hold a bare host, from the old truncating validator."""

    def test_bare_host_gains_https(self):
        self.assertEqual(
            runner.normalise_base_url("api.anthropic.com"),
            "https://api.anthropic.com",
        )

    def test_absolute_origin_untouched(self):
        self.assertEqual(
            runner.normalise_base_url("https://gateway.example.com"),
            "https://gateway.example.com",
        )

    def test_http_scheme_and_port_preserved(self):
        self.assertEqual(
            runner.normalise_base_url("http://10.0.0.5:9000/v1"),
            "http://10.0.0.5:9000",
        )

    def test_empty_is_none(self):
        self.assertIsNone(runner.normalise_base_url(""))
        self.assertIsNone(runner.normalise_base_url(None))
        self.assertIsNone(runner.normalise_base_url("   "))

    def test_trailing_v1_stripped(self):
        """The CLI appends /v1/messages, so a stored /v1 would double it up."""
        self.assertEqual(
            runner.normalise_base_url("https://gateway.example.com/v1"),
            "https://gateway.example.com",
        )

    def test_trailing_slash_stripped(self):
        self.assertEqual(
            runner.normalise_base_url("https://api.anthropic.com/"),
            "https://api.anthropic.com",
        )

    def test_v1_stripped_from_bare_host(self):
        self.assertEqual(
            runner.normalise_base_url("gateway.example.com/v1/"),
            "https://gateway.example.com",
        )

    def test_inner_path_preserved(self):
        """Only a trailing /v1 is the CLI's to add -- a real path stays."""
        self.assertEqual(
            runner.normalise_base_url("https://example.com/llm"),
            "https://example.com/llm",
        )


class AnthropicProbeTests(unittest.IsolatedAsyncioTestCase):
    """Test reports what the endpoint actually says, not just that it accepts TCP."""

    async def asyncSetUp(self):
        await _setup_db(self)
        await db.ai_machine_create(
            "m1", "Anthropic", "api.anthropic.com", 443, "sk-test",
            "claude-opus-5", "https://api.anthropic.com", None, "admin",
            provider="anthropic",
        )
        self._resolve = patch.object(app, "_resolve_host", return_value="160.79.104.10")
        self._resolve.start()

    async def asyncTearDown(self):
        self._resolve.stop()
        await _teardown_db(self)

    async def _run_test(self, probe):
        request = _make_request()
        with patch.object(app, "_probe_anthropic", probe):
            return await app.handle_machine_test(request, "m1")

    async def test_success(self):
        import json as _json

        response = await self._run_test(lambda url, key: (200, b''))
        body = _json.loads(response.body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "reachable")

    async def test_probes_the_models_endpoint_with_the_key(self):
        seen = {}

        def _probe(url, key):
            seen["url"] = url
            seen["key"] = key
            return 200, b""

        await self._run_test(_probe)
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/models")
        self.assertEqual(seen["key"], "sk-test")

    async def test_rejected_key_is_not_reported_as_reachable(self):
        """A bare TCP connect called this 'reachable' while every turn failed."""
        import json as _json

        response = await self._run_test(lambda url, key: (401, b''))
        body = _json.loads(response.body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "auth_failed")
        self.assertEqual(response.status_code, 502)

    async def test_unexpected_status_surfaces_the_code(self):
        import json as _json

        response = await self._run_test(lambda url, key: (404, b''))
        body = _json.loads(response.body)
        self.assertEqual(body["status"], "error")
        self.assertIn("404", body["error"])

    async def test_connection_failure_is_unreachable(self):
        import json as _json

        def _boom(url, key):
            raise OSError("no route to host")

        response = await self._run_test(_boom)
        body = _json.loads(response.body)
        self.assertEqual(body["status"], "unreachable")

    async def test_private_endpoint_blocked(self):
        """The probe goes out from the server, so it keeps the SSRF blocklist."""
        self._resolve.stop()
        try:
            with patch.object(
                app,
                "_resolve_host",
                side_effect=HTTPException(status_code=403, detail="Internal hosts"),
            ), self.assertRaises(HTTPException) as ctx:
                await app.handle_machine_test(_make_request(), "m1")
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            self._resolve.start()

    async def test_key_never_appears_in_the_response(self):
        response = await self._run_test(lambda url, key: (401, b''))
        self.assertNotIn(b"sk-test", response.body)


class GetBackendTests(unittest.IsolatedAsyncioTestCase):
    """runner.get_backend resolves the chat owner's active machine."""

    async def asyncSetUp(self):
        await _setup_db(self)
        await db.chat_create("c1", "Chat", None, "/tmp", "admin")

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_no_active_machine_is_empty(self):
        self.assertEqual(await runner.get_backend("c1"), {})

    async def test_active_proxy_machine_is_empty(self):
        """A proxy machine must not rewrite the CLI's provider environment."""
        await db.ai_machine_create(
            "m1", "Box", "10.0.0.9", 9000, "k", "claude-sonnet-5", None, None, "admin"
        )
        await db.ai_machine_activate("m1", "admin")
        self.assertEqual(await runner.get_backend("c1"), {})

    async def test_active_anthropic_machine(self):
        await db.ai_machine_create(
            "m1", "Anthropic", "api.anthropic.com", 443, "sk-test",
            "claude-opus-5", "https://api.anthropic.com", None, "admin",
            provider="anthropic",
        )
        await db.ai_machine_activate("m1", "admin")
        backend = await runner.get_backend("c1")
        self.assertEqual(backend["provider"], "anthropic")
        self.assertEqual(backend["base_url"], "https://api.anthropic.com")
        self.assertEqual(backend["api_key"], "sk-test")

    async def test_blank_key_is_omitted_not_empty(self):
        """An empty ANTHROPIC_API_KEY would break the host-login fallback."""
        await db.ai_machine_create(
            "m1", "Anthropic", "api.anthropic.com", 443, None,
            "claude-opus-5", "https://api.anthropic.com", None, "admin",
            provider="anthropic",
        )
        await db.ai_machine_activate("m1", "admin")
        backend = await runner.get_backend("c1")
        self.assertNotIn("api_key", backend)

    async def test_unknown_chat_is_empty(self):
        self.assertEqual(await runner.get_backend("does-not-exist"), {})


class BuildEnvTests(unittest.TestCase):
    """Direct subprocess mode points the CLI at the right provider."""

    def test_anthropic_backend_sets_anthropic_vars(self):
        env = runner._build_env(
            {
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-test",
            }
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-test")

    def test_anthropic_backend_excludes_openai_shim(self):
        """A leftover OPENAI_BASE_URL would pull the turn back to the shim."""
        env = runner._build_env(
            {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
        )
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_MODEL_NAME", env)

    def test_anthropic_backend_without_key_omits_it(self):
        env = runner._build_env(
            {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
        )
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_host_login_fallback_clears_simple_mode(self):
        """CLAUDE_CODE_SIMPLE makes the CLI ignore OAuth and the keychain.

        Verified against the installed CLI: with it set, `claude auth status`
        reports loggedIn:false / authMethod:none even for a logged-in host. So
        a keyless Anthropic machine has to run without it or it has no
        credentials at all.
        """
        env = runner._build_env(
            {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
        )
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)

    def test_explicit_key_keeps_simple_mode(self):
        """With a key there is nothing to read from the keychain."""
        env = runner._build_env(
            {
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-test",
            }
        )
        self.assertEqual(env["CLAUDE_CODE_SIMPLE"], "1")

    def test_legacy_host_only_base_url_coerced(self):
        env = runner._build_env(
            {"provider": "anthropic", "base_url": "api.anthropic.com"}
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")

    def test_no_backend_keeps_openai_shim(self):
        env = runner._build_env(None)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(env["OPENAI_MODEL_NAME"], config.MODEL_NAME)


class ProxyBackendEnvTests(unittest.TestCase):
    """claude_proxy builds the child environment for the spawned CLI."""

    def test_inherits_parent_environment(self):
        """A bare dict would strip PATH/HOME and `claude` would not start."""
        env = claude_proxy._backend_env(None)
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))

    def test_anthropic_backend_applied_over_inherited_env(self):
        env = claude_proxy._backend_env(
            {
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-test",
            }
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-test")
        self.assertIn("PATH", env)

    def test_missing_key_drops_inherited_key(self):
        """Otherwise a stale key in the proxy's shell silently wins."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-stale"}):
            env = claude_proxy._backend_env(
                {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
            )
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_missing_key_drops_inherited_simple_mode(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_SIMPLE": "1"}):
            env = claude_proxy._backend_env(
                {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
            )
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)

    def test_non_anthropic_backend_left_alone(self):
        env = claude_proxy._backend_env({"provider": "proxy"})
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_malformed_backend_ignored(self):
        """The payload is client-supplied, so a non-dict must not raise."""
        for value in ("anthropic", 42, [], None):
            env = claude_proxy._backend_env(value)
            self.assertIn("PATH", env)


class TurnPayloadTests(unittest.IsolatedAsyncioTestCase):
    """The backend block reaches the proxy in the turn frame."""

    async def asyncSetUp(self):
        await _setup_db(self)
        await db.chat_create("c1", "Chat", None, "/tmp", "admin")
        await db.ai_machine_create(
            "m1", "Anthropic", "api.anthropic.com", 443, "sk-test",
            "claude-opus-5", "https://api.anthropic.com", None, "admin",
            provider="anthropic",
        )
        await db.ai_machine_activate("m1", "admin")

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_backend_included_in_turn_frame(self):
        import json as _json

        written: list[bytes] = []

        class _Writer:
            def write(self, data):
                written.append(data)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class _Reader:
            async def readuntil(self, sep):
                return b'{"type":"ack"}\n'

        async def _lines(_reader):
            yield b'{"type":"done"}'

        async def _open(*_a, **_kw):
            return _Reader(), _Writer()

        with patch.object(runner.asyncio, "open_connection", _open), \
                patch.object(runner, "_read_lines", _lines):
            await runner._execute_proxy("hi", None, "/tmp", "c1", "claude-opus-5")

        frames = [_json.loads(b.decode()) for b in written]
        turn = next(f for f in frames if f.get("type") == "turn")
        self.assertEqual(turn["backend"]["provider"], "anthropic")
        self.assertEqual(turn["backend"]["base_url"], "https://api.anthropic.com")
        self.assertEqual(turn["backend"]["api_key"], "sk-test")


if __name__ == "__main__":
    unittest.main()
