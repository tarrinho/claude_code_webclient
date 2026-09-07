"""QA: switching a machine back to Anthropic must not keep using the gateway.

The proxy builds a child environment from ``dict(os.environ)`` -- it has to, or
the CLI loses PATH, HOME and the host's own credentials. The cost is that any
Anthropic variable in the shell that launched the proxy is inherited by every
turn unless something removes it.

Two survived:

``ANTHROPIC_BASE_URL`` was set when a machine supplied one and simply left alone
when it did not. A machine with no base_url means "talk to the official API", so
clearing a gateway address in the UI produced a child still pointed at the
gateway, with nothing on screen saying so. The workaround was to
``unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN`` before starting the proxy --
a fix that must be remembered every time and is silent when forgotten.

``ANTHROPIC_AUTH_TOKEN`` was worse. No machine record supplies one, so nothing
ever set it deliberately, and the CLI prefers it over ``ANTHROPIC_API_KEY``. An
inherited token therefore outranked the key the backend had just been given.

The direct runner never had either problem: ``runner._build_env`` starts from an
allowlist, so nothing is inherited. That asymmetry is why this went unnoticed --
the path with the bug is the one that is on by default.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import claude_proxy
import runner

# Values that stand in for a shell that had been pointed at a gateway.
POLLUTED = {
    "ANTHROPIC_BASE_URL": "https://gateway.invalid",
    "ANTHROPIC_AUTH_TOKEN": "inherited-token-value",
}

DIRECT = {"provider": "claude_code", "base_url": "", "api_key": "key-from-record"}
GATEWAY = {"provider": "claude_code", "base_url": "https://machine.invalid",
           "api_key": "key-from-record"}


class ProxyEnvTests(unittest.TestCase):
    """_backend_env is the path that copies the whole environment."""

    def _env(self, backend, extra=None):
        with mock.patch.dict(os.environ, {**POLLUTED, **(extra or {})}, clear=False):
            return claude_proxy._backend_env(backend)

    def test_a_machine_with_no_base_url_drops_an_inherited_one(self):
        """The reported symptom: back to Anthropic, still hitting the gateway."""
        env = self._env(DIRECT)
        self.assertNotIn(
            "ANTHROPIC_BASE_URL", env,
            "the turn still points at whatever the proxy's shell was set to",
        )

    def test_an_inherited_auth_token_never_survives(self):
        """It outranks ANTHROPIC_API_KEY, so it decides the credentials."""
        for name, backend in (("direct", DIRECT), ("gateway", GATEWAY)):
            with self.subTest(backend=name):
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", self._env(backend))

    def test_a_machine_with_a_base_url_still_wins(self):
        """The fix must not stop a gateway machine reaching its gateway."""
        env = self._env(GATEWAY)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://machine.invalid")

    def test_the_machines_key_is_applied(self):
        self.assertEqual(self._env(DIRECT)["ANTHROPIC_API_KEY"], "key-from-record")

    def test_a_non_anthropic_backend_is_still_stripped(self):
        """The pre-existing guarantee, kept."""
        env = self._env({"provider": "claude_code"})
        for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            self.assertNotIn(var, env)

    def test_the_child_still_gets_what_it_needs_to_start(self):
        """Stripping must not turn into building from scratch.

        The CLI cannot launch without PATH and HOME, which is the reason this
        function copies the environment rather than allowlisting like the runner.
        """
        env = self._env(DIRECT)
        self.assertIn("PATH", env)
        self.assertIn("HOME", env)


class DirectRunnerTests(unittest.TestCase):
    """The allowlisted path, asserted so the asymmetry cannot quietly reverse."""

    def test_nothing_is_inherited(self):
        with mock.patch.dict(os.environ, POLLUTED, clear=False):
            env = runner._build_env({"provider": "claude_code", "base_url": "",
                                     "api_key": "key-from-record"})
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_it_still_builds_from_an_allowlist(self):
        """If this becomes a copy of os.environ, it inherits the same bug."""
        source = (__import__("pathlib").Path(runner.__file__)).read_text()
        self.assertIn('safe = {', source)
        self.assertNotIn("env = dict(os.environ)", source)


if __name__ == "__main__":
    unittest.main()
