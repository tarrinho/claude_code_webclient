// Voice conversation settings: the two "Voice conversation model" / "Voice
// speech rate" rows in the Settings dialog's App tab. Split out of app.js
// (already 2254 lines, over this project's 300-line-per-file cap) rather
// than added to it — see rules.md's "no big files" rule.

export function renderVoiceSettingsFields(data) {
  const select = document.getElementById('voiceModelSelect');
  const rateInput = document.getElementById('voiceSpeechRate');
  const rateValue = document.getElementById('voiceSpeechRateValue');
  if (!select || !rateInput) return;

  select.innerHTML = '';
  const options = data.voice_model_options || [];
  for (const opt of options) {
    const el = document.createElement('option');
    el.value = opt.id;
    const timing = opt.turn_count > 0
      ? `~${(opt.avg_ttft_ms / 1000).toFixed(1)}s avg, ${opt.turn_count} turns`
      : 'not yet used';
    el.textContent = `${opt.id} (${timing})`;
    if (opt.id === data.voice_model) el.selected = true;
    select.appendChild(el);
  }

  const rate = data.voice_speech_rate ?? 1.0;
  rateInput.value = rate;
  rateValue.textContent = `${Number(rate).toFixed(1)}x`;
  rateInput.oninput = () => {
    rateValue.textContent = `${parseFloat(rateInput.value).toFixed(1)}x`;
  };
}

export function collectVoiceSettingsFields(body, loadedSettings) {
  const select = document.getElementById('voiceModelSelect');
  const rateInput = document.getElementById('voiceSpeechRate');
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
