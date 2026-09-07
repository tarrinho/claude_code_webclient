# Plan: Voice backend selection dropdown

## Problem
Voice AI machine was hardcoded via `voice_ai_machine_id` setting. User can't switch backend dynamically without editing DB.

## Solution
Replace `voice_ai_machine_id` with `voice_backend_id`. Add a "Voice backend" dropdown in Settings → App tab. Model dropdown populates based on selected machine's `active_models`.

## Files to change

### 1. `routes/misc.py` (GET /api/settings)
- Replace `voice_ai_machine_id` → `voice_backend_id`
- Build `voice_backend_options` from machine list (id, name, provider)
- Build `voice_model_options` from selected machine's `active_models`

### 2. `routes/misc.py` (PATCH /api/settings)
- Store `voice_backend_id` in settings
- Keep backward compat: if `voice_ai_machine_id` was used, treat as deprecated

### 3. `routes/chats.py` (×2 locations)
- Replace `voice_ai_machine_id` → `voice_backend_id` in chat creation and chat patch voice mode pinning

### 4. `config.py`
- Replace `VOICE_AI_MACHINE_ID_DEFAULT` → `VOICE_BACKEND_ID_DEFAULT`

### 5. `web/index.html` (App tab)
- Add `voiceBackendSelect` dropdown before `voiceModelSelect`

### 6. `web/assets/voice-settings.js`
- Add `renderVoiceBackendFields(data, _machines)` — populates machine dropdown
- Add `collectVoiceBackendFields(body, loadedSettings)` — sends backend ID in PATCH
- Change model dropdown to be disabled until backend selected
- Wire model dropdown change to call `renderVoiceModelFields(data)` with selected machine's models

### 7. `web/assets/app.js`
- Import/extend voice settings with machine list from `_machines`
- Pass machines to `renderVoiceBackendFields`
- Wire `collectVoiceBackendFields`

## Data flow
1. GET `/api/settings` → returns `voice_backend_id`, `voice_backend_options` (id/name/provider), `voice_model_options` (id/timing)
2. User picks backend → model dropdown re-renders
3. User picks model → change noted
4. User clicks Save → PATCH `/api/settings` with `voice_backend_id`, `voice_model`, `voice_speech_rate`
5. On chat creation/voice mode toggle → use `voice_backend_id` + `voice_model` to pin chat
