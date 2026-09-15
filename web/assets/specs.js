// specs.js — Settings > Specs: every design spec in the repo, browsable.
//
// No pagination (unlike images.js) -- a few dozen small markdown files is
// cheap to list in full every time; server-side re-scan avoids any cache
// going stale, per specs_gallery.py's own discover_specs() docstring.
import {apiFetch} from './api.js?v=2741508';
import {_showConfirmDialog} from './machines.js?v=3055851';
import {notifyResult} from './server-stats.js?v=1383946';

const byId = id => document.getElementById(id);

// The 3 auto-computed values (specs_gallery.spec_status_v2, git/filesystem
// derived) and the 4 manually-settable ones (routes/db_specs.ALLOWED_STATUSES)
// are deliberately different words -- "planned"/"implemented" (auto) vs
// "planning"/"done" (manual) -- so a status string alone tells you which kind
// it is, the same way spec.status_manual does structurally.
var _statusLabels = {
  implemented: 'Implemented', planned: 'Planned', spec_only: 'Spec only',
  planning: 'Planning', implementing: 'Implementing', done: 'Done',
};
var _statusClasses = {
  implemented: 'status-implemented', planned: 'status-planned', spec_only: 'status-spec-only',
  planning: 'status-planning', implementing: 'status-implementing', done: 'status-done',
};

function _statusLabel(status) {
  return _statusLabels[status] || 'Spec only';
}

function _statusClass(status) {
  return _statusClasses[status] || 'status-spec-only';
}

var _groupOrder = {
  implemented: 0, done: 1, implementing: 2, planned: 3, planning: 4, 'spec-only': 5,
};
var _groupLabel = {
  implemented: 'Implemented', done: 'Done', implementing: 'Implementing',
  planned: 'Planned', planning: 'Planning', 'spec-only': 'Spec only',
};
var _groupStatusKey = {
  implemented: 'implemented', done: 'done', implementing: 'implementing',
  planned: 'planned', planning: 'planning', 'spec-only': 'spec_only',
};

// Preselect a sensible manual value for a spec that has no override yet --
// the combo box is a "set this" control, not a read-only mirror of the
// auto-computed status, so it always needs one of its own 4 options selected.
var _autoToManualDefault = {implemented: 'done', planned: 'planning', spec_only: 'spec_only'};

/** Set an existing <select>'s value/color-class and wire its change handler
 *  for *spec* -- shared by a freshly-built row select and the viewer
 *  dialog's static #specViewerStatus, so both stay in sync with exactly
 *  one place that knows the preselect rule and the color-class swap. */
function _wireStatusSelect(select, spec) {
  select.value = spec.status_manual ? spec.status : (_autoToManualDefault[spec.status] || 'spec_only');
  select.classList.remove(...Object.values(_statusClasses));
  select.classList.add(_statusClass(select.value));
  select.onchange = () => {
    select.classList.remove(...Object.values(_statusClasses));
    select.classList.add(_statusClass(select.value));
    _setSpecStatus(spec, select.value, select);
  };
}

/** A fresh <select> for the row -- the viewer dialog reuses the static one
 *  already in index.html instead (see _openSpec), wired the same way. */
function _makeStatusSelect(spec, extraClass) {
  const select = document.createElement('select');
  select.className = `conversation-model${extraClass ? ' ' + extraClass : ''}`;
  select.setAttribute('aria-label', `Manually set the status of ${spec.title}`);
  for (const [value, label] of [
    ['spec_only', 'Spec only'], ['planning', 'Planning'],
    ['implementing', 'Implementing'], ['done', 'Done'],
  ]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  select.addEventListener('click', event => event.stopPropagation());
  _wireStatusSelect(select, spec);
  return select;
}

function _row(spec) {
  const row = document.createElement('div');
  row.className = 'spec-row';
  row.dataset.specId = spec.id;
  row.addEventListener('click', () => _openSpec(spec));

  const top = document.createElement('div');
  top.className = 'spec-row-top';
  const title = document.createElement('button');
  title.type = 'button';
  title.className = 'spec-row-title';
  title.textContent = spec.title;
  top.appendChild(title);

  const status = _makeStatusSelect(spec, 'spec-row-status');
  top.appendChild(status);
  row.appendChild(top);

  const path = document.createElement('div');
  path.className = 'spec-row-path';
  path.textContent = spec.path;
  row.appendChild(path);

  const meta = document.createElement('div');
  meta.className = 'spec-row-meta';
  const parts = [];
  if (spec.author) parts.push(spec.author);
  if (spec.date) parts.push('commits ' + spec.date);
  if (spec.mtime_iso) parts.push('edited ' + spec.mtime_iso);
  if (spec.referenced_by && spec.referenced_by.length) {
    parts.push(spec.referenced_by.length + ' ref' + (spec.referenced_by.length === 1 ? '' : 's'));
  }
  meta.textContent = parts.join(' · ');
  row.appendChild(meta);

  // The repo-relative path for accessibility context (used in viewer too)
  row.title = spec.path;

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'spec-row-delete';
  del.textContent = '×';
  del.setAttribute('aria-label', `Delete ${spec.title}`);
  del.addEventListener('click', event => {
    event.stopPropagation();
    _showConfirmDialog(
      'Delete this spec?',
      `This removes the file from disk. It is not committed to git automatically -- delete ${spec.title}?`,
      () => _deleteSpec(spec.id, row),
    );
  });
  row.appendChild(del);

  return row;
}

/** Close the spec viewer. Exported for Escape-key handling in app.js, same
 *  convention as machines.js's _closeConfirmDialog. */
export function _closeSpecViewer() {
  byId('specViewerDialog')?.classList.remove('open');
}

async function _openSpec(spec) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(spec.id)}/content`);
    if (!response.ok) return;
    const html = await response.text();
    const clean = window.DOMPurify.sanitize(html);
    byId('specViewerTitle').textContent = spec.title;
    const pathEl = byId('specViewerPath');
    if (pathEl) pathEl.textContent = spec.path;
    const metaEl = byId('specViewerMeta');
    if (metaEl) {
      const parts = [_statusLabel(spec.status)];
      if (spec.author) parts.push(spec.author);
      if (spec.date) parts.push('commits ' + spec.date);
      if (spec.mtime_iso) parts.push('edited ' + spec.mtime_iso);
      metaEl.textContent = parts.join(' · ');
    }
    byId('specViewerContent').innerHTML = clean;
    var statusSelect = byId('specViewerStatus');
    if (statusSelect) _wireStatusSelect(statusSelect, spec);
    var copyBtn = byId('specViewerCopy');
    if (copyBtn) copyBtn.onclick = () => _copySpecMarkdown(spec, copyBtn);
    byId('specViewerDialog').classList.add('open');
  } catch {
    // Silent: same fallback stance as images.js -- a failed open leaves
    // the list intact rather than surfacing a broken viewer.
  }
}

/** Copy a spec's raw markdown source (not the rendered HTML) to the
 *  clipboard -- ?format=raw on the same content endpoint the viewer
 *  already fetched, so headings/code fences/links paste as real markdown
 *  rather than as whatever the rendered HTML's plain text would read as. */
async function _copySpecMarkdown(spec, button) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(spec.id)}/content?format=raw`);
    if (!response.ok) throw new Error('Could not load spec content');
    const text = await response.text();
    await navigator.clipboard.writeText(text);
    const original = button.textContent;
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = original; }, 1500);
  } catch (error) {
    notifyResult(error.message || 'Could not copy to clipboard', 'error');
  }
}

/** Set a spec's manual status. Reverts the select on failure and refreshes
 *  the list on success so its grouping reflects the new status -- same
 *  "server truth over local patching" stance as the rest of this file. */
async function _setSpecStatus(spec, status, selectEl) {
  var previous = selectEl.value;
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(spec.id)}/status`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Could not set status');
    }
    spec.status = status;
    spec.status_manual = true;
    loadSpecs(true);
  } catch (error) {
    selectEl.value = previous;
    selectEl.classList.remove(...Object.values(_statusClasses));
    selectEl.classList.add(_statusClass(previous));
    notifyResult(error.message, 'error');
  }
}

async function _deleteSpec(specId, row) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(specId)}`, {method: 'DELETE'});
    if (!response.ok) {
      // 403 (non-admin) and 404 (already gone) both land here with the
      // server's own detail text -- same pattern as machines.js's
      // _deleteMachine, so a rejected delete is never silent.
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Could not delete spec');
    }
    row.remove();
    // Update the overall count from the remaining rows.
    _updateCount(null);
  } catch (error) {
    // The row staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
    notifyResult(error.message, 'error');
  }
}

function _makeGroup(label, key) {
  var details = document.createElement('details');
  details.className = 'spec-group';
  details.dataset.groupKey = key;
  var summary = document.createElement('summary');
  summary.className = 'spec-section-label';
  summary.textContent = label;
  details.appendChild(summary);
  return details;
}

function _updateCount(specs) {
  var countEl = byId('specsCount');
  if (!countEl) return;
  countEl.textContent = specs.length + ' spec' + (specs.length !== 1 ? 's' : '');
}

/** Load the full list. force=true (Settings tab just opened) always
 *  refetches -- the server itself never caches (specs_gallery.discover_specs
 *  re-scans every call), so a stale in-memory render is the only staleness
 *  risk left, and this closes it.

 *  Groups results into collapsible sections by status
 *  (Implemented, Planned, Spec only), with a count badge. */
export async function loadSpecs(force = false) {
  var list = byId('specsList');
  if (!list) return;
  if (!force && list.children.length) return;

  var payload;
  try {
    const response = await apiFetch('/api/specs');
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'Could not load specs');
    }
    payload = await response.json();
  } catch (error) {
    notifyResult(error.message, 'error');
    return;
  }

  var allSpecs = payload.specs || [];
  list.replaceChildren();

  // Group by status key
  var groups = {};
  for (var i = 0; i < allSpecs.length; i++) {
    var s = allSpecs[i];
    var key = s.status === 'spec_only' ? 'spec-only' : s.status.replace(' ', '-');
    if (!groups[key]) groups[key] = [];
    groups[key].push(s);
  }

  // Sort groups by _groupOrder
  var sortedKeys = Object.keys(groups).sort(function(a, b) {
    return (_groupOrder[a] || 9) - (_groupOrder[b] || 9);
  });

  for (var g = 0; g < sortedKeys.length; g++) {
    var key = sortedKeys[g];
    var group = _makeGroup(_groupLabel[key] + ' · ' + groups[key].length, key);
    for (var i = 0; i < groups[key].length; i++) {
      group.appendChild(_row(groups[key][i]));
    }
    list.appendChild(group);
  }

  _updateCount(allSpecs);
}
