// ── Server statistics ─────────────────────────────────────────────────────────
// Host health rather than model spend. Two requests because they answer
// different questions and fail independently: the live snapshot is read
// straight off /proc, while the history comes from the sampler's table and is
// empty until the server has been up for a sampling interval.

import {apiFetch} from './api.js?v=2741508';
import {showToast, settingsVisible} from './app.js?v=7392132';

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
    const {renderServer, renderAllHosts, renderHostHistory} =
      await import('./server.js');
    renderServer(body, {live});
    // Three containers, one payload pair, no request per transport. The
    // merged charts need both: the series come from the history response and
    // the host names from the live one, which is the only place a transport's
    // name is known.
    const charts = byId('allHostCharts');
    if (charts) renderAllHosts(charts, {history, transports: live.transports || []});
    _renderTransportStats(byId('transportStats'), live.transports || []);
    const figures = byId('serverHistory');
    if (figures) renderHostHistory(figures, history);
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
/** Fill the Server tab's transport table from stored samples.
 *
 *  Nothing is collected here. The tunnel poller writes a sample per connected
 *  transport on its own interval, and this reads the newest one per host --
 *  which is the whole point: opening the tab costs a database read, not four
 *  SSH round-trips.
 */

function _transportNote(text) {
  const note = document.createElement('div');
  note.className = 'transport-stats-note';
  note.textContent = text;
  return note;
}

function _transportCell(value, unit, hotAt) {
  const cell = document.createElement('span');
  // null/undefined is "no reading", and must not print as 0 -- a zero here
  // reads as an idle host, which is exactly what the unmapped collector keys
  // made every transport look like.
  if (value === null || value === undefined) {
    cell.className = 'transport-stat-none';
    cell.textContent = '--';
    cell.title = 'No reading stored yet';
    return cell;
  }
  cell.className = 'transport-stat-num';
  if (hotAt !== undefined && Number(value) >= hotAt) {
    cell.className += ' transport-stat-hot';
  }
  cell.textContent = unit === '%' ? `${Math.round(value)}%` : Number(value).toFixed(2);
  return cell;
}

function _agoText(stamp) {
  if (!stamp) return 'never';
  const then = new Date(/Z$|[+-]\d\d:?\d\d$/.test(stamp) ? stamp : `${stamp}Z`);
  if (Number.isNaN(then.getTime())) return 'unknown';
  const seconds = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function _renderTransportStats(host, rows) {
  if (!rows.length) {
    host.replaceChildren(_transportNote('No SSH transports configured.'));
    return;
  }
  const head = document.createElement('div');
  head.className = 'transport-stat-row is-head';
  ['Transport', 'CPU', 'Mem', 'Disk', 'Load', 'Sampled'].forEach((label, i) => {
    const cell = document.createElement('span');
    cell.className = i === 0 ? 'transport-stat-name' : 'transport-stat-num';
    cell.textContent = label;
    head.appendChild(cell);
  });

  const body = rows.map(row => {
    const line = document.createElement('div');
    line.className = 'transport-stat-row';
    const name = document.createElement('span');
    name.className = 'transport-stat-name';
    name.textContent = row.name || row.id;
    if (row.ssh_host) name.title = row.ssh_host;
    line.append(
      name,
      _transportCell(row.cpu_pct, '%', 90),
      _transportCell(row.mem_pct, '%', 90),
      _transportCell(row.disk_pct, '%', 90),
      _transportCell(row.load1, 'load'),
    );
    const when = document.createElement('span');
    when.className = 'transport-stat-when';
    when.textContent = _agoText(row.sampled_at);
    if (row.sampled_at) when.title = row.sampled_at;
    line.appendChild(when);
    return line;
  });
  host.replaceChildren(head, ...body);
}


export function notifyResult(message, type = '') {
  if (settingsVisible) setStatus(message, type === 'error' ? 'error' : 'success');
  else showToast(message, type);
}

export function setStatus(text, type) {
  const el = byId('settingsStatus');
  el.textContent = text;
  el.className = type ? `toast ${type}` : '';
  if (type === 'success') {
    setTimeout(() => {
      // Only clear the message THIS call put there. The clear used to be
      // unconditional, so a success scheduled it and then wiped whatever the
      // element held 2s later -- including an error raised in between, which
      // is exactly the sequence a user hits: save a backend (success, clear
      // armed), then delete a transport that is still in use (409). The
      // refusal appeared and vanished within two seconds, leaving a failed
      // delete looking like nothing happened at all. Found while testing the
      // transport delete: the message was written and erased before it could
      // be read.
      if (el.textContent !== text) return;
      el.textContent = '';
      el.className = '';
    }, 2000);
  }
}