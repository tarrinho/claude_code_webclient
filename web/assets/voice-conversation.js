// Voice conversation: mic capture, hands-free Live Conversation, TTS
// playback, barge-in. Ported from voice-chat-app's speech-recognition.js /
// thinking-sound.js / app.js, adapted to drive WebConsole's existing
// send(forwardContent) and conversation rendering instead of a bare fetch.
// Kept in its own file since conversation.js (1363 lines) and app.js (2254
// lines) are both already over this project's 300-line-per-file cap.

import {apiFetch} from './api.js?v=1';

const voiceMicBtn = document.getElementById('voiceMicBtn');
const voiceLiveBtn = document.getElementById('voiceLiveBtn');
const voiceStopBtn = document.getElementById('voiceStopBtn');
const voiceSendBtn = document.getElementById('sendBtn');

const SILENCE_TIMEOUT_MS = 1000;
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
let voiceStatus = 'idle'; // idle | listening | thinking | speaking

function updateVoiceButtonVisibility() {
  const inTurn = voiceStatus !== 'idle';
  const active = Boolean(window.state?.currentChat?.voice_mode);
  voiceMicBtn.hidden = !active;
  voiceLiveBtn.hidden = !active;
  voiceMicBtn.disabled = inTurn;
  voiceLiveBtn.disabled = inTurn;
  voiceStopBtn.hidden = !inTurn;
  voiceSendBtn.hidden = inTurn;
}

function setVoiceStatus(next) {
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

function performVoiceStop(endConversation) {
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

function startListening(handsFree) {
  if (!recognition) return;
  handsFreeMode = handsFree;
  recognitionFatalError = false;
  accumulatedText = '';
  if (recognizing) { setVoiceStatus('listening'); return; }
  try { setVoiceStatus('listening'); recognition.start(); }
  catch { handsFreeMode = false; setVoiceStatus('idle'); }
}

voiceMicBtn.addEventListener('click', async () => {
  if (voiceStatus !== 'idle') return;
  const chat = window.state?.currentChat;
  if (!chat) return;
  if (!chat.voice_mode) {
    // Enable voice mode on the current chat so the server routes through
    // stream_voice_turn. Goes through apiFetch, not a bare fetch(): that is
    // what attaches the X-CSRF-Token header CsrfMiddleware requires on every
    // mutating request, and fetch() alone does not reject on a 4xx/5xx, so a
    // rejected PATCH would otherwise look identical to a saved one here.
    try {
      const response = await apiFetch(`/api/chats/${chat.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice_mode: true }),
      });
      if (!response.ok) return; // server rejected — leave mic disabled briefly
      // Refresh the chat in state so voice-mode UI reacts.
      window.state.currentChat = { ...chat, voice_mode: true };
      updateVoiceButtonVisibility();
    } catch {
      return; // network error — leave mic disabled briefly
    }
  }
  startListening(false);
});
voiceLiveBtn.addEventListener('click', () => { if (voiceStatus === 'idle') startListening(true); });
voiceStopBtn.addEventListener('click', () => performVoiceStop(true));

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

function flushSpeechBuffer(finalFlush) {
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

window.voiceConversation = {
  /** Re-read the open conversation and show or hide the voice controls.
   *
   * Called by app.js when a conversation is opened. Visibility is a property
   * of that conversation, not of the voice turn state this module otherwise
   * reacts to, so nothing here would notice the change on its own.
   */
  refreshControls() {
    updateVoiceButtonVisibility();
  },
  onReplyChunk(text) {
    if (!window.state?.currentChat?.voice_mode) return;
    setVoiceStatus('speaking');
    speechBuffer += text;
    flushSpeechBuffer(false);
  },
  onReplyDone() {
    if (!window.state?.currentChat?.voice_mode) return;
    flushSpeechBuffer(true);
  },
  onReplyError() {
    if (!window.state?.currentChat?.voice_mode) return;
    speechBuffer = '';
    if (pendingSpeechCount === 0) setVoiceStatus('idle');
  },
};

updateVoiceButtonVisibility();
