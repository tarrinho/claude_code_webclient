"""POST /api/qa/run -- run the test suite on a remote transport instead of
this host. A thin HTTP wrapper around qa_remote.py, which is where the
actual orchestration lives (see its module docstring for why: it needs the
live app process's tunnel_manager._STATE).

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import qa_remote

_log = logging.getLogger("wc.qa")
router = APIRouter()


@router.post("/api/qa/run")
async def handle_qa_run(request: Request):
    session = request.state.session
    owner = session["user"]
    try:
        body = await request.json()
    except ValueError:
        # As in routes/misc.py: a bad body is the caller's problem and stays
        # silent, but only ValueError is a bad body. Anything else is a bug and
        # should not be disguised as an empty request.
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = body.get("transport") or None

    try:
        prepared = await qa_remote.resolve_transport(owner, name)
    except qa_remote.QaRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc

    async def event_stream():
        try:
            async for event in qa_remote.execute(prepared):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 -- a run dying must reach the
            # client as an event, not vanish into a 200 response with no body
            _log.exception("qa_run_failed transport=%s", prepared.transport.get("id"))
            yield f"data: {json.dumps({'type': 'run-done', 'ok': False, 'reason': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
