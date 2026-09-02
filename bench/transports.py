"""One path to the model: the Claude Code CLI, which is what the console uses.

`CLAUDE.md` §0 is explicit -- the console never talks to a model API, it spawns
the `claude` CLI and varies its parameters. So the CLI is production and there
is nothing else to measure.

**A raw-HTTP transport used to live here and was removed on 2026-09-02.** It
earned its keep first: it is how the 25.8x Qwen3.6 gap was found, and how the
gateway's Azure streaming crash was found. Both are recorded in
`Backend_Models_20260902.comparison.md`, and the runs behind them are committed
as `bench_qwen_transport_20260902.json` and `bench_azure_20260902.json`. What it
could not earn was a place in every future run: it doubled the cost of a sweep
to measure a path no user reaches.

Removing it also took the harness's only API key handling with it. There is no
`gateway_key()` any more, no reading a secret out of the database, and no
credential in any request this module builds -- `bin/wc-claude.sh` resolves the
backend and the CLI holds its own auth. A benchmark that cannot leak a key is
worth more than one that is careful with it.

What is measured, and why each field exists:

* `ttft_s` comes from the result frame's `ttft_ms`, not from timing the first
  `assistant` frame. stream-json emits whole message blocks, so timing the
  stream measures the finished answer -- that gave ttft=57.05 against
  total=57.44 once. The CLI measures it properly itself.
* `reported_cost_usd` with `cost_basis` -- authoritative only when the basis is
  `list`. For a gateway backend the CLI has no rate card and prices it at
  Anthropic rates, which is why every non-Anthropic `cost_usd` in the console's
  `usage_events` is fictional.
* `stop_reason` and `cap_headroom`, so a truncated run is never scoreable as a
  wrong answer. That conflation produced a 55.4% and a withdrawn verdict.
* `files_written` -- the CLI is an agent with file tools, so "return valid
  Python code only" can be answered by writing the code to disk. Two runs
  scored 0 before this was captured.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Default ceilings. Both were raised after both disqualified a working model:
#: 4096 truncated Qwen3.6 mid-thought (it needs ~6400), and 180s cut off every
#: turn slower than the Azure models. Overridable, because a ceiling is a
#: property of the harness and belongs where it can be changed without an edit.
MAX_TOKENS = int(os.environ.get("WC_BENCH_MAX_TOKENS", "16384"))
TIMEOUT_S = int(os.environ.get("WC_BENCH_TIMEOUT_S", "1000"))

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
    #: Files the agent wrote instead of answering inline. The CLI is an agent
    #: with file tools, so "return valid Python code only" can be satisfied by
    #: writing the code to disk and describing it.
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


TRANSPORTS = {"cli": _cli_once}

#: Kept as a hook, empty. It existed to stop Anthropic models being sent at
#: the gateway over HTTP, and with one transport there is nothing to route --
#: but `reachable()` is still called per (model, transport) pair by the runner,
#: and an unreachable backend on some future transport should be skipped with a
#: reason rather than scored as a failure. That property is what the map was
#: for; the routing was incidental.
MODEL_TRANSPORTS: tuple[tuple[str, frozenset[str]], ...] = ()


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
