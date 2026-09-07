"""QA: a terminal session is routed by the same code as a console turn.

Pedro starts the CLI by hand inside `screen`. Those sessions never touched the
console's routing: `bin/wc-claude.sh` existed to fix that and reimplemented the
rule in bash, under a comment reading "Mirror claude_proxy._backend_env exactly,
including what it removes."

Measured across the four live sessions on 2026-09-03, that produced three
different configurations at once — two fully routed, one with a base URL and no
key, and one (this session) with neither, falling through to the CLI's own
default. Nothing reported any of it.

So `bin/wc-backend-env.py` emits the shared decision as shell lines and the
wrapper evals them. These tests drive that helper through a real bash, against a
throwaway database, because the failure being prevented is a shell-level one:
the *removals* only work if bash actually performs them, and a Python-level
assertion about the emitted string would not notice `unset` being spelled wrong.

`WC_PROFILE` is the enforcement half. It is not a secret, it is exported, and a
session without it is one that never went through the wrapper — which is the
only way the console can tell a routed terminal from an unrouted one.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "bin" / "wc-backend-env.py"
WRAPPER = REPO / "bin" / "wc-claude.sh"

# What the shell starts with, so the removals are observable. Every one of these
# must be gone or replaced; an empty starting environment would let a missing
# `unset` pass silently.
DIRTY_SHELL = {
    "ANTHROPIC_AUTH_TOKEN": "inherited-token",
    "ANTHROPIC_BASE_URL": "https://inherited.invalid",
    "ANTHROPIC_API_KEY": "inherited-key",
    "CLAUDE_CODE_SIMPLE": "1",
    "WC_PROFILE": "stale-profile",
}


def make_db(path: Path, **machine) -> None:
    """A throwaway database with one active machine.

    Never the production file: `db.init()` migrates, and running it against the
    live database is what took the write path down for 37 minutes (registry #41).
    """
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE ai_machines (id INTEGER PRIMARY KEY, name TEXT, "
            "provider TEXT, base_url TEXT, api_key TEXT, model TEXT, "
            "active_models TEXT, active INT)"
        )
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        if machine:
            con.execute(
                "INSERT INTO ai_machines (name, provider, base_url, api_key, "
                "model, active_models, active) VALUES (?,?,?,?,?,?,1)",
                (machine.get("name", "Test Machine"),
                 machine.get("provider", "claude_code"),
                 machine.get("base_url", ""),
                 machine.get("api_key", ""),
                 machine.get("model", ""),
                 machine.get("active_models", "")),
            )
        con.commit()
    finally:
        con.close()


class EvalInBashTests(unittest.TestCase):
    """The helper's output is eval'd, so bash is the only honest judge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "wc.db"

    def _env_after_eval(self, **machine) -> dict[str, str]:
        """The environment a shell holds after eval'ing the helper's output."""
        make_db(self.db, **machine)
        names = sorted({*DIRTY_SHELL, "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                        "WC_PROFILE", "WC_PROFILE_NAME",
                        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"})
        # `${x+set}` distinguishes unset from empty, which is the distinction
        # this whole mechanism turns on.
        dump = "; ".join(
            f'printf "%s\\t%s\\t%s\\n" {n} "${{{n}+set}}" "${{{n}-}}"'
            for n in names
        )
        script = f'eval "$(python3 {TOOL} --sh)"\n{dump}\n'
        env = {**os.environ, **DIRTY_SHELL, "WC_DB_PATH": str(self.db)}
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, env=env, timeout=60, check=True)
        result = {}
        for line in out.stdout.splitlines():
            name, present, value = (line.split("\t") + ["", ""])[:3]
            if present == "set":
                result[name] = value
        return result

    def test_an_inherited_auth_token_is_removed_by_bash(self):
        """The CLI prefers the token over the key, so an inherited one sends the
        session out on whatever account the shell was pointed at."""
        env = self._env_after_eval(base_url="https://api.anthropic.com",
                                   api_key="k")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_the_backend_base_url_replaces_the_inherited_one(self):
        env = self._env_after_eval(base_url="https://gw.example.invalid",
                                   api_key="k")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"),
                         "https://gw.example.invalid")

    def test_a_machine_with_no_base_url_removes_the_inherited_one(self):
        """Registry #68, at the shell level: "no base_url" means the official
        API, which requires removing an inherited value rather than leaving it."""
        env = self._env_after_eval(base_url="", api_key="k")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_a_keyless_machine_drops_the_key_and_simple_together(self):
        """Dropping the key without dropping CLAUDE_CODE_SIMPLE leaves the CLI
        unable to read the host login it has been told to fall back to, so the
        session is unauthenticated rather than merely unkeyed."""
        env = self._env_after_eval(base_url="https://api.anthropic.com",
                                   api_key="")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)

    def test_a_non_anthropic_machine_strips_the_anthropic_base_url(self):
        env = self._env_after_eval(provider="openai-compatible",
                                   base_url="https://x.invalid", api_key="k")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_the_profile_is_exported_and_names_the_backend(self):
        env = self._env_after_eval(name="CF AI Machine", api_key="k")
        self.assertEqual(env.get("WC_PROFILE"), "cf-ai-machine")
        self.assertEqual(env.get("WC_PROFILE_NAME"), "CF AI Machine")

    def test_a_stale_profile_is_cleared_when_no_machine_is_active(self):
        """Otherwise a shell that eval'd once keeps claiming a backend it is no
        longer on, and the console's routed/unrouted display inverts."""
        env = self._env_after_eval()  # no machine rows at all
        self.assertNotIn("WC_PROFILE", env)
        self.assertNotIn("WC_PROFILE_NAME", env)

    def test_a_quote_in_a_machine_name_cannot_execute(self):
        """Machine names come from the console UI and are about to be eval'd."""
        probe = Path(self.tmp.name) / "pwned"
        env = self._env_after_eval(
            name=f"evil'; touch {probe}; '", api_key="k")
        self.assertFalse(probe.exists(), "eval executed the machine name")
        self.assertIn("WC_PROFILE", env)


class CredentialHygieneTests(unittest.TestCase):
    """The key must not reach a command line or the JSON output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "wc.db"
        make_db(self.db, base_url="https://api.anthropic.com",
                api_key="super-secret-not-real", name="Test Machine")

    def _run(self, *args) -> str:
        env = {**os.environ, "WC_DB_PATH": str(self.db)}
        return subprocess.run(["python3", str(TOOL), *args], capture_output=True,
                              text=True, env=env, timeout=60,
                              check=True).stdout

    def test_json_mode_never_prints_the_key(self):
        out = self._run("--json")
        self.assertNotIn("super-secret-not-real", out)
        self.assertEqual(json.loads(out)["api_key"], "set")

    def test_the_key_is_read_from_the_database_not_an_argument(self):
        """`/proc/<pid>/cmdline` is world-readable -- that is how the four live
        sessions' routing was read while designing this. A key passed as an
        argument would be readable by every process on the host."""
        import inspect
        import sys
        sys.path.insert(0, str(REPO / "bin"))
        source = TOOL.read_text(encoding="utf-8")
        self.assertNotIn("--api-key", source)
        self.assertNotIn("--base-url", source)
        self.assertIn("mode=ro", source, "the database must be opened read-only")
        del inspect

    def test_sh_mode_does_set_the_key_because_that_is_the_point(self):
        self.assertIn("export ANTHROPIC_API_KEY=", self._run("--sh"))


class TheWrapperDelegatesTests(unittest.TestCase):
    """Structural: the bash copy of the rule must not come back."""

    def test_apply_env_calls_the_shared_helper(self):
        body = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("wc-backend-env.py", body)

    def test_apply_env_no_longer_applies_the_rule_itself(self):
        """These are the lines the bash mirror used. Their return means the rule
        is being maintained in two places again, which is the state that shipped
        two divergences."""
        body = WRAPPER.read_text(encoding="utf-8")
        for line in ('export ANTHROPIC_API_KEY="$API_KEY"',
                     'export ANTHROPIC_BASE_URL="$BASE_URL"'):
            self.assertNotIn(line, body, f"{line} is back in the wrapper")

    def test_the_wrapper_is_still_valid_bash(self):
        subprocess.run(["bash", "-n", str(WRAPPER)], check=True, timeout=60)


if __name__ == "__main__":
    unittest.main()


class SchemaToleranceTests(unittest.TestCase):
    """An older database must not stop a terminal from launching.

    `active_models` was added after first release. Naming columns in the query
    made the tool exit 1 against any file predating it -- and because the
    wrapper evals this tool, that turns a schema difference into "no shell can
    start claude". Found by the throwaway fixture here, which had the older
    shape.
    """

    def test_a_database_without_active_models_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "CREATE TABLE ai_machines (id INTEGER PRIMARY KEY, name TEXT, "
                    "provider TEXT, base_url TEXT, api_key TEXT, model TEXT, "
                    "active INT)")
                con.execute(
                    "INSERT INTO ai_machines (name, provider, base_url, api_key, "
                    "model, active) VALUES ('Old','anthropic','','k','m',1)")
                con.commit()
            finally:
                con.close()
            env = {**os.environ, "WC_DB_PATH": str(db)}
            out = subprocess.run(["python3", str(TOOL), "--json"],
                                 capture_output=True, text=True, env=env,
                                 timeout=60, check=True)
            self.assertEqual(json.loads(out.stdout)["name"], "Old")

    def test_a_missing_database_is_not_a_crash(self):
        """A checkout with no data/ yet must still be able to start a shell."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "WC_DB_PATH": str(Path(tmp) / "absent.db")}
            out = subprocess.run(["python3", str(TOOL), "--sh"],
                                 capture_output=True, text=True, env=env,
                                 timeout=60, check=True)
            self.assertIn("unset WC_PROFILE", out.stdout)


def make_multi_db(path: Path, rows) -> None:
    """A throwaway database with several machines.

    *rows* are (id, name, base_url, api_key, model, active_models, active, owner).
    """
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE ai_machines (id TEXT PRIMARY KEY, name TEXT, "
            "provider TEXT, base_url TEXT, api_key TEXT, model TEXT, "
            "active_models TEXT, active INT, owner_id TEXT)")
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO ai_machines (id, name, provider, base_url, api_key, "
            "model, active_models, active, owner_id) "
            "VALUES (?,?,'anthropic',?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()


class OwnerScopingTests(unittest.TestCase):
    """A profile must never select another owner's backend.

    Found in production rather than reasoned about: a second owner created a
    machine on 2026-09-03 at 09:24, and `--profile` read every row regardless of
    owner. That row happened to carry no key, so nothing leaked -- but the
    design would have exported somebody else's credential into a shell, and
    every other machine read in this codebase is owner-scoped already
    (`get_backend(chat_id, owner)`, `usage_earliest(owner)`).

    The owner is taken from whichever machine is active, because that is the
    identity the console is actually routing under.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "wc.db"

    def _run(self, *args):
        env = {**os.environ, "WC_DB_PATH": str(self.db)}
        # check=False is the point: these cases assert on a refusal.
        return subprocess.run(["python3", str(TOOL), *args], capture_output=True,
                              text=True, env=env, timeout=60, check=False)

    def test_another_owners_machine_is_not_selectable(self):
        make_multi_db(self.db, [
            ("a1", "Mine", "", "", "m1", "[]", 1, "admin"),
            ("b1", "Theirs", "https://gw.invalid", "their-secret", "m2", "[]", 0,
             "someone-else"),
        ])
        result = self._run("--profile", "theirs", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no backend named", result.stderr)

    def test_another_owners_machine_is_not_even_listed(self):
        """The "Known profiles" hint must not enumerate what it will refuse --
        naming another owner's backends is itself a small disclosure."""
        make_multi_db(self.db, [
            ("a1", "Mine", "", "", "m1", "[]", 1, "admin"),
            ("b1", "Theirs", "https://gw.invalid", "s", "m2", "[]", 0, "else"),
        ])
        result = self._run("--profile", "nope", "--json")
        self.assertIn("mine", result.stderr)
        self.assertNotIn("theirs", result.stderr)

    def test_another_owners_model_is_not_resolved_to(self):
        make_multi_db(self.db, [
            ("a1", "Mine", "", "", "m1", "[]", 1, "admin"),
            ("b1", "Theirs", "https://gw.invalid", "s", "exotic/model", "[]", 0,
             "else"),
        ])
        self.assertEqual(self._run("--resolve-model", "exotic/model").stdout.strip(),
                         "")


class SlugCollisionTests(unittest.TestCase):
    """Two machines can share a name. They did, so ambiguity must be refused.

    `matches[0]` over an unordered SELECT silently picked one, which made the
    backend a session ran on depend on row order rather than on a decision.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "wc.db"

    def _run(self, *args):
        env = {**os.environ, "WC_DB_PATH": str(self.db)}
        # check=False is the point: these cases assert on a refusal.
        return subprocess.run(["python3", str(TOOL), *args], capture_output=True,
                              text=True, env=env, timeout=60, check=False)

    def _duplicated(self):
        make_multi_db(self.db, [
            ("a1", "Anthropic API", "", "", "claude-sonnet-5", "[]", 1, "admin"),
            ("a2", "Anthropic API", "", "", "claude-opus-5", "[]", 0, "admin"),
            ("g1", "CF AI Machine", "https://gw.invalid", "k", "vllm/Q", "[]", 0,
             "admin"),
        ])

    def test_a_duplicated_name_is_refused_not_guessed(self):
        self._duplicated()
        result = self._run("--profile", "anthropic-api", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("matches 2 backends", result.stderr)

    def test_the_refusal_names_both_candidates(self):
        """So the operator can tell which two to rename."""
        self._duplicated()
        stderr = self._run("--profile", "anthropic-api", "--json").stderr
        self.assertIn("a1", stderr)
        self.assertIn("a2", stderr)

    def test_a_model_on_a_shared_slug_machine_is_not_inferred(self):
        """Otherwise the wrapper announces "switching to anthropic-api" and then
        fails on the very name it just chose -- an inference that refuses its own
        answer, which is worse than staying silent."""
        self._duplicated()
        self.assertEqual(
            self._run("--resolve-model", "claude-opus-5").stdout.strip(), "")

    def test_a_model_on_a_unique_slug_machine_is_still_inferred(self):
        self._duplicated()
        self.assertEqual(
            self._run("--resolve-model", "vllm/Q").stdout.strip(), "cf-ai-machine")

    def test_an_unambiguous_name_still_works(self):
        self._duplicated()
        result = self._run("--profile", "cf-ai-machine", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["name"], "CF AI Machine")
