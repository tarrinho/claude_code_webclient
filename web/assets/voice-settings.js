// Voice conversation settings: the two "Voice conversation model" / "Voice
// speech rate" rows in the Settings dialog's App tab. Split out of app.js
// (already 2254 lines, over this project's 300-line-per-file cap) rather
// than added to it — see rules.md's "no big files" rule.

import { debugLog } from './app.js?v=11134710';

export function renderVoiceSettingsFields(data, onBackendChange) {
  const backendSelect = document.getElementById('voiceBackendSelect');
  const modelSelect = document.getElementById('voiceModelSelect');
  const rateInput = document.getElementById('voiceSpeechRate');
  const rateValue = document.getElementById('voiceSpeechRateValue');
  if (!modelSelect || !rateInput) return;

  // Populate backend dropdown
  if (backendSelect) {
    backendSelect.innerHTML = '';
    const backends = data.voice_backend_options || [];
    debugLog('[voice-settings] renderVoiceSettingsFields:', {
      has_backend_select: !!backendSelect,
      has_model_select: !!modelSelect,
      voice_backend_id: data.voice_backend_id,
      backend_options_count: backends.length,
      backend_options: backends.slice(0, 3),
    });
    for (const opt of backends) {
      const el = document.createElement('option');
      el.value = opt.id;
      el.textContent = `${opt.name}${opt.provider ? ` (${opt.provider})` : ''}`;
      if (opt.id === data.voice_backend_id) el.selected = true;
      backendSelect.appendChild(el);
    }
    // Repopulate from the payload already in hand, not from a fresh request.
    //
    // This used to call back into app.js, which re-GET /api/settings and
    // read voice_model_options off the response. That could not work: the
    // endpoint derives those from the *stored* backend id, so re-reading it
    // returned the previously saved backend's models however the dropdown
    // had just been changed. Each backend now carries its own `models`, so
    // the correct list is already here and the switch is instant.
    backendSelect.onchange = () => {
      const chosen = backends.find((b) => b.id === backendSelect.value);
      populateModelOptions(
        modelSelect, data.voice_model, chosen ? chosen.models : [],
        backendSelect.value,
      );
      // Still announced, so a caller can refresh anything else that depends
      // on the choice. It is no longer responsible for the model list.
      if (onBackendChange) onBackendChange(backendSelect.value);
    };
  }

  const selectedBackend = (data.voice_backend_options || [])
    .find((b) => b.id === data.voice_backend_id);
  populateModelOptions(
    modelSelect, data.voice_model,
    // The per-backend list is authoritative; voice_model_options is the same
    // data for the stored backend and is the fallback for a payload from a
    // server that predates the per-backend field.
    (selectedBackend && selectedBackend.models) || data.voice_model_options,
    data.voice_backend_id,
  );

  const rate = data.voice_speech_rate ?? 1.0;
  rateInput.value = rate;
  rateValue.textContent = `${Number(rate).toFixed(1)}x`;
  rateInput.oninput = () => {
    rateValue.textContent = `${parseFloat(rateInput.value).toFixed(1)}x`;
  };
}

export function populateModelOptions(modelSelect, selectedModelId, options, backendId) {
  if (!modelSelect) return;

  modelSelect.innerHTML = '';
  const filtered = options || [];
  if (!filtered.length) {
    // Say why the dropdown is empty. A backend whose active_models has never
    // been probed offers nothing, and an empty select with no explanation
    // reads as a broken page rather than an unconfigured backend.
    const el = document.createElement('option');
    el.value = '';
    el.textContent = 'No models declared for this backend';
    el.disabled = true;
    modelSelect.appendChild(el);
    return;
  }
  for (const opt of filtered) {
    const el = document.createElement('option');
    el.value = opt.id;
    const timing = opt.turn_count > 0
      ? `~${(opt.avg_ttft_ms / 1000).toFixed(1)}s avg, ${opt.turn_count} turns`
      : 'not yet used';
    el.textContent = `${opt.id} (${timing})`;
    if (opt.id === selectedModelId) el.selected = true;
    modelSelect.appendChild(el);
  }
}

export function collectVoiceSettingsFields(body, loadedSettings) {
  const backendSelect = document.getElementById('voiceBackendSelect');
  const select = document.getElementById('voiceModelSelect');
  const rateInput = document.getElementById('voiceSpeechRate');

  if (backendSelect && backendSelect.value && backendSelect.value !== loadedSettings?.voice_backend_id) {
    body.voice_backend_id = backendSelect.value;
  }
  if (select && select.value && select.value !== loadedSettings?.voice_model) {
    body.voice_model = select.value;
  }
  if (rateInput) {
    const rate = parseFloat(rateInput.value);
    if (!Number.isNaN(rate) && rate !== loadedSettings?.voice_speech_rate) {
      body.voice_speech_rate = rate;
    }
  }
}
