"""Smoke test: change a conversation's backend and model in a real browser.

Not collected by pytest (no ``test_`` prefix) because it needs Chromium and
chromedriver. Run it directly:

    python3 tests/smoke_conversation_routing.py

Serves the real web/ assets and stubs the JSON APIs, recording every PATCH so
the test can assert what the UI actually sent -- not merely that a control
changed appearance. Exercises the flow as requested: move a conversation from
Anthropic to an AI machine, and from model A to model B, and confirm the other
conversation is unaffected.

Exit code 0 on success, 1 on any failed check, 2 if the browser is unavailable.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
PORT = 8756
FAILURES: list[str] = []

MACHINES = [
    {"id": "anthropic-api", "name": "Anthropic API", "provider": "anthropic",
     "backend_kind": "anthropic", "host": "api.anthropic.com", "port": 443,
     "base_url": "https://api.anthropic.com", "model": "claude-opus-5",
     "active_models": "[]", "active": 1, "description": ""},
    {"id": "ai-machine", "name": "AI Machine", "provider": "anthropic",
     "backend_kind": "anthropic-compatible", "host": "llm.example.invalid",
     "port": 443, "base_url": "https://llm.example.invalid",
     "model": "vllm/Qwen3.6-35B-A3B-NVFP4", "active_models": "[]", "active": 0,
     "description": ""},
]
MODELS_BY_MACHINE = {
    "anthropic-api": ["claude-opus-5", "claude-sonnet-5"],
    "ai-machine": ["vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5.4-mini"],
}
CHATS = [
    {"id": "chatA", "title": "Chat A", "description": None, "work_dir": "/tmp/a",
     "session_id": None, "model": None, "ai_machine_id": None, "archived": 0,
     "pinned": 0, "pinned_at": None, "created_at": "2026-08-29T09:00:00Z",
     "updated_at": "2026-08-29T09:00:00Z", "deleted_at": None},
    {"id": "chatB", "title": "Chat B", "description": None, "work_dir": "/tmp/b",
     "session_id": None, "model": None, "ai_machine_id": None, "archived": 0,
     "pinned": 0, "pinned_at": None, "created_at": "2026-08-29T09:01:00Z",
     "updated_at": "2026-08-29T09:01:00Z", "deleted_at": None},
]
PATCHES: list[tuple[str, dict]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def _chat(chat_id):
    return next((c for c in CHATS if c["id"] == chat_id), None)


class Handler(http.server.SimpleHTTPRequestHandler):
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/api/machines":
            return self._json({"machines": MACHINES})
        if path == "/api/models":
            mid = (query.get("machine_id") or ["anthropic-api"])[0]
            ids = MODELS_BY_MACHINE.get(mid, [])
            return self._json({
                "models": [{"id": i} for i in ids], "active": [], "default": "",
                "source": "endpoint", "machine_id": mid, "endpoint": "x",
            })
        if path == "/api/chats":
            return self._json({"chats": CHATS})
        if path.startswith("/api/chats/"):
            chat = _chat(path.split("/")[3])
            if not chat:
                return self._json({"error": "not found"}, 404)
            # Same shape as handle_chat_get: {"chat": {...}, "messages": [...]}.
            return self._json({"chat": dict(chat), "messages": []})
        if path == "/api/settings":
            return self._json({"settings": {}})
        if path == "/api/usage":
            return self._json({"days": 30, "retention_days": 90, "totals": [],
                               "recent": [], "overall": {"requests": 0}})
        if path == "/api/skills":
            return self._json({"skills": [], "sources": [], "total": 0,
                               "active_count": 0, "session_id": ""})
        if path.startswith(("/api/sessions", "/api/transcripts")):
            return self._json({"sessions": [], "transcripts": [], "turns": []})
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_PATCH(self):
        parsed = urlparse(self.path)
        chat_id = parsed.path.split("/")[3]
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        PATCHES.append((chat_id, body))
        chat = _chat(chat_id)
        if chat is None:
            return self._json({"error": "not found"}, 404)
        # Persist, so a later GET reflects it exactly as the server would.
        chat.update(body)
        return self._json({"ok": True})


def main() -> int:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        print(f"SKIP: selenium unavailable ({exc})")
        return 2
    if not Path("/usr/bin/chromium").exists():
        print("SKIP: chromium not installed")
        return 2

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    opts = Options()
    opts.binary_location = "/usr/bin/chromium"
    for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                 "--window-size=1400,900"):
        opts.add_argument(flag)
    opts.set_capability("goog:loggingPrefs", {"browser": "SEVERE"})
    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)
    js = driver.execute_script

    def select(element_id, value):
        js(
            "const s=document.getElementById(arguments[0]); s.value=arguments[1];"
            "s.dispatchEvent(new Event('change'));",
            element_id, value,
        )
        js("return new Promise(r=>setTimeout(r,450))")

    def options_of(element_id):
        return js(
            "return [...document.getElementById(arguments[0]).options]"
            ".map(o=>({value:o.value,text:o.textContent}));", element_id)

    def open_chat(chat_id):
        # Scope to the desktop list: the mobile sidebar renders the same items
        # first and is `inert`, so clicking that copy silently does nothing.
        js(
            "const row=document.querySelector("
            "`#chatListDesktop [data-chat-id='${arguments[0]}'] .chat-open`);"
            "if(!row) throw new Error('conversation row not found');"
            "row.click();",
            chat_id,
        )
        js("return new Promise(r=>setTimeout(r,700))")

    try:
        driver.get(f"http://127.0.0.1:{PORT}/index.html")
        driver.implicitly_wait(3)
        js("return new Promise(r=>setTimeout(r,900))")

        print("\n== The controls exist on a conversation ==")
        open_chat("chatA")
        check("backend picker present", bool(driver.find_elements(
            By.ID, "conversationBackend")))
        check("model picker present", bool(driver.find_elements(
            By.ID, "conversationModel")))
        check("workspace strip visible", js(
            "return getComputedStyle(document.getElementById('workspaceStrip'))"
            ".display") != "none")

        backends = options_of("conversationBackend")
        check("both backends offered plus Follow active", len(backends) == 3,
              str([b["text"] for b in backends]))
        check("Follow active names the active machine",
              "Anthropic API" in backends[0]["text"], backends[0]["text"])
        check("AI machine is labelled by kind",
              any("Anthropic-compatible" in b["text"] for b in backends),
              str([b["text"] for b in backends]))
        check("chatA starts unpinned",
              js("return document.getElementById('conversationBackend').value") == "")

        print("\n== Move the conversation from Anthropic to the AI machine ==")
        PATCHES.clear()
        select("conversationBackend", "ai-machine")
        sent = [p for p in PATCHES if "ai_machine_id" in p[1]]
        check("a PATCH was sent", bool(sent), str(PATCHES))
        check("it targeted this conversation", sent and sent[0][0] == "chatA",
              str(sent[:1]))
        check("it set the machine", sent and sent[0][1]["ai_machine_id"] == "ai-machine",
              str(sent[:1]))
        check("server state updated", _chat("chatA")["ai_machine_id"] == "ai-machine")

        print("\n== The model list follows the new backend ==")
        js("return new Promise(r=>setTimeout(r,600))")
        models = [o["value"] for o in options_of("conversationModel") if o["value"]]
        check("gateway models offered",
              "vllm/Qwen3.6-35B-A3B-NVFP4" in models, str(models))
        check("previous backend's models gone",
              "claude-opus-5" not in models, str(models))

        print("\n== Move from model A to model B ==")
        PATCHES.clear()
        select("conversationModel", "vllm/Qwen3.6-35B-A3B-NVFP4")
        check("model A persisted",
              _chat("chatA")["model"] == "vllm/Qwen3.6-35B-A3B-NVFP4",
              str(PATCHES))
        select("conversationModel", "azure_ai/gpt-5.4-mini")
        check("model B persisted",
              _chat("chatA")["model"] == "azure_ai/gpt-5.4-mini", str(PATCHES))
        check("each change sent exactly one PATCH", len(PATCHES) == 2, str(PATCHES))

        print("\n== The other conversation is unaffected ==")
        open_chat("chatB")
        check("chatB still follows the active backend",
              js("return document.getElementById('conversationBackend').value") == "",
              _chat("chatB")["ai_machine_id"] or "unpinned")
        check("chatB has no pinned model",
              js("return document.getElementById('conversationModel').value") == "",
              str(_chat("chatB")["model"]))
        check("chatB is offered the active backend's models",
              "claude-opus-5" in [o["value"] for o in options_of("conversationModel")],
              str([o["value"] for o in options_of("conversationModel")]))

        print("\n== Reopening restores the conversation's own routing ==")
        open_chat("chatA")
        check("backend restored", js(
            "return document.getElementById('conversationBackend').value")
            == "ai-machine")
        check("model restored", js(
            "return document.getElementById('conversationModel').value")
            == "azure_ai/gpt-5.4-mini")

        print("\n== Returning to Anthropic clears an unservable model ==")
        PATCHES.clear()
        select("conversationBackend", "anthropic-api")
        check("backend switched back",
              _chat("chatA")["ai_machine_id"] == "anthropic-api",
              str(PATCHES))
        check("the gateway-only model was cleared, not sent to Anthropic",
              _chat("chatA")["model"] is None, str(_chat("chatA")["model"]))
        models = [o["value"] for o in options_of("conversationModel") if o["value"]]
        check("Anthropic models offered again", "claude-opus-5" in models, str(models))

        print("\n== Clearing the pin returns to following ==")
        select("conversationBackend", "")
        check("pin cleared", _chat("chatA")["ai_machine_id"] is None,
              str(_chat("chatA")["ai_machine_id"]))

        logs = [x["message"] for x in driver.get_log("browser")
                if "favicon" not in x["message"].lower()]
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
