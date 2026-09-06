# Orchestrator Orchestration Design

## Purpose

Extend WebConsole with a orchestrator agent that orchestrates multiple Claude Code turns as a structured plan with subtasks, dependencies, model assignment, and real-time progress tracking. The orchestrator is itself a Claude Code session that reasons about the user's request, breaks it into subtasks, assigns the best model for each, and manages execution — all streamed to the user through an integrated three-panel UI.

## Goals

- Allow the user to issue a high-level request and receive a structured plan with subtasks instead of a single monolithic turn.
- Let the orchestrator choose models per subtask based on complexity and cost (simple questions → free/fast models, complex work → capable models).
- Support explicit subtask dependencies so later tasks wait for earlier ones to complete.
- Stream real-time progress with percentages, status badges, and model labels.
- Let the user interact with the orchestrator at any time to adjust the plan, answer agent questions, or add subtasks.
- Keep one subtask's conversation visible while others run in parallel.
- Maintain full fidelity with the existing runner, proxy, SSE, and SQLite architecture.
- Preserve clean blue light palette, light/dark toggle, and the existing SPA architecture.

## Non-goals

- Replacing Claude Code or the existing runner/proxy architecture.
- Multi-user orchestrator sessions (single user per orchestrator).
- Persistent orchestrator plans across sessions (plans live for the duration of the session).
- Visual-only deployment changes beyond adding a new route and frontend files.

## Architecture

Add a orchestrator module that sits alongside the existing runner. The orchestrator is itself a Claude Code session (a "orchestrator chat") that receives the user's request, reasons, and produces a structured plan. The frontend consumes this plan and drives the multi-panel layout.

```
Browser (vanilla JS SPA)
  ├── Existing chat UI (index.html + app.js + chat-list.js + conversation.js)
  └── Orchestrator UI (orchestrator.html + orchestrator.js + orchestrator.css)  [NEW]

FastAPI (app.py — extended)
  ├── /api/supervisors          [NEW] — CRUD orchestrator orchestrations
  ├── /api/supervisors/{id}/stream [NEW] — SSE for orchestrator reasoning + plan
  ├── /api/supervisors/{id}/tasks [NEW] — Subtask CRUD + status
  ├── /api/supervisors/{id}/tasks/{taskId}/stream [NEW] — SSE per subtask
  └── Existing endpoints (unchanged)

orchestrator.py                 [NEW] — Orchestration logic
  ├── PlanParser — Extract structured plan from orchestrator's reasoning
  ├── TaskGraph — Dependency resolution, topological sort
  ├── ModelRouter — Rule-based model assignment
  ├── ProgressTracker — Percentage calculation, status aggregation
  ├── SupervisorEngine — Coordinate plan → tasks → execution → streaming

db.py (extended)
  ├── supervisors table        [NEW]
  ├── supervisor_tasks table   [NEW]
  ├── supervisor_messages table [NEW]
  └── Existing tables (unchanged)

runner.py (extended)
  ├── run_supervisor_turn()    [NEW] — Run a single turn under orchestrator context
  └── Existing turn flow (unchanged)

transcripts.py (extended)
  ├── orchestrator transcript export [NEW]
  └── Existing export (unchanged)
```

## Orchestrator Workflow

### Phase 1: Request → Plan

1. User sends a request to the orchestrator: `"Deploy the new API changes across staging and production"`
2. Orchestrator creates a new orchestrator session (a chat with `orchestrator=1`).
3. The orchestrator session runs as a streaming turn — the orchestrator (a Claude Code instance) reasons about the request, asks clarifying questions if needed, and produces a structured plan.
4. The plan is parsed from the orchestrator's response into structured data:
   - Subtasks with titles, descriptions, and instructions
   - Dependencies between subtasks
   - Suggested models per subtask
   - Estimated complexity (simple / moderate / complex)
5. The plan is streamed back to the frontend in real-time as the orchestrator thinks.

### Phase 2: Plan → Tasks

6. The frontend receives the structured plan and creates database entries for each subtask.
7. The frontend renders the three-panel layout:
   - **Left panel**: Task tree showing all subtasks with status dots, progress bars, and model chips
   - **Center panel**: Conversation for the selected (or running) subtask
   - **Bottom panel**: Orchestrator chat (user ↔ orchestrator conversation)
   - **Right panel** (toggleable): Detail view for the selected subtask
8. The orchestrator immediately begins executing subtasks that have no blockers.
9. Executing a subtask means running a normal Claude Code turn (proxy or direct) with the subtask's instructions as the prompt, scoped to the subtask's workspace.

### Phase 3: Execution → Completion

10. As each subtask runs, the orchestrator monitors progress and updates the task status.
11. When a subtask completes, dependent subtasks become eligible for execution.
12. The orchestrator tracks overall progress and reports it to the frontend.
13. The user can interact at any time:
    - Click a different subtask to see its conversation
    - Message the orchestrator to adjust the plan
    - Ask a question (orchestrator can answer from its own reasoning or delegate to an agent)
    - Add new subtasks
    - Stop all execution
14. When all subtasks complete, the orchestrator presents a summary.

### Orchestrator Intelligence

The orchestrator uses a system prompt template that instructs it to:

1. Analyze the user's request and break it into logical subtasks.
2. For each subtask, assess complexity and recommend a model.
3. Identify dependencies between subtasks.
4. When agents ask questions, the orchestrator either:
   - Answers directly if the question is simple (e.g., configuration, clarification)
   - Delegates to another agent if it requires investigation
5. Stream structured progress updates (not just text) so the frontend can render badges and progress bars.

The orchestrator's output format is a hybrid: prose reasoning + structured JSON markers that the frontend parser can extract.

## Data Model

### New Database Tables

```sql
CREATE TABLE supervisors (
    id            TEXT PRIMARY KEY,        -- uuid4 hex
    chat_id       TEXT NOT NULL REFERENCES chats(id),  -- links to the orchestrator chat
    title         TEXT NOT NULL,           -- user's original request
    status        TEXT NOT NULL DEFAULT 'planning',
    -- Statuses: planning, running, paused, completed, failed, cancelled
    progress      REAL NOT NULL DEFAULT 0, -- 0..100 aggregate across tasks
    plan_content  TEXT,                    -- JSON: structured plan (tasks, deps, models)
    summary       TEXT,                    -- final summary when completed
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE TABLE supervisor_tasks (
    id            TEXT PRIMARY KEY,        -- uuid4 hex
    supervisor_id TEXT NOT NULL REFERENCES supervisors(id),
    parent_id     TEXT REFERENCES supervisor_tasks(id),  -- nested subtask (optional)
    title         TEXT NOT NULL,
    description   TEXT,
    instructions  TEXT,                    -- instructions sent to the agent
    status        TEXT NOT NULL DEFAULT 'pending',
    -- Statuses: pending, waiting, running, completed, failed, cancelled
    progress      REAL NOT NULL DEFAULT 0, -- 0..100 for this task
    model         TEXT,                    -- assigned model for this task
    complexity    TEXT NOT NULL DEFAULT 'moderate',
    -- Complexity: simple, moderate, complex
    chat_id       TEXT,                    -- links to the agent chat running this task (when running)
    depends_on    TEXT,                    -- JSON array of parent task IDs
    error_message TEXT,
    started_at    TEXT,
    completed_at  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE supervisor_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id TEXT NOT NULL REFERENCES supervisors(id),
    role        TEXT NOT NULL,             -- 'user' | 'assistant' | 'system'
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Reuse the existing `messages` table for agent conversations (task-level chats).
-- The `chat_id` on `supervisor_tasks` links to an existing chat row.
```

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_super ON supervisor_tasks(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_status ON supervisor_tasks(supervisor_id, status);
CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_parent ON supervisor_tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_messages_super ON supervisor_messages(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_messages_order ON supervisor_messages(supervisor_id, id);
```

### Existing Table Extensions

- `chats.orchestrator` column (nullable): links a chat to its parent orchestrator. When a task creates a chat, this is set.
  ```sql
  ALTER TABLE chats ADD COLUMN orchestrator TEXT REFERENCES supervisors(id);
  ```

## API Specification

### Create Orchestrator

```
POST /api/supervisors
{
  "title": "Deploy the new API changes across staging and production",
  "description": "Optional description of the overall goal",
  "model": "claude-sonnet-4-20250514"  // optional: which model to use as the orchestrator itself
}
→ {
  "id": "uuid",
  "chat_id": "uuid",  // the orchestrator chat id
  "title": "Deploy...",
  "status": "planning",
  "progress": 0,
  "created_at": "2026-08-30T..."
}
```

### Stream Orchestrator Planning

```
POST /api/supervisors/{id}/stream
→ SSE stream of the orchestrator's reasoning + structured plan extraction

Events:
{ "type": "start", "supervisor_id": "uuid" }
{ "type": "text", "content": "Let me break this down into steps..." }
{ "type": "plan", "data": { ... structured plan ... } }  // emitted when plan is complete
{ "type": "done" }
```

The structured plan format:

```json
{
  "tasks": [
    {
      "title": "Test API Health",
      "description": "Check all API endpoints are responsive",
      "instructions": "Run health checks on the following endpoints...",
      "model": "ollama",           // assigned model (mapped by ModelRouter)
      "complexity": "simple",
      "depends_on": []
    },
    {
      "title": "Deploy Staging",
      "description": "Deploy to staging environment",
      "instructions": "Pull latest code, run migrations, deploy services...",
      "model": "claude-sonnet-4-20250514",
      "complexity": "moderate",
      "depends_on": ["task-uuid-1"]
    }
  ]
}
```

### List Tasks

```
GET /api/supervisors/{id}/tasks
→ {
  "tasks": [
    {
      "id": "uuid",
      "title": "Test API Health",
      "status": "completed",
      "progress": 100,
      "model": "ollama",
      "complexity": "simple",
      "depends_on": [],
      "parent_id": null,
      "chat_id": "uuid-or-null",
      "error_message": null,
      "started_at": "2026-08-30T...",
      "completed_at": "2026-08-30T..."
    }
  ],
  "supervisor_status": "running",
  "supervisor_progress": 35
}
```

### Stream Subtask

```
POST /api/supervisors/{id}/tasks/{taskId}/stream
→ SSE stream of the agent's work on this subtask

Events:
{ "type": "start", "task_id": "uuid" }
{ "type": "status", "content": "Pulling latest code..." }
{ "type": "progress", "value": 25 }
{ "type": "text", "content": "Step 1: Pulling..." }
{ "type": "done", "status": "completed", "progress": 100 }
{ "type": "error", "content": "Failed: ..." }
```

### Message Orchestrator

```
POST /api/supervisors/{id}/messages
{
  "content": "Can you also restart the worker services during deploy?",
  "chat_id": "optional-chat-id"  // link to an existing chat for context
}
→ { "ok": true, "stream_url": "/api/supervisors/{id}/stream" }
```

The orchestrator processes the message and streams its response. The frontend can pipe this through the SSE endpoint or POST /stream returns the SSE URL directly.

### Get Orchestrator

```
GET /api/supervisors/{id}
→ {
  "orchestrator": { ... metadata ... },
  "tasks": [ ... task list ... ],
  "messages": [ ... orchestrator chat messages ... ]
}
```

### Update Orchestrator

```
PATCH /api/supervisors/{id}
{
  "title": "Updated title",
  "status": "paused"  // or "cancelled"
}
→ { "ok": true }
```

Status transitions:
- `planning` → `running`, `cancelled`
- `running` → `paused`, `completed`, `failed`, `cancelled`
- `paused` → `running`, `cancelled`
- `completed`, `failed` → terminal (no transitions)

### Delete Orchestrator

```
DELETE /api/supervisors/{id}
→ { "ok": true }
```

Cancels all running tasks and removes the orchestrator record.

## Orchestrator Engine (`orchestrator.py`)

### Core Classes

#### `PlanParser`

Extracts structured plans from the orchestrator's free-text reasoning. Uses regex + JSON block detection:

```python
class PlanParser:
    @staticmethod
    def extract_plan(text: str) -> dict | None:
        """Extract structured plan from orchestrator's response text.
        Looks for JSON blocks or marked sections in the output.
        Returns None if no valid plan is found.
        """

    @staticmethod
    def stream_parse(text_so_far: str) -> dict | None:
        """Incrementally parse as text accumulates. Returns partial plan."""
```

#### `ModelRouter`

Applies user-defined rules to assign models to subtasks:

```python
class ModelRouter:
    RULES: list[Rule]  # User-configurable rules

    def assign_model(self, complexity: str, task_type: str) -> str:
        """Return the model name for a given complexity and task type.
        Rules are evaluated in order; first match wins.
        Default: simple→fast/free, moderate→sonnet, complex→opus
        """
```

Rules are stored in settings as JSON and include:

```json
{
  "rules": [
    { "match": "complexity=simple", "model": "fast-model" },
    { "match": "complexity=moderate", "model": "sonnet-model" },
    { "match": "complexity=complex", "model": "opus-model" },
    { "match": "type=test", "model": "fast-model" },
    { "match": "type=deploy", "model": "sonnet-model" }
  ],
  "fallback_model": "sonnet-model"
}
```

#### `TaskGraph`

Manages the dependency graph of subtasks:

```python
class TaskGraph:
    def __init__(self, tasks: list[dict]): ...

    def get_ready_tasks(self) -> list[str]:
        """Return task IDs whose dependencies are all completed."""

    def get_dependents(self, task_id: str) -> list[str]:
        """Return tasks that depend on this task."""

    def add_task(self, task_id: str, depends_on: list[str]): ...

    def remove_task(self, task_id: str): ...

    def is_dag(self) -> bool:
        """Validate no circular dependencies."""

    def topological_sort(self) -> list[str]:
        """Return tasks in execution order."""
```

#### `ProgressTracker`

Calculates aggregate progress:

```python
class ProgressTracker:
    def __init__(self, total_tasks: int): ...

    def update_task_progress(self, task_id: str, progress: float): ...

    def task_completed(self, task_id: str): ...

    def task_failed(self, task_id: str): ...

    def aggregate_progress(self) -> float:
        """Weighted average: weight by task complexity.
        simple=1, moderate=2, complex=3.
        Returns 0..100.
        """

    def status_summary(self) -> dict:
        """Returns counts by status: {running: 2, pending: 1, completed: 3}"""
```

#### `SupervisorEngine`

Coordinates the full orchestration:

```python
class SupervisorEngine:
    def __init__(self, supervisor_id: str, db_conn, runner): ...

    async def plan(self, user_request: str, model: str) -> str:
        """Run the orchestrator turn to generate the plan.
        Returns parsed plan dict.
        """

    async def execute_tasks(self, plan: dict):
        """Execute all tasks respecting dependencies.
        Runs ready tasks in parallel (bounded by MAX_CONCURRENT).
        Streams progress updates via event generator.
        """

    async def handle_user_message(self, supervisor_id: str, content: str) -> AsyncGenerator:
        """Process a user message to the orchestrator.
        Streams the orchestrator's response as SSE events.
        """

    async def cancel(self, supervisor_id: str): ...

    async def pause(self, supervisor_id: str): ...

    async def resume(self, supervisor_id: str): ...
```

### Concurrency

The orchestrator respects the existing concurrency model:

- The orchestrator itself uses one concurrency slot (the `MAX_CONCURRENT` semaphore).
- Each subtask agent also uses one concurrency slot.
- To prevent exhaustion, the orchestrator limits parallel subtask execution to `min(MAX_CONCURRENT - 1, 3)`, leaving at least one slot available for other operations.
- Tasks that depend on each other execute sequentially regardless.

## SSE Event Protocol

### Orchestrator Planning Stream

```
data: {"type": "start", "supervisor_id": "uuid"}

data: {"type": "text", "content": "I'll break this down into steps..."}
data: {"type": "text", "content": "First, let's test the API health..."}

data: {"type": "plan", "data": {"tasks": [...], "summary": "4 subtasks planned"}}

data: {"type": "tasks_created", "task_ids": ["uuid1", "uuid2", "uuid3", "uuid4"]}
data: {"type": "tasks_started", "task_ids": ["uuid1"]}  // no dependencies
data: {"type": "progress", "supervisor_id": "uuid", "value": 25}

data: {"type": "done"}
```

### Subtask Stream

```
data: {"type": "start", "task_id": "uuid", "task_title": "Deploy Staging"}

data: {"type": "status", "content": "Pulling latest code..."}
data: {"type": "progress", "task_id": "uuid", "value": 25}
data: {"type": "text", "content": "Step 1: Pulling latest code..."}

data: {"type": "status", "content": "Running database migrations..."}
data: {"type": "progress", "task_id": "uuid", "value": 50}
data: {"type": "text", "content": "Step 2: Running migrations..."}

data: {"type": "progress", "task_id": "uuid", "value": 75}
data: {"type": "text", "content": "Step 3: Deploying services..."}

data: {"type": "done", "status": "completed", "progress": 100}
-- OR --
data: {"type": "error", "content": "Migration failed: table already exists"}
```

### Task Status Change Events (broadcast to all connected subtask streams)

```
data: {"type": "task_status", "task_id": "uuid", "status": "completed", "progress": 100}
data: {"type": "task_ready", "task_id": "uuid"}  // dependencies satisfied
data: {"type": "task_failed", "task_id": "uuid", "error": "message"}
data: {"type": "supervisor_progress", "value": 50}
```

## Frontend Architecture

### New Files

```
web/
├── index.html                        # Existing SPA
├── orchestrator.html                   # NEW: Orchestrator UI shell
├── orchestrator.css                    # NEW: Orchestrator layout styles
├── orchestrator.js                     # NEW: Orchestrator orchestration logic
├── assets/
│   ├── orchestrator-mockup.html        # Existing: design mockup
│   └── orchestrator-task-tree.js       # NEW: Task tree component
└── ...                               # Existing files (unchanged)
```

### Routes

Add `/orchestrator` and `/orchestrator/{path}` to the existing `app.py` static file serving, served as `orchestrator.html` (SPA pattern).

### Layout

The three-panel layout (Option C) is implemented as a flexbox layout with CSS custom properties for resizing:

```
┌─────────────────────────────────────────────────────┐
│  Orchestrator — Three-Panel Split                    │
├──────┬──────────────────────────────┬──────────────┤
│ Task │  Conversation (Center Panel) │ Detail (R)   │
│ Tree │  (resizable)                 │ (toggleable) │
│ (L)  │                              │              │
│      │                              │              │
│      │                              │              │
├──────┴──────────────────────────────┴──────────────┤
│  Orchestrator Chat (bottom panel, resizable)         │
├────────────────────────────────────────────────────┤
│  Status bar: 3 running · 1 pending · 2 done · 35%  │
└────────────────────────────────────────────────────┘
```

### State Management

The orchestrator UI maintains a single state object:

```javascript
const state = {
  supervisorId: null,
  activeChatId: null,       // which chat/task is currently shown
  orchestrator: null,         // orchestrator metadata
  tasks: new Map(),         // task_id → task data
  messages: [],             // orchestrator messages (recent)
  streams: new Map(),       // task_id → AbortController
  status: 'planning',       // current orchestrator status
  progress: 0,              // overall progress 0..100
  connectedAgents: new Map(), // task_id → {streamUrl, abortController}
};
```

### Task Tree Component

Renders the hierarchical task list with:

- Indentation for parent-child relationships
- Status dots (colored circles)
- Progress bars
- Model chips
- Dependency lines (showing what a task depends on)
- Click to open in center panel
- Click to expand/collapse parent tasks
- Auto-scroll to running tasks

```
┌─ 📋 Task Tree ───────────────────────┐
│ 🟩 ① Test API Health      100% [OLL]│
│ 🟦 ② Deploy Staging       60% [SNET]│ ← selected
│   └── depends: ① ✅                │
│ 🟦 ③ Deploy Production      30% [S]│
│   └── depends: ① ✅                │
│ 🟨 ④ Smoke Tests            0% [OLL]│
│   └── depends: ② 🔄 ③ 🔄         │
└──────────────────────────────────────┘
```

### Resizable Panels

Panels are resizable via:

1. **Drag handles**: Horizontal dividers between left/center and center/bottom panels (existing resizer logic).
2. **Size buttons**: +/- buttons in each panel header that increment/decrement size by 20px.
3. **Maximize**: ⛶ button in each panel header that expands that panel to full viewport, hiding others.

Implementation:
- Use JS-tracked dimension variables (PANEL_WIDTHS, PANEL_HEIGHTS) with min/max clamping.
- Apply `min-width`/`max-width` / `min-height`/`max-height` pairs to lock panel sizes (flexbox compatible).
- Maximize mode adds `body.max-{panel}` class which hides non-active panels with `display: none`.
- Restore removes the class and returns to normal layout.

### Conversation Panel

The center panel shows the selected subtask's conversation:

- Messages from the task's chat (fetched from existing `/api/chats/{id}` endpoint)
- Real-time updates via SSE stream to the task's stream endpoint
- Progress indicator header showing task status, model, and percentage
- Status bar with task metadata

### Detail Panel (Right)

Toggleable panel showing subtask details:

- Current status with progress bar
- Assigned model chip
- Agent chat history summary
- Output snippet (latest output from the agent)
- Dependencies (with status of each)
- Context attached (results from dependent tasks)
- Resources (elapsed time, tokens, turns)

### Orchestrator Chat (Bottom)

The bottom panel is the direct conversation with the orchestrator:

- User messages and orchestrator responses
- System messages showing plan changes
- Input field with Ctrl+Enter to send
- Auto-scroll to latest
- Shows overall plan status as system messages

### Status Bar

Beneath the center panel (above the bottom chat):

```
Subtask: Deploy Staging  |  Model: Claude Sonnet  |  Progress: 60%
⏱ 2m 14s  |  📊 3 steps  |  ✅ ①  🔄 ②  🔄 ③  ⏳ ④
```

## Orchestrator System Prompt

The orchestrator is instructed via a system prompt template:

```
You are a deployment orchestrator orchestrating a multi-step plan.
Your job is to break user requests into subtasks, assign models,
track dependencies, and manage execution.

OUTPUT FORMAT:
1. First, provide brief reasoning about the plan.
2. Then, output a structured plan block enclosed in
   [PLAN_START] and [PLAN_END] markers.

Inside the plan block, use this format:
TASK: <title> | <complexity> | <model_hint>
DESC: <description>
INSTRUCTIONS: <instructions for the agent>
DEPENDS: <task_number or "none">
---

After the plan block, explain your reasoning.

STATUS UPDATES:
When a subtask changes, report:
[STATUS] <task_number> <status> <progress>%
Examples:
[STATUS] 1 completed 100%
[STATUS] 2 running 45%
[STATUS] 3 waiting

When responding to user messages, be helpful and concise.
If asked about a subtask's progress, check the latest status.
If asked a question that requires investigation, delegate
to an appropriate agent subtask.

MODEL ASSIGNMENT RULES:
- Tests and health checks: use fast/free models
- Simple analysis: use fast models
- Code changes and deployments: use capable models
- Complex architecture decisions: use the best model
```

## Database Migrations

Incremental migration applied at startup:

```python
# In db.py migration block:

# Add supervisors table
CREATE TABLE IF NOT EXISTS supervisors (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planning',
    progress REAL NOT NULL DEFAULT 0,
    plan_content TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

# Add supervisor_tasks table
CREATE TABLE IF NOT EXISTS supervisor_tasks (
    id TEXT PRIMARY KEY,
    supervisor_id TEXT NOT NULL REFERENCES supervisors(id),
    parent_id TEXT REFERENCES supervisor_tasks(id),
    title TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL NOT NULL DEFAULT 0,
    model TEXT,
    complexity TEXT NOT NULL DEFAULT 'moderate',
    chat_id TEXT,
    depends_on TEXT,
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

# Add supervisor_messages table
CREATE TABLE IF NOT EXISTS supervisor_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id TEXT NOT NULL REFERENCES supervisors(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Extend chats table
ALTER TABLE chats ADD COLUMN orchestrator TEXT REFERENCES supervisors(id);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_super ON supervisor_tasks(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_status ON supervisor_tasks(supervisor_id, status);
CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_parent ON supervisor_tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_messages_super ON supervisor_messages(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_messages_order ON supervisor_messages(supervisor_id, id);
```

## Integration with Existing Architecture

### Runner Integration

The existing `runner.py` handles subtask execution. A subtask is simply a turn with:
- A custom system prompt prefix that identifies it as a supervised subtask
- The subtask's instructions as the user prompt
- The subtask's assigned model
- The task's `work_dir` as the working directory

No changes to the runner's core flow are needed — it already supports model selection, prompt passing, and SSE streaming.

### Chat Integration

Each subtask that requires interaction creates a chat (or reuses one). The `chat_id` on `supervisor_tasks` links to this chat. The frontend fetches messages from the existing `/api/chats/{id}` endpoint.

### SSE Integration

The orchestrator's SSE endpoints follow the same pattern as the existing stream endpoint:
- `StreamingResponse` with an async generator
- Events yield as JSON strings prefixed with `data: `
- Events are written to the log and to supervisor_messages/supervisor_tasks tables on completion

### Auth Integration

Orchestrator endpoints require authentication (same auth middleware). The user must own the orchestrator (linked to their chat). All orchestrator operations are single-user (no multi-user isolation needed).

### Concurrency Integration

The orchestrator shares the existing concurrency semaphore with regular chats:
- Orchestrator planning turn: 1 slot
- Subtask execution: 1 slot per subtask (up to MAX_CONCURRENT - 1, leaving 1 for other operations)
- Total parallel turns never exceeds MAX_CONCURRENT

## Testing

### Backend

- Plan parsing: valid JSON blocks, partial plans, malformed input
- Model routing: rule matching, fallback model, rule validation
- Task graph: dependency resolution, cycle detection, topological sort, dynamic add/remove
- Progress calculation: weighted averages, partial completion, failure impact
- Orchestrator engine: full lifecycle (plan → tasks → execute → complete), pause/resume, cancel, failure recovery
- Dependencies: task B waits for task A, task C waits for A+B, parallel execution of independent tasks
- Model assignment: complexity-based, custom rules, fallback
- SSE streams: event ordering, completion detection, error handling
- Auth: orchestrator isolation by owner_id
- DB migration: idempotent, backward compatible

### Frontend

- Task tree rendering: hierarchy, status, progress, model chips, dependencies
- Panel resizing: drag handles, +/- buttons, minimize/maximize constraints
- Task selection: center panel updates, detail panel populates
- SSE streams: connection, reconnection, error display, completion
- Real-time updates: progress bars animate, status dots change
- Orchestrator chat: send message, receive response, plan display
- Error states: failed tasks, network errors, orchestrator failure
- Keyboard navigation: tab, arrow keys, enter to select
- Responsive layout: works at various viewport widths

### Acceptance

1. Create a orchestrator with a multi-step request.
2. Watch the plan stream in and render in the task tree.
3. See independent tasks start in parallel.
4. Click on different tasks to see their conversations.
5. Send a message to the orchestrator to add a subtask.
6. Verify progress percentages update in real-time.
7. Pause and resume a running orchestrator.
8. Delete a orchestrator and verify tasks are cancelled.
9. Export the orchestrator transcript as Markdown.
10. Verify the mockup matches the actual implementation.

## UI Design Notes

The mockup at `web/assets/orchestrator-mockup.html` covers the visual design (served at `https://kali-2.tail850c40.ts.net/assets/orchestrator-mockup.html`). The implementation should match this mockup with:

- Clean blue light palette (dark blue sidebar, light center, accent blue for active elements)
- Compact typography (10px labels, 9px details, 10px body)
- Status dot system (green=completed, blue=running, amber=pending, red=failed)
- Model chips (OLLAMA=purple, CLAUDE=blue)
- Progress bars with gradient fill
- Resizable panels with drag handles and +/- buttons
- Maximize buttons (⛶) on every panel
- Toggleable right detail panel
- Three-panel layout: task tree (left), conversation (center), orchestrator chat (bottom)
- Status bar between center and bottom panels

## Deployment

Add `/orchestrator` route in `app.py` to serve `orchestrator.html`:

```python
app.add_route("/orchestrator", handle_supervisor_page, methods=["GET"])
app.add_route("/orchestrator/{path:path}", handle_supervisor_page, methods=["GET"])
```

New static files served via existing `StaticFiles` mount at `/assets/`:
- `orchestrator.css` → `/assets/orchestrator.css`
- `orchestrator.js` → `/assets/orchestrator.js`
- `orchestrator-task-tree.js` → `/assets/orchestrator-task-tree.js`

## Implementation Phases

### Phase 1: Data Model & API
- Add orchestrator tables to `db.py` with migration
- CRUD endpoints for supervisors, tasks, and messages
- PATCH for status transitions
- GET for listing and detail views

### Phase 2: Orchestrator Engine
- Implement `PlanParser`, `ModelRouter`, `TaskGraph`, `ProgressTracker`
- Implement `SupervisorEngine` planning phase
- Stream planning events via SSE

### Phase 3: Task Execution
- Integrate with existing runner for subtask execution
- Dependency resolution and parallel execution
- Task status updates via SSE
- Progress tracking and aggregation

### Phase 4: Frontend
- `orchestrator.html` shell with three-panel layout
- `orchestrator.css` styling matching the mockup
- `orchestrator.js` state management and API integration
- `orchestrator-task-tree.js` tree component
- Panel resizing (drag, +/-, maximize)

### Phase 5: Integration & Polish
- Connect all pieces end-to-end
- Real-time streaming between all panels
- Error handling and retry logic
- Export orchestrator transcripts
- Acceptance testing