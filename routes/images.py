"""Routes for /api/images: the cross-chat generated-images gallery.

List, serve, and delete for the generated_images table (routes/db_images.py).
Serving is deliberately independent of the originating chat still existing --
see routes/db_images.py's own docstring for why work_dir is snapshotted.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import db

_log = logging.getLogger("wc.app")

router = APIRouter()

# Same extension set routes/chats.py's _CHAT_FILE_TYPES restricts image
# serving to -- kept as its own copy rather than importing from routes.chats,
# since generated_images never stores a .pdf path (routes/chats.py's
# _GENERATED_IMAGE_EXTENSIONS already excludes it) and importing routes.chats
# here for one dict is not worth the coupling.
_IMAGE_TYPES: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


async def handle_images_list(request: Request):
    """GET /api/images?limit=60&before_id=<id> -- an owner's generated
    images, newest first."""
    session = request.state.session
    limit_raw = request.query_params.get("limit")
    try:
        limit = min(int(limit_raw), 200) if limit_raw else 60
    except ValueError:
        limit = 60
    before_raw = request.query_params.get("before_id")
    before_id = int(before_raw) if before_raw and before_raw.isdigit() else None

    rows, has_more = await db.generated_images_list(
        session["user"], limit=limit, before_id=before_id)
    return JSONResponse({
        "images": [
            {
                "id": r["id"],
                "chat_id": r["chat_id"],
                "chat_title": r["chat_title"],
                "path": r["path"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
        "has_more": has_more,
    })


async def handle_image_file(request: Request, image_id: int):
    """GET /api/images/{id}/file -- serve the bytes directly from the
    row's snapshotted work_dir, independent of whether its chat still
    exists. Same path-containment check routes/chats.py:handle_chat_file
    applies, authorized against owner_id on the row instead of a live
    chat_get lookup."""
    session = request.state.session
    row = await db.generated_image_get(image_id, session["user"])
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found")

    root = Path(row["work_dir"]).resolve()
    try:
        candidate = (root / row["path"]).resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="Image not found")
    if not candidate.is_relative_to(root):
        _log.warning(
            "generated_image_outside_workspace: user=%s image_id=%s",
            session["user"], image_id,
        )
        raise HTTPException(status_code=404, detail="Image not found")

    media_type = _IMAGE_TYPES.get(candidate.suffix.lower())
    if media_type is None or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(candidate, media_type=media_type)


async def handle_image_delete(request: Request, image_id: int):
    """DELETE /api/images/{id} -- remove the file (if present) and the row."""
    session = request.state.session
    deleted = await db.generated_image_delete(image_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Image not found")
    return JSONResponse({"ok": True})


@router.get("/api/images")
async def _api_images_list(request: Request):
    return await handle_images_list(request)


@router.get("/api/images/{image_id}/file")
async def _api_image_file(request: Request, image_id: int):
    return await handle_image_file(request, image_id)


@router.delete("/api/images/{image_id}")
async def _api_image_delete(request: Request, image_id: int):
    return await handle_image_delete(request, image_id)
