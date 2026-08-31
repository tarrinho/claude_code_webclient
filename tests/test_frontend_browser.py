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


def _driver_status() -> tuple[bool, str]:
    """Whether playwright's own node driver can start, and why not if it can't.

    Importing playwright proves nothing: it shells out to a node binary it
    ships itself, and where that is missing it falls back to /usr/bin/node and
    dies at launch. Guarding on the import alone made every test here raise
    FileNotFoundError on machines without a system node instead of skipping.

    compute_driver_executable is private and returned a bare string in older
    releases, hence the isinstance check -- do not "simplify" it to one path.
    The reason is returned so the skip message says `playwright install`
    rather than something unactionable.
    """
    try:
        from playwright._impl._driver import compute_driver_executable
    except Exception as exc:  # noqa: BLE001 -- any import failure means unusable
        return False, f"playwright not importable: {exc.__class__.__name__}"
    try:
        parts = compute_driver_executable()
    except Exception as exc:  # noqa: BLE001
        return False, f"driver path unresolvable: {exc.__class__.__name__}"
    for path in (parts if isinstance(parts, (list, tuple)) else [parts]):
        if not os.path.exists(path):
            return False, f"driver missing: {path} (try: playwright install)"
    return True, "ok"


DRIVER_OK, DRIVER_WHY = _driver_status()

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
BOOT_TIMEOUT_S = 30


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _BrowserFixture(unittest.TestCase):
    """Server + browser lifecycle. No tests of its own.

    Kept separate so a second suite can reuse it: subclassing a class that has
    test methods re-runs every one of them under the new name.
    """

    @classmethod
    def setUpClass(cls):
        # Explicit try/except rather than addClassCleanup. The boot loop below
        # raises and tearDownClass does not run when setUpClass does, so the
        # server and temp dir would leak -- but addClassCleanup is the wrong
        # cure here: unittest keeps class cleanups in a list shared by every
        # TestCase, so entries registered by one class can be drained while
        # another is still running. That terminated a live server mid-class,
        # which showed up as SIGTERM (exit -15) in the middle of a passing run.
        # Releasing them here, on the one path that can leak, keeps the
        # lifetime owned by this class alone.
        cls.tmp = None
        cls.server = None
        cls.log_handle = None
        try:
            cls._start()
        except BaseException:
            cls._release()
            raise

    @classmethod
    def _start(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "projects").mkdir()
        # A home of its own. The supervisor does not read only its database: it
        # merges the live Claude CLI sessions under ~/.claude, so a server
        # pointed at the real home reports whatever the agents on this machine
        # happen to be doing. The alert tests assert on a *rise* in the waiting
        # count, and an unrelated session answering a question in the same poll
        # window cancels the rise -- which is how a correct notification test
        # spent 90 seconds waiting for a notification that had already been
        # netted out. This also keeps the suite from reading the developer's
        # own transcripts.
        home = tmp / "home"
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "projects").mkdir(parents=True)
        cls.port = _free_port()
        cls.password = secrets.token_urlsafe(12)
        env = {
            **os.environ,
            "HOME": str(home),
            # Its own application log. logging.conf names an absolute path, so
            # without this every server started here appends to the production
            # log -- which is how that file came to hold interleaved and
            # future-stamped lines from tests nobody was watching. Inside tmp,
            # which already exists: RotatingFileHandler will not create a
            # missing directory and the server would exit during setUpClass.
            "WC_LOG_FILE": str(tmp / "app.log"),
            # Defaults to the home above, which the projects root is not under.
            "WC_PROJECTS_ROOT_BASE": str(tmp),
            "WC_DB_PATH": str(tmp / "wc.db"),
            "WC_PROJECTS_ROOT": str(tmp / "projects"),
            "WC_SESSION_SECRET": secrets.token_urlsafe(32),
            "WC_ADMIN_PASSWORD": cls.password,
            # No proxy: this exercises the UI, never a real turn.
            "WC_PROXY_ENABLED": "0",
            # The test server is plain HTTP on loopback.
            "WC_COOKIE_ALLOW_INSECURE": "1",
        }
        # A file, not a PIPE. Nothing here ever reads the server's output, and
        # an unread pipe holds only 64K -- past that uvicorn blocks forever on
        # its own access log and stops answering, which surfaces as a browser
        # test failing on a server that looks alive. A file also survives the
        # process, so _server_log below can say what happened.
        cls.log_path = tmp / "server.log"
        cls.log_handle = cls.log_path.open("wb")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, env=env,
            stdout=cls.log_handle, stderr=subprocess.STDOUT,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + BOOT_TIMEOUT_S
        while time.time() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError(f"server exited during boot:\n{cls._server_log()}")
            try:
                urllib.request.urlopen(f"{cls.base}/login", timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        else:  # pragma: no cover - boot failure path
            raise RuntimeError("server did not start")

    @classmethod
    def _server_log(cls, lines: int = 40) -> str:
        try:
            if cls.log_handle is not None:
                cls.log_handle.flush()
        except (ValueError, OSError):  # already closed by _release
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
        """Give back whatever setUpClass actually got. Idempotent, and safe on
        a partly-built class: it runs both from tearDownClass and from the
        failure path in setUpClass, where any of these may still be None."""
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
        # addCleanup, not tearDown: unittest skips tearDown when setUp raises,
        # and _login below raises on any slow boot. A leaked playwright is not
        # merely untidy -- its greenlet loop stays flagged as the *running*
        # asyncio loop for this thread, so every IsolatedAsyncioTestCase that
        # follows dies on "Runner.run() cannot be called from a running event
        # loop". One login timeout took out 850 unrelated tests that way.
        # Cleanups run last-registered-first, hence stop() before close().
        # Say so plainly when the server has died. Otherwise every remaining
        # test in the class fails with ERR_CONNECTION_REFUSED, which names the
        # port and nothing else.
        if self.server.poll() is not None:
            self.fail(
                f"the test server exited with code {self.server.returncode}:\n"
                f"{self._server_log()}"
            )
        self.errors: list[str] = []
        self._pw = sync_playwright().start()
        self.addCleanup(self._pw.stop)
        self.browser = self._pw.chromium.launch(
            executable_path=CHROMIUM, args=["--no-sandbox"]
        )
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error"
            else None,
        )
        self._login()

    def _login(self):
        page = self.page
        # domcontentloaded, not networkidle: this app polls the supervisor, the
        # chat list and pending questions, and the supervisor pane holds an SSE
        # stream open -- so the network is never idle and that wait can only
        # ever time out. The app-shell wait below is the real readiness signal.
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        # By id: the theme toggle is also type=submit and comes first in the DOM.
        page.click("#submitBtn")
        # networkidle resolves before the module renders the toolbar, and the
        # login page has its own template -- a query here would silently run
        # against the wrong document.
        page.wait_for_selector("#settingsBtn", timeout=15_000)

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


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class BackendsPanelBrowserTests(_BrowserFixture):
    """The Backends tab, exercised the way a person exercises it."""

    def test_settings_opens_without_script_errors(self):
        self._open_backends()
        self.assertTrue(self.page.is_visible("#panelBackends"))
        self.assertEqual(self.errors, [])

    def test_tabs_are_the_merged_set(self):
        self._open_backends()
        tabs = [t.inner_text() for t in self.page.query_selector_all(".settings-tab")]
        self.assertEqual(
            tabs, ["Backends", "Usage", "Statistics", "Server", "Skills", "App"]
        )
        # The old standalone Models tab is what got merged into Backends; a
        # backend and the models it serves are one thing.
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

        self.page.reload(wait_until="domcontentloaded")
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


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorBrowserTests(_BrowserFixture):
    """The supervisor section, driven the way the user drives it.

    Inherits the server/browser fixture. The seeded conversation ends on an
    assistant reply that was never read, so it must appear as waiting.
    """

    # Scoped to the desktop list: the markup renders two sidebars, and the
    # mobile one is hidden -- an unscoped selector finds its rows first and
    # every click times out on an invisible element.
    DESKTOP = "#chatListDesktop"

    def _seed_waiting_chat(self):
        """Insert a conversation ending on an unread assistant reply.

        A fresh id and a current timestamp per test: the class shares one
        database, so a fixed id would inherit the read mark left by whichever
        test ran first and the row would never be waiting.
        """
        import datetime
        import sqlite3
        now = datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.chat_id = f"sup-{secrets.token_hex(4)}"
        self.chat_title = f"Waiting {self.chat_id}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (self.chat_id, self.chat_title, stamp, stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (self.chat_id, "user", "do it", stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (self.chat_id, "assistant", "Which way do you want it?", stamp),
        )
        con.commit()
        con.close()

    def _supervisor_rows(self):
        return self.page.query_selector_all(f"{self.DESKTOP} .supervisor-item")

    def _badge(self):
        node = self.page.query_selector(f"{self.DESKTOP} .supervisor-badge")
        return int(node.inner_text()) if node else 0

    def _load(self):
        self._seed_waiting_chat()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .supervisor-item", timeout=15_000)

    def test_supervisor_sits_above_the_other_sections(self):
        self._load()
        labels = [
            e.inner_text().split("·")[0].split("\n")[0].strip()
            for e in self.page.query_selector_all(f"{self.DESKTOP} .chat-section-label")
        ]
        self.assertTrue(labels, "the sidebar rendered no sections")
        self.assertEqual(labels[0].lower(), "supervisor")
        self.assertEqual(self.errors, [])

    def test_badge_counts_the_waiting_agents(self):
        self._load()
        self.assertGreaterEqual(self._badge(), 1)
        self.assertEqual(self._badge(), len(self._supervisor_rows()))

    def test_a_waiting_row_shows_what_the_agent_last_said(self):
        self._load()
        row = next(
            r for r in self._supervisor_rows()
            if self.chat_title in r.query_selector(".chat-title").inner_text()
        )
        self.assertIn("web", row.query_selector(".chat-meta").inner_text())
        self.assertEqual(
            row.query_selector(".chat-snippet").inner_text().strip(),
            "Which way do you want it?",
        )

    def test_jumping_opens_the_conversation(self):
        """Get me there in one tap.

        The badge deliberately does NOT clear here: opening a conversation is
        not answering its question, and a highlight that vanished on a glance
        was the complaint that changed this rule.
        """
        self._load()
        before = self._badge()
        row = next(
            r for r in self._supervisor_rows()
            if self.chat_title in r.query_selector(".chat-title").inner_text()
        )
        row.query_selector(".chat-open").click()
        self.page.wait_for_timeout(3500)

        # The conversation name lives in the workspace strip, not the topbar.
        self.assertEqual(self.page.query_selector("#workspaceName").inner_text(), self.chat_title)
        self.assertIn("Which way do you want it?", self.page.query_selector("#messagesArea").inner_text())
        self.assertEqual(
            self._badge(), before,
            "an unanswered question must survive being looked at",
        )
        self.assertEqual(self.errors, [])

    def test_the_heading_offers_a_link_to_the_supervisor(self):
        """Present, labelled, and pointing at the supervisor page."""
        self._load()
        heading = self.page.query_selector(f"{self.DESKTOP} .supervisor-label")
        self.assertIsNotNone(heading, "the Supervisor section did not render")
        link = heading.query_selector(".supervisor-open")
        self.assertIsNotNone(link, "no link to the supervisor in its own section")
        self.assertEqual(link.get_attribute("aria-label"), "Open the supervisor")

    def test_clearing_leaves_the_link_reachable(self):
        """Nothing waiting is exactly when you want to go and look."""
        self._load()
        clear = self.page.query_selector(f"{self.DESKTOP} .supervisor-clear")
        if clear:
            clear.click()
            self.page.wait_for_timeout(2000)
        self.assertIsNotNone(
            self.page.query_selector(f"{self.DESKTOP} .supervisor-open"),
            "the link vanished once the queue emptied",
        )

    def _pane(self):
        return self.page.query_selector("#supervisorPane")

    def test_the_topbar_control_opens_the_pane_not_a_new_page(self):
        """It used to navigate away, which cost the sidebar and a reload.

        Scoped to the pane deliberately: what loads *inside* the frame is the
        supervisor's own page, with its own engine and SSE stream. Driving that
        from here made this suite depend on another subsystem's behaviour and
        destabilised every test after it.
        """
        self._load()
        before = self.page.url
        self.page.click("#supervisorBtn")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)
        self.assertEqual(self.page.url, before, "it navigated instead of embedding")
        self.assertFalse(self.page.is_visible("#messagesWrap"),
                         "the conversation area is still showing behind it")
        self.assertIn(
            "supervisor.html",
            self.page.query_selector("#supervisorFrame").get_attribute("src"),
        )

    def test_the_sidebar_link_opens_the_same_pane(self):
        self._load()
        self.page.click(f"{self.DESKTOP} .supervisor-open")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)

    def test_the_conversation_list_stays_visible_beside_it(self):
        """The reason for embedding: you can still see who is waiting."""
        self._load()
        self.page.click("#supervisorBtn")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)
        self.assertTrue(self.page.is_visible(self.DESKTOP),
                        "the sidebar went away, which defeats the point")

    def test_closing_returns_to_the_conversation(self):
        self._load()
        self.page.click("#supervisorBtn")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)
        self.page.click("#supervisorPaneClose")
        # state="hidden": the default waits for *visible*, so asserting on a
        # hidden element that way can only ever time out -- the element was
        # correctly hidden the whole time.
        self.page.wait_for_selector("#supervisorPane", state="hidden", timeout=10_000)
        self.assertTrue(self.page.is_visible("#messagesWrap"))

    def test_supervisor_rows_are_not_draggable(self):
        """They point at conversations owned by other sections; a drop here
        would ask the reorder handler to reorder a container it does not own."""
        self._load()
        for row in self._supervisor_rows():
            self.assertNotEqual(row.get_attribute("draggable"), "true")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class DeviceAlertBrowserTests(_BrowserFixture):
    """Alerting the device, which is the point of viewing this on a phone.

    Three levels: the tab title (always), a system notification (permission),
    and a vibration (Android). Only the title is testable without granting
    permission, so this grants it and captures the Notification constructor.
    """

    DESKTOP = "#chatListDesktop"

    def _seed(self, role="assistant", text="Which way do you want it?"):
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"al-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,created_at,"
            "updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Alert {chat_id}", stamp, stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, role, text, stamp),
        )
        con.commit()
        con.close()
        return chat_id

    def test_tab_title_carries_the_count_without_any_permission(self):
        """The one level that always works, on any device, with no prompt."""
        self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .supervisor-item", timeout=15_000)
        self.assertRegex(self.page.title(), r"^\(\d+\) WebConsole$")

    def _answer(self, chat_id):
        """Reply as the user, which is what actually retires a question."""
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, "user", "the second one", stamp),
        )
        con.commit()
        con.close()

    def test_the_count_survives_merely_looking_at_it(self):
        """Opening a conversation is not answering its question.

        Clearing on read let an unanswered question be dismissed by glancing
        at it, which is the opposite of what the badge is for.
        """
        chat_id = self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .supervisor-item", timeout=15_000)
        before = self.page.title()
        self.assertRegex(before, r"^\(\d+\) WebConsole$")

        row = next(
            r for r in self.page.query_selector_all(f"{self.DESKTOP} .supervisor-item")
            if chat_id[-8:] in r.query_selector(".chat-title").inner_text()
            or "Alert" in r.query_selector(".chat-title").inner_text()
        )
        row.query_selector(".chat-open").click()
        self.page.wait_for_timeout(3000)
        self.assertRegex(self.page.title(), r"^\(\d+\) WebConsole$",
                         "the badge cleared just from opening the conversation")

    def test_the_count_clears_once_the_question_is_answered(self):
        chat_id = self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .supervisor-item", timeout=15_000)
        self.assertRegex(self.page.title(), r"^\(\d+\) WebConsole$")

        before = int(self.page.title().split(")")[0].lstrip("("))
        self._answer(chat_id)
        # The class shares a database, so other tests' unanswered questions are
        # legitimately still waiting: assert this one left, not that the queue
        # emptied. Waits out a poll rather than assuming an instant refresh.
        self.page.wait_for_function(
            "(n) => { const m = /^\\((\\d+)\\)/.exec(document.title);"
            "  return m ? Number(m[1]) < n : true; }",
            arg=before, timeout=40_000,
        )

    def test_the_alert_toggle_is_offered(self):
        self.assertFalse(self.page.query_selector("#alertToggle").is_hidden())

    def test_a_notification_fires_for_a_new_waiting_agent(self):
        """Granting permission and capturing the constructor, so this asserts a
        notification was really raised rather than that the code looks right."""
        self.browser.contexts[0].grant_permissions(["notifications"])
        # An alert fires on a *rise* in the waiting count, so the first poll has
        # to have landed before the seed below -- otherwise the seed becomes the
        # baseline and no rise ever happens. Waiting a fixed beat cannot detect
        # that: with an empty queue the title reads "WebConsole" both before the
        # poll and after it. Seeding one question first makes the baseline
        # visible, so the count in the title is proof the poll ran.
        # A real question: the classifier counts only those, which is exactly
        # what test_routine_output_raises_no_alert_at_all pins from the other
        # side. A statement here would never reach the badge at all.
        self._seed(text="Shall I start with the first one?")
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_function(
            "() => /^\\(\\d+\\)/.test(document.title)", timeout=40_000
        )
        # Record every Notification the page constructs.
        self.page.evaluate("""() => {
            window.__notes = [];
            const Real = window.Notification;
            window.Notification = function (title, opts) {
                window.__notes.push({title, body: (opts || {}).body});
            };
            window.Notification.permission = 'granted';
            window.Notification.requestPermission = () => Promise.resolve('granted');
        }""")
        self.page.evaluate("() => localStorage.setItem('wc_alerts', 'on')")
        # The page must not be the focused thing, or alerting would be noise.
        self.page.evaluate("() => Object.defineProperty(document, 'hasFocus', {value: () => false})")
        self._seed()
        # The supervisor polls every 15s and three browser suites contend for
        # this machine, so allow several cycles rather than assuming the first
        # one lands promptly.
        self.page.wait_for_function(
            "() => window.__notes && window.__notes.length > 0", timeout=90_000
        )
        notes = self.page.evaluate("() => window.__notes")
        self.assertTrue(notes)
        self.assertEqual(notes[0]["title"], "WebConsole")
        self.assertIn("needs an answer", notes[0]["body"])

    def test_no_notification_while_the_page_is_in_front_of_you(self):
        """Being looked at already counts as being told."""
        self.browser.contexts[0].grant_permissions(["notifications"])
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.evaluate("""() => {
            window.__notes = [];
            window.Notification = function (t, o) { window.__notes.push({t, o}); };
            window.Notification.permission = 'granted';
        }""")
        self.page.evaluate("() => localStorage.setItem('wc_alerts', 'on')")
        self.page.evaluate("() => Object.defineProperty(document, 'hasFocus', {value: () => true})")
        self._seed()
        self.page.wait_for_timeout(20_000)
        self.assertEqual(self.page.evaluate("() => window.__notes.length"), 0)

    def test_routine_output_raises_no_alert_at_all(self):
        """Pedro's rule: only when information is required or important."""
        self.browser.contexts[0].grant_permissions(["notifications"])
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.evaluate("""() => {
            window.__notes = [];
            window.Notification = function (t, o) { window.__notes.push({t, o}); };
            window.Notification.permission = 'granted';
        }""")
        self.page.evaluate("() => localStorage.setItem('wc_alerts', 'on')")
        self.page.evaluate("() => Object.defineProperty(document, 'hasFocus', {value: () => false})")
        # Wait out the first supervisor poll before reading the baseline. Taken
        # straight after domcontentloaded it is the static title from the HTML,
        # so the poll landing -- not this test's seed -- moved the count, and
        # the comparison below failed whenever an earlier test in the class had
        # left a question waiting.
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_timeout(1500)
        before = self.page.title()
        self._seed(text="Done. Suite is green, ruff clean.")
        self.page.wait_for_timeout(20_000)
        self.assertEqual(self.page.evaluate("() => window.__notes.length"), 0)
        # Routine output must not move the count either. Compared against the
        # title we started with, since questions left unanswered by other tests
        # in this class are legitimately still counted.
        self.assertEqual(self.page.title(), before)


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class ServerPanelBrowserTests(_BrowserFixture):
    """The Server tab refreshes itself, and stops when you leave it.

    Asserted by counting the requests the page actually makes, because the
    thing that was wrong was not visible in the markup: the panel rendered
    correctly and then never changed, so a reading from when the tab was opened
    sat there looking current. The poll is 30s, so these wait a cycle out.
    """

    POLL_S = 30

    def setUp(self):
        super().setUp()
        self.system_calls: list[str] = []
        self.page.on(
            "request",
            lambda r: self.system_calls.append(r.url)
            if "/api/system" in r.url
            else None,
        )

    def _open_server_tab(self):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector("#tabServer", timeout=10_000)
        self.page.click("#tabServer")
        self.page.wait_for_selector("#panelServer:not([hidden])", timeout=10_000)

    def test_opening_the_tab_reads_the_host(self):
        self._open_server_tab()
        self.page.wait_for_function(
            "() => document.querySelector('#serverBody .srv-cards')", timeout=15_000
        )
        self.assertTrue(self.system_calls, "the panel never asked for host stats")

    def test_the_reading_refreshes_without_being_asked(self):
        self._open_server_tab()
        self.page.wait_for_timeout(2000)
        before = len(self.system_calls)
        self.assertGreater(before, 0)
        self.page.wait_for_timeout((self.POLL_S + 8) * 1000)
        self.assertGreater(
            len(self.system_calls), before,
            "the panel made no further request: it is frozen at whatever it "
            "read when the tab was opened",
        )

    def test_a_refresh_leaves_the_reading_on_screen(self):
        """No skeleton flash: a panel that updates must not look like it broke."""
        self._open_server_tab()
        self.page.wait_for_function(
            "() => document.querySelector('#serverBody .srv-cards')", timeout=15_000
        )
        self.page.wait_for_timeout((self.POLL_S + 8) * 1000)
        self.assertIsNotNone(self.page.query_selector("#serverBody .srv-cards"))
        self.assertIsNone(self.page.query_selector("#serverBody .skill-skeleton"))

    def test_leaving_the_tab_stops_the_polling(self):
        """Otherwise every settings visit leaves another interval behind."""
        self._open_server_tab()
        self.page.wait_for_timeout(2000)
        self.page.click("#tabBackends")
        self.page.wait_for_selector("#panelBackends:not([hidden])", timeout=10_000)
        self.page.wait_for_timeout(1500)
        settled = len(self.system_calls)
        self.page.wait_for_timeout((self.POLL_S + 8) * 1000)
        self.assertEqual(len(self.system_calls), settled,
                         "the panel kept polling after it was closed")

    def test_the_controls_default_to_a_day_in_half_hours(self):
        """The 24h view exists to show evolution; by-day would be one bar."""
        self._open_server_tab()
        self.assertEqual(self.page.input_value("#serverRange"), "1")
        self.assertEqual(self.page.input_value("#serverBucket"), "halfhour")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorStreamBrowserTests(_BrowserFixture):
    """Switching supervisors must close the stream it leaves behind.

    connectSSE() used to construct `new EventSource(url, {signal})` and tear
    the previous stream down with `AbortController.abort()`. EventSource's init
    dictionary accepts only `withCredentials`; a `signal` member is ignored, so
    the teardown was inert -- every switch left a stream open on both ends and
    the stale one kept delivering into handleSSEEvent for a supervisor the user
    had left.

    Nothing about that is visible in the source: the code reads as if it tears
    down. readyState is the only thing that settles it, which is why this test
    drives a browser rather than reading the file.
    """

    def _make_supervisor(self, title: str) -> str:
        csrf = next(c["value"] for c in self.page.context.cookies()
                    if c["name"] == "wc_csrf")
        return self.page.evaluate("""async ([csrf, title]) => {
            const r = await fetch('/api/supervisors', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                body: JSON.stringify({title})});
            return (await r.json()).id;
        }""", [csrf, title])

    def _open_page_with_two(self):
        self._make_supervisor("First supervisor")
        self._make_supervisor("Second supervisor")
        self.page.goto(f"{self.base}/supervisor", wait_until="domcontentloaded")
        self.page.wait_for_selector(".supervisor-list-item", timeout=15_000)
        rows = self.page.locator(".supervisor-list-item")
        # A locator, not element handles: the list re-renders on every refresh
        # and on selection, which detaches any handle taken beforehand.
        self.assertGreaterEqual(rows.count(), 2, "both supervisors should be listed")
        return rows

    def test_switching_closes_the_previous_stream(self):
        """readyState 2 is CLOSED. It stayed at 1 with the AbortController."""
        rows = self._open_page_with_two()
        rows.nth(0).click()
        self.page.wait_for_function(
            "() => window._supervisorSSE", timeout=10_000
        )
        self.page.evaluate("() => { window.__first = window._supervisorSSE; }")

        rows.nth(1).click()
        self.page.wait_for_function(
            "() => window._supervisorSSE && window._supervisorSSE !== window.__first",
            timeout=10_000,
        )
        first_state = self.page.evaluate("() => window.__first.readyState")
        self.assertEqual(
            first_state, 2,
            "the stream for the supervisor we left is still open: every switch "
            "leaks a connection and keeps delivering its events",
        )

    def test_the_new_stream_is_the_live_one(self):
        rows = self._open_page_with_two()
        rows.nth(0).click()
        self.page.wait_for_function("() => window._supervisorSSE", timeout=10_000)
        rows.nth(1).click()
        self.page.wait_for_timeout(1500)
        self.assertIn(
            self.page.evaluate("() => window._supervisorSSE.readyState"), (0, 1),
            "the current supervisor has no live stream",
        )

    def test_the_list_refreshes_with_nothing_selected(self):
        """The 30s refresh used to be gated on having a selection.

        That is the state the page opens in, so a supervisor created anywhere
        else never appeared until a manual reload -- and staleness you cannot
        see is worse than a list that never claims to be current.
        """
        source = (ROOT / "web" / "supervisor.js").read_text(encoding="utf-8")
        start = source.index("setInterval(")
        body = source[start:source.index("30000", start)]
        self.assertIn("loadSupervisors()", body)
        guard = body.find("if (activeSupervisorId)")
        self.assertTrue(
            guard == -1 or body.index("loadSupervisors()") < guard,
            "loadSupervisors() is inside the has-a-selection guard again",
        )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorDismissBrowserTests(_BrowserFixture):
    """Every highlighted agent can be taken out of the highlights on its own.

    The heading's clear-all was the only control: dealing with one agent meant
    silencing every other, including questions still unanswered. These drive
    the button rather than reading the markup, because what matters is that the
    row goes away and the others stay.
    """

    DESKTOP = "#chatListDesktop"

    def _seed(self, text="Which way do you want it?"):
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"dis{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,created_at,"
            "updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Waiting {chat_id}", stamp, stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, "assistant", text, stamp),
        )
        con.commit()
        con.close()
        return chat_id

    def _rows(self):
        return self.page.locator(f"{self.DESKTOP} .supervisor-item")

    def _load_with(self, count):
        for _ in range(count):
            self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .supervisor-item", timeout=15_000)
        return self._rows()

    def test_every_highlighted_row_offers_one(self):
        rows = self._load_with(2)
        self.assertGreaterEqual(rows.count(), 2)
        for index in range(rows.count()):
            with self.subTest(row=index):
                self.assertEqual(
                    rows.nth(index).locator(".supervisor-dismiss").count(), 1,
                    "a highlighted agent with no way to dismiss it on its own",
                )

    def test_it_is_labelled_for_a_screen_reader(self):
        """"✕" alone announces as nothing useful."""
        rows = self._load_with(1)
        label = rows.nth(0).locator(".supervisor-dismiss").get_attribute("aria-label")
        self.assertIn("highlights", (label or "").lower())

    def test_clicking_it_removes_that_row(self):
        rows = self._load_with(2)
        before = rows.count()
        self.assertGreaterEqual(before, 2)
        rows.nth(0).locator(".supervisor-dismiss").click()
        self.page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length < n",
            arg=[f"{self.DESKTOP} .supervisor-item", before],
            timeout=15_000,
        )

    def test_it_leaves_the_other_agents_alone(self):
        """The whole reason for a per-row control rather than clear-all."""
        rows = self._load_with(3)
        before = rows.count()
        kept = rows.nth(1).locator(".chat-title").inner_text()
        rows.nth(0).locator(".supervisor-dismiss").click()
        self.page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length < n",
            arg=[f"{self.DESKTOP} .supervisor-item", before],
            timeout=15_000,
        )
        remaining = self.page.locator(f"{self.DESKTOP} .supervisor-item .chat-title")
        titles = [remaining.nth(i).inner_text() for i in range(remaining.count())]
        self.assertIn(kept, titles, "dismissing one silenced the others too")

    def test_it_does_not_open_the_conversation(self):
        """The button sits inside the row, whose click opens the chat.

        Without stopPropagation the control would open the very conversation
        you had just asked to stop being shown -- and, since opening does not
        dismiss, the row would come straight back.
        """
        rows = self._load_with(2)
        rows.nth(0).locator(".supervisor-dismiss").click()
        self.page.wait_for_timeout(1500)
        self.assertTrue(
            self.page.is_visible("#messagesArea .empty-state")
            or not self.page.is_visible("#composerInput"),
            "dismissing opened the conversation instead of only removing it",
        )

    def test_the_dismissal_survives_a_reload(self):
        """A row that comes back on refresh was never really dismissed."""
        rows = self._load_with(2)
        before = rows.count()
        gone = rows.nth(0).locator(".chat-title").inner_text()
        rows.nth(0).locator(".supervisor-dismiss").click()
        self.page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length < n",
            arg=[f"{self.DESKTOP} .supervisor-item", before],
            timeout=15_000,
        )
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .supervisor-item", timeout=15_000)
        self.page.wait_for_timeout(1000)
        titles = self.page.locator(f"{self.DESKTOP} .supervisor-item .chat-title")
        self.assertNotIn(
            gone, [titles.nth(i).inner_text() for i in range(titles.count())],
            "the dismissed agent reappeared after a reload",
        )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorRenameBrowserTests(_BrowserFixture):
    """Renaming a supervisor from the row it appears on.

    Driven in a browser rather than asserted against the source, because the
    two things most likely to break are invisible in the file: whether the
    30-second refresh wipes a half-typed name, and whether clicking the
    rename control also selects a supervisor the user was not looking at.
    Both read as fine in the source and are decided at runtime.
    """

    def _make_supervisor(self, title: str) -> str:
        csrf = next(c["value"] for c in self.page.context.cookies()
                    if c["name"] == "wc_csrf")
        return self.page.evaluate("""async ([csrf, title]) => {
            const r = await fetch('/api/supervisors', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                body: JSON.stringify({title})});
            return (await r.json()).id;
        }""", [csrf, title])

    def _titles(self) -> list[str]:
        return self.page.locator(".supervisor-list-item .sl-title").all_text_contents()

    def _open(self, *titles):
        for title in titles:
            self._make_supervisor(title)
        self.page.goto(f"{self.base}/supervisor", wait_until="domcontentloaded")
        self.page.wait_for_selector(".supervisor-list-item", timeout=15_000)

    def _begin_rename(self, title: str):
        """Click the rename control on the row named *title*.

        Addressed by name rather than by index on purpose. The server is shared
        across the class, so supervisors created by earlier tests are still
        listed and nth(0) is whichever one happens to sort first -- a first
        version of this used an index and failed as soon as a sibling test
        renamed something. A test whose subject depends on execution order is
        not testing what it says.
        """
        # Retried, because this list rebuilds on a 30-second timer as well as
        # on selection: the button can detach between being resolved and being
        # clicked, which surfaces as a click that never lands. Passing alone
        # and failing in the class is what that looks like.
        last = None
        for _ in range(4):
            try:
                row = self.page.locator(".supervisor-list-item", has_text=title)
                # force=True: the button is opacity:0 until the row is hovered,
                # which is a paint state rather than a hit-testing one.
                row.locator(".sl-rename").click(force=True, timeout=4_000)
                self.page.wait_for_selector(".sl-rename-input", timeout=4_000)
                return self.page.locator(".sl-rename-input")
            except Exception as exc:  # noqa: BLE001 -- retried below
                last = exc
                self.page.wait_for_timeout(400)
        raise AssertionError(f"could not open the rename control for {title!r}: {last}")

    def test_the_control_opens_an_input_holding_the_current_name(self):
        self._open("Renameable one")
        field = self._begin_rename("Renameable one")
        self.assertEqual(field.input_value(), "Renameable one")

    def test_enter_persists_the_new_name(self):
        self._open("Before rename")
        field = self._begin_rename("Before rename")
        field.fill("After rename")
        field.press("Enter")
        self.page.wait_for_selector(".sl-rename-input", state="detached", timeout=5_000)
        self.page.wait_for_function(
            "() => [...document.querySelectorAll('.sl-title')]"
            ".some(n => n.textContent === 'After rename')", timeout=10_000)
        # And it survives a reload, so it was stored rather than only painted.
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(".supervisor-list-item", timeout=15_000)
        self.assertIn("After rename", self._titles())

    def test_escape_abandons_the_edit(self):
        self._open("Keep this name")
        field = self._begin_rename("Keep this name")
        field.fill("Discarded")
        field.press("Escape")
        self.page.wait_for_selector(".sl-rename-input", state="detached", timeout=5_000)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(".supervisor-list-item", timeout=15_000)
        titles = self._titles()
        self.assertIn("Keep this name", titles)
        self.assertNotIn("Discarded", titles)

    def test_a_real_refresh_does_not_wipe_a_half_typed_name(self):
        """The hazard the guard exists for, proven against the real timer.

        loadSupervisors() rebuilds the list wholesale on a 30-second interval,
        so without suppressing it during an edit the input is replaced
        mid-typing -- silently, at a moment the user cannot predict.

        Slow on purpose. A first version called
        `window.loadSupervisors && window.loadSupervisors()`, but that function
        lives inside the module closure and is not on window, so the `&&`
        short-circuited and the test asserted that an input survived a refresh
        that never happened. It passed with the guard deleted. The defensive
        `&&` is what hid it.

        So the poll is now *observed* rather than assumed: the request counter
        below is what makes the wait meaningful, and without it this would be
        thirty seconds of proving nothing.
        """
        self._open("Mid-edit supervisor")
        polls = []
        self.page.on("request", lambda r: (
            polls.append(r.url) if r.url.endswith("/api/supervisors")
            and r.method == "GET" else None))

        field = self._begin_rename("Mid-edit supervisor")
        field.fill("Half typed")
        before = len(polls)

        # 30s interval; allow margin for a slow box under a full suite run.
        deadline = time.monotonic() + 45
        while len(polls) == before and time.monotonic() < deadline:
            self.page.wait_for_timeout(500)
        self.assertGreater(
            len(polls), before,
            "no refresh was observed in 45s, so this test proved nothing")

        self.assertEqual(
            self.page.locator(".sl-rename-input").count(), 1,
            "the refresh replaced the input the user was typing into")
        self.assertEqual(field.input_value(), "Half typed")

    def test_renaming_does_not_select_the_row(self):
        """The row's own click selects. Without stopPropagation, renaming a
        supervisor you are not looking at also switches you to it."""
        self._open("First one", "Second one")
        before = self.page.locator(".supervisor-list-item.active").count()
        self._begin_rename("First one")
        self.assertEqual(
            self.page.locator(".supervisor-list-item.active").count(), before,
            "opening the rename control changed the selection")

    def test_the_field_caps_at_the_length_the_server_stores(self):
        """The server slices titles to 200. Without a matching maxlength the
        user types past it and is truncated with no indication, so the rename
        reads as having half worked."""
        self._open("Capped supervisor")
        field = self._begin_rename("Capped supervisor")
        self.assertEqual(field.get_attribute("maxlength"), "200")


if __name__ == "__main__":
    unittest.main()
