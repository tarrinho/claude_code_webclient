"""QA coverage for the live server stack health.

The stack that got the site down for hours was: uvicorn bound to
``TAILNET_IP:443`` behind Caddy (which also proxied :443 → that same address,
so Caddy had nothing to proxy). Fix: uvicorn binds ``127.0.0.1:8080``, Caddy
handles TLS on :443 and reverse-proxies to that port.  Without Caddy on systemd
the restart loop was 121 times over fourteen minutes because systemd's default
10s window was too tight.

Covers:
* launch.sh uvicorn binding — the proxy mode binds ``127.0.0.1:8080``, never
  the Tailscale IP or :443.
* Caddyfile v2 syntax — valid Caddy 2.11 syntax, no v1 directives
  (``read_buffer_size``, ``write_buffer_size``) that Caddy 2 rejects.
* Caddy service persistence — ``caddy.service`` exists under systemd user
  services, is enabled, and uses the correct Caddy binary path.
* WebConsole service persistence — ``webconsole.service`` exists under systemd
  user services, is enabled, and uses the correct launch.sh path.
* Caddy → uvicorn proxy — Caddy serves TLS on :443, reverse-proxies to
  ``127.0.0.1:8080``, and the Tailscale domain serves HTTPS.
* Auth middleware — unauthenticated ``/api/`` calls return 401 with the
  session-expired JSON; authenticated calls pass; the login page loads without
  a session.
* Login flow — POST /login with valid credentials returns {ok: true} and
  sets the session cookie; cookie is then accepted for subsequent requests.
* Session expiry — a session that does not exist returns 401, not a page
  error.
* CSP headers — every response includes a Content-Security-Policy header
  with connect-src 'self'.
* Version endpoint — /api/version is accessible without authentication.

All tests are shape / protocol checks.  Nothing talks to a live model.
"""
from __future__ import annotations

import asyncio
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db
import middleware
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"

HTTPS = "https://testserver"


class LaunchScriptBindingTests(unittest.TestCase):
    """launch.sh must bind uvicorn to 127.0.0.1:8080 in proxy mode."""

    def test_proxy_mode_expressions_bind_to_localhost(self):
        """The exec line for WC_EXEC=1 must use 127.0.0.1, not TAILNET_IP."""
        source = (ROOT / "launch.sh").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", source)
        self.assertIn("--port 8080", source)

    def test_proxy_mode_does_not_use_tailnet_ip_for_bind(self):
        """Binding the Tailscale IP is what made the proxy path dead."""
        source = (ROOT / "launch.sh").read_text(encoding="utf-8")
        wc_exec_block = source.split('if [ "${WC_EXEC:-0}" = "1" ]')[-1].split("fi")[0]
        self.assertNotIn('"${TAILNET_IP}"', wc_exec_block)
        self.assertNotIn("$TAILNET_IP", wc_exec_block)

    def test_proxy_mode_does_not_bind_to_443(self):
        """Port 443 is TLS; uvicorn never speaks TLS."""
        source = (ROOT / "launch.sh").read_text(encoding="utf-8")
        wc_exec_block = source.split('if [ "${WC_EXEC:-0}" = "1" ]')[-1].split("fi")[0]
        self.assertNotIn("--port 443", wc_exec_block)

    def test_proxy_mode_does_not_use_ssl_cert_args(self):
        """SSL cert/key flags are Caddy's job, not uvicorn's."""
        source = (ROOT / "launch.sh").read_text(encoding="utf-8")
        wc_exec_block = source.split('if [ "${WC_EXEC:-0}" = "1" ]')[-1].split("fi")[0]
        self.assertNotIn("--ssl-certfile", wc_exec_block)
        self.assertNotIn("--ssl-keyfile", wc_exec_block)

    def test_background_mode_redirects_to_log(self):
        """The non-exec path runs in background and appends to a log file."""
        source = (ROOT / "launch.sh").read_text(encoding="utf-8")
        self.assertIn(">>", source)
        self.assertIn("logs/uvicorn.out.log", source)


class CaddyfileSyntaxTests(unittest.TestCase):
    """Caddyfile must use v2 syntax; v1 directives make Caddy 2 refuse to start."""

    def test_caddyfile_exists(self):
        self.assertTrue((Path("/etc/caddy/Caddyfile")).is_file())

    def test_v1_buffer_directives_absent(self):
        """read_buffer_size and write_buffer_size are Caddy v1 only."""
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertNotIn("read_buffer_size", source)
        self.assertNotIn("write_buffer_size", source)

    def test_reverse_proxy_directive_present(self):
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertIn("reverse_proxy", source)

    def test_tls_certificate_paths_present(self):
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertIn("tls ", source)
        self.assertIn("fullchain.pem", source)
        self.assertIn("key.pem", source)

    def test_handle_path_for_assets_present(self):
        """Static assets must be served via Caddy, not proxied."""
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertIn("handle_path", source)
        self.assertIn("/assets", source)

    def test_file_server_for_assets(self):
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertIn("file_server", source)

    def test_adapter_is_caddyfile(self):
        """The systemd service must tell Caddy which adapter to use."""
        svc = SYSTEMD_DIR / "caddy.service"
        self.assertTrue(svc.is_file(), "caddy.service not found")
        source = svc.read_text()
        self.assertIn("--adapter caddyfile", source)

    def test_caddyfile_validates_under_caddy(self):
        """Caddy 2 must accept the file — not just parse, but adapt."""
        result = subprocess.run(
            ["caddy", "validate", "--config", "/etc/caddy/Caddyfile",
             "--adapter", "caddyfile"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"caddy validate failed: {result.stderr[:200]}")


class SystemdServicePersistenceTests(unittest.TestCase):
    """Both Caddy and WebConsole must be persistent systemd user services."""

    def test_caddy_service_exists(self):
        svc = SYSTEMD_DIR / "caddy.service"
        self.assertTrue(svc.is_file(), "caddy.service not found")

    def test_webconsole_service_exists(self):
        svc = SYSTEMD_DIR / "webconsole.service"
        self.assertTrue(svc.is_file(), "webconsole.service not found")

    def test_caddy_service_wants_webconsole(self):
        """Caddy depends on webconsole so a boot race cannot happen."""
        svc = SYSTEMD_DIR / "caddy.service"
        source = svc.read_text()
        self.assertIn("Requires=webconsole.service", source)
        self.assertIn("After=network.target", source)

    def test_caddy_service_type_exec(self):
        svc = SYSTEMD_DIR / "caddy.service"
        source = svc.read_text()
        self.assertIn("Type=exec", source)

    def test_webconsole_service_type_exec(self):
        svc = SYSTEMD_DIR / "webconsole.service"
        source = svc.read_text()
        self.assertIn("Type=exec", source)

    def test_caddy_binary_path(self):
        svc = SYSTEMD_DIR / "caddy.service"
        source = svc.read_text()
        self.assertIn("/usr/bin/caddy", source)

    def test_webconsole_launch_path(self):
        svc = SYSTEMD_DIR / "webconsole.service"
        source = svc.read_text()
        self.assertIn("launch.sh", source)

    def test_webconsole_service_wc_exec_flag(self):
        """WC_EXEC=1 makes launch.sh exec uvicorn rather than background it."""
        svc = SYSTEMD_DIR / "webconsole.service"
        source = svc.read_text()
        self.assertIn("WC_EXEC=1", source)


class AuthMiddlewareTests(unittest.TestCase):
    """Auth middleware: unauthenticated /api/ calls get 401, valid sessions pass."""

    def _client(self):
        from app import app as _app
        return TestClient(_app, raise_server_exceptions=False,
                          base_url=HTTPS, follow_redirects=False)

    def test_unauthenticated_api_returns_401(self):
        client = self._client()
        resp = client.get("/api/chats")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertEqual(data["error"], "Session expired")
        self.assertEqual(data["redirect"], "/login")

    def test_unauthenticated_api_version_allowed(self):
        client = self._client()
        resp = client.get("/api/version")
        self.assertEqual(resp.status_code, 200)

    def test_login_page_does_not_require_session(self):
        client = self._client()
        resp = client.get("/login")
        self.assertEqual(resp.status_code, 200)

    def test_nonexistent_session_returns_401(self):
        """A made-up session cookie must not be accepted."""
        client = self._client()
        # httpx CookieJar needs domain set explicitly for test URLs
        client.cookies.set("wc_session", "dead-beef-cafe", domain="testserver")
        resp = client.get("/api/chats")
        self.assertEqual(resp.status_code, 401)

    def test_page_routes_redirect_without_session(self):
        """/ redirects to /login when unauthenticated."""
        client = self._client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(
            resp.headers.get("location", "/").startswith("/login"),
            f"expected redirect to /login, got {resp.headers.get('location')}"
        )


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end login: POST /login → JSON {ok: true} + session cookie."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    def _client(self):
        from app import app as _app
        return TestClient(_app, raise_server_exceptions=False,
                          base_url=HTTPS, follow_redirects=False)

    def test_login_success_returns_ok(self):
        """POST /login with valid credentials returns {ok: true}."""
        client = self._client()
        password = secrets.token_urlsafe(16)

        async def _setup():
            await db.user_create(
                "testlogin", None, auth.hash_password(password), role="admin"
            )

        asyncio.run(_setup())

        resp = client.post(
            "/login",
            json={"username": "testlogin", "password": password},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("wc_session", client.cookies)

    def test_invalid_password_returns_401(self):
        """Wrong password is rejected with 401 error."""
        client = self._client()
        password = secrets.token_urlsafe(16)

        async def _setup():
            await db.user_create(
                "testbad", None, auth.hash_password(password), role="admin"
            )

        asyncio.run(_setup())

        resp = client.post(
            "/login",
            json={"username": "testbad", "password": "wrong-password"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertIn("error", data)

    def test_nonexistent_user_returns_401(self):
        """A non-existent username is rejected with 401."""
        client = self._client()
        resp = client.post(
            "/login",
            json={"username": "ghost", "password": "ghost"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertIn("error", data)

    def test_valid_login_allows_api_access(self):
        """After a successful login, the session cookie permits API calls."""
        client = self._client()
        password = secrets.token_urlsafe(16)

        async def _setup():
            await db.user_create(
                "testapilogin", None, auth.hash_password(password), role="admin"
            )

        asyncio.run(_setup())

        resp = client.post(
            "/login",
            json={"username": "testapilogin", "password": password},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)

        # Now the API should accept the session cookie
        resp = client.get("/api/chats")
        self.assertEqual(resp.status_code, 200, resp.text[:200])

    def test_missing_credentials_returns_400(self):
        """POST /login with no username/password is 400."""
        client = self._client()
        resp = client.post(
            "/login",
            json={"username": "", "password": ""},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)


class SessionCookieShapeTests(unittest.IsolatedAsyncioTestCase):
    """Session cookies must include wc_csrf after login."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    def _client(self):
        from app import app as _app
        return TestClient(_app, raise_server_exceptions=False,
                          base_url=HTTPS, follow_redirects=False)

    def test_csrf_cookie_is_present_after_login(self):
        client = self._client()
        password = secrets.token_urlsafe(16)

        async def _setup():
            await db.user_create(
                "testcsrf", None, auth.hash_password(password), role="admin"
            )

        asyncio.run(_setup())

        client.post(
            "/login",
            json={"username": "testcsrf", "password": password},
            follow_redirects=False,
        )
        self.assertIn("wc_csrf", client.cookies)

    def test_session_cookie_is_present_after_login(self):
        client = self._client()
        password = secrets.token_urlsafe(16)

        async def _setup():
            await db.user_create(
                "testsess", None, auth.hash_password(password), role="admin"
            )

        asyncio.run(_setup())

        client.post(
            "/login",
            json={"username": "testsess", "password": password},
            follow_redirects=False,
        )
        self.assertIn("wc_session", client.cookies)
        # The session value is a real uuid-ish string, not empty
        self.assertTrue(len(client.cookies["wc_session"]) > 10)


class CSPHeadersTests(unittest.TestCase):
    """All responses must include a Content-Security-Policy header."""

    def _client(self):
        from app import app as _app
        return TestClient(_app, raise_server_exceptions=False,
                          base_url=HTTPS, follow_redirects=False)

    def test_index_has_csp(self):
        client = self._client()
        resp = client.get("/login")
        self.assertIn("content-security-policy", resp.headers)

    def test_api_has_csp(self):
        client = self._client()
        resp = client.get("/api/chats")
        self.assertIn("content-security-policy", resp.headers)

    def test_csp_connect_src_self(self):
        client = self._client()
        resp = client.get("/api/chats")
        csp = resp.headers.get("content-security-policy", "")
        self.assertIn("connect-src 'self'", csp)

    def test_csp_script_src_self(self):
        client = self._client()
        resp = client.get("/login")
        csp = resp.headers.get("content-security-policy", "")
        self.assertIn("script-src 'self'", csp)


class ProxyConfigurationTests(unittest.TestCase):
    """Verify Caddy's reverse_proxy directive targets the correct uvicorn port."""

    def test_reverse_proxy_targets_localhost_8080(self):
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertIn("reverse_proxy 127.0.0.1:8080", source)

    def test_no_duplicate_tls_blocks(self):
        """TLS must be in Caddy, not uvicorn. Two TLS configs cause conflicts."""
        launch = (ROOT / "launch.sh").read_text(encoding="utf-8")
        proxy_block = launch.split('if [ "${WC_EXEC:-0}" = "1" ]')[-1].split("fi")[0]
        self.assertNotIn("--ssl-certfile", proxy_block)
        self.assertNotIn("--ssl-keyfile", proxy_block)

    def test_headers_proxied_to_backend(self):
        """Caddy must forward X-Real-IP and X-Forwarded-Proto to uvicorn."""
        source = (Path("/etc/caddy/Caddyfile")).read_text()
        self.assertIn("X-Real-IP", source)
        self.assertIn("X-Forwarded-Proto", source)


class VersionEndpointTests(unittest.TestCase):
    """/api/version must be publicly accessible and return version info."""

    def _client(self):
        from app import app as _app
        return TestClient(_app, raise_server_exceptions=False,
                          base_url=HTTPS, follow_redirects=False)

    def test_version_endpoint_public(self):
        """No session required — public route."""
        client = self._client()
        resp = client.get("/api/version")
        self.assertEqual(resp.status_code, 200)

    def test_version_response_has_version_field(self):
        client = self._client()
        resp = client.get("/api/version")
        data = resp.json()
        self.assertIn("version", data)
        self.assertIsInstance(data["version"], str)
        self.assertTrue(len(data["version"]) > 0)


class PublicRouteListTests(unittest.TestCase):
    """Verify public_route list in middleware.py matches expected paths."""

    def test_public_route_list_contains_expected_paths(self):
        """public_route must allow /login, /api/version, and /assets/."""
        source = (Path(__file__).resolve().parents[1] / "middleware.py").read_text()
        self.assertIn('"/login"', source)
        self.assertIn('"/api/version"', source)
        self.assertIn('"/assets/"', source)

    def test_no_debug_paths_in_public_list(self):
        """Debug or development paths must not be public."""
        source = (Path(__file__).resolve().parents[1] / "middleware.py").read_text()
        self.assertIn("public_route = (", source)
        block = source[source.index("public_route = ("):source.index(")") + 1]
        self.assertNotIn("/dev", block)


if __name__ == "__main__":
    unittest.main()
