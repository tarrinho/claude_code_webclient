"""Smoke test: background turns in a real browser.

Not collected by pytest (no ``test_`` prefix) because it needs Chromium and
chromedriver. Run it directly:

    python3 tests/smoke_background_turns.py

The unit tests prove the server keeps a turn alive. They cannot prove the thing
actually asked for, which is a UI behaviour: that you can send a request and
change conversation. This drives the real web/ assets against stubbed JSON and
SSE endpoints, recording every request so the checks assert what the page *did*
rather than how it looked.

Covers: switching away mid-turn is allowed and does not stop the turn, coming
back reattaches and replays, several conversations show a running dot at once,
a reply that lands elsewhere leaves an unread mark, the queue panel renders and
its Send/Discard buttons reach the right endpoints, a held queue is distinguished
from a waiting one, a terminal-busy conversation gets its own dot, and a
slot-wait is reported instead of looking like a stall.

Exit code 0 on success, 1 on any failed check, 2 if the browser is unavailable.
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
PORT = 8759
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
    "chatA": [{"role": "user", "content": "prior turn",
               "created_at": "2026-08-30T09:00:00Z"}],
    "chatB": [], "chatC": [],
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
        if path.startswith(("/api/sessions", "/api/transcripts", "/api/supervisor",
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
                 "--window-size=1400,900"):
        opts.add_argument(flag)
    opts.set_capability("goog:loggingPrefs", {"browser": "SEVERE"})
    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"),
                              options=opts)
    js = driver.execute_script

    def wait(ms):
        js(f"return new Promise(r=>setTimeout(r,{ms}))")

    def open_chat(chat_id):
        # Scope to the desktop list: the mobile sidebar renders the same rows
        # first and is `inert`, so clicking that copy silently does nothing.
        js("const row=document.querySelector("
           "`#chatListDesktop [data-chat-id='${arguments[0]}'] .chat-open`);"
           "if(!row) throw new Error('row not found: '+arguments[0]);"
           "row.click();", chat_id)
        wait(600)

    def dots(class_name):
        return js("return [...document.querySelectorAll("
                  "`#chatListDesktop .${arguments[0]}`)].length", class_name)

    try:
        global LIVE_FRAMES
        driver.get(f"http://127.0.0.1:{PORT}/index.html")
        driver.implicitly_wait(3)
        wait(900)

        print("\n== Switching conversation mid-turn is allowed ==")
        LIVE_FRAMES = [{"type": "start", "chat_id": "chatA"},
                       {"type": "text", "content": "working", "seq": 1}]
        open_chat("chatA")
        js("document.getElementById('composerInput').value='do a thing';")
        js("document.getElementById('sendBtn').click();")
        wait(700)
        state = js("return document.getElementById('runState').dataset.state")
        check("the turn is active after sending", state in
              ("connecting", "thinking", "responding"), str(state))

        CALLS.clear()
        open_chat("chatB")
        title = js("return document.getElementById('workspaceName').textContent")
        check("the switch actually happened", title == "Beta", str(title))
        toasts = js("return [...document.querySelectorAll('.toast')]"
                    ".map(t=>t.textContent).join('|')")
        check("no 'stop the current response' refusal",
              "Stop the current response" not in toasts, toasts[:80])
        check("the detail request went to the new conversation",
              any(p == "/api/chats/chatB" for _m, p in CALLS), str(CALLS[:4]))
        check("no stop was sent — leaving is not stopping",
              not any(p.endswith("/stop") for _m, p in CALLS), str(CALLS))

        print("\n== Coming back reattaches and replays ==")
        _chat("chatA")["running"] = True
        LIVE_FRAMES = [{"type": "text", "content": "replayed answer", "seq": 1},
                       {"type": "done", "seq": 2}]
        CALLS.clear()
        open_chat("chatA")
        wait(900)
        check("it attached to /live",
              any(p == "/api/chats/chatA/live" for _m, p in CALLS), str(CALLS[:6]))
        body = js("return document.getElementById('messagesArea').textContent")
        check("the replayed answer was rendered", "replayed answer" in body,
              body[-70:])

        print("\n== Several conversations can show a running dot ==")
        _chat("chatA")["running"] = True
        _chat("chatB")["running"] = True
        js("return fetch('/api/chats')")
        wait(200)
        js("return window.__wcRefresh ? window.__wcRefresh() : null")
        wait(6500)   # the sidebar poll interval
        check("two running dots at once", dots("chat-running") >= 2,
              f"found {dots('chat-running')}")
        check("the terminal-busy conversation has its own dot",
              dots("chat-terminal-busy") >= 1,
              f"found {dots('chat-terminal-busy')}")
        _chat("chatA")["running"] = False
        _chat("chatB")["running"] = False

        print("\n== The queue panel renders and is actionable ==")
        QUEUE["chatA"] = [
            {"id": 11, "prompt": "queued prompt one", "model": None,
             "state": "pending", "created_at": "2026-08-30T09:05:00Z"},
            {"id": 12, "prompt": "queued prompt two", "model": None,
             "state": "pending", "created_at": "2026-08-30T09:06:00Z"},
        ]
        _chat("chatA")["queued"] = 2
        LIVE_FRAMES = []
        open_chat("chatA")
        wait(700)
        check("the panel is visible",
              not js("return document.getElementById('queueBar').hidden"))
        check("both prompts are listed",
              js("return document.querySelectorAll('#queueList .queue-item').length") == 2,
              str(js("return document.getElementById('queueList').textContent")))
        check("the prompt text is shown",
              "queued prompt one" in
              js("return document.getElementById('queueList').textContent"))
        check("a pending queue offers no Send button",
              js("return document.querySelectorAll('#queueList .queue-btn').length") == 2,
              "only Discard on each row")

        CALLS.clear()
        js("document.querySelector('#queueList .queue-btn-drop').click()")
        wait(600)
        check("Discard sent a DELETE to the right row",
              ("DELETE", "/api/chats/chatA/queue/11") in CALLS, str(CALLS[:4]))
        check("the row was removed",
              js("return document.querySelectorAll('#queueList .queue-item').length") == 1,
              str(js("return document.getElementById('queueList').textContent")))

        print("\n== A held queue is distinguished and offers Send ==")
        QUEUE["chatA"] = [
            {"id": 21, "prompt": "held prompt", "model": None,
             "state": "held", "created_at": "2026-08-30T09:07:00Z"},
        ]
        open_chat("chatB")
        open_chat("chatA")
        wait(700)
        check("the panel marks it held",
              js("return document.getElementById('queueBar').dataset.held") == "yes")
        check("the note explains the failure",
              "failed" in js("return document.getElementById('queueNote').textContent"),
              js("return document.getElementById('queueNote').textContent")[:70])
        check("a held row offers Send and Discard",
              js("return document.querySelectorAll('#queueList .queue-btn').length") == 2)
        CALLS.clear()
        js("document.querySelector('#queueList .queue-btn').click()")
        wait(600)
        check("Send sent the release",
              ("POST", "/api/chats/chatA/queue/21/release") in CALLS, str(CALLS[:4]))

        print("\n== A slot wait is reported, not left looking like a stall ==")
        QUEUE["chatA"] = []
        _chat("chatA")["queued"] = 0
        LIVE_FRAMES = [
            {"type": "start", "chat_id": "chatA"},
            {"type": "status", "status": "waiting_for_slot",
             "error": "Waiting for a free slot — 3 turns are already running."},
        ]
        open_chat("chatA")
        js("document.getElementById('composerInput').value='another thing';")
        js("document.getElementById('sendBtn').click();")
        wait(900)
        label = js("return document.getElementById('runState').textContent")
        check("the slot wait is shown to the user", "slot" in label.lower(),
              str(label))
        js("document.getElementById('sendBtn').click();")   # stop
        wait(500)

        print("\n== Mobile viewport and the dark/light toggle ==")
        driver.set_window_size(390, 680)
        wait(400)
        QUEUE["chatA"] = [{"id": 31, "prompt": "x" * 400, "model": None,
                           "state": "pending", "created_at": "2026-08-30T09:08:00Z"}]
        open_chat("chatA")
        wait(600)
        overflow = js(
            "const l=document.getElementById('queueList');"
            "return l.scrollWidth - l.clientWidth")
        check("a very long queued prompt does not overflow horizontally",
              overflow <= 1, f"{overflow}px")
        for theme in ("light", "dark"):
            js("document.documentElement.dataset.theme=arguments[0]", theme)
            wait(200)
            visible = js("const b=document.getElementById('queueBar');"
                         "const s=getComputedStyle(b);"
                         "return s.display!=='none' && s.visibility!=='hidden'")
            check(f"the queue panel renders in the {theme} theme", visible)
        driver.set_window_size(1400, 900)

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
