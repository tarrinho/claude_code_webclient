"""A booted database and an authenticated HTTP client, for async route tests.

Star-imported by tests that need to drive a real route against a real schema:

    from tests.fixtures.db import *  # noqa: F401,F403 -- db, test_app

Two fixtures come out of it, and both are function-scoped because the tests
that use them assert on an empty table ("initially empty") and would otherwise
see whatever the previous test left behind.

WHY THE DATABASE IS A THROWAWAY, EVERY TIME
-------------------------------------------
``db.init()`` migrates whatever it opens. CLAUDE.md rule 9 is explicit that it
must never run against the production database, so this points
``config.DB_PATH`` at a fresh temporary file before calling it and restores the
original afterwards. Nothing here ever reads or writes the real one. The
session store is redirected the same way, because ``auth`` persists sessions
through its own handle and would otherwise write into the deployment's file.

WHY THE CLIENT AUTHENTICATES FOR REAL
-------------------------------------
``AuthMiddleware`` answers 401 to any ``/api/`` request without a session, and
``CSRFMiddleware`` answers 403 to any mutating request whose ``X-CSRF-Token``
header does not match its ``wc_csrf`` cookie for that specific session. A
fixture that stubbed either one out would let a route ship with its auth or its
CSRF wiring broken and still show green -- which is the failure both middlewares
exist to catch. So the client holds a genuine session created through
``auth.session_new`` and sends the matching header on every request.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest_asyncio

#: A deliberately small, fixed capability corpus.
#:
#: Written out here rather than copied from the production table, because these
#: tests assert on structure -- that a task type resolves, that a pin overrides
#: the generated ladder, that a gate refuses a fourth rung -- and production
#: numbers move every time the benchmark runs. Seeding from the live table made
#: those assertions depend on today's measurements, which is how a structural
#: test starts failing for a reason that has nothing to do with structure.
#:
#: Costs stay far below ``tiered_delegation.BUDGET_USD`` (3.50) so the budget
#: gate is not what a ladder test trips over; the tests that mean to exercise
#: the budget say so themselves.
#:
#: Columns: model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s
_SEED_CAPABILITY = (
    ("seed-fast",     "coding",        1.00, 24, 0.04, 8.0),
    ("seed-balanced", "coding",        1.00, 24, 0.09, 14.0),
    ("seed-strong",   "coding",        1.00, 18, 0.48, 16.0),
    ("seed-fast",     "reasoning",     0.83, 12, 0.04, 22.0),
    ("seed-balanced", "reasoning",     1.00, 12, 0.09, 24.0),
    ("seed-fast",     "reviewer-gate", 1.00, 12, 0.04, 6.0),
    ("seed-balanced", "reviewer-gate", 1.00, 12, 0.09, 9.0),
)


async def _seed_capability(db_module) -> None:
    """Give the throwaway database a capability table worth querying.

    Without this every ladder route answers 400 "no delegation_capability rows
    for this task type", which reads like a route bug and is really an empty
    fixture.
    """
    await db_module.db_conn.executemany(
        "INSERT OR REPLACE INTO delegation_capability "
        "(model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, "
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        _SEED_CAPABILITY,
    )
    await db_module.db_conn.commit()


@pytest_asyncio.fixture
async def db(monkeypatch):
    """The ``db`` module, bound to a fresh temporary database.

    Yields the module itself rather than a connection, because callers use both
    the connection (``db.db_conn``) and the module-level accessors
    (``db.delegation_rows_all()``).
    """
    import config
    import db as db_module

    with tempfile.TemporaryDirectory(prefix="wc-test-db-") as tmp:
        tmp_path = Path(tmp)
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "webconsole.db"))
        monkeypatch.setattr(
            config, "SESSION_DB_PATH", str(tmp_path / "webconsole-sessions.db")
        )

        previous = db_module.db_conn
        await db_module.init()
        await _seed_capability(db_module)
        try:
            yield db_module
        finally:
            # Close the test connection before restoring, or the temporary
            # directory is removed out from under an open WAL handle.
            if db_module.db_conn is not None:
                await db_module.db_conn.close()
            db_module.db_conn = previous


@pytest_asyncio.fixture
async def test_app(db):
    """An httpx client that reaches the real ASGI app as a logged-in admin.

    Depends on ``db`` so the schema exists before any route runs. The app's
    lifespan is deliberately not started: it launches background pollers and
    opens its own database, neither of which a route test needs, and ``db``
    has already done the one part that matters.
    """
    async for client in _client_with_role("admin"):
        yield client


@pytest_asyncio.fixture
async def non_admin_app(db):
    """The same client, signed in as a non-admin.

    Exists so the admin-only routes can be tested for the half that matters:
    that someone without the role is refused. Asserting only the allowed path
    would leave `_require_admin` free to disappear without a single test
    noticing.
    """
    async for client in _client_with_role("member"):
        yield client


async def _client_with_role(role: str):
    """One authenticated client for *role*, torn down after the test."""
    import auth
    from app import app as asgi_app

    session_id, csrf_token = auth.session_new(f"test-{role}", role=role)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
        cookies={"wc_session": session_id, "wc_csrf": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    try:
        yield client
    finally:
        await client.aclose()
        auth.session_drop(session_id)
