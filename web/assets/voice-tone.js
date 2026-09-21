// The thinking tone: a pulse roughly once a second while the voice model is
// working, silent the moment it starts speaking.
//
// Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md §6
//
// Why a tone at all, when there is already a visual "Thinking…" row: a voice
// conversation is the one surface where the user is not looking at the screen.
// The spec makes the tone's ABSENCE the signal that something is wrong, which
// only works if the tone is the thing being listened for -- so this is
// additional to the visual indicator, never a replacement for it.
//
// Why WebAudio rather than an audio file: a file is one more asset to ship,
// to cache-bust, and to fail to load. A failed load would be silence, and
// silence is exactly the fault signal -- so the failure mode of the simpler
// approach is indistinguishable from the thing it is meant to report.
//
// There is deliberately no in-app mute. A control that silences a fault
// signal defeats the reason for having one; the system volume is the mute.

// One context, created lazily. Browsers refuse to start audio before a user
// gesture, and a voice session always begins with one (the mic or live
// button), so by the time this first runs the gesture has happened.
let audioContext = null;
let timer = null;

/** Roughly one pulse per second. Not exactly one: a metronome-perfect beat is
 *  read as a recording or a stuck process, and the point is to sound like
 *  something is working. */
const PULSE_INTERVAL_MS = 950;
const PULSE_MS = 110;
const PULSE_HZ = 440;
/** Quiet enough to sit under speech without masking it, loud enough to hear
 *  across a room. The user is not looking at the screen. */
const PULSE_GAIN = 0.05;

function context() {
  if (audioContext) return audioContext;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  try {
    audioContext = new Ctor();
  } catch {
    audioContext = null;
  }
  return audioContext;
}

/** One pulse: a short sine with its edges ramped.
 *
 *  The ramps are not decoration. Starting and stopping a sine at full
 *  amplitude produces a click at each edge, which over a long wait is far
 *  more irritating than the tone itself and is the usual reason people reach
 *  for the mute this deliberately does not have.
 */
function pulse() {
  const ctx = context();
  if (!ctx) return;
  // A tab backgrounded mid-turn suspends its context; resume is a no-op when
  // it is already running.
  if (ctx.state === 'suspended') ctx.resume?.().catch(() => {});
  const now = ctx.currentTime;
  const oscillator = ctx.createOscillator();
  const gain = ctx.createGain();
  oscillator.type = 'sine';
  oscillator.frequency.value = PULSE_HZ;
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(PULSE_GAIN, now + 0.015);
  gain.gain.setValueAtTime(PULSE_GAIN, now + PULSE_MS / 1000 - 0.015);
  gain.gain.linearRampToValueAtTime(0, now + PULSE_MS / 1000);
  oscillator.connect(gain).connect(ctx.destination);
  oscillator.start(now);
  oscillator.stop(now + PULSE_MS / 1000);
}

/** Start pulsing. Idempotent: calling it while already running does nothing,
 *  so a status setter that fires twice cannot produce a double beat. */
export function startThinkingTone() {
  if (timer) return;
  pulse();                      // immediately, so the wait is never silent
  timer = setInterval(pulse, PULSE_INTERVAL_MS);
}

/** Stop pulsing. Safe to call when not running, which is the common case:
 *  every status change calls it. */
export function stopThinkingTone() {
  if (!timer) return;
  clearInterval(timer);
  timer = null;
}

/** Whether the tone is currently running. Exported for tests -- the audible
 *  behaviour is not otherwise observable without a speaker. */
export function thinkingToneRunning() {
  return timer !== null;
}
