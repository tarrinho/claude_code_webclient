"""Routes for /api/specs: the design-specs gallery.

List, view, and admin-gated delete for this repo's design specs
(specs_gallery.py). specs_gallery.py itself keeps no database table --
specs are shared, git-tracked files with no per-owner concept, unlike
routes/images.py's generated_images -- but this route module does read one
small table (routes/db_specs.py's spec_status) to overlay a manually-set
status (spec_only/planning/implementing/done) on top of the auto-computed
one, for specs an admin has explicitly moved past what git history implies.
See docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import db
import specs_gallery
from routes.db_specs import ALLOWED_STATUSES

_log = logging.getLogger("wc.app")

router = APIRouter()

# The repo root specs are scanned from. Two directories up from this file
# (routes/specs.py -> routes/ -> repo root), matching how routes/db_backup.py
# and others already locate project-root paths relative to their own file.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


def _discover_and_enrich(root: Path) -> list[dict[str, Any]]:
    """The full discover-then-enrich-every-spec pass, synchronously. Called
    through asyncio.to_thread rather than awaited directly: enrich() runs
    blocking subprocess.run calls (grep, git log) once per spec with no
    executor wrapping of its own, and handle_specs_list is async -- with
    20+ specs that stalls the event loop, and every other in-flight
    request/turn, for the whole duration of one /api/specs call.

    references is computed once for every spec via find_all_references()
    (one grep pass) rather than once per spec inside enrich() -- measured
    2026-09-12, that alone was 12.5s of a 15.2s call for 23 specs. See
    find_all_references()'s and enrich()'s docstrings.
    """
    specs = specs_gallery.discover_specs(root)
    references = specs_gallery.find_all_references(
        root, [Path(s["path"]).name for s in specs])
    return [specs_gallery.enrich(root, s, references=references[Path(s["path"]).name])
            for s in specs]


def _is_known_spec(root: Path, candidate: Path) -> bool:
    """True only if *candidate* is one of the specs discover_specs(root)
    currently reports. decode_id() alone only proves the path stays inside
    root and is a file -- it never checks the path is actually a spec, which
    let a client-supplied id resolve to *any* file in the repo (e.g.
    config.py) and have it served as "spec content" or deleted outright.
    Re-scans every call, deliberately not cached: a cached membership set
    would go stale the same way cached status already caused bugs elsewhere
    in this project."""
    try:
        rel = str(candidate.relative_to(root))
    except ValueError:
        return False
    return any(s["path"] == rel for s in specs_gallery.discover_specs(root))


async def handle_specs_list(request: Request):
    """GET /api/specs -- every spec, enriched, newest (by mtime) first.

    A manual status (routes/db_specs.spec_status) overrides the
    auto-computed one wherever an admin has set it; specs with no override
    keep exactly the git/filesystem-derived status specs_gallery.py always
    computed. ``status_manual`` tells the client which case it is looking
    at, so the viewer's combo box can distinguish "nobody has said
    otherwise" from "someone explicitly marked this done".
    """
    specs = await asyncio.to_thread(_discover_and_enrich, _REPO_ROOT)
    overrides = await db.spec_status_get_all({s["path"] for s in specs})
    for spec in specs:
        override = overrides.get(spec["path"])
        spec["status_manual"] = override is not None
        if override is not None:
            spec["status"] = override
    return JSONResponse({"specs": specs})


async def handle_spec_content(request: Request, spec_id: str):
    """GET /api/specs/{id}/content -- one spec's content, rendered.

    ``?format=raw`` returns the unrendered markdown source as plain text
    instead -- for the viewer's copy-to-clipboard button, which needs the
    real markdown (headings, fenced code, links) rather than the HTML the
    gallery renders it into."""
    session = request.state.session
    path = specs_gallery.decode_id(spec_id, _REPO_ROOT)
    # _is_known_spec re-scans the whole tree (discover_specs), same blocking
    # cost class as _discover_and_enrich above -- off the event loop for the
    # same reason.
    known = path is not None and await asyncio.to_thread(_is_known_spec, _REPO_ROOT, path)
    if not known:
        if path is not None:
            _log.warning(
                "spec_outside_allowed_dirs: user=%s spec_id=%s path=%s",
                session["user"], spec_id, path,
            )
        raise HTTPException(status_code=404, detail="Spec not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    if request.query_params.get("format") == "raw":
        return PlainTextResponse(text)
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
    known = path is not None and await asyncio.to_thread(_is_known_spec, _REPO_ROOT, path)
    if not known:
        if path is not None:
            _log.warning(
                "spec_outside_allowed_dirs: user=%s spec_id=%s path=%s",
                session["user"], spec_id, path,
            )
        raise HTTPException(status_code=404, detail="Spec not found")
    path.unlink(missing_ok=True)
    _log.info("spec_deleted user=%s path=%s", session["user"], path)
    return JSONResponse({"ok": True})


async def handle_spec_status_set(request: Request, spec_id: str):
    """PUT /api/specs/{id}/status -- admin-only. Body: {"status": "..."},
    one of ALLOWED_STATUSES. Sets a manual override that handle_specs_list
    returns in place of the auto-computed status from then on."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    path = specs_gallery.decode_id(spec_id, _REPO_ROOT)
    known = path is not None and await asyncio.to_thread(_is_known_spec, _REPO_ROOT, path)
    if not known:
        if path is not None:
            _log.warning(
                "spec_outside_allowed_dirs: user=%s spec_id=%s path=%s",
                session["user"], spec_id, path,
            )
        raise HTTPException(status_code=404, detail="Spec not found")
    data = await request.json()
    status = data.get("status") if isinstance(data, dict) else None
    if status not in ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}",
        )
    rel = str(path.relative_to(_REPO_ROOT))
    await db.spec_status_set(rel, status)
    _log.info("spec_status_set user=%s path=%s status=%s", session["user"], rel, status)
    return JSONResponse({"ok": True, "status": status})


@router.get("/api/specs")
async def _api_specs_list(request: Request):
    return await handle_specs_list(request)


@router.get("/api/specs/{spec_id}/content")
async def _api_spec_content(request: Request, spec_id: str):
    return await handle_spec_content(request, spec_id)


@router.delete("/api/specs/{spec_id}")
async def _api_spec_delete(request: Request, spec_id: str):
    return await handle_spec_delete(request, spec_id)


@router.put("/api/specs/{spec_id}/status")
async def _api_spec_status_set(request: Request, spec_id: str):
    return await handle_spec_status_set(request, spec_id)
