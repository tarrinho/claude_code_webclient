// ── Voice speech output ───────────────────────────────────────────────────────
// Turning a streamed reply into spoken sentences.
//
// Split out of voice-engine.js when the interrupt work (spec §7) pushed that
// file past its 300-line cap. This is the half that only ever *speaks*: it
// buffers streamed text, cuts it at sentence boundaries, and hands each
// sentence to the synthesiser. It knows nothing about recognition, voice
// status, or the tooltip.
//
// The one thing it cannot decide for itself is what happens when the last
// sentence finishes, because that is a state change and state lives in the
// engine. So the engine registers a handler instead of this module importing
// it -- voice-engine.js already imports from here, and the reverse import
// would be a cycle.

import {rememberSpoken} from './voice-interrupt.js?v=1635187';

let speechBuffer = '';
let pendingSpeechCount = 0;
let onAllSpoken = null;

/** Register what to do when the last queued sentence finishes. */
export function setSpeechIdleHandler(handler) {
  onAllSpoken = handler;
}

function speakSentence(sentence) {
  const trimmed = sentence.trim();
  if (!trimmed) return;
  pendingSpeechCount++;
  const utterance = new SpeechSynthesisUtterance(trimmed);
  utterance.rate = window.state?.settings?.voice_speech_rate || 1.0;
  const finish = () => {
    pendingSpeechCount = Math.max(0, pendingSpeechCount - 1);
    if (pendingSpeechCount === 0) onAllSpoken?.();
  };
  // `onstart`, not here: a queued sentence can sit for seconds before it is
  // heard, and the echo window has to start when the speaker actually said it
  // (spec §7).
  utterance.onstart = () => rememberSpoken(trimmed);
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

/** Append a streamed reply chunk and speak whatever sentences it completes.
 * Exported rather than exporting `speechBuffer` itself: an ES module import
 * cannot write to another module's `let` binding. */
export function appendSpeechBuffer(text) {
  speechBuffer += text;
  flushSpeechBuffer(false);
}

/** Discard whatever has not been spoken yet -- voice-handoff.js's
 * onReplyError, where the rest of the reply is never coming. */
export function clearSpeechBuffer() {
  speechBuffer = '';
}

/** Stop talking now and forget the queue. Used by performVoiceStop, where
 * both halves matter: cancel() silences what is mid-sentence, and the count
 * has to go with it or the next reply starts against a stale tally. */
export function cancelSpeech() {
  window.speechSynthesis.cancel();
  pendingSpeechCount = 0;
  speechBuffer = '';
}

/** How many sentences are queued or being spoken. Read by voice-handoff.js
 * to tell "the reply failed and nothing is speaking" from "the reply failed
 * but three sentences are still in the air". */
export function speechPending() {
  return pendingSpeechCount;
}
