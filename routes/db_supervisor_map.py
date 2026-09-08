"""Database queries for the supervisor map (radial mind map).

Assembles a JSON tree from existing sources: backends, transports,
orchestrators, chat list, CLI sessions, and orchestrator tasks/members.
No schema changes — everything already exists.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("wc.app")


async def supervisor_map(owner_id: str) -> dict[str, Any]:
    """Return the full supervisor map tree for *owner_id*.

    Tree structure (three levels):

        You -> transport -> {orchestrator -> chat, direct chat}

    Returns a dict with ``center``, a list of ``children`` (one per
    transport group), and aggregate ``status`` / ``type`` fields on every
    node.  Max 4 top-level children and max 4 grandchildren per child.
    """
    import db  # noqa: local import — avoids cyclic import when loaded
    # Bound as `turns`, not `turns as _turns`: line 42 below calls
    # `turns.running_ids`, so the aliased name left `turns` undefined and every
    # request to this route raised NameError. flake8 saw it both ways at once --
    # F401 for the unused alias and F821 for the undefined use.
    import turns
    import resource_guard
    from shared import backend_kind  # used for each machine's display kind
    from classification import _classify_cli_session, _cli_maps, classify_chat  # noqa

    # ── Fetch all sources ──────────────────────────────────────────
    # One /proc scan for the whole map, not one per machine node: existing vs
    # total is a property of this host, shared by every node backed by it, and
    # report() (inside capacity()) says plainly it belongs on an
    # infrequent path -- the map is fetched on open, never polled, so once per
    # request is the right cost.
    host_capacity = resource_guard.capacity()
    machines = await db.ai_machines_list(owner_id)
    chats = await db.chat_list(owner_id)
    activity = await db.chat_last_activity(owner_id)
    orchestrators = await db.orchestrator_list(owner_id)
    cli_sessions = await db.read_claude_sessions()

    # Build lookup indexes
    machine_by_id: dict[str, dict] = {m["id"]: m for m in machines}

    # Build CLI status lookups
    marks = await db.read_marks_get(owner_id)
    live_ids = turns.running_ids(owner_id)
    try:
        queued = await db.queue_counts(owner_id)
    except Exception:
        queued = {}
    (
        cli_status_map,
        cli_dismiss_map,
        cli_status_updated_map,
        cli_prompt_map,
    ) = await _cli_maps(marks)

    # ── Classify each chat ─────────────────────────────────────────
    chat_status: dict[str, str] = {}
    for chat in chats:
        if chat.get("archived"):
            continue
        last = activity.get(chat["id"])
        if not last:
            continue
        entry = classify_chat(
            chat, last, live_ids, queued, marks,
            cli_status_map, cli_dismiss_map,
            cli_status_updated_map, cli_prompt_map,
        )
        if entry is not None:
            chat_status[chat["id"]] = entry["status"]

    # ── Index: chat_id -> orchestrator_id ──────────────────────────
    chat_to_orch: dict[str, str] = {}
    for orch in orchestrators:
        for member in await db.orchestrator_members_list(orch["id"]):
            cid = member.get("chat_id")
            if cid and cid not in chat_to_orch:
                chat_to_orch[cid] = orch["id"]

    # ── Group machines by transport ────────────────────────────────
    machines_by_transport: dict[str | None, list[dict]] = {}
    for m in machines:
        machines_by_transport.setdefault(m.get("transport_id"), []).append(m)

    # ── Build tree by transport (You → transport → {machine, orchestrator → chat, direct chat})
    transport_groups: dict[str | None, list[dict[str, Any]]] = {}

    # Group 1: machines (per transport, keyed by transport_id)
    for tid, machine_list in machines_by_transport.items():
        transport_groups.setdefault(tid, []).extend(machine_list)

    # Group 2: orchestrators always live under "direct" (app's own machine)
    for orch in orchestrators:
        transport_groups.setdefault(None, []).append(
            {"__orch": True, "orch": orch}
        )

    # Group 3: direct chats (not owned by an orchestrator) under their machine's transport
    for chat in chats:
        cid = chat["id"]
        if cid in chat_to_orch:
            continue
        if chat.get("archived"):
            continue
        transport_groups.setdefault(chat.get("transport_id"), []).append(
            {"__chat": True, "chat": chat}
        )

    # ── Assemble children ──────────────────────────────────────────
    children: list[dict[str, Any]] = []
    for tid, group in transport_groups.items():
        key = tid or "direct"

        # Sort: machines first, then orchestrators, then direct chats
        machines: list[dict] = []
        orchestrator_nodes: list[dict] = []
        direct_chats: list[dict] = []

        for item in group:
            if "__orch" in item:
                orch = item["orch"]
                orch_id = orch["id"]
                members = await db.orchestrator_members_list(orch_id)
                tasks = await db.orchestrator_tasks_get(orch_id, owner_id)

                orch_children: list[dict[str, Any]] = []
                # Tasks
                for task in tasks:
                    orch_children.append({
                        "id": task.get("id", f"{orch_id}-task-{len(orch_children)}"),
                        "label": task.get("title") or f"Task {len(orch_children) + 1}",
                        "status": _task_status(task),
                        "type": "chat",
                    })
                # Members not covered by a task
                task_ids = {t.get("id") for t in tasks}
                for member in members:
                    cid = member.get("chat_id")
                    if not cid or cid in task_ids:
                        continue
                    orch_children.append({
                        "id": cid,
                        "label": member.get("title") or "Chat",
                        "status": _normalise(chat_status.get(cid)),
                        "type": "chat",
                    })

                if orch_children:
                    # Enrich orchestrator node with chat-level metadata for the detail drawer
                    orch_node = {
                        "id": orch_id,
                        "label": orch.get("title", "Orchestrator"),
                        "status": _aggregate_status(orch_children),
                        "type": "orchestrator",
                        "children": orch_children[:4],
                    }
                    orch_node["_chats"] = []
                    for member in members:
                        cid = member.get("chat_id")
                        if not cid:
                            continue
                        orch_node["_chats"].append({
                            "id": cid,
                            "title": member.get("title") or "Chat",
                            "updated_at": activity.get(cid, {}).get("updated_at", "") if isinstance(activity.get(cid), dict) else "",
                        })
                    orchestrator_nodes.append(orch_node)
            elif "__chat" in item:
                chat = item["chat"]
                cid = chat["id"]
                machine_id = chat.get("ai_machine_id")
                machine = machine_by_id.get(machine_id, {})
                bk = backend_kind(machine) if machine else "unknown"
                direct_chats.append({
                    "id": cid,
                    "label": chat.get("title") or "Untitled",
                    "status": _normalise(chat_status.get(cid)),
                    "type": "chat",
                    "machine_label": bk,
                })
            else:
                m = item
                bk = backend_kind(m)
                node: dict[str, Any] = {
                    "id": m["id"],
                    "label": bk,
                    "status": _aggregate_status([]),
                    "type": "machine",
                }
                if not m.get("transport_id"):
                    node["capacity_existing"] = host_capacity["existing"]
                    node["capacity_total"] = host_capacity["total"]
                machines.append(node)

        # Build transport node children in order: machines, orchestrators, direct chats
        node_children: list[dict[str, Any]] = machines + orchestrator_nodes + direct_chats
        children.append({
            "id": key,
            "label": key.capitalize(),
            "status": _aggregate_status(node_children),
            "type": "transport",
            "children": node_children[:4],
        })

    return {
        "center": "You",
        "children": children[:4],
    }


def _aggregate_status(children: list[dict[str, Any]]) -> str:
    """Compute aggregate status for a parent node.

    Priority (highest wins): error > running > busy > waiting > idle > done.
    """
    if not children:
        return "idle"

    if any(c["status"] == "error" for c in children):
        return "error"
    if any(c["status"] in ("running", "busy") for c in children):
        return "running"
    if any(c["status"] == "waiting" for c in children):
        return "waiting"
    if all(c["status"] == "done" for c in children):
        return "done"
    return "idle"


def _task_status(task: dict) -> str:
    """Derive a status string from an orchestrator task row."""
    state = (task.get("status") or "").lower()
    if state in ("running", "active"):
        return "busy"
    if state == "error":
        return "error"
    if state == "done":
        return "done"
    return "idle"


def _normalise(status: str | None) -> str:
    """Normalise a classify_chat status into the map vocabulary."""
    if status in ("working", "updated"):
        return "running"
    if status in ("running", "busy", "waiting", "idle", "error", "done"):
        return status
    return "idle"


