"""Voice conversation backend: the direct-model-call turn path (bypassing
the claude CLI, a deliberate scoped exception — see
docs/superpowers/specs/2026-09-06-voice-conversation-design.md) and its
usage-timing recording, which also powers the Settings dialog's per-model
average-reply-time display.
"""
from __future__ import annotations

import json
import logging
import time

from openai import AsyncOpenAI

import db
import routes.db_machines as db_machines
from shared import backend_kind

_log = logging.getLogger("wc.voice")

# Conversational tone/brevity — adapted from voice-chat-app's SYSTEM_PROMPT.
# Kept even though this path genuinely has no tool schema available to the
# model (unlike the CLI path, where --tools "" makes the same true and this
# line would be redundant): it still steers tone, and costs nothing here.
VOICE_SYSTEM_PROMPT = (
    "You are a conversational thinking partner in a spoken voice chat. You "
    "have no tools, no file access, and cannot run code or take any action "
    "of any kind — you can only talk. Keep replies short and natural for "
    "speech: plain sentences, no markdown, no bullet lists, no code blocks."
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
    assistant_text = ""
    input_tokens = 0
    output_tokens = 0
    failed = False
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": VOICE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=True,
            # Asks an OpenAI-compatible gateway to attach a usage object to
            # the final chunk. Not every gateway honours it -- chunk.usage
            # stays None on those, and input_tokens/output_tokens stay 0
            # rather than the turn failing over a missing accounting detail.
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    if ttft_ms is None:
                        ttft_ms = int((time.time() - t0) * 1000)
                    assistant_text += delta
                    yield f"data: {json.dumps({'type': 'text', 'content': delta})}\n\n"
            usage = getattr(chunk, "usage", None)
            if usage:
                input_tokens = usage.prompt_tokens or 0
                output_tokens = usage.completion_tokens or 0
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
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an SSE event
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
    if assistant_text.strip():
        await db.messages_batch(chat_id, [("user", prompt), ("assistant", assistant_text)])
    await record_voice_turn_timing(model, ttft_ms or total_ms, total_ms)
    await db.usage_record(
        chat_id=chat_id,
        owner_id=owner,
        model=model,
        provider=backend_kind(await db.ai_machine_active(owner)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=total_ms,
        is_error=failed,
        origin="voice",
    )
