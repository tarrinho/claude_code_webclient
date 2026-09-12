// images.js — Settings > Images: every generated image across all chats.
//
// Seeing an image inline, in the conversation it came from, already works
// (conversation.js's imageChip + openImageViewer, served by
// /api/chats/{id}/file). This module is the cross-chat management view: it
// never re-derives that inline behaviour, it only lists what
// routes/db_images.py already recorded and lets you delete or re-view one.
import {apiFetch} from './api.js?v=2741508';
import {openImageViewer} from './conversation.js?v=13571435';
import {_showConfirmDialog} from './machines.js?v=3055851';

const byId = id => document.getElementById(id);

let _nextBeforeId = null;
let _hasMore = false;

function _fileUrl(image) {
  return `/api/images/${encodeURIComponent(image.id)}/file`;
}

function _tile(image) {
  const tile = document.createElement('div');
  tile.className = 'image-tile';
  tile.dataset.imageId = String(image.id);

  const img = document.createElement('img');
  img.loading = 'lazy';
  img.alt = image.path;
  img.src = _fileUrl(image);
  img.tabIndex = 0;
  img.setAttribute('role', 'button');
  img.setAttribute('aria-label', `View ${image.path}`);
  const _open = () => openImageViewer(_fileUrl(image), image.path, `${image.chat_title} — ${image.path}`);
  img.addEventListener('click', _open);
  img.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    _open();
  });

  const meta = document.createElement('div');
  meta.className = 'image-tile-meta';
  meta.textContent = image.chat_title;

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'image-tile-delete';
  del.textContent = '×';
  del.setAttribute('aria-label', `Delete ${image.path}`);
  del.addEventListener('click', event => {
    event.stopPropagation();
    _showConfirmDialog(
      'Delete this image?',
      `This removes the file. The conversation it came from is left as-is. Delete ${image.path}?`,
      () => _deleteImage(image.id, tile),
    );
  });

  tile.append(img, meta, del);
  return tile;
}

async function _deleteImage(imageId, tile) {
  try {
    const response = await apiFetch(`/api/images/${encodeURIComponent(imageId)}`, {method: 'DELETE'});
    if (response.ok) {
      tile.remove();
      _updateCount();
    }
  } catch {
    // The tile staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
  }
}

function _updateCount() {
  const countEl = byId('imagesCount');
  const grid = byId('imagesGrid');
  if (!countEl || !grid) return;
  const total = grid.children.length;
  countEl.textContent = `${total} image${total === 1 ? '' : 's'}`;
}

/** Load the first page. force=true (Settings tab just opened) always
 *  refetches rather than showing a stale in-memory list. */
export async function loadImages(force = false) {
  const grid = byId('imagesGrid');
  if (!grid) return;
  if (!force && grid.children.length) return;

  grid.replaceChildren();
  _nextBeforeId = null;
  await _loadPage();
}

async function _loadPage() {
  const grid = byId('imagesGrid');
  const moreBtn = byId('imagesLoadMore');
  if (!grid) return;

  const params = new URLSearchParams({limit: '60'});
  if (_nextBeforeId) params.set('before_id', String(_nextBeforeId));

  let payload;
  try {
    const response = await apiFetch(`/api/images?${params}`);
    if (!response.ok) return;
    payload = await response.json();
  } catch {
    return;
  }

  const images = payload.images || [];
  images.forEach(image => grid.appendChild(_tile(image)));
  _hasMore = !!payload.has_more;
  // Server-provided cursor, not derived from the received images: an
  // entire page can come back empty after the server's self-healing
  // filter (every file in that page was gone), and deriving the cursor
  // from an empty array would leave it stuck forever on the same dead
  // page. next_before_id is the lowest id the server actually saw before
  // filtering, so it always advances when the raw page was non-empty.
  if (payload.next_before_id != null) _nextBeforeId = payload.next_before_id;

  _updateCount();
  if (moreBtn) moreBtn.hidden = !_hasMore;
}

export function _wireImagesLoadMore() {
  byId('imagesLoadMore')?.addEventListener('click', () => _loadPage());
}
