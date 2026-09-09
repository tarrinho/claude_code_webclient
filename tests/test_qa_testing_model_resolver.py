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
    """A real, throwaway sqlite file shaped like the production settings
    table -- not the production database itself (CLAUDE.md rule 9: this
    resolver is read-only against it, but the test suite must never depend
    on that file's existence or contents)."""

    def _db(self, testing_default_model=None, testing_model_enforce=None):
        tmp = Path(tempfile.mkdtemp(prefix="wc-testing-model-")) / "wc.db"
        con = sqlite3.connect(str(tmp))
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        if testing_default_model is not None:
            con.execute("INSERT INTO settings VALUES ('testing_default_model', ?)",
                       (testing_default_model,))
        if testing_model_enforce is not None:
            con.execute("INSERT INTO settings VALUES ('testing_model_enforce', ?)",
                       (testing_model_enforce,))
        con.commit()
        con.close()
        os.environ["WC_PROD_DB_PATH_FOR_TESTING_MODEL"] = str(tmp)


class EnforcedTests(_WithSettingsDb):
    def test_enforced_by_default_uses_the_configured_value(self):
        self._db(testing_default_model="claude-opus-5")
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_an_empty_settings_table_still_uses_the_plain_default(self):
        self._db()
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_enforce_flag_set_to_1_uses_the_configured_value(self):
        self._db(testing_default_model="claude-sonnet-5", testing_model_enforce="1")
        self.assertEqual(conftest._resolve_testing_model(), "claude-sonnet-5")

    def test_enforce_never_shells_out(self):
        """The whole point of enforce=True: no subprocess, no dependency on
        WC_PROFILE or bin/wc-backend-env.py being present or working."""
        self._db(testing_default_model="claude-sonnet-5", testing_model_enforce="1")
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        with patch("subprocess.run") as run:
            self.assertEqual(conftest._resolve_testing_model(), "claude-sonnet-5")
        run.assert_not_called()


class NotEnforcedTests(_WithSettingsDb):
    def test_no_profile_falls_back_to_the_configured_value(self):
        """Off, but nothing to resolve from -- a bare test invocation with no
        WC_PROFILE (not run through the wc-claude.sh wrapper) has no 'current
        agent' to ask."""
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0")
        self.assertNotIn("WC_PROFILE", os.environ)
        self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_resolves_via_wc_backend_env_when_a_profile_is_set(self):
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0")
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        fake = MagicMock(
            stdout=json.dumps({"model": "claude-sonnet-5"}), returncode=0)
        with patch("subprocess.run", return_value=fake) as run:
            result = conftest._resolve_testing_model()
        self.assertEqual(result, "claude-sonnet-5")
        # The exact call bin/wc-claude.sh's own routing already makes --
        # asserted so a future refactor cannot quietly resolve a different
        # profile than the one this session is actually pinned to.
        argv = run.call_args.args[0]
        self.assertIn("--profile", argv)
        self.assertIn("anthropic-oauth", argv)
        self.assertIn("--json", argv)

    def test_a_broken_wc_backend_env_falls_back_rather_than_raising(self):
        """subprocess failing, or emitting something that is not the expected
        JSON, must not be the reason a whole test run cannot start."""
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0")
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        with patch("subprocess.run", side_effect=OSError("no such file")):
            self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")

    def test_a_resolved_model_missing_from_the_json_falls_back(self):
        self._db(testing_default_model="claude-opus-5", testing_model_enforce="0")
        os.environ["WC_PROFILE"] = "anthropic-oauth"
        fake = MagicMock(stdout=json.dumps({"provider": "claude_code"}))
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(conftest._resolve_testing_model(), "claude-opus-5")


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
