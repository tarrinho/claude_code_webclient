# Asset serving and module identity — design

**Status:** design, approved in chat 2026-09-16; not implemented.

**Goal:** make the frontend the deployed release serves, so a page can never
load two copies of the same module and register every event listener twice.

---

## The defect this exists to remove

Caddy serves `/assets/*` from the working tree, and everything else from the
app:

```
handle_path /assets/* {
    file_server { root /home/kali/projects/claude-code-webconsole/web/assets }
    header Cache-Control "public, max-age=3600"
}
reverse_proxy 127.0.0.1:8080
```

`index.html` therefore comes from the release snapshot the service runs
(`~/.local/share/webconsole/releases/<sha>`), while every ES module comes from
whatever is on disk in the repo right now. The two disagree the moment the
repo moves ahead of the last deploy, which in a tree shared by eight sessions
is most of the time. Measured on 2026-09-16, with the release one commit
behind `main`:

```
index.html (release 88bf720)   ->  app.js?v=8681051
modules    (repo HEAD)         ->  app.js?v=2881380
```

Both URLs are requested, so the browser holds **two module instances** of
`app.js`. Its `DOMContentLoaded` block runs twice, so every controller is
constructed twice and every listener bound twice. Confirmed through CDP
`DOMDebugger.getEventListeners` rather than inferred:

```
#chatList / #chatListDesktop : click x2   (chat-list.js:951)
#menuBtn                     : click x2   (two different script ids)
#composerInput               : input x2, keydown x2
```

**What it looks like to a user:** the conversation `⋯` menu does nothing.
Controller A opens the menu; controller B's handler runs immediately after,
sees a menu that is already open, and closes it. The toggle is
self-cancelling, so the failure is total and silent — no console error, no
visible flicker. Selecting a conversation still works, because doing that
twice is idempotent, which is exactly why the menu looked like an isolated
bug rather than a page-wide one.

Two things make this worse than a normal regression:

- **A deploy does not control the frontend.** Any commit touching
  `web/assets/` — or any uncommitted edit sitting in the tree — is live for
  every user the instant it is saved. Release snapshots exist so that what
  runs is a known commit; for the frontend they currently guarantee nothing.
- **The `?v=` scheme cannot help.** `bin/wc-asset-versions.py` derives each
  reference from the file's own content, and the asset-version test proves
  the repo is internally consistent. Both checks pass here: the repo is
  consistent, the release is consistent, and the page is broken anyway,
  because it is assembled from one of each.

## Scope

**In:** where the browser gets `/assets/*` from, and a check that fails when
the HTML and the modules it names disagree.

**Out, decided explicitly:**

- **No change to the `?v=` scheme.** It is correct and it already has tests.
  The failure is not a bad version number; it is two builds in one page.
- **No client-side "detect double init" guard.** Making `createChatListController`
  idempotent, or having `app.js` refuse to initialise twice, would hide this
  exact symptom while leaving a page running two copies of every module with
  two copies of every module-level variable. The bug to remove is the mixing,
  not its most visible consequence.
- **No cache-busting or cache-header change.** `max-age=3600` on a
  content-hashed URL is correct. `index.html` is already `no-store`.

## Design

**Serve assets from the release, by removing the special case.** Delete the
`handle_path /assets/*` block from the Caddyfile and let the reverse proxy
carry those requests to the app, which already mounts them:

```python
_WEB_DIR = Path(__file__).parent / "web"      # app.py:158
_assets_dir = _WEB_DIR / "assets"
if _assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(_assets_dir)))
```

The running service's `__file__` is inside the release directory, so this
makes every asset come from the same snapshot as the HTML that names it, by
construction rather than by discipline. One source, one build, no skew
possible.

Chosen over pointing Caddy's `root` at
`~/.local/share/webconsole/releases/current/web/assets`, which fixes the skew
just as well but keeps two ways to serve one thing and a symlink that must
stay in step. Fewer moving parts wins when the failure mode is "the two got
out of step".

**The cost, stated rather than discovered later:** the `Cache-Control:
public, max-age=3600` header Caddy adds is lost unless the app sets it.
`StaticFiles` sends `ETag` and `Last-Modified`, so browsers revalidate rather
than re-download, and the URLs are content-hashed, so a wrong cache is not a
correctness risk. If the extra conditional requests matter, add the header in
the mount rather than reinstating a second server.

**The other cost:** editing a file under `web/assets/` will no longer change
the live site until it is deployed. That is the point — but it removes a
workflow people may be relying on without having said so, which is why it is
called out here rather than buried.

## Testing

- **A test that fails today.** Assert that every `?v=` reference in the
  release's `index.html` resolves to a module whose served bytes match that
  release's copy. It must compare what the *server returns* against the
  release on disk, not repo file against repo file — the existing
  `test_qa_asset_versions_match_content.py` already does the latter and
  passes while the page is broken, which is precisely the gap.
- **A duplicate-instance regression test**, in the browser suite: load the
  page and assert `#chatListDesktop` carries exactly one `click` listener,
  via CDP `DOMDebugger.getEventListeners`. This is the assertion that would
  have caught the defect from the symptom end, and it is cheap.
- **The user-visible check:** open the conversation `⋯` menu and assert it is
  still open a moment later. `tests/test_qa_chat_menu_survives_poll.py`
  already covers the poll wiping it; this adds the "opens at all" half.

## What this does not fix, and why it is listed

`tests/test_frontend_browser.py` carries a comment describing this same
failure from 2026-09-10 — *"app.js is currently loaded as two separate module
instances ... so every button"* — as an ambient condition its tests work
around. That comment is evidence this has happened before and was diagnosed
before, and the workaround outlived the diagnosis. Once the skew is
impossible, that comment and any assertions relaxed because of it should be
revisited, but that is cleanup and belongs in its own change.

## Open questions for the implementation plan

- Whether to set `Cache-Control` on the FastAPI mount, or accept
  revalidation. A measurement, not a design decision.
- Whether anything else in the Caddyfile reaches into the working tree; only
  `/assets/*` was checked.
