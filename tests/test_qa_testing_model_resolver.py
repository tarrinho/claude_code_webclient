"""QA: how tests/conftest.py resolves WC_TESTING_MODEL, the value every test
file reads via tests.testing_model.TESTING_MODEL instead of hardcoding a
model id.

Two sources, chosen by the Settings dialog's "Enforce testing default model"
knob (config.TESTING_MODEL_ENFORCE_DEFAULT / the testing_model_enforce
setting): a fixed configured value, or the model actually in effect for the
current agent session, resolved the same way bin/wc-claude.sh does --
`bin/wc-backend-env.py --profile "$WC_PROFILE" --json`.

_resolve_testing_model is exercised directly (imported from tests.conftest)
rather than through a full pytest subprocess -- it is a plain function with
no fixtures of its own, and every dependency it has (sqlite3, subprocess,
environment) is mockable at the boundary. Every branch is required to fall
back to a safe default rather than raise: a broken resolution must not be why
a whole test run cannot start.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tests.conftest as conftest


class _Env(unittest.TestCase):
    """Isolates os.environ for the duration of each test -- the function
    under test reads several WC_* variables directly, and this suite must
    not depend on (or leak into) whatever the real invoking shell set."""

    _KEYS = (
        "WC_TESTING_MODEL_DEFAULT", "WC_TESTING_MODEL_ENFORCE_DEFAULT",
        "WC_PROD_DB_PATH_FOR_TESTING_MODEL", "WC_PROFILE",
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for k in self._KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class NoProductionDatabaseTests(_Env):
    """The common case for a fresh checkout: no production DB exists yet at
    all. Must not raise, must fall back to the plain default."""

    def test_falls_back_to_the_built_in_default(self):
        os.environ["WC_PROD_DB_PATH_FOR_TESTING_MODEL"] = "/nonexistent/wc.db"
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_respects_a_custom_default(self):
        os.environ["WC_PROD_DB_PATH_FOR_TESTING_MODEL"] = "/nonexistent/wc.db"
        os.environ["WC_TESTING_MODEL_DEFAULT"] = "claude-sonnet-5"
        self.assertEqual(conftest._resolve_testing_model(), "claude-sonnet-5")


class _WithSettingsDb(_Env):
    """A real, throwaway sqlite file shaped like the production settings and
    ai_machines tables -- not the production database itself (CLAUDE.md rule
    9: this resolver is read-only against it, but the test suite must never
    depend on that file's existence or contents)."""

    def _db(self, testing_default_model=None, testing_model_enforce=None,
            machines=()):
        """*machines*: iterable of (model, active_models_json_or_None,
        enabled) tuples, matching what _served_by_any_enabled_machine reads."""
        tmp = Path(tempfile.mkdtemp(prefix="wc-testing-model-")) / "wc.db"
        con = sqlite3.connect(str(tmp))
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "CREATE TABLE ai_machines (model TEXT, active_models TEXT, "
            "enabled INTEGER)")
        if testing_default_model is not None:
            con.execute("INSERT INTO settings VALUES ('testing_default_model', ?)",
                       (testing_default_model,))
        if testing_model_enforce is not None:
            con.execute("INSERT INTO settings VALUES ('testing_model_enforce', ?)",
                       (testing_model_enforce,))
        con.executemany(
            "INSERT INTO ai_machines VALUES (?, ?, ?)", list(machines))
        con.commit()
        con.close()
        os.environ["WC_PROD_DB_PATH_FOR_TESTING_MODEL"] = str(tmp)


class EnforcedTests(_WithSettingsDb):
    """Enforced means a fixed value -- but "fixed" is only useful if
    something can actually serve it. Every case here either has a matching
    machine row (verified reachable) or is checked against what happens when
    nothing does."""

    def test_enforced_uses_the_configured_value_when_a_machine_serves_it(self):
        self._db(testing_default_model="claude-opus-5",
                 machines=[("claude-opus-5", None, 1)])
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_a_value_in_the_active_models_list_also_counts_as_served(self):
        """Not just a machine's own default -- its declared list too, the
        same two sources bin/wc-backend-env.py's own served_models()
        checks."""
        self._db(testing_default_model="claude-haiku-4-5", machines=[
            ("claude-opus-5", '["claude-opus-5", "claude-haiku-4-5"]', 1),
        ])
        self.assertEqual(conftest._resolve_testing_model(), "claude-haiku-4-5")

    def test_an_empty_settings_table_falls_back_to_the_plain_default(self):
        self._db()
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_configured_but_unreachable_falls_back_to_the_plain_default(self):
        """The regression this whole change exists to fix: a configured
        value with no machine anywhere that serves it must not be trusted
        just because it was typed into Settings."""
        self._db(testing_default_model="nobody-serves-this-xyz",
                 testing_model_enforce="1",
                 machines=[("claude-opus-5", None, 1)])
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_a_disabled_machine_does_not_count_as_serving_it(self):
        self._db(testing_default_model="claude-sonnet-5",
                 machines=[("claude-sonnet-5", None, 0)])
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_ambiguity_across_several_machines_is_not_a_failure(self):
        """The exact shape measured live during design: several machines
        legitimately serving the same model must read as reachable, not as
        the ambiguous-refusal bin/wc-backend-env.py's own --resolve-model
        gives for a different question (routing a bare --model flag to
        exactly one backend)."""
        self._db(testing_default_model="vllm/Qwen3.6-35B-A3B-NVFP4", machines=[
            ("vllm/Qwen3.6-35B-A3B-NVFP4", None, 1),
            ("azure_ai/gpt-5.4-mini", '["vllm/Qwen3.6-35B-A3B-NVFP4"]', 1),
        ])
        self.assertEqual(
            conftest._resolve_testing_model(), "vllm/Qwen3.6-35B-A3B-NVFP4")

    def test_enforce_never_shells_out(self):
        """The whole point of enforce=True: no subprocess, no dependency on
        WC_PROFILE or bin/wc-backend-env.py being present or working --
        verification here is a plain local sqlite read, nothing more."""
        self._db(testing_default_model="claude-sonnet-5",
                 testing_model_enforce="1",
                 machines=[("claude-sonnet-5", None, 1)])
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        with patch("subprocess.run") as run:
            self.assertEqual(conftest._resolve_testing_model(), "claude-sonnet-5")
        run.assert_not_called()

    def test_an_unreachable_configured_value_never_falls_through_to_the_environment(self):
        """The deliberate asymmetry: enforce=True failing verification must
        land on the bare code default, never on _resolve_via_current_agent --
        falling through to the environment would make "enforce" silently
        stop being fixed the moment the configured value drifted stale,
        which is worse than just being wrong in an obvious, consistent way."""
        self._db(testing_default_model="nobody-serves-this-xyz",
                 testing_model_enforce="1")
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        with patch("subprocess.run") as run:
            self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")
        run.assert_not_called()


def _fake_run(json_returncode=0, check_returncode=0, json_stdout=None):
    """A subprocess.run stand-in that tells the --json call from the
    --check-model call apart by argv, since _resolve_via_current_agent makes
    both in sequence and a single shared return_value cannot distinguish
    them."""
    def run(argv, **kwargs):
        if "--json" in argv:
            return MagicMock(returncode=json_returncode,
                             stdout=json_stdout or json.dumps({}))
        return MagicMock(returncode=check_returncode, stdout="")
    return run


class NotEnforcedTests(_WithSettingsDb):
    def test_no_profile_falls_back_to_the_configured_value(self):
        """Off, but nothing to resolve from -- a bare test invocation with no
        WC_PROFILE (not run through the wc-claude.sh wrapper) has no 'current
        agent' to ask."""
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0",
                 machines=[("claude-opus-5", None, 1)])
        self.assertNotIn("WC_PROFILE", os.environ)
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_resolves_via_wc_backend_env_when_a_profile_is_set_and_verified(self):
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0")
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        fake = _fake_run(json_stdout=json.dumps({"model": "claude-sonnet-5"}))
        with patch("subprocess.run", side_effect=fake) as run:
            result = conftest._resolve_testing_model()
        self.assertEqual(result, "claude-sonnet-5")
        # Both calls happened, in order, against the same profile -- asserted
        # so a future refactor cannot quietly resolve a different profile
        # than the one this session is actually pinned to, or skip the
        # verification step entirely.
        self.assertEqual(len(run.call_args_list), 2)
        json_argv, check_argv = (c.args[0] for c in run.call_args_list)
        self.assertIn("--json", json_argv)
        self.assertIn("anthropic-oauth", json_argv)
        self.assertIn("--check-model", check_argv)
        self.assertIn("claude-sonnet-5", check_argv)
        self.assertIn("anthropic-oauth", check_argv)

    def test_a_model_the_backend_does_not_serve_falls_back(self):
        """The regression this whole change exists to fix on the not-enforced
        side: --json's own configured default is unverified, and --check-model
        refusing it (the backend declared a list and this is not on it) must
        not be trusted anyway."""
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0",
                 machines=[("claude-opus-5", None, 1)])
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        fake = _fake_run(
            json_stdout=json.dumps({"model": "claude-sonnet-5"}),
            check_returncode=1)
        with patch("subprocess.run", side_effect=fake):
            self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_a_broken_wc_backend_env_falls_back_rather_than_raising(self):
        """subprocess failing, or emitting something that is not the expected
        JSON, must not be the reason a whole test run cannot start."""
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0",
                 machines=[("claude-opus-5", None, 1)])
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        with patch("subprocess.run", side_effect=OSError("no such file")):
            self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_a_resolved_model_missing_from_the_json_falls_back(self):
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0",
                 machines=[("claude-opus-5", None, 1)])
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        fake = _fake_run(json_stdout=json.dumps({"provider": "claude_code"}))
        with patch("subprocess.run", side_effect=fake):
            self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_a_check_model_failure_falls_through_to_the_configured_value_if_it_verifies(self):
        """If the current-agent model fails verification but the configured
        fallback is itself reachable, use the fallback rather than the bare
        code default -- the configured value is still a better answer than
        giving up on it entirely."""
        self._db(testing_default_model="claude-haiku-4-5",
                 testing_model_enforce="0",
                 machines=[("claude-haiku-4-5", None, 1)])
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        fake = _fake_run(
            json_stdout=json.dumps({"model": "claude-sonnet-5"}),
            check_returncode=1)
        with patch("subprocess.run", side_effect=fake):
            self.assertEqual(conftest._resolve_testing_model(), "claude-haiku-4-5")


class LiveIntegrationTests(unittest.TestCase):
    """One real, unmocked run -- proves the resolver's actual subprocess
    invocation of bin/wc-backend-env.py works end to end, not just that the
    mocked call shape looks right."""

    def test_resolves_the_real_current_session_when_profile_is_set(self):
        profile = os.environ.get("WC_PROFILE")
        if not profile:
            self.skipTest("WC_PROFILE not set in this environment")
        with patch.dict(os.environ, {"WC_TESTING_MODEL_ENFORCE_DEFAULT": "0"}), \
                patch("sqlite3.connect", side_effect=sqlite3.OperationalError):
            result = conftest._resolve_testing_model()
        self.assertTrue(result, "resolver returned an empty model id")


if __name__ == "__main__":
    unittest.main()
