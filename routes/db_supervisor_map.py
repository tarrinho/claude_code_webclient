"""Database queries for the supervisor map (radial mind map).

Assembles a JSON tree from existing sources: backends, transports,
orchestrators, chat list, CLI sessions, and orchestrator tasks/members.
No schema changes — everything already exists.
"""
from __future__ import annotations

import logging
from typing import Any, Final

_log = logging.getLogger("wc.app")

# Children per node before an overflow marker replaces the tail.
#
# This used to be 4, applied silently by three separate `[:4]` slices, and the
# map gave no sign it was hiding anything: a host with six backends showed four
# and looked complete. 4 was chosen when the layout could not be panned or
# zoomed at all (the radial tree collapsed into a sub-pixel cluster and no zoom
# control worked), so showing more would not have helped. Both of those are
# fixed, so the cap exists only to stop one enormous group from making the
# whole tree unreadable, and what it drops is now stated in the tree itself.
_MAX_CHILDREN: Final[int] = 12


async def supervisor_map(owner_id: str) -> dict[str, Any]:
    """Return the full supervisor map tree for *owner_id*.

    Tree structure (three levels):

        You -> transport -> {machine, orchestrator -> task/chat,
                             direct chat, CLI session}

    Returns a dict with ``center``, a list of ``children`` (one per
    transport group), and aggregate ``status`` / ``type`` fields on every
    node.  Groups longer than ``_MAX_CHILDREN`` end in a ``type: "more"``
    node naming how many were left out.
    """
    import db  # noqa: local import — avoids cyclic import when loaded
    # Bound as `turns`, not `turns as _turns`: line 42 below calls
    # `turns.running_ids`, so the aliased name left `turns` undefined and every
    # request to this route raised NameError. flake8 saw it both ways at once --
    # F401 for the unused alias and F821 for the undefined use.
    import turns
    import resource_guard
    from shared import backend_kind  # used for each machine's display kind
    from classification import _cli_maps, classify_chat

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

    # Archived conversations are excluded everywhere below, so filter once
    # rather than repeating the check at each of the four places that used to
    # do it (and the one place that forgot).
    live_chats = [chat for chat in chats if not chat.get("archived")]

    # ── Classify each chat ─────────────────────────────────────────
    chat_status: dict[str, str] = {}
    for chat in live_chats:
        # A degraded conversation is an error before anything else is asked.
        # It is set here rather than only on the chat node so that the node
        # and the machine serving it cannot disagree: classify_chat does not
        # look at the column, and it never returns "error" at all, so a
        # machine reading its status from classify output alone could not
        # report a failure that had already been recorded. Recorded activity
        # is not required -- a conversation can be given up on before it has
        # any.
        if chat.get("degraded"):
            chat_status[chat["id"]] = "error"
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
    # Members are fetched once per orchestrator and reused. The assembly loop
    # below queried them a second time for the same orchestrator, so every
    # orchestrator on the map cost two identical queries.
    members_by_orch: dict[str, list[dict]] = {}
    for orch in orchestrators:
        members_by_orch[orch["id"]] = await db.orchestrator_members_list(
            orch["id"], owner_id,
        )

    chat_to_orch: dict[str, str] = {}
    for orch_id, members in members_by_orch.items():
        for member in members:
            cid = member.get("chat_id")
            if cid and cid not in chat_to_orch:
                chat_to_orch[cid] = orch_id

    # ── Index: machine_id -> its conversations ─────────────────────
    # A machine node's status comes from the work running on it. It used to be
    # `_aggregate_status([])`, which is unconditionally "idle" -- so a backend
    # with a failing conversation on it looked exactly like an unused one.
    chats_by_machine: dict[str | None, list[str]] = {}
    for chat in live_chats:
        chats_by_machine.setdefault(chat.get("ai_machine_id"), []).append(chat["id"])

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
    for chat in live_chats:
        cid = chat["id"]
        if cid in chat_to_orch:
            continue
        transport_groups.setdefault(_chat_transport(chat, machine_by_id), []).append(
            {"__chat": True, "chat": chat}
        )

    # Group 4: terminal sessions, under "direct" -- they run on this host.
    # read_claude_sessions() and _classify_cli_session were both already being
    # fetched and imported here and then never used, so the map showed no
    # terminal sessions at all while paying for the read.
    for session_node in await _cli_session_nodes(live_chats, marks):
        transport_groups.setdefault(None, []).append(
            {"__session": True, "node": session_node}
        )

    # ── Assemble children ──────────────────────────────────────────
    children: list[dict[str, Any]] = []
    for tid, group in transport_groups.items():
        key = tid or "direct"

        # Sort: machines first, then orchestrators, then direct chats
        machine_nodes: list[dict] = []
        orchestrator_nodes: list[dict] = []
        direct_chats: list[dict] = []
        session_nodes: list[dict] = []

        for item in group:
            if "__orch" in item:
                orch = item["orch"]
                orch_id = orch["id"]
                members = members_by_orch.get(orch_id, [])
                tasks = await db.orchestrator_tasks_get(orch_id, owner_id)

                orch_children: list[dict[str, Any]] = []
                # Tasks. Typed "task", not "chat": a task id is not a chat id,
                # so the route's _enrich_messages was querying messages_last()
                # with it on every request and always getting nothing, and the
                # drawer offered conversation actions for a row that has none.
                for task in tasks:
                    orch_children.append({
                        "id": task.get("id", f"{orch_id}-task-{len(orch_children)}"),
                        "label": task.get("title") or f"Task {len(orch_children) + 1}",
                        "status": _task_status(task),
                        "type": "task",
                    })
                # Members not covered by a task
                task_ids = {t.get("id") for t in tasks}
                for member in members:
                    cid = member.get("chat_id")
                    if not cid or cid in task_ids:
                        continue
                    member_node = {
                        "id": cid,
                        "label": member.get("title") or "Chat",
                        "status": _normalise(chat_status.get(cid)),
                        "type": "chat",
                    }
                    _attach_last_message(member_node, activity.get(cid))
                    orch_children.append(member_node)

                if orch_children:
                    # Enrich orchestrator node with chat-level metadata for the detail drawer
                    orch_node = {
                        "id": orch_id,
                        "label": orch.get("title", "Orchestrator"),
                        "status": _aggregate_status(orch_children),
                        "type": "orchestrator",
                        "children": _capped(orch_children, orch_id),
                    }
                    if orch.get("degraded"):
                        orch_node["status"] = "error"
                        orch_node["degraded_reason"] = orch.get("degraded_reason") or ""
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
                direct_chats.append(_chat_node(
                    item["chat"], chat_status, queued, machine_by_id, activity,
                ))
            elif "__session" in item:
                session_nodes.append(item["node"])
            else:
                m = item
                bk = backend_kind(m)
                node: dict[str, Any] = {
                    "id": m["id"],
                    "label": bk,
                    "status": _machine_status(m, chats_by_machine, chat_status),
                    "type": "machine",
                }
                if not m.get("transport_id"):
                    node["capacity_existing"] = host_capacity["existing"]
                    node["capacity_total"] = host_capacity["total"]
                machine_nodes.append(node)

        # Build transport node children in order: machines, orchestrators,
        # direct chats, terminal sessions
        node_children: list[dict[str, Any]] = (
            machine_nodes + orchestrator_nodes + direct_chats + session_nodes
        )
        children.append({
            "id": key,
            "label": key.capitalize(),
            "status": _aggregate_status(node_children),
            "type": "transport",
            "children": _capped(node_children, key),
        })

    return {
        "center": "You",
        "children": _capped(children, "root"),
    }


def _capped(nodes: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """*nodes* truncated to ``_MAX_CHILDREN``, with what was dropped named.

    A silent slice is the failure this replaces: the map looked complete
    whatever it left out, so a group of six read as a group of four and there
    was nothing on screen to contradict it. The marker is deliberately inert --
    the rest of the group is not in this response, so there is nothing for a
    click to expand, and a control that looks live and does nothing is worse
    than a label that just reports the number.
    """
    if len(nodes) <= _MAX_CHILDREN:
        return nodes
    hidden = len(nodes) - _MAX_CHILDREN
    return nodes[:_MAX_CHILDREN] + [{
        "id": f"{key}-more",
        "label": f"+{hidden} more",
        "status": "idle",
        "type": "more",
        "hidden_count": hidden,
    }]


def _attach_last_message(node: dict[str, Any], last: dict | None) -> None:
    """Attach the drawer's preview from an already-fetched activity row.

    ``chat_last_activity`` returns ``substr(content, 1, 200)`` for the newest
    message of every one of the owner's conversations, in one grouped query
    that this module already runs to classify them. The route used to ignore
    that and read the same 200 characters again, one query per node.
    """
    if not isinstance(last, dict):
        return
    preview = last.get("preview")
    if preview:
        node["last_message"] = preview
    updated = last.get("created_at")
    if updated:
        node["updated_at"] = updated


def _chat_transport(chat: dict, machine_by_id: dict[str, dict]) -> str | None:
    """Which transport group a conversation belongs in.

    Resolved through the machine it is routed to, because a conversation has no
    transport of its own. This read ``chat.get("transport_id")`` -- a column
    the ``chats`` table does not have -- so it was `None` for every
    conversation ever created, and every direct chat landed in the "Direct"
    group no matter which SSH-proxied backend was actually serving it. Nothing
    raised: `.get` on a missing key is the same as a genuine `NULL`, which is
    exactly why it survived.
    """
    machine = machine_by_id.get(chat.get("ai_machine_id")) or {}
    return machine.get("transport_id")


def _chat_node(
    chat: dict,
    chat_status: dict[str, str],
    queued: dict,
    machine_by_id: dict[str, dict],
    activity: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """One direct-chat node, carrying the three states the map used to drop."""
    from shared import backend_kind

    cid = chat["id"]
    machine = machine_by_id.get(chat.get("ai_machine_id")) or {}
    node: dict[str, Any] = {
        "id": cid,
        "label": chat.get("title") or "Untitled",
        "status": _normalise(chat_status.get(cid)),
        "type": "chat",
        "machine_label": backend_kind(machine) if machine else "unknown",
    }
    _attach_last_message(node, (activity or {}).get(cid))
    # A degraded conversation is an error whatever it is otherwise doing.
    # classify_chat does not look at the column, so without this the map
    # showed a conversation the app had already given up on as plain idle.
    if chat.get("degraded"):
        node["status"] = "error"
        node["degraded_reason"] = chat.get("degraded_reason") or ""
    if chat.get("voice_mode"):
        node["voice_mode"] = True
    pending = queued.get(cid) or 0
    if pending:
        node["queued"] = pending
    return node


def _machine_status(
    machine: dict,
    chats_by_machine: dict[str | None, list[str]],
    chat_status: dict[str, str],
) -> str:
    """A backend's status, from the conversations routed to it.

    A machine row carries no live state of its own -- whether it is working is
    a property of the turns running against it. `enabled` is deliberately not
    folded in here: "switched off" is a configuration fact shown in Settings →
    Backends, and colouring a disabled backend as an error on the map would
    report a chosen state as a fault.
    """
    ids = chats_by_machine.get(machine["id"], [])
    return _aggregate_status(
        [{"status": _normalise(chat_status[cid])} for cid in ids if cid in chat_status]
    )


async def _cli_session_nodes(
    live_chats: list[dict], marks: dict
) -> list[dict[str, Any]]:
    """Terminal (CLI) sessions as map nodes.

    Skips two kinds, matching routes/orchestrators.py's feed so the two
    surfaces do not disagree about what a session is: a WebConsole shadow
    record (``entrypoint == "webconsole"``) describes a conversation that is
    already on the map, and a session linked to a listed conversation would
    appear twice.

    Returns an empty list when the session registry or the transcript index
    cannot be read. The map is one panel among many; a missing terminal
    session is better than a 500 for the whole tree.
    """
    import db
    import transcripts
    from classification import _classify_cli_session

    try:
        sessions = await db.read_claude_sessions()
    except Exception:
        _log.debug("supervisor_map: session registry unreadable", exc_info=True)
        return []
    try:
        meta_by_id = {t["session_id"]: t for t in await transcripts.list_recent(200)}
    except Exception:
        _log.debug("supervisor_map: transcript index unreadable", exc_info=True)
        return []

    linked = {c.get("session_id") for c in live_chats if c.get("session_id")}
    nodes: list[dict[str, Any]] = []
    for cli in sessions:
        session_id = cli.get("sessionId") or ""
        if not session_id or cli.get("entrypoint") == "webconsole":
            continue
        if session_id in linked:
            continue
        meta = meta_by_id.get(session_id)
        if not meta:
            continue
        entry = await _classify_cli_session(
            cli, meta, marks.get(("session", session_id), {})
        )
        if entry is None:
            continue
        nodes.append({
            "id": session_id,
            "label": entry.get("title") or session_id[:8],
            "status": _normalise(entry.get("status")),
            "type": "session",
            "last_message": entry.get("preview") or "",
            "updated_at": entry.get("since") or "",
        })
    return nodes


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
