"""The CSP violation sink: reachable without a session, and safe because of it.

Why it must be public: a browser posts a CSP report with credentials omitted,
so an authenticated endpoint would receive nothing and the report-only rollout
would look clean while reporting nothing. That is the failure this whole spec
is about, so it is worth stating rather than discovering.

Adding a path to the auth-exempt set is a security review of the handler, not a
list edit -- tests/test_qa_api_tokens.py says so, and names the incident that
taught it: /api/hard-refresh was exempted, the guard counts were updated as
asked, and the handler passed an unvalidated `to` into a redirect. So these
cases are mostly about what the handler does with input once anyone on the
network can reach it.

The handler's whole contract: accept a bounded body, keep four fields,
truncate them, strip anything that could forge a log line, write one log
record, return 204 with no body. It reads nothing else, stores nothing, and
echoes nothing back.

Spec: docs/superpowers/specs/2026-09-25-high-assurance-development-design.md
      section 4.4, rollout step 1
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

HTTPS = "https://testserver"
PATH = "/api/csp-report"

REPORT = {
    "csp-report": {
        "document-uri": "https://console.example/index.html",
        "violated-directive": "script-src 'self'",
        "blocked-uri": "inline",
        "line-number": 42,
    }
}


def _client():
    from app import app as _app
    return TestClient(_app, raise_server_exceptions=False,
                      base_url=HTTPS, follow_redirects=False)


class ReachabilityTests(unittest.TestCase):

    def test_a_report_is_accepted_without_a_session(self):
        resp = _client().post(PATH, json=REPORT)
        self.assertEqual(resp.status_code, 204)

    def test_nothing_is_returned_to_the_caller(self):
        """204 and an empty body. A sink that answers is a sink that can be
        used to probe."""
        resp = _client().post(PATH, json=REPORT)
        self.assertEqual(resp.content, b"")

    def test_get_is_not_allowed(self):
        self.assertIn(_client().get(PATH).status_code, (404, 405))


class AbuseTests(unittest.TestCase):
    """Anyone on the network can reach this. None of it should matter."""

    def test_a_malformed_body_is_dropped_quietly(self):
        resp = _client().post(PATH, content=b"{not json",
                              headers={"content-type": "application/csp-report"})
        self.assertEqual(resp.status_code, 204)

    def test_an_empty_body_is_dropped_quietly(self):
        resp = _client().post(PATH, content=b"",
                              headers={"content-type": "application/csp-report"})
        self.assertEqual(resp.status_code, 204)

    def test_an_oversized_body_is_refused_rather_than_read(self):
        """Unauthenticated and unbounded is a memory pedal anyone can press."""
        resp = _client().post(PATH, content=b"x" * 200_000,
                              headers={"content-type": "application/csp-report"})
        self.assertEqual(resp.status_code, 413)

    def test_a_report_of_the_wrong_shape_is_dropped_quietly(self):
        resp = _client().post(PATH, json={"unexpected": ["shape"]})
        self.assertEqual(resp.status_code, 204)


class LogSafetyTests(unittest.TestCase):
    """The one thing this handler does with attacker input is log it."""

    def _log_once(self, report: dict) -> str:
        import logging
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        log = logging.getLogger("wc.csp_report")
        handler = _Capture()
        log.addHandler(handler)
        try:
            _client().post(PATH, json=report)
        finally:
            log.removeHandler(handler)
        self.assertTrue(records, "the violation was not logged at all")
        return records[-1].getMessage()

    def test_a_newline_cannot_forge_a_second_log_line(self):
        forged = dict(REPORT)
        forged["csp-report"] = dict(REPORT["csp-report"])
        forged["csp-report"]["blocked-uri"] = (
            "evil\nWARNING wc.auth: login succeeded for admin")
        message = self._log_once(forged)
        self.assertNotIn("\n", message,
                         "a report can inject a line into the log")
        self.assertNotIn("\r", message)

    def test_a_very_long_field_is_truncated(self):
        long = dict(REPORT)
        long["csp-report"] = dict(REPORT["csp-report"])
        long["csp-report"]["document-uri"] = "https://x/" + ("a" * 5_000)
        message = self._log_once(long)
        self.assertLess(len(message), 1_200,
                        "an unbounded field reaches the log verbatim")


if __name__ == "__main__":
    unittest.main()
