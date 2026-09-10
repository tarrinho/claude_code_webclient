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

// ── Local DOM helpers (server.js has its own copy; modules do not share scope)

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** One card in a srv-cards grid. Mirrors server.js's card() so transport
 *  panels render identically without an import cycle (both files import
 *  from app.js, and server.js imports from stats.js -- circular deps
 *  explode in ESM). */
function _card(grid, {label, value, detail, ratio}) {
  const box = el('div', 'srv-card');
  box.appendChild(el('div', 'srv-card-label', label));
  box.appendChild(el('div', 'srv-card-value', value));
  if (typeof ratio === 'number') {
    const track = el('div', 'srv-meter');
    const fill = el('span', 'srv-meter-fill');
    fill.style.width = `${Math.min(100, Math.max(0, ratio))}%`;
    if (ratio >= 90) fill.className += ' srv-meter-bad';
    else if (ratio >= 70) fill.className += ' srv-meter-warn';
    track.appendChild(fill);
    box.appendChild(track);
  }
  if (detail) box.appendChild(el('div', 'srv-card-detail', detail));
  grid.appendChild(box);
}

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

  const frag = document.createDocumentFragment();
  for (const row of rows) {
    const panel = _renderTransportPanel(row);
    frag.appendChild(panel);
  }
  host.replaceChildren(frag);
}

function _renderTransportPanel(row) {
  const wrapper = document.createElement('div');
  wrapper.className = 'transport-panel';

  const hdr = document.createElement('div');
  hdr.className = 'transport-panel-hdr';
  hdr.textContent = row.name || row.id;
  if (row.ssh_host) {
    const badge = document.createElement('span');
    badge.className = 'transport-panel-ip';
    badge.textContent = row.ssh_host;
    hdr.appendChild(badge);
  }
  wrapper.appendChild(hdr);

  const grid = el('div', 'srv-cards');

  // CPU
  _card(grid, {
    label: 'CPU',
    value: _pct(row.cpu_pct),
    ratio: row.cpu_pct,
    detail: _loadDetail(row),
  });

  // Memory
  if (row.mem_total) {
    _card(grid, {
      label: 'Memory',
      value: _pct(row.mem_pct),
      ratio: row.mem_pct,
      detail: _bytesDetail(row.mem_used, row.mem_total),
    });
  }

  // Disk
  if (row.disk_total) {
    _card(grid, {
      label: 'Disk',
      value: _pct(row.disk_pct),
      ratio: row.disk_pct,
      detail: _bytesDetail(row.disk_used, row.disk_total),
    });
  }

  // Swap
  if (row.swap_pct !== null && row.swap_pct !== undefined && row.swap_pct > 0) {
    _card(grid, {
      label: 'Swap',
      value: _pct(row.swap_pct),
      ratio: row.swap_pct,
      detail: _swapBytesDetail(row),
    });
  }

  // Uptime
  if (row.uptime_s && row.uptime_s > 0) {
    _card(grid, {
      label: 'Uptime',
      value: _duration(row.uptime_s),
      detail: 'since last boot',
    });
  }

  wrapper.appendChild(grid);

  // Hardware info line
  const facts = [];
  if (row.cores && row.cores > 0) {
    facts.push(`${Math.round(row.cores)}t`);
  }
  if (facts.length) {
    wrapper.appendChild(el('p', 'srv-host', facts.join(' · ')));
  }

  return wrapper;
}

function _loadDetail(row) {
  const parts = [];
  if (row.load1 !== null && row.load1 !== undefined) parts.push(row.load1.toFixed(2));
  if (row.load5 !== null && row.load5 !== undefined) parts.push(row.load5.toFixed(2));
  if (row.load15 !== null && row.load15 !== undefined) parts.push(row.load15.toFixed(2));
  const loadStr = parts.length ? ` · load ${parts.join(' ')}` : '';
  return row.cores && row.cores > 0 ? `· ${row.cores}t${loadStr}` : loadStr;
}

function _swapBytesDetail(row) {
  if (row.mem_total && row.mem_total > 0) {
    const swapTotal = row.mem_total * 0.5; // rough estimate
    const swapUsed = (row.swap_pct / 100) * swapTotal;
    return _bytesDetail(swapUsed, swapTotal);
  }
  return '';
}

export function _pct(n) {
  return `${(Number(n) || 0).toFixed(0)}%`;
}

export function _bytesUsed(n) {
  const v = Number(n) || 0;
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GiB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MiB`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KiB`;
  return `${v} B`;
}

function _bytesDetail(used, total) {
  return `${_bytesUsed(used)} of ${_bytesUsed(total)}`;
}

export function _duration(s) {
  const sec = Math.max(0, Math.floor(Number(s) || 0));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${sec}s`;
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