"""QA: transport behaviour that must hold for ANY transport, not one host.

Everything verified by hand this week was verified against a named machine --
Kali3, then Kali_MAc, then AppSec Tools, then Node1-Appsec -- and each one
found a different defect precisely because it differed from the last:

  Kali3          claude at /usr/bin/claude          (unit hardcoded ~/.local/bin)
  Node1-Appsec   claude at ~/.local/bin/claude      (a third distinct path)
  Kali_MAc       claude present, OAuth expired      (path fine, credential not)
  AppSec Tools   worked once the proxy restarted

A test tied to any one of those would have passed while the others were broken.
These cases are written against the properties instead, so a new transport with
a fourth layout is covered on the day it is added.

Nothing here reaches the network. The readiness parser is pure, and the check
verdict is exercised through its own dataclass.
"""
from __future__ import annotations

import unittest

import transport_readiness


class ReadinessParsingQA(unittest.TestCase):
    """`parse_probe` turns the remote's key=value lines into the four answers.

    Pure by design, so the table of remote states needs no SSH and no host --
    which is what makes it possible to cover layouts no transport here has yet.
    """

    PORT = 9000
    TOKEN = "t" * 43

    def _checks(self, raw, token=None):
        return {c.name: c for c in transport_readiness.parse_probe(
            raw, port=self.PORT, local_token=token or self.TOKEN)}

    def test_a_claude_anywhere_on_the_host_counts(self):
        """The defect that cost the most this week: the deploy hardcoded this
        console's own layout onto every transport. Three real transports keep
        claude in three different places, so the check must not care which."""
        for path in ("/usr/bin/claude",
                     "/home/kali-pt/.local/bin/claude",
                     "/opt/claude/bin/claude"):
            with self.subTest(path=path):
                checks = self._checks(f"claude={path}\n")
                claude = next((c for n, c in checks.items()
                               if "claude" in n.lower()), None)
                self.assertIsNotNone(claude, f"no claude check for {path}")
                self.assertTrue(
                    claude.ok,
                    f"{path} reported not-ok; the check is reading the path "
                    "rather than whether a binary was found")

    def test_a_missing_claude_is_reported_with_a_remedy(self):
        checks = self._checks("claude=\n")
        claude = next((c for n, c in checks.items() if "claude" in n.lower()), None)
        self.assertIsNotNone(claude)
        self.assertFalse(claude.ok)
        self.assertTrue(
            (claude.remedy or "").strip(),
            "a failing check with no remedy tells an operator something is "
            "wrong and nothing about what to do")

    def test_every_failing_check_carries_a_detail(self):
        """A red line with no detail is the shape that sent an operator to the
        tunnel when the problem was a binary, and to the binary when the
        problem was a token."""
        raw = "claude=\npython=\nlistening=0\ntoken_sha=deadbeef\n"
        for name, check in self._checks(raw).items():
            if not check.ok:
                with self.subTest(check=name):
                    self.assertTrue(
                        (check.detail or "").strip(),
                        f"{name} failed with an empty detail")

    def test_a_token_mismatch_is_named_as_one(self):
        """Kali3's proxy once ran for days on a stale token while the database
        held another, and every turn died at the handshake with nothing useful
        logged. The check exists so that is visible before a turn is sent."""
        raw = ("claude=/usr/bin/claude\npython=Python 3.13.0\n"
               "listening=1\ntoken_sha=" + "0" * 16 + "\n")
        checks = self._checks(raw)
        tok = next((c for n, c in checks.items() if "token" in n.lower()), None)
        self.assertIsNotNone(tok, "no token check in the probe output")
        self.assertFalse(tok.ok, "a mismatched token digest read as ok")

    def test_a_healthy_host_passes_every_check(self):
        """The control. Without it, a parser that failed everything would
        satisfy every assertion above while calling every transport broken."""
        import hashlib
        digest = hashlib.sha256(self.TOKEN.encode()).hexdigest()[:16]
        raw = (f"claude=/usr/bin/claude\npython=Python 3.13.0\n"
               f"listening=1\ntoken_sha={digest}\n"
               f"proxy_cwd=/home/agent/wc-proxy\n"
               f"expected_path=/home/agent/wc-proxy\n")
        checks = self._checks(raw)
        failed = [n for n, c in checks.items() if not c.ok]
        self.assertEqual(
            failed, [],
            f"a fully healthy host reported failures: {failed}")


class ReadinessVerdictQA(unittest.TestCase):
    """The verdict an operator actually sees."""

    def test_an_unreachable_host_still_carries_an_error(self):
        """`reachable=False` with an empty error is the case the UI renders as
        'unreachable: SSH failed' -- true but useless. The dataclass must
        preserve whatever the probe managed to say."""
        result = transport_readiness.Readiness(
            reachable=False, checks=[], error="Permission denied (publickey)")
        payload = result.as_dict()
        self.assertFalse(payload["reachable"])
        self.assertIn("publickey", payload["error"])

    def test_not_ready_is_not_the_same_as_unreachable(self):
        """Kali3 on 2026-09-25 logged `ready=False reachable=True` -- SSH fine,
        something else wrong. Collapsing the two would send an operator to the
        network for a missing binary."""
        result = transport_readiness.Readiness(
            reachable=True,
            checks=[transport_readiness.Check(
                name="claude", ok=False, detail="not found", remedy="install it")],
            error=None)
        payload = result.as_dict()
        self.assertTrue(payload["reachable"])
        self.assertFalse(payload["ready"])
        self.assertEqual(len(payload["checks"]), 1)
        self.assertEqual(payload["checks"][0]["remedy"], "install it")


if __name__ == "__main__":
    unittest.main()
