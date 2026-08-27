# config.py — WebConsole configuration (env-driven, fail-fast).
#
# All secrets, endpoints, and limits come from the environment. Nothing sensitive
# is hard-coded. Missing *required* values fail fast at boot with a clear message.
from __future__ import annotations

import os


def _str(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    v = _str(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


# --- listen ----------------------------------------------------------------
HOST = _str("WC_HOST", "127.0.0.1")
PORT = _int("WC_PORT", 8080)
# Bind to this interface only. For Tailscale use: the device's tailnet IP
# (e.g. "100.x.x.x"), never "0.0.0.0" for external exposure.
LISTEN_HOST = _str("WC_LISTEN_HOST", HOST)

# --- database --------------------------------------------------------------
DB_PATH = _str("WC_DB_PATH", "/data/webconsole.db")

# --- project directories ---------------------------------------------------
PROJECTS_ROOT = _str("WC_PROJECTS_ROOT", "/projects")

# --- auth ------------------------------------------------------------------
SESSION_SECRET = _str("WC_SESSION_SECRET")         # required, secrets.token_urlsafe(32)
SESSION_TTL_S  = _int("WC_SESSION_TTL_S", 7200)    # 2h absolute
SESSION_IDLE_S = _int("WC_SESSION_IDLE_S", 1800)   # 30m idle timeout
SESSION_MAX    = _int("WC_SESSION_MAX", 50)        # hard cap on concurrent sessions
LOGIN_RATE_MAX = _int("WC_LOGIN_RATE_MAX", 10)     # max attempts per window
LOGIN_RATE_WIN = _int("WC_LOGIN_RATE_WIN", 300)    # window in seconds (5 min)
LOGIN_BACKOFF  = _int("WC_LOGIN_BACKOFF", 30)      # base backoff seconds on lockout

# Allow plain-HTTP cookies (for Tailscale without TLS). DO NOT enable on public interfaces.
COOKIE_ALLOW_INSECURE = _bool("WC_COOKIE_ALLOW_INSECURE", False)

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

# --- Fallback: direct Claude Code subprocess --------------------------------
# These are used only when PROXY_ENABLED=False (not recommended for container).
MODEL_BASE_URL = _str("WC_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
MODEL_API_KEY  = _str("WC_MODEL_API_KEY", "")
MODEL_NAME     = _str("WC_MODEL_NAME", "claude-sonnet-4-20250514")

MAX_CONCURRENT = _int("WC_MAX_CONCURRENT", 3)        # concurrent claude processes
TURN_TIMEOUT_S = _int("WC_TURN_TIMEOUT_S", 300)      # 5 min wall-clock per turn
PROMPT_MAX_CHARS = _int("WC_PROMPT_MAX_CHARS", 8000) # cap on user prompt length

# --- misc ----------------------------------------------------------------
VERSION = "WebConsole_0.1.0"


# Validate at import time -- don't silently boot with weak secrets.
def validate() -> None:
    if not SESSION_SECRET or len(SESSION_SECRET) < 32:
        raise RuntimeError(
            "WC_SESSION_SECRET is unset or too short. "
            "Set it to: $(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )
    if not PROJECTS_ROOT:
        raise RuntimeError("WC_PROJECTS_ROOT must be set to a project directory root")
    if PROXY_ENABLED and (not PROXY_TOKEN or len(PROXY_TOKEN) < 32):
        raise RuntimeError(
            "WC_PROXY_TOKEN is unset or too short while proxy mode is enabled. "
            "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )