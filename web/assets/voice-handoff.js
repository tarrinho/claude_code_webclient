// Voice handoff: Agree / Summarize Only / Reject after a voice conversation
// concludes, and the window.voiceConversation streaming hooks app.js calls
// into (onReplyChunk/onReplyDone/onReplyError). Split out of
// voice-conversation.js (2026-09-10); see voice-engine.js's header for why.

import {apiFetch} from './api.js?v=2741508';
import {showToast} from './app.js?v=11134710';
import {
  flushSpeechBuffer, appendSpeechBuffer, clearSpeechBuffer, setVoiceStatus,
  updateVoiceButtonVisibility, pendingSpeechCount,
} from './voice-engine.js?v=7083095';
import {
  voiceOverlay, voiceTooltipMessages, voiceTooltipConclusion,
  voiceParentState, voiceTempChatId, closeVoiceTooltip,
} from './voice-tooltip.js?v=10927021';

const voiceAgreeBtn = document.getElementById('voiceAgreeBtn');
const voiceSummarizeBtn = document.getElementById('voiceSummarizeBtn');
const voiceRejectBtn = document.getElementById('voiceRejectBtn');

let voiceStreamDone = false;          // written, never read elsewhere -- see resetVoiceHandoffState
export let voiceConversationComplete = false;
let _voiceAssistantDiv = null;   // single div that accumulates assistant text during a stream

/** Reset handoff-side state for a new voice session -- called from
 * voice-tooltip.js's openVoiceTooltip. */
export function resetVoiceHandoffState() {
  voiceStreamDone = false;
  voiceConversationComplete = false;
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
export async function voiceHandoffReject() {
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

voiceAgreeBtn.addEventListener('click', voiceHandoffAgree);
voiceSummarizeBtn.addEventListener('click', voiceHandoffSummarize);
voiceRejectBtn.addEventListener('click', voiceHandoffReject);

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
    appendSpeechBuffer(text);
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
    clearSpeechBuffer();
    _clearAssistantDiv();
    if (pendingSpeechCount === 0) setVoiceStatus('idle');
  },
};
