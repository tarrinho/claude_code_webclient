"""QA: specs_gallery.render_markdown -- server-side rendering with a
plain-text fallback on malformed input. Design:
docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import specs_gallery


class RenderMarkdownTests(unittest.TestCase):
    def test_renders_a_heading(self):
        html = specs_gallery.render_markdown("# Title\n\nbody text\n")
        self.assertIn("<h1", html)
        self.assertIn("Title", html)

    def test_renders_a_code_block(self):
        html = specs_gallery.render_markdown("```\ncode here\n```\n")
        self.assertIn("<pre", html)

    def test_falls_back_to_escaped_text_on_render_failure(self):
        """Malformed input must never surface as a 500 -- escaped plain
        text instead, per spec section 5."""
        with patch("markdown.markdown", side_effect=Exception("boom")):
            html = specs_gallery.render_markdown("<script>evil()</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_embedded_script_does_not_survive_the_success_path(self):
        """Regression test for I1: the previous fix only sanitized
        client-side (specs.js's DOMPurify pass) and only the *fallback*
        path here escaped anything -- the success path returned markdown's
        raw rendered HTML unchanged, so a <script> embedded in otherwise
        valid markdown reached anyone hitting /api/specs/{id}/content
        directly. This must not survive the success path, not just the
        exception fallback."""
        html = specs_gallery.render_markdown(
            "# Title\n\n<script>alert(1)</script>\n\nbody text\n")
        self.assertNotIn("<script", html)
        self.assertNotIn("alert(1)", html)
        # Confirms this actually exercised the success path, not the fallback.
        self.assertIn("<h1", html)

    def test_event_handler_attribute_is_stripped_on_the_success_path(self):
        html = specs_gallery.render_markdown(
            '# Title\n\n<img src="x" onerror="alert(1)">\n')
        self.assertNotIn("onerror", html)

    def test_a_legitimate_spec_still_renders_after_sanitization(self):
        """Proves the sanitizer doesn't break normal specs: heading,
        code block, table and link must all still come through."""
        text = (
            "# Title\n\n"
            "body [a link](https://example.com)\n\n"
            "```\ncode here\n```\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
        )
        html = specs_gallery.render_markdown(text)
        self.assertIn("<h1", html)
        self.assertIn("Title", html)
        self.assertIn("<pre", html)
        self.assertIn("code here", html)
        self.assertIn("<table", html)
        self.assertIn("<td>1</td>", html)
        self.assertIn('href="https://example.com"', html)
