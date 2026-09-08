"""QA: the transport readiness check, and the two things it must not do.

Design: agreed in chat 2026-09-08. Adding a transport creates nothing on the
far side; turns need claude_proxy.py listening on config.PROXY_PORT with this
database's token, plus the claude CLI and python3 for it to use. Their absence
surfaces as "Cannot connect to proxy at 127.0.0.1:<port>" on every turn, which
reads as a local fault and is not one.

`parse_probe` is pure, so the table below is a set of remote states rather
than a mock of SSH.

Two cases exist because the first implementation got them wrong:

* **the probe template renders.** It was written with %(port)d, and the script
  is full of `printf '%s'` -- %-formatting raised "not enough arguments for
  format string" on every single call. Nothing else in the file would have
  caught it, because the template is only interpolated on the SSH path.
* **the CLI is resolved by path before PATH.** `command -v claude` reported
  MISSING on a host where the CLI was installed and working: a non-interactive
  SSH shell has no ~/.local/bin on PATH, and the proxy's unit does not use
  PATH anyway (it sets WC_CLAUDE_PATH). A check that cries wolf on a healthy
  host is one that gets switched off.
"""
from __future__ import annotations

import unittest

import transport_readiness as tr

_TOKEN = "T" * 43


def _sha16(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _raw(**over) -> str:
    fields = {
        "claude": "/home/u/.local/bin/claude",
        "python": "Python 3.13.11",
        "listening": "1",
        "token_len": "43",
        "token_sha": _sha16(_TOKEN),
        "service": "active",
    }
    fields.update(over)
    return "\n".join(f"{k}={v}" for k, v in fields.items())


def _by_name(raw: str) -> dict[str, tr.Check]:
    return {c.name: c for c in tr.parse_probe(raw, port=9000, local_token=_TOKEN)}


class ProbeTemplateTests(unittest.TestCase):
    def test_the_probe_renders_with_the_port_substituted(self):
        """The regression: %-formatting against a script full of printf '%s'."""
        script = tr.probe_script(9000)
        self.assertIn("127.0.0.1:9000", script)
        self.assertNotIn(tr._PORT_MARKER, script)

    def test_the_port_is_substituted_wherever_it_is_configured(self):
        self.assertIn("127.0.0.1:9002", tr.probe_script(9002))

    def test_the_probe_only_reads(self):
        """Init writes; check must not. Asserted because the probe is a shell
        script, where adding a write is one careless line."""
        script = tr.probe_script(9000)
        for writer in ("scp ", "mkdir", "systemctl --user start",
                       "systemctl --user enable", "> ~/", "rm ", "install "):
            self.assertNotIn(writer, script, f"the probe must not {writer!r}")


class ReadyHostTests(unittest.TestCase):
    def test_a_fully_deployed_host_passes_every_check(self):
        checks = tr.parse_probe(_raw(), port=9000, local_token=_TOKEN)
        self.assertTrue(all(c.ok for c in checks), [c.detail for c in checks])

    def test_the_cli_is_accepted_when_found_by_path(self):
        """A non-interactive SSH shell has no ~/.local/bin on PATH, so path
        resolution is the only answer that matches how the proxy launches."""
        checks = _by_name(_raw(claude="/home/u/.local/bin/claude"))
        self.assertTrue(checks["claude CLI"].ok)
        self.assertIn(".local/bin/claude", checks["claude CLI"].detail)


class MissingPiecesTests(unittest.TestCase):
    def test_todays_real_pre_deploy_state(self):
        """Measured on pentester before deploying: CLI and python3 present,
        no proxy, no token."""
        checks = _by_name(_raw(claude="MISSING", listening="0",
                               token_sha="none", service="absent"))
        self.assertFalse(checks["claude CLI"].ok)
        self.assertTrue(checks["python3"].ok)
        self.assertFalse(checks["proxy on 127.0.0.1:9000"].ok)
        self.assertFalse(checks["proxy token matches"].ok)

    def test_a_missing_proxy_names_the_service_state(self):
        """"nothing listening" alone does not say whether it crashed or was
        never installed."""
        checks = _by_name(_raw(listening="0", service="failed"))
        self.assertIn("failed", checks["proxy on 127.0.0.1:9000"].detail)

    def test_a_stale_token_is_caught_even_with_the_proxy_up(self):
        """The Kali3 failure: a proxy running happily on a token the database
        no longer holds, so every turn dies at the handshake."""
        checks = _by_name(_raw(token_sha=_sha16("a-different-token")))
        self.assertFalse(checks["proxy token matches"].ok)
        self.assertTrue(checks["proxy on 127.0.0.1:9000"].ok)

    def test_every_failing_check_offers_a_remedy(self):
        checks = tr.parse_probe(
            _raw(claude="MISSING", python="MISSING", listening="0",
                 token_sha="none"),
            port=9000, local_token=_TOKEN,
        )
        for c in checks:
            if not c.ok:
                self.assertTrue(c.remedy, f"{c.name} fails with no remedy")

    def test_no_local_token_does_not_read_as_a_match(self):
        """An empty local token must never satisfy the comparison -- otherwise
        a console with no token configured reports every host as ready."""
        checks = {c.name: c for c in tr.parse_probe(
            _raw(token_sha="none"), port=9000, local_token="")}
        self.assertFalse(checks["proxy token matches"].ok)


class ReadinessShapeTests(unittest.TestCase):
    def test_unreachable_is_not_ready_even_with_no_failing_checks(self):
        """Kali3 is offline: no checks ran at all, which must not read as
        'nothing failed, therefore ready'."""
        r = tr.Readiness(reachable=False, error="Connection timed out")
        self.assertEqual(r.checks, [])
        self.assertFalse(r.ready)

    def test_the_payload_carries_each_check_separately(self):
        r = tr.Readiness(reachable=True,
                         checks=tr.parse_probe(_raw(), port=9000,
                                               local_token=_TOKEN))
        payload = r.as_dict()
        self.assertTrue(payload["ready"])
        self.assertEqual(len(payload["checks"]), 4)
        self.assertEqual(
            {"name", "ok", "detail", "remedy"}, set(payload["checks"][0]))

    def test_the_token_value_never_appears_in_the_payload(self):
        r = tr.Readiness(reachable=True,
                         checks=tr.parse_probe(_raw(), port=9000,
                                               local_token=_TOKEN))
        self.assertNotIn(_TOKEN, str(r.as_dict()))


if __name__ == "__main__":
    unittest.main()
