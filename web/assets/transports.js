// Transport CRUD: the SSH connection a backend can optionally run over.
// See docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.
import {apiFetch} from './api.js?v=1';
import {byId} from './app.js?v=42';
// notifyResult comes from server-stats.js directly, not via app.js -- app.js
// only re-exports it there as part of unrelated, uncommitted work elsewhere
// in this shared tree; machines.js already imports it the same direct way.
import {notifyResult} from './server-stats.js?v=1';

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
