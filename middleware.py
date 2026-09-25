"""The three middleware classes, and the API-token session they depend on.

Extracted from app.py in 0.10.0. Seven names, 165 contiguous lines, needing
nothing from app.py but a logger.

**Registration stays in app.py, and the order there is load-bearing.** Starlette
applies middleware in the reverse of the order they are added, so the sequence
in app.py -- Security, Auth, CSRF, CORS -- executes as CORS, CSRF, Auth,
Security. Moving the classes does not change that, and this file deliberately
does not register anything: a module that both defines and installs middleware
would make the order depend on import order.
"""
from __future__ import annotations

import logging
import time
from typing import ClassVar, Final

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import db

_log = logging.getLogger("wc.app")


# How often a token's `last_used_at` is written. One write per request would
# put a row update on the hottest path in the server, which is the pressure that
# produced the site-wide `database is locked` (registry #47). A minute is fine
# for the question this column answers -- "is this credential still in use".
_TOKEN_TOUCH_S: Final[float] = 60.0
_token_touched: dict[str, float] = {}


async def _session_from_api_token(request: Request) -> dict | None:
    """Authenticate a request by API token, or return None.

    Tried only after cookie authentication has failed, which keeps the ordering
    unambiguous: a browser session is never silently upgraded or downgraded by a
    header somebody managed to add.
    """
    presented = auth.bearer_from_headers(getattr(request, "headers", None))
    if not presented:
        return None
    ip = "?"
    if getattr(request, "client", None):
        ip = getattr(request.client, "host", "?") or "?"
    claimed = auth.api_token_id_of(presented)
    if not claimed:
        # Not shaped like one of ours. Refused without a database round trip, so
        # a flood of junk Authorization headers cannot become a flood of
        # queries against the connection every other request shares.
        _log.warning("api_token_malformed ip=%s path=%s", ip, request.url.path)
        return None
    try:
        row = await db.api_token_by_hash(auth.hash_api_token(presented))
    except Exception:  # noqa: BLE001 -- a lookup failure must not 500 the request
        _log.warning("api_token_lookup_failed id=%s ip=%s", claimed, ip,
                     exc_info=True)
        return None
    if row is None:
        # Unknown, revoked or expired -- deliberately one message for all three.
        # Telling a caller which of those it is tells an attacker whether an id
        # was ever real.
        _log.warning("api_token_rejected id=%s ip=%s path=%s",
                     claimed, ip, request.url.path)
        return None

    token_id = row["id"]
    now = time.time()
    if now - _token_touched.get(token_id, 0.0) > _TOKEN_TOUCH_S:
        _token_touched[token_id] = now
        try:
            await db.api_token_touch(token_id)
        except Exception:  # noqa: BLE001 -- bookkeeping must never refuse a valid token
            _log.warning("api_token_touch_failed id=%s", token_id, exc_info=True)
    return {
        "user": row["owner_id"],
        "role": row["role"],
        # Read by CsrfMiddleware: a header credential is not attached by a
        # browser on its own, so it does not need the double-submit guard -- and
        # a script cannot hold a cookie to double-submit with anyway.
        "via": "api_token",
        "token_id": token_id,
    }


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, handler):
        sid = request.cookies.get("wc_session")
        request.state.session = auth.session_get(sid) if sid else None
        if request.state.session is None:
            request.state.session = await _session_from_api_token(request)
        # /api/version is the one deliberate exception: the login page has no
        # session yet and still needs to show the running version. It returns
        # only the bare version string -- never the full changelog, which
        # stays behind auth like everything else under /api/.
        #
        # Nothing that reads host state belongs in this list.
        # /api/system/cleanup/preview was added here on 2026-09-20 while the
        # scan was being debugged, and it answers with the host's process
        # table: PIDs, full command lines including --model and --resume, and
        # the Claude session NAMES, which describe what the operator is
        # working on. Measured from outside with no cookie, it returned 200
        # and the live list. The panel calls it through apiFetch with the
        # session cookie, so the exemption bought nothing and was removed.
        # test_qa_api_tokens.py::DevExemptionIsGoneTests is what caught it.
        #
        # "/api/auth/login" went with it. It was added the same day, but no
        # route is registered at that path and nothing in the tree requests
        # it -- the login form posts to /login -- so it exempted nothing and
        # only widened a list whose whole value is being short. If a real
        # endpoint is added there, add the exemption back deliberately and
        # update the guard's expected count in the same commit.
        #
        # "/api/csp-report" is exempt because a browser sends CSP reports with
        # credentials omitted: gated, it would receive nothing, and a
        # report-only rollout would look clean while reporting nothing. The
        # handler was written for the consequence rather than the list being
        # widened for the convenience -- routes/csp_report.py stores nothing,
        # returns no body, echoes no submitted value, and strips control
        # characters out of the one thing it does with the input, which is log
        # it. See tests/test_qa_csp_report.py for the abuse cases.
        public_route = (
            request.url.path == "/login"
            or request.url.path == "/api/version"
            or request.url.path == "/api/hard-refresh"
            or request.url.path == "/api/csp-report"
            or request.url.path.startswith("/assets/")
        )
        if not public_route and request.state.session is None:
            if request.url.path.startswith("/api/"):
                _ip = "?"
                if hasattr(request, "client") and request.client:
                    _ip = getattr(request.client, "host", "?") or "?"
                _log.warning(
                    "session_expired_or_invalid: path=%s ip=%s "
                    "(session may have expired or been revoked) — "
                    "log in again at /login",
                    request.url.path, _ip,
                )
                return JSONResponse(
                    status_code=401,
                    content={"error": "Session expired", "redirect": "/login"},
                )
            return RedirectResponse(url="/login", status_code=303)
        return await handler(request)


def _authenticated_by_token(request: Request) -> bool:
    """Whether this request authenticated with an API token rather than a cookie.

    CSRF exists because a browser attaches cookies to cross-site requests
    automatically. It does not attach an `Authorization` or `X-API-Token` header
    on its own, and a cross-origin script cannot add one without a CORS
    preflight this server never approves -- so for a token-authenticated request
    there is no forgeable ambient credential to guard, and no cookie to
    double-submit against either.

    Read from the session AuthMiddleware built, not from the headers: the header
    is a claim, and only the middleware knows whether it was accepted. Checking
    the header here would let an unauthenticated request skip CSRF by sending a
    junk token -- which is CSRF-off for anyone who asks.
    """
    session = getattr(getattr(request, "state", None), "session", None)
    return isinstance(session, dict) and session.get("via") == "api_token"


class CsrfMiddleware(BaseHTTPMiddleware):
    """Validate the X-CSRF-Token header on mutating requests.

    The login endpoint sets two cookies:
      wc_session  – opaque session id
      wc_csrf     – token issued at login time

    POST /login is exempt – the session cookie is the CSRF guard.
    Every other mutating endpoint must send a matching X-CSRF-Token header.
    GET, OPTIONS, and HEAD are exempt.
    """

    # PUT was missing, so every PUT route was unguarded -- a cross-site request
    # could reorder a user's conversations, and any future PUT would have
    # inherited the same hole silently.
    _MUTATING: ClassVar[set] = {"POST", "PUT", "PATCH", "DELETE"}
    # "/api/csp-report" is exempt because a browser's violation report carries
    # no CSRF token and cannot be made to carry one. The exemption is safe for
    # a narrower reason than the login one: CSRF protects state a forged
    # request could change, and this handler has none -- it stores nothing,
    # returns nothing, and its only effect is a log line whose fields are
    # truncated and stripped of control characters. The worst a forged request
    # achieves is a fabricated violation record, which is also the worst an
    # honest one achieves, since anyone on the network can post to it either
    # way. See routes/csp_report.py.
    _EXEMPT_PATHS: ClassVar[set] = {"/login", "/api/csp-report"}

    async def dispatch(self, request: Request, handler):
        if (
            request.method in self._MUTATING
            and request.url.path not in self._EXEMPT_PATHS
            and not _authenticated_by_token(request)
        ):
            cookie_token = request.cookies.get("wc_csrf", "")
            header_token = request.headers.get("x-csrf-token", "")
            # Pass the session id so the token is checked against *this*
            # session rather than any live one.
            if (
                not cookie_token
                or not header_token
                or not auth._csrf_valid(
                    cookie_token, header_token, request.cookies.get("wc_session")
                )
            ):
                return JSONResponse(
                    status_code=403,
                    content={"error": "CSRF token invalid or missing"},
                )
        return await handler(request)


class SecurityMiddleware(BaseHTTPMiddleware):
    # State-changing methods only. A cross-site GET is what a link is, and
    # refusing one breaks navigation without defending anything the session
    # token does not already cover.
    _MUTATING: ClassVar[set] = {"POST", "PUT", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, handler):
        # Fetch Metadata: refuse a cross-site state-changing request at the
        # edge. This defends the class wc_csrf defends, from a different
        # direction and without per-form plumbing.
        #
        # An ABSENT Sec-Fetch-Site is allowed, and that is the whole design
        # decision rather than an oversight. The remote QA node, the host-side
        # proxy and every curl-based tool send no Sec-Fetch-* headers at all,
        # so a rule requiring the header would have taken out the remote
        # execution path on its first deploy. Only an explicit `cross-site` is
        # refused. The trade -- a non-browser client can always opt out -- is
        # the correct one here, where browser-originated CSRF is the threat
        # and the API token guards the rest.
        if (
            request.method in self._MUTATING
            and request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse(
                status_code=403,
                content={"error": "cross-site request refused"},
            )
        response = await handler(request)
        if hasattr(response, "headers"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            # SAMEORIGIN, not DENY: the console frames its own orchestrator page
            # so it can sit beside the conversation list. The clickjacking
            # threat this header exists for is a *foreign* site framing us,
            # which SAMEORIGIN still refuses -- an attacker's page cannot be
            # same-origin with this one.
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store, no-cache"
            # CSP – emitted unconditionally. This used to be gated on a
            # per-request nonce that nothing ever set, so in practice no CSP
            # shipped at all. Both templates load only external scripts
            # (index.html, login.html) and carry no inline handlers, so
            # script-src 'self' covers them without any nonce plumbing.
            #
            # object-src 'none' rather than leaving it to the default-src
            # fallback: 'self' is not 'none', so a same-origin upload or a
            # reflected path could still be embedded as a plugin document.
            # This application embeds no plugin content, so the directive
            # costs nothing and closes the sink outright.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; object-src 'none'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; "
                "form-action 'self'; report-uri /api/csp-report"
            )
            # Cross-origin isolation. The console opens no cross-origin
            # popups, and its responses are not meant to be embedded
            # anywhere else, so both can be the strictest value.
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            # HSTS – enforce HTTPS for one year, subdomains included.
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )
        return response
