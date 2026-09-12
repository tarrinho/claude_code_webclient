// Voice tooltip: the overlay shell -- creates a temp chat from the parent,
// pauses it, shows mic/live/stop controls, and the mic/live dual-binding
// (page button + tooltip button both call the same handler). Split out of
// voice-conversation.js (2026-09-10); see voice-engine.js's header for why.
// Agree/Summarize/Reject and the streaming hooks live in voice-handoff.js.

import {apiFetch} from './api.js?v=2741508';
import {showToast} from './app.js?v=11134710';
import {
  setVoiceStatus, startListening, updateVoiceButtonVisibility,
  stopListeningForClose, refreshButtonRefs, resetTranscript, voiceStatus,
  voiceMicBtn, voiceLiveBtn,
} from './voice-engine.js?v=7083095';
import {
  resetVoiceHandoffState, voiceConversationComplete, voiceHandoffReject,
} from './voice-handoff.js?v=15895927';

export const voiceOverlay = document.getElementById('voiceOverlay');
const voiceTooltip = document.getElementById('voiceTooltip');
export const voiceTooltipTitle = document.getElementById('voiceTooltipTitle');
const voiceTooltipClose = document.getElementById('voiceTooltipClose');
export const voiceTooltipMessages = document.getElementById('voiceTooltipMessages');
export const voiceTooltipInput = document.getElementById('voiceTooltipInput');
const voiceTooltipSend = document.getElementById('voiceTooltipSend');
const voiceTooltipFooter = document.getElementById('voiceTooltipFooter');
export const voiceTooltipConclusion = document.getElementById('voiceTooltipConclusion');
const voiceTooltipMic = document.getElementById('voiceTooltipMic');
const voiceTooltipLive = document.getElementById('voiceTooltipLive');
const voiceTooltipStop = document.getElementById('voiceTooltipStop');

// Parent chat state captured when voice opens.
export let voiceParentState = null;
export let voiceTempChatId = null;

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
  resetVoiceHandoffState();
  resetTranscript();

  setVoiceStatus('idle');
  updateVoiceButtonVisibility();
}

// ── Close voice tooltip ──
export function closeVoiceTooltip() {
  voiceOverlay.hidden = true;
  window.speechSynthesis.cancel();
  stopListeningForClose();
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
  refreshButtonRefs();
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
