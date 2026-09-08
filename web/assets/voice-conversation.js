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

// ── Voice tooltip: creates a temp chat from the parent, pauses it, shows
//    an overlay with mic/live/stop, and offers Agree / Reject on close ───
const voiceOverlay = document.getElementById('voiceOverlay');
const voiceTooltip = document.getElementById('voiceTooltip');
const voiceTooltipTitle = document.getElementById('voiceTooltipTitle');
const voiceTooltipClose = document.getElementById('voiceTooltipClose');
const voiceTooltipMessages = document.getElementById('voiceTooltipMessages');
const voiceTooltipInput = document.getElementById('voiceTooltipInput');
const voiceTooltipSend = document.getElementById('voiceTooltipSend');
const voiceTooltipFooter = document.getElementById('voiceTooltipFooter');
const voiceTooltipConclusion = document.getElementById('voiceTooltipConclusion');
const voiceTooltipMic = document.getElementById('voiceTooltipMic');
const voiceTooltipLive = document.getElementById('voiceTooltipLive');
const voiceTooltipStop = document.getElementById('voiceTooltipStop');
const voiceAgreeBtn = document.getElementById('voiceAgreeBtn');
const voiceRejectBtn = document.getElementById('voiceRejectBtn');

// Parent chat state captured when voice opens.
let voiceParentState = null;
let voiceTempChatId = null;
let voiceStreamDone = false;
let voiceConversationComplete = false;

// ── Open voice tooltip: creates a temp chat, captures parent state ──
async function openVoiceTooltip() {
  const chat = window.state?.currentChat;
  if (!chat) return;

  // Pause parent: stop current stream, save state
  voiceParentState = {
    id: chat.id,
    title: chat.title,
    streamState: window.state?.streamState || 'ready',
    lastAttempt: window.conversationController?.lastAttempt || null,
  };

  // Create temp voice chat with parent context
  try {
    const response = await apiFetch('/api/chats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: chat.title,
        voice_mode: true,
        is_temporary: true,
        parent_chat_id: chat.id,
      }),
    });
    if (!response.ok) {
      showToast('Could not start voice conversation', 'error');
      return;
    }
    const data = await response.json();
    voiceTempChatId = data.id;
  } catch {
    showToast('Could not start voice conversation', 'error');
    return;
  }

  // Update global state
  window.state.currentChat = { ...chat, voice_mode: true, is_temporary: true };
  window.state.streamState = 'ready';

  // Pause parent: stop any running turn
  if (window.conversationController?.stop) {
    window.conversationController.stop();
  }

  // Reset voice UI
  voiceOverlay.hidden = false;
  voiceTooltipMessages.innerHTML = '';
  voiceTooltipTitle.textContent = `Voice: ${chat.title}`;
  voiceTooltipInput.value = '';
  voiceTooltipConclusion.hidden = true;
  voiceConversationComplete = false;
  voiceStreamDone = false;
  accumulatedText = '';
  lastFinalChunk = '';
  setVoiceStatus('idle');

  // Wire up voice buttons inside tooltip
  voiceMicBtn = voiceTooltipMic;
  voiceLiveBtn = voiceTooltipLive;
  voiceStopBtn = voiceTooltipStop;
  voiceSendBtn = voiceTooltipSend;
  updateVoiceButtonVisibility();
}

// ── Close voice tooltip ──
function closeVoiceTooltip() {
  voiceOverlay.hidden = true;
  window.speechSynthesis.cancel();
  pendingSpeechCount = 0;
  if (recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  // Restore parent chat
  if (voiceParentState) {
    window.state.currentChat = voiceParentState;
    window.state.streamState = voiceParentState.streamState || 'ready';
    // Restore lastAttempt so retry works after voice session
    if (voiceParentState.lastAttempt && window.conversationController) {
      window.conversationController.lastAttempt = voiceParentState.lastAttempt;
    }
    voiceParentState = null;
  }
  voiceTempChatId = null;
  voiceMicBtn = document.getElementById('voiceMicBtn');
  voiceLiveBtn = document.getElementById('voiceLiveBtn');
  voiceStopBtn = document.getElementById('voiceStopBtn');
  voiceSendBtn = document.getElementById('sendBtn');
  updateVoiceButtonVisibility();
}

// ── Handoff: Agree & Apply ──
async function voiceHandoffAgree() {
  if (!voiceTempChatId) return;
  voiceAgreeBtn.disabled = true;
  const parentId = voiceParentState?.id;
  try {
    const response = await apiFetch(`/api/chats/${voiceTempChatId}/voice/handoff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      showToast(data.error || 'Could not handoff to parent chat', 'error');
      closeVoiceTooltip();
      return;
    }
    showToast('Voice conversation applied to parent chat');
    // Restore parent, refresh its messages (handoff appended summary)
    closeVoiceTooltip();
    // Refresh sidebar (temp chat deleted) + parent chat messages
    if (parentId && window.__webConsoleRefresh) {
      window.__webConsoleRefresh(parentId);
    }
  } catch {
    showToast('Could not handoff to parent chat', 'error');
    closeVoiceTooltip();
    if (parentId && window.__webConsoleRefresh) {
      window.__webConsoleRefresh(parentId);
    }
  } finally {
    voiceAgreeBtn.disabled = false;
  }
}

// ── Reject: just close and discard ──
async function voiceHandoffReject() {
  if (!voiceTempChatId) {
    closeVoiceTooltip();
    return;
  }
  const parentId = voiceParentState?.id;
  voiceRejectBtn.disabled = true;
  try {
    await apiFetch(`/api/chats/${voiceTempChatId}`, {
      method: 'DELETE',
    });
  } catch { /* ignore */ }
  showToast('Voice conversation discarded');
  closeVoiceTooltip();
  if (parentId) window.__webConsoleRefresh?.(parentId);
  voiceRejectBtn.disabled = false;
}

// ── Button handlers ──
voiceTooltipClose.addEventListener('click', () => {
  if (voiceConversationComplete) {
    // Post-conclusion: force reject (discard)
    voiceHandoffReject();
  } else if (voiceTempChatId) {
    // Mid-conversation: delete temp, restore parent, refresh sidebar
    const parentId = voiceParentState?.id;
    apiFetch(`/api/chats/${voiceTempChatId}`, {
      method: 'DELETE',
    }).catch(() => {});
    closeVoiceTooltip();
    window.__webConsoleRefresh?.(parentId);
  } else {
    closeVoiceTooltip();
  }
});

voiceAgreeBtn.addEventListener('click', voiceHandoffAgree);
voiceRejectBtn.addEventListener('click', voiceHandoffReject);

// Type to send in voice tooltip
voiceTooltipInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = voiceTooltipInput.value.trim();
    if (text) {
      window.__webConsoleSend?.(text);
      voiceTooltipInput.value = '';
    }
  }
});
voiceTooltipSend.addEventListener('click', () => {
  const text = voiceTooltipInput.value.trim();
  if (text) {
    window.__webConsoleSend?.(text);
    voiceTooltipInput.value = '';
  }
});

// Mic button: if voice_mode is on, open tooltip and start listening
voiceMicBtn.addEventListener('click', async () => {
  if (voiceStatus !== 'idle') return;
  const chat = window.state?.currentChat;
  if (!chat) return;

  // If voice_mode is off, open the tooltip (create temp chat with parent)
  if (!chat.voice_mode) {
    await openVoiceTooltip();
    if (!voiceTempChatId) return;
  }
  startListening(false);
});

voiceLiveBtn.addEventListener('click', () => {
  if (voiceStatus === 'idle') {
    // If in tooltip, start listening; otherwise open tooltip first
    if (!voiceTempChatId) {
      openVoiceTooltip().then(() => { if (voiceTempChatId) startListening(true); });
    } else {
      startListening(true);
    }
  }
});
voiceStopBtn.addEventListener('click', () => performVoiceStop(true));

// Override window.voiceConversation hooks to also render in tooltip
const _origOnReplyChunk = window.voiceConversation?.onReplyChunk;
const _origOnReplyDone = window.voiceConversation?.onReplyDone;
const _origOnReplyError = window.voiceConversation?.onReplyError;

window.voiceConversation = {
  refreshControls() {
    updateVoiceButtonVisibility();
  },
  onReplyChunk(text) {
    // Render in tooltip if active
    if (!voiceOverlay?.hidden && text) {
      const div = document.createElement('div');
      div.className = 'voice-assistant';
      div.textContent = text;
      voiceTooltipMessages.appendChild(div);
      voiceTooltipMessages.scrollTop = voiceTooltipMessages.scrollHeight;
    }
    _origOnReplyChunk?.(text);
  },
  onReplyDone() {
    voiceStreamDone = true;
    if (voiceTempChatId) {
      voiceConversationComplete = true;
      voiceTooltipConclusion.hidden = false;
    }
    _origOnReplyDone?.();
  },
  onReplyError() {
    voiceStreamDone = true;
    _origOnReplyError?.();
  },
};

// ── Original voice button visibility (for non-tooltip use) ──
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
