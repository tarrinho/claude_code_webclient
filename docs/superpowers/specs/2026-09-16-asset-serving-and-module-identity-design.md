# Asset serving and module identity — design

**Status:** tests implemented and verified 2026-09-16; the one-line Caddyfile
change is pending — `/etc/caddy/Caddyfile` is root-owned and this session
cannot write it, so it is the operator's to apply.

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

**Point Caddy's asset root at the release, rather than at the working tree.**
One line in `/etc/caddy/Caddyfile`:

```
-   root /home/kali/projects/claude-code-webconsole/web/assets
+   root /home/kali/.local/share/webconsole/releases/current/web/assets
```

`current` is the symlink `bin/wc-deploy.sh` already flips on every deploy, so
the assets follow the release automatically and the HTML and the modules it
names can never come from different builds. Applied with `caddy reload`,
which is graceful — no dropped connections on Caddy 2.11.4.

**Why not remove the block and let FastAPI serve `/assets` instead.** That was
the first proposal here and it is wrong, on measurement rather than on taste.
The app's `StaticFiles` mount inherits the global cache middleware:

```
app   (StaticFiles): cache-control: no-store, no-cache
Caddy (today)      : cache-control: public, max-age=3600
```

`no-store` means the browser re-downloads every asset on every page load —
27 files, 1.3 MB — on a console that also polls. Keeping Caddy in front
preserves the caching that is already correct, and changes exactly one thing:
where the files come from. Serving them from the app would additionally need
the cache header fixed, which is a second change to make the first one
acceptable.

**What it costs.** Editing a file under `web/assets/` stops changing the live
site until it is deployed. That is the point of the change, and it is also a
workflow removal: frontend iteration now requires a deploy, and a deploy
restarts the service and cancels in-flight turns for every session sharing
this host. If that price is too high, the alternative is to keep serving from
the tree and make the skew loud instead of silent — detect it at startup and
warn — which manages the hazard rather than removing it. Recorded here so the
trade is visible rather than discovered afterwards.

## Testing

Both written and both verified by mutation — each was made to fail by
reproducing the real fault, not by editing the assertion.

- **`tests/smoke_assets_match_release.py`** — live, run directly, not
  collected by pytest. Fetches the served HTML, follows every versioned
  module it names, and compares the served bytes against the release on
  disk. This is the assertion nothing else makes:
  `test_qa_asset_versions_match_content.py` compares repo against repo and
  passed throughout the outage. Verified by pointing it at an older release,
  which it rejects naming the four files that differ.
- **`tests/test_qa_single_module_instance.py`** — in the browser suite. Reads
  the real listeners through CDP `DOMDebugger.getEventListeners` and asserts
  one `click` on the conversation list, at most one `input`/`keydown` on the
  composer, and that no module was fetched under two URLs. Verified by giving
  `index.html` a different `?v=` for `app.js`, which is exactly what the
  outage looked like: all three fail, reporting
  `{'app.js': ['app.js?v=2099422', 'app.js?v=2099423']}` and `click: 2`.

Listener counts rather than behaviour, deliberately: one bound handler per
control is the mechanism. A behavioural assertion would only catch the
doubling on controls that happen to be toggles, and only while they stay
toggles.

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
