"""Smoke test: the last request kept under the workspace strip, in a browser.

Not collected by pytest (no ``test_`` prefix) because it needs Chromium and
chromedriver. Run it directly:

    python3 tests/smoke_background_turns.py

The unit tests prove the server keeps a turn alive. They cannot prove the thing
actually asked for, which is a UI behaviour: that you can send a request and
change conversation. This drives the real web/ assets against stubbed JSON and
SSE endpoints, recording every request so the checks assert what the page *did*
rather than how it looked.

Covers: the line appears with what was asked, shows the newest request rather
than the first, follows a switch between conversations, appears the moment a
request is sent (including one that is queued, which produces no turn here at
all), survives a very long prompt without widening the pane, and is hidden for a
conversation nobody has asked anything in.
"""
from __future__ import annotations

import json
import socketserver
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
PORT = 8763
FAILURES: list[str] = []

# Recorded so the checks can assert on what the page requested.
CALLS: list[tuple[str, str]] = []

CHATS = [
    {"id": "chatA", "title": "Alpha", "description": None, "work_dir": "/tmp/a",
     "session_id": None, "model": None, "ai_machine_id": None, "archived": 0,
     "pinned": 0, "pinned_at": None, "created_at": "2026-08-30T09:00:00Z",
     "updated_at": "2026-08-30T09:00:00Z", "deleted_at": None,
     "running": False, "queued": 0, "terminal_busy": False},
    {"id": "chatB", "title": "Beta", "description": None, "work_dir": "/tmp/b",
     "session_id": None, "model": None, "ai_machine_id": None, "archived": 0,
     "pinned": 0, "pinned_at": None, "created_at": "2026-08-30T09:01:00Z",
     "updated_at": "2026-08-30T09:01:00Z", "deleted_at": None,
     "running": False, "queued": 0, "terminal_busy": False},
    {"id": "chatC", "title": "Gamma", "description": None, "work_dir": "/tmp/c",
     "session_id": "sess-c", "model": None, "ai_machine_id": None, "archived": 0,
     "pinned": 0, "pinned_at": None, "created_at": "2026-08-30T09:02:00Z",
     "updated_at": "2026-08-30T09:02:00Z", "deleted_at": None,
     "running": False, "queued": 0, "terminal_busy": True},
]
# What the server would have persisted. The real /live sends `done` only after
# the answer is stored, and the page reloads from the server on `done` -- so a
# stub that does not persist makes correct behaviour look like a bug.
MESSAGES: dict[str, list[dict]] = {
    "chatA": [
        {"role": "user", "content": "the first thing I asked",
         "created_at": "2026-08-30T09:00:00Z"},
        {"role": "assistant", "content": "done",
         "created_at": "2026-08-30T09:00:30Z"},
        {"role": "user", "content": "the newest thing I asked",
         "created_at": "2026-08-30T09:05:00Z"},
        {"role": "assistant", "content": "also done",
         "created_at": "2026-08-30T09:05:20Z"},
    ],
    "chatB": [{"role": "user", "content": "beta question",
               "created_at": "2026-08-30T09:10:00Z"}],
    "chatC": [],
}
QUEUE: dict[str, list[dict]] = {"chatA": [], "chatB": [], "chatC": []}
# Frames the /live stub replays for chatA, set per scenario.
LIVE_FRAMES: list[dict] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(label)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def _chat(chat_id):
    return next((c for c in CHATS if c["id"] == chat_id), None)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(REPO / "web"), **kw)

    def log_message(self, *a):
        pass

    def _json(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, frames, hold=0.0):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for frame in frames:
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            if hold:
                time.sleep(hold)
        except (BrokenPipeError, ConnectionResetError):
            # The page detached. That is the behaviour under test, not an error.
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        CALLS.append(("GET", path))
        if path == "/api/chats":
            return self._json({"chats": CHATS})
        if path.endswith("/queue") and path.startswith("/api/chats/"):
            chat_id = path.split("/")[3]
            return self._json({"queue": QUEUE.get(chat_id, []), "max": 5,
                               "running": bool(_chat(chat_id)["running"])})
        if path.endswith("/question") and path.startswith("/api/chats/"):
            return self._json({"pending": False})
        if path.endswith("/live") and path.startswith("/api/chats/"):
            chat_id = path.split("/")[3]
            # Persist before `done`, exactly as turns.py does, so the page's
            # reload-on-done finds the answer.
            for frame in LIVE_FRAMES:
                if frame.get("type") == "done":
                    text = "".join(f.get("content", "") for f in LIVE_FRAMES
                                   if f.get("type") == "text")
                    MESSAGES.setdefault(chat_id, []).append(
                        {"role": "assistant", "content": text,
                         "created_at": "2026-08-30T09:10:00Z"})
                    _chat(chat_id)["running"] = False
            return self._sse(LIVE_FRAMES, hold=0.4)
        if path.startswith("/api/chats/") and len(path.split("/")) == 4:
            chat = _chat(path.split("/")[3])
            if not chat:
                return self._json({"error": "not found"}, 404)
            return self._json({
                "chat": {**chat, "turn_seq": 0},
                "messages": MESSAGES.get(chat["id"], []),
            })
        if path == "/api/settings":
            return self._json({"settings": {}, "version": "0.9.0"})
        if path == "/api/machines":
            return self._json({"machines": []})
        if path == "/api/models":
            return self._json({"models": [], "active": [], "default": "",
                               "source": "endpoint", "machine_id": ""})
        if path == "/api/usage":
            return self._json({"days": 30, "retention_days": 90, "totals": [],
                               "recent": [], "overall": {"requests": 0}})
        if path == "/api/skills":
            return self._json({"skills": [], "sources": [], "total": 0,
                               "active_count": 0, "session_id": ""})
        if path.startswith(("/api/sessions", "/api/transcripts", "/api/orchestrator",
                            "/api/stats", "/api/agent-traffic")):
            return self._json({"sessions": [], "transcripts": [], "turns": [],
                               "peers": [], "messages": [], "waiting": [],
                               "working": [], "series": [], "models": []})
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        CALLS.append(("POST", path))
        if path.endswith("/stream"):
            chat_id = path.split("/")[3]
            _chat(chat_id)["running"] = True
            return self._sse(LIVE_FRAMES, hold=1.5)
        if path.endswith("/release"):
            return self._json({"ok": True, "started": False})
        if path.endswith("/stop"):
            chat_id = path.split("/")[3]
            _chat(chat_id)["running"] = False
            return self._json({"ok": True, "stopped": True, "held": 0})
        return self._json({"ok": True, "peers": [], "messages": []})

    def do_DELETE(self):
        path = urlparse(self.path).path
        CALLS.append(("DELETE", path))
        chat_id = path.split("/")[3]
        queue_id = int(path.rsplit("/", 1)[1])
        QUEUE[chat_id] = [r for r in QUEUE[chat_id] if r["id"] != queue_id]
        _chat(chat_id)["queued"] = len(QUEUE[chat_id])
        return self._json({"ok": True})

    def do_PATCH(self):
        CALLS.append(("PATCH", urlparse(self.path).path))
        return self._json({"ok": True})


class Server(socketserver.ThreadingTCPServer):
    # Threading matters: the page holds an SSE connection open while making
    # other requests, and a single-threaded server would deadlock on it.
    daemon_threads = True
    allow_reuse_address = True




def main() -> int:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:
        print(f"SKIP: selenium unavailable ({exc})")
        return 2
    if not Path("/usr/bin/chromium").exists():
        print("SKIP: chromium not installed")
        return 2

    httpd = Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    opts = Options()
    opts.binary_location = "/usr/bin/chromium"
    for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                 "--window-size=390,680"):
        opts.add_argument(flag)
    opts.set_capability("goog:loggingPrefs", {"browser": "SEVERE"})
    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)
    js = driver.execute_script

    def wait(ms):
        js(f"return new Promise(r=>setTimeout(r,{ms}))")

    def open_chat(chat_id):
        js("const row=document.querySelector("
           "`#chatListDesktop [data-chat-id='${arguments[0]}'] .chat-open`);"
           "if(!row) throw new Error('row not found: '+arguments[0]);"
           "row.click();", chat_id)
        wait(600)

    def line():
        return js("const b=document.getElementById('lastCommandBar');"
                  "return {hidden:b.hidden,"
                  " text:document.getElementById('lastCommandText').textContent,"
                  " when:document.getElementById('lastCommandWhen').textContent,"
                  " title:document.getElementById('lastCommandText').title};")

    try:
        global LIVE_FRAMES
        driver.get(f"http://127.0.0.1:{PORT}/index.html")
        driver.implicitly_wait(3)
        wait(900)

        print("\n== Nothing open: no line ==")
        check("hidden before a conversation is opened", line()["hidden"])

        print("\n== Opening a conversation shows the newest request ==")
        open_chat("chatA")
        got = line()
        check("the line is visible", not got["hidden"])
        check("it shows the NEWEST request, not the first",
              got["text"] == "the newest thing I asked", repr(got["text"]))
        check("a relative time is shown", bool(got["when"]), repr(got["when"]))
        check("the full text is available on hover",
              got["title"] == "the newest thing I asked")

        print("\n== It follows a switch between conversations ==")
        open_chat("chatB")
        check("it changed to the other conversation's request",
              line()["text"] == "beta question", repr(line()["text"]))
        open_chat("chatA")
        check("and back again", line()["text"] == "the newest thing I asked")

        print("\n== A conversation nobody has asked anything in ==")
        open_chat("chatC")
        check("hidden when there is no request to show", line()["hidden"],
              repr(line()["text"]))

        print("\n== Sending shows it at once ==")
        LIVE_FRAMES = [{"type": "start", "chat_id": "chatA"},
                       {"type": "text", "content": "working", "seq": 1}]
        open_chat("chatA")
        js("document.getElementById('composerInput').value='a brand new ask';")
        js("document.getElementById('sendBtn').click();")
        wait(500)
        check("the new request appears immediately",
              line()["text"] == "a brand new ask", repr(line()["text"]))
        js("document.getElementById('sendBtn').click();")   # stop
        wait(400)

        print("\n== A queued request still updates the line ==")
        # A queued request produces no turn here, so `send` is the only moment
        # the line can be set for it.
        _chat("chatA")["running"] = True
        LIVE_FRAMES = [{"type": "start", "chat_id": "chatA"},
                       {"type": "queued", "position": 1}]
        open_chat("chatA")
        js("document.getElementById('composerInput').value='queued behind it';")
        js("document.getElementById('sendBtn').click();")
        wait(700)
        check("a queued request is shown as the last one asked",
              line()["text"] == "queued behind it", repr(line()["text"]))
        _chat("chatA")["running"] = False

        print("\n== A very long prompt does not widen the pane ==")
        MESSAGES["chatB"] = [{"role": "user", "content": "z" * 600,
                              "created_at": "2026-08-30T09:20:00Z"}]
        open_chat("chatB")
        overflow = js("const b=document.getElementById('lastCommandBar');"
                      "return b.scrollWidth - b.clientWidth;")
        check("no horizontal overflow at 390px", overflow <= 1, f"{overflow}px")
        check("it is still one line",
              js("return document.getElementById('lastCommandBar').clientHeight") < 40,
              str(js("return document.getElementById('lastCommandBar').clientHeight")))

        print("\n== Both themes ==")
        for theme in ("light", "dark"):
            js("document.documentElement.dataset.theme=arguments[0]", theme)
            wait(150)
            visible = js("const b=document.getElementById('lastCommandBar');"
                         "const s=getComputedStyle(b);"
                         "return s.display!=='none' && s.visibility!=='hidden';")
            check(f"visible in the {theme} theme", visible)

        logs = [entry["message"] for entry in driver.get_log("browser")
                if "favicon" not in entry["message"].lower()]
        check("no console errors", not logs, str(logs[:2]))
    finally:
        driver.quit()
        httpd.shutdown()

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
