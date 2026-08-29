"""Drive the real UI in a real browser.

Everything else in the frontend suite reads app.js as text. That cannot catch a
handler that throws, a selector that matches the wrong element, or state that
fails to reach the DOM -- and all three have happened here. This starts the app
against a throwaway database and drives Chromium at it.

It found the bug it now guards: the conversation model picker offered the
global default model even when the active backend had been told not to offer
it, because populateModelPicker merged _modelOptions in unconditionally. Every
text-level assertion passed while the picker was wrong on screen.

Skipped unless playwright and a Chromium binary are both present, so a machine
without them still runs the rest of the suite.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised only without the dev deps
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
BOOT_TIMEOUT_S = 30


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class BackendsPanelBrowserTests(unittest.TestCase):
    """The Backends tab, exercised the way a person exercises it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "projects").mkdir()
        cls.port = _free_port()
        cls.password = secrets.token_urlsafe(12)
        env = {
            **os.environ,
            "WC_DB_PATH": str(tmp / "wc.db"),
            "WC_PROJECTS_ROOT": str(tmp / "projects"),
            "WC_SESSION_SECRET": secrets.token_urlsafe(32),
            "WC_ADMIN_PASSWORD": cls.password,
            # No proxy: this exercises the UI, never a real turn.
            "WC_PROXY_ENABLED": "0",
            # The test server is plain HTTP on loopback.
            "WC_COOKIE_ALLOW_INSECURE": "1",
        }
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + BOOT_TIMEOUT_S
        while time.time() < deadline:
            if cls.server.poll() is not None:
                output = cls.server.stdout.read().decode(errors="replace")
                raise RuntimeError(f"server exited during boot:\n{output}")
            try:
                urllib.request.urlopen(f"{cls.base}/login", timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        else:  # pragma: no cover - boot failure path
            cls.server.kill()
            raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            cls.server.kill()
        cls.tmp.cleanup()

    def setUp(self):
        self.errors: list[str] = []
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            executable_path=CHROMIUM, args=["--no-sandbox"]
        )
        self.page = self.browser.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error"
            else None,
        )
        self._login()

    def tearDown(self):
        self.browser.close()
        self._pw.stop()

    def _login(self):
        page = self.page
        page.goto(f"{self.base}/login", wait_until="networkidle")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        # By id: the theme toggle is also type=submit and comes first in the DOM.
        page.click("#submitBtn")
        page.wait_for_load_state("networkidle")

    def _open_backends(self):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=10_000)
        self.page.wait_for_timeout(800)

    def _picker(self) -> list[str]:
        return self.page.eval_on_selector_all(
            "#conversationModel option", "els => els.map(e => e.value)"
        )

    def _model_rows(self) -> dict[str, bool]:
        return {
            row.query_selector(".model-item-id").inner_text():
                row.query_selector("input[type=checkbox]").is_checked()
            for row in self.page.query_selector_all(".machine-models .model-item")
        }

    def _activate_if_needed(self):
        button = self.page.query_selector(
            ".machine-actions button:has-text('Activate')"
        )
        if button:
            button.click()
            self.page.wait_for_timeout(1200)

    def _reset_to_offer_all(self):
        """Tick every model, which the UI stores as "offer everything".

        One server and one database serve the whole class, so a selection made
        by an earlier test would otherwise decide what a later one sees. Doing
        it through the checkboxes rather than the API keeps the reset on the
        same path the tests exercise.
        """
        for _ in range(10):
            unticked = [
                row for row in self.page.query_selector_all(".machine-models .model-item")
                if not row.query_selector("input[type=checkbox]").is_checked()
            ]
            if not unticked:
                return
            unticked[0].query_selector("input[type=checkbox]").click()
            self.page.wait_for_timeout(900)
        self.fail("could not restore every model to offered")

    # ── the page loads at all ────────────────────────────────────────────

    def test_settings_opens_without_script_errors(self):
        self._open_backends()
        self.assertTrue(self.page.is_visible("#panelBackends"))
        self.assertEqual(self.errors, [])

    def test_tabs_are_the_merged_set(self):
        self._open_backends()
        tabs = [t.inner_text() for t in self.page.query_selector_all(".settings-tab")]
        self.assertEqual(tabs, ["Backends", "Usage", "Skills", "App"])
        self.assertIsNone(self.page.query_selector("#panelModels"))

    def test_seeded_backend_renders_with_its_models(self):
        self._open_backends()
        cards = self.page.query_selector_all(".machine-card")
        self.assertEqual(len(cards), 1, "a fresh account gets the Anthropic entry")
        self.assertTrue(self._model_rows(), "the card lists models")

    def test_status_line_says_the_list_is_a_fallback(self):
        """Without an API key the endpoint cannot be asked, and the UI must say
        so rather than presenting built-in ids as the real list."""
        self._open_backends()
        status = self.page.query_selector(".models-status").inner_text()
        self.assertIn("built-in", status.lower())

    # ── the feature itself ───────────────────────────────────────────────

    def test_every_model_offered_when_none_are_singled_out(self):
        """An empty active list means all served models are offered.

        That is what a machine starts with, and what the UI stores when every
        box is ticked -- the feature is opt-in and cannot leave an empty picker.
        """
        self._open_backends()
        self._activate_if_needed()
        self._reset_to_offer_all()
        rows = self._model_rows()
        self.assertTrue(rows)
        self.assertTrue(all(rows.values()))
        for model in rows:
            self.assertIn(model, self._picker())

    def test_deactivating_a_model_removes_it_from_the_picker(self):
        """The regression this file was written for.

        populateModelPicker merged the global default in unconditionally, so a
        model the backend had been told not to offer stayed selectable. Text
        assertions could not see it; the browser could.
        """
        self._open_backends()
        self._activate_if_needed()
        self._reset_to_offer_all()
        rows = self.page.query_selector_all(".machine-models .model-item")
        target = rows[1].query_selector(".model-item-id").inner_text()
        self.assertIn(target, self._picker(), "precondition: it starts offered")

        rows[1].query_selector("input[type=checkbox]").click()
        self.page.wait_for_timeout(1500)

        self.assertNotIn(target, self._picker())
        self.assertFalse(self._model_rows()[target])
        # The others must survive: unticking one must not clear the rest.
        still_offered = [m for m, on in self._model_rows().items() if on]
        self.assertTrue(still_offered)
        for model in still_offered:
            self.assertIn(model, self._picker())
        self.assertEqual(self.errors, [])

    def test_selection_survives_a_reload(self):
        self._open_backends()
        self._activate_if_needed()
        self._reset_to_offer_all()
        rows = self.page.query_selector_all(".machine-models .model-item")
        target = rows[1].query_selector(".model-item-id").inner_text()
        rows[1].query_selector("input[type=checkbox]").click()
        self.page.wait_for_timeout(1500)

        self.page.reload(wait_until="networkidle")
        self._open_backends()
        self.assertFalse(self._model_rows()[target])

    def test_refresh_is_not_one_of_the_card_actions(self):
        """It sits in the models toolbar. Sharing .machine-actions made it the
        first action button in the card, so "the first action" found Refresh
        instead of Activate."""
        self._open_backends()
        actions = [
            b.inner_text()
            for b in self.page.query_selector_all(".machine-actions button")
        ]
        self.assertNotIn("Refresh", actions)
        self.assertTrue(self.page.query_selector(".models-refresh"))


if __name__ == "__main__":
    unittest.main()
