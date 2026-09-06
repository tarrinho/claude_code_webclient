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

[Backend: runner._build_cmd_direct / claude_proxy.py spawn]
        │ if chat.voice_mode:
        │   append --tools ""              (hard tool-free guarantee)
        │   append --append-system-prompt   (conversational tone/brevity)
        ▼
      claude CLI, same as every other WebConsole turn
```

## Backend changes

### 1. `chats.voice_mode` column

New column, same migration pattern already used for other `chats`/`ai_machines`
columns in `db.py` (`PRAGMA table_info` guard + `ALTER TABLE ... ADD COLUMN`):

```sql
ALTER TABLE chats ADD COLUMN voice_mode INTEGER NOT NULL DEFAULT 0
```

Set once at chat creation (from the sidebar's voice button), immutable after
— no mid-conversation toggle in this pass.

### 2. Tool-free guarantee via `--tools ""`

Confirmed via `claude --help`: `--tools ""` disables all tools at the
CLI/process level. This is what "tool-free" actually means here — not a
system-prompt request a model could choose to ignore (WebConsole's own
`rules.md` already lists prompt injection into the CLI as a live threat;
relying on the model to self-restrict would not be a real mitigation).

Both `runner._build_cmd_direct` and `claude_proxy.py`'s spawn (the two
places `CLAUDE.md` says must be kept in sync for any new parameter) read
`chat.voice_mode` and append `--tools ""` when true — same "vary CLI
parameters, not code paths" principle every other backend/model choice in
this codebase already follows.

### 3. Conversational tone via `--append-system-prompt`

Also confirmed available. Appended (not replacing) Claude Code's own default
system prompt, when `voice_mode` is true:

```
You are a conversational thinking partner in a spoken voice chat. Keep
replies short and natural for speech: plain sentences, no markdown, no
bullet lists, no code blocks. Speak as if talking out loud to a person in
the room.
```

(Adapted from voice-chat-app's `SYSTEM_PROMPT` — the "you have no tools"
line is dropped here since `--tools ""` already makes that true at the
process level; no need to also tell the model, which would be redundant and
_slightly_ risks the model second-guessing why it's being told that.)

### 4. Voice model + speech rate: global App settings

New keys in the existing `db.setting_get`/`setting_set` key-value store
(same mechanism as `session_ttl`, confirmed in `routes/misc.py`), with
`config.py` env-var defaults as fallback, matching every existing App-tab
setting:

- `voice_model` — model id string, defaults to `config.VOICE_MODEL_DEFAULT`
  (env `WC_VOICE_MODEL_DEFAULT`). Independent of a chat's regular
  `ai_machine_id`/`model` — voice specifically trades off for reply speed,
  same reasoning as voice-chat-app defaulting to `gpt-5.6-luna` over the
  much slower shared vLLM model.
- `voice_speech_rate` — float 0.5–5.0, defaults to `1.0`
  (env `WC_VOICE_SPEECH_RATE_DEFAULT`).

Read once per voice-mode turn when building the `claude` invocation
(`--model` flag) and sent to the client once per page load (for the
`speechSynthesis` rate) — not on every turn.

New Settings dialog fields, in the existing "App" tab, following the
existing `.app-setting-row` markup pattern exactly (label + input + hint):

- "Voice conversation model" — `<select>`, populated live from the gateway's
  model list (same idea as voice-chat-app's `GET /models` — reuse or port
  that endpoint into WebConsole's backend if it doesn't already have an
  equivalent).
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
| Tool-use escape via prompt injection | User's voice-transcribed message | Claude Code CLI | `--tools ""` enforced at the CLI/process level for every voice-mode turn — not a prompt-level request, a real absence of tools to call |
| Voice-mode flag tampering | Client-supplied chat-creation request | Command-building (`_build_cmd_direct`/`claude_proxy.py`) | `voice_mode` is set once at creation and read from the chat's own owner-scoped DB row for every subsequent turn — never trusted from per-turn client input, same pattern as `model`/`ai_machine_id` today |
| Global voice settings changed by non-admin | Settings dialog | `db.setting_set` | Reuses whatever access control already gates the rest of the Settings dialog (session TTL, turn timeout, etc.) — no new authorization surface |

No new RCE/path-traversal/SQL surface: no new subprocess call shape (same
`claude` CLI, same two spawn sites, additional flags only), no new raw SQL
(new column + existing generic settings table).

## Testing

- **Backend:** extend existing chat-creation tests for the `voice_mode`
  flag persisting and round-tripping; extend `_build_cmd_direct`/
  `claude_proxy.py` command-building tests to assert `--tools ""` and
  `--append-system-prompt` appear only when `voice_mode` is true, never
  otherwise.
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

## Open questions for the implementation plan (not blocking this spec)

1. Exact line(s) in `conversation.js` where the TTS hook attaches.
2. Whether WebConsole already has a `GET /models`-equivalent for the
   gateway's live model list, or whether voice-chat-app's needs porting.
3. Frontend debug-logging approach for the ported voice code (see above).
4. Whether to port voice-chat-app's Playwright QA suite or rely on manual
   testing for the frontend piece.
