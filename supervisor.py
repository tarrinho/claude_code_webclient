# supervisor.py -- AI supervisor orchestration engine for WebConsole 0.9.0
#
# Provides the PlanParser, ModelRouter, TaskGraph, ProgressTracker,
# and SupervisorEngine that coordinate multi-agent task decomposition,
# model assignment, dependency tracking, and streaming progress.

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

_log = logging.getLogger("wc.supervisor")

# -- Model routing rules --------------------------------------------------
DEFAULT_RULES: dict[str, Any] = {
    "rules": [
        {"pattern": ".*", "model": config.ANTHROPIC_MODEL},
    ]
}

COMPLEXITY_PATTERNS: dict[str, int] = {
    "architect|design.*system|create.*framework": 4,
    "implement.*multiple|coordinate.*agent|orchestrate": 5,
    "debug.*complex|trace.*error.*chain|performance.*bottleneck": 4,
    "write.*test.*suite|integration.*test|e2e.*test": 3,
    "analyze.*code.*review|refactor.*large|migrate.*database": 4,
    "write.*doc.*umentation|create.*tutorial|explain.*concept": 2,
    "research.*api.*document|find.*replacement|evaluate.*option": 3,
    "read.*file|list.*directory|grep.*pattern|summarize.*log": 1,
    "simple|small|quick|minor|fix.*typo": 1,
}

# -- Result cleaning -------------------------------------------------------

# Claude's text output sometimes includes tool call descriptions such as
# ``Bash(check the error)`` or ``Read(app.py)`` as part of its prose
# explanation.  Strip entire lines that look like tool calls so the chat
# only shows the actual answer.
#
# The "(" must follow the tool name immediately.  Allowing anything between
# the two deleted ordinary prose -- "Read the config file (see below)" and
# "Identified the bug (line 42)" both opened with a tool name and went on to
# contain a bracket, so the line they belonged to vanished from the chat.
# These are the names the CLI prints, not shell command names: a leaked call
# reads "Bash(...)", never "lsblk(...)".
_TOOL_CALL_LINE_RE = re.compile(
    r"^(?:"
    r"Bash|BashOutput|Read|Write|Edit|NotebookEdit|"
    r"Glob|Grep|Task|Agent|Skill|SlashCommand|"
    r"WebFetch|WebSearch|TodoWrite|KillShell|ExitPlanMode"
    r")\([^)]*\)\s*$"
)


def clean_result(text: str) -> str:
    """Remove tool-call lines, collapsing blank lines left behind."""
    # Line by line, and only when the line is nothing but the call. A line
    # that carries prose alongside it ("I ran Bash(x) and it failed") is the
    # answer, not machinery, so it stays.
    lines = text.split("\n")
    kept = [ln for ln in lines if not _TOOL_CALL_LINE_RE.match(ln.strip())]
    cleaned = "\n".join(kept)
    # Collapse more than 2 consecutive newlines into 2.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# -- Plan parsing ----------------------------------------------------------

# Tolerates "<<PLAN" alone and "<<PLAN>>" on one line. The system prompt
# showed the first and the request text asked for the second, so a model
# following either instruction produced a block this could not find --
# and an unparsable plan used to be reported as a completed run.
_PLAN_START_RE = re.compile(r"(?i)^\s*<<PLAN(?:\s*>>)?\s*$", re.MULTILINE)
_PLAN_END_RE = re.compile(r"(?i)^>>\s*$", re.MULTILINE)
# "Task 1: Brief title - Description", which is the format the prompt asks for.
# The number is consumed, not captured: the previous pattern made `(.+?)` stop at
# the first ":" and so put "1" in the title and the whole remainder in the
# description, which is what the task list displayed -- rows named "1" and "2".
# The title/description split is the first " - ", and a line with no dash is all
# title rather than being dropped.
_TASK_MARKER_RE = re.compile(
    r"(?i)^\s*(?:task|#)\s*\d+\s*[.:)]?\s+(.+?)(?:\s+-\s+(.*))?$",
    re.MULTILINE,
)
_DEPENDENCY_RE = re.compile(r"\{#([\w#\s,]+?)\}", re.IGNORECASE)
_MODEL_RE = re.compile(r"\[:(\S+)\]", re.IGNORECASE)
# Words that only ever appear as placeholders in the planner's own
# instructions. A model that copies the example emits them verbatim, and
# they would otherwise be passed to the CLI as a model id.
_MODEL_PLACEHOLDERS = frozenset({"model", "model-name", "model_id",
                                 "model-id", "modelname"})


@dataclass
class ParsedTask:
    """One task extracted from a supervisor plan."""
    id: str
    title: str
    description: str = ""
    model: str | None = None
    parent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    complexity: int = 1


class PlanParser:
    """Extract structured tasks from free-text plan output."""

    @staticmethod
    def parse(text: str) -> list[ParsedTask]:
        """Parse plan text into a list of ParsedTask objects.

        Expected format (instructed via system prompt):

            <<PLAN
            Task 1: Brief title - Description text {#task_id} [:model_id]
            Task 2: Brief title - Description text {#task_id} [:model_id]
            >>
        """
        tasks: list[ParsedTask] = []

        # Extract plan block between <<PLAN and >>
        plan_block = text
        start_m = _PLAN_START_RE.search(text)
        end_m = _PLAN_END_RE.search(text)
        if start_m and end_m:
            plan_block = text[start_m.end():end_m.start()]

        # ── Pass 1: extract raw fields ──────────────────────────────────
        raw: list[tuple[str, str, list[str], str | None]] = []
        for match in _TASK_MARKER_RE.finditer(plan_block):
            title = match.group(1).strip()
            # Optional: a task line with no " - " is all title. Dropping such a
            # line, or crashing on it, would lose a task the planner meant.
            description = (match.group(2) or "").strip()
            # {#task1, #task2} → ["task1", "task2"]
            raw_refs: list[str] = []
            for group in _DEPENDENCY_RE.findall(description):
                for ref in group.replace("#", "").split(","):
                    ref = ref.strip()
                    if ref:
                        raw_refs.append(ref)

            # Also find single-ref patterns like {#task3} that appear
            # outside a comma group (the regex above may not catch them
            # if they appear in other contexts).
            raw_refs = list(dict.fromkeys(raw_refs))  # unique, order-preserving
            model_match = _MODEL_RE.search(description)
            if model_match and model_match.group(1).lower() in _MODEL_PLACEHOLDERS:
                # Copied out of the instructions rather than chosen. Treated as
                # absent, which lets the backend's own model be used instead of
                # a name no gateway serves.
                model_match = None
            model = model_match.group(1) if model_match else None
            raw.append((title, description, raw_refs, model))

        # ── Pass 2: assign IDs (handling duplicates) and build ref→id map ─
        ref_to_id: dict[str, str] = {}
        id_counter: int = 0
        raw_ids: list[str] = []
        seen_titles: dict[str, str] = {}

        for id_counter, (title, description, self_refs, model) in enumerate(raw, start=1):
            slug = PlanParser._slug(title)
            if slug in seen_titles:
                task_id = f"t{id_counter:03d}"
            else:
                seen_titles[slug] = ""
                task_id = f"t{id_counter:03d}"
            raw_ids.append(task_id)

        # Now map user refs (e.g. "task1" from {#task1}) to our IDs.
        # Task N → ref "taskN".
        for idx, task_id in enumerate(raw_ids):
            ref_key = f"task{idx+1}"
            ref_to_id[ref_key] = task_id

        # ── Pass 3: build ParsedTask with resolved deps ─────────────────
        for i, (title, description, self_refs, model) in enumerate(raw):
            # Resolve refs: {#task1} -> "task1" -> "t001" via ref_to_id
            resolved: list[str] = []
            for ref in self_refs:
                cleaned = ref.lower().replace("task", "")
                mapped = ref_to_id.get(ref, ref_to_id.get(cleaned, ref))
                # Exclude self-references and refs to tasks that don't exist
                if mapped != raw_ids[i] and mapped in ref_to_id.values():
                    resolved.append(mapped)

            task = ParsedTask(
                id=raw_ids[i],
                title=title,
                description=description,
                model=model,
                depends_on=resolved,
            )
            tasks.append(task)
            seen_titles[PlanParser._slug(title)] = task.id

        # Fourth pass: compute complexity scores when model not overridden
        for task in tasks:
            if not task.model:
                task.complexity = PlanParser._score_complexity(
                    task.title + " " + task.description
                )
            else:
                task.complexity = 3  # user picked a model

        return tasks

    @staticmethod
    def _slug(title: str) -> str:
        slug = re.sub(r"[^a-z0-9-]", "-", title.lower())
        slug = "-".join(p for p in slug.split("-") if p)
        return slug[:30] or "task"

    @staticmethod
    def _score_complexity(text: str) -> int:
        text_lower = text.lower()
        score = 1
        for pattern, points in sorted(
            COMPLEXITY_PATTERNS.items(), key=lambda x: -len(x[0])
        ):
            if re.search(pattern, text_lower):
                score = max(score, points)
        return score


# -- Model routing -----------------------------------------------------------

_MODEL_NAME_RE = re.compile(r"^[\w][\w\-\.]*(?:\/[\w][\w\-\.]*)*$")


class ModelRouter:
    """Route tasks to models based on configurable rules."""

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self.rules: list[dict[str, str]] = (
            rules.get("rules", []) if rules else []
        )

    def assign_model(
        self, task_title: str, task_desc: str, complexity: int = 1
    ) -> str:
        """Pick the best model for a task using rule matching."""
        combined = (task_title + " " + task_desc).lower()

        for rule in self.rules:
            pattern = rule.get("pattern", "")
            model = rule.get("model", "")
            if pattern and model:
                try:
                    if re.search(pattern, combined):
                        return model
                except re.error:
                    _log.warning(
                        "Invalid regex in model routing rule: %s", pattern
                    )

        # Fallback: complexity-based
        if complexity >= 4:
            return config.ANTHROPIC_MODEL
        return config.ANTHROPIC_MODEL

    @staticmethod
    def validate_model(model: str) -> bool:
        """Check if a model id looks valid."""
        return bool(_MODEL_NAME_RE.match(model))

    def default_config(self) -> dict[str, Any]:
        return DEFAULT_RULES.copy()


# -- Task graph --------------------------------------------------------------

@dataclass
class TaskNode:
    """A task in the supervisor graph with runtime state."""
    id: str
    title: str
    description: str
    status: str = "pending"  # pending|ready|running|done|failed|blocked
    model: str | None = None
    result: str | None = None
    progress_pct: float = 0.0
    parent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class TaskGraph:
    """Manages the DAG of tasks and dependency resolution."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskNode] = {}

    def add_task(self, task: TaskNode) -> None:
        self.tasks[task.id] = task

    def get_ready_tasks(self) -> list[str]:
        """Return ids of tasks whose dependencies are all done."""
        ready: list[str] = []
        for tid, task in self.tasks.items():
            if task.status == "ready":
                continue
            # "blocked" belongs here: it is a terminal state, reached because a
            # dependency failed. Without it a blocked task with no dependencies
            # of its own falls through to the `if not deps` branch below and is
            # flipped back to "ready" and returned as runnable.
            if task.status in ("done", "failed", "running", "blocked"):
                continue
            deps = [d for d in task.depends_on if d in self.tasks]
            if not deps:
                task.status = "ready"
                ready.append(tid)
            else:
                dep_statuses = [self.tasks[d].status for d in deps]
                if all(s == "done" for s in dep_statuses):
                    task.status = "ready"
                    ready.append(tid)
                elif any(s == "failed" for s in dep_statuses):
                    task.status = "blocked"
        return ready

    def get_task(self, task_id: str) -> TaskNode | None:
        return self.tasks.get(task_id)

    def update_status(self, task_id: str, status: str) -> bool:
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            return True
        return False

    def update_progress(self, task_id: str, pct: float) -> bool:
        if task_id in self.tasks:
            self.tasks[task_id].progress_pct = pct
            return True
        return False

    def update_result(self, task_id: str, result: str) -> bool:
        if task_id in self.tasks:
            self.tasks[task_id].result = result
            return True
        return False

    def all_done(self) -> bool:
        """Whether nothing can make further progress.

        "failed" is a terminal state and has to be counted here. It was not,
        and the scheduler loops on `not all_done()`: a single failed task made
        that condition permanently true, so the loop spun at 0.5s for ever with
        nothing runnable and the supervisor never reached a final state.
        Finishing unsuccessfully is still finishing -- whether the run failed
        is what any_failed() answers, and the caller asks it separately.
        """
        if not self.tasks:
            return True
        return all(
            t.status in ("done", "blocked", "failed") for t in self.tasks.values()
        )

    def any_failed(self) -> bool:
        return any(t.status == "failed" for t in self.tasks.values())

    def overall_progress(self) -> float:
        if not self.tasks:
            return 0.0
        total = sum(t.progress_pct for t in self.tasks.values())
        return round(total / len(self.tasks), 1)

    def to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status,
                "model": t.model,
                "result": t.result,
                "progress_pct": t.progress_pct,
                "parent_id": t.parent_id,
                "depends_on": t.depends_on,
            }
            for t in self.tasks.values()
        ]


# -- Progress tracking -------------------------------------------------------

@dataclass
class ProgressEvent:
    """Emitting progress from a running subtask."""
    event_type: str  # task_start, task_progress, task_done, task_error, plan
    task_id: str | None
    data: dict[str, Any] = field(default_factory=dict)


class ProgressTracker:
    """Aggregates progress events and emits summary progress."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []
        self._task_progress: dict[str, float] = {}

    def record(self, event: ProgressEvent) -> None:
        self.events.append(event)
        if event.event_type == "task_progress" and event.task_id:
            self._task_progress[event.task_id] = event.data.get("pct", 0)

    def supervisor_progress(self, graph: TaskGraph) -> float:
        return graph.overall_progress()

    def recent_events(self, last: int = 10) -> list[dict[str, Any]]:
        return [
            {
                "type": e.event_type,
                "task_id": e.task_id,
                "data": e.data,
            }
            for e in self.events[-last:]
        ]


# -- Supervisor engine -------------------------------------------------------

SUPERVISOR_SYSTEM_PROMPT = (
    "You are a Supervisor Agent. Your ONLY job is to output a PLAN block "
    "with numbered tasks and nothing else.\n\n"
    "RULE — output ONLY this block, verbatim:\n"
    "  <<PLAN\n"
    "  Task 1: {title} - {description}\n"
    "  Task 2: {title} - {description}\n"
    "  >>\n\n"
    "NO greetings, NO explanations, NO conversational text.\n"
    "NO markdown formatting inside the block.\n"
    "NO blank lines inside the block.\n\n"
    "DEPENDENCIES — append {#taskId} to any task that must wait:\n"
    "  Task 2: Verify result - Check file content {#task1}\n\n"
    "MODEL SELECTION — append [:model_id] to any task:\n"
    "  Task 3: Write code - Implement the feature [:opus]\n\n"
    "RULE — if the user prompt is very short (e.g. \"hello\", \"check this\"),\n"
    "create a simple plan with 1-2 tasks that fit the request.\n"
    "Never ask the user for clarification — just produce the plan.\n\n"
    "RULE — one task per line. No tables, no bullet points, no explanations."
)


class SupervisorEngine:
    """Main engine: plan parsing, model routing, task graph, and execution."""

    def __init__(self, supervisor_id: str, owner_id: str) -> None:
        self.supervisor_id = supervisor_id
        self.owner_id = owner_id
        self.graph = TaskGraph()
        self.tracker = ProgressTracker()
        self.config: dict[str, Any] = {}
        self.router = ModelRouter()
        self._running = False
        self._paused = False
        self._pre_pause_status: str | None = None  # status to restore on resume
        self._planner_chat_id: str | None = None  # synthetic chat for planning turn
        self._resume_event: asyncio.Event = asyncio.Event()
        # Strong references to background tasks. The event loop keeps only weak
        # ones, so a task nobody holds can be collected mid-run: the work stops
        # with nothing raised and nothing logged. See spawn().
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        """Start background work, keep a reference, and report a failure.

        Two problems with a bare ``asyncio.create_task``: the task can be
        garbage-collected before it finishes, and an exception inside it is
        reported only when the task object is collected, as asyncio's "Task
        exception was never retrieved" -- which reaches the asyncio logger
        rather than anything an operator reads.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._report_task_failure)
        return task

    async def _persist_progress(self) -> None:
        """Write the graph's overall progress onto the supervisor row.

        Individual task rows carried their own progress, but the supervisor's
        did not, so a run whose every task was "done" still reported 0% -- and
        the progress bar is the one thing a supervisor page is watched for.
        """
        import db

        try:
            await db.supervisor_update(
                self.supervisor_id, self.owner_id,
                progress_pct=self.graph.overall_progress(),
            )
        except Exception:  # noqa: BLE001 -- reporting must not stop the run
            _log.exception("could not persist progress for %s", self.supervisor_id)

    async def _set_status(self, status: str) -> None:
        """Record the run's overall status where the UI actually reads it.

        This file updated a graph node called "supervisor" in seven places, and
        no such node is ever created: the only add_task() call inserts parsed
        plan tasks, whose ids come from _slug(title). Every one of those calls
        was therefore a no-op returning False, and nothing here ever wrote to
        the database at all. The persisted status was set to "planning" when
        the prompt was sent and never moved again -- so a run that finished,
        or failed, went on reporting that it was still planning.

        The graph update is kept because it is correct if such a node is ever
        added; the database write is the part the interface can see.
        """
        self.graph.update_status("supervisor", status)
        try:
            import db  # local import: db imports this module at load time
            await db.supervisor_update(self.supervisor_id, self.owner_id,
                                       status=status)
        except Exception:  # noqa: BLE001 -- a status write must not end the run
            _log.exception(
                "supervisor_status_not_persisted supervisor_id=%s status=%s",
                self.supervisor_id, status,
            )

    def _report_task_failure(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return          # asked to stop; not a fault
        exc = task.exception()
        if exc is not None:
            _log.error(
                "supervisor_task_failed supervisor_id=%s",
                self.supervisor_id, exc_info=exc,
            )

    async def start_from_user_prompt(self, user_prompt: str) -> dict[str, Any]:
        """Launch the supervisor and begin planning.

        Starts a background asyncio task that asks the LLM to produce a plan.
        When the plan arrives, PlanParser extracts tasks and the engine
        transitions from 'planning' to 'running' and begins execution.
        """
        self._running = True
        self.spawn(self._run_planner_turn(user_prompt))
        return {
            "supervisor_id": self.supervisor_id,
            "status": "planning",
            "user_prompt": user_prompt,
        }

    @staticmethod
    def _build_plan_prompt(user_prompt: str) -> str:
        # SUPERVISOR_SYSTEM_PROMPT is the hard directive — must come first so
        # the model treats it as the binding constraint, not as background.
        return (
            SUPERVISOR_SYSTEM_PROMPT
            + "\n\n"
            + (
                f"Here is the user request:\n\n"
                f"{user_prompt}\n\n"
                f"Produce the plan block now. One task per line."
            )
        )

    async def _run_planner_turn(self, user_prompt: str) -> None:
        """Run the LLM planning turn, then parse the plan and execute tasks."""
        try:
            plan_chat_id = str(uuid.uuid4())
            self._planner_chat_id = plan_chat_id
            self.tracker.record(ProgressEvent(
                event_type="plan",
                task_id=None,
                data={"user_prompt": user_prompt},
            ))

            # Send planning prompt to the LLM via the turn system
            import runner  # avoid circular import at top level

            prompt_text = self._build_plan_prompt(user_prompt)
            work_dir = str(Path(config.PROJECTS_ROOT).resolve())
            # Explicit, not None. Passing None let the CLI choose its own
            # default, so a gateway serving one local model was asked for
            # claude-opus-5 and answered 429 "No deployments available".
            planner_model = await runner.get_default_model(owner=self.owner_id)
            chunks, _sid = await runner.run_turn(
                prompt_text,
                f"supervisor_{plan_chat_id}",
                work_dir,
                plan_chat_id,
                planner_model,
                # The owner, because plan_chat_id is not a conversation. Backend
                # resolution is keyed on a chats row, so without this the child
                # got no base URL and no API key and every turn died on
                # "Not logged in - Please run /login" -- which is why the
                # supervisor had never once run a task on any backend.
                self.owner_id,
            )
            result = "".join(chunks) if chunks else ""

            if not result:
                _log.warning(
                    "planner_turn returned empty result for supervisor %s",
                    self.supervisor_id,
                )
                await self._set_status("error")
                self._running = False
                return

            # Parse the plan
            tasks = PlanParser.parse(result)
            _log.info(
                "plan_parsed supervisor=%s tasks=%d",
                self.supervisor_id, len(tasks),
            )

            import db

            # Kept whatever the parser made of it. The reply was previously
            # discarded, so `plan` stayed null and a parse that understood
            # nothing left no evidence of what the model had actually said.
            cleaned = clean_result(result)
            await db.supervisor_update(
                self.supervisor_id, self.owner_id, plan=cleaned[:20000])

            if not tasks:
                # A plan nobody could parse is not a finished run. This fell
                # through to the scheduler, which found an empty graph, decided
                # all_done() and reported "done" at 0% -- so a goal that never
                # ran looked exactly like one that succeeded. A false success is
                # worse than a failure, because nobody goes looking.
                await self._set_status("error")
                await db.supervisor_messages_append(
                    self.supervisor_id, "system",
                    "The plan could not be read, so no tasks were created. "
                    "The planner replied:\n\n" + (cleaned[:1500] or "(nothing)"),
                    {"kind": "plan_unparsed"},
                )
                self._running = False
                return

            # Emit the parsed plan as a supervisor message so the chat shows it.
            if tasks:
                task_titles = "\n".join(f"- {t.title}" for t in tasks)
                # `>>`, not `>`: _PLAN_START_RE accepts `<<PLAN` or `<<PLAN>>`
                # and nothing between, so the single-angle form emitted a block
                # the project's own parser cannot recognise. Harmless while this
                # message is only displayed, and exactly the prompt-vs-parser
                # drift that cost the feature once already -- caught by
                # test_the_prompt_and_the_parser_agree_on_the_delimiter, which
                # guards the cause rather than the symptom.
                plan_text = (
                    f"<<PLAN>>\nPlan ({len(tasks)} tasks):\n\n{task_titles}\n<<PLAN>>"
                )
                await db.supervisor_messages_append(
                    self.supervisor_id, "supervisor",
                    plan_text,
                    {"kind": "plan"},
                )

            # Create tasks in the graph and DB
            if tasks:
                # PlanParser numbers tasks from 1 within a plan, so every
                # supervisor produces a t001. supervisor_tasks.id is a global
                # PRIMARY KEY, so the second supervisor's write failed with
                # UNIQUE constraint failed -- caught by a bare warning, so the
                # task ran and finished while the list stayed empty and progress
                # sat at 0%. Namespacing here rather than in the parser keeps
                # {#taskN} references resolvable against the plan's own numbers.
                def _row_id(plan_id: str) -> str:
                    return f"{self.supervisor_id[:8]}_{plan_id}"

                for i, parsed_task in enumerate(tasks):
                    node = TaskNode(
                        id=_row_id(parsed_task.id),
                        title=parsed_task.title,
                        description=parsed_task.description,
                        model=parsed_task.model,
                        parent_id=None,
                        depends_on=[_row_id(d) for d in parsed_task.depends_on],
                        created_at=db._now(),
                        updated_at=db._now(),
                    )
                    self.graph.add_task(node)

                    # Create DB task row (try-catch so plan failure doesn't
                    # prevent tasks from running)
                    try:
                        await db.supervisor_task_create(
                            supervisor_id=self.supervisor_id,
                            task_id=node.id,
                            title=parsed_task.title,
                            description=parsed_task.description,
                            model=parsed_task.model,
                            parent_task_id=None,
                            depends_on=node.depends_on,
                        )
                        self.tracker.record(ProgressEvent(
                            event_type="plan",
                            task_id=node.id,
                            data={
                                "created": True,
                                "title": parsed_task.title,
                                "model": parsed_task.model,
                            },
                        ))
                    except Exception:  # noqa: BLE001
                        # Was a bare warning with no reason attached, which is
                        # why a task list that silently stayed empty while the
                        # work ran took a live run to notice at all.
                        _log.exception(
                            "supervisor_task_create failed for %s",
                            parsed_task.id,
                        )

                # Store parsed plan text
                self.config["parsed_tasks"] = [
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                    }
                    for t in self.graph.tasks.values()
                ]

            # Update supervisor status to running
            await self._set_status("running")
            await asyncio.sleep(0.1)  # let state propagate

            # Start the scheduler loop
            await self.run_schedule_loop()

        except Exception as exc:  # noqa: BLE001
            _log.exception(
                "planner_turn_failed supervisor_id=%s: %s",
                self.supervisor_id, exc,
            )
            await self._set_status("error")
            # The reason has to reach the user, not only the log. Until now a
            # failed run showed the word "error" in the UI and nothing else,
            # so the one thing needed to act on it -- what actually went wrong
            # -- was readable only by someone with shell access to the server.
            # rules.md 3a: every terminal state is named.
            try:
                # Imported here, not borrowed from the try block above: db is
                # imported locally throughout this module because db imports it
                # at load time, which makes `db` a function-local name. The
                # try's import never ran when the failure came before it, so
                # reaching for it here raised UnboundLocalError and swallowed
                # the very message this block exists to record.
                import db

                await db.supervisor_messages_append(
                    self.supervisor_id, "system",
                    f"Run failed: {exc}",
                    {"kind": "error"},
                )
            except Exception:  # noqa: BLE001 -- reporting must not mask the fault
                _log.exception("could not record the failure for the user")
            self._running = False

    async def _execute_task(self, task_id: str, prompt: str, model: str | None) -> str:
        """Execute a single subtask via the turn system.

        Uses runner.run_turn (blocking path) so the scheduler loop can
        synchronously await the result.
        """
        import runner

        graph = self.graph
        node = graph.get_task(task_id)
        if not node:
            return ""

        # A task carries a model only when the plan text named one with
        # [:model_id]; most do not, and passing None let the CLI pick its own
        # default. Against a gateway serving a single local model that came back
        # as 429 "No deployments available for selected model" -- a routing
        # failure wearing a capacity failure's clothes.
        if not model:
            model = await runner.get_default_model(owner=self.owner_id)

        graph.update_status(task_id, "running")
        self.tracker.record(ProgressEvent(
            event_type="task_start",
            task_id=task_id,
            data={"title": node.title},
        ))

        try:
            dep_context = self._build_dep_context(task_id)
            full_prompt = (
                prompt + "\n\nContext from dependent tasks:\n" + dep_context
            ) if dep_context else prompt

            work_dir = str(Path(config.PROJECTS_ROOT).resolve())
            task_chat_id = f"subtask_{task_id}"

            chunks, _sid = await runner.run_turn(
                full_prompt,
                f"supervisor_{task_chat_id}",
                work_dir,
                task_chat_id,
                model,
                # Same reason as the planner: subtask_<id> is not a conversation.
                self.owner_id,
            )
            result = "".join(chunks) if chunks else ""

            graph.update_result(task_id, result)
            graph.update_progress(task_id, 100.0)
            graph.update_status(task_id, "done")

            self.tracker.record(ProgressEvent(
                event_type="task_done",
                task_id=task_id,
                data={"result_len": len(result)},
            ))

            # Write the task result to the messages table so the chat shows it.
            try:
                import db
                node_title = node.title or task_id
                clean = clean_result(result)
                # A task can finish having emitted nothing but tool calls, and
                # cleaning those away leaves an empty body. Saying so beats a
                # header over blank space -- and the char count has to describe
                # what is actually displayed, not the text that was filtered
                # out, or it reads as a message that failed to load.
                if clean:
                    body = f"Task '{node_title}' completed ({len(clean)} chars)\n\n{clean[:3000]}"
                else:
                    body = (
                        f"Task '{node_title}' completed with no text output "
                        f"-- it only made tool calls."
                    )
                await db.supervisor_messages_append(
                    self.supervisor_id, "supervisor",
                    body,
                    {"kind": "task_result", "task_id": task_id},
                )
            except Exception:  # noqa: BLE001 -- task success must not fail silently
                _log.exception("could not record task result message for %s", task_id)

            # Also update DB task row
            try:
                import db
                await db.supervisor_task_update(
                    supervisor_id=self.supervisor_id,
                    task_id=task_id,
                    owner_id=self.owner_id,
                    status="done",
                    result=result,
                    progress_pct=100.0,
                )
            except Exception:  # noqa: BLE001
                _log.exception("could not record task %s as done", task_id)

            return result

        except Exception as exc:  # noqa: BLE001
            graph.update_status(task_id, "failed")
            self.tracker.record(ProgressEvent(
                event_type="task_error",
                task_id=task_id,
                data={"error": str(exc)},
            ))
            _log.error("Task %s failed: %s", task_id, exc)

            # Also update DB task row
            try:
                import db
                await db.supervisor_task_update(
                    supervisor_id=self.supervisor_id,
                    task_id=task_id,
                    owner_id=self.owner_id,
                    status="failed",
                    progress_pct=0.0,
                )
            except Exception:  # noqa: BLE001
                _log.exception("could not record task %s as failed", task_id)

            return ""

    def _build_dep_context(self, task_id: str) -> str:
        """Build context from completed dependency results."""
        node = self.graph.get_task(task_id)
        if not node:
            return ""
        lines: list[str] = []
        for dep_id in node.depends_on:
            dep = self.graph.get_task(dep_id)
            if dep and dep.result:
                lines.append(
                    f"Task {dep_id} result: {dep.result[:500]}"
                )
        return "\n".join(lines) if lines else ""

    async def run_schedule_loop(self) -> None:
        """Main scheduler: check for ready tasks and execute them.

        Everything is wrapped because this runs as a bare background task that
        nothing awaits: an exception escaping here is reported only as asyncio's
        "Task exception was never retrieved", and the supervisor would sit in
        "running" with nothing running and no error anywhere the user can see.
        """
        self._running = True
        try:
            while self._running and not self.graph.all_done():
                ready = self.graph.get_ready_tasks()
                if ready:
                    for tid in ready:
                        if not self._running:
                            break
                        node = self.graph.get_task(tid)
                        if node and node.status == "ready":
                            await self._execute_task(
                                tid,
                                f"Complete this task: {node.title}\n\n{node.description}",
                                node.model,
                            )
                await self._persist_progress()
                await self._wait_if_paused()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # Shutdown, not a fault. Leave the status alone and let it go.
            self._running = False
            raise
        except Exception:  # noqa: BLE001 -- a background task must not die silently
            _log.exception(
                "schedule_loop_failed supervisor_id=%s", self.supervisor_id
            )
            self._running = False
            await self._set_status("error")
            return

        # A loop that was stopped did not finish. Reporting "done" for a run
        # the user cancelled, or one whose tasks failed, is the difference
        # between a result and the appearance of one.
        stopped_early = not self.graph.all_done()
        self._running = False
        await self._persist_progress()
        if stopped_early:
            await self._set_status("idle")
        elif self.graph.any_failed():
            await self._set_status("error")
        else:
            await self._set_status("done")

    def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False

    def pause(self) -> bool:
        """Pause a running supervisor. Returns False if nothing was paused."""
        if self._running and not self._paused:
            self._paused = True
            self._resume_event.clear()
            self._pre_pause_status = "running"
            return True
        return False

    def set_status_for_pause(self, status: str) -> None:
        """Remember what status was before the run started (planning vs running).

        The engine always stores "running" at line 693, but the user's view
        needs to know whether the supervisor was mid-plan or mid-task so the
        resume button shows the right thing.  The simplest correct approach is
        to let the caller tell us — the API handler passes the pre-pause DB
        value here before calling pause(), so the engine remembers it to
        restore later.
        """
        if self._paused:
            self._pre_pause_status = status

    def resume(self) -> bool:
        """Resume a paused supervisor. Returns False if nothing was resumed."""
        if self._paused:
            self._paused = False
            self._resume_event.set()
            return True
        return False

    async def _wait_if_paused(self) -> None:
        """Yield until the pause flag is cleared or a short interval elapses.

        Used in the scheduler loop so a paused supervisor does not spin, but
        also does not block forever: a 2-second poll means the engine loop
        still reaches the progress-persist line at least once every 2 seconds
        even while paused, so the UI never looks stale.
        """
        if not self._paused:
            return
        try:
            await asyncio.wait_for(self._resume_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    def state(self) -> dict[str, Any]:
        """Return full engine state for serialization."""
        return {
            "supervisor_id": self.supervisor_id,
            "status": "running" if self._running else "idle",
            "progress": self.tracker.supervisor_progress(self.graph),
            "tasks": self.graph.to_dict(),
            "recent_events": self.tracker.recent_events(5),
        }
