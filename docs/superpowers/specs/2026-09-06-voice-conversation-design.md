# Voice Conversation — Design

**Status:** Draft, pending user review.
**Origin:** Port of the spoken-conversation feature built and tested standalone in
`voice-chat-app` (sibling project) into WebConsole, as a persisted, real chat.

## Motivation

`voice-chat-app` is a standalone FastAPI + vanilla-JS app: mic capture (Web
Speech API), streaming replies from a bare OpenAI-compatible endpoint, TTS
playback, barge-in. It proved the UX works. This spec brings that experience
into WebConsole as a real feature — a persisted chat, not a toy — using
WebConsole's existing chat/turn/streaming infrastructure rather than
duplicating it.

## Non-goals

- **Not** voice access to a real Claude Code agent with tool/file/code
  capability. Explicitly decided: this is the same tool-free "thinking
  partner" experience voice-chat-app already has, just running through
  WebConsole's persisted-chat pipeline instead of a bare LLM call.
- **Not** a new backend endpoint for sending/streaming messages. WebConsole
  already has both (`conversation.js`'s `send()`, `routes/chats.py`'s
  `stream_handler`/`_api_stream`); voice mode reuses them unchanged.
- **Not** per-conversation voice model/speed override. Both are global App
  settings (see below) for now — reopen this later if it turns out to matter.

## Deliberate exception: direct model connection, not the `claude` CLI

`CLAUDE.md` states a hard rule for this codebase: *"The console never talks
to a model API. It spawns the `claude` CLI and varies its parameters... There
is no second transport, no SDK call, no HTTP client for a model provider
anywhere in this codebase, and adding one is not the way to solve a problem
here."* Voice-mode conversational turns are a **deliberate, scoped exception**
to that rule, made explicitly rather than silently:

- **Why:** voice needs none of what the CLI provides — no tools, no file
  access, no agent capability (see Non-goals above) — and a spoken
  back-and-forth is far more latency-sensitive than typed chat. Spawning a
  subprocess and parsing `stream-json` frames adds overhead a direct
  streaming HTTP call doesn't have, and going through the CLI can only ever
  *restrict* tool access after the fact (`--tools ""`) rather than never
  having the capability in the first place.
- **Scope, precisely:** this exception covers **only** the model call that
  produces a voice conversational reply. It does **not** extend to anything
  else. If a voice conversation ever needs real implementation or research
  work done, that is explicitly out of scope for this feature (see
  Non-goals) — it would be a **separate, normal CLI-routed chat**, not a
  capability bolted onto this one.
- **What's preserved despite bypassing the CLI:** the actual network
  credentials are **not** re-resolved via a new mechanism. Voice turns still
  call the existing `get_backend()` — the same resolution every other chat
  already uses to build CLI env vars — and feed its `base_url`/`api_key`
  directly into an `AsyncOpenAI` client instead of a subprocess environment.
  One resolution mechanism, two different consumers.

## Architecture

Voice mode is a **per-chat flag** that changes how a chat's turns are
invoked, plus a **client-side control panel** that drives WebConsole's
existing send/stream pipeline via voice instead of typing.

```
[Sidebar: 🎙 next to "＋ New conversation"]
        │ creates chat with voice_mode=1
        ▼
[Conversation view — voice_mode chat]
        │
        ├─ Composer row (existing, unmodified): textarea + Send
        │     + 🎙 Mic, 🔁 Live Conversation icons (idle state)
        │     + ⏹ Stop icon (replaces Send, active state only)
        │
        ├─ Recognized speech → conversation.js's existing
        │     send(forcedContent) — it already accepts text directly as
        │     an optional parameter, so the voice code calls
        │     send(recognizedText) instead of populating the textarea
        │     and simulating a click. No new call path, no DOM hack.
        │
        └─ Streamed reply (existing SSE stream_handler, unmodified)
              → rendered as today
              → ALSO fed to speechSynthesis sentence-by-sentence
                (ported from voice-chat-app's app.js/speech-recognition.js)

[Backend: if chat.voice_mode — bypasses runner/claude CLI entirely]
        │ get_backend(chat_id, owner)  (existing resolution, reused as-is)
        │   → base_url, api_key
        ▼
      AsyncOpenAI(base_url, api_key).chat.completions.create(stream=True)
        (same shape as voice-chat-app's main.py; tool-free by construction —
         no Claude Code process is invoked, so there is nothing to restrict)
```

Non-voice chats are completely unaffected: `chat.voice_mode` is checked once,
at the top of whatever turn-dispatch code decides how to run a turn, and
voice turns take this new path instead of `runner.run_turn`/`stream_turn`.
Message history still reads/writes the same `messages` table as any other
chat — persistence doesn't change, only how the reply is generated.

## Backend changes

### 1. `chats.voice_mode` column

New column, same migration pattern already used for other `chats`/`ai_machines`
columns in `db.py` (`PRAGMA table_info` guard + `ALTER TABLE ... ADD COLUMN`):

```sql
ALTER TABLE chats ADD COLUMN voice_mode INTEGER NOT NULL DEFAULT 0
```

Set once at chat creation (from the sidebar's voice button), immutable after
— no mid-conversation toggle in this pass.

### 2. Tool-free by construction, not by flag

No Claude Code process is invoked for a voice turn at all (see the
exception section above), so there is no tool-execution capability to
disable in the first place — nothing equivalent to `--tools ""` is needed.
Conversational tone/brevity is a plain system message in the
`chat.completions.create` call, adapted directly from voice-chat-app's
`SYSTEM_PROMPT`:

```
You are a conversational thinking partner in a spoken voice chat. You have
no tools, no file access, and cannot run code or take any action of any
kind — you can only talk. Keep replies short and natural for speech: plain
sentences, no markdown, no bullet lists, no code blocks.
```

(Unlike the CLI path, the "no tools" line is worth keeping here — the model
genuinely has no tool schema available to it in this raw completion call,
but restating it still steers tone/behavior, and costs nothing since it's
just a message, not a flag.)

### 3. Credential + model resolution: reuses `get_backend()`

Voice turns call the existing `get_backend(chat_id, owner)` — unchanged,
the same resolution every other chat already uses — and feed its
`base_url`/`api_key` into an `AsyncOpenAI` client instead of a subprocess
environment. `chat.model` (already an existing per-chat column) is passed
as-is to `chat.completions.create`; voice doesn't need a parallel model
concept at the per-chat level, only at the *default* level (below), same as
every other chat already has a default model.

### 4. Voice model default + speech rate: global App settings, with real timing data

New keys in the existing `db.setting_get`/`setting_set` key-value store
(same mechanism as `session_ttl`, confirmed in `routes/misc.py`), with
`config.py` env-var defaults as fallback, matching every existing App-tab
setting:

- `voice_model` — model id string, defaults to `config.VOICE_MODEL_DEFAULT`
  (env `WC_VOICE_MODEL_DEFAULT`). This is the model a *new* voice chat is
  created with; same reasoning as voice-chat-app defaulting to
  `gpt-5.6-luna` over the much slower shared vLLM model.
- `voice_speech_rate` — float 0.5–5.0, defaults to `1.0`
  (env `WC_VOICE_SPEECH_RATE_DEFAULT`).

**New: `voice_turn_timing` table**, recorded once per completed voice turn
(model id, time-to-first-token ms, total duration ms, timestamp) — this is
also how voice's usage/cost visibility gap (flagged in Security below) gets
closed: without the CLI's automatic `usage`/`total_cost_usd` recording
(`app.py` does this for every CLI turn; a caller that bypasses the CLI
entirely must do it itself, same lesson `CLAUDE.md` documents for the
orchestrator feature skipping this once already), voice needs its own
recording regardless — this table serves both that need and the one below.

**Model combo box shows a real average, not a synthetic ping:** the
Settings dialog's "Voice conversation model" `<select>` labels each option
with its rolling average TTFT computed from `voice_turn_timing`
(`AVG(ttft_ms) WHERE model = ? AND recorded_at > now - 7d`, global across
all users — deliberately not per-user, since per-user data would be too
sparse and the point is real contention/latency patterns, which are shared
infrastructure properties, not personal ones), e.g.:

```
azure_ai/gpt-5.6-luna       (~1.1s avg, 47 turns)
azure_ai/gpt-5.4-mini       (~0.9s avg, 12 turns)
vllm/Qwen3.6-35B-A3B-NVFP4  (~20.2s avg, 3 turns)
vllm/Qwen3.5-0.8B           (not yet used)
```

A model with zero recorded turns shows "(not yet used)" rather than
breaking or hiding the option — it's still selectable, just unmeasured.
The model *list itself* (which ids exist to choose from) still comes from
the gateway's live model list (same idea as voice-chat-app's `GET /models`
— reuse or port that endpoint if WebConsole doesn't already have an
equivalent); the timing annotation is a separate join against
`voice_turn_timing`, not a live benchmark run at settings-page-load time
(too slow, and duplicates data already being collected from real usage).

New Settings dialog fields, in the existing "App" tab, following the
existing `.app-setting-row` markup pattern exactly (label + input + hint):

- "Voice conversation model" — `<select>` as above, with timing annotations.
- "Voice speech rate" — `<input type="range" min="0.5" max="5" step="0.1">`.

## Frontend changes

### Sidebar button

One new icon button (🎙) next to the existing "＋ New conversation" button,
both mobile and desktop sidebar variants (`web/index.html` — two existing
`.new-chat-btn` locations). Creates a new chat with `voice_mode=1` via the
existing chat-creation call, passing the new flag, then navigates to it —
same navigation as clicking "＋ New conversation" today.

### Composer row (voice-mode chats only)

Composer keeps its existing textarea + Send, unmodified. Voice controls are
inserted directly into the same row, not a separate panel:

- **Idle:** `[textarea] [🎙 Mic] [🔁 Live Conversation] [Send]`
- **Active** (listening/thinking/speaking): textarea and Mic/Live disabled
  (grayed out, matching voice-chat-app's existing disable-while-not-idle
  pattern), Send's slot is replaced by **`[⏹ Stop]`** — same spot, not an
  additional button.

Ported near-verbatim from voice-chat-app: `speech-recognition.js`'s state
machine (Mic single-turn vs. Live Conversation hands-free loop, the
silence-timeout turn-conclusion logic, the growing-re-transcription dedup
fix, the "pause only during speaking, not thinking" echo-prevention fix,
the spoken-"stop" barge-in trigger and its own restart-on-unexpected-end
fix), `thinking-sound.js` (the audio cue during "thinking"), and `app.js`'s
sentence-buffered TTS feed. All of this logic is browser-only and has no
dependency on voice-chat-app's backend — it drives WebConsole's existing
`send()` instead.

**Where the TTS hook attaches:** `conversation.js`'s SSE consumption (the
code that appends streamed reply text to the DOM — exact line to be
identified during planning, not this spec) needs one addition: as reply
text arrives, also feed completed sentences to `speechSynthesis`, exactly
mirroring voice-chat-app's `flushSpeechBuffer`/`speakSentence`. Only active
when the current chat's `voice_mode` is true.

**Debug log:** voice-chat-app's `debugLog()`-to-`/debug-log` pattern is a
project-specific dev tool for a single-user standalone app; it does **not**
port into WebConsole as-is (multi-user, real auth, no per-user debug log
file makes sense). Client-side console logging (or WebConsole's existing
logging conventions, if any exist client-side) suffices for this port —
flagged as an open point for the implementation plan, not decided here.

## Security

Extends WebConsole's `rules.md` §8 (signature security-audit stage) threat
model:

| Threat | Source | Sink | Mitigation |
|---|---|---|---|
| Tool-use escape via prompt injection | User's voice-transcribed message | Nowhere — no Claude Code process exists for this turn | Structural, not a flag: voice turns never invoke the CLI at all, so there is no tool-execution capability to escape into in the first place |
| Voice-mode flag tampering | Client-supplied chat-creation request | Turn-dispatch code deciding CLI vs. direct-call path | `voice_mode` is set once at creation and read from the chat's own owner-scoped DB row for every subsequent turn — never trusted from per-turn client input, same pattern as `model`/`ai_machine_id` today |
| Global voice settings changed by non-admin | Settings dialog | `db.setting_set` | Reuses whatever access control already gates the rest of the Settings dialog (session TTL, turn timeout, etc.) — no new authorization surface |
| Credential exposure via the new direct-call path | `get_backend()`'s resolved `api_key` | `AsyncOpenAI` client construction | Same value already flows to the CLI path today (as an env var); this just feeds the identical resolved value to an HTTP client instead — never logged, never returned in a response, same discipline `CLAUDE.md` already mandates for env-based credential handling |
| Voice spend invisible if not recorded | Direct `AsyncOpenAI` call, bypassing the CLI's automatic usage recording | Usage tables | Voice must record its own usage per turn, same as `CLAUDE.md` documents `orchestrator.py` having to learn the hard way — `voice_turn_timing` (added for the model-timing feature) is the natural home for this, not an afterthought |

No new RCE/path-traversal/SQL surface: no new subprocess call at all for
voice turns (they skip subprocess spawning entirely), no new raw SQL (new
column + existing generic settings table + one new append-only timing table).

## Testing

- **Backend:** extend existing chat-creation tests for the `voice_mode`
  flag persisting and round-tripping; test the direct-call path the same
  way voice-chat-app's own `test_chat.py` does — mock
  `AsyncOpenAI.chat.completions.create`, never a real network call. Test
  that non-voice chats are provably unaffected (still dispatch to
  `runner.run_turn`/`stream_turn` exactly as before).
- **Frontend:** WebConsole has no existing browser-automation (QA) layer
  today, unlike voice-chat-app (which built one this session, Playwright +
  a fake `SpeechRecognition`). Options for the plan to decide: (a) port that
  same fake-recognition QA approach into WebConsole for the voice control
  panel specifically, (b) manual-only testing, documented as such rather
  than silently skipped (matching this project's own rules.md convention
  for stages that don't apply). Not decided in this spec — flagged for the
  plan.

## UI/UX decisions (from visual brainstorming)

- Sidebar: small 🎙 icon button beside "＋ New conversation" (not a full
  second button, not a split-button dropdown).
- Composer: icons live in the same row as Send, not a separate always-visible
  or collapsible bar (rejected both of those in favor of this simpler
  option).
- Stop only appears once voice mode is actually active (after Mic or Live
  Conversation is clicked) — not visible at all in the idle state.
- Speed and model are **not** per-conversation controls; both moved to
  Settings → App as global defaults.
- The model combo box shows each option's rolling average reply time
  (from real recorded turns, last 7 days, global across users), not just a
  bare model id — makes the speed/capability trade-off visible at the
  point of choosing, instead of requiring a separate benchmark run.

## Open questions for the implementation plan (not blocking this spec)

1. Exact line(s) in `conversation.js` where the TTS hook attaches.
2. Whether WebConsole already has a `GET /models`-equivalent for the
   gateway's live model list, or whether voice-chat-app's needs porting.
3. Frontend debug-logging approach for the ported voice code (see above).
4. Whether to port voice-chat-app's Playwright QA suite or rely on manual
   testing for the frontend piece.
5. Exact `voice_turn_timing` schema (columns, retention/pruning policy —
   append-only forever vs. rolling window) and the aggregation query's
   precise window (7 days is this spec's starting assumption, not a firm
   requirement).
