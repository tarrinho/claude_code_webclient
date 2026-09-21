"""Voice conversation backend: the direct-model-call turn path (bypassing
the claude CLI, a deliberate scoped exception — see
docs/superpowers/specs/2026-09-06-voice-conversation-design.md) and its
usage-timing recording, which also powers the Settings dialog's per-model
average-reply-time display.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from openai import AsyncOpenAI

import db
import routes.db_machines as db_machines
import runner
from shared import backend_kind

_log = logging.getLogger("wc.voice")

# Conversational tone/brevity — adapted from voice-chat-app's SYSTEM_PROMPT.
#
# "You have no tools" was true until the fetch tool landed and is now false
# when a session has a parent conversation. Left uncorrected it would have
# been worse than a stale comment: a model told it has no tools declines to
# call the one it was given, so the tool would have been wired, offered, and
# never used.
VOICE_SYSTEM_PROMPT = (
    "You are a conversational thinking partner in a spoken voice chat. You "
    "cannot run code, edit files, or take any action in the world — you can "
    "only talk and look things up in this conversation's own history. Keep "
    "replies short and natural for speech: plain sentences, no markdown, no "
    "bullet lists, no code blocks."
)

#: Appended only when the fetch tool is actually attached, so a session
#: without a parent is never told about a tool it does not have.
VOICE_FETCH_INSTRUCTION = (
    " You have a fetch_messages tool that returns messages from the "
    "conversation this voice session was opened from, by id range. Prefer "
    "calling it over guessing whenever you are unsure of a specific detail — "
    "a number, a name, a path, or what was decided. Any summary you were "
    "given covers only the most recent part of that conversation, so older "
    "detail is only available through the tool. Say you are checking, keep it "
    "brief, and never read raw message text aloud verbatim."
)


async def record_voice_turn_timing(model: str, ttft_ms: int, total_ms: int) -> None:
    await db.db_conn.execute(
        "INSERT INTO voice_turn_timing (model, ttft_ms, total_ms, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (model, ttft_ms, total_ms, db._now()),
    )
    await db.db_conn.commit()


async def voice_model_timing_averages(active_models: list[str]) -> dict[str, dict]:
    """Rolling 7-day average TTFT per model, for every id in active_models.

    A model with no recorded turns yet is still present in the result
    (avg_ttft_ms=None, turn_count=0) rather than omitted — the Settings UI
    shows "not yet used" for it instead of hiding the option.
    """
    result = {model_id: {"avg_ttft_ms": None, "turn_count": 0} for model_id in active_models}
    if not active_models:
        return result
    # Calculate 7-day cutoff in the same format as db._now() to ensure consistent
    # string comparison. Using SQLite's datetime('now', '-7 days') produces a
    # different format with space separators, which causes lexicographic comparison
    # issues with our 'T'/'Z'-formatted timestamps.
    cutoff_timestamp = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - 7 * 24 * 3600)
    )
    placeholders = ",".join("?" for _ in active_models)
    cur = await db.db_conn.execute(
        f"SELECT model, AVG(ttft_ms) AS avg_ttft, COUNT(*) AS n "
        f"FROM voice_turn_timing "
        f"WHERE model IN ({placeholders}) "
        f"AND recorded_at > ? "
        f"GROUP BY model",
        active_models + [cutoff_timestamp],
    )
    for row in await cur.fetchall():
        result[row["model"]] = {
            "avg_ttft_ms": row["avg_ttft"],
            "turn_count": row["n"],
        }
    return result


def _note_replay_turn(*, chat_id, chat, sent_messages, assistant_text,
                      model_requested, model_served, ttft_ms, total_ms,
                      input_tokens, output_tokens, failed) -> None:
    """Hand one voice turn's replay record to `conversation_recording`.

    What makes a turn replayable is the exact `messages` array that went to
    the model -- the system prompt, the structured parent-context block, and
    the user's prompt -- together with the model and the generation settings.
    The stored `messages` rows hold only the user's prompt and the reply, so
    a benchmark replaying from those would send different input and score the
    difference as a model result.

    **`base_url` and `api_key` are deliberately absent.** This module warns
    twice already that they must never be logged (CLAUDE.md #3, and the SSE
    error path above), and a file on disk is a log by another name. The
    backend is identified by `ai_machine_id`, an opaque row id that says
    which backend without carrying the credential or the URL to reach it.

    Never raises: a replay record is an extra on a turn that has already been
    paid for.
    """
    try:
        import conversation_recording

        conversation_recording.note_turn(chat_id, {
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            "model_requested": model_requested,
            # What the gateway actually served. A gateway may answer with a
            # different model than the one asked for, and a replay compared
            # against the wrong model is worse than no replay.
            "model_served": model_served,
            "ai_machine_id": chat.get("ai_machine_id"),
            "messages_sent": sent_messages,
            "assistant_text": assistant_text,
            # The generation settings, written out rather than assumed. No
            # temperature or max_tokens is set on this path, so the gateway's
            # defaults apply -- which is itself a fact a replay needs, because
            # "unset" and "whatever the default was in September" differ.
            "params": {
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": None,
                "max_tokens": None,
            },
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "ttft_ms": ttft_ms,
            "total_ms": total_ms,
            "failed": failed,
        })
    except Exception:                                # noqa: BLE001
        _log.warning("voice replay record failed for chat_id=%s", chat_id)


async def stream_voice_turn(chat: dict, prompt: str, owner: str):
    """Yields SSE frame strings identical in shape to stream_handler's own
    (type: text/done/error), so the existing frontend parser needs no
    changes. Bypasses the claude CLI entirely -- see the spec's "Deliberate
    exception" section for why this is intentional, not a shortcut.

    Persistence and usage accounting happen once, after the try/except, for
    every attempt that reached the model -- success, a mid-stream failure, or
    a content-empty response -- not just a clean success. CLAUDE.md rule 5
    ("record failures too") applies here exactly as it does to the CLI path:
    an attempt that spent tokens or already spoke part of a reply to the user
    has been paid for and must leave a trace, matching routes/chats.py's
    finish(), which stores a cancelled/failed turn's partial text rather than
    discarding it.
    """
    chat_id = chat["id"]
    model = chat.get("model") or ""
    # runner.get_backend() only works for claude_code provider (it returns
    # {} for everything else). Voice mode uses provider='direct' backends,
    # so we query the DB directly: first get the chat's ai_machine_id from
    # chat_routing, then fetch the machine's base_url/api_key by id.
    machine = None
    try:
        routing = await db_machines.chat_routing(chat_id)
        if routing.get("pinned") and routing.get("machine"):
            machine = routing["machine"]
        elif chat.get("ai_machine_id"):
            machine = await db_machines.ai_machine_backend_by_id(
                chat["ai_machine_id"], owner
            )
    except Exception:
        pass
    base_url = (machine or {}).get("base_url")
    api_key = (machine or {}).get("api_key")
    if not base_url or not model:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Voice chat has no configured model/backend'})}\n\n"
        return
    # get_backend()'s base_url comes from normalise_base_url(), which strips
    # a trailing /v1 for the CLI's Anthropic-messages shape -- the opposite
    # of what an OpenAI-compatible client needs. Never log base_url/api_key.
    if not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    client = AsyncOpenAI(base_url=base_url, api_key=api_key or "unused")
    t0 = time.time()
    ttft_ms = None
    model_served = None
    sent_messages: list[dict[str, str]] = []
    assistant_text = ""
    input_tokens = 0
    output_tokens = 0
    failed = False
    try:
        # Build the message list: system prompt, structured parent context
        # (a lightweight pre-summary of goal, status, decisions, open
        # questions, and named entities), and the current voice prompt.
        # The structured summary is much cheaper than dumping raw messages
        # and gives the model a usable picture of what was discussed before
        # the voice session started.
        parent_id = chat.get("parent_chat_id")
        messages: list[dict[str, str]] = []
        # The summary the session opened with, written once by
        # stream_voice_context. It replaces the keyword heuristic below, which
        # bucketed the parent's last 12 messages by substring match -- a line
        # containing "goal" became GOAL, a quoted string became a NAME -- and
        # so described the conversation only when it happened to be phrased
        # the way the matcher expected.
        #
        # The heuristic is kept solely as a fallback for a session that opened
        # before this landed, or one whose ladder walk came back degraded. In
        # the degraded case a crude frame beats none: the alternative is a
        # model that knows nothing at all about the conversation it is being
        # asked about.
        stored_summary = (chat.get("voice_context") or "").strip()
        if stored_summary:
            messages.append({
                "role": "user",
                "content": (
                    "Here is a summary of the conversation this voice session "
                    "was opened from. Use it as context; do not read it back "
                    "verbatim.\n\n" + stored_summary
                ),
            })
        elif parent_id:
            parent_msgs = await db.messages_get(parent_id)
            if parent_msgs:
                # Take the last ~6 turns (up to 12 messages, 6 pairs).
                recent = parent_msgs[-12:] if len(parent_msgs) > 12 else parent_msgs

                # Build compact structured context from the raw history.
                # Instead of passing every raw message we distill the
                # conversation into goal, current status, key decisions,
                # open questions, and notable names/constraints.  This is
                # a lightweight inline heuristic — no extra model call —
                # that gives the voice model a clear frame of reference.
                lines: list[str] = []

                goal_parts: list[str] = []
                status_parts: list[str] = []
                decisions_parts: list[str] = []
                open_parts: list[str] = []
                entities: dict[str, str] = {}

                for msg in recent:
                    content = msg.get("content") or ""
                    if not content.strip():
                        continue
                    role = msg.get("role", "user")
                    cl = content.strip()[:300]  # cap per-msg length

                    # Heuristics for categorisation (case-insensitive).
                    cl_lower = cl.lower()
                    if any(kw in cl_lower for kw in (
                        "goal", "objective", "trying", "need to",
                        "looking for", "working on", "building",
                    )) and role == "user":
                        goal_parts.append(cl)
                    elif any(kw in cl_lower for kw in (
                        "open question", "unresolved", "unclear",
                        "not sure", "decide between", "consider",
                        "should we",
                    )) and role == "user":
                        open_parts.append(cl)
                    elif any(kw in cl_lower for kw in (
                        "decided", "go with", "chose", "settled",
                        "final decision",
                    )) and role == "assistant":
                        decisions_parts.append(cl)
                    else:
                        status_parts.append(cl)

                    # Extract quoted names, paths, URLs, and model names.
                    for match in re.findall(
                        r'"([^"]{2,60})"', cl
                    ):
                        if len(entities) < 20 and match not in entities:
                            entities[match] = match

                if goal_parts:
                    lines.append(
                        f"  GOAL: {goal_parts[0]}"
                    )
                if status_parts:
                    lines.append(
                        "  STATUS: " + "; ".join(
                            s for s in status_parts[:4]
                        )
                    )
                if decisions_parts:
                    lines.append(
                        "  DECISIONS: " + "; ".join(
                            d for d in decisions_parts[:3]
                        )
                    )
                if open_parts:
                    lines.append(
                        "  OPEN QUESTIONS: " + "; ".join(
                            o for o in open_parts[:3]
                        )
                    )
                if entities:
                    lines.append(
                        f"  NAMES / CONSTRAINTS: "
                        f"{', '.join(entities.values())}"
                    )

                if lines:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Here is a structured summary of the ongoing "
                            "conversation before this voice session started. "
                            "Use it as context for your responses; do not "
                            "read it back verbatim.\n\n"
                            + "\n".join(lines)
                        ),
                    })

        messages.append({"role": "user", "content": prompt})
        # Kept for the replay record below: this is the exact input the model
        # saw, including the structured parent-context block, which is built
        # from the parent chat's then-current last 12 messages and cannot be
        # reconstructed afterwards.
        sent_messages = [dict(m) for m in messages]

        # The fetch tool, bound to the conversation this session was opened
        # from. It exists so the model can look a detail up instead of
        # guessing -- which is what makes a short summary, or no summary at
        # all, survivable rather than a session that knows nothing.
        import voice_context as vc

        fetch_tool = None
        if parent_id:
            # The originating chat, plus the other chats of the same CLI
            # session. Widened from requirement 6's single chat by operator
            # decision on 2026-09-21: several chats routinely share one
            # terminal session, and they are the conversations a voice session
            # opened from one of them is most likely to be asked about.
            #
            # Computed once, here, and closed over -- so the reachable set is
            # fixed when the turn starts and the model has no say in it.
            reachable = [parent_id]
            try:
                parent = await db.chat_get(parent_id, owner)
                siblings = await db.chats_in_session(
                    (parent or {}).get("session_id") or "", owner)
                reachable = list(dict.fromkeys([parent_id, *siblings]))
            except Exception:
                # A failure to widen is not a failure to answer: fall back to
                # the parent alone rather than losing the tool entirely.
                _log.exception("voice fetch scope fell back to parent chat_id=%s",
                               chat_id)

            async def _read(chat_ids, low: int, high: int) -> list[dict]:
                return await db.messages_range_in(chat_ids, low, high)

            async def _read_latest(chat_ids) -> list[dict]:
                return await db.messages_latest_in(chat_ids, 40)

            fetch_tool = vc.make_fetch_tool_async(reachable, _read, _read_latest)

            # Tell the model which ids exist. Without this the tool is
            # unusable for anything but recency: it takes ids, and nothing
            # else in the prompt or the summary says what they are, so a
            # range has to be invented and an invented range returns nothing.
            try:
                low_id, high_id = await db.messages_id_bounds(reachable)
            except Exception:
                low_id = high_id = None
            if low_id is not None:
                fetch_scope = (
                    f" The messages you can read have ids from {low_id} to "
                    f"{high_id}; higher ids are more recent. Omit both ids to "
                    f"get the most recent messages, which is usually what you "
                    f"want for 'the last' or 'the latest' anything."
                )
            else:
                fetch_scope = (
                    " Omit both ids to get the most recent messages."
                )

        # Built here, not earlier: the fetch instruction carries the id range,
        # which is only known once the reachable set has been resolved. Then
        # INSERTED at the front rather than appended -- by this point the
        # summary and the user's prompt are already in the list, and a system
        # message arriving after the prompt is not a system message.
        messages.insert(0, {
            "role": "system",
            "content": VOICE_SYSTEM_PROMPT
            + ((VOICE_FETCH_INSTRUCTION + fetch_scope) if fetch_tool else ""),
        })

        tool_kwargs = {"tools": [vc.FETCH_TOOL_SCHEMA]} if fetch_tool else {}

        # Rounds, not one call: a tool call is answered and the turn
        # continues, so the model can fetch and then speak. Bounded because an
        # unbounded loop is a model that can spend the user's money in a
        # circle -- two fetches before answering is already generous for a
        # spoken exchange, where latency is the whole constraint.
        for _round in range(3):
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                # Asks an OpenAI-compatible gateway to attach a usage object to
                # the final chunk. Not every gateway honours it -- chunk.usage
                # stays None on those, and input_tokens/output_tokens stay 0
                # rather than the turn failing over a missing accounting detail.
                stream_options={"include_usage": True},
                **tool_kwargs,
            )
            # Tool calls arrive in fragments across chunks, keyed by index:
            # the id and name usually in the first, the arguments a character
            # at a time after it. They are accumulated and only executed once
            # the round ends.
            pending: dict[int, dict] = {}
            async for chunk in stream:
                if chunk.choices:
                    choice_delta = chunk.choices[0].delta
                    delta = choice_delta.content
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = int((time.time() - t0) * 1000)
                        assistant_text += delta
                        yield f"data: {json.dumps({'type': 'text', 'content': delta})}\n\n"
                    for call in (getattr(choice_delta, "tool_calls", None) or []):
                        slot = pending.setdefault(
                            call.index, {"id": "", "name": "", "arguments": ""})
                        if call.id:
                            slot["id"] = call.id
                        function = getattr(call, "function", None)
                        if function is not None:
                            if getattr(function, "name", None):
                                slot["name"] = function.name
                            if getattr(function, "arguments", None):
                                slot["arguments"] += function.arguments
                served = getattr(chunk, "model", None)
                if served:
                    model_served = served
                usage = getattr(chunk, "usage", None)
                if usage:
                    # Accumulated across rounds: a turn that fetched twice
                    # spent the tokens of three completions, and recording
                    # only the last would under-report it.
                    input_tokens += usage.prompt_tokens or 0
                    output_tokens += usage.completion_tokens or 0

            if not pending or not fetch_tool:
                break

            messages.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": [
                    {"id": slot["id"], "type": "function",
                     "function": {"name": slot["name"],
                                  "arguments": slot["arguments"] or "{}"}}
                    for slot in pending.values()
                ],
            })
            for slot in pending.values():
                try:
                    args = json.loads(slot["arguments"] or "{}")
                    result = await fetch_tool(args.get("from_id"), args.get("to_id"))
                except Exception as exc:  # noqa: BLE001
                    # A failed lookup is an answer the model can act on, not a
                    # dead turn: it can say it could not find something, which
                    # is far better than the session ending mid-sentence.
                    _log.exception("voice fetch_messages failed chat_id=%s", chat_id)
                    result = {"messages": [], "truncated": False,
                              "note": f"Lookup failed: {exc}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": json.dumps(result),
                })
        if assistant_text.strip():
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        else:
            # A clean completion with no real text is a non-answer, not a
            # success -- the same class the CLI path's _is_non_answer exists
            # to catch (runner.py). Voice has no retry loop yet, so this
            # reports it honestly instead of silently storing an empty
            # assistant message and telling the client the turn succeeded.
            failed = True
            _log.warning("stream_voice_turn empty response chat_id=%s model=%s", chat_id, model)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Voice reply came back empty. Please try again.'})}\n\n"
    except Exception:  # noqa: BLE001 - surfaced to the client as an SSE event
        # Never forward str(exc) to the client: openai/httpx exception text
        # commonly embeds the request URL (connection errors, timeouts, DNS
        # failures), which would leak the resolved gateway's base_url to the
        # browser -- the same class of leak as the 2026-09-02 incident (see
        # CLAUDE.md #3), just via an SSE frame instead of a log line. Log the
        # real exception server-side only; the client gets a generic message.
        _log.exception("stream_voice_turn failed for chat_id=%s", chat_id)
        failed = True
        yield f"data: {json.dumps({'type': 'error', 'error': 'Voice reply failed. Please try again.'})}\n\n"
    finally:
        await client.close()

    total_ms = int((time.time() - t0) * 1000)
    _note_replay_turn(
        chat_id=chat_id,
        chat=chat,
        sent_messages=sent_messages,
        assistant_text=assistant_text,
        model_requested=model,
        model_served=model_served,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        failed=failed,
    )
    if assistant_text.strip():
        await db.messages_batch(chat_id, [("user", prompt), ("assistant", assistant_text)])
    await record_voice_turn_timing(model, ttft_ms or total_ms, total_ms)
    # One read, two uses: the display kind and the billing route come from the
    # same machine record, and asking twice would let them disagree about a
    # backend the user switched mid-turn.
    machine = await db.ai_machine_active(owner)
    await db.usage_record(
        chat_id=chat_id,
        owner_id=owner,
        model=model,
        provider=backend_kind(machine),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=total_ms,
        is_error=failed,
        origin="voice",
        billing_route=db.billing_route_from_machine(machine),
    )


async def _record_voice_conversation(chat_id: str,
                                     summary: str | None) -> None:
    """Write the rolling recording of this voice conversation.

    Called immediately before every `chat_delete` in `voice_handoff`, which is
    the last moment the conversation exists: that call removes the chat, its
    messages and their FTS entries, leaving only a 2-4 sentence summary in the
    parent chat -- and on two of the three paths, not even that.

    `require_voice=False` because the caller has already established this is a
    voice chat by reaching `voice_handoff` at all, and re-reading `voice_mode`
    here would fail for a chat mid-teardown.

    Never raises: losing the recording must not stop the teardown it is part
    of, or a failure here would leave voice chats undeleted and accumulating.
    """
    import conversation_recording

    await conversation_recording.record_conversation(
        chat_id, handoff_summary=summary, require_voice=False)
    # The replay records have been written out; drop them so a long-lived
    # service does not hold a torn-down chat's turns for the rest of its life.
    conversation_recording.forget_turns(chat_id)


async def voice_handoff(chat_id: str, owner: str) -> str | None:
    """Generate a handoff summary from a voice chat's messages, append to parent.

    Returns the id of the created summary message on success, or None on
    failure.  Deletes the voice chat and all its messages after handoff.
    """

    voice_chat = await db.chat_get(chat_id, owner)
    if not voice_chat:
        return None
    parent_id = voice_chat.get("parent_chat_id")
    if not parent_id:
        return None

    # Read voice chat messages (the child/temp chat)
    voice_msgs, _ = await db.messages_page(chat_id, limit=2000)
    if not voice_msgs:
        return None

    # Build context from voice chat messages for summarization
    voice_text_parts = []
    for msg in voice_msgs:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if content:
            voice_text_parts.append(f"{role}: {content}")
    voice_summary = "\n".join(voice_text_parts)[:2000]

    # Get parent's model and backend for summarization
    parent = await db.chat_get(parent_id, owner)
    if not parent:
        return None

    parent_model = parent.get("model") or ""
    if not parent_model:
        # Normal chat may not have a pinned model; fall back to the global default.
        try:
            parent_model = await runner.get_default_model()
            if not parent_model:
                return None
        except Exception:
            return None

    # Query the parent's backend for the summary generation
    parent_backend = None
    try:
        parent_routing = await db_machines.chat_routing(parent_id)
        if parent_routing.get("pinned") and parent_routing.get("machine"):
            parent_backend = parent_routing["machine"]
        elif parent.get("ai_machine_id"):
            parent_backend = await db_machines.ai_machine_backend_by_id(
                parent.get("ai_machine_id"), owner
            )
    except Exception:
        pass

    # Fallback: if parent has no pinned machine, try the active default.
    if not parent_backend:
        try:
            default_machine = await db.ai_machine_active(owner)
            if default_machine:
                parent_backend = await db_machines.ai_machine_backend_by_id(
                    default_machine["id"], owner
                )
        except Exception:
            pass

    parent_base_url = (parent_backend or {}).get("base_url")
    parent_api_key = (parent_backend or {}).get("api_key")
    # The parent's `provider` is deliberately not consulted: this path
    # always speaks to an OpenAI-compatible endpoint (see the URL
    # normalisation below). It used to be read into a local that nothing
    # used, which read as though the provider selected a client.

    if not parent_base_url or not parent_api_key:
        # Fallback: delete the voice chat without handoff summary.
        # Record it first -- there is no summary on this path, so without the
        # recording the conversation leaves no trace at all.
        await _record_voice_conversation(chat_id, None)
        await db.chat_delete(chat_id, owner)
        return None

    # Normalize URL for OpenAI-compatible client
    if not parent_base_url.rstrip("/").endswith("/v1"):
        parent_base_url = parent_base_url.rstrip("/") + "/v1"

    try:
        client = AsyncOpenAI(base_url=parent_base_url, api_key=parent_api_key)
        summary_prompt = (
            "Summarize the following voice conversation in 2-4 concise sentences. "
            "Focus on the key points, decisions, and any action items. "
            "Do not include conversational filler. Output only the summary text.\n\n"
            f"VOICE CONVERSATION:\n{voice_summary}"
        )

        response = await client.chat.completions.create(
            model=parent_model,
            messages=[{"role": "user", "content": summary_prompt}],
            stream=False,
        )

        summary = None
        if response.choices and response.choices[0].message.content:
            summary = response.choices[0].message.content.strip()

        if not summary:
            # Fallback: create a basic summary from the voice messages
            user_msgs = [m for m in voice_msgs if m.get("role") == "user"]
            if user_msgs:
                summary = f"Voice conversation with {len(user_msgs)} exchanges concluded. " \
                    f"Key topics: {'; '.join(m.get('content', '')[:100] for m in user_msgs[:3])}"
            else:
                summary = "Voice conversation concluded."

        # Insert summary as an assistant message in the parent chat
        await db.messages_batch(parent_id, [("assistant", summary)])

        # Record before deleting, with the summary: chat_delete removes the
        # chat's messages and their FTS entries too, so this is the last
        # moment the conversation exists anywhere.
        await _record_voice_conversation(chat_id, summary)

        # chat_delete removes the chat's messages and their FTS entries too.
        await db.chat_delete(chat_id, owner)

        return summary
    except Exception:
        _log.exception("voice_handoff failed for chat_id=%s", chat_id)
        # Still delete the voice chat even if handoff failed -- so record it
        # first. This is the path that loses the most: the summary never got
        # written to the parent either, so the recording is the only thing
        # that will survive this conversation.
        await _record_voice_conversation(chat_id, None)
        await db.chat_delete(chat_id, owner)
        return None


# ── Session context (spec 2026-09-21-voice-session-context-design) ───────────
#
# A voice session opens knowing what the chat it came from is about. The
# summary is produced here, once, at open -- not on the spoken path, which is
# why it runs through the Claude Code CLI like every other caller rather than
# widening this file's direct-API exception. See voice_context's docstring.


async def stream_voice_context(chat: dict, owner: str):
    """Summarise the parent chat, reporting each attempt as an SSE event.

    Yields `status` frames and ends with either `ready` (summary stored) or
    `degraded` (no summary, session opens anyway). Degraded is a normal
    outcome per spec §2, not an error: the fetch tool is what makes it
    survivable, and refusing to open would deny the user the one thing the
    feature is for.
    """
    import voice_context as vc
    from routes.db_delegation import (
        delegation_operational_all, delegation_pin_all, delegation_rows_all,
        rows_to_capability,
    )
    from tiered_delegation import CapabilityTable

    chat_id = chat["id"]
    parent_id = chat.get("parent_chat_id")

    def frame(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    yield frame({"type": "status", "state": vc.STATUS_INITIALISING})

    if not parent_id:
        # Nothing to summarise is not a failure; it is a session opened from
        # no parent, which the UI already allows.
        yield frame({"type": "status", "state": vc.STATUS_DEGRADED,
                     "reason": "no originating conversation"})
        return

    messages = await db.messages_get(parent_id)
    window, truncated = vc.select_window(messages)
    if not window:
        yield frame({"type": "status", "state": vc.STATUS_DEGRADED,
                     "reason": "the conversation has no messages yet"})
        return

    rows = await delegation_rows_all()
    ladder = CapabilityTable(
        rows_to_capability(rows),
        operational=await delegation_operational_all(),
        pins=await delegation_pin_all(),
    ).ladder("comprehension")
    accuracy = {
        r["model"]: r.get("accuracy")
        for r in rows if r.get("task_type") == "comprehension"
    }
    latency = {
        r["model"]: r.get("median_latency_s")
        for r in rows if r.get("task_type") == "comprehension"
    }
    rungs = vc.eligible_rungs(ladder, accuracy)
    if not rungs:
        yield frame({"type": "status", "state": vc.STATUS_DEGRADED,
                     "reason": "no model is available to summarise with"})
        return

    prompt = vc.summary_prompt(window, truncated)
    clock = vc.BudgetClock()
    events: list[dict] = []

    async def run_rung(model: str) -> str | None:
        # owner is passed because this chat_id is a real chat but the rule in
        # CLAUDE.md §2 is to pass it on every chain regardless -- the cost of
        # getting it wrong is a turn that dies on "Not logged in".
        chunks, _ = await runner.run_turn(
            prompt, None, chat.get("work_dir") or ".", chat_id,
            model=model, owner=owner,
        )
        text = "".join(chunks).strip()
        # CLAUDE.md §5: the caller records its own usage, and records failures
        # too -- a rung that ran and returned nothing still spent tokens.
        try:
            from routes.chats import _record_turn_usage
            frame_usage = runner.take_last_usage(chat_id)
            if frame_usage:
                await _record_turn_usage(chat_id, owner, frame_usage,
                                         origin="voice-summary")
        except Exception:
            _log.exception("voice summary usage not recorded chat_id=%s", chat_id)
        return text

    def emit(state: str, model: str | None) -> None:
        events.append({"type": "status", "state": state, "model": model})

    summary = await vc.walk_summary_ladder(
        rungs=rungs, run_rung=run_rung, clock=clock,
        # An unmeasured rung is assumed to fit; refusing it on a missing
        # number would skip a model that might well be fast.
        expected_s=lambda m: float(latency.get(m) or 0.0),
        emit=emit,
    )
    for event in events:
        yield frame(event)

    if summary:
        await db.chat_update(chat_id, owner, voice_context=summary)
        yield frame({"type": "status", "state": vc.STATUS_READY})
    else:
        yield frame({"type": "status", "state": vc.STATUS_DEGRADED,
                     "reason": "no model produced a usable summary in time"})
