"""QA: one definition of the environment a backend implies.

The rule lived in three places -- `claude_proxy._backend_env` for proxied
turns, `runner._build_env` for direct ones, and `apply_env` in
`bin/wc-claude.sh` for a session started by hand. The shell one carried a
comment saying "Mirror claude_proxy._backend_env exactly, including what it
removes", which is an instruction to a human to keep two files in step by
hand. Registry #68 is what that costs: the proxy inherited ANTHROPIC_BASE_URL
from its shell, so switching a machine off a gateway and back to Anthropic kept
sending turns to the gateway, with nothing in the UI to say so.

`backend_env.deltas` is now the single definition. These tests do two jobs, and
the second is the one that makes the refactor safe to land:

* they state the rule directly, case by case
* they assert `deltas` agrees with the *existing* `claude_proxy._backend_env`
  across a matrix, so the refactor is demonstrably behaviour-preserving rather
  than merely believed to be

The equivalence test was written and run against the old implementation before
anything was rewired. That ordering is the point: a test written after the
rewrite only proves the new code agrees with itself.

The removals matter more than the additions and are easier to get wrong. An
added variable is visible in any log; a variable that should have been removed
and was not produces turns that succeed against the wrong endpoint or the wrong
account, which is the failure mode with no symptom.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import backend_env
import claude_proxy
import runner

ANTHROPIC = {
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "api_key": "test-key-not-real",
}
GATEWAY = {
    "provider": "anthropic",
    "base_url": "https://llm.example.invalid",
    "api_key": "test-key-not-real",
}
KEYLESS = {"provider": "anthropic", "base_url": "https://api.anthropic.com",
           "api_key": ""}
DEFAULT_URL = {"provider": "anthropic", "base_url": "", "api_key": "k"}
NON_ANTHROPIC = {"provider": "openai-compatible", "base_url": "https://x.invalid",
                 "api_key": "k"}

# Every shape a machine record reaches these functions in, including the ones a
# database written before a column existed still produces.
MATRIX = [
    ("no backend", None),
    ("empty dict", {}),
    ("provider missing", {"base_url": "https://x.invalid"}),
    ("non-anthropic", NON_ANTHROPIC),
    ("anthropic official", ANTHROPIC),
    ("anthropic gateway", GATEWAY),
    ("anthropic keyless", KEYLESS),
    ("anthropic default url", DEFAULT_URL),
    ("whitespace url and key", {"provider": "anthropic", "base_url": "   ",
                                "api_key": "  "}),
    ("non-string url", {"provider": "anthropic", "base_url": 123, "api_key": None}),
]

# A caller's environment that has every variable the rule cares about already
# set, which is the state that makes the removals observable at all. An empty
# starting environment would let a missing `unset` pass every assertion.
# Written by the injection payload if the escaping fails, so its absence is the
# assertion. Under /tmp deliberately: that is where the payload points.
PWNED_PROBE = os.path.join(tempfile.gettempdir(), "wc_pwned_probe")

DIRTY = {
    "PATH": "/usr/bin",
    "HOME": "/home/kali",
    "ANTHROPIC_AUTH_TOKEN": "inherited-token",
    "ANTHROPIC_BASE_URL": "https://inherited.invalid",
    "ANTHROPIC_API_KEY": "inherited-key",
    "CLAUDE_CODE_SIMPLE": "1",
}


# Captured from `claude_proxy._backend_env` on 2026-09-03, *before* it was
# rewired to call `backend_env.deltas`, and verified at that moment to match
# what deltas produced. Frozen here as literal data on purpose: once the proxy
# delegates to deltas, comparing the two only proves the new code agrees with
# itself. This table is the only thing in the suite that still remembers the
# behaviour that shipped, so a rewrite that changes it fails here.
#
# Updated 2026-09-04 (rules.md registry #81): the four non-anthropic/no-backend
# cases used to leave an inherited ANTHROPIC_API_KEY in place, unset only in the
# anthropic-with-empty-key branch. `bin/wc-claude.sh`'s hot-swap loop is a single
# long-lived process that calls `apply_env` repeatedly as the active machine
# changes, so a key exported for an earlier machine survived a switch to no
# machine or a non-anthropic one -- caught by
# `test_qa_wc_claude_hotswap.py::HotswapLoopTests::test_a_mid_session_deactivation_hands_off_unmanaged`.
# `deltas()` now unsets it unconditionally whenever the backend is not an
# anthropic machine with a key, and this table's four affected rows were
# deliberately updated to match, not just re-frozen around the old bug.
GOLDEN = {
    "anthropic default url": {
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "anthropic gateway": {
        "ANTHROPIC_API_KEY": "test-key-not-real",
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": "https://llm.example.invalid",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "anthropic keyless": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": None
    },
    "anthropic official": {
        "ANTHROPIC_API_KEY": "test-key-not-real",
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "empty dict": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "no backend": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "non-anthropic": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "non-string url": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": None
    },
    "provider missing": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": "1"
    },
    "whitespace url and key": {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_SIMPLE": None
    }
}


class TheRuleTests(unittest.TestCase):
    """Stated directly, case by case."""

    def test_the_auth_token_is_always_removed(self):
        """The CLI prefers ANTHROPIC_AUTH_TOKEN over ANTHROPIC_API_KEY, and a
        machine record never supplies a token. An inherited one therefore
        outranks the key we set, and the turn succeeds against whatever account
        the launching shell was pointed at -- a failure with no symptom."""
        for label, backend in MATRIX:
            with self.subTest(case=label):
                self.assertIn("ANTHROPIC_AUTH_TOKEN",
                              backend_env.deltas(backend).unset)

    def test_a_non_anthropic_backend_strips_the_anthropic_base_url(self):
        got = backend_env.deltas(NON_ANTHROPIC)
        self.assertIn("ANTHROPIC_BASE_URL", got.unset)
        self.assertNotIn("ANTHROPIC_BASE_URL", got.set)

    def test_an_explicit_base_url_is_set(self):
        self.assertEqual(
            backend_env.deltas(GATEWAY).set["ANTHROPIC_BASE_URL"],
            "https://llm.example.invalid")

    def test_no_base_url_removes_an_inherited_one(self):
        """Registry #68. "No base_url" means the official API, which requires
        removing an inherited value rather than leaving it in place."""
        got = backend_env.deltas(DEFAULT_URL)
        self.assertIn("ANTHROPIC_BASE_URL", got.unset)
        applied = got.apply_to(DIRTY)
        self.assertNotIn("ANTHROPIC_BASE_URL", applied)

    def test_a_key_is_set_and_a_missing_key_removes_the_inherited_one(self):
        self.assertEqual(backend_env.deltas(ANTHROPIC).set["ANTHROPIC_API_KEY"],
                         "test-key-not-real")
        got = backend_env.deltas(KEYLESS)
        self.assertIn("ANTHROPIC_API_KEY", got.unset)
        self.assertNotIn("ANTHROPIC_API_KEY", got.apply_to(DIRTY))

    def test_keyless_also_removes_simple_so_the_host_login_is_readable(self):
        """Under CLAUDE_CODE_SIMPLE the CLI ignores OAuth and the keychain, so
        leaving it set with no key leaves the turn unauthenticated."""
        self.assertIn("CLAUDE_CODE_SIMPLE", backend_env.deltas(KEYLESS).unset)

    def test_an_inherited_simple_is_removed_even_by_a_caller_that_never_sets_it(self):
        """This test originally asserted the opposite, and was wrong.

        The reasoning was that only the direct runner sets CLAUDE_CODE_SIMPLE,
        so only it should be told to remove one. The equivalence test below
        disproved it: the proxy removes an inherited one too, and must. The
        variable does not need to have been set by us to be present, and while
        it is set the CLI ignores OAuth and the keychain -- so a keyless backend
        under an inherited value sends a turn with no credentials at all.
        """
        self.assertIn("CLAUDE_CODE_SIMPLE", backend_env.deltas(KEYLESS).unset)
        self.assertNotIn(
            "CLAUDE_CODE_SIMPLE",
            backend_env.deltas(KEYLESS).apply_to(DIRTY),
            "an inherited CLAUDE_CODE_SIMPLE survived a keyless backend, which "
            "leaves the host login unreadable and the turn unauthenticated",
        )

    def test_whitespace_is_not_a_value(self):
        got = backend_env.deltas(
            {"provider": "anthropic", "base_url": "   ", "api_key": "  "})
        self.assertIn("ANTHROPIC_BASE_URL", got.unset)
        self.assertIn("ANTHROPIC_API_KEY", got.unset)

    def test_a_name_is_never_both_set_and_unset(self):
        for label, backend in MATRIX:
            with self.subTest(case=label):
                got = backend_env.deltas(backend)
                self.assertEqual(
                    set(got.set) & set(got.unset), set(),
                    "a variable in both is order-dependent, and the order is "
                    "not something a shell caller can be relied on to preserve",
                )

    def test_apply_to_does_not_mutate_its_input(self):
        original = dict(DIRTY)
        backend_env.deltas(ANTHROPIC).apply_to(DIRTY)
        self.assertEqual(DIRTY, original)


class ShellRenderingTests(unittest.TestCase):
    """The wrapper evals this, so a value is an injection surface."""

    def test_removals_and_settings_both_appear(self):
        rendered = backend_env.deltas(GATEWAY).as_shell()
        self.assertIn("unset ANTHROPIC_AUTH_TOKEN", rendered)
        self.assertIn("export ANTHROPIC_BASE_URL=", rendered)

    def test_a_quote_in_a_value_cannot_break_out(self):
        """A base URL or key is operator-supplied data, and it is being eval'd.

        Without escaping, a value containing a single quote closes the quoting
        and everything after it is executed by the shell.
        """
        evil = {"provider": "anthropic",
                "base_url": f"https://x.invalid'; touch {PWNED_PROBE}; '",
                "api_key": "k"}
        rendered = backend_env.deltas(evil).as_shell()
        # Deliberately *not* asserting the payload text is absent from the
        # rendered line. An earlier version of this test did, and it was wrong:
        # the text is the operator's data and must survive verbatim. What must
        # not survive is its *execution*, which is what the two assertions below
        # measure -- bash reproduces the value exactly, and the file the payload
        # would create does not exist.
        import subprocess
        out = subprocess.run(
            ["bash", "-c", f'{rendered}\nprintf "%s" "$ANTHROPIC_BASE_URL"'],
            capture_output=True, text=True, check=True, timeout=30,
        )
        self.assertEqual(out.stdout, evil["base_url"])
        self.assertFalse(
            os.path.exists(PWNED_PROBE),
            "eval executed the payload; the escaping does not hold",
        )


class DescribeTests(unittest.TestCase):
    """The banner and the logs must never carry the credential."""

    def test_the_key_value_never_appears(self):
        for backend in (ANTHROPIC, GATEWAY, NON_ANTHROPIC):
            with self.subTest(provider=backend["provider"]):
                self.assertNotIn("test-key-not-real",
                                 backend_env.describe(backend))
                self.assertNotIn(str(backend["api_key"]),
                                 backend_env.describe(backend))

    def test_presence_is_reported_because_it_is_the_useful_fact(self):
        self.assertIn("api_key=set", backend_env.describe(ANTHROPIC))
        self.assertIn("api_key=host login", backend_env.describe(KEYLESS))


class EquivalenceWithTheProxyTests(unittest.TestCase):
    """`deltas` must agree with the implementation it replaces.

    Written and run against the old `claude_proxy._backend_env` before anything
    was rewired, so it demonstrates the refactor preserves behaviour instead of
    asserting that the new code agrees with itself.

    The proxy copies the whole parent environment, so this compares against
    `deltas(...).apply_to(DIRTY)` with the same starting environment. Only the
    variables the rule governs are compared: the proxy also carries everything
    else in `os.environ`, which is not this function's business.
    """

    GOVERNED = (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    )

    def test_deltas_reproduces_the_shipped_behaviour(self):
        """Against the frozen table, so it keeps meaning after the rewire."""
        for label, backend in MATRIX:
            with self.subTest(case=label):
                mine = backend_env.deltas(backend).apply_to(DIRTY)
                for name, expected in GOLDEN[label].items():
                    self.assertEqual(
                        expected, mine.get(name),
                        f"{label}: {name} changed -- shipped behaviour was "
                        f"{expected!r}, deltas now says {mine.get(name)!r}",
                    )

    def test_the_proxy_still_produces_the_shipped_behaviour(self):
        """And that the proxy actually routes through it.

        Compared against the same frozen table rather than against `deltas`, so
        a proxy that quietly stopped delegating and kept its own copy would have
        to keep that copy correct forever to pass -- which is the drift this
        whole change removes.
        """
        for label, backend in MATRIX:
            with self.subTest(case=label):
                with patch.dict(os.environ, DIRTY, clear=True):
                    theirs = claude_proxy._backend_env(backend)
                for name, expected in GOLDEN[label].items():
                    self.assertEqual(
                        expected, theirs.get(name),
                        f"{label}: {name} changed in the proxy -- shipped "
                        f"behaviour was {expected!r}, got {theirs.get(name)!r}",
                    )

    def test_the_proxy_delegates_rather_than_duplicating(self):
        """Structural: the rule must not exist twice again.

        Without this, the two tests above pass just as happily against a proxy
        that kept a hand-maintained copy -- which is exactly the state this
        started in, and it passed its tests too.
        """
        import inspect
        body = inspect.getsource(claude_proxy._backend_env)
        self.assertIn("backend_env", body)
        for own in ('env.pop("ANTHROPIC_AUTH_TOKEN"',
                    'env["ANTHROPIC_API_KEY"]',
                    'env.pop("ANTHROPIC_BASE_URL"'):
            self.assertNotIn(
                own, body,
                f"the proxy still applies {own} itself, so the rule lives in "
                "two places again",
            )


if __name__ == "__main__":
    unittest.main()


class TheRunnerUsesTheSameRuleTests(unittest.TestCase):
    """`runner._build_env` is the direct path, and it had drifted.

    It builds from an allowlist rather than copying the environment, so it never
    needed the inheritance removals -- and precisely because it did not need
    them, its copy of the rule was maintained less carefully. Putting the matrix
    through both implementations found two divergences that had been shipping.
    """

    def _built(self, backend):
        with patch.dict(os.environ, DIRTY, clear=True):
            return runner._build_env(backend)

    def test_a_whitespace_key_is_no_longer_exported_as_the_key(self):
        """It tested truthiness, not content.

        `if backend.get("api_key")` is true for "  ", so a machine record with a
        blank-but-not-empty key exported ANTHROPIC_API_KEY="  " and the turn
        failed authentication, instead of falling back to the host login the way
        the proxy path did with the same record.
        """
        env = self._built({"provider": "anthropic", "base_url": "https://a.invalid",
                           "api_key": "  "})
        self.assertIsNone(env.get("ANTHROPIC_API_KEY"))
        self.assertIsNone(
            env.get("CLAUDE_CODE_SIMPLE"),
            "a keyless turn must also drop CLAUDE_CODE_SIMPLE, or the CLI will "
            "not read the host login it has just been told to fall back to",
        )

    def test_a_non_string_base_url_no_longer_takes_the_turn_down(self):
        """`normalise_base_url` guarded truthiness, then called .strip().

        An int is truthy, so it reached .strip() and raised AttributeError --
        the whole turn lost to a type error rather than falling back to the
        default endpoint. SQLite is dynamically typed: a TEXT column returns
        whatever was written into it.
        """
        env = self._built({"provider": "anthropic", "base_url": 123, "api_key": "k"})
        self.assertIsNone(env.get("ANTHROPIC_BASE_URL"))
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "k")
        self.assertIsNone(runner.normalise_base_url(123))
        self.assertIsNone(runner.normalise_base_url(None))

    def test_both_paths_now_agree_on_every_anthropic_case(self):
        """The property the shared rule exists to provide.

        Only the anthropic cases: for anything else the runner adds the local
        shim's OPENAI_* configuration, which the proxy has no concept of.
        """
        governed = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_API_KEY", "CLAUDE_CODE_SIMPLE")
        for label, backend in MATRIX:
            if not (isinstance(backend, dict)
                    and backend.get("provider") == "anthropic"):
                continue
            with self.subTest(case=label):
                mine = self._built(backend)
                with patch.dict(os.environ, DIRTY, clear=True):
                    theirs = claude_proxy._backend_env(backend)
                for name in governed:
                    self.assertEqual(
                        theirs.get(name), mine.get(name),
                        f"{label}: {name} -- proxy {theirs.get(name)!r} vs "
                        f"runner {mine.get(name)!r}",
                    )

    def test_the_runner_delegates_rather_than_duplicating(self):
        import inspect
        body = inspect.getsource(runner._build_env)
        self.assertIn("backend_env", body)
        self.assertNotIn('env["ANTHROPIC_API_KEY"]', body)

    def test_the_allowlist_and_shim_config_are_untouched(self):
        """Sharing the rule must not have taken the rest of the job with it."""
        env = self._built(None)
        self.assertEqual(env.get("PYTHONUNBUFFERED"), "1")
        self.assertIn("OPENAI_MODEL_NAME", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        # The allowlist still drops everything not named in it.
        self.assertNotIn("ANTHROPIC_API_KEY", env)
