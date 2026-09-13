"""Settings > Images (and every other Settings-tab delete: machines.js,
specs.js) opens a shared #confirmDialog on top of the already-open
#settingsDialog. Reported by Pedro: "the delete popup appeares below the
popup of the page itself."

Both are `.dialog-backdrop`, both at z-index:400 by default -- equal
z-index falls back to DOM order, and #confirmDialog sits *earlier* in
index.html than #settingsDialog, so the later-painted Settings backdrop
covered it. #specViewerDialog hit the exact same class of bug before (its
own CSS comment says so) and was fixed the same way: a higher z-index for
"a dialog opened from within Settings". #confirmDialog never got that
same bump.

Read from source alone (z-index numbers) is not proof of what a browser
actually stacks -- this repo has a standing pattern for that
(test_qa_auto_answer_geometry.py's own docstring), so this renders the
real index.html + styles.css headlessly, forces both dialogs open, and
asks the browser itself which element is on top at their shared center
via elementFromPoint -- the same question a real click resolves.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
ASSETS = WEB / "assets"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def probe_stacking(width: int = 1024, height: int = 800) -> dict[str, object]:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (ASSETS / "styles.css").read_text(encoding="utf-8")
    html = re.sub(r'<link rel="stylesheet"[^>]*>',
                  lambda _m: f"<style>{css}</style>", html)
    html = re.sub(r'<script type="module"[^>]*></script>', "", html)

    probe = """
<script>
window.addEventListener("load", function () {
  document.getElementById("settingsDialog").classList.add("open");
  document.getElementById("confirmDialog").classList.add("open");
  var box = document.getElementById("confirmDialogBody").getBoundingClientRect();
  var x = box.x + box.width / 2, y = box.y + box.height / 2;
  var top = document.elementFromPoint(x, y);
  var out = {
    topIsInsideConfirm: !!(top && top.closest("#confirmDialog")),
    topId: top ? top.id : null,
    topClosestDialog: top ? (top.closest(".dialog-backdrop") || {}).id || null : null,
  };
  document.title = JSON.stringify(out);
});
</script></body>"""
    html = html.replace("</body>", probe, 1)

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={page.parent}/chrome-profile",
             f"--window-size={width},{height}",
             "--virtual-time-budget=3000", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    match = re.search(r"<title>(.*?)</title>", result.stdout, re.DOTALL)
    if not match:
        raise AssertionError("probe produced no title; chromium output: "
                             + result.stdout[:400])
    return json.loads(match.group(1))


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class ConfirmDialogStacksAboveSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geo = probe_stacking()

    def test_confirm_dialog_is_the_topmost_element_at_its_own_center(self):
        """The regression itself, and the property a real click needs: with
        both dialogs open, the element a click actually reaches at the
        confirm dialog's own center must be inside #confirmDialog, not the
        Settings backdrop painted on top of it."""
        self.assertTrue(
            self.geo["topIsInsideConfirm"],
            f"expected the topmost element at #confirmDialog's center to be "
            f"inside it; got id={self.geo['topId']!r} whose nearest "
            f".dialog-backdrop ancestor is {self.geo['topClosestDialog']!r} "
            "-- the Settings backdrop is covering the confirm dialog",
        )
