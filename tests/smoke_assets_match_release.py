"""Smoke test: the assets the site serves must come from the release it runs.

Not collected by pytest (no ``test_`` prefix) because it needs the live host —
the running service, its release snapshot on disk, and the reverse proxy in
front of both. Run it directly:

    .venv/bin/python tests/smoke_assets_match_release.py

What it is for. On 2026-09-16 the console served ``index.html`` from the
deployed release while Caddy served ``/assets/*`` straight from the working
tree:

    handle_path /assets/* {
        file_server { root /home/kali/projects/claude-code-webconsole/web/assets }
    }

Whenever the repo was ahead of the last deploy — with eight sessions sharing
the tree, most of the time — the page was assembled from two builds. The HTML
asked for ``app.js?v=8681051`` and the modules imported ``app.js?v=2881380``,
so the browser held two instances of every module and bound every listener
twice. The conversation ⋯ menu did nothing: one controller opened it, its
duplicate closed it in the same tick, silently, with no console error.

Why the existing checks all passed through that outage, and this one would
not have. ``bin/wc-asset-versions.py`` and
``tests/test_qa_asset_versions_match_content.py`` compare repo files against
repo files, and the repo was internally consistent. The release was
internally consistent too. Only the *combination* was broken, so the only
check that can see it is one that asks the server what it actually returns
and compares that against the release the service is running out of. That is
this file, and it is the one assertion nothing else in the suite makes.

Exit code 0 when every module the served HTML names is byte-identical to the
release's copy, 1 when any differ or a module is fetched under two versions,
2 when the service, the release or the credentials are unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# HTTPS through the tailnet name: the session cookie is Secure, so a cookie
# jar never sends it back over http and every authenticated request answers
# 401 while the login itself reports 200 (registry #54).
BASE = os.environ.get("WC_SMOKE_URL", "https://kali-2.tail850c40.ts.net")
RELEASE = Path(os.environ.get(
    "WC_RELEASE_DIR",
    str(Path.home() / ".local/share/webconsole/releases/current")))

FAILURES: list[str] = []


def _fail(message: str) -> None:
    FAILURES.append(message)
    print(f"  FAIL: {message}")


def _admin_password() -> str:
    """From .env, never echoed."""
    env = REPO / ".env"
    if not env.is_file():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("WC_ADMIN_PASSWORD="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class Client:
    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "wc_csrf":
                return cookie.value or ""
        return ""

    def get(self, path: str) -> bytes:
        req = urllib.request.Request(BASE + path)
        with self.opener.open(req, timeout=60) as response:
            return response.read()

    def login(self, password: str) -> None:
        self.get("/login")
        body = json.dumps({"username": "admin", "password": password}).encode()
        req = urllib.request.Request(BASE + "/login", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        token = self._csrf()
        if token:
            req.add_header("X-CSRF-Token", token)
        self.opener.open(req, timeout=60).read()


def main() -> int:
    password = _admin_password()
    if not password:
        print("WC_ADMIN_PASSWORD not found in .env -- cannot reach the service")
        return 2
    if not (RELEASE / "web" / "index.html").is_file():
        print(f"no release at {RELEASE} -- nothing to compare against")
        return 2

    client = Client()
    try:
        client.login(password)
        html = client.get("/").decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach {BASE}: {exc}")
        return 2

    print(f"release : {RELEASE.resolve().name}")

    # Every versioned asset the page names, from the HTML and from the modules
    # it loads -- a module's own imports are where the two builds diverged.
    seen: dict[str, set[str]] = {}

    def note(text: str) -> None:
        for ref in re.findall(r"/?assets/([a-zA-Z0-9_.-]+\.(?:js|css)\?v=\d+)", text):
            seen.setdefault(ref.split("?")[0], set()).add(ref)

    note(html)
    for ref in sorted({r for refs in seen.values() for r in refs}):
        try:
            note(client.get(f"/assets/{ref}").decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError) as exc:
            _fail(f"{ref} could not be fetched: {exc}")

    print(f"modules : {len(seen)} referenced\n")

    # 1. No module may be referenced under two versions: that is the state
    #    that puts two instances in one page.
    for name, refs in sorted(seen.items()):
        if len(refs) > 1:
            _fail(f"{name} is referenced under {len(refs)} versions "
                  f"({', '.join(sorted(refs))}) -- the page will hold one "
                  f"instance of each")

    # 2. What the server returns must be what the release holds. Compared on
    #    bytes rather than on the ?v= number, because the number is derived
    #    from content and the question here is whether the content agrees.
    for name, refs in sorted(seen.items()):
        on_disk = RELEASE / "web" / "assets" / name
        if not on_disk.is_file():
            _fail(f"{name} is served but is not in the release")
            continue
        try:
            served = client.get(f"/assets/{sorted(refs)[0]}")
        except (urllib.error.URLError, OSError) as exc:
            _fail(f"{name} could not be fetched: {exc}")
            continue
        if hashlib.sha256(served).hexdigest() != \
                hashlib.sha256(on_disk.read_bytes()).hexdigest():
            _fail(f"{name} served by the site differs from the release copy "
                  f"-- the page is mixing two builds")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} problem(s); the site is not serving its own release")
        return 1
    print(f"every one of the {len(seen)} served modules matches the release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
