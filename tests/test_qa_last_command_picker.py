"""QA: the last-request strip offers the recent requests and lets one be pinned.

The strip under the workspace toolbar shows what you last asked here. It now
opens a picker listing the ten most recent requests in the conversation, and
selecting one pins it in the strip instead of the newest.

Driven in a real browser against a real server, because everything worth
asserting here is behaviour: a popover opens, a row is chosen, the strip changes
and says it is pinned. A source-reading test would pass against markup nobody
can click -- which is the failure this project has recorded repeatedly, most
recently a shell block that could never succeed passing nine assertions.

The conversation is seeded by writing to the database **before the server
starts**. Registry #41: a second process writing to a live SQLite file left the
server's connection unable to ever take the write lock again, and the site
served HTTP 200 while recording nothing for 37 minutes. Seeding first means
there is only ever one writer.
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

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = Path("/usr/bin/chromium")
BOOT_TIMEOUT_S = 45

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover -- absence is a skip
    sync_playwright = None

# Twelve, deliberately more than the ten the picker offers, so the cap is
# exercised rather than assumed. Distinct texts so a row can be identified.
PROMPTS = [f"request number {n:02d}" for n in range(1, 13)]
# One prompt that would execute if it ever reached innerHTML. The strip and every
# row render user text, so this is the case that matters most.
MARKUP_PROMPT = "<img src=x onerror=alert('xss')>"
# Where the markup prompt is seeded, counted from the oldest. Position 3 puts it
# *inside* the ten the picker offers -- as the oldest of them -- while leaving
# the newest request a normal one.
#
# It was first seeded as the very oldest, which put it outside the ten. The
# picker therefore never rendered it, and the XSS case asserted that no <img>
# existed in a menu that had never been given one: it passed with `textContent`
# swapped for `innerHTML`, so the one assertion here that guards a real
# vulnerability could not fail. Found by mutation, not by reading -- and the
# docstring had reasoned its way into the mistake out loud.
MARKUP_AT = 3
# Oldest first, as seeded.
SEEDED = [*PROMPTS[:MARKUP_AT], MARKUP_PROMPT, *PROMPTS[MARKUP_AT:]]
# Newest first, as the picker shows them, capped at ten.
EXPECTED_ROWS = list(reversed(SEEDED))[:10]


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
    except Exception:
        return True


class LastRequestPickerTests(unittest.TestCase):
    server = None
    log_handle = None
    tmp = None
    chat_id = None

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

        # Schema first, in its own process, so nothing shares the file with the
        # server. Also creates the admin row the browser logs in as.
        bootstrap = (
            "import asyncio, auth, db\n"
            "async def go():\n"
            "    await db.init()\n"
            "    await auth.bootstrap_admin()\n"
            "    await db.close()\n"
            "asyncio.run(go())"
        )
        create = subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=ROOT, env=cls.env, capture_output=True, text=True, check=False,
        )
        if create.returncode != 0:
            raise RuntimeError(f"could not create the schema:\n{create.stderr}")

        cls.chat_id = uuid.uuid4().hex
        cls._seed(cls.chat_id)

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
    def _seed(cls, chat_id: str):
        """A conversation with 13 user requests, oldest first."""
        work_dir = str(Path(cls.env["WC_PROJECTS_ROOT"]) / "seeded")
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(cls.db_path))
        try:
            conn.execute(
                "INSERT INTO chats (id, title, description, work_dir, owner_id, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (chat_id, "Seeded", None, work_dir, "admin",
                 "2026-09-01T08:00:00Z", "2026-09-01T08:00:00Z"),
            )
            # Ascending timestamps so "newest" is unambiguous, and an assistant
            # reply between each so the walk-back has to skip non-user rows.
            for index, prompt in enumerate(SEEDED):
                stamp = f"2026-09-01T09:{index:02d}:00Z"
                conn.execute(
                    "INSERT INTO messages (chat_id, role, content, created_at) "
                    "VALUES (?,?,?,?)", (chat_id, "user", prompt, stamp))
                conn.execute(
                    "INSERT INTO messages (chat_id, role, content, created_at) "
                    "VALUES (?,?,?,?)",
                    (chat_id, "assistant", f"reply {index}", stamp))
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
            return "\n".join(
                cls.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError as exc:
            return f"<no server log: {exc}>"

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
        # addCleanup registered immediately after each acquisition, LIFO, so the
        # browser closes before playwright stops. registry #35: a playwright
        # that is never stopped leaves its greenlet loop flagged as this
        # thread's running loop and kills every async test that follows.
        self._pw = sync_playwright().start()
        self.addCleanup(self._pw.stop)
        self.browser = self._pw.chromium.launch(
            executable_path=str(CHROMIUM), args=["--no-sandbox"])
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        self._login()
        self._open_seeded_chat()

    def _login(self):
        page = self.page
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        page.click("#submitBtn")
        page.wait_for_selector("#settingsBtn", timeout=15_000)

    def _open_seeded_chat(self):
        # `:visible` is load-bearing: the sidebar renders each conversation more
        # than once (the desktop list and the mobile drawer both hold a row with
        # the same data-chat-id), and a bare selector resolves to the hidden one
        # first, then waits out its timeout on an element that will never be
        # visible.
        row = self.page.locator(
            f'.chat-item[data-chat-id="{self.chat_id}"]:visible').first
        row.wait_for(state="visible", timeout=15_000)
        row.click()
        # The strip is the subject; waiting on it rather than on a timeout.
        self.page.wait_for_selector("#lastCommandBar:not([hidden])", timeout=15_000)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _strip_text(self) -> str:
        return self.page.inner_text("#lastCommandText").strip()

    def _rows(self) -> list[str]:
        return self.page.eval_on_selector_all(
            "#lastCommandMenu .lastcmd-item .lastcmd-item-text",
            "els => els.map(e => e.textContent.trim())",
        )

    def _open_menu(self):
        self.page.click("#lastCommandText")
        self.page.wait_for_selector("#lastCommandMenu:not([hidden])", timeout=5_000)

    def _wait_closed(self):
        """Wait for the picker to be gone, by state rather than by selector.

        ``wait_for_selector("#lastCommandMenu[hidden]")`` cannot ever succeed:
        the default state is ``visible``, and an element carrying ``hidden`` is
        by definition not visible, so the call waits out its timeout on a
        condition that is already true. Six cases here failed that way -- a wait
        that cannot be satisfied is the same shape of defect as an assertion
        that cannot fail.
        """
        self.page.wait_for_selector("#lastCommandMenu", state="hidden", timeout=5_000)

    # ── the strip itself ───────────────────────────────────────────────────────

    def test_the_strip_opens_showing_the_newest_request(self):
        self.assertEqual(self._strip_text(), PROMPTS[-1])
        self.assertEqual(
            self.page.get_attribute("#lastCommandBar", "data-pinned"), "0")

    def test_the_trigger_announces_that_it_controls_a_popup(self):
        """It is a control, so it has to say so to anything not using a mouse."""
        self.assertEqual(
            self.page.get_attribute("#lastCommandText", "aria-haspopup"), "listbox")
        self.assertEqual(
            self.page.get_attribute("#lastCommandText", "aria-expanded"), "false")
        self._open_menu()
        self.assertEqual(
            self.page.get_attribute("#lastCommandText", "aria-expanded"), "true")

    # ── the picker ─────────────────────────────────────────────────────────────

    def test_it_offers_ten_requests_newest_first(self):
        self._open_menu()
        rows = self._rows()
        self.assertEqual(len(rows), 10, f"expected the ten most recent, got {rows}")
        self.assertEqual(rows, EXPECTED_ROWS)

    def test_the_oldest_requests_are_dropped_not_the_newest(self):
        """A cap applied at the wrong end would offer the least useful ten."""
        self._open_menu()
        rows = self._rows()
        self.assertIn(SEEDED[-1], rows, "the newest request is missing")
        self.assertNotIn(SEEDED[0], rows, "an aged-out request is still offered")
        # The markup prompt sits inside the ten on purpose; see MARKUP_AT.
        self.assertIn(MARKUP_PROMPT, rows)

    def test_choosing_an_older_request_pins_it_in_the_strip(self):
        self._open_menu()
        wanted = EXPECTED_ROWS[3]
        self.page.click(f'#lastCommandMenu .lastcmd-item:has-text("{wanted}")')
        self._wait_closed()
        self.assertEqual(self._strip_text(), wanted)
        self.assertEqual(
            self.page.get_attribute("#lastCommandBar", "data-pinned"), "1")
        # It must stop claiming to be the newest, in words and not only colour.
        self.assertIn("pinned", self.page.inner_text("#lastCommandWhen").lower())

    def test_a_pinned_strip_offers_a_way_back_to_the_latest(self):
        self._open_menu()
        wanted = EXPECTED_ROWS[3]
        self.page.click(f'#lastCommandMenu .lastcmd-item:has-text("{wanted}")')
        self._wait_closed()

        self._open_menu()
        self.page.click("#lastCommandMenu .lastcmd-item-latest")
        self._wait_closed()
        self.assertEqual(self._strip_text(), PROMPTS[-1])
        self.assertEqual(
            self.page.get_attribute("#lastCommandBar", "data-pinned"), "0")

    def test_the_return_row_is_absent_until_something_is_pinned(self):
        """Offering it while nothing is pinned is a control that does nothing."""
        self._open_menu()
        self.assertEqual(
            self.page.query_selector_all("#lastCommandMenu .lastcmd-item-latest"), [])

    def test_the_chosen_row_is_marked_selected(self):
        self._open_menu()
        selected = self.page.eval_on_selector_all(
            '#lastCommandMenu .lastcmd-item[aria-selected="true"] .lastcmd-item-text',
            "els => els.map(e => e.textContent.trim())")
        self.assertEqual(selected, [PROMPTS[-1]])

    # ── dismissal ──────────────────────────────────────────────────────────────

    def test_escape_closes_it(self):
        self._open_menu()
        self.page.keyboard.press("Escape")
        self._wait_closed()
        self.assertEqual(
            self.page.get_attribute("#lastCommandText", "aria-expanded"), "false")

    def test_a_click_outside_closes_it(self):
        # The topbar, not the message area: the picker drops *over* the messages,
        # so a click there lands on the popover itself and is not "outside" at
        # all -- playwright reports the menu intercepting its own dismissal.
        self._open_menu()
        self.page.click("#topbarTitle")
        self._wait_closed()

    def test_clicking_the_trigger_again_closes_it(self):
        self._open_menu()
        self.page.click("#lastCommandText")
        self._wait_closed()

    def test_arrow_keys_move_between_rows(self):
        """The picker is reachable without a pointer, which half the devices
        this console is used from do not have in a usable form."""
        self._open_menu()
        first = self.page.evaluate("document.activeElement?.textContent?.trim()")
        self.page.keyboard.press("ArrowDown")
        second = self.page.evaluate("document.activeElement?.textContent?.trim()")
        self.assertNotEqual(first, second)
        self.assertTrue(self.page.evaluate(
            "document.activeElement?.classList.contains('lastcmd-item')"))

    # ── the thing that must never regress ──────────────────────────────────────

    def test_a_prompt_that_looks_like_markup_stays_text(self):
        """Every row and the strip render text the user typed.

        The payload is seeded *inside* the ten (see MARKUP_AT) so the picker
        actually renders it -- asserted through the DOM, because the property is
        that no element was ever created from it, and asserted as text so a row
        that quietly dropped the content would not pass either.
        """
        self._open_menu()
        rows = self._rows()
        self.assertIn(MARKUP_PROMPT, rows,
                      "the payload was not rendered at all, so this proves nothing")
        self.assertEqual(
            self.page.query_selector_all("#lastCommandMenu img"), [],
            "a prompt was interpreted as markup inside the picker")

        # And in the strip itself, by pinning it there.
        self.page.click(
            f'#lastCommandMenu .lastcmd-item[data-index="{len(rows) - 1}"]')
        self._wait_closed()
        self.assertEqual(self._strip_text(), MARKUP_PROMPT)
        self.assertEqual(
            self.page.query_selector_all("#lastCommandBar img"), [],
            "a prompt was interpreted as markup inside the strip")

    def test_the_page_reports_no_errors_while_using_it(self):
        self._open_menu()
        wanted = EXPECTED_ROWS[2]
        self.page.click(f'#lastCommandMenu .lastcmd-item:has-text("{wanted}")')
        self._wait_closed()
        self._open_menu()
        self.page.keyboard.press("Escape")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
