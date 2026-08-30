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

# -- Plan parsing ----------------------------------------------------------

_PLAN_START_RE = re.compile(r"(?i)^\s*<<PLAN\s*$", re.MULTILINE)
_PLAN_END_RE = re.compile(r"(?i)^>>\s*$", re.MULTILINE)
_TASK_MARKER_RE = re.compile(
    r"(?i)^\s*(?:TASK|#\s*\d+\.?\s*)(.+?)(?::|\s*-|\s+)(.+)", re.MULTILINE
)
_DEPENDENCY_RE = re.compile(r"\{#([\w#\s,]+?)\}", re.IGNORECASE)
_MODEL_RE = re.compile(r"\[:(\S+)\]", re.IGNORECASE)


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
            description = match.group(2).strip()
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
            if task.status in ("done", "failed", "running"):
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
        if not self.tasks:
            return True
        return all(
            t.status in ("done", "blocked") for t in self.tasks.values()
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
    "You are a Supervisor Agent coordinating a team of AI sub-agents "
    "to complete a complex task.\n\n"
    "YOUR ROLE:\n"
    "- Break the user request into a clear structured plan with numbered tasks\n"
    "- Each task should be an independent subtask assigned to a sub-agent\n"
    "- Track progress and reassign tasks as needed\n"
    "- Answer sub-agent questions on behalf of the user\n\n"
    "TASK FORMAT:\n"
    "Use this format when presenting your plan:\n"
    "  <<PLAN\n"
    "  Task 1: {title} - {description} {#{taskId}} [:model]\n"
    "  Task 2: {title} - {description} {#{taskId}} [:model]\n"
    "  >>\n\n"
    "DEPENDENCIES:\n"
    "Reference other tasks with {#taskId}.\n\n"
    "MODEL SELECTION:\n"
    "Use [:model_id] to suggest which model to use. Omit to auto-select.\n\n"
    "WORKFLOW:\n"
    "1. First produce the PLAN block with all tasks\n"
    "2. The system executes tasks in dependency order\n"
    "3. You receive progress updates as tasks complete\n"
    "4. Review results and produce a final summary\n\n"
    "Keep tasks focused and actionable. Aim for 3-10 tasks."
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
        self._planner_chat_id: str | None = None  # synthetic chat for planning turn

    async def start_from_user_prompt(self, user_prompt: str) -> dict[str, Any]:
        """Launch the supervisor and begin planning.

        Starts a background asyncio task that asks the LLM to produce a plan.
        When the plan arrives, PlanParser extracts tasks and the engine
        transitions from 'planning' to 'running' and begins execution.
        """
        self._running = True
        asyncio.create_task(self._run_planner_turn(user_prompt))
        return {
            "supervisor_id": self.supervisor_id,
            "status": "planning",
            "user_prompt": user_prompt,
        }

    @staticmethod
    def _build_plan_prompt(user_prompt: str) -> str:
        prompt = (
            f"I need you to act as a Supervisor Agent for this task.\n\n"
            f"Here is the user request:\n\n"
            f"{user_prompt}\n\n"
            f"Break this down into a clear plan with independent tasks "
            f"that sub-agents can execute. Use the <<PLAN>>..>> format "
            f"with numbered tasks. Include dependencies where needed.\n\n"
            "For each task, be specific about what the sub-agent should "
            f"produce. Use {{#taskId}} to reference dependencies and "
            "[:model_id] for model suggestions."
        )
        return prompt

    async def _run_planner_turn(self, user_prompt: str) -> None:
        """Run the LLM planning turn, then parse the plan and execute tasks."""
        try:
            plan_chat_id = uuid.uuid4().hex
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
            chunks, _sid = await runner.run_turn(
                prompt_text,
                f"supervisor_{plan_chat_id}",
                work_dir,
                plan_chat_id,
                None,
            )
            result = "".join(chunks) if chunks else ""

            if not result:
                _log.warning(
                    "planner_turn returned empty result for supervisor %s",
                    self.supervisor_id,
                )
                self.graph.update_status("supervisor", "error")
                self._running = False
                return

            # Parse the plan
            tasks = PlanParser.parse(result)
            _log.info(
                "plan_parsed supervisor=%s tasks=%d",
                self.supervisor_id, len(tasks),
            )

            # Create tasks in the graph and DB
            if tasks:
                import db  # avoid circular import at top level

                for i, parsed_task in enumerate(tasks):
                    node = TaskNode(
                        id=parsed_task.id,
                        title=parsed_task.title,
                        description=parsed_task.description,
                        model=parsed_task.model,
                        parent_id=None,
                        depends_on=parsed_task.depends_on,
                        created_at=db._now(),
                        updated_at=db._now(),
                    )
                    self.graph.add_task(node)

                    # Create DB task row (try-catch so plan failure doesn't
                    # prevent tasks from running)
                    try:
                        await db.supervisor_task_create(
                            supervisor_id=self.supervisor_id,
                            task_id=parsed_task.id,
                            title=parsed_task.title,
                            description=parsed_task.description,
                            model=parsed_task.model,
                            parent_task_id=None,
                            depends_on=parsed_task.depends_on,
                        )
                        self.tracker.record(ProgressEvent(
                            event_type="plan",
                            task_id=parsed_task.id,
                            data={
                                "created": True,
                                "title": parsed_task.title,
                                "model": parsed_task.model,
                            },
                        ))
                    except Exception:  # noqa: BLE001
                        _log.warning(
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
            self.graph.update_status("supervisor", "running")
            await asyncio.sleep(0.1)  # let state propagate

            # Start the scheduler loop
            await self.run_schedule_loop()

        except Exception as exc:  # noqa: BLE001
            _log.exception(
                "planner_turn_failed supervisor_id=%s: %s",
                self.supervisor_id, exc,
            )
            self.graph.update_status("supervisor", "error")
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
            except Exception:  # noqa: BLE001, S110
                pass

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
            except Exception:  # noqa: BLE001, S110
                pass

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
        """Main scheduler: check for ready tasks and execute them."""
        self._running = True
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
            await asyncio.sleep(0.5)

        self._running = False
        self.graph.update_status("supervisor", "done")

    def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False

    def state(self) -> dict[str, Any]:
        """Return full engine state for serialization."""
        return {
            "supervisor_id": self.supervisor_id,
            "status": "running" if self._running else "idle",
            "progress": self.tracker.supervisor_progress(self.graph),
            "tasks": self.graph.to_dict(),
            "recent_events": self.tracker.recent_events(5),
        }
