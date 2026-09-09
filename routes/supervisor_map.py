"""Routes for /api/supervisor-map — the radial mind map data."""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from routes.db_supervisor_map import supervisor_map

_log = logging.getLogger("wc.app")

router = APIRouter()


@router.get("/api/supervisor-map")
async def handle_supervisor_map(request: Request):
    """Return the supervisor map tree for the logged-in user.

    There used to be an ``_enrich_messages`` pass here that walked the finished
    tree and ran ``db.messages_last(node["id"])`` once per chat node, to attach
    the preview the detail drawer shows. It is gone, and the preview now comes
    from ``chat_last_activity`` inside ``supervisor_map`` -- a single grouped
    query that was already being run to classify every conversation, and which
    already returns the same first 200 characters.

    Two things made the old pass worth deleting rather than tuning. It was one
    query per conversation on a panel that now refreshes itself every ten
    seconds, so a dozen conversations meant a dozen extra reads every tick. And
    ``messages_last`` takes no owner: the ids it was given came from the
    owner's own chat list, so nothing leaked, but the safety lived in the
    caller rather than in the query. Reading from the owner-scoped query
    removes both at once.
    """
    session = request.state.session
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        tree = await supervisor_map(session["user"])
        return JSONResponse(content=tree)
    except Exception:
        _log.exception("supervisor_map failed")
        raise HTTPException(status_code=500, detail="Data unavailable")
