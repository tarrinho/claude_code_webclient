# config.py — WebConsole configuration (env-driven, fail-fast).
#
# All secrets, endpoints, and limits come from the environment. Nothing sensitive
# is hard-coded. Missing *required* values fail fast at boot with a clear message.
from __future__ import annotations

import os
import pathlib
import re


def _str(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    v = _str(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


# --- listen ----------------------------------------------------------------
HOST = _str("WC_HOST", "0.0.0.0")  # nosec B104: default, warn in README to use private IP
PORT = _int("WC_PORT", 8080)
# Bind to this interface only. For Tailscale use: the device's tailnet IP
# (e.g. "100.x.x.x"), never "0.0.0.0" for external exposure.
LISTEN_HOST = _str("WC_LISTEN_HOST", HOST)

# --- database --------------------------------------------------------------
# Both databases live in the same data directory for simplicity (backup,
# export, restore). The separation is purely architectural — no collision.
_db_dir = pathlib.Path(__file__).parent / "data"
_db_dir.mkdir(parents=True, exist_ok=True)

DB_PATH = _str("WC_DB_PATH", str(_db_dir / "webconsole.db"))
SESSION_DB_PATH = _str("WC_SESSION_DB_PATH", str(_db_dir / "webconsole-sessions.db"))

# --- project directories ---------------------------------------------------
PROJECTS_ROOT = _str("WC_PROJECTS_ROOT", None)
if PROJECTS_ROOT is None:
    PROJECTS_ROOT = str(
        (pathlib.Path(__file__).parent / "projects").resolve()
    )

# --- logging ---------------------------------------------------------------
# Where logging.conf's rotating handler writes. Overridable because the config
# file names an absolute path, so every server the test suite spawns inherited
# it and appended to the production log -- which then carried interleaved,
# out-of-order and future-stamped lines from processes nobody was watching.
# That is not untidiness: the log is the first thing anyone opens during an
# incident, and it was actively misleading about ordering.
#
# The parent is created for both branches, not just the default. A path whose
# directory does not exist makes RotatingFileHandler fail at construction, and
# a test server dying that way surfaces as an exit during setUpClass with the
# real reason buried.
LOG_FILE = _str("WC_LOG_FILE", None)
if LOG_FILE is None:
    LOG_FILE = str(pathlib.Path(__file__).parent / "logs" / "webconsole.log")
pathlib.Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

# --- auth ------------------------------------------------------------------
SESSION_SECRET = _str("WC_SESSION_SECRET")  # required, secrets.token_urlsafe(32)
SESSION_TTL_S = _int("WC_SESSION_TTL_S", 7200)  # 2h absolute
SESSION_IDLE_S = _int("WC_SESSION_IDLE_S", 1800)  # 30m idle timeout
SESSION_MAX = _int("WC_SESSION_MAX", 50)  # hard cap on concurrent sessions
LOGIN_RATE_MAX = _int("WC_LOGIN_RATE_MAX", 10)  # max attempts per window
LOGIN_RATE_WIN = _int("WC_LOGIN_RATE_WIN", 300)  # window in seconds (5 min)
LOGIN_BACKOFF = _int("WC_LOGIN_BACKOFF", 30)  # base backoff seconds on lockout
TOKEN_DEFAULT_TTL_DAYS = _int("WC_TOKEN_DEFAULT_TTL_DAYS", 90)  # 90d default expiry; 0 = no expiry

# Allow plain-HTTP cookies (for Tailscale without TLS). DO NOT enable on public
# interfaces. Defaults to False so a deployment that forgets to set it still
# marks the session cookie Secure; opt in explicitly for plain-HTTP setups.
COOKIE_ALLOW_INSECURE = _bool("WC_COOKIE_ALLOW_INSECURE", False)

# --- rate limiting ---------------------------------------------------------
RATE_LIMIT_MAX = _int("WC_RATE_LIMIT_MAX", 120)  # default: 120 req per window
RATE_LIMIT_WINDOW = _int("WC_RATE_LIMIT_WINDOW", 60)  # window in seconds
# Per-endpoint limits: endpoint → (capacity, rate).
RATE_LIMIT_ENDPOINTS: dict[str, tuple[int, float]] = {
    "/api/chats/": (10, 0.167),  # 10 req/min for chat prompts
    "/api/machines": (20, 0.333),  # 20 req/min for machine ops
}

# --- SSH proxy -------------------------------------------------------------
# Max concurrent SSH tunnels per deployment. Each tunnel holds one TCP
# connection to a remote host and a local port forward.
MAX_TUNNELS = _int("WC_MAX_TUNNELS", 5)
# Seconds between tunnel health probes (keepalive + proxy ping).
# How often the remote-session cache is refreshed. Each pass SSHes to every
# transport and reads a file per session, so this is a network cost, not a
# memory one -- it was hardcoded at 3 seconds, which starved the event loop
# badly enough that /login took 75 seconds to answer.
REMOTE_SESSION_CACHE_S = _int("WC_REMOTE_SESSION_CACHE_S", 60)
# Whether read_claude_sessions() reaches SSH transports to collect remote CLI
# sessions.  Defaults off so tests that create a local sessions dir do not
# unexpectedly hit the real network.
REMOTE_SESSIONS = _bool("WC_REMOTE_SESSIONS", False)

TUNNEL_HEALTH_INTERVAL_S = _int("WC_TUNNEL_HEALTH_INTERVAL_S", 30)
# Seconds between remote stats collection (CPU, mem, disk via SSH exec).
TUNNEL_STATS_INTERVAL_S = _int("WC_TUNNEL_STATS_INTERVAL_S", 120)
# Seconds before an unreachable remote proxy probe times out.
TUNNEL_PROBE_TIMEOUT_S = _int("WC_TUNNEL_PROBE_TIMEOUT_S", 2)
# Local port range for tunnel binds.
TUNNEL_PORT_RANGE_LOW = _int("WC_TUNNEL_PORT_RANGE_LOW", 9000)
TUNNEL_PORT_RANGE_HIGH = _int("WC_TUNNEL_PORT_RANGE_HIGH", 10000)
# Seconds of consecutive failed reconnects before auto-disconnect.
TUNNEL_DISCONNECT_TIMEOUT = _int("WC_TUNNEL_DISCONNECT_TIMEOUT", 300)

# --- Claude Code proxy (host-side) ----------------------------------------
# When PROXY_ENABLED=True the runner connects to the host-side proxy instead
# of spawning `claude` directly inside the container.
PROTOCOL = "webconsole-v1"
PROXY_HOST = _str("WC_PROXY_HOST", "127.0.0.1")
PROXY_PORT = _int("WC_PROXY_PORT", 9000)
PROXY_ENABLED = _bool("WC_PROXY_ENABLED", True)
PROXY_CONNECT_TIMEOUT_S = _int("WC_PROXY_CONNECT_TIMEOUT_S", 10)
PROXY_TURN_TIMEOUT_S = _int("WC_PROXY_TURN_TIMEOUT_S", 300)
PROXY_TOKEN = _str("WC_PROXY_TOKEN")

# --- Sync-request watcher (transport project sync) --------------------------
# OFF, and it must stay off until transcripts._agent_events_sync tail-reads
# instead of whole-file reading. Measured on 2026-09-09, the day this shipped:
# one transcripts.agent_traffic(scan_files=12) call is 31.4s wall and 603MB
# peak RSS, because it read_bytes() + decode() + splitlines() the 12 newest
# transcripts (86, 50, 49, 48, 44, 40, 31, 13MB...) -- three full in-memory
# copies each. On a 10s interval that measured 417MB of disk read per 20s and
# 76% of one core, permanently, on a 3.7GB host that was already 2.7GB into
# swap. Before the watcher existed, agent_traffic() ran only when someone
# opened the "Messages between sessions" panel (transcript.js showTraffic,
# one-shot, never polled), so this turned a rare 30s request into a
# continuous loop. The UI-triggered half of transport sync (the Sync button)
# is unaffected by this flag and still works.
SYNC_REQUEST_WATCHER_ENABLED = _bool("WC_SYNC_REQUEST_WATCHER", False)
# Interval when it is enabled. 10s was the shipped value and is far too
# aggressive for the current whole-file implementation; defence in depth for
# whoever flips the flag before the redesign lands.
SYNC_REQUEST_WATCHER_INTERVAL_S = _int("WC_SYNC_REQUEST_WATCHER_INTERVAL_S", 300)

# --- Anthropic API (Claude Code's native backend) ---------------------------
# The CLI talks to https://api.anthropic.com unless ANTHROPIC_BASE_URL says
# otherwise, and authenticates with ANTHROPIC_API_KEY if set, falling back to
# the host's own `claude` login. A machine with provider='anthropic' carries
# these values to the CLI; leaving the key empty keeps the login fallback.
ANTHROPIC_BASE_URL = _str("WC_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_MODEL = _str("WC_ANTHROPIC_MODEL", "claude-opus-5")

# Shown only when the active machine cannot be asked what it serves. The UI
# labels these as a fallback rather than presenting them as the real list.
KNOWN_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-haiku-4-5",
)

# --- Fallback: direct Claude Code subprocess --------------------------------
# These are used only when PROXY_ENABLED=False (not recommended for container).
MODEL_BASE_URL = _str("WC_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
MODEL_API_KEY = _str("WC_MODEL_API_KEY", "")
MODEL_NAME = _str("WC_MODEL_NAME", "claude-sonnet-5")

# --- Voice conversation defaults -------------------------------------------
# Settings dialog can override at runtime via the generic settings table;
# these are the fallback when unset.
VOICE_BACKEND_ID_DEFAULT = _str("WC_VOICE_BACKEND_ID_DEFAULT", None)
VOICE_AI_MACHINE_ID_DEFAULT = _str("WC_VOICE_AI_MACHINE_ID_DEFAULT", None)
VOICE_MODEL_DEFAULT = _str("WC_VOICE_MODEL_DEFAULT", "azure_ai/gpt-5.6-luna")
VOICE_SPEECH_RATE_DEFAULT = _float("WC_VOICE_SPEECH_RATE_DEFAULT", 1.0)

# --- cross-session peer messaging ------------------------------------------
# Default to "accept" so webconsole sessions can message each other without
# approval prompts (the whole point of the webconsole cluster).
CROSS_SESSION_INBOUND_DEFAULT = _str("WC_CROSS_SESSION_INBOUND_DEFAULT", "accept")

# --- testing default model --------------------------------------------------
# What a test file should write instead of hardcoding "claude-opus-5" (or any
# other literal) wherever the test needs a real model id and the specific
# value is not itself the thing under test. Settings dialog can override at
# runtime via the generic settings table, same as the voice defaults above.
# Deliberately not config.MODEL_NAME: that is the production application's own
# default for a real turn, already asserted against by tests
# (test_qa_active_models.py, test_qa_model_choice_sticks.py) -- conflating the
# two would make a Settings-dialog change to one silently change what the
# other verifies.
TESTING_MODEL_DEFAULT = _str("WC_TESTING_MODEL_DEFAULT", "claude-opus-5")
# Off = tests/conftest.py resolves the model actually in effect for the
# current agent session (bin/wc-backend-env.py --profile "$WC_PROFILE" --json)
# instead of the fixed default above. See tests/conftest.py for the resolver.
TESTING_MODEL_ENFORCE_DEFAULT = _bool("WC_TESTING_MODEL_ENFORCE_DEFAULT", True)

# --- remote QA execution -----------------------------------------------------
# See docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.
# Same env vars bin/run-suite-chunked.sh already reads for the equivalent
# local-run settings, deliberately -- a remote run and a local chunked run
# should agree on what "enough room" and "too long" mean unless told
# otherwise.
QA_CAPACITY_FLOOR_MB = _int("WC_SUITE_COST_MB", 700)
QA_CHUNK_TIMEOUT = _int("WC_CHUNK_TIMEOUT", 600)

# --- usage accounting ------------------------------------------------------
# How long per-turn usage rows are kept. Pruned once per startup; 0 disables
# pruning and keeps everything.
USAGE_RETENTION_DAYS = _int("WC_USAGE_RETENTION_DAYS", 90)

# --- host statistics -------------------------------------------------------
# Seconds between host samples. 60 gives 30 readings inside the narrowest
# bucket the statistics pages offer (half an hour), which is enough for an
# average to mean something while costing ~1440 rows a day.
SYSTEM_SAMPLE_S = _int("WC_SYSTEM_SAMPLE_S", 60)
# Kept shorter than usage retention on purpose: these rows arrive on a timer
# whether or not the console is used, so they are the one table that grows
# without anybody doing anything.
SYSTEM_RETENTION_DAYS = _int("WC_SYSTEM_RETENTION_DAYS", 30)

# A model id, and the one rule that makes it safe to hand to the CLI. Square
# brackets are allowed for the documented "[1m]" context-window suffix
# (`claude-opus-5[1m]`), which the CLI itself tells users to append.
#
# The leading character is the security-relevant part. A model id reaches the
# child process as the *value* of `--model`, and `runner._build_cmd_direct`
# already protects the prompt from exactly this by putting it after a `--`
# sentinel "where a leading dash cannot be mistaken for a CLI flag" -- the model
# has no sentinel, so the check has to live in the value. Without it,
# `-dangerously-skip-permissions` and `-p` were both accepted model ids, and a
# plan carrying `[:--mcp-config=/tmp/evil.json]` put an attacker-chosen argv
# token into the subprocess.
#
# Defined here rather than in app.py because orchestrator.py needs the same rule
# for models it reads out of model-authored plan text, and two copies of a
# security pattern in one repository is the drift this project keeps paying for.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\[\]-]*$")
MODEL_ID_MAX = 120


def valid_model_id(value: str | None) -> bool:
    """Whether *value* is safe to pass as the argument to ``--model``."""
    if not value or not isinstance(value, str):
        return False
    if len(value) > MODEL_ID_MAX:
        return False
    return bool(MODEL_ID_RE.match(value))


MAX_CONCURRENT = _int("WC_MAX_CONCURRENT", 3)  # concurrent claude processes
TURN_TIMEOUT_S = _int("WC_TURN_TIMEOUT_S", 300)  # 5 min wall-clock per turn
PROMPT_MAX_CHARS = _int("WC_PROMPT_MAX_CHARS", 8000)  # cap on user prompt length

# A turn that ends cleanly (no CLI/network error) but produces near-empty
# output -- observed on small gateway models asked to self-identify, see
# CLAUDE.md's Qwen3.5 note -- is retried up to this many times before the
# last attempt is delivered as-is. 0 disables the retry.
TURN_RETRY_MAX = _int("WC_TURN_RETRY_MAX", 2)

# Seconds to wait before retrying a turn that failed because the Claude CLI
# stopped producing output mid-stream ("The response stopped arriving …").
# Only one retry is attempted regardless of TURN_RETRY_MAX.
PROXY_TURN_RETRY_DELAY_S = _int("WC_PROXY_TURN_RETRY_DELAY_S", 5)
# Below this many output tokens, with no text content, a turn counts as a
# non-answer for retry purposes.
TURN_RETRY_MIN_TOKENS = _int("WC_TURN_RETRY_MIN_TOKENS", 5)

# --- web console address ---------------------------------------------------
# The public-facing URL of this WebConsole instance. Users can edit this in
# the App tab of Settings so the UI can build shareable links (comparison
# reports, PDFs, etc.) that point back to the site rather than the proxy
# port or internal hostname.  Environment default is unset so the admin
# must opt in rather than leaking a guess.
WC_WEBCONSOLE_URL = _str("WC_WEBCONSOLE_URL")

# --- misc ----------------------------------------------------------------
VERSION = "WebConsole_0.17.0"
MAX_UPLOAD_BYTES = _int("WC_MAX_UPLOAD_BYTES", 524288000)  # 500 MB upload cap


# Validate at import time -- don't silently boot with weak secrets.
def validate() -> None:
    if not SESSION_SECRET or len(SESSION_SECRET) < 32:
        raise RuntimeError(
            "WC_SESSION_SECRET is unset or too short. "
            "Set it to: $(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )
    if not PROJECTS_ROOT:
        raise RuntimeError("WC_PROJECTS_ROOT must be set to a project directory root")
    if MAX_TUNNELS < 1:
        raise RuntimeError("WC_MAX_TUNNELS must be >= 1")

    if PROXY_ENABLED and (not PROXY_TOKEN or len(PROXY_TOKEN) < 32):
        raise RuntimeError(
            "WC_PROXY_TOKEN is unset or too short while proxy mode is enabled. "
            "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
