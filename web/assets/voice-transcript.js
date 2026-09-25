// ── Voice transcript assembly ─────────────────────────────────────────────────
// Turning a stream of recogniser results into one finished utterance, and
// deciding when the user has stopped talking.
//
// Split out of voice-engine.js when the interrupt work (spec §7) pushed that
// file past its 300-line cap. This half owns the text and the silence clock;
// the engine owns the recogniser that feeds it and the status the finished
// utterance changes. It calls out exactly once, through a registered handler,
// when a silence has lasted long enough to mean "that was the question".

const SILENCE_TIMEOUT_MS = 2000;

let accumulatedText = '';
let lastFinalChunk = '';
let silenceTimer = null;
let onUtterance = null;

/** Register what to do with a finished utterance. */
export function setUtteranceHandler(handler) {
  onUtterance = handler;
}

/** Fold one final recogniser chunk into the accumulated text.
 *
 * Chunks overlap: a recogniser re-emits a growing prefix as it refines, so
 * appending blindly produces "what is the what is the database". Two shapes
 * are handled -- the new chunk extending the last one (replace it), and the
 * new chunk already sitting at the end of what we have (drop it).
 */
export function mergeFinalChunk(chunk) {
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

/** Restart the silence clock. Called on every result, so the user pausing
 *  mid-sentence does not count as having finished. */
export function resetSilenceTimer() {
  if (silenceTimer) clearTimeout(silenceTimer);
  silenceTimer = setTimeout(() => {
    silenceTimer = null;
    const finalText = accumulatedText.trim();
    if (!finalText) return;
    accumulatedText = '';
    lastFinalChunk = '';
    onUtterance?.(finalText);
  }, SILENCE_TIMEOUT_MS);
}

/** Whether a silence is currently being timed.
 *
 * The engine reads this in `onend` to tell a recogniser that stopped
 * mid-utterance (restart it) from one that stopped because the turn is over
 * (let it go). Exported as a question rather than as the timer handle: the
 * handle is this module's to clear.
 */
export function silenceTimerActive() {
  return silenceTimer !== null;
}

export function clearSilenceTimer() {
  if (silenceTimer) clearTimeout(silenceTimer);
  silenceTimer = null;
}

/** Drop any half-assembled utterance. */
export function resetTranscript() {
  accumulatedText = '';
  lastFinalChunk = '';
}
