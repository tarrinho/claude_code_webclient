// Voice conversation: mic capture, hands-free Live Conversation, TTS
// playback, barge-in. Ported from voice-chat-app's speech-recognition.js /
// thinking-sound.js / app.js, adapted to drive WebConsole's existing
// send(forwardContent) and conversation rendering instead of a bare fetch.
// Kept in its own file since conversation.js (1363 lines) and app.js (2254
// lines) are both already over this project's 300-line-per-file cap.

import {apiFetch} from './api.js?v=1';
import {showToast} from './app.js?v=55';

let voiceMicBtn = document.getElementById('voiceMicBtn');
let voiceLiveBtn = document.getElementById('voiceLiveBtn');
let voiceStopBtn = document.getElementById('voiceStopBtn');
let voiceSendBtn = document.getElementById('sendBtn');

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
const voiceSummarizeBtn = document.getElementById('voiceSummarizeBtn');
const voiceRejectBtn = document.getElementById('voiceRejectBtn');

// Parent chat state captured when voice opens.
let voiceParentState = null;
let voiceTempChatId = null;
let voiceStreamDone = false;
let voiceConversationComplete = false;
let _voiceAssistantDiv = null;   // single div that accumulates assistant text during a stream

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

  // Show "loading context" feedback IMMEDIATELY so the user sees something
  // is happening before the async API call even starts.
  voiceOverlay.hidden = false;
  voiceTooltipMessages.innerHTML = '';
  voiceTooltipTitle.textContent = `Voice: ${chat.title}`;
  const loadingMsg = document.createElement('div');
  loadingMsg.className = 'voice-status';
  loadingMsg.textContent = 'Using pre-existing conversation context in voice chat…';
  voiceTooltipMessages.appendChild(loadingMsg);

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
      voiceTooltipMessages.innerHTML = '';
      return;
    }
    const data = await response.json();
    voiceTempChatId = data.id;
  } catch {
    showToast('Could not start voice conversation', 'error');
    voiceTooltipMessages.innerHTML = '';
    return;
  }

  // Update global state
  window.state.currentChat = { ...chat, voice_mode: true, is_temporary: true, id: voiceTempChatId };
  window.state.streamState = 'ready';

  // Pause parent: stop any running turn
  if (window.conversationController?.stop) {
    window.conversationController.stop();
  }

  // Replace loading with "waiting for input" message
  voiceTooltipMessages.innerHTML = '';
  const idleMsg = document.createElement('div');
  idleMsg.className = 'voice-status';
  idleMsg.textContent = 'Waiting for your voice input…';
  voiceTooltipMessages.appendChild(idleMsg);

  voiceTooltipInput.value = '';
  voiceTooltipConclusion.hidden = true;
  voiceConversationComplete = false;
  voiceStreamDone = false;
  accumulatedText = '';
  lastFinalChunk = '';

  setVoiceStatus('idle');
  updateVoiceButtonVisibility();
}

// ── Close voice tooltip ──
function closeVoiceTooltip() {
  voiceOverlay.hidden = true;
  window.speechSynthesis.cancel();
  pendingSpeechCount = 0;
  if (recognition && (recognizing || voiceStatus === 'listening')) {
    intentionalStop = true;
    recognition.stop();
  }
  // Restore parent chat
  if (voiceParentState) {
    window.state.currentChat = { ...voiceParentState, voice_mode: false };
    window.state.streamState = voiceParentState.streamState || 'ready';
    // Restore lastAttempt so retry works after voice session
    if (voiceParentState.lastAttempt && window.conversationController) {
      window.conversationController.lastAttempt = voiceParentState.lastAttempt;
    }
    voiceParentState = null;
  }
  // Re-render sidebar so voice-mode badge clears
  if (window.__webConsoleRefresh) {
    window.__webConsoleRefresh(window.state.currentChat?.id);
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

// ── Summarize Only: handoff the summary, display it in tooltip ──
async function voiceHandoffSummarize() {
  if (!voiceTempChatId) return;
  voiceSummarizeBtn.disabled = true;
  voiceTooltipConclusion.innerHTML = '';
  const loading = document.createElement('div');
  loading.className = 'voice-status';
  loading.textContent = 'Generating summary…';
  voiceTooltipConclusion.appendChild(loading);
  voiceTooltipConclusion.hidden = false;
  try {
    const response = await apiFetch(`/api/chats/${voiceTempChatId}/voice/handoff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!response.ok) throw new Error('Handoff failed');
    // Backend returns the summary as a plain string, not JSON
    const summary = await response.text().catch(() => '');
    voiceTooltipConclusion.innerHTML = '';
    const summaryDiv = document.createElement('div');
    summaryDiv.className = 'voice-assistant';
    summaryDiv.textContent = summary || 'Summary generated from voice conversation.';
    voiceTooltipConclusion.appendChild(summaryDiv);
    voiceTooltipConclusion.hidden = false;
    showToast('Summary generated and appended to parent chat');
  } catch (err) {
    voiceTooltipConclusion.innerHTML = '';
    const errDiv = document.createElement('div');
    errDiv.className = 'voice-status';
    errDiv.style.color = '#f87171';
    errDiv.textContent = `Summarize failed: ${err.message}`;
    voiceTooltipConclusion.appendChild(errDiv);
    voiceTooltipConclusion.hidden = false;
    showToast('Summarize failed', 'error');
  } finally {
    voiceSummarizeBtn.disabled = false;
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
voiceSummarizeBtn.addEventListener('click', voiceHandoffSummarize);
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

// ── Mic / Live button handlers (dual binding) ──
// Bug fix: the tooltip mic/live buttons need their OWN click handlers in
// addition to the page buttons. Listeners attached to page buttons at load
// time don't follow when voiceMicBtn is reassigned to the tooltip element.
// Both buttons share the same handler via closures over voiceTempChatId.

// Called from the page mic button (opens tooltip if not in voice mode, else listens)
// and from the tooltip mic button (goes straight to listening).
function startVoiceFromMic() {
  if (voiceStatus !== 'idle') return;
  const chat = window.state?.currentChat;
  if (!chat) return;

  if (chat.voice_mode && voiceTempChatId) {
    // Already in voice tooltip — go straight to listening.
    voiceTooltipMessages.innerHTML = '';
    const msg = document.createElement('div');
    msg.className = 'voice-status';
    msg.textContent = 'Listening…';
    voiceTooltipMessages.appendChild(msg);
    startListening(false);
  } else {
    // Open tooltip first, then listen once the temp chat is created.
    openVoiceTooltip().then(() => {
      if (voiceTempChatId) {
        voiceTooltipMessages.innerHTML = '';
        const msg = document.createElement('div');
        msg.className = 'voice-status';
        msg.textContent = 'Listening…';
        voiceTooltipMessages.appendChild(msg);
        startListening(false);
      }
    });
  }
}

// Page button: wired at load time.
voiceMicBtn.addEventListener('click', startVoiceFromMic);
// Tooltip button: wired at load time so it works regardless of voice_mode state.
voiceTooltipMic.addEventListener('click', startVoiceFromMic);

// Live button: same dual binding — open tooltip if not in voice mode, else listen.
function startVoiceLive() {
  if (voiceStatus !== 'idle') return;
  if (voiceTempChatId) {
    voiceTooltipMessages.innerHTML = '';
    const msg = document.createElement('div');
    msg.className = 'voice-status';
    msg.textContent = 'Listening (hands-free)…';
    voiceTooltipMessages.appendChild(msg);
    startListening(true);
  } else {
    openVoiceTooltip().then(() => {
      if (voiceTempChatId) {
        voiceTooltipMessages.innerHTML = '';
        const msg = document.createElement('div');
        msg.className = 'voice-status';
        msg.textContent = 'Listening (hands-free)…';
        voiceTooltipMessages.appendChild(msg);
        startListening(true);
      }
    });
  }
}

voiceLiveBtn.addEventListener('click', startVoiceLive);
voiceTooltipLive.addEventListener('click', startVoiceLive);

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

// ── Voice overlay: single accumulating div for assistant replies, TTS, conclusion ──

function _ensureAssistantDiv() {
  if (!_voiceAssistantDiv) {
    _voiceAssistantDiv = document.createElement('div');
    _voiceAssistantDiv.className = 'voice-assistant';
    voiceTooltipMessages.appendChild(_voiceAssistantDiv);
  }
  return _voiceAssistantDiv;
}

function _clearAssistantDiv() {
  _voiceAssistantDiv = null;
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
    // Render in tooltip: accumulate in a single div (streaming), never create
    // a new div per chunk which would read as jumpy fragments.
    if (text && voiceOverlay && !voiceOverlay.hidden) {
      _ensureAssistantDiv().textContent += text;
      voiceTooltipMessages.scrollTop = voiceTooltipMessages.scrollHeight;
    }
    // TTS pipeline
    setVoiceStatus('speaking');
    speechBuffer += text;
    flushSpeechBuffer(false);
  },
  onReplyDone() {
    if (!window.state?.currentChat?.voice_mode) return;
    voiceStreamDone = true;
    // Flush remaining buffer
    flushSpeechBuffer(true);
    // Close the stream div and show conclusion buttons
    _clearAssistantDiv();
    if (voiceTempChatId) {
      voiceConversationComplete = true;
      voiceTooltipConclusion.hidden = false;
    }
  },
  onReplyError() {
    if (!window.state?.currentChat?.voice_mode) return;
    voiceStreamDone = true;
    speechBuffer = '';
    _clearAssistantDiv();
    if (pendingSpeechCount === 0) setVoiceStatus('idle');
  },
};

updateVoiceButtonVisibility();
