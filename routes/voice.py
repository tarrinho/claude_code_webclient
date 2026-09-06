"""Voice conversation backend: the direct-model-call turn path (bypassing
the claude CLI, a deliberate scoped exception — see
docs/superpowers/specs/2026-09-06-voice-conversation-design.md) and its
usage-timing recording, which also powers the Settings dialog's per-model
average-reply-time display.
"""
from __future__ import annotations

import json
import time

from openai import AsyncOpenAI

import db
import runner

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
    """
    chat_id = chat["id"]
    model = chat.get("model") or ""
    backend = await runner.get_backend(chat_id, owner)
    base_url = backend.get("base_url")
    api_key = backend.get("api_key")
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
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": VOICE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                if ttft_ms is None:
                    ttft_ms = int((time.time() - t0) * 1000)
                assistant_text += delta
                yield f"data: {json.dumps({'type': 'text', 'content': delta})}\n\n"
        await db.messages_batch(chat_id, [("user", prompt), ("assistant", assistant_text)])
        total_ms = int((time.time() - t0) * 1000)
        await record_voice_turn_timing(model, ttft_ms or total_ms, total_ms)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an SSE event
        yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
    finally:
        await client.close()
