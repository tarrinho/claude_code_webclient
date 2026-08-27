# Mobile Conversation Management Design

## Purpose

WebConsole is a mobile-first remote control surface for Claude Code. This milestone makes it fast to find and organize work, reliable during long-running responses, and unambiguous about what Claude is doing.

Docker and deployment changes are outside the implementation scope until all application changes and tests pass.

## Goals

- Make navigation and conversation actions dependable on phone and desktop.
- Give each running turn a clear, persistent state.
- Support pinned, recent, and archived conversations.
- Support rename, archive, restore, permanent chat deletion, and Markdown export.
- Preserve drafts and restore the last active conversation.
- Keep workspace files when deleting a conversation.
- Preserve the current FastAPI, SQLite, vanilla JavaScript, and clean blue light/dark design direction.

## Non-goals

- Folders, tags, or multiple chats grouped under projects.
- Swipe-only gestures.
- Deleting workspace directories.
- Replacing the frontend with a framework.
- Docker or volume restructuring before the feature work is verified.

## Architecture

Use progressive enhancement of the existing application. FastAPI remains the server boundary, SQLite remains authoritative for conversation metadata, and browser storage is limited to per-device conveniences such as drafts, theme, and the last selected conversation.

Split the monolithic frontend into focused files:

```text
web/
├── index.html
├── styles.css
├── app.js
├── api.js
├── chat-list.js
├── conversation.js
└── login.html
```

Responsibilities:

- `index.html`: semantic page structure and dialogs.
- `styles.css`: responsive layout, light/dark tokens, state styling, focus and motion rules.
- `api.js`: authenticated requests, session-expiration redirects, and export requests.
- `chat-list.js`: search, grouping, row action menus, pin/archive/restore/delete interactions.
- `conversation.js`: transcript rendering, stream lifecycle, composer, drafts, copy, retry, and scrolling.
- `app.js`: startup, shared state, theme, drawer, dialogs, and cross-module coordination.

The signature UI element is a compact workspace/status strip beneath the conversation title. It displays the active directory in monospace and the current turn state.

## Data model

Add these columns to `chats`:

```sql
pinned INTEGER NOT NULL DEFAULT 0,
pinned_at TEXT,
deleted_at TEXT
```

Startup performs an idempotent migration for existing databases. `pinned_at` preserves pin order. `deleted_at` reserves a lifecycle marker, although the initial Delete operation permanently removes the database records.

Pinning, archiving, and metadata edits are persistent and owner-scoped. Browser storage must not be used for authoritative conversation organization.

## API

Retain the current endpoints and extend their behavior:

```text
GET    /api/chats
POST   /api/chats
GET    /api/chats/{id}
PATCH  /api/chats/{id}
DELETE /api/chats/{id}
GET    /api/chats/{id}/export
POST   /api/chats/{id}/stream
```

`PATCH /api/chats/{id}` accepts any valid subset of:

```json
{
  "title": "New title",
  "description": "Optional description",
  "pinned": true,
  "archived": false
}
```

The list endpoint sorts conversations in this order:

1. Pinned active conversations, newest pin first.
2. Other active conversations, newest activity first.
3. Archived conversations, newest activity first.

The client presents these groups separately.

### Deletion

`DELETE /api/chats/{id}` verifies ownership and then deletes messages and the chat record in one transaction. It returns `404` for a missing or unauthorized conversation. It never removes or alters the workspace directory.

The UI requires confirmation and includes the conversation title in the prompt. Deleting the active conversation returns to the welcome view and removes its local draft.

### Markdown export

`GET /api/chats/{id}/export` returns a UTF-8 Markdown attachment with a safe filename. It includes:

```markdown
# Conversation title

- Created: 27 August 2026
- Workspace: /projects/example
- Session: …

## User

Prompt text

## Assistant

Response text
```

Messages appear in chronological order. Export failures produce a visible notification without navigating away.

## Conversation navigation

The drawer is available from every mobile screen, including the initial empty state. It contains:

- Search input.
- Pinned conversations.
- Recent conversations.
- Archived conversations.
- One accessible action menu per conversation.

The action menu provides Pin or Unpin, Rename, Export, Archive or Restore, and Delete as appropriate. Explicit controls are used instead of swipe gestures.

Selecting a conversation closes the mobile drawer, restores its draft, updates the workspace/status strip, and records the conversation ID in browser storage. On reload, the client attempts to restore that conversation; if it is unavailable, the welcome state is shown.

Search matches title and description and filters all groups immediately.

## Conversation and composer behavior

The header displays the conversation title. The strip beneath it displays the workspace path and one execution state:

```text
Ready → Connecting → Thinking → Responding → Ready
                         ↓          ↓
                      Retrying    Stopped
                         ↓          ↓
                       Failed ←─────┘
```

Rules:

- Only one request runs within a browser session at a time.
- Send becomes Stop during a turn.
- Stop aborts the stream and does not create an empty assistant message.
- Retry resends the last failed or stopped prompt.
- API retry events display the attempt, maximum attempts, and delay when available.
- Network failures produce a specific notification and preserve the prompt for retry.
- Switching conversations during a response asks the user to stop first.
- Completion refreshes transcript data from the server.
- The composer regains focus after send, stop, retry, and conversation changes where appropriate.

Drafts are stored per conversation in guarded browser storage and cleared after a successful send. Losing browser storage must not break rendering or messaging.

## Reading behavior

Assistant replies support safe basic Markdown presentation and horizontally scrollable fenced code blocks without unsafe HTML injection. Each assistant response has a Copy action.

Automatic scrolling occurs only while the reader is near the bottom. If the reader scrolls upward during streaming, the viewport remains stable and a `Jump to latest` control appears. Activating it resumes bottom-following.

## Mobile and accessibility

- Do not disable browser zoom.
- Touch targets are at least 44 by 44 CSS pixels on coarse-pointer devices.
- Chat rows and actions use native controls.
- Drawer, menus, and dialogs support Escape and restore focus.
- Modal dialogs trap keyboard focus while open.
- Status and errors use live regions.
- Theme, pin, and archive controls expose current state and descriptive labels.
- Long titles truncate before displacing controls.
- Long paths and code scroll within their containers rather than widening the page.
- Reduced-motion preferences disable pulsing, smooth scrolling, and nonessential transitions.
- Login errors are announced, and the username remains populated after failed authentication.

## Error handling

- Server state remains authoritative after all mutations.
- Optimistic changes roll back if the request fails.
- Rename, pin, archive, restore, delete, and export failures display actionable notifications.
- A terminal stream error changes state to Failed and exposes Retry.
- Cancellation changes state to Stopped and exposes Retry.
- Session expiration redirects to login.
- A failed request with no response text leaves no empty assistant bubble.

## Testing

### Backend

Cover:

- Idempotent migration from the current schema.
- Pin and unpin persistence and ordering.
- Owner-scoped metadata changes.
- Archive and restore preserving messages and workspace metadata.
- Transactional deletion of chat and messages while preserving `work_dir`.
- Markdown export metadata and chronological messages.
- Missing or unauthorized resources returning `404`.
- Cancelled or failed streams not persisting empty assistant messages.

### Frontend

Extract stateful logic sufficiently to test:

- Pinned, recent, and archived grouping.
- Search by title and description.
- Action-menu keyboard behavior and focus restoration.
- Draft and last-conversation restoration.
- Valid timestamps with and without a timezone suffix.
- Stream state transitions.
- Stop, retry, and failed-empty-response behavior.
- Near-bottom detection and Jump to latest visibility.
- Delete confirmation and draft cleanup.
- Export error handling.

### Acceptance

At phone and desktop widths:

1. Open the drawer and switch conversations.
2. Search and pin a conversation.
3. Send and stop a response.
4. Retry the stopped prompt.
5. Scroll upward during streaming and use Jump to latest.
6. Rename, archive, restore, export, and delete a conversation.
7. Refresh and confirm restoration of the active conversation and drafts.
8. Navigate drawer, menus, and dialogs using only the keyboard.

The milestone passes when every action works on touch and keyboard, stream state never appears frozen, stop/retry avoids duplicate and empty messages, organization persists, deletion preserves workspace files, Markdown export is complete, and the existing live two-turn Claude test still passes.

## Deployment boundary

Only after application and acceptance tests pass:

1. Rebuild the WebConsole image.
2. Recreate the application container while preserving the current working proxy/model configuration.
3. Run the deployed login, create, stream, resume, organize, export, and delete smoke tests.
