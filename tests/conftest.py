"""Test-wide environment defaults, applied before anything imports config.

``config`` resolves ``LOG_FILE`` at import time, so this cannot be a fixture:
by the time a fixture runs, the module-level constant is already bound and the
rotating handler is already pointed at whatever it said. pytest imports
conftest before collecting test modules, which is the only hook early enough.

Why it exists: the suite was writing into the production log. That was fixed
once, for the servers the suite *spawns* -- the browser fixture passes
``WC_LOG_FILE`` into its subprocess -- and the fix was reported as done. But
four test modules drive the app with an in-process ``TestClient``, which imports
``app`` inside the pytest process itself and configures logging there, with
``WC_LOG_FILE`` unset. Those bypassed the subprocess fix entirely and kept
appending: ``ip=testclient``, ``user=alice``, ``chat_id=c1`` and a stored-XSS
probe all appear in logs/webconsole.log, interleaved with real traffic.

The test that was supposed to prevent this asserted the property only for the
redirected-subprocess path, so it passed throughout. A guard that covers one of
two routes reads as covered and is worse than none, because nobody looks again.
"""
from __future__ import annotations

import os
import pathlib
import tempfile

# Not overridden if the caller already set it: test_qa_log_path drives
# WC_LOG_FILE deliberately, and a harness or CI may point it somewhere it
# collects from.
if not os.environ.get("WC_LOG_FILE"):
    _log_dir = pathlib.Path(tempfile.gettempdir()) / "wc-test-logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    # A stable path rather than a fresh directory per run: several sessions run
    # this suite on one machine, and a per-run directory would leave a litter of
    # them. Interleaving between concurrent runs is fine here -- that is the
    # cost this file exists to keep *out* of the production log, not something
    # the test log needs to be protected from.
    os.environ["WC_LOG_FILE"] = str(_log_dir / "suite.log")

# Session persistence now lives in a separate database to avoid writer
# collision with the main app DB.  Point it at a throwaway so tests that
# spawn subprocesses never touch the production sessions store.
if not os.environ.get("WC_SESSION_DB_PATH"):
    _sdir = pathlib.Path(tempfile.gettempdir()) / "wc-test-sessions"
    os.environ["WC_SESSION_DB_PATH"] = str(_sdir / "sessions.db")

# The resource guard refuses a turn when the host is short of memory, which is
# correct in production and makes a test suite depend on how much RAM happened
# to be free while it ran. Measured on this host: `test_app.py`'s
# StreamPersistenceTests passed earlier in the day and failed hours later with
# 522 MB available against a 400 MB floor -- the turn was refused, so nothing
# persisted and the assertions read as a persistence bug. Nothing about those
# tests concerns memory.
#
# Off by default for the suite, and only when the caller has not spoken:
# tests/test_qa_resource_guard.py is the one place that must see the real rule,
# and it is already immune -- it passes its own `env=` dict (`_CLEAN`) precisely
# so a developer's shell variable cannot make it pass for the wrong reason.
if not os.environ.get("WC_RESOURCE_GUARD"):
    os.environ["WC_RESOURCE_GUARD"] = "off"


# ── Testing default model ────────────────────────────────────────────────────
#
# Resolved once, here, before any test file is collected -- every test that
# needs "a" model id imports tests.testing_model.TESTING_MODEL instead of
# hardcoding "claude-opus-5" (or any other literal) wherever the specific
# value is not itself the thing under test.
#
# Two sources, chosen by the Settings dialog's "Enforce testing default
# model" knob, read once from the production database -- read-only, a single
# SELECT, never db.init() against it (CLAUDE.md rule 9):
#
#   enforced (the default): the fixed value configured in Settings -> App, or
#     config.TESTING_MODEL_DEFAULT if never set.
#   not enforced: the model actually in effect for *this* agent session,
#     resolved the same way bin/wc-claude.sh does --
#     `bin/wc-backend-env.py --profile "$WC_PROFILE" --json`'s own "model"
#     field. Confirmed live during design: this resolves to "claude-sonnet-5"
#     for a session pinned to the anthropic-oauth profile.
#
# Every failure mode here (no production DB yet, WC_PROFILE unset, the script
# missing, a timeout) falls back to the configured/default value rather than
# raising -- a broken resolution must not be the reason a test run cannot
# start, the same reasoning resource_guard's own fail-open follows.
if not os.environ.get("WC_TESTING_MODEL"):
    def _resolve_testing_model() -> str:
        import json
        import sqlite3
        import subprocess

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        default = os.environ.get("WC_TESTING_MODEL_DEFAULT", "claude-opus-5")
        enforce_default = os.environ.get(
            "WC_TESTING_MODEL_ENFORCE_DEFAULT", "1") == "1"

        configured, enforce = default, enforce_default
        try:
            db_path = os.environ.get(
                "WC_PROD_DB_PATH_FOR_TESTING_MODEL",
                str(repo_root / "data" / "webconsole.db"))
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            rows = {
                r["key"]: r["value"] for r in con.execute(
                    "SELECT key, value FROM settings WHERE key IN "
                    "('testing_default_model', 'testing_model_enforce')")
            }
            con.close()
            configured = rows.get("testing_default_model") or default
            raw = rows.get("testing_model_enforce")
            enforce = enforce_default if raw is None else raw == "1"
        except Exception:
            pass  # no production DB yet, or unreadable -- use the defaults above

        if enforce:
            return configured

        profile = os.environ.get("WC_PROFILE")
        if not profile:
            return configured
        try:
            script = repo_root / "bin" / "wc-backend-env.py"
            proc = subprocess.run(
                [str(repo_root / ".venv" / "bin" / "python"), str(script),
                 "--profile", profile, "--json"],
                capture_output=True, text=True, timeout=5, cwd=str(repo_root),
            )
            resolved = json.loads(proc.stdout).get("model")
            return resolved or configured
        except Exception:
            return configured

    os.environ["WC_TESTING_MODEL"] = _resolve_testing_model()


# ── Rate limiter isolation ───────────────────────────────────────────────────
#
# `_Ratelimiter._buckets` is a ClassVar, so it is one dict for the whole
# process and a fresh instance inherits it. Every in-process TestClient login
# arrives as ip "testclient" on path "/login", so the suite spends one shared
# budget of RATE_LIMIT_MAX (120) per RATE_LIMIT_WINDOW (60s) across every test
# module that logs in.
#
# What that looks like when it runs out is the reason this is worth a fixture:
# the limiter answers 429 to the *fixture* login, so the failure is reported
# against whichever test happened to be holding the 121st login, with an
# assertion about the thing that test was really checking. Measured:
# `test_qa_session_delete.py::test_refuses_to_delete_non_webconsole_file`
# passed alone and failed after `test_qa_rate_limit.py` with
# "429 != 200 : fixture must log in" -- nothing to do with deleting sessions,
# and it moves to a different test as soon as the file order changes.
#
# Clearing before each test rather than after: a test that fails part-way
# through still leaves its buckets behind, and "after" would skip the cleanup
# on exactly the runs that need it most. tests/test_qa_rate_limit.py is
# unaffected -- it builds the state it asserts on inside a single test, and
# starting each of its tests from empty is what it already assumes.
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Empty the process-global bucket map before each test."""
    try:
        import rate_limit
    except Exception:  # pragma: no cover - a run that cannot import the app
        yield
        return
    rate_limit._Ratelimiter._buckets.clear()
    yield


# ── Capability guard ─────────────────────────────────────────────────────────
#
# Refuses a run that would silently skip a whole layer of the suite because of
# the interpreter it was invoked with. See tests/capabilities.py for why that is
# narrower than "check the dependencies are installed".
#
# Four decisions worth stating, because each has an obvious wrong answer:
#
# 1. It fires on what was *collected*, not on what exists. `pytest
#    tests/test_qa_db.py` needs neither quickjs nor a browser, and a
#    session-wide refusal would train everyone to set the opt-out permanently --
#    at which point the guard is gone and the variable stays.
# 2. It fires only when the capability is missing *here* and present in the
#    venv. Absent from both is a fact about the machine, and the per-file
#    `skipUnless` guards report that correctly; escalating it would stop a
#    laptop with no Chromium from running the suite at all.
# 3. It does not replace those per-file skips. They answer "can this machine
#    do it"; this answers "did you invoke the wrong interpreter". Two questions,
#    two mechanisms, and only one of them is a mistake.
# 4. The opt-out prints what it is giving up. An escape hatch that silences the
#    count is the original defect (rules.md #50) with an extra step.
import os as _os

import pytest

_ALLOW_PARTIAL = "WC_ALLOW_PARTIAL_SUITE"


def _needs_capability(item, cap_name: str) -> bool:
    """Whether *item* belongs to a file that needs *cap_name*.

    By module path rather than by importing anything: at collection time the
    module is already imported, and asking it questions risks running the very
    import that is about to fail.
    """
    path = str(getattr(item, "fspath", "") or "")
    if cap_name == "quickjs":
        return path.endswith("test_frontend_syntax.py")
    if cap_name == "playwright-driver":
        module = getattr(item, "module", None)
        return module is not None and hasattr(module, "sync_playwright")
    return False


def pytest_collection_modifyitems(session, config, items):
    from . import capabilities

    wrong = capabilities.wrong_interpreter()
    if not wrong:
        for cap in capabilities.missing_everywhere():
            affected = sum(1 for i in items if _needs_capability(i, cap.name))
            if affected:
                print(
                    f"\nNOTE: {cap.name} is unavailable on this machine, so "
                    f"{affected} collected test(s) covering {cap.covers} will "
                    f"skip. This is a property of the machine, not of the "
                    f"invocation."
                )
        return

    blocking = []
    for cap, where in wrong:
        affected = sum(1 for i in items if _needs_capability(i, cap.name))
        if affected:
            blocking.append((cap, where, affected))
    if not blocking:
        return

    lines = [
        "",
        "This interpreter cannot run part of what you asked it to collect.",
        "",
    ]
    for cap, where, affected in blocking:
        lines.append(
            f"  {cap.name}: missing here, {where} -- {affected} collected "
            f"test(s) covering {cap.covers} would skip."
        )
    lines += [
        "",
        f"  Run:  .venv/bin/python -m pytest {' '.join(config.invocation_params.args)}",
        "",
        "  A skip reads as 'not applicable here', which is indistinguishable",
        "  from 'ran and passed' in a total. That is rules.md #50, and it has",
        "  since recurred against readers who had read the rule -- which is why",
        f"  this stops the run instead of warning. Set {_ALLOW_PARTIAL}=1 to",
        "  proceed anyway; the count above will be reported either way.",
        "",
    ]
    message = "\n".join(lines)
    if _os.environ.get(_ALLOW_PARTIAL):
        print(message)
        print(f"{_ALLOW_PARTIAL} is set -- continuing without the tests listed above.")
        return
    # UsageError, not SystemExit: raising SystemExit from a hook is reported
    # as INTERNALERROR with a traceback, which buries the actionable line
    # under noise that looks like a bug in the guard. UsageError is pytest's
    # own channel for "you invoked this wrong" and prints the message alone.
    raise pytest.UsageError(message)
