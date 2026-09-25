// ── Scratch-space reclaim ─────────────────────────────────────────────────────
// The second action panel on the Server tab. Process cleanup (server-stats.js)
// kills processes; this deletes stale files from the host's tmpfs mounts,
// where /tmp lives in RAM and grows until something removes it.
//
// Kept in its own module rather than added to server-stats.js, which is
// already over the 300-line cap, and kept as a separate panel rather than a
// second mode of the cleanup panel: one button that might kill your agent and
// might delete your files is a button nobody can click confidently.
//
// Nothing here trusts the rendered list. The server re-derives its own
// preview on execute and refuses anything absent from it, so a page left open
// for an hour cannot delete a directory that a test run has since claimed.

import {apiFetch} from './api.js?v=2741508';
import {showToast} from './app.js?v=5926217';

const byId = id => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** "3 days ago" / "12 min ago" — an epoch seconds value in, one unit out. */
export function _since(epochSeconds) {
  const s = Math.max(0, Date.now() / 1000 - (Number(epochSeconds) || 0));
  if (s >= 86400) return `${Math.floor(s / 86400)} days ago`;
  if (s >= 3600) return `${Math.floor(s / 3600)} hours ago`;
  if (s >= 60) return `${Math.floor(s / 60)} min ago`;
  return 'just now';
}

/** "1.4 GB" / "181 MB" — MB in, the unit the reader wants out. */
export function _mb(n) {
  const value = Number(n) || 0;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} GB`;
  return `${value.toFixed(1)} MB`;
}

/** "3 days" / "4 hours" / "12 min" — seconds in, one unit out. */
export function _age(seconds) {
  const s = Number(seconds) || 0;
  if (s >= 86400) return `${Math.floor(s / 86400)} days`;
  if (s >= 3600) return `${Math.floor(s / 3600)} hours`;
  return `${Math.floor(s / 60)} min`;
}

export function renderReclaimDefault(panel) {
  panel.textContent = '';
  const status = el('div', 'srv-reclaim-status',
    'Click below to scan /tmp and /dev/shm for stale scratch space.');
  status.id = 'reclaimStatus';
  panel.appendChild(status);
  const bar = el('div', 'srv-action-bar');
  const scan = el('button', 'srv-action-btn primary', 'Scan for reclaimable space');
  scan.id = 'reclaimScanBtn';
  scan.onclick = () => scanReclaim(panel);
  bar.appendChild(scan);
  panel.appendChild(bar);
}

/** One checkbox per family, not per file.
 *
 *  The first version of this listed every entry: 452 rows on this host, of
 *  which one family held 109.4 MB and most of the rest were sub-megabyte
 *  logs. The operator's verdict was that they could not tell which were safe
 *  to delete, which was correct and was the panel's fault -- the scan has
 *  already proved every listed entry is stale, unowned by any process and
 *  this service's own. Asking the reader to re-make that judgment 452 times
 *  added no safety and plenty of doubt.
 *
 *  Everything starts checked, because everything offered is now something
 *  this service made and can name. The file-by-file detail is still one
 *  click away for anyone who wants it.
 */
function _familyList(families, selected, onChange) {
  const wrap = el('div', 'srv-reclaim-list');
  for (const family of families) {
    const row = el('div', 'srv-reclaim-family');
    const label = el('label');
    const box = el('input');
    box.type = 'checkbox';
    box.className = 'reclaim-check';
    box.dataset.family = family.id;
    box.dataset.mb = String(family.size_mb);
    box.checked = true;
    for (const path of family.paths) selected.add(path);
    box.addEventListener('change', () => {
      for (const path of family.paths) {
        if (box.checked) selected.add(path);
        else selected.delete(path);
      }
      onChange();
    });
    label.appendChild(box);
    label.appendChild(el('strong', null, ` ${family.label}`));
    label.appendChild(el('span', 'srv-reclaim-detail',
      ` — ${family.count} ${family.count === 1 ? 'entry' : 'entries'}, ` +
      `${_mb(family.size_mb)}, none touched for at least ${_age(family.age_s)}`));
    row.appendChild(label);

    // The detail, present and collapsed. Nobody has to read it; anybody who
    // wants to check what a family actually contains can.
    const detail = el('details', 'srv-reclaim-members');
    detail.appendChild(el('summary', null, 'Show files'));
    const list = el('ul', 'srv-reclaim-items');
    for (const entry of family.entries) {
      list.appendChild(el('li', null,
        `${entry.name} — ${entry.is_dir ? 'directory' : 'file'}, ` +
        `${_mb(entry.size_mb)}, untouched for ${_age(entry.age_s)}`));
    }
    detail.appendChild(list);
    row.appendChild(detail);
    wrap.appendChild(row);
  }
  return wrap;
}

/** Why the scan passed something over.
 *
 *  Collapsed, and present even when the list above is empty: "nothing to
 *  reclaim" and "everything is in use by a running job" look identical
 *  without it, and only the second one means come back later.
 *
 *  Grouped by reason rather than listed path by path. Most skips on a busy
 *  host are "not one of this service's own scratch families", which is a
 *  single fact about hundreds of files -- printing it hundreds of times is
 *  the same mistake this panel was just rewritten to stop making.
 */
function _skippedBlock(skipped) {
  const byReason = new Map();
  for (const row of skipped) {
    if (!byReason.has(row.reason)) byReason.set(row.reason, []);
    byReason.get(row.reason).push(row.path);
  }
  const box = el('details', 'srv-reclaim-skipped');
  box.appendChild(el('summary', null, `Left alone (${skipped.length})`));
  const list = el('ul', 'srv-reclaim-items');
  // Largest group first: the reason accounting for most of the skips is the
  // one that answers "why is my directory not here".
  const groups = [...byReason.entries()].sort((a, b) => b[1].length - a[1].length);
  for (const [reason, paths] of groups) {
    const item = el('li');
    item.appendChild(el('span', null, `${paths.length} × ${reason}`));
    const names = paths.slice(0, 12).map(p => p.split('/').pop()).join(', ');
    item.appendChild(el('span', 'srv-reclaim-detail',
      ` — ${names}${paths.length > 12 ? `, and ${paths.length - 12} more` : ''}`));
    list.appendChild(item);
  }
  box.appendChild(list);
  return box;
}

function _renderPreview(stats, panel) {
  const families = stats.families || [];
  const entries = stats.entries || [];
  const selected = new Set();
  panel.textContent = '';
  panel.setAttribute('data-reclaimed', '1');

  const summary = el('div', 'srv-reclaim-status');
  summary.id = 'reclaimStatus';
  summary.textContent = entries.length
    ? `${_mb(stats.total_mb)} reclaimable in ${(stats.roots || []).join(' and ')}, ` +
      `across ${families.length} ${families.length === 1 ? 'family' : 'families'}.`
    : `Nothing to reclaim in ${(stats.roots || []).join(' and ') || 'tmpfs'} right now.`;
  panel.appendChild(summary);

  // The timer, made visible. A sweep that runs unattended and says nothing is
  // indistinguishable from one that is broken, and the panel showing little
  // to reclaim needs the explanation sitting next to it.
  if (stats.last_sweep) {
    panel.appendChild(el('div', 'srv-reclaim-status',
      `Last automatic sweep ${_since(stats.last_sweep.at)}: ` +
      `${stats.last_sweep.deleted} entries, ${_mb(stats.last_sweep.freed_mb)}.`));
  }

  const deleteBtn = el('button', 'srv-action-btn danger');
  deleteBtn.id = 'reclaimDeleteBtn';

  function _updateBtn() {
    let mb = 0;
    for (const box of panel.querySelectorAll('.reclaim-check')) {
      if (box.checked) mb += Number(box.dataset.mb) || 0;
    }
    if (!selected.size) {
      deleteBtn.disabled = true;
      deleteBtn.textContent = entries.length ? 'Select a family above' : 'Nothing to delete';
    } else {
      deleteBtn.disabled = false;
      // Counts entries, not families: "Reclaim 2 families" hides the scale of
      // what is about to be deleted behind a number that is always small.
      deleteBtn.textContent = `Reclaim ${selected.size} entries (${_mb(mb)})`;
    }
  }

  if (families.length) panel.appendChild(_familyList(families, selected, _updateBtn));
  if ((stats.skipped || []).length) panel.appendChild(_skippedBlock(stats.skipped));

  const bar = el('div', 'srv-action-bar');
  bar.appendChild(deleteBtn);
  const rescan = el('button', 'srv-action-btn push-right', 'Scan again');
  rescan.id = 'reclaimRescan';
  rescan.onclick = () => scanReclaim(panel);
  bar.appendChild(rescan);
  panel.appendChild(bar);

  deleteBtn.onclick = () => _runReclaim(panel, [...selected]);
  _updateBtn();
}

async function _runReclaim(panel, paths) {
  const btn = byId('reclaimDeleteBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Deleting…';
  }
  try {
    const res = await apiFetch('/api/system/reclaim/execute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const result = await res.json();
    _renderResult(result, panel);
  } catch (err) {
    _renderError(panel, `Delete failed: ${err.message}`);
  }
}

function _renderResult(result, panel) {
  panel.textContent = '';
  panel.setAttribute('data-reclaimed', '1');
  const lines = [
    `Deleted ${result.deleted.length} entries, ${_mb(result.freed_mb)} freed.`,
  ];
  const before = result.mem_before || {};
  const after = result.mem_after || {};
  if (after.mem_available_mb !== undefined) {
    lines.push(
      `Available memory ${_mb(before.mem_available_mb)} → ${_mb(after.mem_available_mb)}, ` +
      `free swap ${_mb(before.swap_free_mb)} → ${_mb(after.swap_free_mb)}.`);
  }
  if (result.failed.length) lines.push(`${result.failed.length} could not be deleted.`);
  if (result.refused.length) {
    lines.push(`${result.refused.length} were in use by then and were left alone.`);
  }
  for (const line of lines) panel.appendChild(el('div', 'srv-reclaim-status', line));

  const bar = el('div', 'srv-action-bar');
  const rescan = el('button', 'srv-action-btn', 'Scan again');
  rescan.id = 'reclaimRescan';
  rescan.onclick = () => scanReclaim(panel);
  bar.appendChild(rescan);
  panel.appendChild(bar);
  showToast(`Reclaimed ${_mb(result.freed_mb)}`, 'success');
}

/** The error path sets data-reclaimed for the same reason the cleanup panel
 *  does: without it the 30-second poll re-renders the default and the failure
 *  disappears before it can be read. */
function _renderError(panel, message) {
  panel.textContent = '';
  panel.setAttribute('data-reclaimed', '1');
  panel.appendChild(el('div', 'srv-reclaim-status error', message));
  const bar = el('div', 'srv-action-bar');
  const retry = el('button', 'srv-action-btn', 'Scan again');
  retry.id = 'reclaimRescan';
  retry.onclick = () => scanReclaim(panel);
  bar.appendChild(retry);
  panel.appendChild(bar);
}

export async function scanReclaim(panel) {
  const btn = byId('reclaimScanBtn') || byId('reclaimRescan');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Scanning…';
  }
  try {
    const res = await apiFetch('/api/system/reclaim/preview');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _renderPreview(await res.json(), panel);
  } catch (err) {
    _renderError(panel, `Scan failed: ${err.message}`);
  }
}
