"""Tests for skill discovery and the GET /api/skills response.

Covers: SKILL.md description parsing (frontmatter, wrapped values, quoting),
one-line summary condensation, user-skill discovery, plugin discovery driven by
installed_plugins.json, namespaced plugin names, and the response grouping.

All discovery roots are patched to temporary directories, so no test reads or
writes the real ~/.claude tree.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import config
import runner


def _make_request(query=None):
    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/skills"),
        cookies={},
        headers={"accept": "*/*"},
        query_params=dict(query or {}),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        json=AsyncMock(return_value={}),
    )
    return request


def _write_skill(root: Path, name: str, description: str | None = "A skill.") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\n"
    if description is not None:
        body += f"description: {description}\n"
    body += "---\n\n# Heading\n\nSome body text.\n"
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


# ── Description parsing ────────────────────────────────────────────────────────


class SkillDescriptionTests(unittest.TestCase):
    def test_reads_frontmatter_description(self):
        text = "---\nname: demo\ndescription: Does a thing.\n---\n\nBody.\n"
        self.assertEqual(app._skill_description(text), "Does a thing.")

    def test_joins_wrapped_continuation_lines(self):
        text = (
            "---\nname: demo\n"
            "description: First part of the value\n"
            "  and the wrapped remainder.\n"
            "metadata: other\n---\n"
        )
        self.assertEqual(
            app._skill_description(text),
            "First part of the value and the wrapped remainder.",
        )

    def test_stops_at_next_frontmatter_key(self):
        text = "---\ndescription: Only this.\nallowed-tools: Read\n---\n"
        self.assertEqual(app._skill_description(text), "Only this.")

    def test_strips_surrounding_double_quotes(self):
        text = '---\ndescription: "Quoted value."\n---\n'
        self.assertEqual(app._skill_description(text), "Quoted value.")

    def test_strips_surrounding_single_quotes(self):
        text = "---\ndescription: 'Quoted value.'\n---\n"
        self.assertEqual(app._skill_description(text), "Quoted value.")

    def test_falls_back_to_bare_line_without_frontmatter(self):
        text = "# Title\n\ndescription: Plain line.\n"
        self.assertEqual(app._skill_description(text), "Plain line.")

    def test_returns_empty_when_absent(self):
        self.assertEqual(app._skill_description("# Title\n\nJust prose.\n"), "")

    def test_caps_length(self):
        text = "---\ndescription: %s\n---\n" % ("x" * 900)
        self.assertEqual(len(app._skill_description(text)), 500)


# ── Summary condensation ───────────────────────────────────────────────────────


class SkillSummaryTests(unittest.TestCase):
    def test_prefers_first_sentence(self):
        text = "Creates a proxy bundle for the platform. Also does other things later."
        self.assertEqual(
            app._skill_summary(text), "Creates a proxy bundle for the platform."
        )

    def test_truncates_long_text_on_word_boundary(self):
        summary = app._skill_summary("word " * 60)
        self.assertLessEqual(len(summary), app._SKILL_SUMMARY_MAX + 1)
        self.assertTrue(summary.endswith("…"))
        self.assertNotIn("  ", summary)

    def test_strips_markdown_emphasis_and_code_ticks(self):
        text = "**MANDATORY** — call the `do_thing` tool with __care__ before starting."
        self.assertEqual(
            app._skill_summary(text),
            "MANDATORY — call the do_thing tool with care before starting.",
        )

    def test_preserves_snake_case_identifiers(self):
        self.assertIn("get_design_context", app._skill_summary("Use `get_design_context` now."))

    def test_collapses_whitespace(self):
        self.assertEqual(app._skill_summary("A   b\n\tc."), "A b c.")

    def test_empty_description_yields_empty_summary(self):
        self.assertEqual(app._skill_summary(""), "")
        self.assertEqual(app._skill_summary("  \n "), "")


# ── User skill discovery ───────────────────────────────────────────────────────


class UserSkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.root.mkdir()
        self.patch = patch.object(app, "_USER_SKILLS_ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_finds_skills_sorted_by_name(self):
        _write_skill(self.root, "zebra")
        _write_skill(self.root, "alpha")
        names = [s["name"] for s in app._discover_user_skills()]
        self.assertEqual(names, ["alpha", "zebra"])

    def test_entry_shape(self):
        _write_skill(self.root, "demo", "Does a thing well.")
        skill = app._discover_user_skills()[0]
        self.assertEqual(skill["name"], "demo")
        self.assertEqual(skill["description"], "Does a thing well.")
        self.assertEqual(skill["summary"], "Does a thing well.")
        self.assertEqual(skill["source"], "user")
        self.assertEqual(skill["source_label"], "Your skills")
        self.assertTrue(skill["installed"])

    def test_skips_directory_without_skill_md(self):
        (self.root / "empty").mkdir()
        self.assertEqual(app._discover_user_skills(), [])

    def test_skips_loose_files(self):
        (self.root / "README.md").write_text("hi", encoding="utf-8")
        self.assertEqual(app._discover_user_skills(), [])

    def test_skips_invalid_directory_names(self):
        _write_skill(self.root, ".hidden")
        _write_skill(self.root, "ok-name")
        names = [s["name"] for s in app._discover_user_skills()]
        self.assertEqual(names, ["ok-name"])

    def test_missing_root_is_not_an_error(self):
        with patch.object(app, "_USER_SKILLS_ROOT", self.root / "nope"):
            self.assertEqual(app._discover_user_skills(), [])


# ── Plugin skill discovery ─────────────────────────────────────────────────────


class PluginSkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugins = Path(self.tmp.name) / "plugins"
        self.plugins.mkdir()
        self.patch = patch.object(app, "_PLUGINS_ROOT", self.plugins)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _manifest(self, plugins):
        (self.plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
        )

    def _install(self, plugin, version="1.0.0"):
        path = self.plugins / "cache" / plugin / version
        (path / "skills").mkdir(parents=True)
        return path

    def test_namespaces_plugin_skill_names(self):
        install = self._install("figma")
        _write_skill(install / "skills", "figma-use", "Use Figma.")
        self._manifest({"figma@official": [{"installPath": str(install)}]})
        skills = app._discover_plugin_skills()
        self.assertEqual([s["name"] for s in skills], ["figma:figma-use"])
        self.assertEqual(skills[0]["source"], "plugin:figma")
        self.assertEqual(skills[0]["source_label"], "figma")

    def test_reads_every_skill_of_a_plugin(self):
        install = self._install("superpowers")
        for name in ("brainstorming", "writing-plans", "tdd"):
            _write_skill(install / "skills", name)
        self._manifest({"superpowers@sp": [{"installPath": str(install)}]})
        self.assertEqual(len(app._discover_plugin_skills()), 3)

    def test_ignores_plugin_without_skills_directory(self):
        path = self.plugins / "cache" / "code-review" / "1.0.0"
        path.mkdir(parents=True)
        self._manifest({"code-review@official": [{"installPath": str(path)}]})
        self.assertEqual(app._discover_plugin_skills(), [])

    def test_ignores_install_path_outside_plugins_root(self):
        outside = Path(self.tmp.name) / "elsewhere"
        (outside / "skills").mkdir(parents=True)
        _write_skill(outside / "skills", "sneaky")
        self._manifest({"evil@x": [{"installPath": str(outside)}]})
        self.assertEqual(app._discover_plugin_skills(), [])

    def test_deduplicates_repeated_installs(self):
        install = self._install("figma")
        _write_skill(install / "skills", "figma-use")
        self._manifest(
            {
                "figma@official": [
                    {"installPath": str(install), "scope": "user"},
                    {"installPath": str(install), "scope": "project"},
                ]
            }
        )
        self.assertEqual(len(app._discover_plugin_skills()), 1)

    def test_missing_manifest_returns_empty(self):
        self.assertEqual(app._discover_plugin_skills(), [])

    def test_malformed_manifest_returns_empty(self):
        (self.plugins / "installed_plugins.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(app._discover_plugin_skills(), [])

    def test_manifest_without_plugins_key_returns_empty(self):
        (self.plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2}), encoding="utf-8"
        )
        self.assertEqual(app._discover_plugin_skills(), [])

    def test_skips_non_dict_install_entries(self):
        self._manifest({"figma@official": ["not-a-dict", {"installPath": ""}]})
        self.assertEqual(app._discover_plugin_skills(), [])


# ── Response shape ─────────────────────────────────────────────────────────────


class SkillsResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.user_root = base / "skills"
        self.user_root.mkdir()
        self.plugins = base / "plugins"
        self.plugins.mkdir()
        self.patches = [
            patch.object(app, "_USER_SKILLS_ROOT", self.user_root),
            patch.object(app, "_PLUGINS_ROOT", self.plugins),
        ]
        for p in self.patches:
            p.start()

        _write_skill(self.user_root, "pentesting", "Runs an authorized assessment.")
        _write_skill(self.user_root, "writing-plans", "Writes a plan.")
        install = self.plugins / "cache" / "figma" / "1.0.0"
        (install / "skills").mkdir(parents=True)
        _write_skill(install / "skills", "figma-use", "Drives the Figma plugin API.")
        (self.plugins / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"figma@official": [{"installPath": str(install)}]}}),
            encoding="utf-8",
        )

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def _get(self, query=None, active=()):
        with patch.object(runner, "active_skills", return_value=list(active)):
            response = await app.handle_skills_get(_make_request(query))
        return json.loads(response.body)

    async def test_response_schema(self):
        data = await self._get()
        for key in ("skills", "sources", "total", "active_count", "session_id"):
            self.assertIn(key, data)

    async def test_lists_user_and_plugin_skills(self):
        data = await self._get()
        names = [s["name"] for s in data["skills"]]
        self.assertEqual(names, ["pentesting", "writing-plans", "figma:figma-use"])
        self.assertEqual(data["total"], 3)

    async def test_entries_keep_legacy_fields(self):
        data = await self._get()
        for skill in data["skills"]:
            self.assertIn("description", skill)
            self.assertIn("installed", skill)
            self.assertIn("active", skill)

    async def test_sources_group_with_counts(self):
        data = await self._get()
        groups = {g["id"]: g for g in data["sources"]}
        self.assertEqual(groups["user"]["count"], 2)
        self.assertEqual(groups["user"]["label"], "Your skills")
        self.assertEqual(groups["plugin:figma"]["count"], 1)
        self.assertEqual(groups["plugin:figma"]["label"], "figma")

    async def test_active_matches_bare_name(self):
        data = await self._get(query={"session_id": "s1"}, active=["pentesting"])
        active = {s["name"]: s["active"] for s in data["skills"]}
        self.assertTrue(active["pentesting"])
        self.assertFalse(active["writing-plans"])
        self.assertEqual(data["active_count"], 1)

    async def test_active_matches_plugin_skill_by_bare_name(self):
        # Sessions record skills by their bare name, without the plugin prefix.
        data = await self._get(query={"session_id": "s1"}, active=["figma-use"])
        active = {s["name"]: s["active"] for s in data["skills"]}
        self.assertTrue(active["figma:figma-use"])

    async def test_active_matches_namespaced_name(self):
        data = await self._get(query={"session_id": "s1"}, active=["figma:figma-use"])
        active = {s["name"]: s["active"] for s in data["skills"]}
        self.assertTrue(active["figma:figma-use"])

    async def test_source_active_counts(self):
        data = await self._get(query={"session_id": "s1"}, active=["figma-use"])
        groups = {g["id"]: g for g in data["sources"]}
        self.assertEqual(groups["plugin:figma"]["active_count"], 1)
        self.assertEqual(groups["user"]["active_count"], 0)

    async def test_session_id_echoed(self):
        data = await self._get(query={"session_id": "abc"})
        self.assertEqual(data["session_id"], "abc")

    async def test_no_session_id_gives_empty_string(self):
        data = await self._get()
        self.assertEqual(data["session_id"], "")
        self.assertEqual(data["active_count"], 0)

    async def test_respects_skill_limit(self):
        for i in range(12):
            _write_skill(self.user_root, f"bulk-{i:02d}")
        with patch.object(app, "_SKILL_LIMIT", 5):
            data = await self._get()
        self.assertEqual(len(data["skills"]), 5)
        self.assertEqual(data["total"], 5)


if __name__ == "__main__":
    unittest.main()


# ── Client IP attribution (rate-limit keying) ──────────────────────────────────


class ClientIpTests(unittest.TestCase):
    """_client_ip only honours forwarded headers from a trusted proxy."""

    def _req(self, peer, headers=None):
        return SimpleNamespace(
            client=SimpleNamespace(host=peer),
            headers=headers or {},
        )

    def test_uses_peer_when_no_proxies_configured(self):
        req = self._req("203.0.113.9", {"x-real-ip": "1.2.3.4"})
        with patch.object(config, "_str", return_value=""):
            self.assertEqual(app._client_ip(req), "203.0.113.9")

    def test_spoofed_header_is_ignored_from_untrusted_peer(self):
        # The app is exposed directly today, so an attacker rotating X-Real-IP
        # must not be able to get a fresh rate-limit bucket.
        req = self._req("203.0.113.9", {"x-real-ip": "9.9.9.9"})
        with patch.object(config, "_str", return_value="127.0.0.1/32"):
            self.assertEqual(app._client_ip(req), "203.0.113.9")

    def test_header_is_honoured_from_trusted_peer(self):
        req = self._req("127.0.0.1", {"x-real-ip": "9.9.9.9"})
        with patch.object(config, "_str", return_value="127.0.0.1/32"):
            self.assertEqual(app._client_ip(req), "9.9.9.9")

    def test_forwarded_for_chain_uses_leftmost(self):
        req = self._req("127.0.0.1", {"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        with patch.object(config, "_str", return_value="127.0.0.1/32"):
            self.assertEqual(app._client_ip(req), "9.9.9.9")

    def test_malformed_header_falls_back_to_peer(self):
        req = self._req("127.0.0.1", {"x-real-ip": "not-an-ip"})
        with patch.object(config, "_str", return_value="127.0.0.1/32"):
            self.assertEqual(app._client_ip(req), "127.0.0.1")

    def test_invalid_cidr_entries_are_skipped(self):
        req = self._req("127.0.0.1", {"x-real-ip": "9.9.9.9"})
        with patch.object(config, "_str", return_value="garbage,127.0.0.1/32"):
            self.assertEqual(app._client_ip(req), "9.9.9.9")

    def test_missing_client_returns_unknown(self):
        req = SimpleNamespace(client=None, headers={})
        with patch.object(config, "_str", return_value="127.0.0.1/32"):
            self.assertEqual(app._client_ip(req), "unknown")


# ── CSRF token binding ────────────────────────────────────────────────────────


class CsrfBindingTests(unittest.TestCase):
    """_csrf_valid must bind the token to the requesting session."""

    def setUp(self):
        self.sid_a, self.csrf_a = auth.session_new("alice", "user")
        self.sid_b, self.csrf_b = auth.session_new("bob", "user")
        self.addCleanup(auth.session_drop, self.sid_a)
        self.addCleanup(auth.session_drop, self.sid_b)

    def test_own_token_validates(self):
        self.assertTrue(auth._csrf_valid(self.csrf_a, self.csrf_a, self.sid_a))

    def test_another_sessions_token_is_rejected(self):
        # Previously this scanned every live session and accepted any match.
        self.assertFalse(auth._csrf_valid(self.csrf_b, self.csrf_b, self.sid_a))

    def test_mismatched_halves_rejected(self):
        self.assertFalse(auth._csrf_valid(self.csrf_a, self.csrf_b, self.sid_a))

    def test_unknown_session_rejected(self):
        self.assertFalse(auth._csrf_valid(self.csrf_a, self.csrf_a, "no-such-sid"))

    def test_empty_tokens_rejected(self):
        self.assertFalse(auth._csrf_valid("", "", self.sid_a))
        self.assertFalse(auth._csrf_valid(self.csrf_a, "", self.sid_a))

    def test_legacy_call_without_sid_still_works(self):
        self.assertTrue(auth._csrf_valid(self.csrf_a, self.csrf_a))
        self.assertFalse(auth._csrf_valid("bogus-token", "bogus-token"))
