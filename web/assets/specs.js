// specs.js — Settings > Specs: every design spec in the repo, browsable.
//
// No pagination (unlike images.js) -- a few dozen small markdown files is
// cheap to list in full every time; server-side re-scan avoids any cache
// going stale, per specs_gallery.py's own discover_specs() docstring.
import {apiFetch} from './api.js?v=2741508';
import {_showConfirmDialog} from './machines.js?v=3055851';
import {notifyResult} from './server-stats.js?v=5278923';

const byId = id => document.getElementById(id);

function _statusLabel(status) {
  return status === 'planned' ? 'Planned' : 'Spec only';
}

function _statusClass(status) {
  return status === 'planned' ? 'status-planned' : 'status-spec-only';
}

function _row(spec) {
  const row = document.createElement('div');
  row.className = 'spec-row';
  row.dataset.specId = spec.id;
  // The row itself is the click target, not just the title -- a card you
  // can click anywhere on reads as browsable; a title-sized hit zone inside
  // a bordered card that looks clickable everywhere does not. The title
  // stays a real <button> underneath for keyboard/AT focus, but does not
  // carry its own listener: its native click bubbles here, so Enter/Space
  // on it and a mouse click anywhere else on the card go through one path.
  row.addEventListener('click', () => _openSpec(spec));

  const top = document.createElement('div');
  top.className = 'spec-row-top';
  const title = document.createElement('button');
  title.type = 'button';
  title.className = 'spec-row-title';
  title.textContent = spec.title;
  top.appendChild(title);

  const status = document.createElement('span');
  status.className = `spec-row-status ${_statusClass(spec.status)}`;
  status.textContent = _statusLabel(spec.status);
  top.appendChild(status);
  row.appendChild(top);

  const meta = document.createElement('div');
  meta.className = 'spec-row-meta';
  const parts = [];
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
    // Sanitized at the point of insertion, not trusted from the server:
    // discover_specs() scans this repo tree for any markdown file carrying
    // the marker line, not only a curated directory. render_markdown()'s
    // success path is now sanitized server-side too (nh3), but DOMPurify
    // stays here as defense-in-depth rather than a replacement -- applied
    // right at the innerHTML sink rather than trusted upstream.
    const clean = window.DOMPurify.sanitize(html);
    // In-page panel, not window.open(): that call used to land after an
    // await, outside the click's original user-gesture window, so popup
    // blockers would likely kill it -- and the spec (section 3/4) asked for
    // a panel, not a new tab, in the first place.
    byId('specViewerTitle').textContent = spec.title;
    // Same orientation the row already gave before opening it -- a long
    // document with no status/author/date visible while reading loses the
    // context that made you pick it.
    const metaEl = byId('specViewerMeta');
    if (metaEl) {
      const parts = [_statusLabel(spec.status)];
      if (spec.author) parts.push(`${spec.author}${spec.date ? ` · ${spec.date}` : ''}`);
      metaEl.textContent = parts.join(' · ');
    }
    byId('specViewerContent').innerHTML = clean;
    byId('specViewerDialog').classList.add('open');
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
    if (!response.ok) {
      // Same discipline as _deleteSpec: a failed load must not read the
      // same as "there are genuinely no specs" -- an expired session or a
      // 500 used to leave the list silently empty with nothing to explain
      // why.
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'Could not load specs');
    }
    payload = await response.json();
  } catch (error) {
    notifyResult(error.message, 'error');
    return;
  }

  list.replaceChildren();
  (payload.specs || []).forEach(spec => list.appendChild(_row(spec)));
  _updateCount();
}
