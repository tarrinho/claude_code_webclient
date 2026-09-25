// Voice engine: the recognisers, the voice status, and the transitions
// between them. Split out of voice-conversation.js (2026-09-10) once that
// file passed 300 lines; voice-tooltip.js and voice-handoff.js are the UI
// shell and backend-integration halves.
//
// Three more pieces left this file when the interrupt work (spec §7) pushed
// it past the same cap, and the line each one draws is worth knowing:
// voice-speech.js turns a streamed reply into spoken sentences,
// voice-transcript.js assembles heard chunks into one utterance and times
// the silence, and voice-interrupt.js decides whether a heard phrase is the
// user saying stop. What stays here is what needs the status: the two
// recognisers, and the lifecycle that starts and stops them.
//
// Ported originally from voice-chat-app's speech-recognition.js /
// thinking-sound.js.

// The audible thinking cue this file's own header records as ported from
// voice-chat-app's thinking-sound.js -- it never actually crossed over.
import {startThinkingTone, stopThinkingTone} from './voice-tone.js?v=6299239';
// Spec §7's matching rules, including the self-echo filter. Pure and
// browser-free, so they live apart from the recogniser that uses them.
import {
  INTERRUPT_STATES, matchInterrupt, clearSpokenHistory,
} from './voice-interrupt.js?v=1635187';
// Speech output lives apart from speech input. The engine drives it and is
// told when it runs dry; it never reaches into the sentence queue itself.
import {
  appendSpeechBuffer, cancelSpeech, clearSpeechBuffer, flushSpeechBuffer,
  setSpeechIdleHandler, speechPending,
} from './voice-speech.js?v=14085491';
// Transcript assembly and the silence clock. Same shape as the speech half:
// it owns the text, the engine owns the recogniser and the status.
import {
  clearSilenceTimer, mergeFinalChunk, resetSilenceTimer, resetTranscript,
  setUtteranceHandler, silenceTimerActive,
} from './voice-transcript.js?v=685931';

export {resetTranscript};

// A finished utterance is the moment the turn changes hands.
setUtteranceHandler((text) => {
  setVoiceStatus('thinking');
  window.__webConsoleSend?.(text);
});

// The public surface voice-handoff.js imports from this module. Re-exported
// here rather than repointing that file: which of this pair owns the sentence
// queue is their business, not their caller's.
export {appendSpeechBuffer, clearSpeechBuffer, flushSpeechBuffer, speechPending};

// The one state change the speech half cannot make for itself, because state
// lives here. Registered rather than imported the other way round: this
// module already imports from there, and the reverse would be a cycle.
setSpeechIdleHandler(() => {
  if (voiceStatus === 'speaking') setVoiceStatus('idle');
});

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

let recognition = null;
let bargeInRecognition = null;
let recognizing = false;
let handsFreeMode = false;
let intentionalStop = false;
let intentionalBargeInStop = false;
let recognitionFatalError = false;
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

/** Show or clear the "Thinking…" line in the voice tooltip.
 *
 * Between the silence timeout firing and the first spoken word of the reply
 * there was nothing on screen at all -- several seconds of silence with the
 * mic closed, which reads as the conversation having died. `thinking` was
 * already a voiceStatus value (set in resetSilenceTimer) and nothing had ever
 * rendered it.
 *
 * Its own element, removed by id rather than by clearing the container:
 * voice-handoff.js writes the streamed reply into the same messages area, and
 * wiping it here would delete text the assistant had already said. */
function renderThinkingIndicator(show) {
  const messages = document.getElementById('voiceTooltipMessages');
  if (!messages) return;
  const existing = document.getElementById('voiceThinking');
  if (!show) { existing?.remove(); return; }
  if (existing) return;
  const row = document.createElement('div');
  row.id = 'voiceThinking';
  row.className = 'voice-status voice-thinking';
  const dot = document.createElement('span');
  dot.className = 'voice-thinking-dot';
  dot.setAttribute('aria-hidden', 'true');
  row.appendChild(dot);
  row.appendChild(document.createTextNode('Thinking…'));
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
}

export function setVoiceStatus(next) {
  const previous = voiceStatus;
  voiceStatus = next;
  updateVoiceButtonVisibility();
  // Only while actually thinking: the first spoken sentence moves the status
  // to `speaking`, which is when the indicator has done its job.
  renderThinkingIndicator(next === 'thinking');
  // The audible half of the same signal (spec §6). Driven from the one place
  // status changes, so the tone cannot outlive the state that started it --
  // a beep still pulsing while the model speaks would be worse than none.
  if (next === 'thinking') startThinkingTone();
  else stopThinkingTone();
  // The interrupt recogniser runs across BOTH states an interrupt can reach
  // (spec §7), not only `speaking`. While speaking, a trigger halts the
  // speech; while thinking, it cancels the reply that is on its way, before
  // that reply can start talking seconds after being told not to. Under the
  // old `speaking`-only lifecycle, "stop" said during the pause between
  // asking and the first spoken word was heard by nothing at all.
  const wasInterruptible = INTERRUPT_STATES.includes(previous);
  const isInterruptible = INTERRUPT_STATES.includes(next);
  // The main recogniser stops on the way in. Its work is finished by then --
  // resetSilenceTimer has already sent the text -- and leaving it running
  // would put two SpeechRecognition instances on one microphone.
  if (isInterruptible && !wasInterruptible && recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  if (isInterruptible && !wasInterruptible) startBargeInListening();
  if (!isInterruptible && wasInterruptible) stopBargeInListening();
  if (next === 'idle' && previous !== 'idle' && handsFreeMode) startListening(true);
}

export function performVoiceStop(endConversation) {
  if (endConversation) handsFreeMode = false;
  // Only when the conversation ends. A barge-in stop keeps the history,
  // because the words that were just spoken are exactly the ones still
  // echoing around the room.
  if (endConversation) clearSpokenHistory();
  cancelSpeech();
  if (recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  resetTranscript();
  setVoiceStatus('idle');
  // Announce the stop so voice-handoff.js can offer the handoff choices. It
  // listens rather than being called, because this module imports nothing and
  // voice-handoff.js imports from it -- a direct call would be circular.
  // `endConversation` is passed through so a barge-in stop, which only
  // interrupts the speaking and leaves hands-free mode running, does not look
  // like the end of the conversation.
  document.dispatchEvent(new CustomEvent('voice:stopped', {
    detail: {endConversation: Boolean(endConversation)},
  }));
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
    if (silenceTimerActive() && !recognitionFatalError) {
      try { recognition.start(); return; } catch { recognitionFatalError = true; }
    }
    clearSilenceTimer();
    recognitionFatalError = false;
    setVoiceStatus('idle');
  };

  bargeInRecognition = new SpeechRecognitionImpl();
  bargeInRecognition.continuous = true;
  bargeInRecognition.interimResults = true;
  bargeInRecognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (!matchInterrupt(event.results[i][0].transcript)) continue;
      handleInterrupt();
      return;
    }
  };
  bargeInRecognition.onend = () => {
    if (intentionalBargeInStop) { intentionalBargeInStop = false; return; }
    if (voiceStatus === 'speaking') { try { bargeInRecognition.start(); } catch { /* ignore */ } }
  };
}

/** Act on an interrupt word, differently depending on what it interrupts.
 *
 *  While speaking, the speech is what has to stop. While thinking there is no
 *  speech yet, so the reply on its way is what has to stop -- otherwise it
 *  arrives and starts talking seconds after being told not to, which is the
 *  failure that makes people stop trusting the word.
 */
export function handleInterrupt() {
  if (voiceStatus === 'thinking') window.__webConsoleCancel?.();
  // `false`: an interrupt ends the reply, not the conversation. In hands-free
  // mode setVoiceStatus('idle') reopens the mic, so both states return to
  // listening, which is what makes "wait" usable mid-thought.
  performVoiceStop(false);
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
  resetTranscript();
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

// The stop button is a pure engine concern -- no tooltip involvement -- so
// it is wired here rather than with the mic/live buttons in
// voice-tooltip.js, which call into openVoiceTooltip()/startListening()
// first.
voiceStopBtn.addEventListener('click', () => performVoiceStop(true));

updateVoiceButtonVisibility();
