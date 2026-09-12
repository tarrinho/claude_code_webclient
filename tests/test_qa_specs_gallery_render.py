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
