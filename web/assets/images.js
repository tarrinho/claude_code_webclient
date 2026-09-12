// images.js — Settings > Images: every generated image across all chats.
//
// Seeing an image inline, in the conversation it came from, already works
// (conversation.js's imageChip + openImageViewer, served by
// /api/chats/{id}/file). This module is the cross-chat management view: it
// never re-derives that inline behaviour, it only lists what
// routes/db_images.py already recorded and lets you delete or re-view one.
import {apiFetch} from './api.js?v=2741508';
import {openImageViewer} from './conversation.js?v=16308196';
import {_showConfirmDialog} from './machines.js?v=13880237';

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
  img.addEventListener('click', () => {
    openImageViewer(_fileUrl(image), image.path, `${image.chat_title} — ${image.path}`);
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
    if (response.ok) tile.remove();
  } catch {
    // The tile staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
  }
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
  const countEl = byId('imagesCount');
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
  _nextBeforeId = images.length ? images[images.length - 1].id : _nextBeforeId;

  if (countEl) {
    const total = grid.children.length;
    countEl.textContent = `${total} image${total === 1 ? '' : 's'}`;
  }
  if (moreBtn) moreBtn.hidden = !_hasMore;
}

export function _wireImagesLoadMore() {
  byId('imagesLoadMore')?.addEventListener('click', () => _loadPage());
}
