# Supervisor Map — Radial Mind Map

## Motivation

The orchestrator panel shows individual agent activity, but there is no
bird's-eye view of the full system: how many agents are running, from which
transport/host, and which orchestrator contains them. Operators need a single
glanceable view of all agents across all transports, organized as a radial
mind map.

## Non-goals

- Real-time streaming updates (polls on the same interval as the existing
  supervisor endpoint).
- Editing or managing agents from the map (read-only).
- More than 3 levels below center (you → transport → orchestrator/chat).
- D3/npm build — the dependency is a CDN script tag only.

## Data model

No schema changes. The map is assembled at request time from four existing
sources:

1. `ai_machines` with their `transport_id` — group backends by transport,
   identify which backends are "direct" (local server).
2. `orchestrators` — list all orchestrators owned by the user.
3. `orchestrator_tasks` / `orchestrator_messages` — for each orchestrator,
   determine whether it has active tasks.
4. `chat_list` + CLI sessions (`db.read_claude_sessions`) — classify each
   chat as busy/waiting/idle/error/done using the same classification
   function already used by `/api/supervisor`.
5. `orchestrator_members` — which conversations are tied to which
   orchestrator.

The query path:

```
backend → orchestrator → chat
backend → chat (direct, no orchestrator)
```

Each backend group is keyed by `transport_id` name (or "direct" when
`transport_id IS NULL`).

## API design

`GET /api/supervisor-map` returns a single JSON tree:

```json
{
  "center": "You",
  "transport": "direct",
  "children": [
    {
      "id": "orch-abc",
      "label": "Alpha Orchestrator",
      "status": "running",
      "type": "orchestrator",
      "children": [
        {"id": "chat-1", "label": "Research task", "status": "busy", "type": "chat"},
        {"id": "chat-2", "label": "Cleanup", "status": "idle", "type": "chat"}
      ]
    },
    {"id": "chat-3", "label": "Simple chat", "status": "waiting", "type": "chat"}
  ]
}
```

`transport` is `"direct"` when the backend has no transport. When a backend
is on a transport, `transport` carries the transport name and the
top-level children are orchestrators/chats under that transport.

Each node carries:
- `id` — unique identifier (chat_id, orchestrator_id, or transport_id)
- `label` — human-readable name
- `status` — one of: `"running"`, `"busy"`, `"waiting"`, `"idle"`, `"error"`, `"done"`
- `type` — `"transport"` (level 1), `"orchestrator"` (level 2), `"chat"` (leaf)
- `children` — present only for transport and orchestrator nodes, each entry
  is a child node with the same structure

Aggregate status for parent nodes (transport / orchestrator):
- `"running"` if any child is `"running"` or `"busy"`
- `"waiting"` if no child is running but at least one is `"waiting"`
- `"idle"` otherwise
- `"error"` if any child is `"error"` (overrides all)
- `"done"` only if all children are `"done"` (or no children at all)

## UI design

### Top bar

New icon button next to `#orchestratorBtn`. Click opens/closes the
`#supervisorMapPanel` side panel.

### Panel layout

- **Header**: title "Supervisor Map", close button (×), zoom controls
  (fit, +, −)
- **Map area**: D3 radial tree SVG, fills remaining panel space
- **Detail drawer**: slides from right edge when a leaf node is clicked;
  shows chat title, status badge, last message preview (truncated at
  200 chars), timestamp, backend/transport info

### Node rendering

Nodes are colored by status:

| Color     | Status    | Meaning                            |
|-----------|-----------|-------------------------------------|
| `#10b981` | running   | Active turn / mid-stream            |
| `#f59e0b` | busy      | Terminal occupied (CLI busy)        |
| `#f97316` | waiting   | Output unread, needs attention      |
| `#3b82f6` | idle      | Terminal up, nothing happening      |
| `#ef4444` | error     | Turn failed / crashed               |
| `#6b7280` | done      | Finished conversation               |
| `#9ca3af` | transport | Level-1 transport node (neutral)    |

Node shapes:
- Center "You": larger filled circle (dark, theme-aware)
- Transport nodes: medium ring with label
- Orchestrator nodes: small filled circle + label, clickable (expand/collapse)
- Chat nodes: small filled circle + label (leaf, clickable → detail drawer)

Hover tooltips on all nodes: `[status] · label`

### Interactions

- **Click a transport node**: expand/collapse its children (orchestrators and
  direct chats). Animation: smooth transition.
- **Click an orchestrator node**: expand/collapse its chat children.
- **Click a leaf node (chat)**: open detail drawer.
- **Click empty map area**: close detail drawer, deselect nodes.
- **Zoom/pan**: D3 zoom behavior on SVG (wheel, drag). Zoom buttons
  available in header: fit-to-view, 50%, 100%, 150%, 200%, 500%.

### Accessibility

- `aria-label` on each node with `[type] · [status] · [label]`.
- Nodes are `<g role="button" tabindex="0">` for keyboard navigation.
- Color contrast follows WCAG AA (statuses use colors verified against
  a11y palette).

## Testing

### Unit tests (`tests/test_qa_supervisor_map.py`)

- `test_supervisor_map_structure` — verify the JSON tree: transport →
  orchestrators/chats, status classification, child counts match expected.
- `test_supervisor_map_aggregate_status` — parent nodes pick the correct
  aggregate color/status.
- `test_supervisor_map_direct_backend` — backends with `transport_id IS NULL`
  appear under "direct".
- `test_supervisor_map_empty` — no backends → single tree with empty children.
- `test_supervisor_map_auth` — 401 for anonymous, 200 for admin.

### Browser test (`tests/test_frontend_browser.py` → `SupervisorMapBrowserTests`)

- `test_panel_opens_closes` — click icon → panel renders SVG, click close →
  SVG removed.
- `test_zoom_controls_exist` — zoom buttons present, click fit → view resets.
- `test_leaf_click_shows_detail` — click a chat node → detail drawer appears
  with status and title.
- `test_orchestrator_expand` — click an orchestrator node → its children
  appear in the map.

### Smoke test

`curl -b <cookie> http://localhost:8080/api/supervisor-map` returns 200 with
a valid tree structure.

## Error handling

- No backends configured → show "No agents" message in panel (no map).
- DB error → show "Data unavailable" message.
- Timeout → 5s timeout on the aggregate query (covers slow orchestrator task
  fetches).

## File changes

| File | Change |
|------|--------|
| `routes/supervisor_map.py` | New file — handler + db call + router registration |
| `routes/__init__.py` or `app.py` | Register new router (if needed) |
| `web/index.html` | New icon button, new panel div, D3 script import |
| `web/assets/supervisor-map.js` | New file — D3 renderer, zoom, node click, detail drawer |
| `web/assets/app.js` | Wire new button to panel open/close in `DOMContentLoaded` |
| `tests/test_qa_supervisor_map.py` | New file — unit tests |
| `tests/test_frontend_browser.py` | Add `SupervisorMapBrowserTests` class |
| `requirements.txt` | No changes — D3 is CDN-loaded |

## Scope

Focused: one new API endpoint, one new JS module, small HTML changes, no DB
schema changes. D3 v7 from CDN. No npm, no build step. The detail drawer
reuses existing message/query patterns already in the codebase.

The map is a read-only observability tool, not a management interface. That's
an intentional boundary — future management features (stop an agent, send
a prompt) are separate requests.

---

## Changes since this spec (2026-09-09)

This section is appended rather than edited into the text above: the document
is a dated design record, and rewriting it would erase what was decided on
2026-09-07. Where the two disagree, this section is what the code does.

**Node types.** The spec lists three (`transport`, `orchestrator`, `chat`).
There are now seven. `machine` (a backend, carrying `capacity_existing` /
`capacity_total` when the host serving it is this one), `task` (an
orchestrator task; it was being emitted as `chat`, which made the route query
`messages_last()` with a task id on every request and offered conversation
actions for a row with no conversation), `session` (a terminal session,
drawn as a square — the registry was already being read here and discarded,
so these had never appeared), and `more` (an overflow marker, below).

**Children per node.** The spec's "max 4 top-level children and max 4
grandchildren" was implemented as three silent `[:4]` slices, so a host with
six backends showed four and the map gave no sign it had left anything out.
The cap is now 12 and the tail is replaced by a `type: "more"` node carrying
`hidden_count`. Aggregate status is still computed over the *full* group, so a
failure in a hidden node still colours its parent.

**Grouping.** Direct conversations were grouped on `chat["transport_id"]`, a
column the `chats` table does not have — so it was `None` for every
conversation ever created and everything fell into the "Direct" group whatever
backend served it. Resolved through the machine now.

**Refresh.** The non-goal "real-time streaming updates (polls on the same
interval as the existing supervisor endpoint)" was read as "does not poll at
all": the map was fetched once on open and never again. It now refreshes every
10 seconds while open, skipped under an open drawer, and a refresh keeps the
reader's current zoom rather than re-fitting.

**Read-only is no longer the boundary.** The spec's closing paragraph reserves
"stop an agent" for a separate request. The detail drawer now has *Open
conversation* and *Stop*, shown only for the node kinds they apply to. That was
a deliberate later decision, not an oversight in this document.

**Not in the spec at all, and worth recording because each was a live defect.**
`d3.tree()` was created without `.size()`, so the whole tree rendered inside
about one square pixel; `d3.zoom()` was created with no `.on("zoom", ...)`
handler, so every zoom control was inert; `zoomToFit()` called
`_tree.bounds()`, which is not a d3 API and always threw; the root node was
pinned to a hardcoded `translate(200,200)` away from its own children; the SVG
had no `viewBox`; collapsing a branch cleared the collapsed set on the
re-render it triggered, so it could not work; and opening the panel set
`main.hidden = true`, blanking the rest of the page as a workaround for the
click problems the above caused.
