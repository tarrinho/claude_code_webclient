// ── Server statistics ─────────────────────────────────────────────────────────
// Host health rather than model spend. Two requests because they answer
// different questions and fail independently: the live snapshot is read
// straight off /proc, while the history comes from the sampler's table and is
// empty until the server has been up for a sampling interval.

import {apiFetch} from './api.js?v=1';
import {showToast, settingsVisible} from './app.js?v=42';

// This file is loaded as its own <script type="module"> in index.html and
// does not share app.js's own `const byId` (ES modules do not share
// top-level scope across files), so it needs its own -- same pattern as
// orchestrator.js/device-alerts.js/usage.js/skills.js.
const byId = id => document.getElementById(id);

// A live reading that never changes is worse than no reading: it looks current
// and is not. The panel refreshes itself while it is on screen, at half the
// sampler's interval so a new stored sample shows up promptly without the page
// asking for data that cannot have changed yet.
const SERVER_POLL_MS = 30000;
let _serverTimer = null;

export function startServerPolling() {
  if (_serverTimer) return;   // never stack intervals on repeated tab clicks
  _serverTimer = setInterval(() => {
    // A hidden tab is not being read, and /proc is not free.
    if (document.visibilityState !== 'visible') return;
    if (byId('panelServer')?.hidden !== false) return;
    loadServer(true);
  }, SERVER_POLL_MS);
}

export function stopServerPolling() {
  if (!_serverTimer) return;
  clearInterval(_serverTimer);
  _serverTimer = null;
}

/**
 * @param {boolean} quiet A background refresh: leave the current reading on
 *   screen while the new one is fetched. Skeletons on every tick would make a
 *   panel that updates itself look like a panel that keeps breaking.
 */
export async function loadServer(quiet = false) {
  const body = byId('serverBody');
  if (!body) return;
  const range = byId('serverRange')?.value || '1';
  const bucket = byId('serverBucket')?.value || 'halfhour';

  const count = byId('serverCount');
  if (!quiet) {
    body.replaceChildren(...Array.from({length: 2}, () => {
      const row = document.createElement('div');
      row.className = 'skill-skeleton';
      return row;
    }));
    if (count) count.textContent = 'Loading…';
  }
  try {
    const [liveResp, histResp] = await Promise.all([
      apiFetch('/api/system'),
      apiFetch(`/api/system/series?days=${encodeURIComponent(range)}` +
               `&bucket=${encodeURIComponent(bucket)}`),
    ]);
    if (!liveResp.ok) throw new Error('Could not read host statistics');
    if (!histResp.ok) throw new Error('Could not load host history');
    const live = await liveResp.json();
    const history = await histResp.json();
    // Lazily imported for the same reason as the statistics module: it is a
    // rarely-opened tab and pure weight in every other page load.
    const {renderServer} = await import('./server.js');
    renderServer(body, {live, history});
    if (count) {
      const cpu = Math.round(live.cpu_pct || 0);
      const mem = Math.round(live.mem_pct || 0);
      count.textContent = `CPU ${cpu}% · memory ${mem}%`;
    }
  } catch (error) {
    // A background refresh keeps what is on screen. One failed poll is not
    // worth replacing a good reading with an error, and the next tick will
    // either recover or the user will reopen the tab and see it properly.
    if (quiet) return;
    if (count) count.textContent = '';
    const notice = document.createElement('div');
    notice.className = 'skills-notice';
    notice.textContent = error.message;
    body.replaceChildren(notice);
  }
}

// Report the outcome of an action inside whichever surface the user is looking
// at. A toast renders in .toast-region (z-index 300) while the settings dialog
// is .dialog-backdrop (z-index 400), so a toast raised from Settings is painted
// underneath the modal overlay -- the result appeared to land on the page
// behind. #settingsStatus is the dialog's own aria-live region, so it is both
// visible and announced.
export function notifyResult(message, type = '') {
  if (settingsVisible) setStatus(message, type === 'error' ? 'error' : 'success');
  else showToast(message, type);
}

export function setStatus(text, type) {
  const el = byId('settingsStatus');
  el.textContent = text;
  el.className = type ? `toast ${type}` : '';
  if (type === 'success') setTimeout(() => { el.textContent = ''; el.className = ''; }, 2000);
}