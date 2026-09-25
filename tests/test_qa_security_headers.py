"""The security headers, asserted against real responses.

Every case here drives the actual app through TestClient and reads the header
off the response. None of them greps middleware.py, and that is deliberate: a
test that matches the source text of a header passes whether or not the header
is ever sent, which is the defect class the high-assurance spec exists for.

Spec: docs/superpowers/specs/2026-09-25-high-assurance-development-design.md
  section 4.1 -- object-src 'none'
  section 6   -- Fetch Metadata, COOP, CORP
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

HTTPS = "https://testserver"


def _client():
    from app import app as _app
    return TestClient(_app, raise_server_exceptions=False,
                      base_url=HTTPS, follow_redirects=False)


class ObjectSrcTests(unittest.TestCase):
    """`default-src 'self'` covers object-src by fallback, but 'self' is not
    'none': a same-origin upload or reflected path can still be embedded as a
    plugin document. This application embeds no plugin content, so the
    directive costs nothing."""

    def test_csp_forbids_plugin_content_outright(self):
        resp = _client().get("/login")
        csp = resp.headers.get("content-security-policy", "")
        self.assertIn("object-src 'none'", csp,
                      f"object-src is not locked down: {csp}")


class ReportSinkTests(unittest.TestCase):
    """The policy must name the sink, or the sink records nothing and a
    report-only rollout reads as clean because it is deaf."""

    def test_the_policy_points_at_the_report_endpoint(self):
        resp = _client().get("/login")
        csp = resp.headers.get("content-security-policy", "")
        self.assertIn("report-uri /api/csp-report", csp,
                      f"violations have nowhere to go: {csp}")


class CrossOriginIsolationTests(unittest.TestCase):
    """COOP and CORP. The console opens no cross-origin popups and its
    responses are not meant to be embedded anywhere else."""

    def test_opener_policy_isolates_the_window(self):
        resp = _client().get("/login")
        self.assertEqual(
            resp.headers.get("cross-origin-opener-policy"), "same-origin")

    def test_resource_policy_refuses_foreign_embedding(self):
        resp = _client().get("/login")
        self.assertEqual(
            resp.headers.get("cross-origin-resource-policy"), "same-origin")


class FetchMetadataTests(unittest.TestCase):
    """Refuse a cross-site state-changing request at the edge.

    The rule treats an ABSENT Sec-Fetch-Site as allowed and refuses only an
    explicit `cross-site`. That is not laziness: the remote QA node, the
    proxy and every curl-based tool send no Sec-Fetch-* headers at all, so a
    rule requiring the header would take out the remote execution path on its
    first deploy. The trade is that a non-browser client can always opt out --
    correct here, where browser-originated CSRF is the threat and the API
    token guards the rest.
    """

    def test_a_cross_site_post_is_refused(self):
        resp = _client().post(
            "/login", json={"password": "wrong"},
            headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(resp.status_code, 403)

    def test_a_same_origin_post_is_not_refused_by_this_rule(self):
        resp = _client().post(
            "/login", json={"password": "wrong"},
            headers={"Sec-Fetch-Site": "same-origin"})
        self.assertNotEqual(resp.status_code, 403)

    def test_a_request_with_no_fetch_metadata_is_allowed(self):
        """The remote QA node and the proxy send none. This is the case that
        breaks the deployment if the rule is written the obvious way."""
        resp = _client().post("/login", json={"password": "wrong"})
        self.assertNotEqual(resp.status_code, 403)

    def test_a_cross_site_GET_is_not_refused(self):
        """Only state-changing methods are gated. A cross-site GET of a page
        is what a link is, and refusing it breaks navigation without
        defending anything the token does not already cover."""
        resp = _client().get("/login", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertNotEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
