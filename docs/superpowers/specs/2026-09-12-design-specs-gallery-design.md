# Design Specs Gallery

Design for a read-only (plus admin-gated delete) browsing gallery for this
project's design specs, mirroring the pattern of the generated-images
gallery (`routes/images.py` / `routes/db_images.py` / `web/assets/images.js`,
`docs/superpowers/specs/2026-09-12-generated-images-gallery-design.md`) where
the content type allows, and diverging where it doesn't.

Produced via `/brainstorming`, 2026-09-12. Architectural path: this document
is the spec; implementation follows via the `writing-plans` skill, not this
file directly.

## 1. Why this diverges from the images gallery

Generated images are personal, per-owner, DB-tracked rows: each one belongs
to whoever's turn produced it. Design specs are the opposite — shared,
git-tracked markdown files that every authenticated user should be able to
browse, with no per-owner concept at all. A literal copy of the images
pattern (a new DB table, an `owner_id` column) would be a mismatch for this
content. This design uses a **filesystem scan, no new database table**
instead.

## 2. What counts as a spec

Two-part rule, chosen specifically to avoid a hand-maintained whitelist that
drifts:

1. Everything under `docs/superpowers/specs/*.md` counts automatically —
   already an unambiguous, existing convention.
2. Anywhere else in the repo, a markdown file counts **only if it
   self-declares** with an exact, anchored marker line (not a fuzzy
   substring match — see the false-positive lesson `auto_answer.py`'s own
   docstring already states for exactly this class of matching: *"matching
   phrases against model-authored prose is what made [it] fire on unrelated
   text elsewhere in this tree."*). `AGENT-MODELS-DECISION.md` already
   carries a qualifying line unprompted: *"Produced via `/brainstorming`...
   this document is the spec."* Any future root-level spec needs the same
   kind of line; anything without it (`README.md`, `CHANGELOG.md`,
   `TODO.md`, `SECURITY.md`, etc.) is silently excluded.

This mirrors the "declared, not guessed" discipline this project already
applies to `ai_machines.active_models` (`CLAUDE.md` §0.1) — same shape of
problem, same fix.

## 3. Architecture & components

- **`routes/specs.py`** (new) — filesystem-scan backed, no DB table.
  - `GET /api/specs` — list, per §2's rule, sorted by mtime descending.
  - `GET /api/specs/{id}/content` — one file's content, rendered.
  - `DELETE /api/specs/{id}` — admin-only, file-unlink only.
- **`id`** is a safe encoding of the file's relative path, never the raw path
  itself — same discipline `routes/images.py`'s path-containment check
  already applies, so a client-supplied value can never escape the allowed
  directories.
- **Markdown rendering is server-side**, using the `Markdown` Python
  library — already present as a **dev-only** dependency
  (`requirements-dev.txt`), promoted to a pinned production dependency in
  `requirements.txt` (same convention every existing entry there follows:
  exact version, comment justifying it). No client-side markdown library is
  introduced; there wasn't one before this design (checked: only a false
  positive on the word "marked" in an unrelated comment).
- **`web/assets/specs.js`** (new) — mirrors `images.js`'s list/grid
  structure, but each entry shows title, date, status badge, and
  reverse-link count instead of a thumbnail. Click opens the rendered HTML
  in a panel. A delete button appears only when the session's role is
  admin, and always confirms via `window.confirm()` before calling DELETE
  — same pattern `_deleteTransport` already uses in `transports.js`.
- New Settings tab, wired into `index.html`/`app.js` the same way the images
  tab was.

### 3.1 Three enrichments (shipped now, not deferred)

- **Reverse-link to implementation.** Grep the codebase for each spec's
  filename; specs already get referenced back from code today (example:
  `routes/transports.py`'s own docstring names its design doc). Turns
  "read the spec" into "see what actually implements it."
- **Status badge: Spec only / Planned.** Check `docs/superpowers/plans/`
  for a same-topic plan file (example: cweb8's
  `docs/superpowers/plans/2026-09-12-generated-images-gallery.md`). A match
  → "Planned"; no match → "Spec only."
- **Provenance via `git log --follow`** (author, date introduced) per file —
  no new bookkeeping; git already has this.

### 3.2 Explicitly deferred (future work, not designed here)

- **Stale-spec detection** (flagging specs that name files/functions no
  longer in the codebase) — needs its own heuristic design pass; a
  false-positive-prone flag is worse than none.
- **Full-text search across spec bodies** (reusing the existing FTS5
  precedent from chat message search) — worth adding once title-browsing
  genuinely fails at a larger spec count, not preemptively for ~20 files.

## 4. Data flow

1. `GET /api/specs` scans `docs/superpowers/specs/*.md` (always) plus a
   repo-wide scan for the exact marker (§2). No cache — cheap enough
   (dozens of small files) to re-scan every request, avoiding the
   "list went stale" class of bug this project has already hit twice with
   cached status elsewhere.
2. Per file: extract the first H1 (fallback: filename) and mtime (sort
   key); run the three §3.1 enrichments, each isolated so one file's
   enrichment failure never fails the whole listing.
3. Response: `[{id, title, mtime, status, referenced_by: [...], author,
   date}, ...]`, sorted by mtime descending.
4. `GET /api/specs/{id}/content` resolves `id` back to a real path with the
   same containment check `routes/images.py` uses, reads the file, renders
   it through the (now production) `Markdown` library, returns HTML.
5. `DELETE /api/specs/{id}` — admin-only (403 otherwise). Unlinks the file.
   **No git action** — the removal sits as an uncommitted working-tree
   change like any other edit, for a human/agent to commit deliberately
   afterward. Auto-committing from a UI click in this shared, multi-session
   tree would be exactly the kind of autonomous git action that has caused
   collisions all session (per this project's own working memory).
6. Frontend renders the list, opens content on click, and gates the delete
   button on `session.role === 'admin'` plus a confirm dialog.

## 5. Error handling

- **`id` doesn't resolve to a real file** (deleted since listed, bad
  encoding, race) → 404. Same convention `db_images.py` already uses:
  doesn't-exist, wrong-location, and gone-since-listed all read as "not
  found," never a 403 leaking existence.
- **Path containment check fails** → 404 plus a warning log
  (`spec_outside_allowed_dirs`), mirroring `handle_image_file`'s pattern for
  the same class of check.
- **Non-admin calls DELETE** → 403.
- **DELETE races a concurrent edit** (real risk in this shared tree) →
  unlink is naturally idempotent; an already-gone file is just success with
  nothing to do, same as `generated_image_delete`'s `unlink(missing_ok=True)`.
- **Reverse-link grep or `git log --follow` fails or times out** for one
  file → that entry gets an empty reverse-link list / null provenance.
  Never fails the whole listing.
- **No matching plan file found** → status defaults to "Spec only," not an
  error.
- **Markdown rendering throws** on malformed content → falls back to
  escaped plain text rather than a 500.

## 6. Testing plan

Conventions: `.venv/bin/python -m pytest` invoked bare (never `pytest
tests/`), no writes to the production database, throwaway paths for
anything filesystem-backed.

**Unit tests:**

- Marker regex: exact anchored match only — a file merely mentioning
  "brainstorming"/"spec" in prose does not qualify (regression test for the
  false-positive class `auto_answer.py` already documents).
- `docs/superpowers/specs/*.md` files are always included regardless of
  marker presence.
- A root-level file *with* the exact marker is included; *without* it,
  common noise (`README.md`, `CHANGELOG.md`, `TODO.md`, `SECURITY.md`) stays
  excluded.
- Sort order by mtime holds across mixed naming conventions (dated vs.
  undated filenames).
- Title extraction: H1 present → used; H1 absent → filename fallback.
- `id` encode/decode round-trips correctly; a manipulated `id` that decodes
  outside the allowed directories is rejected (404 + warning log).
- Reverse-link grep: a spec referenced in another file's docstring shows up
  in `referenced_by`; a spec with zero references returns an empty list; a
  grep failure doesn't crash the whole listing (per-file isolation).
- Status badge: a matching file under `docs/superpowers/plans/` yields
  "Planned"; no match yields "Spec only."
- Git provenance: `git log --follow` returns author/date for a committed
  file; an uncommitted file returns null gracefully, no error.
- Admin-only delete: non-admin → 403; admin → succeeds.
- Delete is idempotent (already-gone file doesn't error) and **never
  invokes any git command** — a dedicated assertion, since that is the
  load-bearing safety property from §5.
- Markdown rendering: valid input → expected HTML structure; malformed
  input → escaped plain-text fallback, never a 500.

**Integration tests:**

- `GET /api/specs` against a temp directory mixing real
  `docs/superpowers/specs/` files, one self-declared root file, and one
  noise file (README-shaped) → only the real specs appear, sorted
  correctly, each enriched.
- `GET /api/specs/{id}/content` for a real file → rendered HTML; for a
  since-removed `id` → 404.
- Full delete flow: admin session deletes → file gone, next list omits it;
  non-admin attempt → 403, file untouched.

## 7. Open items for the implementation plan

- Exact `id` encoding scheme (e.g. base64 of the relative path, or a hash)
  is left to the implementation plan.
- The precise anchored-marker pattern's regex is left to the implementation
  plan, following §2's exactness requirement.
