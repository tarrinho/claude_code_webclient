"""Smoke test: one conversation walked through every model switch, in order.

Not collected by pytest (no ``test_`` prefix) because it talks to the running
service and spends real turns on real backends. Run it directly:

    .venv/bin/python tests/smoke_model_switch.py

What it proves that the unit tests cannot. The unit suite pins the machinery
-- the picker sends a question, the preflight sizes the context, the window is
learned from a refusal. None of that establishes the thing actually in doubt
on 2026-09-15, which was whether the model you *select* is the model that
*runs*. "local : 13 : models comparison" was set to
vllm/Qwen3.6-35B-A3B-NVFP4, recorded a usage row on exactly that, and replied
"Claude Opus 5" -- both true at once, because a headless turn and the live
terminal behind that conversation share one transcript. Only running real
turns against real backends settles it.

**One journey, not three separate tests.** Every switch below happens in a
single conversation, in sequence, and each step carries a token handed to the
first model and asked back from every model after it. That is deliberate: a
per-leg test can only say each model answers when picked, and the question
underneath this work was also whether a conversation survives being moved
between backends mid-thread. Chaining the legs asks both at once -- every step
checks *what ran* and *what it still remembered*, so a switch that silently
starts a fresh context fails here even though the model id would look right.

The journey, and why each step is in it:

1. **Seed on the gateway.** The token goes in on Qwen.
2. **Cross to Anthropic.** The first direction, and the first continuity check.
3. **Cross back to the gateway.** The other direction. A one-way test passes
   if the second model simply never takes effect and the first reply repeats.
4. **Cross to Anthropic again**, closing the round trip.
5. **Move the gateway off its default model** (azure_ai/gpt-5-mini).
6. **Move Anthropic off its default model** (claude-sonnet-5).

Steps 5 and 6 are the only ones sensitive to the model field at all, and they
exist because mutation testing found the earlier version blind. Removing
``model`` from the PATCH entirely, leaving only ``ai_machine_id``, still
passed every cross-backend check: each backend carries a default model -- the
gateway's is vllm/Qwen3.6-35B-A3B-NVFP4, Anthropic's is claude-opus-5 -- so
picking the backend alone already lands in the right family. Only a
non-default model on an unchanged backend can tell an honoured selection from
an ignored one. With ``model`` dropped, steps 5 and 6 fail and the run exits 1.

Two more things worth knowing before changing this file:

* **Backend and model are always set together.** A model id is only meaningful
  against the backend serving it (CLAUDE.md section 0.1):
  vllm/Qwen3.6-35B-A3B-NVFP4 exists on the gateway only, claude-opus-5 on
  Anthropic only. Sending one to the other answers 429 "No deployments
  available for selected model", a routing failure wearing a capacity error's
  clothes. So every step pins ai_machine_id as well as model, which is what
  the UI does.

* **The verdict comes from the usage row, NOT from what the model says it
  is.** This is the lesson of the first version of this file, which reported
  two failures that were not failures. Asked "which model are you", the
  gateway steps answered "claude-sonnet-5" -- truthfully, from their point of
  view, because every turn is the Claude Code CLI (CLAUDE.md section 0) and
  the CLI's system prompt tells whatever model is behind it that it is Claude
  Code. Qwen was reading its prompt, not lying. Meanwhile usage_events
  recorded those same turns as vllm/Qwen3.6-35B-A3B-NVFP4 with ~19k input
  tokens against ~118 for the Anthropic steps, which is the CLI preamble a
  gateway model has to be sent in full. Self-report is unusable here; the
  usage row is the backend's own accounting of what ran, written from the
  CLI's result frame, and it is what this asserts. The self-report is printed
  for the reader and never asserted on.

Models are matched by marker rather than exact id, because a gateway may
report a turn under a different id than it was requested under (nvidia/... for
vllm/...) -- the same split that made the usage statistics confusing in the
first place. Steps 5 and 6 need narrower markers than the rest: "claude" would
match the opus default that step 6 is trying to move off.

Exit code 0 when every step ran on the model that was selected and every step
after the first still had the token; 1 on any mismatch, lost context or failed
request; 2 when the service or credentials are unavailable, which includes the
host refusing a turn for low memory -- a refused turn says nothing about
routing, and counting it as a defect is how a busy host becomes a false alarm.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# HTTPS via the tailnet name, not http://127.0.0.1:8080, and that is not a
# preference: the session cookie is set Secure unless COOKIE_ALLOW_INSECURE, so
# a cookie jar will not send it back over http and every authenticated request
# answers 401 while the login itself reports 200. Registry #54 records the same
# trap costing a whole API test class its authentication without anyone
# noticing. Also note a bare tailscale IP cannot be used here -- Caddy routes
# by SNI, so an address answers 000 (bin/wc-health-url.sh has the measurements).
BASE = os.environ.get("WC_SMOKE_URL", "https://kali-2.tail850c40.ts.net")

# Backend ids are read live from /api/machines rather than hardcoded: they
# differ per deployment, and a stale id would fail as "model not served" and
# look like the defect this is meant to detect.
QWEN_MODEL = "vllm/Qwen3.6-35B-A3B-NVFP4"
CLAUDE_MODEL = "claude-opus-5"
# Non-default on their own backend, which is what makes the last two steps
# sensitive to the model field rather than the backend field. Both are served:
# gpt-5-mini is in CF AI Machine's active_models, and Anthropic declares no
# list (a backend that publishes none is not checked -- CLAUDE.md section 0.1).
GATEWAY_ALT_MODEL = "azure_ai/gpt-5-mini"
ANTHROPIC_ALT_MODEL = "claude-sonnet-5"

# Arbitrary and unlikely to be produced by chance, so finding it in a reply is
# evidence the earlier turn is still in context rather than a lucky guess.
TOKEN = "PELICAN-4417"
SEED_PROMPT = (
    f"Remember this token for the rest of our conversation: {TOKEN}. "
    "Reply with just the word: stored"
)
RECALL_PROMPT = (
    "Reply with exactly two lines and nothing else. "
    "Line 1: the token I asked you to remember earlier. "
    "Line 2: your model id."
)

FAILURES: list[str] = []
# Steps the host declined to run at all. Kept apart from FAILURES on purpose:
# a refused turn proves nothing about model routing, and reporting it as a
# defect is how a busy host turns into a false alarm.
UNAVAILABLE: list[str] = []


def _fail(message: str) -> None:
    FAILURES.append(message)
    print(f"    FAIL: {message}")


def _admin_password() -> str:
    """From .env, never echoed. Absent means this cannot run at all."""
    env = REPO / ".env"
    if not env.is_file():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("WC_ADMIN_PASSWORD="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class Client:
    """Cookie-carrying JSON client for the live service."""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "wc_csrf":
                return cookie.value or ""
        return ""

    def request(self, method: str, path: str, body: dict | None = None,
                raw: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = self._csrf()
        if token:
            req.add_header("X-CSRF-Token", token)
        with self.opener.open(req, timeout=300) as response:
            payload = response.read().decode("utf-8", "replace")
        if raw:
            return payload
        return json.loads(payload) if payload.strip() else {}

    def login(self, password: str) -> None:
        self.request("GET", "/login", raw=True)          # seeds the CSRF cookie
        self.request("POST", "/login",
                     {"username": "admin", "password": password})


def _read_stream(sse: str) -> tuple[str, str, str]:
    """(reply text, blocking status, status message) out of an SSE stream.

    The status frames matter as much as the text. A turn the host declines to
    start -- `low_memory` when the resource guard is holding turns back,
    `waiting_for_slot` when MAX_CONCURRENT is saturated -- arrives as
    {"type":"status", ...} followed by `done`, with no text and no usage row.
    An earlier version of this file ignored status frames, so a run that
    collided with the full pytest suite reported three steps as "no turn ran"
    in exactly the shape of a real routing defect. That is an unavailable
    host, not a wrong model, and the two must not share an exit code.

    `waiting_for_slot` is not by itself blocking -- the turn does run after
    the wait -- so it is only reported when nothing followed it.
    """
    parts: list[str] = []
    status = message = ""
    for line in sse.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "text":
            parts.append(str(event.get("content") or ""))
        elif kind == "error":
            parts.append(f"[error] {event.get('error')}")
        elif kind == "status":
            status = str(event.get("status") or "")
            message = str(event.get("error") or "")
    text = "".join(parts).strip()
    if status == "waiting_for_slot" and text:
        status = message = ""      # it waited, then ran; nothing to report
    return text, status, message


def _machine_ids(client: Client) -> tuple[str, str]:
    """(gateway id, anthropic id), chosen by which models each one serves."""
    machines = client.request("GET", "/api/machines").get("machines", [])
    gateway = anthropic = ""
    for machine in machines:
        served = str(machine.get("active_models") or "")
        default = str(machine.get("model") or "")
        kind = str(machine.get("backend_kind") or "")
        if kind == "ssh-proxy":
            continue          # needs a tunnel up; not this test's concern
        if not gateway and QWEN_MODEL in served:
            gateway = machine.get("id") or ""
        if not anthropic and default.startswith("claude-"):
            anthropic = machine.get("id") or ""
    return gateway, anthropic


def _usage_rows(client: Client, chat_id: str) -> list[dict]:
    """This conversation's turns, newest first, as the backend accounted them.

    /api/usage returns `recent` ordered by id DESC, so no timestamp tiebreaker
    is needed here -- created_at has one-second resolution and several turns of
    one run can land in the same second.
    """
    payload = client.request("GET", "/api/usage?days=all&limit=200")
    return [row for row in payload.get("recent", [])
            if row.get("chat_id") == chat_id]


def _step(client: Client, chat_id: str, number: int, label: str,
          machine_id: str, model: str, marker: str, prompt: str,
          expect_token: bool) -> None:
    """One switch: pin backend+model, run the UI's preflight, ask, then check.

    Two assertions per step, and they fail for different reasons on purpose --
    the model check catches routing that ignored the selection, the token check
    catches a switch that silently dropped the conversation's history.
    """
    before = len(_usage_rows(client, chat_id))

    client.request("PATCH", f"/api/chats/{chat_id}",
                   {"ai_machine_id": machine_id, "model": model})
    client.request("POST", f"/api/chats/{chat_id}/preflight")
    answer, status, status_message = _read_stream(client.request(
        "POST", f"/api/chats/{chat_id}/stream", {"content": prompt}, raw=True))

    print(f"  step {number}: {label}")
    print(f"    selected   : {model}")

    if status:
        # Not a failure: the host declined to start the turn, so this run
        # cannot say anything about routing either way.
        UNAVAILABLE.append(f"step {number} ({status})")
        print(f"    SKIPPED    : the host refused the turn -- {status}: "
              f"{status_message[:140]}")
        return

    rows = _usage_rows(client, chat_id)
    if len(rows) <= before:
        _fail(f"step {number}: no usage row was recorded, so no turn ran "
              f"-- the reply was {answer[:120]!r}")
        return

    row = rows[0]
    ran = str(row.get("model") or "")
    reply = " | ".join(answer.splitlines())[:90]
    # Printed either way: on a pass it is the evidence, on a failure it is the
    # first thing worth seeing. The self-report sits beside the usage row
    # precisely because the two disagreeing is normal (see the docstring).
    print(f"    usage row  : {ran}  "
          f"(in={row.get('input_tokens')} out={row.get('output_tokens')})")
    print(f"    replied    : {reply!r}")

    if marker not in ran.lower():
        _fail(f"step {number}: selected {model} but the turn ran on {ran!r}")
    elif "[error]" in answer.lower():
        _fail(f"step {number}: ran on the right model but the turn failed "
              f"-- {answer[:200]}")

    if expect_token and TOKEN.lower() not in answer.lower():
        _fail(f"step {number}: the conversation lost its history across this "
              f"switch -- {TOKEN} went in at step 1 and this reply does not "
              f"have it")


def main() -> int:
    password = _admin_password()
    if not password:
        print("WC_ADMIN_PASSWORD not found in .env -- cannot reach the service")
        return 2

    client = Client()
    try:
        client.login(password)
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach {BASE}: {exc}")
        return 2

    gateway, anthropic = _machine_ids(client)
    if not gateway or not anthropic:
        print(f"need one gateway and one Anthropic backend; found "
              f"gateway={gateway!r} anthropic={anthropic!r}")
        return 2
    print(f"gateway backend  : {gateway}")
    print(f"anthropic backend: {anthropic}")

    # A throwaway conversation, not an existing one. "models comparison"
    # carries 412,326 tokens of context, which does not fit Qwen's window at
    # all, so a switch there would fail for a reason that has nothing to do
    # with what this tests. A fresh chat also has no linked CLI session, which
    # removes the shared-transcript path entirely -- what runs is then
    # unambiguously the model that was selected.
    created = client.request("POST", "/api/chats", {
        "title": "smoke: model switch journey",
        "description": "throwaway, created and removed by smoke_model_switch.py",
    })
    chat_id = created.get("id") or created.get("chat", {}).get("id")
    if not chat_id:
        print(f"could not create a conversation: {created}")
        return 2
    print(f"conversation     : {chat_id}")
    print(f"token            : {TOKEN}\n")

    journey = [
        ("seed the token on the gateway",
         gateway, QWEN_MODEL, "qwen", SEED_PROMPT, False),
        ("cross to Anthropic",
         anthropic, CLAUDE_MODEL, "claude", RECALL_PROMPT, True),
        ("cross back to the gateway",
         gateway, QWEN_MODEL, "qwen", RECALL_PROMPT, True),
        ("cross to Anthropic again, closing the round trip",
         anthropic, CLAUDE_MODEL, "claude", RECALL_PROMPT, True),
        ("gateway, off its default model",
         gateway, GATEWAY_ALT_MODEL, "gpt-5-mini", RECALL_PROMPT, True),
        ("Anthropic, off its default model",
         anthropic, ANTHROPIC_ALT_MODEL, "sonnet", RECALL_PROMPT, True),
    ]

    try:
        for number, (label, machine, model, marker, prompt, want) in enumerate(
                journey, start=1):
            _step(client, chat_id, number, label, machine, model, marker,
                  prompt, want)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        _fail(f"request failed mid-journey: {exc}")
    finally:
        try:
            client.request("DELETE", f"/api/chats/{chat_id}")
            print(f"\ncleaned up {chat_id}")
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask a result
            print(f"\ncould not delete {chat_id}: {exc}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed across {len(journey)} steps")
        if UNAVAILABLE:
            print(f"(and {len(UNAVAILABLE)} step(s) never ran: "
                  f"{', '.join(UNAVAILABLE)})")
        return 1
    if UNAVAILABLE:
        # Deliberately not 0. Nothing failed, but the journey is incomplete,
        # and a green exit code on a half-run test is the kind of reassurance
        # that is worse than no test at all.
        print(f"inconclusive: the host refused {len(UNAVAILABLE)} of "
              f"{len(journey)} steps ({', '.join(UNAVAILABLE)}). "
              f"Re-run when the host is quieter -- the full pytest suite "
              f"running alongside this is enough to trigger it.")
        return 2
    print(f"all {len(journey)} steps ran on the model that was selected, and "
          f"the conversation kept its history across every switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
