// ── Voice interrupts (spec §7) ────────────────────────────────────────────────
// Deciding whether a heard phrase is the user telling the model to stop.
//
// Split out of voice-engine.js rather than added to it: that file was at 299
// lines against a 300-line cap, and this is the one piece of it that is pure
// -- no DOM, no recogniser, no speech synthesis, no module state beyond the
// echo history. Which also means it can be tested without a browser, and the
// echo rule is the part most worth testing.

// Whole words only. Substrings must not match, so "stopping" and "waitress"
// do nothing. No confidence threshold: occasional false triggers from
// transcription error are accepted, because a missed "stop" is worse than an
// extra one.
export const INTERRUPT_RE = /\b(stop|wait|pause)\b/i;

// The two voice states an interrupt can reach. `idle` and `listening` are not
// among them: there is nothing to interrupt, and "stop" is then just a word
// the user said.
export const INTERRUPT_STATES = ['thinking', 'speaking'];

// The one false trigger that IS filtered, and the reason it is the only one:
// the model saying "wait a moment" interrupts itself, systematically rather
// than occasionally, and on laptop speakers browser echo cancellation does
// not prevent it. Thresholdless matching is preserved for the user's speech.
//
// The cost is a real "stop" said within three seconds of the model saying
// "stop" being missed. The user says it again, which is a far better failure
// than the model cutting itself off mid-sentence for no visible reason.
export const ECHO_WINDOW_MS = 3000;

let spokenRecently = [];

/** Record a sentence as it becomes audible.
 *
 *  Call this from the utterance's `onstart`, not when it is queued: a queued
 *  sentence can sit for seconds before it is heard, and the window has to
 *  start when the speaker actually said it.
 */
export function rememberSpoken(text) {
  spokenRecently.push({text: String(text).toLowerCase(), at: Date.now()});
}

/** Did the model itself say this word within the echo window? */
export function isSelfEcho(word) {
  const cutoff = Date.now() - ECHO_WINDOW_MS;
  // Pruned on read rather than on a timer: nothing else needs to run, and an
  // unpruned entry can only ever be older than the window, never newer.
  spokenRecently = spokenRecently.filter(entry => entry.at >= cutoff);
  // Whole-word here too, or the model saying "stopped" would suppress the
  // user saying "stop".
  const pattern = new RegExp(`\\b${String(word).toLowerCase()}\\b`, 'i');
  return spokenRecently.some(entry => pattern.test(entry.text));
}

/** The interrupt word in *transcript*, or null if there is nothing to act on.
 *
 *  Null covers both "no interrupt word" and "the model's own voice", which
 *  the caller treats identically -- neither is the user asking for anything.
 */
export function matchInterrupt(transcript) {
  const match = INTERRUPT_RE.exec(String(transcript || ''));
  if (!match) return null;
  if (isSelfEcho(match[1])) return null;
  return match[1].toLowerCase();
}

/** Forget what has been spoken, so a word from the last exchange cannot
 *  suppress the first word of the next. */
export function clearSpokenHistory() {
  spokenRecently = [];
}
