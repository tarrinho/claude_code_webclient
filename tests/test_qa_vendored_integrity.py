"""Vendored bundles must carry an integrity hash that matches their bytes.

`script-src 'self'` permits these two files unconditionally, because they are
same-origin. So CSP -- however strict -- offers nothing against a modification
to the files themselves, whether from a dependency update nobody read or from
an agent with write access to this tree. `purify.min.js` is the sanitiser
guarding the innerHTML sink in specs.js, so anything able to edit it owns the
sanitiser as well as the page.

This test is the drift stage, not merely a presence check. It recomputes the
hash from the file on disk and compares it to the attribute in the template,
so the two cannot diverge: change the bundle and the test fails until someone
regenerates the attribute deliberately. A test asserting only that an
`integrity=` attribute exists would pass forever after the first stale hash --
another check that stops checking.

Spec: docs/superpowers/specs/2026-09-25-high-assurance-development-design.md
      section 4.6, rollout step 3
"""
from __future__ import annotations

import base64
import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "web" / "index.html"

#: Bundles we did not write, served from our own origin. Anything added here
#: must also gain an integrity attribute in the template.
VENDORED = ("d3.min.js", "purify.min.js")


def _sri(path: Path) -> str:
    return "sha384-" + base64.b64encode(
        hashlib.sha384(path.read_bytes()).digest()).decode()


class VendoredIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.html = TEMPLATE.read_text()

    def _tag_for(self, name: str) -> str:
        match = re.search(
            r"<script[^>]*src=\"/assets/" + re.escape(name) + r"\"[^>]*>", self.html)
        self.assertIsNotNone(
            match, f"no script tag loads {name}; update VENDORED if it was removed")
        return match.group(0)

    def test_every_vendored_bundle_declares_its_hash(self):
        for name in VENDORED:
            with self.subTest(bundle=name):
                self.assertIn("integrity=", self._tag_for(name),
                              f"{name} is served with no integrity attribute")

    def test_the_declared_hash_matches_the_bytes_on_disk(self):
        """The drift half. This is what fails when a bundle is replaced."""
        for name in VENDORED:
            with self.subTest(bundle=name):
                tag = self._tag_for(name)
                declared = re.search(r"integrity=\"([^\"]+)\"", tag)
                self.assertIsNotNone(declared, f"{name} has no integrity value")
                self.assertEqual(
                    declared.group(1), _sri(ROOT / "web" / "assets" / name),
                    f"{name} on disk does not match the hash in index.html. "
                    "If the update was intended, regenerate the attribute; if "
                    "it was not, this is the alarm.")

    def test_no_other_first_party_script_is_pinned_by_accident(self):
        """First-party modules are cache-busted by `?v=`, which changes with
        every edit. Pinning one would fail the build on ordinary work, so the
        absence is deliberate rather than an oversight."""
        for tag in re.findall(r"<script[^>]*integrity=[^>]*>", self.html):
            src = re.search(r"src=\"/assets/([^\"?]+)", tag)
            self.assertIsNotNone(src, f"integrity on a tag with no src: {tag}")
            self.assertIn(src.group(1), VENDORED,
                          f"{src.group(1)} is pinned but is not vendored")


if __name__ == "__main__":
    unittest.main()
