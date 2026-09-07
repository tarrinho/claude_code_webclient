"""Checks for the dependency-free comparison PDF renderer."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "bin" / "render_comparison_pdf.py"
_SPEC = importlib.util.spec_from_file_location("render_comparison_pdf", _SCRIPT)
_RENDERER = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_RENDERER)


class ComparisonPdfRendererTests(unittest.TestCase):
    def test_text_uses_absolute_coordinates(self):
        rows = [("First line", "body"), ("Second line", "body")]
        streams = _RENDERER._stream(rows)
        content = streams[0].decode("latin-1")
        self.assertIn("1 0 0 1 48 790.0 Tm (First line) Tj", content)
        self.assertIn("1 0 0 1 48 777.0 Tm (Second line) Tj", content)
        self.assertNotIn(" Td ", content)

    def test_rendered_pdf_has_extractable_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "comparison.md"
            target = Path(directory) / "comparison.pdf"
            source.write_text(
                "# Visible heading\n\nThis text must remain visible.\n",
                encoding="utf-8",
            )
            _RENDERER.render(source, target)
            data = target.read_bytes()

        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertIn(b"(Visible heading)", data)
        self.assertIn(b"(This text must remain visible.)", data)
        self.assertIn(b"/Type /Page", data)

    def test_rendered_pages_are_not_empty(self):
        rows = _RENDERER._markdown_lines(
            (ROOT / "Backend_Models_20260902.comparison.md").read_text(
                encoding="utf-8"
            )
        )
        streams = _RENDERER._stream(rows)
        self.assertGreaterEqual(len(streams), 2)
        self.assertTrue(all(b" Tj" in stream for stream in streams))


if __name__ == "__main__":
    unittest.main()
