"""Routes with no prefix of their own: transcripts, tokens, sessions, settings, system, usage and admin.

Part of the 0.10.0 routes split. The cluster was measured: every function
reached from a route with this prefix, closed over its private helpers.
Registration stays in app.py -- FastAPI matches routes in the order routers are
included, so that order belongs in one readable place.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import auth
import config
import db
import runner
import sysstats
import transcripts
from middleware import _token_touched
from net_validation import _HOST_PATTERN, _validate_host
from shared import (
    _HEX_SESSION_ID_RE,
    _MODEL_RE,
    _SSE_INTERNAL,
    _question_to_text,
    _turn_to_message,
)

_log = logging.getLogger("wc.app")

router = APIRouter()

# ── Settings cache ──────────────────────────────────────────────────────────────
# In-memory cache for GET /api/settings keyed by (owner_id, cache_version).
# cache_version increments on every PATCH write so stale entries expire instantly;
# the TTL is a safety net if version somehow gets out of sync.
_SETTINGS_CACHE_TTL_S: Final[int] = 30
_settings_cache_version: int = 0
_settings_cache: dict[tuple[str, int], tuple[dict, float]] = {}


async def _settings_cache_get(owner_id: str) -> dict[str, Any] | None:
    """Return cached settings for *owner_id* if still valid, else None."""
    global _settings_cache_version
    key = (owner_id, _settings_cache_version)
    entry = _settings_cache.get(key)
    if entry is None:
        return None
    payload, loaded_at = entry
    if time.monotonic() - loaded_at > _SETTINGS_CACHE_TTL_S:
        _settings_cache.pop(key, None)
        return None
    return payload


def _settings_cache_put(owner_id: str, payload: dict[str, Any]) -> None:
    """Store *payload* for *owner_id* at the current cache_version."""
    global _settings_cache_version
    key = (owner_id, _settings_cache_version)
    _settings_cache[key] = (payload, time.monotonic())


def _settings_invalidate() -> None:
    """Bump the cache version so every key becomes stale on the next GET."""
    global _settings_cache_version
    _settings_cache_version += 1
    _settings_cache.clear()  # version bump makes all entries stale anyway


# Skill discovery roots. Plain module attributes (not Final) so tests can patch
# them and never touch the real ~/.claude tree.
_USER_SKILLS_ROOT: Path = Path.home() / ".claude" / "skills"


_PLUGINS_ROOT: Path = Path.home() / ".claude" / "plugins"


# Directory names accepted as skill / plugin identifiers.
_SKILL_DIR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


# Upper bound on skills returned, so a pathological tree cannot blow up the response.
_SKILL_LIMIT: Final[int] = 500

# ── Asset version endpoint ──────────────────────────────────────────────────
# Serves a version hash so the frontend can bust caches by appending ?v=<hash>
# to any static asset URL. Bypasses any browser/CDN cache that ignores query
# strings (they are only ignored when no query string is present).
#
# Reads the git commit short hash at request time so deploys to the same host
# always get a fresh value without build-time injection.  Falls back to the
# VERSION string if git is unavailable.


def _asset_version() -> str:
    """Return a short version string for cache busting."""
    try:
        out = os.popen(
            "git -C /home/kali/projects/claude-code-webconsole "
            "rev-parse --short HEAD 2>/dev/null"
        ).read().strip()
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    return config.VERSION.removeprefix("WebConsole_")


@router.get("/api/version/hash")
async def _api_version_hash(request: Request):
    """GET /api/version/hash -- opaque version for cache busting."""
    return JSONResponse({"version": _asset_version()})


@router.post("/api/ping")
async def _api_ping(request: Request):
    """POST /api/ping -- refresh session TTL without side effects.

    Frontend calls this every 5 minutes to keep the cookie alive.
    Returns session_ttl_remaining so the UI can show a warning.
    No auth check needed beyond what AuthMiddleware already enforces.
    """
    session = request.state.session
    if not session:
        return JSONResponse(
            status_code=401,
            content={"error": "Session expired", "redirect": "/login"},
        )
    sid = request.cookies.get("wc_session", "")
    ttl = int(
        await db.setting_get("session_ttl") or config.SESSION_TTL_S
    )
    try:
        info = auth.session_info(sid)
        if info and "created_at" in info:
            created = float(info["created_at"])
            elapsed = time.time() - created
            remaining = max(0, int(ttl - elapsed))
        else:
            remaining = ttl
    except Exception:  # noqa: BLE001
        remaining = ttl
    return JSONResponse({"session_ttl_remaining": remaining, "ttl": ttl})


# WebConsole URL — full URL with scheme + host, optional port and path.
# Accepts trailing slash, port, and a short path segment (e.g. trailing slash).
_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://[A-Za-z0-9][A-Za-z0-9._-]*(?::\d{1,5})?(?:/[^\s]?)?$"
)


# Longest one-line summary shown on a collapsed skill card.
_SKILL_SUMMARY_MAX: Final[int] = 120


@router.get("/api/admin/export")
async def _api_db_backup(request: Request):
    return await handle_db_backup(request)


@router.post("/api/admin/import")
async def _api_db_restore(request: Request):
    return await handle_db_restore(request)


def _skill_description(text: str) -> str:
    """Pull the description out of a SKILL.md.

    Prefers the YAML frontmatter key, following wrapped continuation lines so a
    folded description is not truncated at the first newline. Falls back to a
    bare ``description:`` line anywhere near the top of the file.
    """
    lines = text.splitlines()
    body = lines
    # Restrict to the frontmatter block when one is present.
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = lines[1:index]
                break
    parts: list[str] = []
    for index, line in enumerate(body[:80]):
        if not line.lower().startswith("description:"):
            continue
        first = line.split(":", 1)[1].strip()
        # A folded / literal block scalar ("description: >-") carries no value on
        # the key line; the marker itself must not leak into the description.
        if not re.fullmatch(r"[>|][+-]?\d*", first):
            parts.append(first)
        # Consume indented / unkeyed continuation lines of a wrapped value.
        for follow in body[index + 1 :]:
            if not follow.strip():
                break
            if re.match(r"^[A-Za-z0-9_-]+\s*:", follow):
                break
            parts.append(follow.strip())
        break
    value = " ".join(part for part in parts if part).strip()
    # YAML scalars are often quoted; the quotes are not part of the value.
    for quote in ('"', "'"):
        if len(value) > 1 and value.startswith(quote) and value.endswith(quote):
            value = value[1:-1].strip()
            break
    return value[:500]


def _skill_summary(description: str) -> str:
    """Condense a description to a single short line for the collapsed card."""
    text = " ".join(description.split())
    # Descriptions are model-facing prose and often carry Markdown emphasis;
    # strip the markers so the summary reads as plain text. Underscores are only
    # treated as emphasis at word boundaries, to keep snake_case identifiers.
    text = text.replace("`", "")
    text = re.sub(r"\*{1,2}(\S(?:.*?\S)?)\*{1,2}", r"\1", text)
    text = re.sub(
        r"(?<![A-Za-z0-9_])_{1,2}(\S(?:.*?\S)?)_{1,2}(?![A-Za-z0-9_])", r"\1", text
    ).strip()
    if not text:
        return ""
    # Prefer a sentence boundary if one falls inside the budget.
    match = re.search(rf"^(.{{20,{_SKILL_SUMMARY_MAX}}}?[.!?])(?:\s|$)", text)
    if match:
        return match.group(1)
    if len(text) <= _SKILL_SUMMARY_MAX:
        return text
    clipped = text[:_SKILL_SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}…"


def _read_skill(directory: Path, name: str, source: str, source_label: str) -> dict | None:
    """Build one skill entry from a skill directory, or None if unreadable."""
    try:
        text = (directory / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    description = _skill_description(text)
    return {
        "name": name,
        "description": description,
        "summary": _skill_summary(description),
        "source": source,
        "source_label": source_label,
        "installed": True,
    }


def _iter_skill_dirs(root: Path):
    """Yield validated skill subdirectories of ``root``, sorted by name."""
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except (OSError, PermissionError):
        return
    for entry in entries:
        if not entry.is_dir() or not _SKILL_DIR_PATTERN.fullmatch(entry.name):
            continue
        yield entry


def _discover_user_skills() -> list[dict]:
    """Skills the user authored under ~/.claude/skills."""
    found = []
    for entry in _iter_skill_dirs(_USER_SKILLS_ROOT):
        skill = _read_skill(entry, entry.name, "user", "Your skills")
        if skill:
            found.append(skill)
    return found


def _discover_plugin_skills() -> list[dict]:
    """Skills provided by installed plugins.

    ``installed_plugins.json`` is the source of truth for what is actually
    installed -- walking the plugin cache directly would also surface
    marketplace checkouts and stale versions the user never installed.
    """
    manifest_path = _PLUGINS_ROOT / "installed_plugins.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return []

    try:
        plugins_root = _PLUGINS_ROOT.resolve()
    except OSError:
        return []

    found: list[dict] = []
    seen: set[str] = set()
    for key, installs in sorted(plugins.items()):
        plugin = str(key).split("@", 1)[0]
        if not _SKILL_DIR_PATTERN.fullmatch(plugin) or not isinstance(installs, list):
            continue
        for install in installs:
            if not isinstance(install, dict):
                continue
            raw_path = install.get("installPath")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            try:
                skills_root = (Path(raw_path) / "skills").resolve()
            except OSError:
                continue
            # Only read inside the plugins tree, whatever the manifest claims.
            if plugins_root not in skills_root.parents or not skills_root.is_dir():
                continue
            for entry in _iter_skill_dirs(skills_root):
                name = f"{plugin}:{entry.name}"
                if name in seen:
                    continue
                skill = _read_skill(entry, name, f"plugin:{plugin}", plugin)
                if skill:
                    seen.add(name)
                    found.append(skill)
    return found


async def handle_skills_get(request: Request):
    """GET /api/skills -- list installed skills and session activity."""
    session = request.state.session
    session_id = request.query_params.get("session_id")
    if not session_id:
        chat_id = request.query_params.get("chat_id")
        if chat_id:
            chat = await db.chat_get(chat_id, session["user"])
            session_id = chat.get("session_id") if chat else None

    skills = (_discover_user_skills() + _discover_plugin_skills())[:_SKILL_LIMIT]

    # A session records skills by bare name; match those against both the bare
    # name and the namespaced plugin name.
    active = set(runner.active_skills(session_id))
    for skill in skills:
        bare = skill["name"].split(":", 1)[-1]
        skill["active"] = skill["name"] in active or bare in active

    # Group order follows the list: user skills first, then plugins A-Z.
    sources: list[dict] = []
    by_source: dict[str, dict] = {}
    for skill in skills:
        group = by_source.get(skill["source"])
        if group is None:
            group = {
                "id": skill["source"],
                "label": skill["source_label"],
                "count": 0,
                "active_count": 0,
            }
            by_source[skill["source"]] = group
            sources.append(group)
        group["count"] += 1
        group["active_count"] += 1 if skill["active"] else 0

    return JSONResponse(
        {
            "skills": skills,
            "sources": sources,
            "total": len(skills),
            "active_count": sum(1 for skill in skills if skill["active"]),
            "session_id": session_id or "",
        }
    )


async def handle_db_backup(request: Request):
    """Download a gzip-compressed database backup."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    data = await db.db_backup()
    await db.admin_action_record(session["user"], "db_backup", "backup downloaded")
    date_str = (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    )
    fname = f'webconsole-backup-{date_str}.db.gz'
    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "content-disposition": (
                f'attachment; filename="{fname}"'
            )
        },
    )


async def handle_db_restore(request: Request):
    """POST /api/admin/import -- restore database from uploaded backup."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    file_form = await request.form()
    file = file_form.get("file")
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if len(file.filename) > 200:
        raise HTTPException(status_code=400, detail="Filename too long")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large")

    success = await db.db_restore(data)
    if not success:
        raise HTTPException(status_code=500, detail="Restore failed — invalid or corrupted backup")
    await db.admin_action_record(session["user"], "db_restore", "database restored")

    return JSONResponse({"ok": True, "message": "Database restored successfully"})


# Terminal sessions have no signed-in user, so their spend is attributed here.
# A fixed owner rather than whoever happens to open the tab: the byte cursor is
# per transcript, not per user, so attributing to the viewer would let the first
# person to look claim every row and leave the second an empty report. Correct
# only while this is a single-operator console -- the day a second account
# exists, this line is the bug.
CLI_USAGE_OWNER: Final[str] = "admin"


async def _import_cli_usage(owner: str = CLI_USAGE_OWNER) -> int:
    """Fold terminal-session spend into the usage table.

    Turns run in a terminal never pass through this app, so without this the
    Usage tab reports only what was typed into the website -- which on this
    machine was 6 events against roughly nine thousand real ones.

    Every assistant record in a transcript carries its model and token counts,
    so the history is recoverable after the fact. A byte cursor per transcript
    keeps a re-run from counting the same turns twice; the first import pays
    for the whole archive, later ones read only what was appended.
    """
    imported = 0
    try:
        entries = await transcripts.list_recent(limit=60)
    except OSError:
        return 0
    for entry in entries:
        session_id = entry["session_id"]
        try:
            cursor = await db.usage_cursor_get(session_id)
            rows, new_offset = await transcripts.usage_since(session_id, cursor)
            if new_offset != cursor:
                imported += await db.usage_import(owner, session_id, rows, new_offset)
        except (OSError, ValueError) as exc:
            # One unreadable transcript must not cost the whole report.
            _log.warning("cli_usage_import_failed session_id=%s: %s", session_id, exc)
    if imported:
        _log.info("cli_usage_imported owner=%s rows=%d", owner, imported)
    return imported


async def handle_usage_get(request: Request):
    """GET /api/usage -- per-model token totals and a recent-turn log.

    Scoped to the signed-in user's own rows. Like the settings GET, readable by
    any authenticated user rather than admin-only, and for the same reason: it
    is their own data and carries no secret.

    ``cost_usd`` is reported only for ``provider='claude_code'`` rows. Claude Code
    prices every turn with Anthropic's rates, so the figure is meaningless for a
    self-hosted or third-party gateway; ``cost_note`` tells the client why the
    value is absent so the UI can explain the blank rather than just show one.
    """
    await _import_cli_usage()
    session = request.state.session
    owner = session["user"]

    raw_days = request.query_params.get("days", "30")
    days: int | None
    if raw_days in ("all", "0", ""):
        days = None
    else:
        try:
            days = max(1, min(int(raw_days), 3650))
        except (TypeError, ValueError):
            days = 30
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
    except (TypeError, ValueError):
        limit = 50

    by_origin = await db.usage_by_origin(owner, days)
    for row in by_origin:
        if row.get("unsplit_requests"):
            # Said in the response rather than left for the reader to infer from
            # a suspiciously large number, matching how cost is already
            # suppressed with a note for backends where it is not meaningful.
            row["unsplit_note"] = (
                "Excluded from the token total: this model reported no cache "
                "breakdown, so each turn counts the whole conversation again "
                "rather than new tokens."
            )
    totals = await db.usage_totals(owner, days)
    for row in totals:
        if row.get("provider") != "through_claude_code":
            row["cost_usd"] = None
            # Prefer the CLI's own assessment when it gave one: that is a
            # statement from the tool, not an inference from our base_url.
            row["cost_note"] = (
                "Claude Code reported the cost basis as unknown for this model."
                if row.get("cost_basis_unknown")
                else "Priced with Anthropic rates; not meaningful for this backend."
            )
    recent = await db.usage_recent(owner, limit)
    for row in recent:
        if row.get("provider") != "through_claude_code":
            row["cost_usd"] = None

    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "retention_days": config.USAGE_RETENTION_DAYS,
            "overall": await db.usage_overall(owner, days),
            # Where the turns came from, and which session spent it. Without
            # these the page reported one figure dominated by adopted agent
            # sessions and presented it as the operator's own usage.
            "by_origin": by_origin,
            "by_session": await db.usage_by_session(owner, days),
            "totals": totals,
            "recent": recent,
        }
    )


async def handle_usage_series_get(request: Request):
    """GET /api/usage/series -- usage bucketed over time, for the charts.

    Separate from /api/usage rather than folded into it: that endpoint is read
    on every visit to the Usage tab and returns a flat table, while this one is
    read only by the statistics page and scans a far wider window. Keeping them
    apart means the common request does not pay for the rare one.

    Same ownership rule as /api/usage -- the caller's own rows, readable by any
    authenticated user because it is their own data and carries no secret.
    """
    await _import_cli_usage()
    owner = request.state.session["user"]

    raw_days = request.query_params.get("days", "30")
    days: int | None
    if raw_days in ("all", "0", ""):
        days = None
    else:
        try:
            days = max(1, min(int(raw_days), 3650))
        except (TypeError, ValueError):
            days = 30

    bucket = request.query_params.get("bucket", "day")
    if bucket not in db.USAGE_BUCKETS:
        bucket = "day"

    series = await db.usage_series(owner, days, bucket)
    # Cost is only meaningful for the official API: Claude Code prices every
    # turn with Anthropic's rates, so a gateway's figure is arithmetic on the
    # wrong number. Blanked here for the same reason /api/usage blanks it.
    for row in series:
        if row.get("provider") != "through_claude_code":
            row["cost_usd"] = None

    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "bucket": bucket,
            "buckets": list(db.USAGE_BUCKETS),
            "retention_days": config.USAGE_RETENTION_DAYS,
            "series": series,
            "models": await db.usage_model_series(owner, days, bucket),
            # The complete bucket axis for the window, including the buckets no
            # row falls into. The series GROUP BY only returns buckets that
            # have rows, and the chart places points by index, so an idle hour
            # is not drawn as a gap -- it is missing from the axis, and its
            # neighbours are rendered adjacent. Five idle hours overnight put
            # midnight one step from 06:00.
            #
            # Sent as a separate axis rather than as zero-valued rows: a
            # placeholder row would need a provider and a model to be shaped
            # like the others, and inventing those adds a series nobody used.
            # An hour with no usage really is zero tokens, so the client fills
            # these with zeros -- unlike the system series, where a missing
            # bucket means no measurement was taken and must stay null.
            "spine": db.bucket_spine(
                bucket, days,
                await db.usage_earliest(owner) if days is None else None,
            ),
        }
    )


def _system_range(request: Request) -> tuple[int | None, str]:
    """Parse and clamp the days/bucket query pair shared by the system routes."""
    raw_days = request.query_params.get("days", "1")
    days: int | None
    if raw_days in ("all", "0", ""):
        days = None
    else:
        try:
            days = max(1, min(int(raw_days), 3650))
        except (TypeError, ValueError):
            days = 1
    bucket = request.query_params.get("bucket", "halfhour")
    if bucket not in db.USAGE_BUCKETS:
        bucket = "halfhour"
    return days, bucket


async def handle_system_get(request: Request):
    """GET /api/system -- a live snapshot of the host this server runs on.

    Readable by any authenticated user, matching /api/settings: it carries no
    secret, and an operator checking whether the box is struggling should not
    need an admin account to do it. Hostname and CPU model are the most
    identifying values here and both are already implicit in reaching the
    site at all.
    """
    snapshot = await sysstats.sample_async()
    snapshot["sample_interval_s"] = config.SYSTEM_SAMPLE_S
    snapshot["retention_days"] = config.SYSTEM_RETENTION_DAYS
    snapshot["transports"] = await _transport_stats(request.state.session)
    return JSONResponse(snapshot)


async def _transport_stats(session) -> list[dict[str, Any]]:
    """The last stored sample for each of this owner's SSH transports.

    Read from storage, never collected here: the whole point of the background
    poller is that opening a page costs nothing. Every transport the owner has
    is listed, including ones that have never reported -- a transport missing
    from the table because nothing was ever sampled from it is
    indistinguishable from a transport that does not exist, and the second is
    the reassuring reading.
    """
    if not session:
        return []
    try:
        transports = await db.ssh_transports_list(session["user"])
        samples = {row["host_id"]: row for row in await db.system_latest_by_host()}
    except Exception:
        _log.exception("transport stats unavailable")
        return []
    out: list[dict[str, Any]] = []
    for transport in transports:
        sample = samples.get(transport["id"]) or {}
        out.append({
            "id": transport["id"],
            "name": transport.get("name") or transport["id"],
            "ssh_host": transport.get("ssh_host") or "",
            # None rather than 0 where there is no reading: the panel prints
            # "--" for those, and a 0 would read as an idle host. This is the
            # same distinction parse_stats exists to preserve upstream.
            "cpu_pct": sample.get("cpu_pct"),
            "mem_pct": sample.get("mem_pct"),
            "disk_pct": sample.get("disk_pct"),
            "load1": sample.get("load1"),
            "sampled_at": sample.get("created_at") or None,
        })
    return out


async def handle_system_series_get(request: Request):
    """GET /api/system/series -- stored host samples bucketed over time.

    Defaults to the last 24 hours in half-hour buckets rather than the 30 days
    the usage page defaults to. These two pages answer different questions: a
    token bill is read by the month, and a machine in trouble is read by the
    hour.
    """
    days, bucket = _system_range(request)
    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "bucket": bucket,
            "buckets": list(db.USAGE_BUCKETS),
            "sample_interval_s": config.SYSTEM_SAMPLE_S,
            "retention_days": config.SYSTEM_RETENTION_DAYS,
            # fill=True: the buckets no sample fell into come back as nulls, so
            # the chart can break its line instead of skipping the interval and
            # drawing the readings either side as though they were consecutive.
            "series": await db.system_series(days, bucket, fill=True),
            # Every transport's series in the same response, keyed by
            # transport id. One request and one grouped query rather than a
            # round trip per configured host: the Server page draws a chart
            # per transport, and the number of transports is not fixed.
            #
            # Rows here are NOT gap-filled, unlike the local series above. A
            # transport is only sampled while its tunnel is connected, so a
            # gap is the normal state rather than a missing reading, and
            # filling it would draw a confident null across every disconnect.
            "transports": await db.system_series_by_host(days, bucket),
        }
    )


def _parse_changelog(path: str) -> list[dict]:
    """Parse CHANGELOG.md into [{version, date, sections: [{type, items}]}].

    Splits the file on ``## [`] boundaries, then within each version block
    splits on ``### `` headings.  This avoids tricky multi-line regexes and
    stays readable.
    """
    try:
        text = Path(path).read_text()
    except Exception:
        return []
    rows: list[dict] = []

    for ver_match in re.finditer(r'^## \[(\d+\.\d+(?:\.\d+)?)\] — ([\d-]+)', text, re.MULTILINE):
        start = ver_match.start()
        # Find the next ## [ or end of file
        next_ver = re.search(r'^## \[', text[start + len(ver_match.group(0)):], re.MULTILINE)
        end = (start + len(ver_match.group(0)) + next_ver.start()) if next_ver else len(text)
        block = text[start:end]
        # Skip lines after version heading until first ### or content
        lines = block.split('\n')
        body_lines: list[str] = []
        found_section = False
        for line in lines[1:]:
            if re.match(r'^### ', line):
                body_lines.append(line)
                found_section = True
            elif not found_section and line.startswith('>'):
                # Blockquote intro — skip
                continue
            elif not found_section and not line.strip():
                # Blanks before first section — skip
                continue
            else:
                body_lines.append(line)
                found_section = True
        if not found_section:
            body_lines = lines[1:]  # fallback: everything after heading

        sections: list[dict] = []
        _current_section: dict | None = None
        for line in body_lines:
            sm = re.match(r'^### (Added|Fixed|Changed|Security|Removed|Testing|Documentation)\n?', line)
            if sm:
                if _current_section and _current_section['items']:
                    sections.append(_current_section)
                _current_section = {'type': sm.group(1), 'items': []}
                continue
            if _current_section is None:
                continue
            bm = re.match(r'^- (.+)', line)
            if bm:
                _current_section['items'].append(bm.group(1).strip())
            elif line.strip():
                # Continuation of previous item
                if _current_section['items']:
                    _current_section['items'][-1] += ' ' + line.strip()
            # blanks/--- end of section, nothing to do

        if _current_section and _current_section['items']:
            sections.append(_current_section)

        rows.append({'version': ver_match.group(1), 'date': ver_match.group(2), 'sections': sections})

    return rows


async def handle_changelog_get(request: Request):
    """GET /api/changelog — return parsed changelog sections as JSON."""
    changelog_path = Path(__file__).resolve().parent.parent / 'CHANGELOG.md'
    return JSONResponse(_parse_changelog(str(changelog_path)))


async def handle_settings_get(request: Request):
    """GET /api/settings -- return non-secret runtime and app settings.

    Deliberately readable by any authenticated user, unlike the PATCH
    counterpart, which is admin-only: the frontend reads it on every page load
    to render the version and the settings form. Every value below must
    therefore stay non-sensitive -- never add a secret, key, or token here.

    Uses an in-memory cache keyed by (owner_id, cache_version) so that the
    same-owner re-fetch within a 30-second window bypasses the DB entirely.
    Every PATCH write bumps the version, making stale entries instantly invalid.
    """
    session = request.state.session
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")

    owner_id = session["user"]

    # Fast path: cache hit.
    cached = await _settings_cache_get(owner_id)
    if cached is not None:
        return JSONResponse(cached)

    # Slow path: build from DB.
    host = await runner.get_proxy_host()

    # Batch-read all setting values in a single query.
    _SETTINGS_KEYS = frozenset((
        "session_ttl", "turn_timeout", "prompt_max",
        "voice_backend_id", "voice_ai_machine_id", "voice_model",
        "voice_speech_rate", "webconsole_url", "debug_console",
        "cross_session_inbound", "testing_default_model", "testing_model_enforce",
        "default_model",
    ))
    _settings_rows = await db.setting_get_all(_SETTINGS_KEYS)

    def _get(key: str, default: Any = None) -> str | None:
        return _settings_rows.get(key, default)

    try:
        session_ttl = int(_get("session_ttl") or config.SESSION_TTL_S)
    except (TypeError, ValueError):
        session_ttl = config.SESSION_TTL_S
    try:
        turn_timeout = int(_get("turn_timeout") or config.TURN_TIMEOUT_S)
    except (TypeError, ValueError):
        turn_timeout = config.TURN_TIMEOUT_S
    try:
        prompt_max = int(_get("prompt_max") or config.PROMPT_MAX_CHARS)
    except (TypeError, ValueError):
        prompt_max = config.PROMPT_MAX_CHARS

    voice_backend_id = _get("voice_backend_id")
    if not voice_backend_id:
        voice_backend_id = _get("voice_ai_machine_id")
    voice_backend_id = voice_backend_id or config.VOICE_BACKEND_ID_DEFAULT
    voice_model = _get("voice_model") or config.VOICE_MODEL_DEFAULT
    try:
        voice_speech_rate = float(
            _get("voice_speech_rate") or config.VOICE_SPEECH_RATE_DEFAULT
        )
    except (TypeError, ValueError):
        voice_speech_rate = config.VOICE_SPEECH_RATE_DEFAULT

    webconsole_url = _get("webconsole_url")
    if not webconsole_url and config.WC_WEBCONSOLE_URL:
        webconsole_url = config.WC_WEBCONSOLE_URL

    debug_console = _get("debug_console") == "1"

    cross_session_inbound = _get("cross_session_inbound")
    if cross_session_inbound not in ("accept", "prompt"):
        cross_session_inbound = config.CROSS_SESSION_INBOUND_DEFAULT

    testing_default_model = _get("testing_default_model") or config.TESTING_MODEL_DEFAULT
    testing_model_enforce_raw = _get("testing_model_enforce")
    testing_model_enforce = (
        config.TESTING_MODEL_ENFORCE_DEFAULT if testing_model_enforce_raw is None
        else testing_model_enforce_raw == "1"
    )

    from routes.db_machines import parse_active_models
    from routes.voice import voice_model_timing_averages

    if voice_backend_id and not await db.ai_machine_get(voice_backend_id, owner_id):
        voice_backend_id = None

    # active_models travels with each backend, so the Settings dialog can
    # repopulate the model dropdown from the *selected* backend without
    # another request.
    #
    # It used to be sent only for the stored backend, and the dialog's
    # onchange handler re-GET this endpoint to "refresh" the list -- which
    # cannot work: this handler reads voice_backend_id from the settings
    # table, so re-reading it returns the same stored backend's models no
    # matter what the dropdown now shows. The list only ever changed after a
    # Save and a reopen.
    cur = await db.db_conn.execute(
        "SELECT id, name, provider, active_models FROM ai_machines "
        "WHERE owner_id = ? AND enabled = 1 ORDER BY active DESC, name ASC",
        (owner_id,),
    )
    rows = await cur.fetchall()
    per_backend = {
        row["id"]: parse_active_models(row["active_models"])
        for row in rows
    }

    # One timing query for the union, not one per backend: the helper is a
    # single GROUP BY over whatever model list it is handed, so asking it
    # once for every id any backend offers costs the same as asking for one
    # backend's.
    every_model = sorted({m for models in per_backend.values() for m in models})
    averages = await voice_model_timing_averages(every_model)

    def _options(machine_id: str) -> list[dict]:
        return [
            {"id": model_id, **averages[model_id]}
            for model_id in per_backend.get(machine_id, [])
        ]

    voice_backend_options = [
        {
            "id": row["id"],
            "name": row["name"],
            "provider": row["provider"],
            "models": _options(row["id"]),
        }
        for row in rows
    ]
    _log.info("settings_get: voice_backend_options=%s voice_backend_id=%s",
              [m["name"] for m in voice_backend_options], voice_backend_id)

    # Retained alongside the per-backend lists: this is the selected
    # backend's list, which is what the dialog shows before anyone touches
    # the dropdown, and what an API client reading settings expects.
    voice_model_options = _options(voice_backend_id) if voice_backend_id else []

    payload = {
        "ai_machine_host": host,
        "ai_machine_port": config.PROXY_PORT,
        "proxy_enabled": config.PROXY_ENABLED,
        "default_model": _get("default_model") or config.MODEL_NAME,
        "webconsole_url": webconsole_url,
        "version": config.VERSION.removeprefix("WebConsole_"),
        "session_ttl_s": session_ttl,
        "turn_timeout_s": turn_timeout,
        "prompt_max": prompt_max,
        "debug_console": debug_console,
        "voice_backend_id": voice_backend_id,
        # Compatibility key for older clients; both values are the same
        # owner-scoped selection and never expose another user's machine.
        "voice_ai_machine_id": voice_backend_id,
        "voice_backend_options": voice_backend_options,
        "voice_model": voice_model,
        "voice_speech_rate": voice_speech_rate,
        "voice_model_options": voice_model_options,
        "cross_session_inbound": cross_session_inbound,
        "testing_default_model": testing_default_model,
        "testing_model_enforce": testing_model_enforce,
    }

    # Cache the full payload (including dynamic parts like active_models and
    # timing averages) so that subsequent GETs within the TTL for the same
    # owner hit the cache without touching the DB.
    _settings_cache_put(owner_id, payload)

    return JSONResponse(payload)


async def handle_settings_patch(request: Request):
    """PATCH /api/settings -- update runtime or app settings.

    Admin-only: this endpoint writes the session secret, the proxy token, the
    model API key and projects_root. projects_root is the sandbox boundary
    that runner.py validates every work_dir against, so write access here is
    equivalent to choosing where Claude may run.
    """
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    data = await request.json()
    if "ai_machine_host" in data:
        host = data.get("ai_machine_host")
        if not isinstance(host, str):
            raise HTTPException(status_code=400, detail="AI machine host must be text")
        host = host.strip()
        if not _HOST_PATTERN.fullmatch(host):
            raise HTTPException(
                status_code=400, detail="Enter a valid hostname or IP address"
            )
        # SSRF protection: block internal IPs before persisting.
        _validate_host(host)
        await db.setting_set("ai_machine_host", host)
        _log.info("AI machine host updated by user=%s host=%s", session["user"], host)
        await db.admin_action_record(
            session["user"], "settings_ai_machine_host", f"host={host}",
        )

    for setting_name, field_name, error in (
        ("voice_backend_id", "voice_backend_id", "Voice backend id must be text"),
        ("voice_ai_machine_id", "voice_ai_machine_id", "Voice AI machine id must be text"),
    ):
        if field_name not in data:
            continue
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail=error)
        machine_id = (value or "").strip()
        if machine_id and not await db.ai_machine_get(machine_id, session["user"]):
            raise HTTPException(status_code=404, detail="Voice backend not found")
        await db.setting_set(setting_name, machine_id)
    if "voice_model" in data:
        from routes.db_machines import parse_active_models
        value = data.get("voice_model")
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Voice model must be text")
        if value is not None:
            model_id = value.strip()
            backend_id = await db.setting_get("voice_backend_id") or config.VOICE_BACKEND_ID_DEFAULT
            if backend_id:
                cur = await db.db_conn.execute(
                    "SELECT active_models FROM ai_machines "
                    "WHERE id = ? AND owner_id = ?",
                    (backend_id, session["user"]),
                )
                row = await cur.fetchone()
                if row and row["active_models"]:
                    allowed = parse_active_models(row["active_models"])
                    if model_id not in allowed:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Model must be one of: {', '.join(allowed)}",
                        )
            await db.setting_set("voice_model", model_id)
    if "voice_speech_rate" in data:
        value = data.get("voice_speech_rate")
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Voice speech rate must be a number")
        if not (0.5 <= rate <= 5.0):
            raise HTTPException(status_code=400, detail="Voice speech rate must be between 0.5 and 5")
        await db.setting_set("voice_speech_rate", str(rate))

    if "debug_console" in data:
        value = data.get("debug_console")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="debug_console must be a boolean")
        await db.setting_set("debug_console", "1" if value else "0")

    if "cross_session_inbound" in data:
        value = data.get("cross_session_inbound")
        if value not in ("accept", "prompt"):
            raise HTTPException(
                status_code=400,
                detail="cross_session_inbound must be 'accept' or 'prompt'",
            )
        await db.setting_set("cross_session_inbound", value)

    if "testing_default_model" in data:
        value = data.get("testing_default_model")
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=400, detail="testing_default_model must be non-empty text"
            )
        await db.setting_set("testing_default_model", value.strip())

    if "testing_model_enforce" in data:
        value = data.get("testing_model_enforce")
        if not isinstance(value, bool):
            raise HTTPException(
                status_code=400, detail="testing_model_enforce must be a boolean"
            )
        await db.setting_set("testing_model_enforce", "1" if value else "0")

    # Boot secrets – these live in the DB so the app can run without .env.
    boot_secrets = {
        "session_secret": ("session_secret", "Session secret must be at least 32 characters"),
        "projects_root": ("projects_root", "Projects root path is required"),
        "proxy_token": ("proxy_token", "Proxy token must be at least 32 characters"),
        "model_base_url": ("model_base_url", "Model base URL is required"),
        "model_api_key": ("model_api_key", None),  # optional, can be empty
    }
    for key, (db_key, error) in boot_secrets.items():
        if key in data:
            value = data[key]
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=400, detail=error)
            if key in ("session_secret", "proxy_token") and value is not None:
                value = value.strip()
                if len(value) < 32:
                    raise HTTPException(status_code=400, detail=error)
            elif key == "projects_root" and value is not None:
                value = value.strip()
                if not value:
                    raise HTTPException(status_code=400, detail=error)
                value = _validate_projects_root(value)
                # Reject if any existing chat work_dir would be stranded.
                rows = await db.db_conn.execute(
                    "SELECT work_dir FROM chats WHERE deleted_at IS NULL"
                )
                for row in await rows.fetchall():
                    wd = row["work_dir"]
                    try:
                        resolved = Path(wd).resolve()
                    except (OSError, RuntimeError):
                        continue
                    if resolved.is_relative_to(Path(value).resolve()):
                        # Still inside the new root — fine.
                        continue
                    raise HTTPException(
                        status_code=400,
                        detail=f"Existing chat workspace {wd} is outside the new projects root",
                    )
            elif key == "model_base_url" and value is not None:
                value = value.strip()
                if not value:
                    raise HTTPException(status_code=400, detail=error)
            await db.setting_set(db_key, value.strip() if value is not None else None)
            # Log the change (value omitted to avoid writing secrets).
            await db.admin_action_record(
                session["user"], "settings_change", f"{db_key}=*",
            )

    # fallback_model was accepted and stored here but never read by anything --
    # a settings control implying a retry behaviour that did not exist. Removed
    # rather than left dead; the per-machine default is the real control now.
    for key in ("default_model",):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or len(value.strip()) > 100:
                raise HTTPException(
                    status_code=400, detail=f"{key} must be text up to 100 characters"
                )
            value = value.strip()
            if value and not _MODEL_RE.fullmatch(value):
                raise HTTPException(
                    status_code=400, detail=f"{key} contains invalid characters"
                )
            await db.setting_set(key, value)
    for key, default in (
        ("session_ttl", config.SESSION_TTL_S),
        ("turn_timeout", config.TURN_TIMEOUT_S),
        ("prompt_max", config.PROMPT_MAX_CHARS),
    ):
        if key in data:
            val = data[key]
            if not isinstance(val, int) or val < 30 or val > 86400:
                raise HTTPException(status_code=400, detail=f"{key} must be 30-86400")
            await db.setting_set(key, str(val))
    # WebConsole URL — the public-facing site address used to build
    # shareable links for comparison reports and exported files.
    if "webconsole_url" in data:
        value = data["webconsole_url"]
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail="webconsole_url must be text")
        value = (value or "").strip()
        value = value.rstrip("/")  # trailing slash is cosmetic, strip before host extraction
        if value and not _URL_RE.fullmatch(value):
            raise HTTPException(
                status_code=400, detail="Enter a valid URL (http:// or https:// with a host)"
            )
        if value:
            _validate_host(value.split("://", 1)[1].split(":", 1)[0])
            await db.setting_set("webconsole_url", value)
            await db.admin_action_record(
                session["user"], "settings_webconsole_url", f"url=*",
            )
            _log.info("WebConsole URL updated by user=%s", session["user"])
        else:
            # Empty string clears the stored value.
            await db.setting_set("webconsole_url", "")
            _log.info("WebConsole URL cleared by user=%s", session["user"])
    # Bust the settings cache so the next GET rebuilds from DB.
    _settings_invalidate()
    return JSONResponse(
        {"ok": True, "ai_machine_host": await db.setting_get("ai_machine_host")}
    )


_TOKEN_NAME_MAX: Final[int] = 100


# A token with no expiry is a deliberate option -- a cron job should not stop
# working at 3am because nobody renewed it -- but an unbounded *requested*
# lifetime is not, so a supplied value is capped at a year.
_TOKEN_MAX_TTL_DAYS: Final[int] = 365


async def handle_tokens_get(request: Request):
    """GET /api/tokens -- this user's live tokens, without the secrets.

    Scoped to the caller: a token list is a list of credentials, and the fact
    that another account holds one is not this account's business.
    """
    session = request.state.session
    rows = await db.api_token_list(session["user"])
    return JSONResponse({"tokens": rows, "count": len(rows)})


async def handle_tokens_create(request: Request):
    """POST /api/tokens -- mint a token for the calling user.

    The secret is returned exactly once, in this response, and is unrecoverable
    afterwards because only its hash is stored. That is stated in the payload
    itself rather than only in the docs, since the one-shot nature is the part
    a caller has to act on immediately.

    Deliberately *not* admin-only. The token carries the caller's own identity
    and role and grants nothing they do not already have, so requiring admin
    would only push non-admin users back towards sharing a password. Creation
    requires a **cookie** session, though: a token that can mint further tokens
    turns one leaked credential into an unrevocable supply of them.
    """
    session = request.state.session
    if session.get("via") == "api_token":
        raise HTTPException(
            status_code=403,
            detail="Tokens can only be created from a logged-in session, not "
                   "with another token",
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    name = str(data.get("name") or "").strip()[:_TOKEN_NAME_MAX] or "unnamed"
    expires_at = None
    # L6 fix: default to a configurable TTL so tokens don't live forever.
    # Explicit "never" (0 or the string) disables expiry; a positive int
    # sets a custom TTL; anything else uses _TOKEN_DEFAULT_TTL_DAYS.
    _days_cfg = config.TOKEN_DEFAULT_TTL_DAYS
    if "expires_in_days" in data:
        val = data["expires_in_days"]
        if isinstance(val, str) and val.lower() == "never":
            expires_at = None  # explicit never
        elif isinstance(val, (int, float)):
            days = int(val)
            if days == 0:
                expires_at = None  # 0 = explicit never
            elif 1 <= days <= _TOKEN_MAX_TTL_DAYS:
                expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)
                expires_at = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"expires_in_days must be 1-{_TOKEN_MAX_TTL_DAYS}, or 0 for no expiry",
                )
        else:
            raise HTTPException(
                status_code=400, detail="expires_in_days must be a number"
            )
    elif _days_cfg > 0:
        # Apply configurable default TTL.
        expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=_days_cfg)
        expires_at = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")

    token_id, secret, token_hash = auth.new_api_token()
    await db.api_token_create(
        token_id, name, token_hash, session["user"],
        session.get("role") or "user", expires_at,
    )
    # The id, never the secret. A log line is exactly the sort of place a
    # credential should not end up, and the id is enough to revoke by.
    _log.info(
        "api_token_created id=%s user=%s name=%s expires=%s",
        token_id, session["user"], name, expires_at or "never",
    )
    await db.admin_action_record(
        session["user"], "api_token_created", f"id={token_id} name={name} expires={expires_at or 'never'}",
    )
    return JSONResponse({
        "id": token_id,
        "name": name,
        "token": secret,
        "expires_at": expires_at,
        "note": "This is the only time the token is shown. Store it now; the "
                "server keeps only a hash and cannot show it again.",
        "usage": "Authorization: Bearer <token>  (or X-API-Token: <token>)",
    })


async def handle_tokens_revoke(request: Request):
    """DELETE /api/tokens/{id} -- revoke one of this user's tokens.

    Allowed with a token as well as a session: being able to retire a
    credential you suspect is loose should never be the operation that needs the
    credential you have lost.
    """
    session = request.state.session
    token_id = request.path_params["token_id"]
    if not await db.api_token_revoke(token_id, session["user"]):
        # 404 whether it never existed, belongs to somebody else, or was already
        # revoked -- distinguishing those tells a caller about tokens that are
        # not theirs.
        raise HTTPException(status_code=404, detail="Token not found")
    _token_touched.pop(token_id, None)
    _log.info("api_token_revoked id=%s user=%s", token_id, session["user"])
    await db.admin_action_record(
        session["user"], "api_token_revoked", f"id={token_id}",
    )
    return JSONResponse({"ok": True, "revoked": token_id})


def _validate_projects_root(value: str) -> str:
    """Confirm a candidate projects_root is a safe workspace parent.

    runner.py validates every work_dir against this path, so an unconstrained
    value relocates the sandbox -- pointing it at "/" would let a conversation
    workspace be created anywhere and run Claude there with
    --dangerously-skip-permissions. Constrain it to a real directory under the
    server account's home, or set WC_PROJECTS_ROOT_BASE to widen that.
    """
    base = Path(config._str("WC_PROJECTS_ROOT_BASE", str(Path.home())) or "/").resolve()
    try:
        candidate = Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Projects root path is invalid")
    if not candidate.is_absolute() or not candidate.is_relative_to(base):
        raise HTTPException(
            status_code=400,
            detail=f"Projects root must be an absolute path under {base}",
        )
    if not candidate.is_dir():
        raise HTTPException(
            status_code=400, detail="Projects root must be an existing directory"
        )
    return str(candidate)


async def handle_sessions_list(request: Request):
    """GET /api/sessions -- list CLI sessions + Web chats for sidebar."""
    session = request.state.session
    cli_sessions = await db.read_claude_sessions()
    web_chats = await db.chat_list(session["user"])
    linked_session_ids = {
        chat.get("session_id") for chat in web_chats if chat.get("session_id")
    }
    cli_sessions = [
        item for item in cli_sessions if item.get("sessionId") not in linked_session_ids
    ]
    # Merge unlinked CLI sessions with WebConsole chats.
    items = cli_sessions + [
        {
            "id": c["id"],
            "name": c["title"],
            "cwd": c["work_dir"],
            "kind": "web",
            "startedAt": c["created_at"],
            "updatedAt": c["updated_at"],
            "sessionId": c.get("session_id", ""),
            "model": c.get("model") or "",
            "webchat": True,
        }
        for c in web_chats
    ]
    return JSONResponse({"sessions": items})


def _sanitize_session_id(session_id: str) -> str:
    """Validate a session_id, rejecting anything usable for path traversal.

    An earlier version accepted any non-empty string, on the stated grounds
    that ``Path.mkdir(parents=True)`` can only create inside PROJECTS_ROOT.
    That is not true: ``handle_sessions_resume`` interpolates ``session_id[:8]``
    into a directory name, so an id of ``a/../../`` yields a work_dir that
    resolves *above* the root. A payload of only ``..`` does cancel out against
    the date suffix, which is likely why the invariant looked safe.

    Real ids are UUIDs, so the charset below is not restrictive in practice,
    and it matches the guard already enforced by
    ``db.write_claude_session_file``.
    """
    if not session_id or not _HEX_SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID")
    return session_id


def _adopt_session_cwd(source_cwd: str | None, session_id: str) -> str:
    """Pick the work_dir for a chat adopting a CLI session.

    work_dir is the directory Claude actually runs in, so a resumed
    conversation that gets a freshly minted ``cli-import-<id>`` folder keeps
    its whole history but lands somewhere empty, unable to read the files it
    was just discussing. Adopting the terminal's own cwd fixes that.

    The transcript is not the reason: a session stays anchored to wherever it
    was created and keeps appending there no matter which directory it is
    resumed from, so nothing forks either way.

    The cwd still has to sit inside PROJECTS_ROOT -- runner.py rejects
    anything outside it before spawning a turn, and this must not be the hole
    in that boundary. Anything missing or outside falls back to the old
    scratch directory, which is worse but always valid.
    """
    root = Path(config.PROJECTS_ROOT).resolve()
    candidate: Path | None = None
    if source_cwd and source_cwd.strip():
        try:
            candidate = Path(source_cwd.strip()).resolve()
        except (OSError, RuntimeError):
            candidate = None

    if candidate and candidate.is_dir() and candidate.is_relative_to(root):
        return str(candidate)

    if candidate:
        _log.info(
            "cli_session_cwd_not_adopted: cwd=%s (missing, or outside "
            "PROJECTS_ROOT=%s) — falling back to a scratch workspace",
            candidate, root,
        )
    date_suffix = datetime.datetime.now(datetime.UTC).date().isoformat()
    fallback = root / f"cli-import-{session_id[:8]}-{date_suffix}"
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


async def handle_sessions_resume(request: Request, session_id: str):
    """POST /api/sessions/{session_id}/resume -- open a WebConsole chat for a CLI session."""
    session = request.state.session
    session_id = _sanitize_session_id(session_id)
    available = await db.read_claude_sessions()
    source = next(
        (item for item in available if item.get("sessionId") == session_id), None
    )
    if source is None:
        # ~/.claude/sessions only lists sessions that are still running, so a
        # finished conversation is absent from it while its transcript lives on.
        # Fall back to the transcript, which records the cwd the session ran in,
        # so any past conversation can be reopened -- not just a live terminal.
        cwd = await transcripts.session_cwd(session_id)
        if cwd:
            source = {"sessionId": session_id, "cwd": cwd}
        else:
            _log.error(
                "cli_session_not_found: user=%s session_id=%s "
                "(no running session and no transcript on disk) — "
                "ensure claude-code is running, or that the conversation "
                "exists under the projects directory",
                session["user"], session_id,
            )
            raise HTTPException(
                status_code=404,
                detail="Conversation not found — no running session and no transcript",
            )

    existing = next(
        (
            chat
            for chat in await db.chat_list(session["user"])
            if chat.get("session_id") == session_id
        ),
        None,
    )
    if existing:
        # Backfill a chat resumed before the import existed, which would
        # otherwise stay permanently empty. Guarded on the chat having no
        # messages so a conversation continued here is never duplicated.
        if not await db.messages_get(existing["id"]):
            await _import_transcript(existing["id"], session_id)
        return JSONResponse(
            {
                "id": existing["id"],
                "title": existing["title"],
                "session_id": session_id,
            }
        )

    # Create a new WebConsole chat linked to the CLI session
    chat_id = uuid.uuid4().hex
    # Prefer a name a human would recognise: the terminal's own session name,
    # else the conversation's opening prompt. "CLI: 5bfd4035-b6d..." tells the
    # reader nothing about which conversation it is.
    title = (
        (source.get("name") or "").strip()
        or (await transcripts.session_title(session_id)).strip()
        or f"CLI: {session_id[:12]}..."
    )[:200]
    work_dir = _adopt_session_cwd(source.get("cwd"), session_id)
    await db.chat_create(chat_id, title, None, work_dir, session["user"])
    # Link the CLI session ID
    await db.chat_set_session(chat_id, session_id)
    # Seed the chat with the conversation already on disk, so it opens where
    # the terminal left off rather than blank.
    imported = await _import_transcript(chat_id, session_id)
    # Write session file so CLI can see it too
    try:
        db.write_claude_session_file(session_id, title, work_dir)
    except (OSError, ValueError) as exc:
        _log.warning(
            "could not write CLI session file session_id=%s: %s", session_id, exc
        )

    return JSONResponse(
        {
            "id": chat_id,
            "title": title,
            "session_id": session_id,
            "imported_messages": imported,
        }
    )


async def _import_transcript(chat_id: str, session_id: str) -> int:
    """Seed a resumed chat with the conversation already on disk.

    A resumed CLI session used to open empty: the history lived only in the
    transcript viewer, so the chat gave no sense of what had been discussed.
    Importing it once at resume makes the conversation read as if it had
    always been here -- scrollable, searchable, exportable and forkable like
    any other, because it is now ordinary message rows.
    """
    try:
        payload = await transcripts.read_turns(session_id)
    except OSError as exc:
        _log.warning("transcript_import_failed session_id=%s: %s", session_id, exc)
        return 0
    if not payload.get("found"):
        return 0
    rows = [
        row
        for row in (_turn_to_message(turn) for turn in payload.get("turns") or [])
        if row is not None
    ]
    # The 512 KB tail read misses unanswered ``AskUserQuestion`` blocks that
    # live in the older part of a large transcript.  A quick full-file scan
    # finds them and attaches them as message rows.  Filter by IDs we haven't
    # already rendered so re-importing stays idempotent.
    try:
        # transcript_path returns None when a session has no transcript on
        # disk, and _scan_questions_sync is typed for a Path: it guards
        # read_bytes with OSError, which None.read_bytes() is not. Skipping
        # here keeps that signature honest rather than teaching the scanner
        # to accept a value it says it does not take.
        scan_path = transcripts.transcript_path(session_id)
        question_blocks = await asyncio.to_thread(
            # Private, because transcripts.py exposes no public full-file scan.
            transcripts._scan_questions_sync,
            scan_path,
        ) if scan_path is not None else []
    except Exception:
        # Logged, not silent. The call above was unqualified until now, so it
        # raised NameError on every request and this handler turned that into
        # "no questions found" -- meaning the older unanswered questions the
        # scan exists to find were the exact thing it never returned. A bare
        # pass here makes a programming error indistinguishable from a
        # transcript that legitimately had none, and one log line would have
        # found it in a single run.
        _log.warning("question_scan_failed session=%s", session_id, exc_info=True)
        question_blocks = []

    seen_ids: set[str] = await db.chat_get_question_ids(chat_id)
    filtered_questions: list[dict[str, Any]] = []
    for qb in question_blocks:
        qid = str(qb.get("id") or "")
        if (qid and qid not in seen_ids) or (not qid and qb not in filtered_questions):
            filtered_questions.append(qb)

    extra_rows: list[tuple[str, str]] = []
    for qb in filtered_questions:
        rendered = _question_to_text(qb)
        if rendered:
            extra_rows.append(("assistant", rendered))
    if extra_rows:
        rows.extend(extra_rows)
        _log.info(
            "transcript_imported_questions chat_id=%s questions=%d",
            chat_id, len(extra_rows),
        )
        await db.chat_set_question_ids(chat_id, [str(q.get("id", "")) for q in filtered_questions])
    # Record the read position even when nothing was worth importing, so the
    # sync does not re-examine the same bytes on every poll.
    await db.chat_set_transcript_offset(chat_id, int(payload.get("offset") or 0))
    if not rows:
        return 0
    await db.messages_batch(chat_id, rows)
    _log.info(
        "transcript_imported chat_id=%s session_id=%s turns=%d truncated=%s",
        chat_id, session_id, len(rows), bool(payload.get("truncated")),
    )
    return len(rows)


async def handle_session_delete(request: Request, session_id: str):
    """DELETE /api/sessions/{session_id} -- drop a dead CLI session entry.

    Only removes the shadow record WebConsole itself wrote when the session
    was resumed. db.delete_claude_session_file refuses anything else, so a
    running session cannot be cleared out of the sidebar by accident.
    """
    session = request.state.session
    session_id = _sanitize_session_id(session_id)
    try:
        removed = await asyncio.to_thread(db.delete_claude_session_file, session_id)
    except ValueError as exc:
        _log.warning(
            "session_delete_refused: user=%s session_id=%s (%s)",
            session["user"], session_id, exc,
        )
        raise HTTPException(status_code=409, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="Session entry not found")
    _log.info("session_entry_removed session_id=%s user=%s", session_id, session["user"])
    return JSONResponse({"ok": True})


@router.get("/api/changelog")
async def _api_changelog_get(request: Request):
    """Return parsed changelog sections for the popover."""
    return await handle_changelog_get(request)


@router.get("/api/version")
async def _api_version_get(request: Request):
    """Public: the bare running version, nothing else.

    middleware.AuthMiddleware exempts this one path deliberately, because the
    login page needs to show a version number and has no session to show it
    with. Everything else this app knows about the release -- the full
    changelog -- stays behind auth like the rest of /api/.
    """
    return JSONResponse({"version": config.VERSION.removeprefix("WebConsole_")})


@router.get("/api/skills")
async def _api_skills_get(request: Request):
    return await handle_skills_get(request)


@router.get("/api/usage")
async def _api_usage_get(request: Request):
    return await handle_usage_get(request)


# Registered before no path parameter shadows it; FastAPI matches in order and
# /api/usage has no wildcard sibling, but keeping them adjacent means a future
# /api/usage/{id} cannot silently capture this one.
@router.get("/api/usage/series")
async def _api_usage_series_get(request: Request):
    return await handle_usage_series_get(request)


@router.get("/api/system")
async def _api_system_get(request: Request):
    return await handle_system_get(request)


# Same ordering care as /api/usage/series above: adjacent so a later
# /api/system/{id} cannot capture this path.
@router.get("/api/system/series")
async def _api_system_series_get(request: Request):
    return await handle_system_series_get(request)


@router.get("/api/settings")
async def _api_settings_get(request: Request):
    return await handle_settings_get(request)


@router.patch("/api/settings")
async def _api_settings_patch(request: Request):
    return await handle_settings_patch(request)


@router.get("/api/tokens")
async def _api_tokens_get(request: Request):
    return await handle_tokens_get(request)


@router.post("/api/tokens")
async def _api_tokens_create(request: Request):
    return await handle_tokens_create(request)


@router.delete("/api/tokens/{token_id}")
async def _api_tokens_revoke(request: Request, token_id: str):
    return await handle_tokens_revoke(request)


async def handle_transcripts_list(request: Request):
    """GET /api/transcripts -- recent CLI conversations, newest first."""
    try:
        limit = int(request.query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return JSONResponse({"transcripts": await transcripts.list_recent(limit)})


async def handle_agent_traffic(request: Request):
    """GET /api/agent-traffic -- messages exchanged between concurrent sessions.

    Read out of the transcripts, not off the sockets the sessions actually talk
    over: a log rather than an interception layer, and it needs no knowledge of
    that private protocol.
    """
    try:
        limit = int(request.query_params.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    try:
        scan = int(request.query_params.get("files", 12))
    except (TypeError, ValueError):
        scan = 12
    messages = await transcripts.agent_traffic(limit=limit, scan_files=scan)
    return JSONResponse({"messages": messages, "count": len(messages)})


async def handle_transcript_get(request: Request, session_id: str):
    """GET /api/transcripts/{id} -- one conversation's history.

    Omit both parameters for the most recent page. ``before`` pages backwards
    through a long session; ``offset`` resumes forwards from a byte position.
    """
    raw_before = request.query_params.get("before")
    if raw_before is not None:
        try:
            before = max(0, int(raw_before))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="before must be a number")
        page = await transcripts.read_before(session_id, before)
    else:
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        page = await transcripts.read_turns(session_id, offset)

    if not page["found"]:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return JSONResponse(page)


async def handle_transcript_stream(request: Request, session_id: str):
    """GET /api/transcripts/{id}/stream -- follow a running session over SSE."""
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    if transcripts.transcript_path(session_id) is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    async def event_generator():
        cursor = offset
        idle = 0.0
        yield f"data: {json.dumps({'type': 'start', 'offset': cursor})}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    return
                page = await transcripts.read_turns(session_id, cursor)
                if not page["found"]:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Transcript went away'})}\n\n"
                    return
                cursor = page["offset"]
                for turn in page["turns"]:
                    yield f"data: {json.dumps({'type': 'turn', 'turn': turn, 'offset': cursor})}\n\n"
                idle = 0.0 if page["turns"] else idle + transcripts.TAIL_POLL_S
                if idle >= 15.0:
                    # Comment frame: keeps proxies from dropping an idle stream.
                    yield ": keep-alive\n\n"
                    idle = 0.0
                await asyncio.sleep(transcripts.TAIL_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("transcript stream failed session_id=%s", session_id)
            yield f"data: {json.dumps({'type': 'error', 'error': _SSE_INTERNAL})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/transcripts")
async def _api_transcripts_list(request: Request):
    return await handle_transcripts_list(request)


@router.get("/api/agent-traffic")
async def _api_agent_traffic(request: Request):
    return await handle_agent_traffic(request)


@router.get("/api/transcripts/{session_id}")
async def _api_transcript_get(request: Request, session_id: str):
    return await handle_transcript_get(request, session_id)


@router.get("/api/transcripts/{session_id}/stream")
async def _api_transcript_stream(request: Request, session_id: str):
    return await handle_transcript_stream(request, session_id)


@router.get("/api/sessions")
async def _api_sessions_list(request: Request):
    return await handle_sessions_list(request)


@router.post("/api/sessions/{session_id}/resume")
async def _api_sessions_resume(request: Request, session_id: str):
    return await handle_sessions_resume(request, session_id)


@router.delete("/api/sessions/{session_id}")
async def _api_sessions_delete(request: Request, session_id: str):
    return await handle_session_delete(request, session_id)


# ── Comparison report share ──────────────────────────────────────────────────
# A non-secret report that an admin may want to email or paste to a colleague.
# Served from the project root, not from a chat workspace, so it is not
# subject to per-conversation work_dir containment.

_ROOT: Path = Path(__file__).resolve().parent.parent
_SHARED_FILES: Final[dict[str, tuple[str, str]]] = {
    "comparison.html": ("Backend_Models_20260902.comparison.html", "text/html"),
    "comparison.pdf": ("Backend_Models_20260902.comparison.pdf", "application/pdf"),
    "comparison.md": ("Backend_Models_20260902.comparison.md", "text/plain; charset=utf-8"),
}


@router.get("/api/share/{filename}")
async def _api_share_file(request: Request, filename: str):
    """GET /api/share/{filename} — serve an admin-shared file.

    Read-only, no auth required — the file carries no secret.
    """
    pair = _SHARED_FILES.get(filename)
    if pair is None:
        raise HTTPException(status_code=404, detail="Shared file not found")
    path, media_type = pair
    candidate = _ROOT / path
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Shared file not found")
    return FileResponse(
        candidate,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
