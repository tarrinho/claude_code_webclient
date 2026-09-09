"""Watch incoming agent traffic for a sync request, and queue it for approval.

Design: docs/superpowers/specs/2026-09-09-transport-project-sync-design.md.

Mirrors auto_answer.py's `_loop`/`_pass` shape. auto_answer.py's own
docstring states this codebase's rule against heuristic text matching --
*"Deliberately not a heuristic on the prompt text... matching phrases
against model-authored prose is what made [it] fire on unrelated text
elsewhere in this tree."* The same rule applies here, more forcefully:
this trigger ends in a file-writing operation queued for a human to
approve, so a false positive wastes a person's attention on a request
nobody made.

Convention: an incoming message must **start** with the literal token
``SYNC_REQUEST``, followed by the transport's name or id -- e.g.
``SYNC_REQUEST Kali3``. The transport is named explicitly rather than
inferred from the sender's identity: inferring it would need a live
reverse-lookup against every transport's remote session registry (fragile,
and a second network round-trip per incoming message on every poll tick)
for a fact the sender already knows about itself.

Nothing here ever pushes a file. This only creates a `pending` row in
transport_sync_requests; a human approves or rejects it through the UI.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import db
import transcripts

_log = logging.getLogger("wc.sync_request_watcher")

_MARKER_RE = re.compile(r"^SYNC_REQUEST\s+(\S+)", re.ASCII)

_task: asyncio.Task | None = None
def start(interval_s: float = 10.0) -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(interval_s))


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


async def _loop(interval_s: float) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("sync-request watch pass failed")


async def _pass() -> None:
    events = await transcripts.agent_traffic(limit=200, scan_files=12)
    transports = await db.ssh_transports_list_all()
    by_name = {t["name"].strip().lower(): t for t in transports}
    by_id = {t["id"]: t for t in transports}

    # Pending-request check per owner, cached across this pass -- a DB
    # query, not an in-memory set: an in-memory "already handled" marker
    # would never evict once a request resolved, permanently suppressing a
    # legitimate later re-request for the same transport. One pending
    # request per transport at a time is the right dedup unit regardless of
    # which sender's message triggers it -- two different senders asking to
    # sync the same transport should collapse to one approval, not two.
    pending_by_owner: dict[str, set[str]] = {}

    for event in events:
        if event.get("direction") != "in":
            continue
        text = str(event.get("text") or "").strip()
        match = _MARKER_RE.match(text)
        if not match:
            continue

        needle = match.group(1).strip()
        transport = by_id.get(needle) or by_name.get(needle.lower())
        if not transport:
            _log.info(
                "sync_request unresolved sender=%s transport_ref=%r -- no "
                "matching transport by name or id",
                event.get("sender"), needle,
            )
            continue

        owner_id = transport["owner_id"]
        if owner_id not in pending_by_owner:
            pending = await db.sync_request_list_pending(owner_id)
            pending_by_owner[owner_id] = {r["transport_id"] for r in pending}
        if transport["id"] in pending_by_owner[owner_id]:
            continue

        try:
            await db.sync_request_create(
                transport["id"], owner_id,
                requested_by=str(event.get("sender") or "unknown"),
                status="pending",
            )
            pending_by_owner[owner_id].add(transport["id"])
            _log.info(
                "sync_request queued transport=%s requested_by=%s",
                transport["name"], event.get("sender"),
            )
        except Exception:
            _log.exception(
                "sync_request queue failed transport=%s", transport.get("name"))
