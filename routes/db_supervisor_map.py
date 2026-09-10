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
    # One grouped read for every transport's newest CPU/RAM sample, keyed by
    # the transport name the map groups on. Best-effort: the dashboard's glow
    # is worth having and is not worth failing the whole map for, and a host
    # that has never been sampled is a normal state (a transport added a
    # minute ago has no rows yet).
    # Keyed by the same value the map groups on. system_samples.host_id holds
    # the transport id for a remote host and the literal "local" for this one,
    # and the map's group key is `tid or "direct"` -- so the transport ids join
    # directly and only "local" has to be translated. There is no host_label
    # column; an earlier draft of this read one and would have keyed every
    # sample under the empty string.
    host_samples: dict[str, dict] = {}
    try:
        for row in await db.system_latest_by_host():
            host_id = (row.get("host_id") or "").strip()
            if host_id:
                host_samples[host_id] = row
        # system_latest_by_host filters host_type != 'local', so the machine
        # the console itself runs on -- the "direct" hub, and the one most
        # likely to be saturated -- would have no glow at all without this.
        local = await db.system_latest()
        if local:
            host_samples["direct"] = local
    except Exception:
        _log.warning("supervisor_map: host samples unavailable", exc_info=True)
    # Transport names, for the hub labels. Without this the label was
    # `key.capitalize()` on a group key that is a transport *id*, so every
    # remote hub read as a capitalised hex string
    # ("F6f52152ad874e76a9c88b8dd5271655") instead of "Kali3". The dashboard
    # spec names hubs as machines, so the id was never going to do.
    transport_names: dict[str, str] = {}
    try:
        for row in await db.ssh_transports_list(owner_id):
            if row.get("id"):
                transport_names[row["id"]] = row.get("name") or row["id"]
    except Exception:
        _log.warning("supervisor_map: transport names unavailable", exc_info=True)
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
                    # Dashboard fields. Two separate keys on purpose -- the
                    # spec forbids merging the transport-mechanism badge and
                    # the model/provider badge into one label.
                    "provider_family": _provider_family(m),
                    "transport_mechanism": _transport_mechanism(m),
                    "model_label": m.get("model") or "",
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
        hub: dict[str, Any] = {
            "id": key,
            "label": transport_names.get(key) or ("Direct" if key == "direct" else key),
            "status": _aggregate_status(node_children),
            "type": "transport",
            "children": _capped(node_children, key),
            # How many agents hang off this hub, for the compact view -- which
            # shows hubs and counts only, so it must not have to walk the
            # children it is not rendering. Counted before _capped, so an
            # overflowed group still reports its real size.
            "agent_count": len(node_children),
        }
        # Load, for the hub's glow. Absent rather than zeroed when the host has
        # never reported: nothing measured and nothing happening must not look
        # alike, and a 0% glow would read as "healthy" for a host that is
        # simply not talking to us.
        sample = host_samples.get(key)
        if sample is not None:
            hub["cpu_pct"] = sample.get("cpu_pct")
            hub["mem_pct"] = sample.get("mem_pct")
            hub["mem_used"] = sample.get("mem_used")
            hub["mem_total"] = sample.get("mem_total")
            hub["load1"] = sample.get("load1")
            hub["load5"] = sample.get("load5")
            hub["load15"] = sample.get("load15")
            hub["sampled_at"] = sample.get("created_at")
            hub["load_index"] = _load_index(sample)
        children.append(hub)

    return {
        "center": "You",
        "children": _capped(children, "root"),
        # The header shows "last updated Ns ago", so the answer has to come
        # from the server: a client clock that is wrong makes a stale map look
        # fresh, which is the one thing a freshness indicator must not do.
        "generated_at": _utcnow(),
    }




def _utcnow() -> str:
    """An ISO-8601 UTC stamp, matching what the rest of the schema stores."""
    import datetime
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Supervisor Dashboard fields ─────────────────────────────────────────
# Added for the dashboard spec, which needs four things the map never
# carried: which provider family an agent talks to, whether it reaches it
# through the Claude Code CLI or a direct API call, how the operator can
# talk to it, and a state vocabulary that distinguishes "waiting for me"
# from "busy".
#
# All four are derived from data already stored -- nothing new is collected.
# The map's own `status` field is left exactly as it was: seven existing
# tests asserts its vocabulary, and the dashboard's states are a different
# question ("what does this agent need from me") answered alongside it.

# The three families the spec asks for, keyed off what the backend row says.
# Deliberately not a lookup on provider alone: a LiteLLM gateway and the
# official API are both "anthropic-compatible" to the CLI, and the thing that
# tells them apart is the host.
_ANTHROPIC_HOSTS: Final[frozenset[str]] = frozenset({"api.anthropic.com"})


def _provider_family(machine: dict) -> str:
    """One of anthropic / google_litellm / local.

    `local` means a self-hosted or free endpoint -- a vllm model on the
    gateway, or anything on a private address. The spec calls this
    "free/self-hosted", and the operator's reason for wanting it separate is
    that those turns cost nothing, which is also why it must not be inferred
    from the model name alone: `azure_ai/...` on the same gateway is not free.
    """
    host = (machine.get("host") or "").strip().lower()
    model = (machine.get("model") or "").strip().lower()
    base = (machine.get("base_url") or "").strip().lower()
    if host in _ANTHROPIC_HOSTS:
        return "anthropic"
    # vllm-served models are the self-hosted ones on this deployment; the
    # gateway also fronts azure_ai/* and anthropic models, which are not.
    if model.startswith("vllm/") or "localhost" in base or "127.0.0.1" in base:
        return "local"
    if host or base:
        return "google_litellm"
    return "local"


def _transport_mechanism(machine: dict) -> str:
    """"cli" when the turn goes through the Claude Code CLI, else "direct_api".

    backend_kind already draws this line -- it returns "through_claude_code"
    for a CLI-spawned turn and "direct"/"ssh-proxy"/"proxy" otherwise -- so
    this is a rename into the spec's vocabulary rather than a new judgement.
    The spec is explicit that this badge stays separate from the model badge,
    so they are two fields here and never one string.
    """
    from shared import backend_kind
    kind = backend_kind(machine)
    # "ssh-proxy" is a CLI turn too -- claude_proxy.py on the far side spawns
    # the same `claude` binary, so the mechanism is identical and only the
    # host differs. An earlier version compared against "through_claude_code"
    # alone and labelled every transport-routed backend as a direct API call,
    # which is the opposite of what happens: on this deployment those are the
    # CLI turns. "proxy" is the legacy spelling of the same thing.
    return "cli" if kind in ("through_claude_code", "ssh-proxy", "proxy") else "direct_api"


def _comms(chat: dict) -> str:
    """How the operator can reach this agent: "text", "voice" or "both"."""
    return "both" if chat.get("voice_mode") else "text"


def _agent_state(status: str, *, degraded: bool = False,
                 has_question: bool = False, running: bool = False) -> str:
    """The spec's state vocabulary, alongside the map's own `status`.

    Three values matter to an operator scanning the dashboard, and they are
    not the same question the seven-value `status` answers:

    * ``waiting_for_input`` -- it has asked something and stopped. This is the
      one worth an external notification, and the only one the operator can
      clear.
    * ``blocked`` -- it stopped and cannot continue on its own.
    * ``running`` -- mid-turn.

    ``has_question`` is passed in rather than read here: the caller resolves
    it once per request from the pending-question scan, which reads
    transcripts and must not be run per node.
    """
    if degraded or status == "error":
        return "blocked"
    if has_question or status == "waiting":
        return "waiting_for_input"
    if running or status in ("running", "busy"):
        return "running"
    return "idle"


def _load_index(sample: dict | None) -> float | None:
    """A single 0..1 saturation number for a hub's glow, or None if unknown.

    The spec asks for "combined CPU/RAM load: cool/blue when healthy, warm/red
    as it saturates", so the colour needs one number rather than two. Taken as
    the worse of CPU and memory rather than their average: a host at 100%
    memory and 10% CPU is saturated, and averaging would paint it as healthy.
    """
    if not sample:
        return None
    try:
        cpu = float(sample.get("cpu_pct") or 0.0)
        mem = float(sample.get("mem_pct") or 0.0)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, max(cpu, mem) / 100.0))


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
    # Dashboard fields. The provider family and mechanism come from the
    # backend actually serving this conversation, so a chat pinned to the
    # gateway and one on the official API are told apart even inside the same
    # transport group.
    if machine:
        node["provider_family"] = _provider_family(machine)
        node["transport_mechanism"] = _transport_mechanism(machine)
        node["model_label"] = chat.get("model") or machine.get("model") or ""
    node["comms"] = _comms(chat)
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
    # The spec's state vocabulary, alongside `status` rather than replacing
    # it: `status` drives the existing colours and seven tests assert its
    # values, while this answers the different question the dashboard asks --
    # does this agent need me.
    node["agent_state"] = _agent_state(
        node["status"], degraded=bool(chat.get("degraded")),
    )
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
