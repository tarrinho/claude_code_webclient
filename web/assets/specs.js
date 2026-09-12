// specs.js — Settings > Specs: every design spec in the repo, browsable.
//
// No pagination (unlike images.js) -- a few dozen small markdown files is
// cheap to list in full every time; server-side re-scan avoids any cache
// going stale, per specs_gallery.py's own discover_specs() docstring.
import {apiFetch} from './api.js?v=2741508';
import {_showConfirmDialog} from './machines.js?v=3055851';
import {notifyResult} from './server-stats.js?v=8469847';

const byId = id => document.getElementById(id);

function _statusLabel(status) {
  return status === 'planned' ? 'Planned' : 'Spec only';
}

function _row(spec) {
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

  // Shown to every user, same convention as every other admin-gated action
  // in this app (machines.js's delete, transports.js's delete, etc.): none
  // hide the control client-side, they all rely on the server's 403 as the
  // real enforcement. _deleteSpec surfaces that 403 through notifyResult.
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

async function _openSpec(spec) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(spec.id)}/content`);
    if (!response.ok) return;
    const html = await response.text();
    // Sanitized at the point of insertion, not trusted from the server:
    // discover_specs() scans this repo tree for any markdown file carrying
    // the marker line, not only a curated directory, and render_markdown()'s
    // success path passes embedded raw HTML through unchanged (only its
    // own failure fallback escapes). A spec file with injected
    // <script>/event-handler HTML would otherwise execute here, in an
    // authenticated same-origin tab -- DOMPurify (vendored, purify.min.js)
    // is the real boundary against that, applied right at the innerHTML
    // sink rather than trusted upstream.
    const clean = window.DOMPurify.sanitize(html);
    const win = window.open('', '_blank');
    if (win) {
      win.document.title = spec.title;
      win.document.body.innerHTML = clean;
    }
  } catch {
    // Silent: same fallback stance as images.js -- a failed open leaves
    // the list intact rather than surfacing a broken viewer.
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
    _updateCount();
  } catch (error) {
    // The row staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
    notifyResult(error.message, 'error');
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

  list.replaceChildren();
  (payload.specs || []).forEach(spec => list.appendChild(_row(spec)));
  _updateCount();
}
