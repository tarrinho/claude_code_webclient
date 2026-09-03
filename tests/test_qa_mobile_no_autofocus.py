"""QA: opening a conversation must not raise the on-screen keyboard.

Pedro, on a phone: changing chat popped the keyboard every time. It covers most
of the conversation, so the first thing you see after choosing what to read is
the thing that stops you reading it -- and you did not ask to type.

Three sites called ``.focus()`` on a text input unconditionally:
``loadChat`` after every open, the ``finally`` of the streaming send after
every completed turn, and ``openSidebar`` focusing the chat-search box on the
one gesture -- tapping the menu icon -- that is unambiguously a request to
browse, not to type. On a pointer device that focus is a genuine convenience;
on a touch device it is a keyboard nobody asked for. All three are now
conditional on ``(hover: hover) and (pointer: fine)`` rather than removed for
everybody.

Matched on pointer capability, **not** on touch support. A laptop with a
touchscreen reports touch and still wants the focus, so ``'ontouchstart' in
window`` would take the behaviour away from a machine that benefits from it. What
matters is how the user is driving the device, which is what these media
features describe.

Driven in a real browser with real device emulation, because the whole change is
a media query: a source-reading test would confirm the string is present and
nothing about whether a phone is actually spared. Chromium under
``playwright.devices['iPhone 13']`` reports ``pointer: coarse`` and
``hover: none``, which is the condition the code reads.

**Two conversations are seeded, not one.** The report was about *changing* chat,
and a single conversation only ever exercises the first open. The mobile case
below switches between them and asserts the composer stays unfocused across the
switch, which is the sequence that was actually complained about.

Seeded before the server starts (registry #41): a second process writing to a
live SQLite file left the server unable to take the write lock again, serving
HTTP 200 while recording nothing.
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

# The media query the production code reads. Asserted directly in both contexts
# so a device-emulation change that stopped discriminating would fail loudly
# here rather than silently making the behavioural assertions vacuous.
POINTER_QUERY = "(hover: hover) and (pointer: fine)"


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


class ComposerAutoFocusTests(unittest.TestCase):
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
            "WC_PROJECTS_ROOT_BASE": str(tmp),
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
        cls._seed(cls.first_chat, "First conversation", "one")
        cls._seed(cls.second_chat, "Second conversation", "two")

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
            conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) "
                "VALUES (?,?,?,?)",
                (chat_id, "user", f"hello from {marker}", "2026-09-01T09:00:00Z"))
            conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) "
                "VALUES (?,?,?,?)",
                (chat_id, "assistant", f"reply {marker}", "2026-09-01T09:00:01Z"))
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
        # before playwright stops. registry #35: a playwright left running keeps
        # its greenlet loop flagged as this thread's running loop and kills every
        # async test that follows.
        self._pw = sync_playwright().start()
        self.addCleanup(self._pw.stop)
        self.browser = self._pw.chromium.launch(
            executable_path=str(CHROMIUM), args=["--no-sandbox"])
        self.addCleanup(self.browser.close)

    # ── contexts ───────────────────────────────────────────────────────────────

    def _page(self, **context_kwargs):
        """A logged-in page in its own context; cookies do not cross contexts."""
        context = self.browser.new_context(**context_kwargs)
        self.addCleanup(context.close)
        page = context.new_page()
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        page.click("#submitBtn")
        page.wait_for_selector("#settingsBtn", timeout=15_000)
        return page

    def _phone(self):
        return self._page(**self._pw.devices["iPhone 13"])

    def _desktop(self):
        return self._page(viewport={"width": 1280, "height": 900})

    # ── helpers ────────────────────────────────────────────────────────────────

    def _open(self, page, chat_id: str):
        # `:visible` is load-bearing: the sidebar renders every conversation
        # twice, once in the desktop list and once in the mobile drawer, so a
        # bare selector resolves the hidden copy and waits out its timeout.
        row = page.locator(f'.chat-item[data-chat-id="{chat_id}"]:visible').first
        row.wait_for(state="visible", timeout=15_000)
        row.click()
        page.wait_for_selector("#composerArea", timeout=15_000)
        # The focus, if it happens, happens in the same task as the render.
        page.wait_for_timeout(400)

    @staticmethod
    def _composer_focused(page) -> bool:
        return page.evaluate(
            "() => document.activeElement === document.getElementById('composerInput')")

    @staticmethod
    def _matches(page, query: str) -> bool:
        return page.evaluate("(q) => window.matchMedia(q).matches", query)

    def _open_drawer_if_present(self, page):
        """The phone layout hides the conversation list behind a menu button."""
        button = page.locator("#menuBtn")
        if button.is_visible():
            button.click()
            page.wait_for_timeout(300)

    # ── the discriminator itself ────────────────────────────────────────────────

    def test_the_emulated_phone_reports_a_coarse_pointer(self):
        """Guards every mobile assertion below.

        If emulation stopped reporting `pointer: coarse` the production guard
        would take the desktop branch and the tests asserting "not focused"
        would be checking nothing.
        """
        page = self._phone()
        self.assertFalse(self._matches(page, POINTER_QUERY),
                         "the emulated phone claims a fine pointer")

    def test_the_desktop_context_reports_a_fine_pointer(self):
        """The other half: a guard that never matched would 'fix' mobile by
        breaking the desktop convenience it is meant to keep."""
        page = self._desktop()
        self.assertTrue(self._matches(page, POINTER_QUERY))

    # ── the behaviour ──────────────────────────────────────────────────────────

    def test_opening_a_chat_on_a_phone_does_not_focus_the_composer(self):
        """The report, directly."""
        page = self._phone()
        self._open_drawer_if_present(page)
        self._open(page, self.first_chat)
        self.assertFalse(
            self._composer_focused(page),
            "opening a conversation on a phone focused the composer, which "
            "raises the on-screen keyboard")

    def test_changing_chat_on_a_phone_does_not_focus_the_composer(self):
        """What was actually complained about: not the first open, the switch."""
        page = self._phone()
        self._open_drawer_if_present(page)
        self._open(page, self.first_chat)
        self._open_drawer_if_present(page)
        self._open(page, self.second_chat)
        self.assertFalse(
            self._composer_focused(page),
            "changing conversation on a phone focused the composer")

    def test_tapping_the_composer_on_a_phone_does_focus_it(self):
        """The other half of the request: *only* if I tap the box.

        Without this, disabling the focus entirely would pass every assertion
        above while leaving the composer unusable.
        """
        page = self._phone()
        self._open_drawer_if_present(page)
        self._open(page, self.first_chat)
        page.tap("#composerInput")
        page.wait_for_timeout(200)
        self.assertTrue(
            self._composer_focused(page),
            "tapping the composer did not focus it")

    def test_opening_a_chat_on_a_desktop_still_focuses_the_composer(self):
        """The convenience this change deliberately keeps.

        Removing the focus for everyone would have been the easy fix and would
        pass all three mobile tests.
        """
        page = self._desktop()
        self._open(page, self.first_chat)
        self.assertTrue(
            self._composer_focused(page),
            "a pointer device lost the type-straight-away behaviour")

    # ── the sidebar search box ──────────────────────────────────────────────

    @staticmethod
    def _search_focused(page) -> bool:
        return page.evaluate(
            "() => document.activeElement === document.getElementById('chatSearch')")

    def test_opening_the_sidebar_on_a_phone_does_not_focus_search(self):
        """Tapping the menu icon is a request to browse, not to type."""
        page = self._phone()
        page.click("#menuBtn")
        page.wait_for_timeout(300)
        self.assertFalse(
            self._search_focused(page),
            "opening the sidebar on a phone focused search, which raises the "
            "on-screen keyboard over the list the user just opened")

    def test_tapping_sidebar_search_on_a_phone_does_focus_it(self):
        """The other half: *only* if the user taps the box themselves."""
        page = self._phone()
        page.click("#menuBtn")
        page.wait_for_timeout(300)
        page.tap("#chatSearch")
        page.wait_for_timeout(200)
        self.assertTrue(
            self._search_focused(page),
            "tapping the sidebar search box did not focus it")

    def test_no_script_errors_in_either_context(self):
        """A guard on the rest: an exception before the focus call would make
        'not focused' true for the wrong reason."""
        phone = self._phone()
        self._open_drawer_if_present(phone)
        self._open(phone, self.first_chat)
        desktop = self._desktop()
        self._open(desktop, self.first_chat)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
