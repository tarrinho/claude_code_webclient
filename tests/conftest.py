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
# SELECT, never db.init() against it (CLAUDE.md rule 9). Both are VERIFIED
# reachable before being trusted -- a configured or resolved model id is
# worthless if nothing actually serves it, which is exactly the trap
# CLAUDE.md 0.1 documents: a model id sent to the wrong (or no) backend
# answers 429 "No deployments available" rather than anything that names the
# real problem.
#
#   enforced (the default): the fixed value configured in Settings -> App, or
#     config.TESTING_MODEL_DEFAULT if never set. Verified with
#     _served_by_any_enabled_machine -- an existence check ("does at least
#     one enabled machine serve this"), deliberately not
#     bin/wc-backend-env.py's own --resolve-model, which answers the
#     stricter "exactly one" (for routing a bare `--model` flag
#     unambiguously) and refuses on ambiguity. Measured live during this
#     design: four enabled machines all legitimately serve the same
#     configured model, and --resolve-model correctly refuses to pick one --
#     which would have looked identical to "nobody serves this" to a caller
#     that could not tell the two failure shapes apart.
#   not enforced: the model actually in effect for *this* agent session,
#     resolved the same way bin/wc-claude.sh does --
#     `bin/wc-backend-env.py --profile "$WC_PROFILE" --json`'s own "model"
#     field, then verified with `--check-model` against that same profile
#     (the same verification bin/wc-claude.sh's own real session startup
#     already trusts) -- because that field is the backend's own configured
#     default, unverified, and a machine's default can drift out of its own
#     active_models list.
#
# Either source failing verification falls through to the other, and if both
# fail, to the bare code default (config.TESTING_MODEL_DEFAULT's value) --
# unverified at that point, because a broken resolution must not be the
# reason a whole test run cannot start, the same fail-open reasoning
# resource_guard already follows.
if not os.environ.get("WC_TESTING_MODEL"):
    def _served_by_any_enabled_machine(model: str, db_path: str) -> bool:
        """Does at least one enabled machine serve *model* -- by its own
        default `model` column or its declared `active_models` list?
        Existence, not uniqueness; see the module comment above for why
        --resolve-model is the wrong tool for this question."""
        import json
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT model, active_models FROM ai_machines WHERE enabled = 1"
            ).fetchall()
            con.close()
        except Exception:
            return False  # cannot verify -- treat as not confirmed, not as served
        for row in rows:
            if (row["model"] or "").strip() == model:
                return True
            raw = row["active_models"]
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, list) and model in parsed:
                return True
        return False

    def _resolve_via_current_agent(repo_root, profile: str | None) -> str:
        """The model bin/wc-claude.sh would actually route this session to,
        verified reachable on that same backend. Empty string on any
        failure -- unset WC_PROFILE, no backend, the script missing, a
        timeout, or the resolved model failing --check-model."""
        import json
        import subprocess

        if not profile:
            return ""
        script = repo_root / "bin" / "wc-backend-env.py"
        python = str(repo_root / ".venv" / "bin" / "python")
        try:
            proc = subprocess.run(
                [python, str(script), "--profile", profile, "--json"],
                capture_output=True, text=True, timeout=5, cwd=str(repo_root),
            )
            resolved = json.loads(proc.stdout).get("model")
        except Exception:
            return ""
        if not resolved:
            return ""
        try:
            check = subprocess.run(
                [python, str(script), "--profile", profile,
                 "--check-model", resolved],
                capture_output=True, text=True, timeout=5, cwd=str(repo_root),
            )
        except Exception:
            return ""
        return resolved if check.returncode == 0 else ""

    def _resolve_testing_model() -> str:
        repo_root = pathlib.Path(__file__).resolve().parent.parent
        default = os.environ.get("WC_TESTING_MODEL_DEFAULT", "claude-opus-5")
        enforce_default = os.environ.get(
            "WC_TESTING_MODEL_ENFORCE_DEFAULT", "1") == "1"
        db_path = os.environ.get(
            "WC_PROD_DB_PATH_FOR_TESTING_MODEL",
            str(repo_root / "data" / "webconsole.db"))

        configured, enforce = default, enforce_default
        try:
            import sqlite3

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

        profile = os.environ.get("WC_PROFILE")

        if enforce:
            # Deliberately does NOT fall through to _resolve_via_current_agent
            # on failure: the whole point of "enforce" is a fixed value that
            # does not depend on which session or backend happens to be
            # running the suite. Falling back to the environment here would
            # make "enforce" silently stop meaning that the moment the
            # configured value drifted out of every machine's declared list.
            if configured and _served_by_any_enabled_machine(configured, db_path):
                return configured
            return default
        else:
            resolved = _resolve_via_current_agent(repo_root, profile)
            if resolved:
                return resolved
            if configured and _served_by_any_enabled_machine(configured, db_path):
                return configured
            return default

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


# ── Settings cache isolation ─────────────────────────────────────────────────
#
# Same shape of problem as the rate limiter above, and it arrived the same way.
# `routes/misc._settings_cache` is module-level, so it is one dict for the whole
# process, and `GET /api/settings` serves from it for 30s
# (`_SETTINGS_CACHE_TTL_S`).
#
# The production path is correct and this fixture is not covering for a bug in
# it: the only writer of settings is `handle_settings_patch`, and it calls
# `_settings_invalidate()` before returning. Tests are different because they
# build state *underneath* the endpoint -- `db.setting_set`, fresh fixture
# databases, a different selected backend per subtest -- none of which is a
# PATCH, so nothing invalidates and the endpoint correctly serves the payload
# it built for the previous test.
#
# Measured on 2026-09-10: 40 failures across `test_model_settings.py`,
# `test_voice_turn.py`, `test_qa_voice_model_per_backend.py` and
# `test_qa_usage.py`, every one of which passed when run alone. The symptom is
# misleading in the same way the limiter's was -- the clearest example asserted
# `voice_model_options` and got `[]`, which reads as "the options are not being
# built" rather than "you are looking at the last test's answer".
#
# Clearing before each test, for the reason given above: a test that fails
# part-way through still leaves a warm cache behind, and cleaning up "after"
# would skip exactly the runs that need it.
@_pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Drop the process-global GET /api/settings cache before each test."""
    try:
        from routes import misc as _misc
    except Exception:  # pragma: no cover - a run that cannot import the app
        yield
        return
    _misc._settings_invalidate()
    yield


# ── Leaked database connections must not hold the interpreter open ───────────
#
# A test whose `asyncSetUp` raises never reaches its `asyncTearDown`, so
# `db.close()` is never called. aiosqlite's connection worker is a *non-daemon*
# thread, so `threading._shutdown()` then waits on it for ever: pytest prints
# its full result summary and the process hangs until the caller's timeout
# kills it. The results are real and the exit code is a lie -- 124 or 143 on a
# run that actually finished in under two seconds.
#
# Measured 2026-09-12, isolated with a probe rather than inferred: with the
# connection left open, one thread survives (`_connection_worker_thread`,
# daemon=False) and the interpreter hangs; closing it first leaves zero threads
# and exits 0. faulthandler shows nothing, because the hang happens after
# pytest has torn its own hooks down.
#
# That cost real time before it was understood: this session chased it as four
# separate "timeouts" and nearly attributed it to the code under test, and a
# peer session independently ruled it "pre-existing, out of scope" seven times
# in one evening. The trigger today is 154 call sites still passing
# owner_id="admin" to `chat_create`, which now rejects it -- but the trap is
# not specific to that. *Any* future setUp failure, for any reason, hangs the
# run instead of reporting it, which is the worst failure mode a suite can
# have: it hides its own result.
#
# So this is deliberately a safety net and not a fix for whatever raised. It
# runs after every test, closes a connection only if one was left open, and
# never fails a test for it -- a leak is the other bug's symptom to report, not
# this fixture's to punish.
#
# Closing from a *different* event loop than the one that opened it is the part
# worth verifying, and it was: by teardown the test's loop is gone, and
# `asyncio.run(db.close())` on a fresh loop succeeds and joins the thread.
# One file (`self.addCleanup(lambda: asyncio.run(db.close()))`) had already
# found that independently.
@_pytest.fixture(autouse=True)
def _close_leaked_db_connection():
    """Close a connection a failed setUp left behind, so the run can exit."""
    yield
    try:
        import asyncio as _asyncio

        import db as _db
    except Exception:  # pragma: no cover - a run that cannot import the app
        return
    if getattr(_db, "db_conn", None) is None:
        return  # the test closed it properly; nothing leaked
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop running, which is the expected teardown state
    else:  # pragma: no cover - an async runner would own the close itself
        return
    try:
        _asyncio.run(_asyncio.wait_for(_db.close(), timeout=10))
    except Exception as exc:  # noqa: BLE001 - best effort, never fail a test
        # Said out loud rather than swallowed: a close that cannot complete
        # means the run may still hang, and silence would send the next person
        # back to chasing a phantom timeout.
        print(f"\nconftest: could not close a leaked db connection: "
              f"{type(exc).__name__}: {exc}")


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
