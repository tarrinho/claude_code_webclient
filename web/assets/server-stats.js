// ── Server statistics ─────────────────────────────────────────────────────────
// Host health rather than model spend. Two requests because they answer
// different questions and fail independently: the live snapshot is read
// straight off /proc, while the history comes from the sampler's table and is
// empty until the server has been up for a sampling interval.

import {apiFetch} from './api.js?v=2741508';
import {showToast, settingsVisible} from './app.js?v=10621393';

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
      // A range ending in "h" is shorter than a day and travels as `hours`;
      // sending it as `days=1h` would parse to one day on the server, which
      // is a working request for the wrong window.
      apiFetch(`/api/system/series?${range.endsWith('h')
                 ? `hours=${encodeURIComponent(range.slice(0, -1))}`
                 : `days=${encodeURIComponent(range)}`}` +
               `&bucket=${encodeURIComponent(bucket)}`),
    ]);
    if (!liveResp.ok) throw new Error('Could not read host statistics');
    if (!histResp.ok) throw new Error('Could not load host history');
    const live = await liveResp.json();
    const history = await histResp.json();
    // The server coarsens a width the window cannot draw. Put back what it
    // actually used, so the control does not name a grouping nobody is
    // looking at -- same reasoning as the Statistics tab.
    const usedBucket = history.bucket;
    const bucketSelect = byId('serverBucket');
    if (usedBucket && bucketSelect && bucketSelect.value !== usedBucket) {
      bucketSelect.value = usedBucket;
    }
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
    // Render the cleanup panel on first tab open only (marked by the
    // _cleaned-up attribute). The 30s poll must never overwrite an already-
    // scanned panel, because that would destroy the results the user just saw.
    const cleanupPanel = byId('cleanupPanel');
    if (cleanupPanel && !cleanupPanel.hasAttribute('data-cleaned-up')) {
      _renderCleanupDefault(cleanupPanel);
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
    value: _pctOrDash(row.cpu_pct),
    ratio: row.cpu_pct,
    detail: _loadDetail(row),
  });

  // Memory
  if (row.mem_total) {
    _card(grid, {
      label: 'Memory',
      value: _pctOrDash(row.mem_pct),
      ratio: row.mem_pct,
      detail: _bytesDetail(row.mem_used, row.mem_total),
    });
  }

  // Disk
  if (row.disk_total) {
    _card(grid, {
      label: 'Disk',
      value: _pctOrDash(row.disk_pct),
      ratio: row.disk_pct,
      detail: _bytesDetail(row.disk_used, row.disk_total),
    });
  }

  // Swap
  if (row.swap_total && row.swap_total > 0) {
    _card(grid, {
      label: 'Swap',
      value: _pctOrDash(row.swap_pct),
      ratio: row.swap_pct,
      detail: _bytesDetail(row.swap_used, row.swap_total),
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
  // When the newest sample was taken, and "never" when there is none. Without
  // this a silent transport is only distinguishable from a working one by its
  // dashes, and a card grid full of dashes does not say why.
  facts.push(row.sampled_at
    ? `last reading ${row.sampled_at}`
    : 'last reading never');
  wrapper.appendChild(el('p', 'srv-host', facts.join(' · ')));

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

/** A percentage that may not have been measured at all.
 *
 *  `_pct` turns null into "0%", which reads as a host sitting idle -- and a
 *  transport that has never reported is not idle, it is silent. /api/system
 *  sends null rather than 0 for exactly this reason ("the panel prints '--'
 *  for those"), and the panel stopped honouring it when the table became a
 *  card grid, so every never-sampled transport rendered as a healthy machine
 *  at 0%. That is the reading the broken collector produced for a day.
 */
export function _pctOrDash(n) {
  return (n === null || n === undefined || !Number.isFinite(Number(n)))
    ? '--'
    : _pct(n);
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

// ── Process cleanup ─────────────────────────────────────────────────────────

const KIND_LABEL = {
  zombie: 'Zombie/defunct',
  claude: 'Long-running agent (--resume, idle >2h)',
  chrome: 'Stale Chrome/Chromium tab',
  python: 'Stray Python/test process',
};

/** Render the initial "scan first" state in the cleanup panel. */
function _renderCleanupDefault(panel) {
  panel.innerHTML = `<div id="cleanupStatus" style="color: var(--muted); font-size: 13px;">
    Click below to scan for reclaimable processes.
  </div>
  <button id="cleanupScanBtn" class="srv-action-btn primary" style="margin-top: 8px;">
    Scan for reclaimable processes
  </button>`;
  byId('cleanupScanBtn').onclick = () => _scanCleanup(panel);
}

function _durationCompact(s) {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function _bytesMb(n) {
  return `${n.toFixed(0)} MB`;
}

/** Render the cleanup preview results in the cleanup status container. */
function _renderPreview(stats, container) {
  if (!stats || !stats.counts) {
    container.innerHTML = '<p style="color: var(--muted);">Scan failed.</p>';
    return;
  }
  const {counts, total_estimated_mb} = stats;
  const hasAny = Object.values(counts).some(v => v > 0);

  // Selection tracker (closure-scoped).
  const _selected = new Set();
  let _selectedMb = 0;

  // Seed with all PIDs (everything starts checked).
  for (const kind of Object.keys(counts)) {
    for (const p of stats[kind] || []) {
      _selected.add(p.pid);
      _selectedMb += p.rss_mb;
    }
  }

  function _updateBtn() {
    const btn = container.querySelector('#cleanupExecuteBtn');
    if (!btn) return;
    if (!hasAny) {
      btn.disabled = true;
      btn.textContent = 'Nothing to kill';
      return;
    }
    if (_selected.size === 0) {
      btn.disabled = true;
      btn.textContent = 'Select processes above';
    } else {
      btn.disabled = false;
      btn.textContent = `Stop ${_selected.size} selected (~${_bytesMb(_selectedMb)})`;
    }
  }

  // Build process list with checkboxes, category by category.
  const listHtml = Object.entries(stats)
    .filter(([k]) => k !== 'total_estimated_mb' && k !== 'counts')
    .map(([kind, procs]) => {
      if (!procs.length) return '';
      const label = KIND_LABEL[kind] || kind;
      const catMb = procs.reduce((s, p) => s + p.rss_mb, 0);
      let html = `<div style="margin: 4px 0;">`;
      // Per-category checkbox.
      html += `<label style="font-weight: bold; font-size: 12px; display: flex; align-items: center; gap: 4px;">`;
      html += `<input type="checkbox" class="cleanup-cat-check" data-kind="${kind}" checked> `;
      html += `${label} (${procs.length}, ${_bytesMb(catMb)})`;
      html += `</label>`;
      // Per-process checkboxes.
      html += `<ul style="font-size: 12px; margin: 2px 0 4px 24px; padding-left: 0; list-style: none;">`;
      for (const p of procs) {
        html += `<li style="color: var(--fg); display: flex; align-items: center; gap: 4px;">`;
        html += `<input type="checkbox" class="cleanup-pid-check" data-kind="${kind}" data-pid="${p.pid}" data-rss="${p.rss_mb}" checked> `;
        html += `<strong>${p.name}</strong>`;
        if (p.session_name) html += ` <span style="color: var(--muted);">[${p.session_name}]</span>`;
        html += ` — PID ${p.pid}, `;
        html += `${_bytesMb(p.rss_mb)}, running ${_durationCompact(p.age_s)}`;
        if (p.cmdline) html += `, <code style="font-size: 11px;">${p.cmdline.substring(0, 80)}</code>`;
        html += `</li>`;
      }
      html += '</ul></div>';
      return html;
    }).join('');

  let html = '';
  if (hasAny) {
    const parts = [];
    for (const [kind, count] of Object.entries(counts)) {
      if (count > 0) {
        const label = KIND_LABEL[kind] || kind;
        parts.push(`${label} × ${count}`);
      }
    }
    html += `<p><strong>Found:</strong> ${parts.join(' · ')}</p>`;
    html += `<p>Estimated memory to free: <strong>${_bytesMb(total_estimated_mb)}</strong></p>`;
    html += listHtml;
    html += '<div class="srv-action-bar">';
    html += `<button id="cleanupExecuteBtn" class="srv-action-btn danger">`;
    html += `Stop ${_selected.size} selected (~${_bytesMb(_selectedMb)})`;
    html += '</button>';
    // Rendering the results sets data-cleaned-up, which stops the 30s poll
    // restoring the default panel -- deliberately, so a poll cannot wipe the
    // list out from under you. The cost was that a scan had no way back: the
    // only control left was the one that kills things, and re-scanning meant
    // reloading the page. Pushed to the far end so it is never adjacent to it.
    html += '<button id="cleanupRescan" class="srv-action-btn push-right">Scan again</button>';
    html += '</div>';
  } else {
    // Same dead end, and worse: this branch used to be a bare paragraph with
    // no control at all, so a scan that found nothing left the panel inert
    // until the page was reloaded.
    html = '<p style="color: var(--ok);">All processes healthy. Nothing to free.</p>';
    html += '<div class="srv-action-bar">';
    html += '<button id="cleanupRescan" class="srv-action-btn primary">Scan again</button>';
    html += '</div>';
  }

  container.innerHTML = html;
  // Tell the 30s poll not to wipe this panel back to "scan first".
  container.setAttribute('data-cleaned-up', 'true');

  // Both branches above render this button, so it is wired once here rather
  // than in each. innerHTML discards listeners, so this has to run after the
  // assignment above, not before it.
  const rescan = container.querySelector('#cleanupRescan');
  if (rescan) rescan.onclick = () => _scanCleanup(container);

  // Cache the full payload on the container for the execute handler.
  container._cleanupStats = stats;

  // Select-all: check/uncheck all PIDs in a category.
  container.querySelectorAll('.cleanup-cat-check').forEach(cb => {
    cb.onchange = () => {
      const kind = cb.dataset.kind;
      const checked = cb.checked;
      container.querySelectorAll(`.cleanup-pid-check[data-kind="${kind}"]`).forEach(pcb => {
        pcb.checked = checked;
        const pid = Number(pcb.dataset.pid);
        const rss = parseFloat(pcb.dataset.rss);
        if (checked) { _selected.add(pid); _selectedMb += rss; }
        else { _selected.delete(pid); _selectedMb -= rss; }
      });
      _updateBtn();
    };
  });

  // Per-process checkboxes.
  container.querySelectorAll('.cleanup-pid-check').forEach(cb => {
    cb.onchange = () => {
      const pid = Number(cb.dataset.pid);
      const rss = parseFloat(cb.dataset.rss);
      if (cb.checked) { _selected.add(pid); _selectedMb += rss; }
      else { _selected.delete(pid); _selectedMb -= rss; }
      _updateBtn();
    };
  });

  const execBtn = container.querySelector('#cleanupExecuteBtn');
  if (execBtn) {
    execBtn.onclick = () => {
      if (_selected.size > 0) {
        _runCleanup(container._cleanupStats, container, [..._selected]);
      }
    };
  }
}

/** Execute the cleanup and render the result. */
async function _runCleanup(previewStats, container, selectedPids) {
  container.innerHTML = '<p>Terminating processes…</p>';
  try {
    const resp = await apiFetch('/api/system/cleanup/execute', {
      method: 'POST',
      body: JSON.stringify({pids: selectedPids}),
    });
    if (!resp.ok) {
      // Prefer the server's own reason. The 400 raised when every selected
      // PID has gone stale says what to do about it ("scan again"), and
      // "Cleanup failed" throws that away and reads like a server fault.
      let detail = '';
      try {
        detail = (await resp.json()).detail || '';
      } catch { /* not JSON: fall through to the generic message */ }
      throw new Error(detail || 'Cleanup failed');
    }
    const result = await resp.json();
    const hasFail = result.failed && result.failed.length > 0;

    let html = '';
    html += `<p style="color: var(--ok);">Cleaned up.</p>`;
    html += `<p>Killed: <strong>${result.killed ? result.killed.length : 0}</strong> processes · `;
    html += `Freed: <strong>${_bytesMb(result.freed_mb || 0)}</strong>`;
    if (hasFail) {
      html += ` · Failed: <strong>${result.failed.length}</strong> <span style="color: var(--warn);">`;
      html += `(check logs)</span>`;
    }
    html += '</p>';

    // Show what was actually killed
    if (result.killed && result.killed.length > 0) {
      html += '<details><summary style="font-size: 12px; color: var(--muted);">Details</summary>';
      html += '<ul style="font-size: 12px; padding-left: 20px; margin: 4px 0;">';
      for (const k of result.killed) {
        html += `<li style="color: var(--fg);"><strong>${k.kind}</strong> PID ${k.pid}${k.note ? ' — ' + k.note : ''}${k.rss_mb !== undefined ? ` (${_bytesMb(k.rss_mb)})` : ''}</li>`;
      }
      html += '</ul></details>';
    }

    // Primary here because it is the only thing left to do on this screen.
    html += '<div class="srv-action-bar">';
    html += `<button id="cleanupRescan" class="srv-action-btn primary">Scan again</button>`;
    html += '</div>';
    container.innerHTML = html;
    container.querySelector('#cleanupRescan').onclick = () => _scanCleanup(container);
  } catch (error) {
    // Built as nodes with a real listener, not as markup calling
    // `window._cleanupRescan()`. That name is a module export and was never
    // assigned to `window`, so the only way back from a failed cleanup was to
    // reload the page. textContent also keeps a server-supplied message out
    // of the HTML parser.
    container.replaceChildren();
    const notice = document.createElement('p');
    notice.style.color = 'var(--warn)';
    notice.textContent = `Cleanup failed: ${error.message}`;
    const bar = document.createElement('div');
    bar.className = 'srv-action-bar';
    const retry = document.createElement('button');
    retry.className = 'srv-action-btn primary';
    retry.textContent = 'Scan again';
    retry.onclick = () => _scanCleanup(container);
    bar.appendChild(retry);
    container.append(notice, bar);
    // Same reason the success path marks it: without this the 30s poll resets
    // the panel and the error disappears before it can be read.
    container.setAttribute('data-cleaned-up', 'true');
  }
}

/** Entry point: fetch preview and render. */
export async function _scanCleanup(container) {
  const btn = byId('cleanupScanBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Scanning…';
  }
  try {
    const resp = await apiFetch('/api/system/cleanup/preview');
    if (!resp.ok) throw new Error('Could not scan processes');
    const stats = await resp.json();
    _renderPreview(stats, container);
  } catch (error) {
    // Mark the panel and re-offer the button, for the reason the success path
    // marks it: without the attribute the 30s poll calls _renderCleanupDefault
    // and resets the panel to "click below to scan", so a failed scan erased
    // its own error within seconds and read as the click doing nothing at all.
    container.replaceChildren();
    const notice = document.createElement('p');
    notice.style.color = 'var(--warn)';
    notice.textContent = error.message;
    const bar = document.createElement('div');
    bar.className = 'srv-action-bar';
    const retry = document.createElement('button');
    retry.className = 'srv-action-btn primary';
    retry.textContent = 'Scan for reclaimable processes';
    retry.onclick = () => _scanCleanup(container);
    bar.appendChild(retry);
    container.append(notice, bar);
    container.setAttribute('data-cleaned-up', 'true');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Scan for reclaimable processes';
    }
  }
}

export function setStatus(text, type) {
  const el = byId('settingsStatus');
  el.textContent = text;
  el.className = type ? `toast ${type}` : '';
  if (type === 'success' || type === 'error') {
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