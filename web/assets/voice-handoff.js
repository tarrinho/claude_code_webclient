// Voice handoff: Agree / Summarize Only / Reject after a voice conversation
// concludes, and the window.voiceConversation streaming hooks app.js calls
// into (onReplyChunk/onReplyDone/onReplyError). Split out of
// voice-conversation.js (2026-09-10); see voice-engine.js's header for why.

import {apiFetch} from './api.js?v=2741508';
import {showToast} from './app.js?v=11484994';
import {
  flushSpeechBuffer, appendSpeechBuffer, clearSpeechBuffer, setVoiceStatus,
  updateVoiceButtonVisibility, pendingSpeechCount,
} from './voice-engine.js?v=4274770';
import {
  voiceOverlay, voiceTooltipMessages, voiceTooltipConclusion,
  voiceParentState, voiceTempChatId, closeVoiceTooltip,
} from './voice-tooltip.js?v=10927021';

const voiceAgreeBtn = document.getElementById('voiceAgreeBtn');
const voiceSummarizeBtn = document.getElementById('voiceSummarizeBtn');
const voiceRejectBtn = document.getElementById('voiceRejectBtn');
const voiceConclusionOutput = document.getElementById('voiceConclusionOutput');

/** Write progress, the summary, or an error into the conclusion panel's own
 * output area.
 *
 * Never touches the panel's innerHTML. The three handoff buttons are children
 * of that panel, and clearing it to show text destroyed them along with the
 * label: after one "Summarize Only" the panel was empty and could not be used
 * again without reloading. The references held above would have gone stale
 * too -- they point at the original elements, and their click listeners stay
 * bound to the detached nodes, so even re-creating the markup would produce
 * buttons that do nothing. */
function showConclusionOutput(text, isError) {
  if (!voiceConclusionOutput) return;
  // Unhide *before* writing. `[hidden]{display:none!important}` means an
  // element with the attribute is not rendered, and a live region that
  // changes while unrendered announces nothing -- so with the old order the
  // role="status" on this element was decorative.
  voiceConclusionOutput.hidden = false;
  voiceTooltipConclusion.hidden = false;
  voiceConclusionOutput.classList.toggle('is-error', Boolean(isError));
  voiceConclusionOutput.textContent = text;
}

/** The voice conversation no longer exists, so no handoff button can do
 * anything but fail.
 *
 * routes/voice.py's voice_handoff deletes the chat and its messages on every
 * path it has -- success, missing credentials, and the exception handler --
 * so one POST consumes the conversation whatever the outcome. Leaving the
 * buttons live afterwards was a regression introduced by making them survive:
 * before that they were destroyed, so a second click was impossible. A second
 * click now POSTs against a deleted chat and gets a 400.
 *
 * The tooltip's own close button still works and is the way out from here. */
function markHandoffConsumed() {
  voiceAgreeBtn.disabled = true;
  voiceSummarizeBtn.disabled = true;
  voiceRejectBtn.disabled = true;
}

/** Clear it for a new voice session. Called from resetVoiceHandoffState
 * below, which voice-tooltip.js's openVoiceTooltip already calls -- so the
 * reset stays one call from the tooltip's point of view. */
function clearConclusionOutput() {
  if (!voiceConclusionOutput) return;
  voiceConclusionOutput.textContent = '';
  voiceConclusionOutput.classList.remove('is-error');
  voiceConclusionOutput.hidden = true;
}

let voiceStreamDone = false;          // written, never read elsewhere -- see resetVoiceHandoffState
export let voiceConversationComplete = false;
let _voiceAssistantDiv = null;   // single div that accumulates assistant text during a stream

/** Reset handoff-side state for a new voice session -- called from
 * voice-tooltip.js's openVoiceTooltip. */
export function resetVoiceHandoffState() {
  voiceStreamDone = false;
  voiceConversationComplete = false;
  // A previous session's summary or error must not greet the next one. The
  // panel itself is hidden by openVoiceTooltip; this empties what is inside
  // it, which used to happen for free when the panel was wiped wholesale.
  clearConclusionOutput();
  // And re-arm the buttons markHandoffConsumed() disabled. A new voice
  // conversation is a new chat, so they are live again -- without this, one
  // handoff would leave every later session with three dead buttons, which is
  // the bug this file just fixed wearing different clothes.
  voiceAgreeBtn.disabled = false;
  voiceSummarizeBtn.disabled = false;
  voiceRejectBtn.disabled = false;
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
  showConclusionOutput('Generating summary…', false);
  try {
    const response = await apiFetch(`/api/chats/${voiceTempChatId}/voice/handoff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!response.ok) throw new Error('Handoff failed');
    // routes/chats.py's handle_voice_handoff returns
    // JSONResponse({"ok": True, "summary": result}). Reading it as text put
    // the raw `{"ok":true,"summary":"…"}` on screen -- survivable while the
    // next interaction wiped the panel, permanent now that it does not.
    const data = await response.json().catch(() => ({}));
    const summary = (data.summary || '').trim();
    showConclusionOutput(summary || 'Summary generated from voice conversation.', false);
    showToast('Summary generated and appended to parent chat');
  } catch (err) {
    // Not retryable, and saying so matters: voice_handoff deletes the chat
    // and its messages before returning None on the failure path too, so the
    // conversation is already gone. A retry would 400 for ever, and a comment
    // promising otherwise would send the next reader looking for a bug in the
    // wrong place.
    showConclusionOutput(
      `Summarize failed: ${err.message}. The voice conversation has been discarded.`, true);
    showToast('Summarize failed', 'error');
  } finally {
    // Consumed either way -- see markHandoffConsumed.
    markHandoffConsumed();
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

// The conversation has actually ended -- the stop button, or "stop" spoken
// with the intent to finish. Show the handoff choices only now, and only if a
// reply happened, because with nothing said there is nothing to hand off.
//
// An event rather than a call from voice-engine.js: that module imports
// nothing by design, and voice-handoff.js already imports *from* it, so a
// direct call would make the two circular. This keeps the knowledge where it
// belongs -- the engine knows the conversation stopped, this module knows
// whether anything was said.
document.addEventListener('voice:stopped', (event) => {
  if (!event.detail?.endConversation) return;
  if (!voiceTempChatId || !voiceConversationComplete) return;
  voiceTooltipConclusion.hidden = false;
});

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
      // "There is now something worth handing off", which is what
      // voice-tooltip.js's close handler reads it for -- not "the
      // conversation is over".
      voiceConversationComplete = true;
    }
    // The conclusion panel is NOT shown here. It used to be, and it appeared
    // after every single reply: the panel reads "Conversation complete.
    // Handoff result to parent chat:" while the conversation was still
    // running, and in hands-free mode the mic reopened underneath it. It now
    // waits for the conversation to actually end -- see the voice:stopped
    // listener below.
  },
  onReplyError() {
    if (!window.state?.currentChat?.voice_mode) return;
    voiceStreamDone = true;
    clearSpeechBuffer();
    _clearAssistantDiv();
    if (pendingSpeechCount === 0) setVoiceStatus('idle');
  },
};
