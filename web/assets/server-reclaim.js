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

const KIND_LABEL = {
  webconsole: 'WebConsole test and scan leftovers',
  other: 'Other stale scratch space',
};

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

/** The list of entries, each with its own checkbox.
 *
 *  WebConsole's own leftovers start checked; everything else starts
 *  unchecked. Both are equally safe by the server's rules, but only one of
 *  them is ours, and a default that deletes a stranger's files is a default
 *  that gets clicked through once and regretted.
 */
function _entryList(entries, selected, onChange) {
  const wrap = el('div', 'srv-reclaim-list');
  for (const kind of ['webconsole', 'other']) {
    const rows = entries.filter(row => row.kind === kind);
    if (!rows.length) continue;
    const groupMb = rows.reduce((sum, row) => sum + row.size_mb, 0);
    const heading = el('p', 'srv-reclaim-group',
      `${KIND_LABEL[kind]} — ${rows.length}, ${_mb(groupMb)}`);
    wrap.appendChild(heading);
    const list = el('ul', 'srv-reclaim-items');
    for (const row of rows) {
      const item = el('li');
      const label = el('label');
      const box = el('input');
      box.type = 'checkbox';
      box.className = 'reclaim-check';
      box.dataset.path = row.path;
      box.dataset.mb = String(row.size_mb);
      box.checked = kind === 'webconsole';
      if (box.checked) selected.add(row.path);
      box.addEventListener('change', () => {
        if (box.checked) selected.add(row.path);
        else selected.delete(row.path);
        onChange();
      });
      label.appendChild(box);
      label.appendChild(el('strong', null, ` ${row.name}`));
      const detail = row.is_dir ? 'directory' : 'file';
      label.appendChild(el('span', 'srv-reclaim-detail',
        ` — ${detail}, ${_mb(row.size_mb)}, untouched for ${_age(row.age_s)}`));
      item.appendChild(label);
      list.appendChild(item);
    }
    wrap.appendChild(list);
  }
  return wrap;
}

/** Why the scan passed something over.
 *
 *  Collapsed, and present even when the list above is empty: "nothing to
 *  reclaim" and "everything is in use by a running job" look identical
 *  without it, and only the second one means come back later.
 */
function _skippedBlock(skipped) {
  const box = el('details', 'srv-reclaim-skipped');
  box.appendChild(el('summary', null, `Left alone (${skipped.length})`));
  const list = el('ul', 'srv-reclaim-items');
  for (const row of skipped.slice(0, 40)) {
    list.appendChild(el('li', null, `${row.path} — ${row.reason}`));
  }
  if (skipped.length > 40) {
    list.appendChild(el('li', null, `…and ${skipped.length - 40} more`));
  }
  box.appendChild(list);
  return box;
}

function _renderPreview(stats, panel) {
  const entries = stats.entries || [];
  const selected = new Set();
  panel.textContent = '';
  panel.setAttribute('data-reclaimed', '1');

  const summary = el('div', 'srv-reclaim-status');
  summary.id = 'reclaimStatus';
  summary.textContent = entries.length
    ? `${entries.length} entries, ${_mb(stats.total_mb)} reclaimable in ${(stats.roots || []).join(' and ')}.`
    : `Nothing to reclaim in ${(stats.roots || []).join(' and ') || 'tmpfs'} right now.`;
  panel.appendChild(summary);

  const deleteBtn = el('button', 'srv-action-btn danger');
  deleteBtn.id = 'reclaimDeleteBtn';

  function _updateBtn() {
    let mb = 0;
    for (const box of panel.querySelectorAll('.reclaim-check')) {
      if (box.checked) mb += Number(box.dataset.mb) || 0;
    }
    if (!selected.size) {
      deleteBtn.disabled = true;
      deleteBtn.textContent = entries.length ? 'Select entries above' : 'Nothing to delete';
    } else {
      deleteBtn.disabled = false;
      deleteBtn.textContent = `Delete ${selected.size} selected (${_mb(mb)})`;
    }
  }

  if (entries.length) panel.appendChild(_entryList(entries, selected, _updateBtn));
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
