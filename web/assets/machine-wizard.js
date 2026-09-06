// SSH init wizard — three-step flow: SSH test → proxy detection → tunnel.
import {apiFetch} from './api.js?v=1';
import {byId, setStatus, notifyResult} from './app.js?v=38';

let _wizardMachineId = null;
let _wizardStep = 0;

// Called from app.js when Settings opens. Renders the wizard inside
// #panelSettings > .settings-panel if any ssh_proxy machine is pending.
export function _renderSshWizard(machines) {
  const panel = byId('panelSettings');
  if (!panel) return;

  const sshMachines = machines.filter(m => m.provider === 'ssh_proxy');
  if (!sshMachines.length) return;

  // Remove old wizard if present.
  const old = byId('sshWizard');
  if (old) old.remove();

  const box = document.createElement('div');
  box.id = 'sshWizard';
  box.className = 'settings-panel';
  box.hidden = false;

  box.innerHTML = `
    <h3>SSH Proxy Setup</h3>
    <div class="wizard-steps">
      <div class="wizard-step" id="step1">
        <strong>Step 1: SSH Test</strong>
        <p id="step1Status">Testing SSH connection...</p>
      </div>
      <div class="wizard-step" id="step2">
        <strong>Step 2: Remote Detection</strong>
        <p id="step2Status">Waiting...</p>
      </div>
      <div class="wizard-step" id="step3">
        <strong>Step 3: Tunnel Test</strong>
        <p id="step3Status">Waiting...</p>
      </div>
    </div>
    <div class="machine-form-actions">
      <button class="btn-secondary" id="wizardCancel">Close</button>
      <button class="btn-primary" id="wizardStart">Run Wizard</button>
    </div>
  `;

  panel.insertBefore(box, panel.querySelector('.usage-content') || panel.lastElementChild);

  byId('wizardCancel').addEventListener('click', () => {
    panel.removeChild(box);
  });

  byId('wizardStart').addEventListener('click', () => {
    _wizardMachineId = sshMachines[0].id;
    _wizardStep = 0;
    _runWizard(sshMachines[0]);
  });
}

async function _runWizard(machine) {
  // Step 1: SSH test.
  byId('step1Status').textContent = 'Testing SSH connection...';
  byId('step1Status').className = '';
  try {
    const resp = await apiFetch('/api/init/ssh-test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ssh_host: machine.ssh_host || '',
        ssh_user: machine.ssh_user || 'kali',
        ssh_key_path: machine.ssh_key_path || '',
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      byId('step1Status').textContent = 'SSH connection OK';
      byId('step1Status').className = 'ok';
    } else {
      byId('step1Status').textContent = `SSH failed: ${data.error || 'unknown'}`;
      byId('step1Status').className = 'error';
      return;
    }
  } catch (err) {
    byId('step1Status').textContent = `SSH test error: ${err.message}`;
    byId('step1Status').className = 'error';
    return;
  }
  _wizardStep = 1;

  // Step 2: Remote probe.
  byId('step2Status').textContent = 'Probing remote host...';
  byId('step2Status').className = '';
  try {
    const resp = await apiFetch('/api/init/probe-remote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({machine_id: machine.id}),
    });
    const data = await resp.json();
    if (data.ok && data.results) {
      const r = data.results;
      const parts = [];
      if (r.claude !== 'NOT_FOUND') parts.push('claude CLI: OK');
      else parts.push('claude CLI: not found');
      if (r.python3 && !r.python3.includes('NOT_FOUND')) parts.push('python3: OK');
      else parts.push('python3: not found');
      if (r.disk) parts.push(`disk: ${r.disk}`);
      byId('step2Status').textContent = parts.join(' | ');
      byId('step2Status').className = parts.some(s => s.includes('not found')) ? 'warning' : 'ok';
    } else {
      byId('step2Status').textContent = `Probe failed: ${data.error || 'unknown'}`;
      byId('step2Status').className = 'error';
    }
  } catch (err) {
    byId('step2Status').textContent = `Probe error: ${err.message}`;
    byId('step2Status').className = 'error';
  }
  _wizardStep = 2;

  // Step 3: Start tunnel.
  byId('step3Status').textContent = 'Starting tunnel...';
  byId('step3Status').className = '';
  try {
    const resp = await apiFetch('/api/tunnel/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({machine_id: machine.id}),
    });
    const data = await resp.json();
    if (data.ok) {
      byId('step3Status').textContent = 'Tunnel started — polling for proxy...';
      byId('step3Status').className = 'ok';
    }
  } catch (err) {
    byId('step3Status').textContent = `Tunnel start failed: ${err.message}`;
    byId('step3Status').className = 'error';
  }
}
