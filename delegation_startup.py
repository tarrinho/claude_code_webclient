# delegation_startup.py -- load the capability table and enforce spec 1.1.
#
# Kept apart from tiered_delegation.py because that module is deliberately
# pure: no database, no config read, no import from app. This is the half
# that touches the database, so the pure half stays testable against a
# constructed table rather than against production data.
from __future__ import annotations

import logging

import db
from routes.db_delegation import rows_to_capability
from tiered_delegation import CapabilityTable

_log = logging.getLogger("wc.app")


class DelegationConfigError(RuntimeError):
    """Spec 1.1: the system refuses to start rather than route on bad data."""


async def load_capability_table() -> CapabilityTable:
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = await db.delegation_operational_all()
    return CapabilityTable(rows, operational=operational)


async def live_known_models() -> frozenset[str] | None:
    """The model combo box's live contents (spec 9.3), or None when they
    could not be read.

    Not module-private: spec 1.1 requires the same six checks to run "with
    the same code path and the same error text" at all three moments a table
    is validated -- config load (`validate_or_die`, below), a row write, and
    an operational flip (`routes/delegation.py`'s `handle_row_put` and
    `handle_operational_put`). All three call this and feed the result into
    the same `CapabilityTable.validate(known_models=...)`, so "resolves"
    means the same thing regardless of which of the three asks.

    None is not the same as an empty set. An empty set would tell
    `CapabilityTable.validate` "the combo box has nothing in it", which would
    fail every rung's resolution -- turning a local read failure into a
    refusal, which is a worse outcome than the gap this closes, at any of the
    three call sites (a write endpoint failing because a backend happens to
    be unreachable is if anything worse than boot failing the same way: the
    operator is looking at the page trying to fix data, not restarting a
    process). None instead means "this run could not find out", and
    `validate()` already treats that the same as the caller passing nothing
    at all: it falls back to `config.KNOWN_MODELS`, today's shape-only
    behaviour. The two failure modes -- "this model does not resolve" and "I
    could not find out which models exist" -- must not share a code path, so
    this is the only place that catches the second one; a real resolution
    problem still comes back from `table.validate()` and still gets refused
    by every caller.

    `routes.machines.known_backend_models` is DB-only (see its docstring) so
    this is not expected to fail in practice, but no caller may treat "the
    query raised" as "this data is unsafe" -- that would convert a local
    hiccup into a refusal that has nothing to do with what the operator is
    trying to fix.

    Fetched fresh on every call, no caching: every caller here is either the
    once-per-boot lifespan or an admin settings write, both cheap enough that
    sharing a cache across calls would be solving a problem that does not
    exist yet.
    """
    try:
        from routes.machines import known_backend_models
        return await known_backend_models()
    except Exception:
        _log.warning(
            "delegation: could not read the live model list for spec "
            "1.1's resolution check; falling back to config.KNOWN_MODELS",
            exc_info=True,
        )
        return None


async def validate_or_die() -> CapabilityTable:
    """Build the table and refuse to start if any invariant is broken.

    Every problem is reported at once. Reporting the first only means one
    restart per problem, and the operator is fixing data in a settings page
    rather than reading a stack trace.
    """
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = await db.delegation_operational_all()
    table = CapabilityTable(rows, operational=operational)
    known_models = await live_known_models()
    problems = table.validate(known_models=known_models)
    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise DelegationConfigError(
            "tiered delegation configuration is invalid; refusing to start:\n"
            + detail
        )
    # Logged from the `operational` set already fetched above, not from
    # table._operational -- CapabilityTable deliberately exposes only
    # is_operational(task_type), and reaching past that for data this
    # function already holds would be gratuitous coupling to a private
    # attribute.
    if operational:
        _log.info("delegation: operational task types: %s",
                   ", ".join(sorted(operational)))
    else:
        # The shipped state for 0.19.0. Said out loud so an operator wondering
        # why nothing routes finds the answer in the log rather than in a spec.
        _log.info(
            "delegation: no task type is operational; routing falls back to "
            "the existing behaviour for every task"
        )
    return table
