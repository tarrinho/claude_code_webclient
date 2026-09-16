#!/usr/bin/env python3
"""Capture a page or element from the working tree, and optionally file the
result in the image gallery.

Why this exists: a design screenshot of a settings panel was captured once
with a throwaway script under /tmp, which was then deleted -- and the next
person who needed one (the panel changes; the gallery image goes stale) had
to rediscover, the hard way, three things that are not obvious from the UI
alone:

1. The live service serves a *deployed release*, not this checkout. Pointing
   a browser at the running app previews yesterday's code. Seeing an
   undeployed change means booting `app:app` from this working tree against a
   throwaway database -- exactly what tests/test_frontend_browser.py's
   `_BrowserFixture` already does for the browser test suite, so this script
   copies that recipe rather than inventing a second one.
2. A fresh temp database is empty, so anything driven by data (the Settings >
   Delegation panel, for one) renders blank. `bin/wc-seed-delegation.py`
   fills the one table that panel reads; `--seed-delegation` runs it against
   this run's throwaway database before the browser ever opens a page.
3. A plain Playwright element screenshot only captures what happened to be
   scrolled into view. Settings dialogs on this app clip their contents with
   inline style on `.settings-dialog`, `.settings-body` and `#settingsDialog`
   -- left alone, an element screenshot of a panel inside that dialog comes
   out cropped, with a stray duplicate of the fixed Close button baked in.
   This clears that inline clip via `page.evaluate` before every capture,
   not only when a settings tab is requested, because a selector that
   matches nothing on a given page is a harmless no-op.

Registration goes through `routes/db_images.generated_image_record`, the same
function the app itself uses to record a turn's images -- never a raw INSERT
-- and is opt-in via `--register`, never the default: that function's target
database is `config.DB_PATH` unless overridden, which on a real deployment
*is* the production database, and a tool that writes there unless told
otherwise is the wrong shape for something run by hand. Registration also
never calls `db.init()`: that function migrates, and a registration is a
single insert through an existing table, not an occasion to run a migration
against a live database. It connects directly and assigns `db.db_conn`
instead.

Usage
-----
    .venv/bin/python bin/wc-screenshot.py --out /tmp/panel.png \\
        --settings-tab delegation --seed-delegation

    .venv/bin/python bin/wc-screenshot.py --out /tmp/panel.png \\
        --settings-tab delegation --seed-delegation \\
        --register --chat-title "Settings > Delegation, redesign" \\
        --work-dir /home/kali/projects/screenshots-2026-09-16

The second form is the one that writes to production -- run it only once the
first form's PNG has been looked at and is the right picture, since a small
but non-empty PNG (a cropped panel, a stray element) passes the size guard
below without being correct.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
from routes import db_images  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: Seconds to wait for the throwaway server to answer /login. Same figure
#: tests/test_frontend_browser.py uses for the same boot.
DEFAULT_BOOT_TIMEOUT_S = 30

#: The owner every existing gallery row uses for images that are not turn
#: output belonging to a real chat -- see the rows already in generated_images.
DEFAULT_OWNER_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

#: Below this many bytes, a PNG is treated as a failed capture rather than a
#: small-but-real one -- a blank page, a 1x1 clip, or a 0-byte file left by a
#: crashed browser all land well under it. Chosen from the smallest genuine
#: panel capture on record (tens of kilobytes); a few hundred bytes of PNG
#: framing with no real content inside it does not reach this floor.
DEFAULT_MIN_BYTES = 2048

#: Elements whose inline scroll-clip must be cleared before a settings-panel
#: screenshot -- see the module docstring, point 3. Matching nothing on a
#: given page is harmless, so these are applied unconditionally rather than
#: only when --settings-tab is given.
DEFAULT_CLEAR_SCROLL_CLIP = [".settings-dialog", ".settings-body", "#settingsDialog"]

#: Mirrors app.js's `_switchTab` panel map -- the id to wait for once a tab
#: has been clicked, and the whole set of tab names this script accepts.
TAB_PANEL_IDS = {
    "backends": "panelBackends", "usage": "panelUsage", "stats": "panelStats",
    "server": "panelServer", "skills": "panelSkills", "app": "panelApp",
    "images": "panelImages", "specs": "panelSpecs", "delegation": "panelDelegation",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Capture a page or element from a server booted off this "
                    "working tree, and optionally register it in the image "
                    "gallery.",
    )
    p.add_argument(
        "--out", required=True, type=Path,
        help="PNG path to write the capture to. Required, so a capture "
             "never lands somewhere implied rather than chosen.",
    )
    p.add_argument(
        "--path", default="/",
        help="URL path to open after logging in, before any --settings-tab "
             "click (default: %(default)s, the app shell).",
    )
    p.add_argument(
        "--selector", default=None,
        help="CSS selector of the element to screenshot. Omit for a "
             "full-page capture.",
    )
    p.add_argument(
        "--settings-tab", default=None, choices=sorted(TAB_PANEL_IDS),
        help="Open Settings and switch to this tab before capturing. Waits "
             "for the matching panel to become visible.",
    )
    p.add_argument(
        "--seed-delegation", action="store_true",
        help="Seed the delegation_capability table on this run's throwaway "
             "database first, via bin/wc-seed-delegation.py. Needed for the "
             "Delegation panel (and anything else reading that table) to "
             "render anything but blank on a fresh database.",
    )
    p.add_argument(
        "--clear-scroll-clip", action="append",
        default=list(DEFAULT_CLEAR_SCROLL_CLIP), metavar="SELECTOR",
        help="Extra selector to clear inline scroll-clip on before "
             "capturing, in addition to the default settings-dialog "
             "selectors (%(default)s). Repeatable.",
    )
    p.add_argument(
        "--wait-ms", type=int, default=800,
        help="Milliseconds to wait after opening a tab, before capturing, "
             "so its async load has settled (default: %(default)s).",
    )
    p.add_argument(
        "--min-bytes", type=int, default=DEFAULT_MIN_BYTES,
        help="Reject the capture as a failed render below this many bytes "
             "(default: %(default)s).",
    )
    p.add_argument(
        "--boot-timeout-s", type=int, default=DEFAULT_BOOT_TIMEOUT_S,
        help="Seconds to wait for the throwaway server to answer /login "
             "(default: %(default)s).",
    )
    p.add_argument(
        "--register", action="store_true",
        help="File the capture in the image gallery after taking it. Off "
             "by default: this writes to config.DB_PATH, which on a real "
             "deployment is the production database.",
    )
    p.add_argument(
        "--register-db-path", default=None,
        help="Database to register into. Defaults to config.DB_PATH (the "
             "production database on a real deployment). Only override this "
             "to register into a throwaway or staging copy.",
    )
    p.add_argument(
        "--work-dir", default=None, type=Path,
        help="Gallery work_dir. Defaults to --out's parent directory. --out "
             "must resolve to a path inside it (routes/db_images.py's "
             "_resolved_inside enforces this on every read).",
    )
    p.add_argument(
        "--chat-title", default=None,
        help="Free-text gallery description. Required with --register.",
    )
    p.add_argument(
        "--owner-id", default=DEFAULT_OWNER_ID,
        help="Gallery owner_id (default: %(default)s, the placeholder the "
             "existing hand-registered rows use).",
    )
    return p


def _check_capture_ok(path: Path, min_bytes: int) -> str | None:
    """Return a failure reason, or None if *path* looks like a real capture.

    This catches the crude version of "the capture is wrong" -- a missing
    file, or one too small for any real rendered panel to have produced
    (a 0-byte file from a crashed browser, a few hundred bytes of PNG
    framing around nothing). It does not and cannot catch a capture that is
    wrong in *content* -- cropped, or holding the wrong element -- both of
    which can be a perfectly normal number of bytes. That failure needs a
    human to look at the picture; this one is a size check standing in for
    the parts of "did the capture actually work" that a byte count can
    answer on its own.
    """
    if not path.exists():
        return f"{path} does not exist"
    if not path.is_file():
        return f"{path} is not a file"
    size = path.stat().st_size
    if size < min_bytes:
        return (
            f"{path} is {size} bytes, below the {min_bytes}-byte floor -- "
            "looks like a failed capture, not a real screenshot"
        )
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    """Boot `app:app` from this working tree on a throwaway database.

    Same recipe as tests/test_frontend_browser.py's `_BrowserFixture._start`,
    kept in step with it deliberately: the live service always serves a
    deployed release, so this is the only way to preview an undeployed
    change, and any divergence from the browser suite's own recipe is a
    second place the same boot can go stale.
    """

    def __init__(self, boot_timeout_s: int = DEFAULT_BOOT_TIMEOUT_S):
        self.tmp = tempfile.TemporaryDirectory(prefix="wc-screenshot-")
        tmp = Path(self.tmp.name)
        (tmp / "projects").mkdir()
        home = tmp / "home"
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "projects").mkdir(parents=True)
        self.db_path = tmp / "wc.db"
        self.port = _free_port()
        self.password = secrets.token_urlsafe(12)
        env = {
            **os.environ,
            "HOME": str(home),
            "WC_LOG_FILE": str(tmp / "app.log"),
            "WC_PROJECTS_ROOT_BASE": str(tmp),
            "WC_DB_PATH": str(self.db_path),
            "WC_PROJECTS_ROOT": str(tmp / "projects"),
            "WC_SESSION_SECRET": secrets.token_urlsafe(32),
            "WC_ADMIN_PASSWORD": self.password,
            # No proxy: this never runs a real turn, only renders pages.
            "WC_PROXY_ENABLED": "0",
            "WC_COOKIE_ALLOW_INSECURE": "1",
        }
        # A file, not a PIPE: nothing here reads the server's stdout, and an
        # unread pipe holds only 64K -- past that uvicorn blocks forever on
        # its own access log and stops answering, which looks like a server
        # that is up but never responds. See _BrowserFixture._start.
        self.log_path = tmp / "server.log"
        self.log_handle = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=env,
            stdout=self.log_handle, stderr=subprocess.STDOUT,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + boot_timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._fail_boot(
                    f"server exited during boot (code {self.proc.returncode})"
                )
            try:
                urllib.request.urlopen(f"{self.base}/login", timeout=1)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        self._fail_boot("server did not answer /login in time")

    def _fail_boot(self, msg: str) -> None:
        try:
            tail = self.log_path.read_text(errors="replace")[-4000:]
        except OSError:
            tail = "<no server log>"
        self.close()
        raise RuntimeError(f"{msg}\n{tail}")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_handle.close()
        self.tmp.cleanup()


def _seed_delegation(db_path: Path) -> None:
    """Run bin/wc-seed-delegation.py against this run's throwaway database.

    A subprocess, not an in-process call: that script refuses to run against
    config.DB_PATH, computed at *its own* import time, and calling it in
    this process would mean this process's config had already been pointed
    at the throwaway path -- exactly the state the refusal exists to catch
    everywhere else. A subprocess keeps the two scripts' notions of "the
    production database" from ever being able to interfere with each other.
    """
    subprocess.run(
        [sys.executable, str(ROOT / "bin" / "wc-seed-delegation.py"),
         "--db-path", str(db_path)],
        cwd=ROOT, check=True,
    )


def _capture(args: argparse.Namespace, server: _Server) -> None:
    from playwright.sync_api import sync_playwright

    chromium = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium, args=["--no-sandbox"])
        try:
            page = browser.new_page()
            # domcontentloaded, not networkidle: this app polls several
            # endpoints continuously, so the network is never idle -- see
            # _BrowserFixture._login for the same choice and reasoning.
            page.goto(f"{server.base}/login", wait_until="domcontentloaded")
            page.fill("#username", "admin")
            page.fill("#password", server.password)
            page.click("#submitBtn")
            page.wait_for_selector("#settingsBtn", timeout=15_000)

            if args.path not in ("/", ""):
                page.goto(f"{server.base}{args.path}", wait_until="domcontentloaded")

            if args.settings_tab:
                page.click("#settingsBtn")
                page.wait_for_selector("#settingsDialog", timeout=10_000)
                page.click(f'.settings-tab[data-tab="{args.settings_tab}"]')
                panel_id = TAB_PANEL_IDS[args.settings_tab]
                page.wait_for_selector(
                    f"#{panel_id}", state="visible", timeout=10_000,
                )

            page.wait_for_timeout(args.wait_ms)

            # Clear whatever inline scroll-clip these selectors carry -- see
            # the module docstring, point 3. A selector matching nothing on
            # this page is a no-op, so this runs unconditionally.
            page.evaluate(
                """(selectors) => {
                    for (const sel of selectors) {
                        document.querySelectorAll(sel).forEach((el) => {
                            el.style.clip = '';
                            el.style.clipPath = '';
                            el.style.overflow = '';
                        });
                    }
                }""",
                args.clear_scroll_clip,
            )

            args.out.parent.mkdir(parents=True, exist_ok=True)
            if args.selector:
                target = page.wait_for_selector(
                    args.selector, state="visible", timeout=10_000,
                )
                target.screenshot(path=str(args.out))
            else:
                page.screenshot(path=str(args.out), full_page=True)
        finally:
            browser.close()


async def _register(args: argparse.Namespace) -> None:
    reason = _check_capture_ok(args.out, args.min_bytes)
    if reason:
        raise SystemExit(f"refusing to register: {reason}")

    work_dir = (args.work_dir or args.out.parent).resolve()
    try:
        rel_path = args.out.resolve().relative_to(work_dir)
    except ValueError:
        raise SystemExit(
            f"refusing to register: {args.out} does not resolve to a path "
            f"inside work_dir {work_dir} -- "
            "routes/db_images.py's _resolved_inside would refuse to serve "
            "it back, so the row would point at an image the app treats as "
            "missing"
        )

    db_path = Path(args.register_db_path or config.DB_PATH).resolve()
    print(f"registering {args.out} in the gallery ({db_path})")

    # Connect directly rather than db.init(): init() runs the full
    # CREATE TABLE / migration script on every call, which this must never
    # do against a live database. generated_image_record only needs
    # db.db_conn assigned to something open on the right file.
    db.db_conn = await aiosqlite.connect(str(db_path))
    db.db_conn.row_factory = aiosqlite.Row
    try:
        await db_images.generated_image_record(
            chat_id=secrets.token_hex(16),
            chat_title=args.chat_title,
            work_dir=str(work_dir),
            owner_id=args.owner_id,
            paths=[str(rel_path)],
        )
    finally:
        await db.close()
    print("registered.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.register and not args.chat_title:
        parser.error("--chat-title is required with --register")

    server = _Server(args.boot_timeout_s)
    try:
        if args.seed_delegation:
            _seed_delegation(server.db_path)
        _capture(args, server)
    finally:
        server.close()

    reason = _check_capture_ok(args.out, args.min_bytes)
    if reason:
        print(f"capture failed: {reason}", file=sys.stderr)
        return 1
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")

    if args.register:
        asyncio.run(_register(args))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
