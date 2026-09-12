// specs.js — Settings > Specs: every design spec in the repo, browsable.
//
// No pagination (unlike images.js) -- a few dozen small markdown files is
// cheap to list in full every time; server-side re-scan avoids any cache
// going stale, per specs_gallery.py's own discover_specs() docstring.
import {apiFetch} from './api.js?v=2741508';
import {_showConfirmDialog} from './machines.js?v=3055851';

const byId = id => document.getElementById(id);

function _statusLabel(status) {
  return status === 'planned' ? 'Planned' : 'Spec only';
}

function _row(spec, isAdmin) {
  const row = document.createElement('div');
  row.className = 'spec-row';
  row.dataset.specId = spec.id;

  const title = document.createElement('button');
  title.type = 'button';
  title.className = 'spec-row-title';
  title.textContent = spec.title;
  title.addEventListener('click', () => _openSpec(spec));
  row.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'spec-row-meta';
  const parts = [_statusLabel(spec.status)];
  if (spec.author) parts.push(`${spec.author}${spec.date ? ` · ${spec.date}` : ''}`);
  if (spec.referenced_by && spec.referenced_by.length) {
    parts.push(`referenced by ${spec.referenced_by.length} file${spec.referenced_by.length === 1 ? '' : 's'}`);
  }
  meta.textContent = parts.join(' · ');
  row.appendChild(meta);

  if (isAdmin) {
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
  }

  return row;
}

async function _openSpec(spec) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(spec.id)}/content`);
    if (!response.ok) return;
    const html = await response.text();
    const win = window.open('', '_blank');
    if (win) {
      win.document.title = spec.title;
      win.document.body.innerHTML = html;
    }
  } catch {
    // Silent: same fallback stance as images.js -- a failed open leaves
    // the list intact rather than surfacing a broken viewer.
  }
}

async function _deleteSpec(specId, row) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(specId)}`, {method: 'DELETE'});
    if (response.ok) {
      row.remove();
      _updateCount();
    }
  } catch {
    // The row staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
  }
}

function _updateCount() {
  const countEl = byId('specsCount');
  const list = byId('specsList');
  if (!countEl || !list) return;
  const total = list.children.length;
  countEl.textContent = `${total} spec${total === 1 ? '' : 's'}`;
}

/** Load the full list. force=true (Settings tab just opened) always
 *  refetches -- the server itself never caches (specs_gallery.discover_specs
 *  re-scans every call), so a stale in-memory render is the only staleness
 *  risk left, and this closes it. */
export async function loadSpecs(force = false) {
  const list = byId('specsList');
  if (!list) return;
  if (!force && list.children.length) return;

  let payload;
  try {
    const response = await apiFetch('/api/specs');
    if (!response.ok) return;
    payload = await response.json();
  } catch {
    return;
  }

  const isAdmin = window.state?.session?.role === 'admin';
  list.replaceChildren();
  (payload.specs || []).forEach(spec => list.appendChild(_row(spec, isAdmin)));
  _updateCount();
}
