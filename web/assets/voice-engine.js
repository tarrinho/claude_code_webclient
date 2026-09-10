// Voice engine: mic capture (Web Speech recognition), barge-in, silence
// detection, and TTS sentence-by-sentence playback. Split out of
// voice-conversation.js (2026-09-10) once that file passed 300 lines --
// this is the pure speech-mechanics half; voice-tooltip.js and
// voice-handoff.js are the UI shell and backend-integration halves.
// Ported originally from voice-chat-app's speech-recognition.js /
// thinking-sound.js.

let voiceMicBtn = document.getElementById('voiceMicBtn');
let voiceLiveBtn = document.getElementById('voiceLiveBtn');
let voiceStopBtn = document.getElementById('voiceStopBtn');
let voiceSendBtn = document.getElementById('sendBtn');

/** Re-fetch the four page-button refs by id. Exported rather than letting
 * voice-tooltip.js's closeVoiceTooltip() reassign these `let` bindings
 * directly -- an ES module import cannot write to another module's `let`,
 * only read it or call an exported function that writes it. Kept as a
 * defensive re-fetch (same behavior as the original single-file version)
 * even though the element ids never actually change underneath it. */
export function refreshButtonRefs() {
  voiceMicBtn = document.getElementById('voiceMicBtn');
  voiceLiveBtn = document.getElementById('voiceLiveBtn');
  voiceStopBtn = document.getElementById('voiceStopBtn');
  voiceSendBtn = document.getElementById('sendBtn');
  updateVoiceButtonVisibility();
}

export { voiceMicBtn, voiceLiveBtn };

const SILENCE_TIMEOUT_MS = 2000;
let recognition = null;
let bargeInRecognition = null;
let recognizing = false;
let handsFreeMode = false;
let intentionalStop = false;
let intentionalBargeInStop = false;
let recognitionFatalError = false;
let accumulatedText = '';
let lastFinalChunk = '';
let silenceTimer = null;
let pendingSpeechCount = 0;
let speechBuffer = '';
export let voiceStatus = 'idle'; // idle | listening | thinking | speaking

export function updateVoiceButtonVisibility() {
  const inTurn = voiceStatus !== 'idle';
  const active = Boolean(window.state?.currentChat?.voice_mode);
  voiceMicBtn.hidden = !active;
  voiceLiveBtn.hidden = !active;
  voiceMicBtn.disabled = inTurn;
  voiceLiveBtn.disabled = inTurn;
  voiceStopBtn.hidden = !inTurn;
  voiceSendBtn.hidden = inTurn;
}

export function setVoiceStatus(next) {
  const previous = voiceStatus;
  voiceStatus = next;
  updateVoiceButtonVisibility();
  if (next === 'speaking' && previous !== 'speaking' && recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  if (next === 'speaking' && previous !== 'speaking') startBargeInListening();
  if (next !== 'speaking' && previous === 'speaking') stopBargeInListening();
  if (next === 'idle' && previous !== 'idle' && handsFreeMode) startListening(true);
}

export function performVoiceStop(endConversation) {
  if (endConversation) handsFreeMode = false;
  window.speechSynthesis.cancel();
  pendingSpeechCount = 0;
  if (recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  accumulatedText = '';
  lastFinalChunk = '';
  setVoiceStatus('idle');
}

function mergeFinalChunk(chunk) {
  const trimmedChunk = chunk.trim();
  if (!trimmedChunk) return;
  const lowerChunk = trimmedChunk.toLowerCase();
  if (lastFinalChunk && lowerChunk.startsWith(lastFinalChunk.toLowerCase())) {
    accumulatedText = (
      accumulatedText.slice(0, accumulatedText.length - lastFinalChunk.length) + trimmedChunk
    ).trim();
    lastFinalChunk = trimmedChunk;
    return;
  }
  if (accumulatedText.toLowerCase().endsWith(lowerChunk)) return;
  accumulatedText = (accumulatedText + ' ' + trimmedChunk).trim();
  lastFinalChunk = trimmedChunk;
}

function resetSilenceTimer() {
  if (silenceTimer) clearTimeout(silenceTimer);
  silenceTimer = setTimeout(() => {
    silenceTimer = null;
    const finalText = accumulatedText.trim();
    if (!finalText) return;
    accumulatedText = '';
    lastFinalChunk = '';
    setVoiceStatus('thinking');
    window.__webConsoleSend?.(finalText);
  }, SILENCE_TIMEOUT_MS);
}

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SpeechRecognitionImpl) {
  recognition = new SpeechRecognitionImpl();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onstart = () => { recognizing = true; resetSilenceTimer(); };
  recognition.onresult = (event) => {
    if (voiceStatus !== 'listening') return;
    resetSilenceTimer();
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) mergeFinalChunk(event.results[i][0].transcript);
    }
  };
  recognition.onerror = (event) => {
    if (['not-allowed', 'audio-capture', 'service-not-allowed'].includes(event.error)) {
      recognitionFatalError = true;
      handsFreeMode = false;
    }
  };
  recognition.onend = () => {
    recognizing = false;
    if (voiceStatus !== 'listening') return;
    if (silenceTimer && !recognitionFatalError) {
      try { recognition.start(); return; } catch { recognitionFatalError = true; }
    }
    if (silenceTimer) clearTimeout(silenceTimer);
    silenceTimer = null;
    recognitionFatalError = false;
    setVoiceStatus('idle');
  };

  bargeInRecognition = new SpeechRecognitionImpl();
  bargeInRecognition.continuous = true;
  bargeInRecognition.interimResults = true;
  bargeInRecognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (/\bstop\b/i.test(event.results[i][0].transcript)) {
        performVoiceStop(false);
        return;
      }
    }
  };
  bargeInRecognition.onend = () => {
    if (intentionalBargeInStop) { intentionalBargeInStop = false; return; }
    if (voiceStatus === 'speaking') { try { bargeInRecognition.start(); } catch { /* ignore */ } }
  };
}

function startBargeInListening() { try { bargeInRecognition?.start(); } catch { /* ignore */ } }
function stopBargeInListening() {
  intentionalBargeInStop = true;
  try { bargeInRecognition?.stop(); } catch { intentionalBargeInStop = false; }
}

export function startListening(handsFree) {
  if (!recognition) return;
  handsFreeMode = handsFree;
  recognitionFatalError = false;
  accumulatedText = '';
  if (recognizing) { setVoiceStatus('listening'); return; }
  try { setVoiceStatus('listening'); recognition.start(); }
  catch { handsFreeMode = false; setVoiceStatus('idle'); }
}

/** Stop recognition as part of closing the voice tooltip.
 *
 * Exported rather than letting voice-tooltip.js reach into `recognition`/
 * `recognizing`/`intentionalStop` directly: those are this module's own
 * mutable state, and an ES module import cannot write to another module's
 * `let` bindings -- this keeps the "stop cleanly, mark it intentional"
 * behavior in the one file that owns it.
 */
export function stopListeningForClose() {
  if (recognition && (recognizing || voiceStatus === 'listening')) {
    intentionalStop = true;
    recognition.stop();
  }
}

function speakSentence(sentence) {
  const trimmed = sentence.trim();
  if (!trimmed) return;
  pendingSpeechCount++;
  const utterance = new SpeechSynthesisUtterance(trimmed);
  utterance.rate = window.state?.settings?.voice_speech_rate || 1.0;
  const finish = () => {
    pendingSpeechCount = Math.max(0, pendingSpeechCount - 1);
    if (pendingSpeechCount === 0 && voiceStatus === 'speaking') setVoiceStatus('idle');
  };
  utterance.onend = finish;
  utterance.onerror = finish;
  window.speechSynthesis.speak(utterance);
}

export function flushSpeechBuffer(finalFlush) {
  const sentenceEnd = /[^.!?]*[.!?]+(\s|$)/g;
  let match;
  let consumed = 0;
  while ((match = sentenceEnd.exec(speechBuffer)) !== null) {
    speakSentence(match[0]);
    consumed = sentenceEnd.lastIndex;
  }
  speechBuffer = speechBuffer.slice(consumed);
  if (finalFlush && speechBuffer.trim()) { speakSentence(speechBuffer); speechBuffer = ''; }
}

/** Append a streamed reply chunk to the TTS buffer and speak whatever
 * sentences it completes. Exported (rather than exporting `speechBuffer`
 * itself) for the same reason as stopListeningForClose -- it's a write to
 * this module's own state, called from voice-handoff.js's onReplyChunk. */
export function appendSpeechBuffer(text) {
  speechBuffer += text;
  flushSpeechBuffer(false);
}

/** Discard whatever hasn't been spoken yet -- called from
 * voice-handoff.js's onReplyError, same reasoning as appendSpeechBuffer. */
export function clearSpeechBuffer() {
  speechBuffer = '';
}

/** Clear any transcript left over from a previous voice session, before a
 * new one begins -- called from voice-tooltip.js's openVoiceTooltip, ahead
 * of any actual listening (startListening() does its own reset once
 * listening starts; this covers the window before that). */
export function resetTranscript() {
  accumulatedText = '';
  lastFinalChunk = '';
}

export { pendingSpeechCount };

// The stop button is a pure engine concern -- no tooltip involvement -- so
// it is wired here rather than with the mic/live buttons in
// voice-tooltip.js, which call into openVoiceTooltip()/startListening()
// first.
voiceStopBtn.addEventListener('click', () => performVoiceStop(true));

updateVoiceButtonVisibility();
