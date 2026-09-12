# Generated images gallery — design

**Status:** approved in chat 2026-09-12, not yet implemented.

**Goal:** let Pedro browse and delete every image any conversation has ever
generated, from one place in Settings, without hunting back through old
chats to find one.

---

## What already exists — and is out of scope

Seeing a generated image inline, in the conversation it came from, is
**already built** and needs no work:

- `routes/chats.py:_new_workspace_images()` walks a chat's `work_dir` after
  every turn and returns any image file newer than when the turn started —
  detected by mtime, not by the model happening to mention it in prose.
- `_image_markdown()` appends those paths to the stored assistant message as
  markdown image links.
- `GET /api/chats/{id}/file?path=...` (`handle_chat_file`) serves them, with
  path-containment checking scoped to that chat's own `work_dir` — the
  application already runs Claude with `--dangerously-skip-permissions`, so
  this endpoint is deliberately the narrowest boundary that still works.
- `web/assets/conversation.js` already renders a markdown image link as a
  clickable `imageChip`, opening an inline `image-viewer` lightbox.

None of that is touched by this design. What is missing, and what this spec
covers, is a **cross-chat management surface**: list every image an account
has ever generated, in one place, and delete the ones no longer wanted.

## Scope

**In:** a global, owner-scoped gallery in Settings; list, view, delete.

**Out, decided explicitly:**

- **No editing of the original chat message.** Deleting an image from the
  gallery removes the file from disk and its index row. It does not rewrite
  the assistant message that once linked to it — messages are otherwise
  immutable everywhere else in this codebase, and there is no precedent for
  editing one after the fact. The chip in the original conversation is left
  to point at a file that is now gone; see "Frontend" for how that renders.
- **No thumbnail generation.** No image-processing library is a dependency
  today (`requirements.txt` has none), and adding one is a bigger step than
  this feature needs. Full images, lazy-loaded, in a CSS-constrained grid —
  revisit only if this measurably underperforms in practice.
- **No cascade delete when a chat is deleted.** `ARCHITECTURE.md` already
  documents that deleting a chat deliberately leaves its workspace on disk.
  If this feature cascaded on chat deletion, deleting a chat would silently
  make its images vanish from the gallery while the files still sit on
  disk — the opposite of what that existing decision is for. Orphaned rows
  stay listed, labeled with a snapshotted chat title, still manageable.
- **No storage quota, no size cap, no automatic pruning.** This is a
  browsing/cleanup tool a person drives; nothing in it decides on its own
  that an image should go away.

## Data model

New table, additive migration in `db.py`, same pattern `ai_machines` and
`supervisor_members` already use:

```sql
CREATE TABLE generated_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    chat_title  TEXT NOT NULL,           -- snapshotted at index time
    work_dir    TEXT NOT NULL,           -- snapshotted at index time
    owner_id    TEXT NOT NULL,
    path        TEXT NOT NULL,           -- relative to work_dir
    created_at  TEXT NOT NULL            -- ISO 8601 UTC, first detected
);
CREATE INDEX idx_generated_images_owner ON generated_images(owner_id, created_at);
CREATE UNIQUE INDEX idx_generated_images_chat_path ON generated_images(chat_id, path);
```

Two columns are snapshotted rather than joined live, and both exist for the
same reason: **serving and displaying an orphaned image must not depend on
the chat row still existing.**

- `chat_title` — so the gallery can still say *what* generated this image
  after the chat itself is gone, instead of an id no one recognizes.
- `work_dir` — so the file can still be located and served. The existing
  `GET /api/chats/{id}/file` route calls `db.chat_get(chat_id, ...)` first
  and 404s if the chat is gone; reusing it verbatim would make every
  orphaned image permanently unservable the moment its chat is deleted,
  which defeats the "orphans stay manageable" decision above. This is why
  a new serving route exists (see API) instead of reusing that one as-is.

`chat_id` deliberately carries **no foreign-key constraint** to `chats`, for
the same reason: a `chats` row can be deleted out from under this table
without SQLite refusing the operation or requiring an explicit cascade
decision at delete time.

**Idempotency:** the unique index on `(chat_id, path)` means inserting the
same discovered image twice (e.g. if the turn-completion code path is ever
retried) is a no-op, not a duplicate row — insert with
`INSERT OR IGNORE`.

**Self-healing reads:** every listing query checks
`Path(work_dir) / path` exists on disk before returning a row, and silently
skips ones that don't. A file removed by some path other than this feature
(manual cleanup, workspace deleted by hand) just stops appearing next time
the gallery loads — no reconciliation job, no stale-row cleanup task.

## Wiring into the existing turn-completion path

`routes/chats.py`, at the point `_new_workspace_images()` already runs
(around line 1338) and builds the markdown for newly-found images: add one
call, `db.generated_image_record(chat_id, chat_title, work_dir, owner_id,
paths)`, right alongside the existing `_image_markdown(images)` call. No new
scan, no new filesystem walk — the discovery already happened; this is one
more write using data already in hand.

## API

Three new routes, `routes/chats.py` or a new `routes/images.py` (decide at
implementation time based on file size — `routes/chats.py` is already large
per the file-inventory notes elsewhere in this repo's docs).

- **`GET /api/images?limit=60&before=<id>`** — owner-scoped (session user),
  newest first, cursor-paginated on `id`. Applies the self-healing disk
  check per row; a page can come back shorter than `limit` if some rows
  were skipped, which is fine — the client's "Load more" button just asks
  again with the next cursor.
- **`GET /api/images/{id}/file`** — serves the image bytes directly from the
  row's snapshotted `work_dir` + `path`. Same path-containment check
  `handle_chat_file` already applies (resolve, then `is_relative_to`
  against `work_dir`), authorized against `owner_id` on the row rather than
  a live `chat_get` lookup — this is the one genuinely new serving path,
  and it exists specifically so orphaned images keep working.
- **`DELETE /api/images/{id}`** — owner-checked. Deletes the file from disk
  (if present — a missing file is not an error, just means there's only
  the row left to clean up), then deletes the row.

## Frontend

- A seventh Settings tab, **"Images"**, following the exact pattern the
  existing six already use (`data-tab`, `settings-tab`/`settings-panel`
  pairing, `_switchTab`).
- A CSS grid of `<img loading="lazy">` tiles, each `src` pointing at
  `/api/images/{id}/file`. No thumbnail generation (see Scope) — the grid
  constrains tile size in CSS and the browser downscales on decode.
- Clicking a tile opens the existing lightbox viewer (`image-viewer` in
  `conversation.js`), generalized to accept a direct URL rather than only a
  `(chatId, path)` pair — the viewer's DOM and interaction are unchanged,
  only how its `src` is computed.
- Each tile shows the originating chat's title, snapshotted or live; if the
  chat still exists, the title links back to it. If not, it reads
  "`<title>` (chat deleted)" and is plain text.

  **Descoped from v1, found at final review (2026-09-12):** the shipped
  implementation renders `chat_title` as plain text unconditionally, with
  no link and no "(chat deleted)" distinction — the API's list response
  carries `chat_id` but no `chat_exists` signal, and the SPA has no
  URL-per-chat route to link to; wiring "jump to this chat" through the
  existing client-side chat-switching machinery is real, undesigned scope,
  not a one-line fix. Left as an acknowledged gap rather than implemented
  under final-review time pressure. Revisit by adding a `chat_exists`
  boolean to `GET /api/images`'s response (cheap: one extra query per page,
  or a join) and a click handler that calls into the same chat-selection
  path `chat-list.js` already uses.
- A delete button per tile reuses the Yes/No confirmation popup pattern
  already shipped for backend deletion (`3601e65`), rather than a new
  confirmation component.
- **What a deleted image looks like in its original chat:** the markdown
  link is still there (see Scope — messages are never rewritten), so
  `imageChip`/`image-viewer` will request a file that now 404s. The image
  viewer already has an error path for a failed load (used for any file
  that vanished from a workspace by hand); a deleted-from-the-gallery image
  falls into that same existing path, not a new one.
- Pagination: an explicit "Load more" button, matching this app's existing
  preference for explicit user-triggered refresh over infinite scroll
  (e.g. the Backends panel's own "Refresh all", the sync/refresh
  conventions used throughout `web/assets/app.js`).

## Testing

- **Unit** (`db.py`): insert idempotency on `(chat_id, path)`; owner-scoped
  listing excludes another user's rows; a row whose file no longer exists
  is skipped on read; deleting a row with a missing file does not error.
- **Component/API**: list/delete/serve route contracts; ownership
  boundaries (another user's image id 404s, not 403 — matches this
  codebase's existing convention of not confirming an id's existence to a
  non-owner); serving an orphaned image (chat row deleted, row survives)
  still returns the file.
- **QA**, mirroring `tests/test_qa_chat_generated_images.py`'s style: a
  generated image lands in the gallery without any extra step; deleting it
  from the gallery removes it from a second `GET /api/images` call but
  leaves the original chat's message content untouched (asserts the "no
  message rewriting" decision directly, so a future change can't
  reintroduce it silently).

## Open questions for the implementation plan, not this spec

- Whether `routes/images.py` is its own file or lives in `routes/chats.py`
  — a file-size call, not a design call.
- Exact CSS grid sizing/breakpoints — implementation detail, not
  architecture.
