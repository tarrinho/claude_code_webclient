"""The CSP violation sink.

Its own module rather than a function in routes/misc.py, because it is one of
the guardrail surfaces named in the high-assurance spec: it is the only
unauthenticated write path in the application, and a reviewer should be able to
read the whole of it without reading anything else.

WHY IT IS PUBLIC. A browser posts a CSP report with credentials omitted. An
authenticated endpoint would therefore receive nothing at all, and a
report-only rollout would look clean while reporting nothing -- a check that
reports success without checking, which is the defect class this spec exists to
remove. So the exemption is deliberate, and the handler is written for the
consequence: anyone on the network can reach it.

WHAT IT THEREFORE DOES NOT DO. No database write, so it cannot be used to grow
the disk. No response body, so it cannot be used to probe. No echo of any
submitted value. No redirect -- /api/hard-refresh was exempted the same way and
passed an unvalidated `to` into one, which is the incident the auth-exemption
guard in tests/test_qa_api_tokens.py now exists to prevent being repeated by
list edit.

WHAT IT DOES. Reads a bounded body, keeps four fields, truncates each, strips
anything that could forge a log line, writes one record, returns 204.

Spec: docs/superpowers/specs/2026-09-25-high-assurance-development-design.md
      section 4.4, rollout step 1
"""
from __future__ import annotations

import json
import logging
from typing import Any, Final

from fastapi import APIRouter, Request, Response

router = APIRouter()

_log = logging.getLogger("wc.csp_report")

#: A real report is a few hundred bytes. This is generous by two orders of
#: magnitude and still bounds what an unauthenticated caller can make the
#: process allocate -- the body is refused on the declared length before it is
#: read, not measured after.
_MAX_BODY: Final[int] = 16 * 1024

#: The only fields kept. A report may carry a dozen more; none of them are
#: needed to answer "what did the policy block, and where", and every field
#: kept is another piece of attacker-controlled text in the log.
_FIELDS: Final[tuple[str, ...]] = (
    "document-uri", "violated-directive", "blocked-uri", "line-number",
)

#: Per-field cap. Long enough for a real URI, short enough that a report
#: cannot push anything else out of a log line.
_MAX_FIELD: Final[int] = 200


def _clean(value: Any) -> str:
    """One log-safe token from an arbitrary submitted value.

    Control characters are removed rather than escaped: a newline in a
    `blocked-uri` would otherwise start what looks like a second log record,
    and a reader has no way to tell a forged line from a real one after the
    fact. `\\r` matters as much as `\\n` -- a lone carriage return rewrites
    the line in a terminal.
    """
    text = str(value)[:_MAX_FIELD]
    return "".join(ch for ch in text if ch.isprintable())


@router.post("/api/csp-report", status_code=204)
async def csp_report(request: Request) -> Response:
    """Record one CSP violation. Always 204, except an oversized body.

    Malformed input is dropped silently and deliberately. A caller who can
    provoke a distinguishable error learns something about the parser, and
    there is no legitimate client that would act on the difference -- browsers
    ignore the response entirely.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY:
        return Response(status_code=413)

    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return Response(status_code=413)

    try:
        payload = json.loads(raw or b"{}")
        report = payload.get("csp-report") or {}
        if not isinstance(report, dict):
            raise ValueError("csp-report is not an object")
    except (ValueError, AttributeError):
        # Not a report. Nothing to say, to anyone.
        return Response(status_code=204)

    if not report:
        return Response(status_code=204)

    _log.warning(
        "csp_violation: %s",
        " ".join(f"{field}={_clean(report.get(field, ''))}"
                 for field in _FIELDS),
    )
    return Response(status_code=204)
