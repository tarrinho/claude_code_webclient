"""QA: bin/wc-claude.sh points a terminal session at the configured backend.

WebConsole's active machine governs the turns *it* spawns. A session started by
hand from a shell never reads that database, so it ignores the configuration and
falls through to the CLI's default -- api.anthropic.com on the host login. Six
terminal sessions on this machine were doing that all day while the console was
configured for a gateway, and nothing said so. The wrapper closes that gap.

**These tests execute the script.** That is the whole point of them. Registry
#48: a reclaim block in launch.sh could never succeed, and nine tests passed
because every one of them read the source instead of running it. This file runs
`bash bin/wc-claude.sh` against a throwaway database, in WC_CLAUDE_DRY_RUN mode
so nothing is spawned, and asserts on the environment it reports.

The dry-run output is the contract being tested, so it is treated as one: it
prints the exec line and each variable as set/unset, with secrets reduced to a
length. A test that needed the real value would be a test that leaks it.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "wc-claude.sh"

GATEWAY_URL = "https://gateway.invalid"
GATEWAY_MODEL = "vendor/some-model"
GATEWAY_KEY = "not-a-real-key-0123456789"


def make_db(path: Path, *, machines: list[dict], default_model: str = ""):
    """A WebConsole database with just enough schema for the wrapper's query."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ai_machines (id TEXT PRIMARY KEY, name TEXT, provider TEXT,"
        " base_url TEXT, api_key TEXT, model TEXT, active INTEGER DEFAULT 0)"
    )
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    for m in machines:
        con.execute(
            "INSERT INTO ai_machines (id, name, provider, base_url, api_key,"
            " model, active) VALUES (?,?,?,?,?,?,?)",
            (m["id"], m["name"], m.get("provider", "anthropic"),
             m.get("base_url", ""), m.get("api_key", ""), m.get("model", ""),
             int(m.get("active", 0))),
        )
    if default_model:
        con.execute("INSERT INTO settings (key, value) VALUES ('default_model', ?)",
                    (default_model,))
    con.commit()
    con.close()


@unittest.skipUnless(shutil.which("bash"), "bash not on PATH")
@unittest.skipUnless(WRAPPER.is_file(), "wrapper not present")
class WrapperTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "webconsole.db"
        self.addCleanup(self.tmp.cleanup)

    def run_wrapper(self, *args, db: Path | None = None,
                    env_extra: dict | None = None) -> str:
        """Run the wrapper in dry-run mode and return stdout+stderr."""
        env = dict(os.environ)
        env["WC_CLAUDE_DRY_RUN"] = "1"
        env["WC_DB_PATH"] = str(self.db if db is None else db)
        # Pollute the environment the way a shell that had been pointed at a
        # gateway would be. The wrapper has to clear what does not apply.
        env["ANTHROPIC_BASE_URL"] = "https://inherited.invalid"
        env["ANTHROPIC_AUTH_TOKEN"] = "inherited-token"
        env.update(env_extra or {})
        proc = subprocess.run(
            ["bash", str(WRAPPER), *args],
            capture_output=True, text=True, env=env, timeout=120, check=False,
        )
        return proc.stdout + proc.stderr

    # ── the case the wrapper exists for ──────────────────────────────────────

    def test_a_gateway_machine_sets_its_endpoint(self):
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        out = self.run_wrapper()
        self.assertIn(f"ANTHROPIC_BASE_URL = {GATEWAY_URL}", out)
        self.assertIn("ANTHROPIC_API_KEY = <set", out)

    def test_the_machines_model_is_passed_to_the_cli(self):
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        self.assertIn(f"--model {GATEWAY_MODEL}", self.run_wrapper())

    def test_the_key_is_never_printed(self):
        """The wrapper reports secrets by length; a leak here would be shipped."""
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        self.assertNotIn(GATEWAY_KEY, self.run_wrapper())

    # ── registry #68, applied to the shell ───────────────────────────────────

    def test_an_inherited_auth_token_is_always_dropped(self):
        """The CLI prefers it over the API key, so it would decide the account."""
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        self.assertIn("ANTHROPIC_AUTH_TOKEN = (unset)", self.run_wrapper())

    def test_a_machine_with_no_base_url_clears_an_inherited_one(self):
        """No base_url means the official API -- registry #68's exact shape."""
        make_db(self.db, machines=[{
            "id": "m1", "name": "Anthropic", "base_url": "",
            "api_key": "", "model": "claude-opus-5", "active": 1}])
        out = self.run_wrapper()
        self.assertIn("ANTHROPIC_BASE_URL = (unset)", out)

    def test_a_machine_with_no_key_falls_back_to_the_host_login(self):
        """CLAUDE_CODE_SIMPLE must go, or the CLI refuses to read the login."""
        make_db(self.db, machines=[{
            "id": "m1", "name": "Anthropic", "base_url": "",
            "api_key": "", "model": "claude-opus-5", "active": 1}])
        out = self.run_wrapper(env_extra={"CLAUDE_CODE_SIMPLE": "1"})
        self.assertIn("ANTHROPIC_API_KEY = (unset)", out)
        self.assertIn("CLAUDE_CODE_SIMPLE = (unset)", out)

    def test_a_non_anthropic_backend_gets_no_anthropic_variables(self):
        make_db(self.db, machines=[{
            "id": "m1", "name": "Proxy", "provider": "proxy",
            "base_url": GATEWAY_URL, "api_key": GATEWAY_KEY, "active": 1}])
        out = self.run_wrapper()
        self.assertIn("ANTHROPIC_BASE_URL = (unset)", out)
        self.assertIn("ANTHROPIC_AUTH_TOKEN = (unset)", out)

    # ── falling through rather than failing ──────────────────────────────────

    def test_an_explicit_model_is_not_overridden(self):
        """Yours wins; the wrapper only supplies one when you gave none."""
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        out = self.run_wrapper("--model", "mine/choice")
        self.assertNotIn(f"--model {GATEWAY_MODEL}", out)
        self.assertIn("mine/choice", out)

    def test_no_active_machine_starts_claude_unchanged(self):
        make_db(self.db, machines=[{
            "id": "m1", "name": "Idle", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 0}])
        self.assertIn("no active machine", self.run_wrapper())

    def test_a_missing_database_starts_claude_unchanged(self):
        """A wrapper that fails closed would make the CLI unusable."""
        out = self.run_wrapper(db=Path(self.tmp.name) / "absent.db")
        self.assertIn("no WebConsole database", out)

    def test_the_global_default_is_used_when_the_machine_names_no_model(self):
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": "", "active": 1}],
            default_model="global/model")
        self.assertIn("--model global/model", self.run_wrapper())

    def test_user_arguments_are_passed_through(self):
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        self.assertIn("--continue", self.run_wrapper("--continue"))

    # ── the database is read, never written ──────────────────────────────────

    def test_the_database_is_not_modified(self):
        """Registry #41: a second writer took the production write path down."""
        make_db(self.db, machines=[{
            "id": "m1", "name": "Gateway", "base_url": GATEWAY_URL,
            "api_key": GATEWAY_KEY, "model": GATEWAY_MODEL, "active": 1}])
        before = self.db.read_bytes()
        self.run_wrapper()
        self.assertEqual(self.db.read_bytes(), before)

    def test_it_opens_the_database_read_only(self):
        """Asserted on the source too: mode=ro is the guarantee, not the effect."""
        self.assertIn("mode=ro", WRAPPER.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
