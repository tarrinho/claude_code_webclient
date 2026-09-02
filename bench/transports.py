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
import tempfile
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
    #: Files the agent wrote instead of answering inline. Empty on the
    #: http path, which has no tools.
    files_written: list[str] = field(default_factory=list)
    #: Cached input. `input_tokens` alone badly understates what a turn cost:
    #: the CLI caches its system prompt and tool definitions, so opus-5
    #: reported a constant 12,029 input tokens for every task including the
    #: 30k-token long-context one, while the real volume sat in these two
    #: fields. Costing a turn from `input_tokens` undercounts it by orders of
    #: magnitude.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: What the CLI itself says the turn cost, and on what basis. This is
    #: authoritative where present -- it is the number the vendor bills -- and
    #: is preferred over anything computed from a rate card. `cost_basis` is
    #: 'list' for first-party models and absent or 'unknown' for a gateway
    #: backend that the CLI has no rates for, which is precisely the
    #: distinction that made every non-Anthropic figure in usage_events
    #: fictional.
    reported_cost_usd: float | None = None
    cost_basis: str | None = None

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
    """Stream if we can, fall back to a single response if streaming breaks.

    Streaming is preferred because it is the only way to measure
    time-to-first-token, which the comparison document ranked "Interactive?"
    on without ever measuring. But it is not available for every backend: the
    gateway's Azure passthrough crashes when streaming, so insisting on it
    would score five working models at zero.
    """
    turn = _http_stream(model, messages, key)
    if turn.error and not turn.text:
        fallback = _http_blocking(model, messages, key)
        fallback.detail.insert(0, f"streaming failed ({turn.error}); "
                                  "re-ran unstreamed, so no ttft for this run")
        return fallback
    return turn


def _http_blocking(model: str, messages: list[dict], key: str) -> Turn:
    """One unstreamed request. No TTFT is available, and none is invented."""
    payload = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(GATEWAY_URL, data=payload, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")

    turn = Turn(text="", transport="http", model_requested=model)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 -- a transport fault is a datum
        turn.error = f"{type(exc).__name__}: {exc}"
        turn.total_s = round(time.monotonic() - started, 2)
        return turn

    turn.total_s = round(time.monotonic() - started, 2)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        turn.error = f"gateway error: {msg.splitlines()[0][:200]}"
        return turn

    text_parts, thinking = [], 0
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "thinking":
            thinking += len(block.get("thinking") or "")
    usage = data.get("usage") or {}
    turn.text = "".join(text_parts).strip()
    turn.had_text_block = bool(turn.text)
    turn.thinking_chars = thinking
    turn.input_tokens = usage.get("input_tokens", 0)
    turn.output_tokens = usage.get("output_tokens", 0)
    turn.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    turn.cache_write_tokens = usage.get("cache_creation_input_tokens", 0)
    turn.stop_reason = data.get("stop_reason")
    turn.model_served = data.get("model")
    return turn


def _http_stream(model: str, messages: list[dict], key: str) -> Turn:
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
                # An error delivered *inside* the stream. This has to be
                # checked before `type`, because the frame has no `type` at
                # all -- it is `{"error": {"message": ...}}` -- so a parser
                # that switches on `type` drops it and reports silence.
                #
                # That is not hypothetical. The gateway crashes when asked to
                # stream an Azure model:
                #
                #   {"error": {"message": "list index out of range\n\n
                #    Traceback ... litellm/proxy/..."}}
                #
                # Every one of 16 Azure runs came back with out=0, stop=None
                # and an empty response, and would have been published as the
                # models scoring zero. A server-side traceback is the one thing
                # that must never be attributed to a model.
                if "error" in event and "type" not in event:
                    err = event["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    turn.error = f"gateway error in stream: {msg.splitlines()[0][:200]}"
                    turn.detail.append("the gateway returned an error mid-stream, "
                                       "not the model")
                    break
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
    # A private directory per turn, for two reasons found the hard way.
    #
    # This used to be `cwd="/tmp"`, which is shared with everything else on the
    # host -- the run left an `lru_cache.py` there next to eight other
    # sessions' files, and a second run would have read the first one's answer.
    #
    # And the CLI is an *agent*, not a completion endpoint: it has file tools
    # and runs with --dangerously-skip-permissions, so "return valid Python
    # code only" can be satisfied by writing the code to disk and replying
    # "Done. Doubly-linked list + hash map." That is a reasonable reading of
    # the instruction and it scored 0 against a verifier that only reads the
    # response. The directory is scanned below so the answer is found wherever
    # the model chose to put it.
    workdir = tempfile.mkdtemp(prefix="wc-bench-cli-")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=workdir,
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
                        # TTFT is deliberately NOT taken from here.
                        #
                        # `--output-format stream-json` emits a whole assistant
                        # *message* per frame, not per-token deltas, so the
                        # earliest moment this loop can observe text is when
                        # the complete answer has already arrived. Timing it
                        # here produced ttft=57.05 against total=57.44 -- which
                        # would have said "unusable interactively" about a
                        # model whose stream simply is not visible from here.
                        #
                        # The real figure comes off the `result` frame's
                        # `ttft_ms`, below. Measured on the same model that
                        # produced the 57.05: 2.35s against 4.94s total.
                        text_parts.append(block["text"])
                    elif block.get("type") == "thinking":
                        turn.thinking_chars += len(block.get("thinking") or "")
            elif kind == "result":
                usage = frame.get("usage") or {}
                turn.input_tokens = usage.get("input_tokens", 0)
                turn.output_tokens = usage.get("output_tokens", 0)
                turn.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                turn.cache_write_tokens = usage.get(
                    "cache_creation_input_tokens", 0)
                turn.stop_reason = frame.get("stop_reason") or "end_turn"
                # The CLI reports its own cost and the basis for it. Preferred
                # over anything this harness computes: it is what gets billed.
                if frame.get("total_cost_usd") is not None:
                    turn.reported_cost_usd = float(frame["total_cost_usd"])
                for entry in (frame.get("modelUsage") or {}).values():
                    if isinstance(entry, dict) and entry.get("costBasis"):
                        turn.cost_basis = entry["costBasis"]
                        break
                # Real time-to-first-token, from the field rather than from
                # watching the stream. An earlier version of this module
                # concluded TTFT was "not measurable over cli" because
                # stream-json emits whole message blocks -- true of the stream,
                # and beside the point, since the CLI measures it for us.
                for key in ("ttft_stream_ms", "ttft_ms"):
                    if frame.get(key):
                        turn.ttft_s = round(float(frame[key]) / 1000.0, 2)
                        break
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
    if turn.ttft_s is None:
        turn.detail.append(
            "no ttft in the result frame; stream-json emits whole message "
            "blocks so it cannot be recovered from the stream either")

    # Whatever the agent wrote, appended as fenced code so the verifier sees
    # it. Recorded in `detail` as well: an answer delivered by file is a real
    # difference between the two transports and must be visible in the data,
    # not silently folded into the response text.
    try:
        written = sorted(Path(workdir).rglob("*.py"))
    except OSError:
        written = []
    if written:
        turn.files_written = [p.name for p in written]
        blocks = []
        for path in written:
            try:
                blocks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        if blocks:
            turn.detail.append(
                f"answer delivered as {len(blocks)} file(s) rather than inline: "
                + ", ".join(p.name for p in written))
            turn.text = (turn.text + "\n\n```python\n"
                         + "\n\n".join(blocks) + "\n```").strip()
    shutil.rmtree(workdir, ignore_errors=True)
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
