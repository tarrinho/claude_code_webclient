// ── Device alerts ─────────────────────────────────────────────────────────────

import {apiFetch} from './api.js?v=1';
import {notifyResult} from './server-stats.js?v=1';
import {listController} from './app.js?v=38';
// Three levels, because on a phone the page is usually not the thing in front
// of you:
//   1. the tab title, which always works and needs no permission;
//   2. a system notification, which on Android reaches the notification
//      shade and needs permission granted from a real tap;
//   3. a short vibration, which is the only one you notice in a pocket.
// Only a RISE in the count fires 2 and 3 -- the orchestrator re-polls every few
// seconds and re-alerting on the same unanswered question would be unusable.
// This file is loaded two ways at once: as its own <script type="module"> in
// index.html, and via `import` from app.js for refreshSupervisor and friends.
// Neither loading path shares app.js's own `const byId` (ES modules do not
// share top-level scope across files), so it needs its own.
const byId = id => document.getElementById(id);
// Pure and stateless -- unlike `state`/`listController`, there is nothing to
// keep in sync by sharing one instance, so a local copy is simpler and just
// as correct as importing app.js's.
const storageGet = key => { try { return localStorage.getItem(key); } catch { return null; } };
const storageSet = (key, value) => { try { localStorage.setItem(key, value); } catch {} };

const BASE_TITLE = 'WebConsole';
// null until the first poll: opening the page must not announce agents that
// were already waiting before you arrived. The first result sets the baseline
// silently, and only a later rise is worth interrupting for.
const SUPERVISOR_POLL_MS = 15000;
let _supervisorTimer = null;
let _lastWaitingCount = null;
// Counted separately from the title's total, because a notification fires on a
// rise in rows that need a person -- not on a rise in the feed. Sharing one
// counter would let a completion arriving alongside an ask mask the ask, or a
// completion on its own be mistaken for one.
let _lastActionableCount = 0;

function _alertsEnabled() {
  return storageGet('wc_alerts') === 'on'
    && typeof Notification !== 'undefined'
    && Notification.permission === 'granted';
}

export function _syncAlertToggle() {
  const button = byId('alertToggle');
  if (!button) return;
  // Hidden entirely where the API does not exist rather than offering a
  // control that cannot work.
  const supported = typeof Notification !== 'undefined';
  button.hidden = !supported;
  if (!supported) return;
  const on = _alertsEnabled();
  button.setAttribute('aria-pressed', String(on));
  button.textContent = on ? '🔔' : '🔕';
  button.title = on
    ? 'Alerts on — you will be notified when an agent needs you'
    : 'Alerts off — tap to be notified when an agent needs you';
}

export async function toggleAlerts() {
  if (typeof Notification === 'undefined') return;
  if (_alertsEnabled()) {
    storageSet('wc_alerts', 'off');
    _syncAlertToggle();
    return;
  }
  // Must be called from the tap itself: Android refuses a permission prompt
  // that is not tied to a user gesture.
  let permission = Notification.permission;
  if (permission === 'default') {
    try {
      permission = await Notification.requestPermission();
    } catch {
      permission = 'denied';
    }
  }
  if (permission === 'granted') {
    storageSet('wc_alerts', 'on');
    notifyResult('Alerts on', 'success');
  } else {
    storageSet('wc_alerts', 'off');
    notifyResult('Android blocked notifications for this site', 'error');
  }
  _syncAlertToggle();
}

export function _applyDeviceAlert(waiting) {
  // The tab title counts everything the attention feed holds, completions
  // included: it is ambient, and reading it costs nothing.
  const count = waiting.length;
  document.title = count ? `(${count}) ${BASE_TITLE}` : BASE_TITLE;

  // A desktop notification is a different instrument. It interrupts a person
  // who is not looking at this machine, so it fires only for rows that need
  // one: an ask, a blocker, a failure. A finished agent is worth *showing* --
  // Pedro's rule is that an ended action is worth surfacing, and the badge and
  // the row both do that -- but "surface it" and "interrupt them wherever they
  // are" are not the same request, and the rule as given said highlight.
  //
  // Without this split, "Done. Suite is green" raised an OS notification on an
  // unfocused machine. That is the case where the promotion does the most work
  // and earns the least, and it is what this function's own contract already
  // said: only when information is required or important.
  const actionable = waiting.filter((entry) => entry.reason !== 'done');
  const rose = _lastWaitingCount !== null && actionable.length > _lastActionableCount;
  _lastWaitingCount = count;
  _lastActionableCount = actionable.length;
  if (!rose || !_alertsEnabled()) return;
  // Looking at the page already counts as being told.
  if (document.visibilityState === 'visible' && document.hasFocus()) return;

  const newest = actionable[actionable.length - 1] || {};
  const who = newest.title || 'An agent';
  // "finished" is its own message. The feed now carries completions as well as
  // asks, and telling someone their finished agent "needs an answer" sends them
  // off to answer nothing -- the same complaint that made the failed-endpoint
  // wording wrong.
  const body = newest.reason === 'blocked'
    ? `${who} is blocked`
    : newest.reason === 'done'
      ? `${who} has finished`
      : `${who} needs an answer`;
  try {
    new Notification('WebConsole', {
      body: newest.preview ? `${body} — ${newest.preview}` : body,
      tag: 'wc-orchestrator',   // replaces its predecessor instead of stacking
      renotify: false,
    });
  } catch {
    // Some Android builds only allow notifications from a service worker.
    // The title badge above still carries the count.
  }
  if (navigator.vibrate) {
    try { navigator.vibrate(200); } catch { /* not supported */ }
  }
}

export async function refreshSupervisor() {
  try {
    const response = await apiFetch('/api/orchestrator');
    if (!response.ok) return;
    const data = await response.json();
    listController.setSupervisor(data);
    _applyDeviceAlert(data.waiting || []);
  } catch {
    // Supervision is supplementary; the sidebar must render without it.
  }
}

// The other half of the note in orchestrator.js: this owns the poller because
// it owns _supervisorTimer and SUPERVISOR_POLL_MS, not because polling is a
// device-alert concern by itself. orchestrator.js only owns the pane/picker UI.
export function startSupervisorPolling() {
  if (_supervisorTimer) return;
  // Deliberately NOT skipped while hidden, unlike the other pollers: the OS
  // notification a few lines up in _applyDeviceAlert exists specifically for
  // an agent finishing or asking something while this tab is not being
  // watched. Pausing the poll here would silence the one case it is for.
  _supervisorTimer = setInterval(refreshSupervisor, SUPERVISOR_POLL_MS);
}

export async function dismissAgent(kind, id) {
  if (!kind || !id) return;
  try {
    await apiFetch('/api/orchestrator/read', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind, id, dismiss: true}),
    });
  } catch {
    notifyResult('Could not remove it from the highlights', 'error');
    return;
  }
  await refreshSupervisor();
}

export async function clearSupervisor() {
  try {
    await apiFetch('/api/orchestrator/read', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({all: true}),
    });
  } catch {
    notifyResult('Could not clear alerts', 'error');
  }
  await refreshSupervisor();
}

export async function markAgentSeen(kind, id) {
  if (!id) return;
  try {
    await apiFetch('/api/orchestrator/read', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind, id}),
    });
  } catch {
    // A failed mark just means it stays listed; nothing to tell the user.
  }
  await refreshSupervisor();
}