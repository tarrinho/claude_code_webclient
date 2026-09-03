"""QA: the composer shows which chat it belongs to, in the hint's own style.

The workspace strip at the top of the page already names the open chat, but a
long conversation scrolls it out of view, and on a phone the keyboard covers
most of what is left -- so "which chat am I typing into" had nothing left to
answer it near the composer. `#composerChatName` repeats the name directly
under the input, styled identically to "Enter to send · Shift+Enter for
newline" so the two read as one line of small print rather than a new UI
element competing for attention.

Driven in a real browser rather than read from source: the requirement is
partly visual (same font/size/color as its sibling hint), and a source-reading
test can confirm the class name is present without confirming anything
actually renders that way.

Seeded before the server starts (registry #41): a second process writing to a
live SQLite file left the server unable to take the write lock again.
"""
from __future__ import annotations

import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover -- absence is a skip
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = Path("/usr/bin/chromium")
BOOT_TIMEOUT_S = 45

# A title long enough to overflow the composer width, exercising the CSS
# ellipsis without ever letting it change what is actually stored or read back.
LONG_TITLE = "Investigate why the nightly export silently drops rows " * 3


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _driver_missing() -> bool:
    if sync_playwright is None or not CHROMIUM.exists():
        return True
    try:
        from playwright._impl._driver import compute_driver_executable

        driver = compute_driver_executable()
        driver = driver[0] if isinstance(driver, (list, tuple)) else driver
        return not Path(driver).exists()
    except Exception:  # noqa: BLE001
        return True


class ComposerChatNameTests(unittest.TestCase):
    server = None
    log_handle = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        if _driver_missing():
            raise unittest.SkipTest("chromium/playwright unavailable")
        try:
            cls._build()
        except Exception:
            cls._release()
            raise

    @classmethod
    def _build(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        home = tmp / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        (tmp / "projects").mkdir()
        cls.port = _free_port()
        cls.password = secrets.token_urlsafe(12)
        cls.db_path = tmp / "wc.db"
        cls.env = {
            **os.environ,
            "HOME": str(home),
            "WC_LOG_FILE": str(tmp / "app.log"),
            "WC_DB_PATH": str(cls.db_path),
            "WC_PROJECTS_ROOT": str(tmp / "projects"),
            "WC_SESSION_SECRET": secrets.token_urlsafe(32),
            "WC_ADMIN_PASSWORD": cls.password,
            "WC_PROXY_ENABLED": "0",
            "WC_COOKIE_ALLOW_INSECURE": "1",
        }

        bootstrap = (
            "import asyncio, auth, db\n"
            "async def go():\n"
            "    await db.init()\n"
            "    await auth.bootstrap_admin()\n"
            "    await db.close()\n"
            "asyncio.run(go())"
        )
        created = subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=ROOT, env=cls.env, capture_output=True, text=True, check=False,
        )
        if created.returncode != 0:
            raise RuntimeError(f"could not create the schema:\n{created.stderr}")

        cls.first_chat = uuid.uuid4().hex
        cls.second_chat = uuid.uuid4().hex
        cls.long_chat = uuid.uuid4().hex
        cls.disposable_chat = uuid.uuid4().hex
        cls._seed(cls.first_chat, "First conversation", "one")
        cls._seed(cls.second_chat, "Second conversation", "two")
        cls._seed(cls.long_chat, LONG_TITLE, "three")
        cls._seed(cls.disposable_chat, "Disposable conversation", "four")
        # first_chat has actually served a turn on a real model; second_chat
        # never has. Neither is pinned (chats.model stays unset by both
        # _seed and this), so the model label's only possible source is
        # last_model_used -- this is what proves the fallback wired to it
        # rather than something incidental making the label show anything.
        cls._seed_usage(cls.first_chat, "vllm/Qwen3.6-35B-A3B-NVFP4")

        cls.log_path = tmp / "server.log"
        cls.log_handle = cls.log_path.open("wb")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, env=cls.env,
            stdout=cls.log_handle, stderr=subprocess.STDOUT,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + BOOT_TIMEOUT_S
        while time.time() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError(f"server exited during boot:\n{cls._log()}")
            try:
                urllib.request.urlopen(f"{cls.base}/login", timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        else:  # pragma: no cover
            raise RuntimeError(f"server did not start:\n{cls._log()}")

    @classmethod
    def _seed(cls, chat_id: str, title: str, marker: str):
        work_dir = str(Path(cls.env["WC_PROJECTS_ROOT"]) / marker)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(cls.db_path))
        try:
            conn.execute(
                "INSERT INTO chats (id, title, description, work_dir, owner_id, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (chat_id, title, None, work_dir, "admin",
                 "2026-09-01T08:00:00Z", "2026-09-01T08:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _seed_usage(cls, chat_id: str, model: str):
        conn = sqlite3.connect(str(cls.db_path))
        try:
            conn.execute(
                "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
                "created_at) VALUES (?,?,?,?,?)",
                (chat_id, "admin", model, "anthropic-compatible",
                 "2026-09-01T09:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _log(cls, lines: int = 40) -> str:
        try:
            if cls.log_handle is not None:
                cls.log_handle.flush()
        except (ValueError, OSError):
            pass
        try:
            tail = cls.log_path.read_text(errors="replace").splitlines()[-lines:]
        except OSError as exc:
            return f"<no server log: {exc}>"
        return "\n".join(tail) or "<server log empty>"

    @classmethod
    def tearDownClass(cls):
        cls._release()

    @classmethod
    def _release(cls):
        if cls.server is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                cls.server.kill()
            cls.server = None
        if cls.log_handle is not None:
            cls.log_handle.close()
            cls.log_handle = None
        if cls.tmp is not None:
            cls.tmp.cleanup()
            cls.tmp = None

    def setUp(self):
        if self.server.poll() is not None:
            self.fail(f"the server exited: {self._log()}")
        self.errors: list[str] = []
        # addCleanup right after each acquisition, LIFO, so the browser closes
        # before playwright stops (registry #35).
        self._pw = sync_playwright().start()
        self.addCleanup(self._pw.stop)
        self.browser = self._pw.chromium.launch(
            executable_path=str(CHROMIUM), args=["--no-sandbox"])
        self.addCleanup(self.browser.close)
        context = self.browser.new_context(viewport={"width": 900, "height": 700})
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        self.page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        self.page.fill("#username", "admin")
        self.page.fill("#password", self.password)
        self.page.click("#submitBtn")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)

    def _open(self, chat_id: str):
        row = self.page.locator(f'.chat-item[data-chat-id="{chat_id}"]:visible').first
        row.wait_for(state="visible", timeout=15_000)
        row.click()
        self.page.wait_for_selector("#composerArea", timeout=15_000)
        self.page.wait_for_timeout(200)

    def _name(self) -> str:
        return self.page.evaluate(
            "() => document.getElementById('composerChatName').textContent")

    # ── the behaviour ─────────────────────────────────────────────────────

    def test_opening_a_chat_shows_its_name_below_the_composer(self):
        self._open(self.first_chat)
        self.assertEqual(self._name(), "First conversation")

    def test_switching_chat_updates_the_name_rather_than_keeping_the_old_one(self):
        self._open(self.first_chat)
        self._open(self.second_chat)
        self.assertEqual(
            self._name(), "Second conversation",
            "the composer still names the previous conversation after switching")

    def test_no_chat_open_leaves_it_empty(self):
        """The welcome screen names no conversation, so this must not either.

        Reached by deleting the open chat rather than by reloading: the app
        deliberately restores the last-opened conversation on reload
        (`wc_last_chat` in localStorage), so a reload never actually reaches
        the welcome screen once a chat has been opened -- the first version of
        this test asserted against that and failed for a reason that had
        nothing to do with the feature under test. Deletion is a real path
        `showWelcome()` is called from (app.js), and a chat seeded solely for
        this test means destroying it cannot affect any other test's fixture.
        """
        # `:visible` is load-bearing here too, same reason as `_open`: the
        # sidebar renders every row twice (desktop list, mobile drawer), so a
        # bare selector resolves two elements and clicks whichever is first
        # in the DOM regardless of which one a user could actually see.
        self._open(self.disposable_chat)
        self.page.click(
            f'button[data-action="menu"][data-chat-id="{self.disposable_chat}"]:visible')
        self.page.click(
            f'button[data-action="delete"][data-chat-id="{self.disposable_chat}"]:visible')
        self.page.click("#dialogSave")
        self.page.wait_for_selector(".empty-state", timeout=15_000)
        self.assertEqual(self._name(), "")

    def test_the_element_is_not_rendered_when_empty(self):
        """`#composerChatName:empty{display:none}` -- no visible empty box on
        the welcome screen before any chat has ever been opened."""
        self.assertFalse(
            self.page.locator("#composerChatName").is_visible(),
            "an empty chat-name element is taking up space before any chat is open")

    def test_a_long_title_does_not_change_what_is_stored(self):
        """CSS may truncate the *display*; the DOM text must stay exact so a
        title is never silently shortened by the fix that shows it."""
        self._open(self.long_chat)
        self.assertEqual(self._name(), LONG_TITLE)

    def test_matches_the_enter_to_send_hint_in_font_size_and_color(self):
        """The requirement, checked as a requirement: same size of text as
        the hint on the right, not merely the same CSS class name."""
        self._open(self.first_chat)
        style = self.page.evaluate("""() => {
            const name = getComputedStyle(document.getElementById('composerChatName'));
            const hint = getComputedStyle(document.querySelector('.composer-hint'));
            return {
                nameFont: name.font, hintFont: hint.font,
                nameColor: name.color, hintColor: hint.color,
            };
        }""")
        self.assertEqual(style["nameFont"], style["hintFont"])
        self.assertEqual(style["nameColor"], style["hintColor"])

    # ── the top-of-conversation model label ──────────────────────────────

    def _model_label(self) -> dict:
        return self.page.evaluate("""() => {
            const el = document.getElementById('modelLabel');
            return {hidden: el.hidden, text: el.textContent};
        }""")

    def test_the_model_label_falls_back_to_the_last_model_actually_used(self):
        """Neither chat is pinned (chats.model is unset), so the label can
        only be reading last_model_used -- if it fell back to nothing here,
        GET /api/chats/{id} is not the same shape as the list endpoint."""
        self._open(self.first_chat)
        label = self._model_label()
        self.assertFalse(label["hidden"])
        self.assertIn("vllm/Qwen3.6-35B-A3B-NVFP4", label["text"])

    def test_the_model_label_stays_hidden_for_a_chat_with_no_turns_yet(self):
        """The other half: an unpinned chat that has never run must not show
        a stale or invented model just because the label exists."""
        self._open(self.second_chat)
        self.assertTrue(self._model_label()["hidden"])

    def test_no_script_errors(self):
        self._open(self.first_chat)
        self._open(self.second_chat)
        self._open(self.long_chat)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
