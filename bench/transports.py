"""Two paths to the same model, both streaming, both reporting TTFT.

The reason this module exists as a choice rather than a constant: the water-jug
task cannot be completed in 600s over the HTTP gateway and is answered
correctly in 23.0s by the CLI. Same model, same prompt, at least 26x apart. A
model cannot be 26x slower because the caller used a different socket, so
transport is a variable — and until it is recorded on every result, no speed
figure from this harness means anything.

Both transports stream. Two reasons:

* **Time to first token is a different measurement from total time**, and the
  old harness reported only the second. A model at 290s total but 2s to first
  token is usable interactively; one at 30s total and 30s to first token is not.
  The comparison document ranked "Interactive?" without ever measuring the
  quantity that decides it.
* Streaming is also the leading hypothesis for the 26x gap — if an unstreamed
  gateway buffers the whole reasoning output before returning a byte, then
  asking for a stream is the experiment.

Every result carries `stop_reason` and `cap_headroom` so a run that hit its
token ceiling can never be scored as a wrong answer, which is the mistake that
produced a 55.4%.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Default ceilings. Both were raised after both disqualified a working model:
#: 4096 truncated Qwen3.6 mid-thought (it needs ~6400), and 180s cut off every
#: turn slower than the Azure models. Overridable, because a ceiling is a
#: property of the harness and belongs where it can be changed without an edit.
MAX_TOKENS = int(os.environ.get("WC_BENCH_MAX_TOKENS", "16384"))
TIMEOUT_S = int(os.environ.get("WC_BENCH_TIMEOUT_S", "1000"))

GATEWAY_URL = os.environ.get(
    "WC_BENCH_BASE_URL", "https://llm.ai-machine.cfappsecurity.com/v1/messages"
)


@dataclass
class Turn:
    """One model reply, and everything needed to know how it was produced."""

    text: str
    transport: str
    model_requested: str
    model_served: str | None = None
    ttft_s: float | None = None
    total_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    had_text_block: bool = False
    thinking_chars: int = 0
    error: str | None = None
    detail: list[str] = field(default_factory=list)

    @property
    def cap_headroom(self) -> float | None:
        """Fraction of `max_tokens` left unused, or None if unknown.

        0.0 means the answer was cut off at the ceiling. Recorded so that a
        truncated run is never confused with a model that answered badly --
        the exact conflation that ranked a working model last.
        """
        if not self.output_tokens or not MAX_TOKENS:
            return None
        return round(max(0.0, 1.0 - self.output_tokens / MAX_TOKENS), 4)

    @property
    def hit_cap(self) -> bool:
        return self.stop_reason == "max_tokens" or self.cap_headroom == 0.0


# --- gateway key -------------------------------------------------------------


def gateway_key() -> str:
    """The gateway key, from the environment or the active machine.

    Never stored in this file or written to output. Same resolution order as
    the rest of the tooling, and the database is opened read-only: opening it
    read-write from a second process is what took the production write path
    down for 37 minutes (registry #41).
    """
    for var in ("WC_BENCH_API_KEY", "ANTHROPIC_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    db = ROOT / "data" / "webconsole.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT api_key FROM ai_machines WHERE active = 1 "
                "AND api_key IS NOT NULL AND api_key != '' LIMIT 1").fetchone()
        finally:
            con.close()
        if row and row[0]:
            return row[0]
    raise SystemExit(
        "bench: no gateway key. Set WC_BENCH_API_KEY, or activate a machine "
        "that has one in the WebConsole database.")


# --- HTTP transport ----------------------------------------------------------


def _http_once(model: str, messages: list[dict], key: str) -> Turn:
    payload = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(GATEWAY_URL, data=payload, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "text/event-stream")

    turn = Turn(text="", transport="http", model_requested=model)
    started = time.monotonic()
    text_parts: list[str] = []
    thinking_chars = 0
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    event = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "message_start":
                    message = event.get("message") or {}
                    turn.model_served = message.get("model")
                    turn.input_tokens = (message.get("usage") or {}).get(
                        "input_tokens", 0)
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "thinking_delta":
                        thinking_chars += len(delta.get("thinking") or "")
                    else:
                        chunk = delta.get("text") or ""
                        if chunk and turn.ttft_s is None:
                            # First *answer* token, not the first thinking
                            # token: what a person waits for is the answer
                            # starting, and a model can think for minutes
                            # before it does.
                            turn.ttft_s = round(time.monotonic() - started, 2)
                        text_parts.append(chunk)
                elif kind == "message_delta":
                    turn.stop_reason = (event.get("delta") or {}).get("stop_reason")
                    turn.output_tokens = (event.get("usage") or {}).get(
                        "output_tokens", turn.output_tokens)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        turn.error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 -- a transport fault is a datum
        turn.error = f"{type(exc).__name__}: {exc}"

    turn.total_s = round(time.monotonic() - started, 2)
    turn.text = "".join(text_parts).strip()
    turn.had_text_block = bool(turn.text)
    turn.thinking_chars = thinking_chars
    if not turn.text and thinking_chars and not turn.error:
        # Never a placeholder. The old harness wrote the literal string
        # "[thinking]" here, which made a truncated run indistinguishable from
        # an empty one and is the single defect that cost a model its ranking.
        turn.detail.append(
            f"no answer text; {thinking_chars} chars of thinking only "
            f"(stop_reason={turn.stop_reason})")
    return turn


# --- CLI transport -----------------------------------------------------------


def _cli_binary() -> str:
    """`wc-claude.sh` if present, else bare `claude`.

    The wrapper is preferred because it resolves the same active machine the
    console would, so a CLI measurement and an HTTP measurement are aimed at
    the same backend rather than silently at api.anthropic.com.
    """
    wrapper = ROOT / "bin" / "wc-claude.sh"
    if wrapper.is_file() and os.access(wrapper, os.X_OK):
        return str(wrapper)
    found = shutil.which("claude")
    if not found:
        raise SystemExit("bench: no claude binary and no bin/wc-claude.sh")
    return found


def _bare_cli() -> str:
    """`claude` itself, with no machine resolution in front of it."""
    found = shutil.which("claude")
    if not found:
        raise SystemExit("bench: no claude binary on PATH")
    return found


def _cli_invocation(model: str) -> tuple[str, dict[str, str] | None]:
    """Binary and environment for reaching *model* over the CLI.

    Two different things were being conflated by "the CLI transport", and the
    first live run found it: `wc-claude.sh` resolves the **active machine**,
    which on this host is the gateway, so asking it for `claude-haiku-4-5`
    returns

        API Error: 400 anthropic_messages: Invalid model name passed in
        model=claude-haiku-4-5

    -- the gateway rejecting a model it does not serve. The wrapper is correct
    for gateway families and is the only way to compare http against cli on the
    *same* backend, which is the 26x measurement. It cannot reach Anthropic.

    So Anthropic models get the bare binary plus the environment
    `runner._build_env` builds for a `provider='anthropic'` machine with no
    stored key: base URL at the official API, and `CLAUDE_CODE_SIMPLE` left
    **unset**. That last part is load-bearing and counter-intuitive -- under
    `CLAUDE_CODE_SIMPLE` the CLI refuses to read the host's own login
    (`claude auth status` reports `loggedIn:false, authMethod:none`), so
    setting it would hand the subprocess no credentials at all. See CLAUDE.md
    §3, which records it because it cost real debugging time once.

    Verified 2026-09-02 by driving `runner._build_cmd_direct` and
    `runner._build_env` directly: all four Anthropic models answered, and
    `claude-haiku-4-5` came back served as `claude-haiku-4-5-20251001`.
    """
    if not fnmatch.fnmatch(model, "claude-*"):
        return _cli_binary(), None
    safe = {"HOME", "PATH", "SHELL", "LANG", "LC_ALL", "TERM"}
    env = {k: v for k, v in os.environ.items() if k in safe}
    env["PYTHONUNBUFFERED"] = "1"
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    env["ANTHROPIC_BASE_URL"] = os.environ.get(
        "WC_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    # No ANTHROPIC_API_KEY and no CLAUDE_CODE_SIMPLE, deliberately: the host
    # login is the credential, and the flag would suppress it.
    return _bare_cli(), env


def _cli_once(model: str, messages: list[dict], _key: str) -> Turn:
    """One CLI turn. Multi-turn is handled by the caller via --resume.

    Only the final user message is sent; earlier turns are replayed by the CLI
    from its own session, which is the behaviour the console relies on and the
    thing a benchmark that concatenates history would fail to measure.
    """
    prompt = messages[-1]["content"]
    session_id = messages[0].get("_session_id")
    resume = messages[0].get("_resume")
    binary, env = _cli_invocation(model)
    cmd = [
        binary, "-p", "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions", "--model", model,
    ]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--", prompt]

    turn = Turn(text="", transport="cli", model_requested=model)
    started = time.monotonic()
    text_parts: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd="/tmp",  # nosec B108 -- throwaway cwd on purpose
            env=env,  # None inherits, which is what wc-claude.sh needs
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = frame.get("type")
            if kind == "assistant":
                message = frame.get("message") or {}
                turn.model_served = message.get("model") or turn.model_served
                for block in message.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        # No TTFT is recorded on this path, on purpose.
                        #
                        # `--output-format stream-json` emits a whole assistant
                        # *message* per frame, not per-token deltas, so the
                        # earliest moment this loop can observe text is when the
                        # complete answer has arrived. The first live run made
                        # that obvious: ttft=57.05 against total=57.44.
                        #
                        # Publishing that as a time-to-first-token would say
                        # "unusable interactively" about a model whose stream
                        # this harness simply cannot see -- the exact shape of
                        # the defect this whole module exists to prevent. An
                        # absent measurement is reported as absent.
                        text_parts.append(block["text"])
                    elif block.get("type") == "thinking":
                        turn.thinking_chars += len(block.get("thinking") or "")
            elif kind == "result":
                usage = frame.get("usage") or {}
                turn.input_tokens = usage.get("input_tokens", 0)
                turn.output_tokens = usage.get("output_tokens", 0)
                turn.stop_reason = frame.get("stop_reason") or "end_turn"
                if frame.get("is_error"):
                    # CLAUDE.md §4: a failed turn arrives as an event, not an
                    # exception. Code that only catches exceptions reads it as
                    # a successful empty turn.
                    turn.error = str(frame.get("result") or "cli reported is_error")
            elif kind == "error":
                turn.error = str(frame.get("error"))
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            turn.error = f"cli did not exit within {TIMEOUT_S}s"
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        if proc.returncode not in (0, None) and not turn.error:
            turn.error = f"cli exited {proc.returncode}: {stderr.strip()[-200:]}"
    except (OSError, subprocess.SubprocessError) as exc:
        turn.error = f"{type(exc).__name__}: {exc}"

    turn.total_s = round(time.monotonic() - started, 2)
    turn.text = "".join(text_parts).strip()
    turn.had_text_block = bool(turn.text)
    turn.ttft_s = None
    turn.detail.append(
        "ttft not measurable over cli: stream-json emits whole message blocks")
    return turn


TRANSPORTS = {"http": _http_once, "cli": _cli_once}

#: Which paths can actually reach which model family.
#:
#: This exists because the comparison document said "Anthropic models are NOT
#: accessible through this gateway" and then omitted them from every table --
#: reading a gateway's model list as the set of models that exist. They are
#: reachable, just not over `http`: `bin/wc-claude.sh` runs the CLI against the
#: host's own `claude` login, and all four answered on 2026-09-02 (opus-5 6.0s,
#: sonnet-5 4.4s, fable-5 6.6s, haiku-4-5 4.3s, the last served as
#: claude-haiku-4-5-20251001).
#:
#: Declared per family rather than left to the caller, because "which socket
#: can see this model" is a fact about the deployment. A runner that guessed
#: would record an unreachable path as a model failure, which is the whole
#: class of mistake this harness exists to stop.
MODEL_TRANSPORTS: tuple[tuple[str, frozenset[str]], ...] = (
    # Anthropic models: no gateway route, so CLI only.
    ("claude-*", frozenset({"cli"})),
    # Gateway-served families. The CLI reaches these too, via the active
    # machine wc-claude.sh resolves -- which is what makes the 26x
    # http-versus-cli comparison possible at all.
    ("azure_ai/*", frozenset({"http", "cli"})),
    ("vllm/*", frozenset({"http", "cli"})),
)


def reachable(model: str, transport: str) -> bool:
    """Whether *transport* can reach *model* on this deployment.

    An unlisted model is assumed reachable both ways: a new backend should not
    be silently skipped, and a real failure is more informative than a guess
    that hides it.
    """
    for pattern, allowed in MODEL_TRANSPORTS:
        if fnmatch.fnmatch(model, pattern):
            return transport in allowed
    return True


def why_unreachable(model: str, transport: str) -> str:
    """The reason, for a skip line that a reader can act on."""
    if transport == "http" and fnmatch.fnmatch(model, "claude-*"):
        return (f"{model} is not served by the gateway; use --transports cli, "
                "which reaches it through the host's claude login")
    return f"{model} is not reachable over {transport} on this deployment"


def send(transport: str, model: str, messages: list[dict], key: str) -> Turn:
    """Dispatch one turn. Unknown transport is an error, not a default."""
    try:
        fn = TRANSPORTS[transport]
    except KeyError:
        raise SystemExit(
            f"bench: unknown transport {transport!r}; "
            f"choose from {sorted(TRANSPORTS)}") from None
    return fn(model, messages, key)
