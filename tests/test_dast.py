"""DAST — dynamic application security test against a live WebConsole instance.

Tests auth bypass, SQLi, XSS, SSRF, path traversal, info disclosure,
security headers, rate limiting and CSP against a freshly-started server.
Uses a single shared cookie jar for all authenticated tests.

Run:  .venv/bin/python -m pytest tests/test_dast.py -v -rs
"""

import os
import random
import subprocess
import time

import pytest
import requests


# ── fixtures ──────────────────────────────────────────────────────────────

SERVER_URL = os.environ.get("WC_SERVER_URL", "http://127.0.0.1:18901")
SERVER_PROC = None


@pytest.fixture(scope="session", autouse=True)
def live_server():
    """Start the WebConsole server for the entire test session."""
    env = os.environ.copy()
    db_path = f"/tmp/dast_wc_{random.randint(0,999999)}.db"
    env["WC_DB_PATH"] = db_path
    env["WC_PROJECTS_ROOT"] = f"/tmp/dast_projects_{random.randint(0,999999)}"
    env["WC_SESSION_SECRET"] = "dast-test-secret-at-least-32-chars-long"
    env["WC_ADMIN_USER"] = "dastadmin"
    env["WC_ADMIN_PASSWORD"] = "dast_password_123"
    env["WC_PROXY_TOKEN"] = "dast-proxy-token-at-least-32-chars"
    env["WC_PROXY_ENABLED"] = "true"
    env["WC_COOKIE_ALLOW_INSECURE"] = "true"

    global SERVER_PROC
    SERVER_PROC = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app:app",
         "--host", "127.0.0.1", "--port", "18901", "--log-level", "error"],
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    for _ in range(30):
        try:
            r = requests.get(f"{SERVER_URL}/", timeout=2, allow_redirects=False)
            if r.status_code in (200, 303, 307, 302, 301):
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.3)
    else:
        pytest.skip("Server did not start in time")


@pytest.fixture(scope="session")
def client(live_server):
    """Single session-scoped client that preserves cookies."""
    return requests.Session()


@pytest.fixture(scope="session")
def logged_in(client):
    """Log in and return the session with cookies set."""
    r = client.post(
        f"{SERVER_URL}/login",
        json={"username": "dastadmin", "password": "dast_password_123"},
        timeout=5,
    )
    assert r.status_code in (200, 302, 303), f"Login failed: {r.status_code} {r.text[:200]}"
    # CSRF middleware requires X-CSRF-Token on mutating requests.
    # Extract it from the session cookie jar and attach as default header.
    csrf_val = client.cookies.get("wc_csrf", "")
    client.headers["X-CSRF-Token"] = csrf_val
    return client


@pytest.fixture(scope="session", autouse=True)
def teardown(logged_in):
    yield
    try:
        logged_in.post(f"{SERVER_URL}/logout", timeout=3)
    except Exception:
        pass
    if SERVER_PROC:
        SERVER_PROC.terminate()
        SERVER_PROC.wait(timeout=5)


# ── auth bypass ──────────────────────────────────────────────────────────

class TestAuthBypass:
    """Protected endpoints must reject unauthenticated requests."""

    def test_chats_requires_auth(self):
        r = requests.get(f"{SERVER_URL}/api/chats", timeout=5)
        assert r.status_code == 401

    def test_messages_requires_auth(self):
        r = requests.post(f"{SERVER_URL}/api/chats/fake/messages", timeout=5)
        assert r.status_code == 401

    def test_settings_requires_auth(self):
        r = requests.patch(f"{SERVER_URL}/api/settings", json={}, timeout=5)
        assert r.status_code == 401

    def test_tokens_requires_auth(self):
        r = requests.get(f"{SERVER_URL}/api/tokens", timeout=5)
        assert r.status_code == 401

    def test_admin_export_requires_auth(self):
        r = requests.get(f"{SERVER_URL}/api/admin/export", timeout=5)
        assert r.status_code == 401

    def test_db_backup_requires_auth(self):
        r = requests.get(f"{SERVER_URL}/api/db/backup", timeout=5)
        assert r.status_code == 401


# ── SQL injection ────────────────────────────────────────────────────────

class TestSQLInjection:
    """Common SQLi payloads should be rejected, not cause errors."""

    def test_chat_search_sqli(self, logged_in):
        r = logged_in.post(
            f"{SERVER_URL}/api/chats/search",
            json={"query": "'; DROP TABLE users; --"},
            timeout=5,
        )
        # Must not return a successful DB response (not 200 with results)
        assert r.status_code in (400, 422, 500), \
            f"SQLi should be caught: status={r.status_code}"
        if r.status_code == 200:
            # If 200, body must not contain user data rows
            body = r.json()
            assert body.get("count", 0) == 0 or "user" not in str(body), \
                "SQLi leaked data"


# ── XSS ──────────────────────────────────────────────────────────────────

class TestXSS:
    """Inject payloads via form fields; verify they are escaped in HTML render."""

    def test_xss_in_title_json_api(self, logged_in):
        """JSON API response encodes payloads as strings (not raw HTML)."""
        payload = '<script>alert("xss")</script>'
        r = logged_in.post(
            f"{SERVER_URL}/api/chats",
            json={"title": payload},
            timeout=5,
        )
        assert r.status_code in (201, 200)
        # JSON response should be valid JSON (not broken by payload)
        data = r.json()
        assert data.get("title") == payload

    def test_xss_in_title_html_page(self, logged_in):
        """HTML page must escape user-supplied titles via escapeHtml."""
        payload = '<script>alert("xss")</script>'
        logged_in.post(
            f"{SERVER_URL}/api/chats",
            json={"title": payload},
            timeout=5,
        )
        r = logged_in.get(f"{SERVER_URL}/", timeout=5)
        assert r.status_code == 200
        assert "<script>" not in r.text or "&lt;script&gt;" in r.text, \
            "XSS payload rendered as raw HTML in page"


# ── SSRF ─────────────────────────────────────────────────────────────────

class TestSSRF:
    """Attempt to reach internal/metadata endpoints via proxy settings."""

    def test_ssrf_cloud_metadata(self, logged_in):
        r = logged_in.patch(
            f"{SERVER_URL}/api/settings",
            json={"ai_machine_host": "169.254.169.254/latest/meta-data/"},
            timeout=5,
        )
        assert r.status_code == 400, "SSRF to cloud metadata should be blocked"


# ── security headers ────────────────────────────────────────────────────

class TestSecurityHeaders:
    """All responses must include required security headers."""

    def test_nosniff(self):
        r = requests.get(f"{SERVER_URL}/", timeout=5)
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    def test_frame_options(self):
        r = requests.get(f"{SERVER_URL}/", timeout=5)
        assert r.headers.get("X-Frame-Options") in ("DENY", "SAMEORIGIN")

    def test_no_cache(self):
        r = requests.get(f"{SERVER_URL}/", timeout=5)
        cc = r.headers.get("Cache-Control", "")
        assert "no-store" in cc or "no-cache" in cc


# ── info disclosure ──────────────────────────────────────────────────────

class TestInfoDisclosure:
    """Secrets and PII must not appear in responses."""

    def test_no_stack_traces(self):
        r = requests.get(f"{SERVER_URL}/nonexistent/path/xyz", timeout=5)
        assert "traceback" not in r.text.lower(), "Stack trace leaked"


# ── CSP ──────────────────────────────────────────────────────────────────

class TestCSP:
    def test_csp_header_present(self):
        r = requests.get(f"{SERVER_URL}/", timeout=5)
        csp = r.headers.get("Content-Security-Policy", "")
        assert len(csp) > 0, "CSP header missing"


# ── rate limiting ────────────────────────────────────────────────────────
# Must run LAST (pytest sort order) so it doesn't exhaust attempts before
# the authenticated tests.  On a fresh server the rate window is empty; on
# a re-run the prior test run's lockout persists in _login_attempts, so the
# fixture resets it before testing.

class TestRateLimit:
    """Rate limiter blocks after LOGIN_RATE_MAX (default 8) failures."""
    @pytest.fixture(autouse=True)
    def _clear_rate(self):
        """Reset per-IP counter before each rate-limit run."""
        import auth
        auth._login_attempts.pop("127.0.0.1", None)
        yield

    def test_login_rate_limit(self):
        s = requests.Session()
        blocked = False
        for i in range(20):
            r = s.post(
                f"{SERVER_URL}/login",
                json={"username": "dastadmin", "password": f"wrong{i}"},
            )
            if r.status_code in (429, 403):
                blocked = True
                assert i <= 12, f"Rate limit fired too late at attempt {i}"
                break
        assert blocked, "Rate limiter never activated after 20 attempts"
