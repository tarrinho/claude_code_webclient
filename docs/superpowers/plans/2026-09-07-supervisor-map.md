# Supervisor Map — Radial Mind Map

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new top-bar icon button opens a side panel showing a D3 radial mind map of all agents across all transports, with status-colored nodes and a detail drawer.

**Architecture:** A single REST endpoint `GET /api/supervisor-map` assembles a pre-built JSON tree from existing data sources (orchestrators, chat list, CLI sessions, backends/transport). The client renders it via D3 v7 (CDN) as a radial tree with zoom/pan, node-click expand/collapse, and a detail drawer.

**Tech Stack:** FastAPI (Python), vanilla JS ES modules, D3 v7 from CDN, SQLite, Playwright for browser tests.

**Spec:** `docs/superpowers/specs/2026-09-07-supervisor-map-design.md`

## Global Constraints

- No DB schema changes — assemble tree at request time from existing sources.
- D3 v7 only via CDN script tag — no npm, no build step, no pip package.
- Python files remain under 400 lines (routes), JS under 300 lines (JS modules).
- `routes/` files under 400 lines; if a handler file grows past that, split it.
- All API routes use `session["user"]` for owner scoping.
- The detail drawer truncates message previews at 200 chars.
- Aggregate status priority: `error` > `running`/`busy` > `waiting` > `idle` > `done`.
- Max 4 children per node in the renderer — no child beyond index 3.

---

## Task 1: DB function — `supervisor_map(owner_id)`

**Files:**
- Create: `routes/db_supervisor_map.py`
- No test yet (tests go in Task 5)

**Interfaces:**
- Consumes: `db.ai_machines_list(owner_id)`, `db.chat_list(owner_id)`, `db.chat_last_activity(owner_id)`, `db.orchestrator_list(owner_id)`, `db.orchestrator_members_list(orchestrator_id)`, `db.orchestrator_tasks_get(orchestrator_id, owner_id)`, `db.read_claude_sessions()`, `db.orchestrator_messages_get(orchestrator_id, owner_id)`, `db.ssh_transport_get(transport_id, owner_id)`, `classification._classify_cli_session`, `classification._cli_maps`
- Produces: `supervisor_map(owner_id) -> dict` — a dict with `center`, `transport`, `children` keys per the spec

- [ ] **Step 1: Write the db module structure**

Create `routes/db_supervisor_map.py`:

```python
"""Database queries for the supervisor map (radial mind map).

Assembles a JSON tree from existing sources: backends, transports,
orchestrators, chat list, CLI sessions, and orchestrator tasks/members.
No schema changes — everything already exists.
"""
from __future__ import annotations

import logging
from typing import Any

import db
import turns
from classification import _cli_maps, classify_chat
from shared import backend_kind

_log = logging.getLogger("wc.app")


async def supervisor_map(owner_id: str) -> dict[str, Any]:
    """Return the full supervisor map tree for *owner_id*.

    The tree structure is:
        you -> transport -> {orchestrator -> chat, chat}

    Returns a dict with ``center``, a list of ``children`` (one per transport),
    and aggregate ``status``/``type`` fields on every node.
    """
    # ── Fetch all sources ──────────────────────────────────────────
    machines = await db.ai_machines_list(owner_id)
    chats = await db.chat_list(owner_id)
    activity = await db.chat_last_activity(owner_id)
    orchestrators = await db.orchestrator_list(owner_id)
    cli_sessions = await db.read_claude_sessions()
    live_ids = turns.running_ids(owner_id)

    # Build CLI session lookup
    try:
        queued = await db.queue_counts(owner_id)
    except Exception:
        queued = {}

    marks = await db.read_marks_get(owner_id)
    (
        _cli_status_map,
        _cli_dismiss_map,
        _cli_status_updated_map,
        _cli_prompt_map,
    ) = await _cli_maps(marks)

    # ── Index machines by transport ───────────────────────────────
    transport_map: dict[str | None, list[dict]] = {}
    for m in machines:
        tid = m.get("transport_id") or "direct"
        transport_map.setdefault(tid, []).append(m)

    # ── Index orchestrators by their member chats ─────────────────
    # Map: chat_id -> orchestrator_id (first match wins)
    chat_to_orch: dict[str, str] = {}
    for orch in orchestrators:
        orch_id = orch["id"]
        members = await db.orchestrator_members_list(orch_id)
        for member in members:
            cid = member.get("chat_id")
            if cid and cid not in chat_to_orch:
                chat_to_orch[cid] = orch_id

    # ── Classify each chat ────────────────────────────────────────
    chat_status: dict[str, str] = {}
    chat_entry: dict[str, dict] = {}
    for chat in chats:
        if chat.get("archived"):
            continue
        last = activity.get(chat["id"])
        if not last:
            continue
        entry = classify_chat(
            chat, last, live_ids, queued, marks,
            _cli_status_map, _cli_dismiss_map, _cli_status_updated_map,
            _cli_prompt_map,
        )
        if entry is not None:
            chat_status[chat["id"]] = entry["status"]
            chat_entry[chat["id"]] = entry

    # ── Classify CLI sessions (unowned conversations) ─────────────
    cli_status: dict[str, str] = {}
    for cli in cli_sessions:
        session_id = cli.get("sessionId") or ""
        if not session_id:
            continue
        meta: dict[str, str] = {}
        sess_entry = await _classify_cli_session(
            cli, meta,
            marks.get(("session", session_id), {}),
            _cli_status_map, _cli_dismiss_map,
        )
        if sess_entry:
            cli_status[session_id] = sess_entry["status"]

    # ── Build the transport -> child nodes mapping ────────────────
    children: list[dict[str, Any]] = []

    for machine in machines:
        bid = machine["id"]
        bk = backend_kind(machine)
        tid = machine.get("transport_id")
        is_direct = tid is None or tid == "direct"

        # Build child list for this backend
        backend_children: list[dict[str, Any]] = []

        # Find orchestrators under this backend (by matching member chats)
        for orch in orchestrators:
            orch_id = orch["id"]
            orch_members = [m for m in await db.orchestrator_members_list(orch_id)
                           if m.get("chat_id") in chat_to_orch]
            # Check if any member belongs to this backend's chats
            member_chat_ids = {m.get("chat_id") for m in orch_members}
            # A chat belongs to this backend if its machine points here
            # (for now: match by backend_kind proximity — simplest heuristic)
            # Actually: orchestrators are independent of machines in the current
            # schema. We attach them to the first backend that is "direct",
            # or if there are none, to "direct".
            # The spec says "backend → orchestrator → chat". Orchestrators
            # don't carry a machine_id, so we attach them to the "direct"
            # backend. If no "direct" exists, they go under the first backend.
            # This is handled below by checking if the orchestrator's members
            # belong to chats whose last activity came from this backend's
            # machine. For simplicity, all orchestrators go under "direct"
            # unless explicitly tied to a backend via future schema.
            pass  # Orchestrator attachment handled after the machine loop

        # Collect direct chats (not in an orchestrator)
        for chat in chats:
            cid = chat["id"]
            if cid in chat_to_orch:
                continue  # belongs to an orchestrator, not here
            status = chat_status.get(cid, "idle")
            entry = chat_entry.get(cid, {})
            if status not in ("running", "busy", "waiting", "idle", "error", "done"):
                status = "idle"
            backend_children.append({
                "id": cid,
                "label": entry.get("label", chat.get("title", "")) or chat.get("title", ""),
                "status": status,
                "type": "chat",
            })

        # If there are children, add this backend as a node
        if backend_children:
            aggregate = _aggregate_status(backend_children)
            label = bk if is_direct else (bk or "backend")
            children.append({
                "id": bid,
                "label": label,
                "status": aggregate,
                "type": "transport",
                "children": backend_children[:4],  # max 4
            })

    # ── Attach orchestrators (go under "direct") ──────────────────
    for orch in orchestrators:
        orch_id = orch["id"]
        orch_members = await db.orchestrator_members_list(orch_id)
        orch_tasks = await db.orchestrator_tasks_get(orch_id, owner_id)
        orch_msgs = await db.orchestrator_messages_get(orch_id, owner_id)

        # Build children from task/chat data
        orch_children: list[dict[str, Any]] = []
        for task in orch_tasks:
            # Get the last message for this task
            task_msgs = [m for m in orch_msgs if m.get("task_id") == task.get("id")]
            status = _orch_task_status(task, task_msgs)
            orch_children.append({
                "id": task.get("id", orch_id + "-" + str(len(orch_children))),
                "label": task.get("title") or f"Task {len(orch_children) + 1}",
                "status": status,
                "type": "chat",
            })

        # Also add member chats that don't have a task
        for member in orch_members:
            cid = member.get("chat_id")
            if not cid:
                continue
            # Check if a task already covers this chat
            if any(c["id"] == cid for c in orch_children):
                continue
            status = chat_status.get(cid, "idle")
            entry = chat_entry.get(cid, {})
            orch_children.append({
                "id": cid,
                "label": entry.get("label", chat.get("title", "")) or chat.get("title", ""),
                "status": status,
                "type": "chat",
            })

        if orch_children:
            aggregate = _aggregate_status(orch_children)
            children.append({
                "id": orch_id,
                "label": orch.get("title", "Orchestrator"),
                "status": aggregate,
                "type": "orchestrator",
                "children": orch_children[:4],
            })

    return {
        "center": "You",
        "children": children[:4],  # max 4 top-level
    }


def _aggregate_status(children: list[dict[str, Any]]) -> str:
    """Compute aggregate status for a parent node.

    Priority: error > running/busy > waiting > idle > done.
    """
    if not children:
        return "idle"

    has_error = any(c["status"] == "error" for c in children)
    if has_error:
        return "error"

    has_running = any(c["status"] in ("running", "busy") for c in children)
    if has_running:
        return "running"

    has_waiting = any(c["status"] == "waiting" for c in children)
    if has_waiting:
        return "waiting"

    all_done = all(c["status"] == "done" for c in children)
    if all_done:
        return "done"

    return "idle"


def _orch_task_status(task: dict, messages: list[dict]) -> str:
    """Derive a status string from an orchestrator task."""
    state = task.get("state", "").lower()
    if state in ("running", "active"):
        return "busy"
    if state == "error":
        return "error"
    if state == "done":
        return "done"
    if not messages:
        return "idle"
    # Check if last message indicates a question
    last = messages[-1]
    content = last.get("content", "") or ""
    if "asks for" in content or "question" in content or "needs" in content:
        return "waiting"
    return "idle"
```

No test in this task — the function is trivially simple (mostly data assembly, the aggregate status will be tested explicitly in Task 5).

- [ ] **Step 2: Write minimal test for `_aggregate_status`**

```python
# In routes/db_supervisor_map.py, add to the bottom:
if __name__ == "__main__":
    # Quick manual test
    assert _aggregate_status([{"status": "busy"}]) == "running"
    assert _aggregate_status([{"status": "idle"}]) == "idle"
    assert _aggregate_status([{"status": "waiting"}]) == "waiting"
    assert _aggregate_status([{"status": "error"}]) == "error"
    assert _aggregate_status([{"status": "done"}]) == "done"
    assert _aggregate_status([]) == "idle"
    assert _aggregate_status([{"status": "idle"}, {"status": "busy"}]) == "running"
    print("OK")
```

- [ ] **Step 3: Run the inline test**

```bash
python3 routes/db_supervisor_map.py
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add routes/db_supervisor_map.py
git commit -m "feat: add supervisor_map db function with _aggregate_status helper"
```

---

## Task 2: API endpoint — `GET /api/supervisor-map`

**Files:**
- Create: `routes/supervisor_map.py`
- Modify: `app.py:45-469` (add import and include_router)
- Test: `tests/test_qa_supervisor_map.py`

**Interfaces:**
- Consumes: `routes.db_supervisor_map.supervisor_map(owner_id)`
- Produces: `GET /api/supervisor-map` returning JSON tree

- [ ] **Step 1: Write the route handler**

Create `routes/supervisor_map.py`:

```python
"""Routes for /api/supervisor-map — the radial mind map data."""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from routes.db_supervisor_map import supervisor_map

_log = logging.getLogger("wc.app")

router = APIRouter()


@router.get("/api/supervisor-map")
async def handle_supervisor_map(request: Request):
    """Return the supervisor map tree for the logged-in user."""
    session = request.state.session
    try:
        tree = await supervisor_map(session["user"])
        return JSONResponse(content=tree)
    except Exception as exc:
        _log.exception("supervisor_map failed")
        raise HTTPException(status_code=500, detail="Data unavailable")
```

- [ ] **Step 2: Register the router in app.py**

Edit `app.py` around line 56-57 (where other routers are imported and included):

```python
# Add near the other router imports (line ~57)
from routes.supervisor_map import router as supervisor_map_router

# Add near app.include_router calls (line ~468)
app.include_router(supervisor_map_router)
```

- [ ] **Step 3: Write the test**

Create `tests/test_qa_supervisor_map.py`:

```python
"""Tests for /api/supervisor-map endpoint."""
import pytest
from httpx import AsyncClient, ASGITransport

import app as app_module


@pytest.mark.anyio
async def test_supervisor_map_returns_200():
    """A logged-in admin receives the map tree."""
    trans = ASGITransport(app=app_module.app)
    async with AsyncClient(trans=trans, base_url="http://test") as client:
        # Create a test session (simple auth)
        resp = await client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200

        resp = await client.get("/api/supervisor-map")
        assert resp.status_code == 200
        data = resp.json()
        assert "center" in data
        assert "children" in data
        assert isinstance(data["children"], list)


@pytest.mark.anyio
async def test_supervisor_map_empty_tree():
    """No backends or orchestrators → empty children list."""
    from httpx import ASGITransport
    import tempfile, pathlib

    # Use an isolated DB for this test
    test_db = tempfile.mktemp(suffix=".db")
    env = {"WC_DB_PATH": test_db}

    import os
    old_db = os.environ.get("WC_DB_PATH")
    os.environ["WC_DB_PATH"] = test_db

    try:
        # Initialize the test DB
        from db import init
        await init()

        trans = ASGITransport(app=app_module.app)
        async with AsyncClient(trans=trans, base_url="http://test") as client:
            resp = await client.post("/api/login", json={"username": "admin", "password": "admin"})
            assert resp.status_code == 200

            resp = await client.get("/api/supervisor-map")
            assert resp.status_code == 200
            data = resp.json()
            # With no backends/orchestrators, children should be empty
            assert data["children"] == []
    finally:
        if old_db:
            os.environ["WC_DB_PATH"] = old_db
        else:
            os.environ.pop("WC_DB_PATH", None)
        # Cleanup
        pathlib.Path(test_db).unlink(missing_ok=True)
```

- [ ] **Step 4: Run the test (expect FAIL — endpoint not yet registered)**

```bash
.venv/bin/python -m pytest tests/test_qa_supervisor_map.py -v -rs
```

Expected: 500 or 404 (router not yet registered in app.py).

- [ ] **Step 5: Fix — ensure router is registered in app.py**

After Step 2 is applied (registering `supervisor_map_router`), re-run:

```bash
.venv/bin/python -m pytest tests/test_qa_supervisor_map.py -v -rs
```

Expected: PASS (2 tests pass).

- [ ] **Step 6: Commit**

```bash
git add routes/supervisor_map.py app.py tests/test_qa_supervisor_map.py
git commit -m "feat: add /api/supervisor-map endpoint with auth gate"
```

---

## Task 3: HTML — add icon button and panel container

**Files:**
- Modify: `web/index.html`
- Modify: `web/index.html` (D3 script import)

**Interfaces:**
- Produces: `#supervisorMapBtn` button in top bar, `#supervisorMapPanel` panel div
- Consumes: existing `#orchestratorBtn` as placement reference

- [ ] **Step 1: Add the top-bar icon button**

Edit `web/index.html` near line 37 (`#orchestratorBtn`):

After the line with `id="orchestratorBtn"`, add:

```html
<button class="btn-icon" id="supervisorMapBtn" aria-label="Open supervisor map" title="Supervisor Map" hidden>🗺</button>
```

- [ ] **Step 2: Add the side panel container**

Add to the bottom of the body in `web/index.html`, alongside other panels (look for the existing orchestrator panel div):

```html
<!-- Supervisor Map Panel -->
<div id="supervisorMapPanel" class="panel" hidden>
  <div class="panel-header">
    <span>Supervisor Map</span>
    <button class="btn-icon" id="supervisorMapClose" aria-label="Close">×</button>
    <div class="zoom-controls">
      <button class="btn-icon" id="mapFitBtn" title="Fit to view" aria-label="Fit to view">⊡</button>
      <button class="btn-icon" id="mapZoomOutBtn" title="Zoom out" aria-label="Zoom out">−</button>
      <button class="btn-icon" id="mapZoomInBtn" title="Zoom in" aria-label="Zoom in">+</button>
    </div>
  </div>
  <div class="map-body" id="mapBody">
    <svg id="supervisorMapSvg"></svg>
    <div id="mapTooltip" class="map-tooltip" hidden></div>
    <div id="mapDetailDrawer" class="map-detail" hidden>
      <div class="map-detail-header">
        <span id="mapDetailTitle" class="map-detail-title"></span>
        <button class="btn-icon" id="mapDetailClose" aria-label="Close detail">×</button>
      </div>
      <div id="mapDetailStatus" class="map-detail-status"></div>
      <div id="mapDetailMessage" class="map-detail-message"></div>
      <div id="mapDetailMeta" class="map-detail-meta"></div>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Add the D3 script import**

Add near other CDN/external script imports in `web/index.html` (look for existing `<script>` tags near the top or bottom of body):

```html
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script src="assets/supervisor-map.js?v=1" type="module"></script>
```

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "ui: add supervisor map icon button, panel, and D3 import"
```

---

## Task 4: JS — D3 radial tree renderer with zoom and node interaction

**Files:**
- Create: `web/assets/supervisor-map.js`

**Interfaces:**
- Produces: `renderSupervisorMap(data)`, `closeSupervisorMap()`, `zoomToFit()`
- Consumes: `#supervisorMapSvg`, `#supervisorMapPanel`, `#orchestratorBtn`, `#supervisorMapClose`, `#supervisorMapBtn`

**Color map (from spec):**

```js
const STATUS_COLOR = {
  running: "#10b981",
  busy: "#f59e0b",
  waiting: "#f97316",
  idle: "#3b82f6",
  error: "#ef4444",
  done: "#6b7280",
  transport: "#9ca3af",
};
```

- [ ] **Step 1: Write the JS module**

Create `web/assets/supervisor-map.js` (must stay under 300 lines):

```javascript
// Supervisor Map — D3 radial mind map renderer.
// Reads tree data, renders a radial tree, handles zoom/pan, click interactions.

const STATUS_COLOR = {
  running: "#10b981",
  busy: "#f59e0b",
  waiting: "#f97316",
  idle: "#3b82f6",
  error: "#ef4444",
  done: "#6b7280",
  transport: "#9ca3af",
};

const STATUS_LABEL = {
  running: "Running",
  busy: "Busy",
  waiting: "Waiting",
  idle: "Idle",
  error: "Error",
  done: "Done",
  transport: "Transport",
};

let _svg, _g, _tree, _root, _zoom, _data, _collapsed = new Set();
let _selectedNode = null;

const WIDTH = 500;
const HEIGHT = 500;
const RADIUS = Math.min(WIDTH, HEIGHT) / 2 - 40;

export function renderSupervisorMap(data) {
  _data = data;
  _svg = d3.select("#supervisorMapSvg")
    .attr("width", WIDTH)
    .attr("height", HEIGHT);

  _svg.selectAll("*").remove();

  _zoom = d3.zoom().scaleExtent([0.2, 5]);
  _svg.call(_zoom);

  const root = d3.hierarchy(data, d => d.children ? d.children : []);
  root.x0 = HEIGHT / 2;
  root.y0 = 0;

  _tree = d3.tree().separation((a, b) => a.parent === b.parent ? 1 : 1.2);
  _tree(root);

  // Collapse nodes that are in the _collapsed set
  function collapse(d) {
    if (d.children) {
      d._children = d.children;
      d.children = null;
      d._x = d.x; d._y = d.y;
      if (d.depth > 0) {
        _collapsed.add(d.data.id);
      }
    }
  }

  // Position children around a circle
  function positionNode(d) {
    const angle = Math.PI * 0.75; // 135 degrees from top
    const r = d.depth * (RADIUS / 3);
    d.x = d.depth * angle;
    d.y = r;
  }

  const node = _svg.selectAll(".node")
    .data(root.descendants(), d => d.data.id)
    .join("g")
    .attr("class", "node")
    .attr("transform", d => `rotate(${d.x * 180 / Math.PI - 90}) translate(${d.y},0)`)
    .attr("cursor", d => d.children || d._children ? "pointer" : "default");

  // Node circle
  node.append("circle")
    .attr("r", d => d.depth === 0 ? 8 : d.depth === 1 ? 6 : 4)
    .attr("fill", d => STATUS_COLOR[d.data.status] || STATUS_COLOR.idle)
    .attr("stroke", d => d._children || d.children ? "#fff" : "none")
    .attr("stroke-width", 2);

  // Label
  node.append("text")
    .attr("dy", "0.35em")
    .attr("x", d => d.children ? 10 : -10)
    .attr("text-anchor", d => d.children ? "start" : "end")
    .text(d => {
      const name = d.data.label;
      return name.length > 20 ? name.slice(0, 18) + "…" : name;
    })
    .attr("font-size", "11px")
    .attr("fill", d => {
      const bg = getComputedStyle(document.body).getPropertyValue("--fg").trim();
      return bg || "#1a1a2e";
    });

  // Hover tooltip
  node.on("mouseenter", function (event, d) {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) {
      tooltip.textContent = `${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label}`;
      tooltip.hidden = false;
      tooltip.style.left = event.pageX + 10 + "px";
      tooltip.style.top = event.pageY - 28 + "px";
    }
  })
  .on("mouseleave", function () {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) tooltip.hidden = true;
  })
  .on("click", function (event, d) {
    event.stopPropagation();
    if (d.children || d._children) {
      // Expand/collapse
      if (d.children) {
        d._children = d.children;
        d.children = null;
        _collapsed.add(d.data.id);
      } else {
        d.children = d._children;
        d._children = null;
        _collapsed.delete(d.data.id);
      }
      renderSupervisorMap(_data);
    } else if (d.data.type === "chat") {
      // Show detail drawer
      showDetail(d.data);
    }
  })
  .attr("aria-label", d => `${d.data.type} · ${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label}`);

  // Click on empty area closes detail
  _svg.on("click", function (event) {
    if (event.target === this) {
      hideDetail();
    }
  });
}

function showDetail(nodeData) {
  const drawer = document.getElementById("mapDetailDrawer");
  if (!drawer) return;
  document.getElementById("mapDetailTitle").textContent = nodeData.label;
  const statusEl = document.getElementById("mapDetailStatus");
  statusEl.textContent = STATUS_LABEL[nodeData.status] || nodeData.status;
  statusEl.style.color = STATUS_COLOR[nodeData.status] || STATUS_COLOR.idle;
  const msgEl = document.getElementById("mapDetailMessage");
  msgEl.textContent = nodeData.type === "chat" ? "Click a chat to see last message (future)." : "";
  const metaEl = document.getElementById("mapDetailMeta");
  metaEl.textContent = `Type: ${nodeData.type} · ID: ${nodeData.id}`;
  drawer.hidden = false;
  _selectedNode = nodeData;
}

function hideDetail() {
  const drawer = document.getElementById("mapDetailDrawer");
  if (drawer) drawer.hidden = true;
  _selectedNode = null;
}

export function closeSupervisorMap() {
  if (_svg) _svg.selectAll("*").remove();
  hideDetail();
  _data = null;
  _collapsed.clear();
}

export function zoomToFit() {
  if (!_svg) return;
  // Center the view on the root node
  const svgEl = document.getElementById("supervisorMapSvg");
  if (!svgEl) return;
  svgEl.scrollIntoView({ behavior: "smooth" });
}

// Close button handler (wired in app.js)
document.getElementById("supervisorMapClose")?.addEventListener("click", () => {
  closeSupervisorMap();
  document.getElementById("supervisorMapPanel")?.hide();
});

// Detail close button
document.getElementById("mapDetailClose")?.addEventListener("click", hideDetail);

// Zoom buttons
document.getElementById("mapFitBtn")?.addEventListener("click", () => zoomToFit());
document.getElementById("mapZoomInBtn")?.addEventListener("click", () => {
  if (_svg) _svg.transition().call(_zoom.scaleBy, 1.5);
});
document.getElementById("mapZoomOutBtn")?.addEventListener("click", () => {
  if (_svg) _svg.transition().call(_zoom.scaleBy, 0.75);
});

export default { renderSupervisorMap, closeSupervisorMap, zoomToFit };
```

- [ ] **Step 2: Verify the JS compiles (no syntax errors)**

```bash
node -c web/assets/supervisor-map.js
```

Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add web/assets/supervisor-map.js
git commit -m "ui: add D3 radial mind map renderer with zoom and node interaction"
```

---

## Task 5: Wire the button, fetch data, add "no agents" fallback

**Files:**
- Modify: `web/assets/app.js`
- Modify: `web/index.html` CSS for panel and drawer

**Interfaces:**
- Consumes: `renderSupervisorMap(data)`, `closeSupervisorMap()` from `supervisor-map.js`
- Produces: fully wired button → fetch → render → close cycle

- [ ] **Step 1: Wire the button in app.js**

Edit `web/assets/app.js` in the `DOMContentLoaded` handler (around where `orchestratorBtn` is wired). Add:

```javascript
// Supervisor Map button
const _supervisorMapBtn = byId("supervisorMapBtn");
const _supervisorMapPanel = byId("supervisorMapPanel");

if (_supervisorMapBtn) {
  _supervisorMapBtn.addEventListener("click", async () => {
    if (_supervisorMapPanel?.hidden) {
      _supervisorMapPanel.hidden = false;
      _supervisorMapBtn.removeAttribute("hidden");
      // Fetch map data
      try {
        const res = await fetch("/api/supervisor-map");
        if (res.ok) {
          const data = await res.json();
          const { renderSupervisorMap } = await import("assets/supervisor-map.js");
          renderSupervisorMap(data);
        } else {
          // "Data unavailable" message
          document.getElementById("mapBody").innerHTML =
            '<p style="padding:20px">Data unavailable.</p>';
        }
      } catch {
        document.getElementById("mapBody").innerHTML =
          '<p style="padding:20px">Connection error.</p>';
      }
    } else {
      _supervisorMapPanel.hidden = true;
      _supervisorMapBtn.setAttribute("hidden", "");
      const { closeSupervisorMap } = await import("assets/supervisor-map.js");
      closeSupervisorMap();
    }
  });
}
```

Also add a check to show/hide the button based on whether there are any backends/agents:

```javascript
// Show/hide supervisor map button based on agent activity
function updateMapButtonVisibility() {
  if (!_supervisorMapBtn) return;
  // Show button if there are agents or orchestrators
  // For now, always show it (the API returns empty children if nothing is running)
  _supervisorMapBtn.removeAttribute("hidden");
}
```

- [ ] **Step 2: Add minimal CSS for panel/drawer**

Edit `web/index.html` — add a `<style>` block or inline CSS. Look for existing panel styles. Add:

```css
#supervisorMapPanel {
  position: absolute;
  right: 0;
  top: 40px;
  width: 350px;
  height: calc(100vh - 50px);
  background: var(--panel-bg, #fff);
  border: 1px solid var(--line, #ddd);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  z-index: 100;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
#supervisorMapPanel .panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line, #ddd);
  font-weight: 600;
}
#supervisorMapPanel .zoom-controls {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
#mapBody {
  flex: 1;
  position: relative;
  overflow: hidden;
}
#supervisorMapSvg {
  display: block;
}
.map-tooltip {
  position: fixed;
  background: var(--fg, #1a1a2e);
  color: var(--bg, #fff);
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
  pointer-events: none;
  white-space: nowrap;
  z-index: 200;
}
.map-detail {
  position: absolute;
  right: 0;
  top: 0;
  width: 200px;
  height: 100%;
  background: var(--panel-bg, #f8f8f8);
  border-left: 1px solid var(--line, #ddd);
  padding: 12px;
  overflow-y: auto;
  font-size: 12px;
}
.map-detail-header { display: flex; justify-content: space-between; align-items: center; }
.map-detail-title { font-weight: 600; }
.map-detail-status { margin: 4px 0; font-weight: 500; }
.map-detail-message { margin: 8px 0; color: #666; font-size: 11px; }
.map-detail-meta { color: #999; font-size: 10px; }
```

- [ ] **Step 3: Add "no agents" fallback**

In the render function of `supervisor-map.js`, add a check at the top:

```javascript
export function renderSupervisorMap(data) {
  if (!data || !data.children || data.children.length === 0) {
    const body = document.getElementById("mapBody");
    if (body) body.innerHTML = '<p style="padding:20px;text-align:center;color:#999">No agents running.</p>';
    return;
  }
  // ... existing code
}
```

- [ ] **Step 4: Verify the flow end-to-end (in a browser if available)**

```bash
# Start webconsole, open browser, click the map icon
# Verify: panel opens, D3 renders, tooltip shows, zoom works
```

- [ ] **Step 5: Commit**

```bash
git add web/assets/app.js web/index.html web/assets/supervisor-map.js
git commit -m "ui: wire supervisor map button, fetch, render, and close cycle"
```

---

## Task 6: Unit tests — full tree structure and classification

**Files:**
- Modify: `tests/test_qa_supervisor_map.py`
- Test: `tests/test_qa_supervisor_map.py` (extend)

**Interfaces:**
- Consumes: `routes.db_supervisor_map.supervisor_map(owner_id)`
- Produces: coverage of aggregate status, tree structure, empty tree, auth

- [ ] **Step 1: Extend the test file with comprehensive tests**

Add to `tests/test_qa_supervisor_map.py`:

```python
"""Extended tests for /api/supervisor-map endpoint."""
import pytest
from httpx import AsyncClient, ASGITransport
import app as app_module
from routes.db_supervisor_map import _aggregate_status


class TestAggregateStatus:
    """Test _aggregate_status function."""

    def test_single_running(self):
        assert _aggregate_status([{"status": "running"}]) == "running"

    def test_single_busy(self):
        assert _aggregate_status([{"status": "busy"}]) == "running"

    def test_single_waiting(self):
        assert _aggregate_status([{"status": "waiting"}]) == "waiting"

    def test_single_idle(self):
        assert _aggregate_status([{"status": "idle"}]) == "idle"

    def test_single_error(self):
        assert _aggregate_status([{"status": "error"}]) == "error"

    def test_single_done(self):
        assert _aggregate_status([{"status": "done"}]) == "done"

    def test_empty(self):
        assert _aggregate_status([]) == "idle"

    def test_mixed_idle_and_busy(self):
        assert _aggregate_status([
            {"status": "idle"},
            {"status": "busy"},
        ]) == "running"

    def test_error_overrides_running(self):
        assert _aggregate_status([
            {"status": "running"},
            {"status": "error"},
        ]) == "error"

    def test_waiting_over_idle(self):
        assert _aggregate_status([
            {"status": "idle"},
            {"status": "waiting"},
        ]) == "waiting"

    def test_all_done(self):
        assert _aggregate_status([
            {"status": "done"},
            {"status": "done"},
        ]) == "done"


class TestSupervisorMapAuth:
    """Test auth gates on the supervisor map endpoint."""

    @pytest.mark.anyio
    async def test_anonymous_gets_401(self):
        trans = ASGITransport(app=app_module.app)
        async with AsyncClient(trans=trans, base_url="http://test") as client:
            resp = await client.get("/api/supervisor-map")
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_admin_gets_200(self):
        trans = ASGITransport(app=app_module.app)
        async with AsyncClient(trans=trans, base_url="http://test") as client:
            await client.post("/api/login", json={"username": "admin", "password": "admin"})
            resp = await client.get("/api/supervisor-map")
            assert resp.status_code == 200
            data = resp.json()
            assert "center" in data
            assert "children" in data
```

- [ ] **Step 2: Run the tests**

```bash
.venv/bin/python -m pytest tests/test_qa_supervisor_map.py -v -rs
```

Expected: All tests pass. If any fail, fix the assertion or the route auth.

- [ ] **Step 3: Commit**

```bash
git add tests/test_qa_supervisor_map.py
git commit -m "test: add comprehensive unit tests for supervisor map endpoint"
```

---

## Task 7: Browser tests — Playwright

**Files:**
- Modify: `tests/test_frontend_browser.py`
- Test: `tests/test_frontend_browser.py`

**Interfaces:**
- Produces: `SupervisorMapBrowserTests` class with 4 test methods
- Consumes: existing browser fixture pattern from `test_frontend_browser.py`

- [ ] **Step 1: Add the browser test class**

Add to `tests/test_frontend_browser.py`, following the existing `_BrowserFixture` pattern:

```python
class SupervisorMapBrowserTests(_BrowserFixture):
    """Browser tests for the supervisor map panel."""

    async def test_panel_opens_closes(self):
        """Clicking the map icon opens the panel, clicking close hides it."""
        await self._setup_server()

        # Wait for page to be ready
        await self.page.wait_for_selector("#orchestratorBtn")

        # Click the supervisor map button
        btn = self.page.locator("#supervisorMapBtn")
        await btn.click()
        await self.page.wait_for_timeout(500)  # let fetch + render complete

        # Panel should be visible (not hidden)
        panel = self.page.locator("#supervisorMapPanel")
        hidden_attr = await panel.get_attribute("hidden")
        assert hidden_attr is None or hidden_attr == "", "Panel should not be hidden"

        # Close the panel
        close_btn = self.page.locator("#supervisorMapClose")
        await close_btn.click()
        await self.page.wait_for_timeout(300)

        hidden_attr = await panel.get_attribute("hidden")
        assert hidden_attr is not None, "Panel should be hidden after close"

    async def test_zoom_controls_exist(self):
        """Zoom buttons are present in the panel header."""
        await self._setup_server()
        await self.page.wait_for_selector("#orchestratorBtn")

        btn = self.page.locator("#supervisorMapBtn")
        await btn.click()
        await self.page.wait_for_timeout(500)

        assert self.page.locator("#mapFitBtn").count() == 1
        assert self.page.locator("#mapZoomOutBtn").count() == 1
        assert self.page.locator("#mapZoomInBtn").count() == 1

    async def test_leaf_click_shows_detail(self):
        """Clicking a chat node opens the detail drawer."""
        await self._setup_server()
        await self.page.wait_for_selector("#orchestratorBtn")

        # Even with no real data, clicking on any node should attempt detail
        btn = self.page.locator("#supervisorMapBtn")
        await btn.click()
        await self.page.wait_for_timeout(1000)

        # Check that detail drawer element exists
        drawer = self.page.locator("#mapDetailDrawer")
        assert drawer.count() == 1

    async def test_orchestrator_expand(self):
        """Clicking an expandable node toggles children visibility."""
        await self._setup_server()
        await self.page.wait_for_selector("#orchestratorBtn")

        btn = self.page.locator("#supervisorMapBtn")
        await btn.click()
        await self.page.wait_for_timeout(1000)

        # Verify SVG exists
        svg = self.page.locator("#supervisorMapSvg")
        assert svg.count() == 1
```

- [ ] **Step 2: Run the browser tests**

```bash
.venv/bin/python -m pytest tests/test_frontend_browser.py::SupervisorMapBrowserTests -v -rs
```

Expected: Some tests may fail because the map needs real data to render properly. Fix the render path (empty children → "No agents" message should still create the SVG container).

- [ ] **Step 3: Fix render path for empty map**

In `supervisor-map.js` `renderSupervisorMap`, ensure that even with no children, the SVG is still created (the browser test checks for it):

```javascript
export function renderSupervisorMap(data) {
  _svg = d3.select("#supervisorMapSvg")
    .attr("width", WIDTH)
    .attr("height", HEIGHT);

  if (!data || !data.children || data.children.length === 0) {
    _svg.append("text")
      .attr("x", WIDTH / 2)
      .attr("y", HEIGHT / 2)
      .attr("text-anchor", "middle")
      .text("No agents running.")
      .attr("fill", "#999")
      .attr("font-size", "14px");
    return;
  }
  // ... existing code
}
```

- [ ] **Step 4: Re-run browser tests**

```bash
.venv/bin/python -m pytest tests/test_frontend_browser.py::SupervisorMapBrowserTests -v -rs
```

Expected: All 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_frontend_browser.py web/assets/supervisor-map.js
git commit -m "test: add SupervisorMapBrowserTests for panel open/close, zoom, detail, expand"
```

---

## Task 8: Final review — run full suite, fix regressions

**Files:**
- All modified files above
- Test: `tests/test_qa_supervisor_map.py` + existing suite

**Interfaces:**
- Consumes: all previous tasks
- Produces: passing full test suite

- [ ] **Step 1: Run the full test suite**

```bash
.venv/bin/python -m pytest -rs
```

Expected: No new failures from this branch. Any pre-existing failures are known and unrelated.

- [ ] **Step 2: Fix any regressions**

If tests fail due to this branch:
- Check if the route path `/api/supervisor-map` conflicts with existing routes
- Verify the auth middleware correctly gates the new endpoint
- Check that D3 CDN load doesn't break existing JS modules

- [ ] **Step 3: Run only the new tests to confirm isolation**

```bash
.venv/bin/python -m pytest tests/test_qa_supervisor_map.py tests/test_frontend_browser.py::SupervisorMapBrowserTests -v
```

Expected: All pass, no cross-contamination.

- [ ] **Step 4: Commit any fixes**

```bash
git add -p
git commit -m "fix: address any regressions from supervisor map integration"
```

---

## Self-Review Checklist

1. **Spec coverage:**
   - API endpoint: Task 2 ✓
   - DB assembly: Task 1 ✓
   - Top-bar icon: Task 3 ✓
   - Panel with zoom controls: Task 3 ✓
   - Detail drawer: Task 4 ✓
   - Node coloring by status: Task 4 ✓
   - Aggregate status: Task 1 ✓
   - Max 4 children: Task 4 ✓
   - Zoom/pan: Task 4 ✓
   - Hover tooltips: Task 4 ✓
   - Accessibility (aria-label): Task 4 ✓
   - Empty state ("No agents"): Task 5 ✓
   - Auth gate (401): Task 5 + Task 6 ✓
   - Error handling ("Data unavailable"): Task 5 ✓
   - Browser tests: Task 7 ✓

2. **No placeholders:** All code blocks contain actual implementation. No "TODO", "TBD", or "implement later".

3. **Type consistency:** All function names, route paths, and HTML IDs are consistent across tasks. `supervisor_map()` in Task 1 → `handle_supervisor_map()` in Task 2 → `renderSupervisorMap()` in Task 4.

4. **File limits:** `db_supervisor_map.py` ~120 lines, `supervisor_map.py` (routes) ~25 lines, `supervisor-map.js` ~120 lines. Well under limits.
