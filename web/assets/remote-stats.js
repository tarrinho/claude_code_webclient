// Remote host stats for the Server page — renders latest system_samples.
import {apiFetch} from './api.js?v=1';
import {byId} from './app.js?v=34';
import {esc} from './app.js?v=34';

const _REMOTE_HOSTS = new Map();

export function _setRemoteHosts(hosts) {
  _REMOTE_HOSTS.clear();
  hosts.forEach(h => _REMOTE_HOSTS.set(h.id, h));
  _renderRemoteStats();
}

export function _pollRemoteStats() {
  // Called on Server page load to fetch remote stats.
  _fetchRemoteStats();
}

async function _fetchRemoteStats() {
  try {
    const resp = await apiFetch('/api/system_samples?host_type=remote&limit=10');
    if (!resp.ok) return;
    const data = await resp.json();
    const samples = data.samples || [];
    // Group by host_id and show latest.
    const latest = new Map();
    samples.forEach(s => {
      if (!latest.has(s.host_id)) latest.set(s.host_id, s);
    });
    _setRemoteHosts(latest);
  } catch (_) { /* ignore */ }
}

function _renderRemoteStats() {
  const container = byId('remoteStatsContainer');
  if (!container) return;
  container.innerHTML = '';

  if (!_REMOTE_HOSTS.size) {
    container.innerHTML = '<p class="sidebar-empty">No remote stats yet.</p>';
    return;
  }

  _REMOTE_HOSTS.forEach((sample, id) => {
    try {
      const parsed = JSON.parse(sample.data || '{}');
      const stats = parsed.stats || {};

      const card = document.createElement('div');
      card.className = 'remote-stat-card';
      card.innerHTML = `
        <div class="remote-stat-header">
          <strong>${esc(id)}</strong>
          <span class="remote-stat-time">${esc(sample.created_at || '')}</span>
        </div>
        <div class="remote-stat-body">
          ${stats.disk ? `<div class="remote-stat-item"><span>Disk</span><span>${esc(String(stats.disk))}</span></div>` : ''}
          ${stats.load ? `<div class="remote-stat-item"><span>Load</span><span>${esc(String(stats.load))}</span></div>` : ''}
          ${stats.uptime ? `<div class="remote-stat-item"><span>Uptime</span><span>${esc(String(stats.uptime))}</span></div>` : ''}
        </div>
      `;
      container.appendChild(card);
    } catch (_) { /* skip malformed */ }
  });
}
