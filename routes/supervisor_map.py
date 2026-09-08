"""Routes for /api/supervisor-map — the radial mind map data."""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from routes.db_supervisor_map import supervisor_map

_log = logging.getLogger("wc.app")

router = APIRouter()


@router.get("/api/supervisor-map")
async def handle_supervisor_map(request: Request):
    """Return the supervisor map tree for the logged-in user."""
    session = request.state.session
    try:
        tree = await supervisor_map(session["user"])
        return JSONResponse(content=tree)
    except Exception:
        _log.exception("supervisor_map failed")
        raise HTTPException(status_code=500, detail="Data unavailable")
