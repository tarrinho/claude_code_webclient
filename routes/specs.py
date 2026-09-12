"""Routes for /api/specs: the design-specs gallery.

List, view, and admin-gated delete for this repo's design specs
(specs_gallery.py). No database table -- specs are shared, git-tracked
files with no per-owner concept, unlike routes/images.py's generated_images.
See docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import specs_gallery

_log = logging.getLogger("wc.app")

router = APIRouter()

# The repo root specs are scanned from. Two directories up from this file
# (routes/specs.py -> routes/ -> repo root), matching how routes/db_backup.py
# and others already locate project-root paths relative to their own file.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


async def handle_specs_list(request: Request):
    """GET /api/specs -- every spec, enriched, newest (by mtime) first."""
    specs = [specs_gallery.enrich(_REPO_ROOT, s)
             for s in specs_gallery.discover_specs(_REPO_ROOT)]
    return JSONResponse({"specs": specs})


async def handle_spec_content(request: Request, spec_id: str):
    """GET /api/specs/{id}/content -- one spec's content, rendered."""
    path = specs_gallery.decode_id(spec_id, _REPO_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="Spec not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    return HTMLResponse(specs_gallery.render_markdown(text))


async def handle_spec_delete(request: Request, spec_id: str):
    """DELETE /api/specs/{id} -- admin-only. Unlinks the file. Never runs
    a git command: the removal sits as an uncommitted working-tree change
    for a human/agent to commit deliberately, same as any other edit --
    auto-committing from a UI click in this shared, multi-session tree
    would be exactly the kind of autonomous git action that has caused
    collisions this project has already documented (spec section 5)."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    path = specs_gallery.decode_id(spec_id, _REPO_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="Spec not found")
    path.unlink(missing_ok=True)
    _log.info("spec_deleted user=%s path=%s", session["user"], path)
    return JSONResponse({"ok": True})


@router.get("/api/specs")
async def _api_specs_list(request: Request):
    return await handle_specs_list(request)


@router.get("/api/specs/{spec_id}/content")
async def _api_spec_content(request: Request, spec_id: str):
    return await handle_spec_content(request, spec_id)


@router.delete("/api/specs/{spec_id}")
async def _api_spec_delete(request: Request, spec_id: str):
    return await handle_spec_delete(request, spec_id)
