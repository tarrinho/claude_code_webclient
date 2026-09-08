"""Routes for /api/supervisor-map — the radial mind map data."""
from __future__ import annotations

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from routes.db_supervisor_map import supervisor_map

_log = logging.getLogger("wc.app")

router = APIRouter()


@router.get("/api/supervisor-map")
async def handle_supervisor_map(request: Request):
    """Return the supervisor map tree for the logged-in user."""
    session = request.state.session
    try:
        tree = await supervisor_map(session["user"])
        # Enrich leaf chat nodes with last message preview
        tree = await _enrich_messages(tree)
        return JSONResponse(content=tree)
    except Exception:
        _log.exception("supervisor_map failed")
        raise HTTPException(status_code=500, detail="Data unavailable")


async def _enrich_messages(tree: dict) -> dict:
    """Walk the tree and fetch the last message for each chat leaf node.

    Returns a new tree dict with ``last_message`` (max 200 chars)
    added to every node that has ``type == "chat"``.
    """
    import db  # noqa: local import — avoids cyclic import

    async def _walk(node: dict) -> dict:
        children = node.get("children")
        if children:
            node["children"] = [await _walk(c) for c in children]
        if node.get("type") == "chat":
            try:
                msgs = await db.messages_last(node["id"], count=1)
                if msgs:
                    content = msgs[0].get("content") or ""
                    node["last_message"] = (
                        content[:200] + "…" if len(content) > 200 else content
                    )
                    node["updated_at"] = msgs[0].get("created_at", "")
            except Exception:
                pass
        return node

    return await _walk(tree)
