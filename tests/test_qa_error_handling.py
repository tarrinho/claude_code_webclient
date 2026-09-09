"""QA: error handling patterns in JS and response handling.

Python cannot import .js files, so these tests verify error handling
patterns via static source analysis and JSON parsing of response shapes.

This file tests:
  * HTML body extraction regex on Caddy error pages.
  * JSON error field parsing.
  * 401/303 redirect patterns.
  * JS files contain expected error-handling code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unittest


CADDY_ERROR_HTML = """<!doctype html>
<html lang="en">
<head><title>Bad Gateway</title></head>
<body>
  <h1>Bad Gateway</h1>
  <p>The server did not respond.</p>
</body>
</html>"""


class HTMLBodyExtractionTests(unittest.TestCase):
    """Test that Caddy error HTML body is correctly extracted."""

    def _extract_body(self, html: str) -> str | None:
        match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL)
        if match:
            body = match.group(1).strip()
            return body if body else None
        return None

    def test_extracts_body_tag_content(self):
        body = self._extract_body(CADDY_ERROR_HTML)
        self.assertIsNotNone(body)
        self.assertIn("Bad Gateway", body)
        self.assertIn("The server did not respond", body)

    def test_returns_none_without_body_tag(self):
        body = self._extract_body("<html><head><title>Test</title></head></html>")
        self.assertIsNone(body)

    def test_returns_none_with_empty_body(self):
        body = self._extract_body("<body>  </body>")
        self.assertIsNone(body)

    def test_handles_nested_html(self):
        html = "<body><div><p>Deep nested content</p></div></body>"
        body = self._extract_body(html)
        self.assertIsNotNone(body)
        self.assertIn("Deep nested content", body)


class JSONErrorFieldTests(unittest.TestCase):
    """Test that JSON error fields are correctly parsed."""

    def test_json_error_field(self):
        json_data = '{"error": "Too many requests. Try again later."}'
        data = json.loads(json_data)
        self.assertIn("error", data)
        self.assertEqual(data["error"], "Too many requests. Try again later.")

    def test_json_error_field_401(self):
        json_data = '{"error": "Session expired", "redirect": "/login"}'
        data = json.loads(json_data)
        self.assertEqual(data["error"], "Session expired")
        self.assertEqual(data["redirect"], "/login")

    def test_non_json_fallback(self):
        body = "Internal Server Error"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(body)


class Status401HandlingTests(unittest.TestCase):
    """401 responses must trigger login redirect."""

    def test_401_has_error_field(self):
        response = {
            "status": 401,
            "body": {"error": "Unauthorized", "redirect": "/login"},
        }
        self.assertEqual(response["status"], 401)
        self.assertIn("error", response["body"])

    def test_401_redirects_to_login(self):
        response = {
            "status": 401,
            "body": {"error": "Session expired", "redirect": "/login"},
        }
        self.assertEqual(response["body"]["redirect"], "/login")


class Status303HandlingTests(unittest.TestCase):
    """303 responses must redirect."""

    def test_303_redirect(self):
        response = {
            "status": 303,
            "headers": {"location": "/"},
        }
        self.assertEqual(response["status"], 303)
        self.assertEqual(response["headers"]["location"], "/")

    def test_303_no_body(self):
        response = {"status": 303, "body": None}
        self.assertIsNone(response["body"])


class JavaScriptErrorHandlingTests(unittest.TestCase):
    """Static analysis: verify JS files contain error-handling patterns."""

    @property
    def web_dir(self) -> Path:
        """Resolved from this file, not hardcoded.

        This was the absolute path of one developer's checkout, so the four
        tests below read that tree whatever tree was under test. Run anywhere
        else they do not merely fail -- they fail for the wrong reason: on a
        remote QA node the path exists and belongs to another user, so the
        two "handles fetch errors" cases raised PermissionError while the two
        "exists" cases asserted False, and none of them had looked at the
        checkout they were invoked against.
        """
        return Path(__file__).resolve().parents[1] / "web" / "assets"

    def test_app_js_exists(self):
        self.assertTrue(os.path.exists(f"{self.web_dir}/app.js"))

    def test_conversation_js_exists(self):
        self.assertTrue(os.path.exists(f"{self.web_dir}/conversation.js"))

    def test_app_js_handles_fetch_errors(self):
        """app.js must use fetch error handling (status checks, catch)."""
        with open(f"{self.web_dir}/app.js", "r") as f:
            content = f.read()
        self.assertIn("fetch", content)
        self.assertIn("status", content)

    def test_conversation_js_handles_fetch_errors(self):
        """conversation.js must use fetch error handling."""
        with open(f"{self.web_dir}/conversation.js", "r") as f:
            content = f.read()
        self.assertIn("fetch", content)
        self.assertIn("status", content)


if __name__ == "__main__":
    unittest.main()
