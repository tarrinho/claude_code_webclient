// Transport CRUD: the SSH connection a backend can optionally run over.
// See docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.
import {apiFetch} from './api.js?v=2741508';
import {byId} from './app.js?v=6561250';
// notifyResult comes from server-stats.js directly, not via app.js -- app.js
// only re-exports it there as part of unrelated, uncommitted work elsewhere
// in this shared tree; machines.js already imports it the same direct way.
import {notifyResult} from './server-stats.js?v=8469847';

export let _transports = [];
let _transportEditing = null;

export async function loadTransports() {
  try {
    const resp = await apiFetch('/api/transports');
    if (!resp.ok) return;
    const data = await resp.json();
    _transports = data.transports || [];
  } catch {
    _transports = [];
  }
}

/** Fill the "Transport" dropdown with "Direct" + every transport. */
export function populateTransportPicker(selectedId) {
  const picker = byId('machineTransport');
  if (!picker) return;
  const local = document.createElement('option');
  local.value = '';
  local.textContent = 'Direct';
  const options = [local];
  _transports.forEach(t => {
    const option = document.createElement('option');
    option.value = t.id;
    option.textContent = t.name;
    options.push(option);
  });
  picker.replaceChildren(...options);
  picker.value = _transports.some(t => t.id === selectedId) ? selectedId : '';
}

export function _showAddTransport() {
  _transportEditing = null;
  byId('transportFormTitle').textContent = 'Add transport';
  byId('transportName').value = '';
  byId('transportSshHost').value = '';
  byId('transportSshUser').value = 'kali';
  byId('transportSshKeyPath').value = '';
  byId('transportTestResult').textContent = '';
  byId('transportForm').hidden = false;
  byId('addTransportBtn').hidden = true;
  byId('transportName').focus();
}

/** Open the same form against an existing transport.
 *
 * The PATCH half of `_saveTransport` has been here since transports shipped,
 * but `_transportEditing` was only ever assigned `null` -- three times -- so
 * nothing could reach it and an added transport could not be changed at all.
 * This is the missing entry point, not new machinery.
 *
 * Fields are pre-filled from the record rather than blanked: a PATCH sends all
 * four, so a blank form would rewrite the untouched ones to empty and the
 * server would refuse it ("SSH host cannot be empty") -- or worse, silently
 * accept a name-only edit that dropped the rest if that validation ever
 * loosened.
 */
export function _showEditTransport(transport) {
  if (!transport) return;
  _transportEditing = transport.id;
  byId('transportFormTitle').textContent = 'Edit transport';
  byId('transportName').value = transport.name || '';
  byId('transportSshHost').value = transport.ssh_host || '';
  byId('transportSshUser').value = transport.ssh_user || 'kali';
  byId('transportSshKeyPath').value = transport.ssh_key_path || '';
  byId('transportTestResult').textContent = '';
  byId('transportForm').hidden = false;
  byId('addTransportBtn').hidden = true;
  byId('transportName').focus();
}

/** Delete a transport, surfacing the server's refusal when it is still in use.
 *
 * The reason for reading `detail` rather than reporting the status: the server
 * refuses with 409 and a count ("2 backend(s) still use this transport --
 * delete or repoint them first") because ai_machines has no foreign key on
 * transport_id, so removing a referenced transport would leave those backends
 * permanently unable to connect. That message is the entire value of the
 * guard; a bare "Could not delete (409)" would leave the user to guess why.
 */
export async function _deleteTransport(transport, onDeleted) {
  if (!transport) return;
  const label = transport.name || 'this transport';
  if (!window.confirm(`Delete ${label}? Backends using it must be repointed first.`)) return;
  try {
    const resp = await apiFetch(`/api/transports/${encodeURIComponent(transport.id)}`,
                                {method: 'DELETE'});
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || body.error || `Could not delete transport (${resp.status})`);
    }
    await loadTransports();
    if (onDeleted) onDeleted();
    notifyResult('Transport deleted');
  } catch (error) {
    notifyResult(error.message, 'error');
  }
}

// ── Readiness: Check, then Init ───────────────────────────────────────────
//
// Two buttons rather than one, and the split is deliberate. Check only reads
// the far side; Init copies files, installs a token and enables a systemd
// service on a host this console does not own. Everything else the console
// does to a transport reads, so the one operation that writes gets its own
// deliberate click.
//
// They live on the transport group header, not on a machine card: there is
// one proxy per host, and machines share transports. Two Init buttons acting
// on the same host would make the second look broken because the first
// already did the work.

// Report lines are appended to the header rather than shown in an alert: the
// point of Check is a four-line breakdown, and "not ready" without saying
// which of the four failed sends the reader hunting -- which is exactly the
// hunt this whole feature exists to remove.
function _renderReadiness(header, data) {
  header.querySelectorAll('.transport-readiness').forEach(n => n.remove());
  const box = document.createElement('div');
  box.className = 'transport-readiness';

  if (!data.reachable) {
    const line = document.createElement('div');
    line.className = 'transport-check error';
    line.textContent = `unreachable: ${data.error || 'SSH failed'}`;
    box.appendChild(line);
    header.appendChild(box);
    return;
  }
  (data.checks || []).forEach(c => {
    const line = document.createElement('div');
    line.className = `transport-check ${c.ok ? 'ok' : 'error'}`;
    // textContent throughout: detail carries remote output (a path, a
    // systemctl state) and must never be parsed as markup.
    line.textContent = `${c.ok ? '✓' : '✗'} ${c.name}: ${c.detail}`;
    if (!c.ok && c.remedy) line.title = c.remedy;
    box.appendChild(line);
  });
  header.appendChild(box);
}

export async function _checkTransport(transport, header, btn) {
  if (!transport) return;
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try {
    const resp = await apiFetch(
      `/api/transports/${encodeURIComponent(transport.id)}/check`, {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.error || `Check failed (${resp.status})`);
    }
    _renderReadiness(header, data);
    if (data.ready && data.tunnel_started) {
      notifyResult(`${transport.name} is ready and connecting…`);
      // machines.js listens for this to refresh the status badge immediately
      // rather than leaving it stale for up to 5s until the next poll tick.
      document.dispatchEvent(new CustomEvent('wc:tunnel-start-queued'));
    } else if (data.ready) {
      // ready === true, tunnel_started === false: either already connected
      // (nothing to start) or nothing is assigned to this transport yet.
      notifyResult(`${transport.name} is ready`);
    } else {
      notifyResult(`${transport.name} is not ready — see the checks`, 'error');
    }
  } catch (error) {
    notifyResult(error.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

// ── Sync: push this checkout's git-tracked files to the transport ────────
//
// Same trust tier as Init: it writes to a host this console does not own.
// Unlike Init, it is safe to click repeatedly -- a no-op sync (nothing
// changed since the last one) costs one diff computation and pushes
// nothing, so no confirm() dialog gates it the way Init's does.

export async function _syncTransport(transport, header, btn) {
  if (!transport) return;
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
  try {
    const resp = await apiFetch(
      `/api/transports/${encodeURIComponent(transport.id)}/sync`, {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      throw new Error(data.reason || data.detail || `Sync failed (${resp.status})`);
    }
    notifyResult(
      data.files_changed > 0
        ? `${transport.name}: synced ${data.files_changed} file(s)`
        : `${transport.name}: already up to date`);
  } catch (error) {
    notifyResult(error.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

// ── Pending sync requests: an agent asked, a human decides ────────────────
//
// SYNC_REQUEST messages from sync_request_watcher.py land here as
// status='pending' rows -- nothing is pushed until approve() is called.
// See docs/superpowers/specs/2026-09-09-transport-project-sync-design.md.

export async function _loadPendingSyncRequests() {
  try {
    const resp = await apiFetch('/api/transports/sync-requests/pending');
    if (!resp.ok) return [];
    const data = await resp.json().catch(() => ({}));
    return data.requests || [];
  } catch {
    return [];
  }
}

export async function _resolveSyncRequest(transportId, requestId, action, onDone) {
  try {
    const resp = await apiFetch(
      `/api/transports/${encodeURIComponent(transportId)}/sync-requests/`
      + `${encodeURIComponent(requestId)}/${action}`, {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.reason || `${action} failed (${resp.status})`);
    }
    notifyResult(
      action === 'approve'
        ? (data.ok ? `Sync approved — ${data.files_changed ?? 0} file(s)` : (data.reason || 'Sync failed'))
        : 'Sync request rejected');
    if (onDone) onDone();
  } catch (error) {
    notifyResult(error.message, 'error');
  }
}

export async function _initTransport(transport, header, btn) {
  if (!transport) return;
  // Confirmed because it writes to another machine. Idempotent, so re-running
  // is safe -- but "safe to repeat" is not "safe to trigger by accident".
  if (!window.confirm(
        `Install and start the WebConsole proxy on ${transport.name}?\n\n`
        + 'This copies files and enables a systemd service on that host.')) return;
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Initialising…'; }
  try {
    const resp = await apiFetch(
      `/api/transports/${encodeURIComponent(transport.id)}/init`, {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      // The tail of the deploy output is the useful part of a failure, so it
      // goes to the console rather than being swallowed by a one-line toast.
      if (data.output) console.error('[transport init]', data.output);
      throw new Error(data.error || data.detail
                      || `Init failed (exit ${data.returncode ?? resp.status})`);
    }
    notifyResult(data.tunnel_started
      ? `${transport.name} initialised and connecting…`
      : `${transport.name} initialised — assign a backend to it to connect`);
    if (data.tunnel_started) {
      // machines.js listens for this to refresh the status badge immediately
      // rather than leaving it stale for up to 5s until the next poll tick.
      document.dispatchEvent(new CustomEvent('wc:tunnel-start-queued'));
    }
    // Immediately re-check, so the buttons never leave the reader guessing
    // whether it worked.
    await _checkTransport(transport, header, null);
  } catch (error) {
    notifyResult(error.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

export function _cancelTransportForm() {
  byId('transportForm').hidden = true;
  byId('addTransportBtn').hidden = false;
  _transportEditing = null;
}

export async function _testTransportForm() {
  const btn = byId('testTransport');
  const result = byId('transportTestResult');
  btn.disabled = true;
  result.textContent = 'Testing…';
  try {
    const resp = await apiFetch('/api/transports/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ssh_host: byId('transportSshHost').value.trim(),
        ssh_user: byId('transportSshUser').value.trim() || 'kali',
        ssh_key_path: byId('transportSshKeyPath').value.trim(),
      }),
    });
    const data = await resp.json();
    result.textContent = data.ok ? 'Connection OK' : `Failed: ${data.error || 'unknown'}`;
    result.className = data.ok ? 'ok' : 'error';
  } catch (err) {
    result.textContent = `Error: ${err.message}`;
    result.className = 'error';
  } finally {
    btn.disabled = false;
  }
}

export async function _saveTransport(onSaved) {
  const name = byId('transportName').value.trim();
  const ssh_host = byId('transportSshHost').value.trim();
  const ssh_user = byId('transportSshUser').value.trim() || 'kali';
  const ssh_key_path = byId('transportSshKeyPath').value.trim();
  if (!name) { byId('transportName').focus(); return; }
  if (!ssh_host) { byId('transportSshHost').focus(); return; }
  if (!ssh_key_path) { byId('transportSshKeyPath').focus(); return; }

  const save = byId('saveTransport');
  save.disabled = true;
  try {
    const body = {name, ssh_host, ssh_user, ssh_key_path};
    const resp = _transportEditing
      ? await apiFetch(`/api/transports/${encodeURIComponent(_transportEditing)}`, {
          method: 'PATCH', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        })
      : await apiFetch('/api/transports', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
    if (!resp.ok) throw new Error(`Could not save transport (${resp.status})`);
    _cancelTransportForm();
    await loadTransports();
    if (onSaved) onSaved();
    notifyResult('Transport saved');
  } catch (error) {
    notifyResult(error.message, 'error');
  } finally {
    save.disabled = false;
  }
}
