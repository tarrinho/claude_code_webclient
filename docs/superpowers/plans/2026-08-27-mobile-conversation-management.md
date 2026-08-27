# Mobile Conversation Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make WebConsole dependable for daily mobile use while adding persistent pinning, complete conversation actions, Markdown export, and accessible organization.

**Architecture:** Extend the existing SQLite/FastAPI model and API, then divide the current inline frontend into focused modules under the already-mounted `web/assets/` directory. SQLite remains authoritative for conversation state; guarded `localStorage` stores only drafts, theme, and the last selected chat. Docker changes are deferred until all application and acceptance tests pass.

**Tech Stack:** Python 3.13, FastAPI 0.115.6, aiosqlite 0.21.0, Python `unittest`, vanilla ES modules, HTML/CSS, Docker for final deployment only.

**Spec:** `docs/superpowers/specs/2026-08-27-mobile-conversation-management-design.md`

## Global Constraints

- Preserve FastAPI, SQLite, vanilla JavaScript, and the clean blue light/dark visual direction.
- Organization consists only of Pinned, Recent, and Archived groups; do not add folders, tags, or projects.
- Delete removes the database conversation and messages permanently but never removes the workspace directory.
- Export format is Markdown only.
- Do not disable browser zoom.
- Touch controls must be at least 44 by 44 CSS pixels on coarse-pointer devices.
- Use browser storage only for theme, per-chat drafts, and last selected chat; every access must tolerate storage failure.
- Use text nodes or `textContent` for untrusted content; do not introduce unsafe HTML rendering.
- Run Docker build/redeployment only after all application tests pass.
- This directory is not a Git repository, so this plan intentionally has verification checkpoints instead of commit steps.

## File Structure

Create or modify these units:

```text
db.py                         SQLite schema migration and chat data operations
app.py                        Authenticated chat API and Markdown export
web/index.html                Semantic application shell and dialog markup
web/assets/styles.css         Responsive visual system and interaction states
web/assets/api.js             Authenticated HTTP and Markdown-download boundary
web/assets/app.js             Startup, shared state, theme, drawer, dialogs
web/assets/chat-list.js       Search, grouping, row menus, conversation mutations
web/assets/conversation.js    Transcript, streaming, drafts, retry, copy, scrolling
web/login.html                Accessible login feedback and storage guards
test_functional.py            Existing runner/proxy regression coverage
tests/test_db.py              Schema migration, ordering, deletion tests
tests/test_app.py             API mutation and export tests
tests/test_frontend.py        Static contract checks for modular UI and accessibility
```

The approved design names frontend responsibilities at `web/*.js`; this implementation places executable assets under `web/assets/` because that is the existing authenticated application's static mount. The responsibility boundaries remain unchanged.

---

### Task 1: Persist pinning and implement real conversation deletion

**Files:**
- Modify: `db.py:21-66`
- Modify: `db.py:79-152`
- Create: `tests/test_db.py`

**Interfaces:**
- Produces: `async db.chat_get(chat_id: str, owner_id: str, include_archived: bool = False) -> dict | None`
- Produces: `async db.chat_update(chat_id: str, owner_id: str, **fields) -> bool`
- Produces: `async db.chat_delete(chat_id: str, owner_id: str) -> bool`
- Produces chat dictionaries containing `pinned: int`, `pinned_at: str | None`, and existing fields.
- `chat_delete` deletes database rows only and must not access the filesystem.

- [ ] **Step 1: Add isolated database test setup**

Create `tests/test_db.py` using `unittest.IsolatedAsyncioTestCase`. Patch `config.DB_PATH` and `config.PROJECTS_ROOT` to a `TemporaryDirectory`, call `await db.init()` in setup, and `await db.close()` in teardown.

```python
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/webconsole.db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()
```

- [ ] **Step 2: Write failing migration and field tests**

Add tests that inspect `PRAGMA table_info(chats)` and require `pinned`, `pinned_at`, and `deleted_at`. Create a chat and assert all three values are returned by `chat_list()` and `chat_get()` with defaults `0`, `None`, and `None`.

- [ ] **Step 3: Run the focused tests and confirm the schema assertions fail**

Run:

```bash
cd /home/kali/projects/claude-code-webconsole
PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_db
```

Expected: failures reporting missing `pinned`, `pinned_at`, and `deleted_at` columns.

- [ ] **Step 4: Add an idempotent schema migration**

After `executescript()` in `db.init()`, query `PRAGMA table_info(chats)` and add only missing columns:

```python
async def _ensure_chat_columns() -> None:
    cursor = await db_conn.execute("PRAGMA table_info(chats)")
    columns = {row["name"] for row in await cursor.fetchall()}
    migrations = {
        "pinned": "ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        "pinned_at": "ALTER TABLE chats ADD COLUMN pinned_at TEXT",
        "deleted_at": "ALTER TABLE chats ADD COLUMN deleted_at TEXT",
    }
    for name, sql in migrations.items():
        if name not in columns:
            await db_conn.execute(sql)
```

Call it before the final commit in `init()`. Also include the columns in the fresh `CREATE TABLE` declaration.

- [ ] **Step 5: Write failing pin-order and deletion tests**

Create three chats, pin two through `chat_update()`, and assert `chat_list()` orders pinned active chats by `pinned_at DESC`, then unpinned active chats by `updated_at DESC`, then archived chats by `updated_at DESC`.

Create a workspace directory and a chat with two messages, call `chat_delete()`, then assert:

```python
self.assertIsNone(await db.chat_get(chat_id, "admin", include_archived=True))
self.assertEqual(await db.messages_get(chat_id), [])
self.assertTrue(work_dir.exists())
```

Also assert deletion returns `False` for an unknown or wrong-owner chat.

- [ ] **Step 6: Implement owner-scoped retrieval, sorting, pin timestamps, and transactional delete**

Use explicit allowed fields in `chat_update()`:

```python
_ALLOWED_CHAT_FIELDS = {"title", "description", "archived", "pinned", "pinned_at"}
```

Reject any unexpected field rather than interpolating it. When pinning, set `pinned_at = _now()`; when unpinning, set it to `None`. Make `chat_get(..., include_archived=True)` omit the `archived = 0` condition.

Implement deletion atomically:

```python
async with db_conn.execute(
    "SELECT id FROM chats WHERE id = ? AND owner_id = ?", (chat_id, owner_id)
) as cursor:
    if await cursor.fetchone() is None:
        return False
try:
    await db_conn.execute("BEGIN")
    await db_conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    await db_conn.execute("DELETE FROM chats WHERE id = ? AND owner_id = ?", (chat_id, owner_id))
    await db_conn.commit()
except Exception:
    await db_conn.rollback()
    raise
return True
```

Do not call `Path.unlink`, `rmtree`, or any other filesystem deletion API.

- [ ] **Step 7: Run database tests**

Run the Task 1 command. Expected: all `tests.test_db` tests pass.

---

### Task 2: Complete chat mutation and Markdown export APIs

**Files:**
- Modify: `app.py:119-196`
- Modify: `app.py:335-365`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes Task 1 chat dictionaries and database methods.
- Produces `PATCH /api/chats/{chat_id}` supporting `title`, `description`, `archived`, and `pinned`.
- Produces `DELETE /api/chats/{chat_id}` with truthful `404` behavior.
- Produces `GET /api/chats/{chat_id}/export` as a UTF-8 Markdown attachment.

- [ ] **Step 1: Write failing list and patch handler tests**

Use `SimpleNamespace(state=SimpleNamespace(session={"user": "admin"}))` requests and `AsyncMock` database methods. Require the list JSON to expose `pinned` and `pinned_at`. Require patching `pinned: true` to call:

```python
await db.chat_update(chat_id, "admin", pinned=1, pinned_at=ANY)
```

Require title values containing only whitespace to return `400`, descriptions to be capped at 500 characters, and booleans to be normalized to SQLite integers.

- [ ] **Step 2: Write failing delete tests**

Mock `db.chat_delete()` to return `False` and assert `handle_chat_delete()` raises `HTTPException(404)`. Mock it to return `True` and assert the response is `{"ok": true}`.

- [ ] **Step 3: Write failing Markdown export tests**

Add handler-level tests requiring:

- Owner-scoped chat lookup with `include_archived=True`.
- Chronological messages from `db.messages_get()`.
- `Content-Type: text/markdown; charset=utf-8`.
- `Content-Disposition` with a slug-derived `.md` filename.
- Markdown headings and metadata.
- Neutral role labels for unexpected roles.
- `404` for a missing chat.

Use a response body expectation containing:

```markdown
# Example chat

- Created: 27 August 2026
- Workspace: `/projects/example`
- Session: `session-1`

## User

Hello

## Assistant

Hi
```

- [ ] **Step 4: Run API tests and confirm failures**

Run:

```bash
cd /home/kali/projects/claude-code-webconsole
PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_app
```

Expected: failures for missing pin response fields, missing export handler, and current non-deleting DELETE behavior.

- [ ] **Step 5: Implement validation and responses**

Update list/get response projections to include `pinned`, `pinned_at`, and archive state. In PATCH:

- Trim title and reject an empty value.
- Normalize description to `str | None`, capped at 500 characters.
- Normalize `archived` and `pinned` with strict boolean checks.
- Set `pinned_at` from `db._now()` only when pinning.
- Return `404` if no owned row changed.

Update DELETE to return `404` when `db.chat_delete()` returns false.

- [ ] **Step 6: Implement Markdown export**

Import `Response` and quote values with backticks where needed. Add:

```python
async def handle_chat_export(request: Request, chat_id: str):
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    messages = await db.messages_get(chat_id)
    body = render_chat_markdown(chat, messages)
    filename = f"{db.slug_from_title(chat['title'])}.md"
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

Register `GET /api/chats/{chat_id}/export` before the generic chat GET route if route ordering requires it. Keep Markdown rendering in a pure `render_chat_markdown(chat, messages) -> str` helper for direct testing.

- [ ] **Step 7: Run API and existing functional tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_app test_functional.py
```

Expected: all tests pass.

---

### Task 3: Split the frontend into focused static modules

**Files:**
- Modify: `web/index.html`
- Create: `web/assets/styles.css`
- Create: `web/assets/api.js`
- Create: `web/assets/app.js`
- Create: `web/assets/chat-list.js`
- Create: `web/assets/conversation.js`
- Create: `tests/test_frontend.py`

**Interfaces:**
- `api.js` exports `apiFetch(url, options)`, `downloadMarkdown(chat)`, and `ApiError`.
- `chat-list.js` exports `filterChats(chats, query)`, `groupChats(chats)`, and `createChatListController(dependencies)`.
- `conversation.js` exports `parseTimestamp(iso)`, `createConversationController(dependencies)`, and `renderSafeText(container, text)`.
- `app.js` imports those interfaces and owns startup and shared application state.

- [ ] **Step 1: Write failing static frontend contract tests**

In `tests/test_frontend.py`, read the HTML and asset files. Initially require:

```python
self.assertIn('type="module" src="/assets/app.js"', html)
self.assertNotIn("async function sendMessage", html)
self.assertNotIn("<style>", html)
self.assertIn('rel="stylesheet" href="/assets/styles.css"', html)
```

Also assert each expected module exists and contains its named exports.

- [ ] **Step 2: Run the frontend contract test and confirm failure**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_frontend
```

Expected: failure because CSS and JavaScript remain inline.

- [ ] **Step 3: Extract CSS without changing behavior**

Move the complete current style block into `web/assets/styles.css`; replace it with:

```html
<link rel="stylesheet" href="/assets/styles.css">
```

Ensure the page still has no horizontal body overflow and retains light/dark tokens, coarse-pointer sizing, focus-visible styling, and reduced-motion behavior.

- [ ] **Step 4: Implement the API module**

Create `web/assets/api.js`:

```javascript
export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function apiFetch(url, options = {}) {
  const response = await fetch(url, {...options, credentials: 'same-origin'});
  if (response.status === 401) {
    window.location.assign('/login');
    throw new ApiError('Session expired', 401);
  }
  return response;
}

export async function downloadMarkdown(chat) {
  const response = await apiFetch(`/api/chats/${chat.id}/export`);
  if (!response.ok) throw new ApiError('Could not export conversation', response.status);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${chat.title}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}
```

- [ ] **Step 5: Move list and conversation logic into their modules**

Move existing list rendering/search/action logic into `chat-list.js` and stream/transcript/composer logic into `conversation.js`. Pass dependencies such as `apiFetch`, state accessors, toast, and selection callbacks into controller factories; do not import mutable state cyclically.

Use this grouping contract:

```javascript
export function groupChats(chats) {
  return {
    pinned: chats.filter(c => !c.archived && c.pinned),
    recent: chats.filter(c => !c.archived && !c.pinned),
    archived: chats.filter(c => c.archived),
  };
}
```

- [ ] **Step 6: Create `app.js` and reduce `index.html` to structure**

`app.js` owns:

```javascript
const state = {
  chats: [],
  currentChat: null,
  streamState: 'ready',
};
```

Initialize theme, drawer, dialog, list controller, conversation controller, authentication check, and last-chat restoration on `DOMContentLoaded`. Keep dialog markup in `index.html`, but remove all inline JavaScript.

- [ ] **Step 7: Run frontend contract and backend regression tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_frontend test_functional.py
```

Expected: all tests pass and `web/index.html` contains no inline style or application script.

---

### Task 4: Add pinned/recent/archive actions and complete conversation management

**Files:**
- Modify: `web/index.html`
- Modify: `web/assets/styles.css`
- Modify: `web/assets/app.js`
- Modify: `web/assets/chat-list.js`
- Modify: `web/assets/api.js`
- Modify: `tests/test_frontend.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes Task 2 REST endpoints.
- `createChatListController()` provides `render(chats, currentChatId)`, `setQuery(query)`, and `closeMenus()`.
- App-level callbacks provide `selectChat(id)`, `refreshChats()`, `showToast(message, type)`, and `showWelcome()`.

- [ ] **Step 1: Add test fixtures for grouping and action contracts**

Add a browser-independent fixture with one pinned, one recent, and one archived chat. Assert the generated UI includes headings in that order and action labels:

```text
Unpin Example
Rename Example
Export Example
Archive Example
Delete Example
Restore Archived example
```

Assert archived rows cannot be opened but can be restored, exported, or deleted.

- [ ] **Step 2: Replace the single archive icon with an accessible action menu**

Each row contains one `button` with `aria-haspopup="menu"`, `aria-expanded`, and an adjacent menu. Menu items are native buttons with `data-action` and `data-chat-id`. Opening one menu closes any other; Escape closes it and restores focus to its trigger.

- [ ] **Step 3: Implement pin, archive, restore, and rename actions**

Use PATCH calls with one mutation each. Do not mutate the cached object before server success. After success, refresh the list and preserve the active selection. Reuse the existing conversation dialog in edit mode for rename and description changes.

- [ ] **Step 4: Implement delete confirmation**

Add a confirmation mode to the existing dialog that renders the conversation name as text, not HTML. On confirmation:

```javascript
await apiFetch(`/api/chats/${chat.id}`, {method: 'DELETE'});
storageRemove(`wc_draft_${chat.id}`);
if (state.currentChat?.id === chat.id) showWelcome();
await refreshChats();
```

The primary button must say `Delete conversation`, use the fault-red style, and be disabled while pending.

- [ ] **Step 5: Implement Markdown export action**

Invoke `downloadMarkdown(chat)`. If it throws, show `Could not export conversation. Try again.` in the live toast region. Do not navigate away from the current chat.

- [ ] **Step 6: Implement last-selected-chat restoration**

On successful selection, store `wc_last_chat`. After the initial chat list loads, select that ID only if it exists and is not archived; otherwise show the welcome screen. Remove the key when its chat is archived or deleted.

- [ ] **Step 7: Run API and frontend tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m unittest -v tests.test_app tests.test_frontend test_functional.py
```

Expected: all tests pass.

---

### Task 5: Harden the mobile stream experience

**Files:**
- Modify: `web/index.html`
- Modify: `web/assets/styles.css`
- Modify: `web/assets/conversation.js`
- Modify: `web/assets/app.js`
- Modify: `tests/test_frontend.py`
- Modify: `test_functional.py`

**Interfaces:**
- `createConversationController()` exposes `selectChat(chat)`, `send(content?)`, `stop()`, `retry()`, `restoreDraft(chatId)`, and `destroy()`.
- Stream states are exactly: `ready`, `connecting`, `thinking`, `retrying`, `responding`, `stopped`, `failed`.

- [ ] **Step 1: Add stream-state contract tests**

Test source-level contracts for all seven state names, visible Stop and Retry controls, and `AbortController`. Extend the fake-proxy tests so `status`, `text`, `error`, and `done` remain ordered and timeout returns one error event.

- [ ] **Step 2: Implement one explicit stream state reducer**

In `conversation.js`, centralize state transitions:

```javascript
const labels = {
  ready: 'Ready', connecting: 'Connecting…', thinking: 'Thinking…',
  retrying: 'Retrying…', responding: 'Responding…',
  stopped: 'Stopped', failed: 'Failed',
};

function setStreamState(next, detail = '') {
  state.streamState = next;
  statusElement.textContent = detail || labels[next];
  statusElement.dataset.state = next;
  sendButton.textContent = ['connecting','thinking','retrying','responding'].includes(next) ? '■' : '➜';
}
```

Only the reducer updates the visible status, send/stop button, disabled composer state, and Retry visibility.

- [ ] **Step 3: Make cancellation and retry deterministic**

Retain `lastAttempt = {chatId, content}` until a turn finishes successfully. Stop calls `AbortController.abort()`, transitions to `stopped`, restores composer focus, and exposes Retry. Retry operates only when the selected chat matches `lastAttempt.chatId`.

Do not create an assistant row until the first text event. On error or cancellation before text, no empty assistant element remains. On successful completion, clear `lastAttempt` and refresh the authoritative transcript.

- [ ] **Step 4: Add scroll-following and Jump to latest**

Add a button over the message viewport. Before appending a chunk, calculate:

```javascript
const distance = area.scrollHeight - area.scrollTop - area.clientHeight;
const shouldFollow = distance < 96;
```

Scroll only when `shouldFollow` is true. Show `Jump to latest` while farther away; clicking it scrolls to the bottom and resumes following.

- [ ] **Step 5: Finish draft behavior**

Persist the composer value under `wc_draft_<chatId>` on input and before navigation. Restore it after selecting a chat. Clear it only after the server accepts the stream request. If the request fails before acceptance, restore the prompt into the composer and retain it for Retry.

- [ ] **Step 6: Add dialog focus trapping and mobile focus restoration**

When a modal opens, save the active element, focus the first field, and cycle Tab/Shift+Tab between focusable elements. Escape closes the topmost open modal or drawer. Closing returns focus to the original trigger. The mobile drawer uses `aria-hidden` and the menu button uses `aria-expanded`.

- [ ] **Step 7: Complete safe response presentation**

Keep untrusted text in text nodes. Convert fenced code segments to `<pre><code>` using `textContent`. Make code blocks horizontally scrollable. Copy uses `navigator.clipboard.writeText`; failure produces a toast.

- [ ] **Step 8: Run the complete application test suite**

Run:

```bash
cd /home/kali/projects/claude-code-webconsole
PYTHONPATH=. .venv/bin/python -m py_compile *.py tests/*.py
PYTHONPATH=. .venv/bin/python -m unittest discover -v
```

Expected: all tests pass.

---

### Task 6: Verify the application before Docker deployment

**Files:**
- Modify only if verification exposes a defect in files already listed above.

**Interfaces:**
- Consumes the complete application from Tasks 1–5.
- Produces a verified source tree ready for deployment.

- [ ] **Step 1: Start the application against an isolated fake proxy**

Use temporary database and project paths and a fake NDJSON server that emits session ID, status, text, and done frames. Start Uvicorn on an unused loopback port.

- [ ] **Step 2: Run the complete HTTP lifecycle**

Verify:

1. Login.
2. Create a named conversation with description.
3. Stream a response.
4. Reload and confirm transcript/session persistence.
5. Send a resumed second turn.
6. Pin, rename, archive, restore, export, and delete.
7. Confirm the workspace directory remains after deletion.

- [ ] **Step 3: Run desktop and phone viewport checks**

Use an available browser automation runtime. At approximately `390x844` and `1440x900`, verify drawer access, menu focus, dialog focus, composer visibility, Stop/Retry controls, no horizontal body scroll, and Jump to latest behavior. If no browser runtime exists, record that limitation and complete equivalent DOM/static checks rather than claiming visual verification.

- [ ] **Step 4: Run a live two-turn Claude test without Docker changes**

Use the already-running proxy or launch the source proxy with:

```bash
ANTHROPIC_BASE_URL=https://llm.ai-machine.cfappsecurity.com/ \
ANTHROPIC_AUTH_TOKEN="$ANTHROPIC_AUTH_TOKEN" \
WC_CLAUDE_MODEL=azure_ai/gpt-5.6-sol \
.venv/bin/python claude_proxy.py --port 9000 --claude "$(command -v claude)"
```

Verify that the first response streams, the session ID persists, and the second prompt resumes the same session.

- [ ] **Step 5: Re-run all tests after any verification fixes**

Run compilation and `unittest discover -v` again. Expected: no failures.

---

### Task 7: Rebuild Docker last and verify the deployed UI

**Files:**
- Modify: `docker/Dockerfile` only if new static assets are not already copied by `COPY web/ web/`.
- Do not redesign volumes or deployment topology in this milestone.

**Interfaces:**
- Consumes the verified source tree and current working proxy/model configuration.
- Produces the final running `webconsole:latest` deployment.

- [ ] **Step 1: Build only after Task 6 passes**

Run:

```bash
docker build -t webconsole:latest -f docker/Dockerfile .
```

Confirm `web/assets/styles.css`, `app.js`, `api.js`, `chat-list.js`, and `conversation.js` exist inside the image.

- [ ] **Step 2: Recreate only the WebConsole application container**

Preserve the currently working environment values and proxy address. Do not remove the proxy container unless its source changed.

- [ ] **Step 3: Run deployed smoke tests**

Against `http://127.0.0.1:8081`, verify login, create, stream, resume, pin, search, rename, archive, restore, Markdown export, and delete. Confirm the deleted conversation returns `404` and its workspace path still exists.

- [ ] **Step 4: Record final evidence**

Report the test count, live response text, reused session ID, export filename/content type, deletion result, preserved workspace path, deployed container status, and any browser-test limitation. Do not report an item as verified unless its command or browser assertion actually passed.
