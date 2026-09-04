// ── Machines ──────────────────────────────────────────────────────────────────────
//
// This file was split out of app.js, and everything below is either state
// app.js still owns or a helper app.js still defines. `_activeMachineId` and
// `_machineEditing` go through setters rather than direct assignment: an
// imported `let` binding is read-only from the importing side (the same
// restriction app.js's own `previousFocus` comment documents), so this file
// can read them live but cannot reassign them directly. `_machines` is the
// exception -- an array's contents can be mutated in place without ever
// reassigning the binding, so it stays a plain import and this file empties
// and refills it instead of replacing it.
import {
  byId, storageGet, storageSet, _machines, _activeMachineId, _setActiveMachineId,
  _machineEditing, _setMachineEditing, _servedModels, _modelOptions, _modelsSource,
  _turnsByModel, loadModelsFor, _refreshServedModels,
  // Defined in app.js and called from the model rows built here. The app.js
  // split left these as bare references across the module boundary, so every
  // "offered"/"default" checkbox threw ReferenceError on change.
  _toggleModelOffered, _setModelDefault,
} from './app.js?v=32';
import {apiFetch} from './api.js?v=1';
import {notifyResult, setStatus} from './server-stats.js?v=1';

// loadInitialData() calls this at boot and loadBackends() calls it again
// whenever Settings opens; those two callers are not coordinated. Without the
// dedup below, two independent fetches raced to write the same shared
// _machines array, and a slow-to-resolve boot-time call could wipe a
// Settings-triggered render's data out from under it well after that render
// had already shown correct cards -- reproduced live: the panel opened with
// real machine cards, then a moment later the list went empty with no error
// at all, because the losing call's failure handler cleared _machines on its
// way out. A failed refresh now leaves the last good list alone instead of
// clearing it, and concurrent callers share one in-flight fetch instead of
// racing two.
let _machinesLoad = null;

async function _fetchMachines() {
  try {
    const resp = await apiFetch('/api/machines');
    // !resp.ok reaches here too -- fetch() only rejects on a network error,
    // not on a 4xx/5xx status. Falling through to the same "replace with
    // whatever we got" line as success used to overwrite a previously good
    // list with an empty one on every HTTP error, not just a thrown
    // exception -- the same erasure the catch below guards against, taking
    // a path the catch never sees.
    if (resp.ok) {
      const list = (await resp.json()).machines || [];
      _machines.length = 0;
      _machines.push(...list);
    }
  } catch {
    // Keep whatever _machines already held -- a failed refresh should not
    // erase a previously successful one.
  }
}

// force=true is for a caller that just changed the server's own state
// (activate/delete/save) and needs the *next* fetch to actually be new, not
// whatever GET was already in flight from an unrelated poll. Coalescing that
// caller onto a stale in-flight promise the way plain dedup does would
// re-render a machine that had just been deleted, still present, with no
// error at all -- the mutation succeeded but the read racing it predated it.
export async function loadMachines(force = false) {
  if (force || !_machinesLoad) {
    const load = _fetchMachines().finally(() => {
      if (_machinesLoad === load) _machinesLoad = null;
    });
    _machinesLoad = load;
  }
  await _machinesLoad;
  // Check for a stored active machine. Runs for every caller, not only the
  // one that triggered the fetch -- a caller that coalesced onto someone
  // else's in-flight promise still needs this to have run once, and an
  // early return here used to skip it for exactly that caller.
  const stored = storageGet('wc_active_machine');
  if (stored && _machines.some(m => m.id === stored)) {
    _setActiveMachineId(stored);
  }
}

// 'anthropic' is the wire protocol, not the vendor: a gateway speaking the
// Anthropic API at a custom base_url is still provider='anthropic'. The server
// classifies this (app.backend_kind) and the Usage tab gates its cost column on
// the same value, so read it rather than re-deriving it here -- two
// implementations agreeing by coincidence is a latent disagreement.
const _BACKEND_KIND_LABELS = {
  'anthropic': 'Anthropic API',
  'anthropic-compatible': 'Anthropic-compatible',
  'proxy': 'Claude Code proxy',
};

function _providerLabel(machine) {
  return _BACKEND_KIND_LABELS[machine.backend_kind]
    || (machine.provider === 'anthropic' ? 'Anthropic API' : 'Claude Code proxy');
}

// Per-machine model state, keyed by machine id: {models, active, default,
// source, reason, endpoint}. Fetched lazily so opening Settings does not
// query every configured backend at once. Exported: app.js's own model-picker
// code reads this Map by .get/.set/.has and never reassigns it, so a plain
// live-binding export is enough -- no setter needed, unlike _activeMachineId.
export const _modelsByMachine = new Map();

function _buildModelSection(machine) {
  const section = document.createElement('div');
  section.className = 'machine-models';

  if (machine.provider !== 'anthropic') {
    const note = document.createElement('p');
    note.className = 'machine-hint';
    note.textContent = 'A Claude Code proxy does not publish a model list.';
    section.appendChild(note);
    return section;
  }

  const entry = _modelsByMachine.get(machine.id);
  const toolbar = document.createElement('div');
  toolbar.className = 'models-toolbar';

  const status = document.createElement('span');
  status.className = 'models-status';
  if (!entry) {
    status.textContent = 'Loading models…';
  } else if (entry.source === 'endpoint') {
    const count = entry.models.length;
    status.textContent = `${count} model${count === 1 ? '' : 's'} from ${entry.endpoint}`;
  } else if (entry.reason) {
    // Never present a guess as the real list -- say why it is a guess.
    status.classList.add('models-status-warn');
    status.textContent = `${entry.reason} Showing built-in suggestions.`;
  } else {
    // No reason means the server called this fallback expected, not a fault --
    // an anthropic machine with no stored key cannot be probed over HTTP, yet
    // serves turns normally through the host login. Warning about it flagged a
    // working backend as broken, so this stays a quiet label.
    status.textContent = 'Built-in model list';
  }
  toolbar.appendChild(status);

  const refresh = document.createElement('button');
  refresh.type = 'button';
  // Its own class: it sits above .machine-actions, so sharing that class made
  // it the first .machine-action in the card and any selector reaching for
  // "the first action" landed on Refresh instead of Activate.
  refresh.className = 'machine-action models-refresh';
  refresh.textContent = 'Refresh';
  refresh.addEventListener('click', () => loadModelsFor(machine.id, true));
  toolbar.appendChild(refresh);
  section.appendChild(toolbar);

  if (!entry) return section;

  // An empty active list means every served model is offered. Rendering that
  // as all-checked keeps the feature opt-in; rendering it as none-checked
  // would imply the picker is empty, which it is not.
  const offersAll = entry.active.length === 0;

  // The rail is the point of this layout: it runs down the live backend and
  // terminates on the default row, so "this backend, this model" is one thing
  // to read rather than two facts to assemble.
  const body = document.createElement('div');
  body.className = 'models-body';
  body.appendChild(document.createElement('div')).className = 'models-rail';

  const grid = document.createElement('div');
  grid.className = 'models-grid';

  // The two controls were unlabelled, so nothing said which column offered a
  // model and which made it the default.
  const header = document.createElement('div');
  header.className = 'models-head';
  ['Offered', 'Default', 'Model', 'Turns', ''].forEach(label => {
    const cell = document.createElement('span');
    cell.textContent = label;
    header.appendChild(cell);
  });
  grid.appendChild(header);

  entry.models.forEach(model => {
    grid.appendChild(_buildModelRow(machine, model, entry, offersAll));
  });
  body.appendChild(grid);
  section.appendChild(body);

  const hint = document.createElement('p');
  hint.className = 'machine-hint';
  // Both controls are explained in either state. The all-offered wording used
  // to describe only the tickbox, so in the state every backend starts in, the
  // radio column was never accounted for at all.
  hint.textContent = offersAll
    ? 'All models are offered — tick a subset to narrow the picker. The selected ● is this backend’s default for new chats.'
    : 'Ticked models are offered when starting a turn; the selected ● is this backend’s default for new chats.';
  section.appendChild(hint);
  return section;
}

function _bareModel(id) {
  return id.slice(id.lastIndexOf('/') + 1);
}

function _turnsFor(modelId) {
  return _turnsByModel.get(_bareModel(modelId)) || 0;
}

function _peakTurns() {
  let peak = 0;
  for (const n of _turnsByModel.values()) peak = Math.max(peak, n);
  return peak;
}

function _buildModelRow(machine, model, entry, offersAll) {
  const row = document.createElement('div');
  row.className = 'model-item';
  // The rail's terminating node hangs off this class, so the default row is
  // what visually closes the path from the LIVE chip.
  if (entry.default === model.id) row.classList.add('model-item-default');

  const offered = document.createElement('input');
  offered.type = 'checkbox';
  offered.checked = offersAll || entry.active.includes(model.id);
  offered.setAttribute('aria-label', `Offer ${model.id}`);
  // Also a tooltip: the two controls sit unlabelled side by side, so a mouse
  // user had no way to tell the "offered" column from the "default" one.
  offered.title = `Offer ${model.id} when starting a turn`;
  offered.addEventListener('change', () => _toggleModelOffered(machine, model.id));
  row.appendChild(offered);

  const isDefault = document.createElement('input');
  isDefault.type = 'radio';
  isDefault.name = `default-model-${machine.id}`;
  isDefault.checked = entry.default === model.id;
  isDefault.setAttribute('aria-label', `Default to ${model.id}`);
  isDefault.title = `Make ${model.id} this backend's default for new chats`;
  isDefault.addEventListener('change', () => _setModelDefault(machine, model.id));
  row.appendChild(isDefault);

  const name = document.createElement('span');
  name.className = 'model-item-id';
  name.title = model.id;
  // "azure_ai/" repeated down the column buries the part that differs, so the
  // family is dimmed and the distinguishing name reads first.
  const slash = model.id.indexOf('/');
  if (slash > 0) {
    const family = document.createElement('span');
    family.className = 'model-item-family';
    family.textContent = model.id.slice(0, slash + 1);
    name.appendChild(family);
    name.appendChild(document.createTextNode(model.id.slice(slash + 1)));
  } else {
    name.textContent = model.id;
  }
  row.appendChild(name);

  // Traffic. Scaled against the busiest model anywhere, so the bars compare
  // across backends and not only within one.
  const turns = _turnsFor(model.id);
  const peak = _peakTurns();
  const bar = document.createElement('span');
  bar.className = 'model-bar';
  const fill = document.createElement('i');
  fill.style.width = peak && turns ? `${Math.max(1, (turns / peak) * 100)}%` : '0';
  bar.appendChild(fill);
  bar.title = `${turns.toLocaleString()} turns in the last 30 days`;
  row.appendChild(bar);

  const count = document.createElement('span');
  count.className = turns ? 'model-turns' : 'model-turns model-turns-zero';
  count.textContent = turns ? turns.toLocaleString() : 'never';
  row.appendChild(count);
  return row;
}

// The wires are drawn rather than declared, because a wire has to end at
// whichever backend is live and that position is only known once the cards
// have been laid out. Measured after render and again on resize; if the
// measurement fails the map still reads, since the LIVE chip carries the same
// fact in words.
export function _drawMapWires() {
  const wires = byId('mapWires');
  const map = byId('backendMap');
  const src = document.querySelector('.map-src');
  if (!wires || !map || !src) return;
  wires.replaceChildren();

  const cards = Array.from(document.querySelectorAll('#machineList .machine-card'));
  if (!cards.length) return;
  const base = wires.getBoundingClientRect();
  if (!base.height) return;   // panel is hidden; nothing to measure against
  const from = src.getBoundingClientRect();
  const startY = from.top + from.height / 2 - base.top;

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${base.width} ${base.height}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  cards.forEach(card => {
    const box = card.getBoundingClientRect();
    const endY = box.top + 22 - base.top;      // the card's header row
    const live = card.classList.contains('machine-active');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const w = base.width;
    path.setAttribute('d', `M0 ${startY} C${w * 0.55} ${startY} ${w * 0.45} ${endY} ${w} ${endY}`);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', live ? 'var(--ok)' : 'var(--line)');
    path.setAttribute('stroke-width', live ? '2.5' : '1.5');
    // A standby route exists but carries nothing, which is what a dashed line
    // says and a thin solid one does not.
    if (!live) path.setAttribute('stroke-dasharray', '3 3');
    svg.appendChild(path);
  });
  wires.appendChild(svg);
}

export function _renderMachineList() {
  const list = byId('machineList');
  list.replaceChildren();
  if (!_machines.length) {
    const empty = document.createElement('div');
    empty.className = 'sidebar-empty';
    empty.textContent = 'No machines yet. Add one below.';
    list.appendChild(empty);
    return;
  }
  _machines.forEach(m => {
    const card = document.createElement('div');
    card.className = 'machine-card';
    if (m.active) card.classList.add('machine-active');

    const top = document.createElement('div');
    top.className = 'machine-card-top';

    // Which backend is live was previously a 3px border. It is the single most
    // important fact on this panel, so it is stated in words.
    const state = document.createElement('span');
    state.className = m.active ? 'machine-state machine-state-live' : 'machine-state';
    state.textContent = m.active ? 'LIVE' : 'STANDBY';
    top.appendChild(state);

    const ident = document.createElement('div');
    ident.className = 'machine-ident';

    const name = document.createElement('span');
    name.className = 'machine-name';
    name.textContent = m.name;
    name.title = m.name;
    ident.appendChild(name);

    const meta = document.createElement('div');
    meta.className = 'machine-meta';
    // Anthropic machines are identified by their endpoint; host/port only
    // describe the transport and would read as noise on the card.
    const where = m.provider === 'anthropic'
      ? (m.base_url || 'https://api.anthropic.com')
      : m.host;
    meta.textContent = where;
    meta.title = where;
    ident.appendChild(meta);
    top.appendChild(ident);

    const provider = document.createElement('span');
    provider.className = 'machine-provider';
    provider.textContent = _providerLabel(m);
    top.appendChild(provider);

    card.appendChild(top);

    // The models a backend serves belong to the backend, so they render inside
    // its card rather than in a separate tab that silently described whichever
    // machine happened to be active.
    card.appendChild(_buildModelSection(m));

    const actions = document.createElement('div');
    actions.className = 'machine-actions';

    if (!m.active) {
      const activateBtn = document.createElement('button');
      activateBtn.type = 'button';
      activateBtn.className = 'machine-action';
      activateBtn.textContent = 'Activate';
      activateBtn.addEventListener('click', () => _activateMachine(m.id));
      actions.appendChild(activateBtn);
    }

    const testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'machine-action';
    testBtn.textContent = 'Test';
    // Pass the button itself: an active machine has no Activate button, so any
    // positional lookup lands on a different action for active vs inactive cards.
    testBtn.addEventListener('click', () => _testMachine(m.id, testBtn));
    actions.appendChild(testBtn);

    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'machine-action';
    editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', () => _editMachine(m.id));
    actions.appendChild(editBtn);

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'machine-action machine-action-danger';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', () => _deleteMachine(m.id));
    actions.appendChild(delBtn);

    card.appendChild(actions);
    list.appendChild(card);
  });

  const total = byId('mapTotal');
  if (total) {
    let sum = 0;
    for (const n of _turnsByModel.values()) sum += n;
    total.textContent = sum ? `${sum.toLocaleString()} turns` : 'no turns yet';
  }
  // Layout has to settle before the cards can be measured.
  requestAnimationFrame(_drawMapWires);
}

export async function _activateMachine(id) {
  try {
    const resp = await apiFetch(`/api/machines/${encodeURIComponent(id)}/activate`, {method: 'POST'});
    if (!resp.ok) throw new Error('Could not activate machine');
    await loadMachines(true);
    _setActiveMachineId(id);
    storageSet('wc_active_machine', id);
    // The model picker offers what the *active* machine serves, and
    // _refreshServedModels() derives that from _machines -- so changing which
    // machine is active invalidates it. Nothing re-derived it here, so
    // _servedModels kept whatever it held from before activation. On a console
    // whose machine starts inactive that is [], and populateModelPicker reads an
    // empty served list as "no backend is active" and falls back to
    // _modelOptions: a single id, the global default. The picker then offered
    // one model no matter how many the backend served, and stayed that way
    // until an unrelated model toggle happened to call the sync from
    // _saveMachineModels and repair it.
    //
    // loadModelsFor first, because the newly active machine's list may never
    // have been fetched; it returns early when cached, which is why the sync is
    // called explicitly rather than left to it.
    await loadModelsFor(id);
    _refreshServedModels();
    _renderMachineList();
    notifyResult('Machine activated');
  } catch (error) {
    notifyResult(error.message, 'error');
  }
}

async function _testMachine(id, btn) {
  if (btn) { btn.textContent = 'Testing…'; btn.disabled = true; }
  try {
    const resp = await apiFetch(`/api/machines/${encodeURIComponent(id)}/test`, {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    // The endpoint reports {ok, status, error} only — the address comes from
    // the machine record we already hold, not from the response.
    const machine = _machines.find(m => m.id === id);
    const target = machine ? `${machine.host}:${machine.port}` : 'machine';
    if (data.ok) {
      notifyResult(`Connected to ${target}`);
    } else {
      notifyResult(`Could not reach ${target}: ${data.error || data.status || 'unreachable'}`, 'error');
    }
  } catch (error) {
    notifyResult(`Test failed: ${error.message}`, 'error');
  } finally {
    if (btn) { btn.textContent = 'Test'; btn.disabled = false; }
  }
}

async function _deleteMachine(id) {
  try {
    const resp = await apiFetch(`/api/machines/${encodeURIComponent(id)}`, {method: 'DELETE'});
    if (!resp.ok) throw new Error('Could not delete machine');
    if (_activeMachineId === id) _setActiveMachineId(null);
    await loadMachines(true);
    _renderMachineList();
    notifyResult('Machine deleted');
  } catch (error) {
    notifyResult(error.message, 'error');
  }
}

// Anthropic machines are configured by endpoint, proxy machines by host, so
// only one of the two field groups is ever relevant.
// Exported: app.js wires this as the 'change' listener on #machineProvider.
export function _syncMachineProviderFields() {
  const provider = byId('machineProvider').value;
  const isAnthropic = provider === 'anthropic';
  byId('machineProxyFields').hidden = isAnthropic;
  byId('machineAnthropicFields').hidden = !isAnthropic;
  // The whole api_key group, not just its hint. Only an anthropic backend
  // carries its key to the CLI; a proxy machine's key is stored and then never
  // read by any turn, so offering the field there asked for a credential that
  // could not take effect.
  byId('machineApiKeyFields').hidden = !isAnthropic;
  byId('machineModel').placeholder = isAnthropic ? 'claude-opus-5' : 'claude-sonnet-5';
}

export function _editMachine(id) {
  const m = _machines.find(x => x.id === id);
  if (!m) return;
  _setMachineEditing(id);
  byId('machineFormTitle').textContent = 'Edit machine';
  byId('machineName').value = m.name;
  byId('machineProvider').value = m.provider === 'anthropic' ? 'anthropic' : 'proxy';
  byId('machineHost').value = m.host;
  byId('machineBaseUrl').value = m.base_url || '';
  byId('machineModel').value = m.model;
  byId('machineApiKey').value = '';
  byId('machineApiKey').placeholder = 'Leave blank to keep current';
  _syncMachineProviderFields();
  byId('machineForm').hidden = false;
  byId('addMachineBtn').hidden = true;
  byId('machineName').focus();
}

export async function _saveMachine() {
  const name = byId('machineName').value.trim();
  const provider = byId('machineProvider').value === 'anthropic' ? 'anthropic' : 'proxy';
  const isAnthropic = provider === 'anthropic';
  const host = byId('machineHost').value.trim();
  const base_url = byId('machineBaseUrl').value.trim();
  const model = (byId('machineModel').value || '').trim()
    || (isAnthropic ? 'claude-opus-5' : 'claude-sonnet-5');
  const api_key = byId('machineApiKey').value.trim() || null;

  if (!name) { byId('machineName').focus(); return; }
  if (!isAnthropic && !host) { byId('machineHost').focus(); return; }

  const save = byId('saveMachine');
  save.disabled = true;
  try {
    let resp;
    // Only send fields the form actually collects — the server rejects the
    // whole request if the body carries any field outside its allowlist.
    const body = { name, provider, model };
    if (isAnthropic) {
      // Blank means "the default endpoint"; the server fills it in.
      if (base_url) body.base_url = base_url;
      // Only an anthropic backend consumes the key, so only that provider
      // sends one. Storing it for a proxy machine put a live credential in the
      // database that no turn could ever use -- cost with no effect.
      if (api_key !== null) body.api_key = api_key;
    } else {
      body.host = host;
    }
    if (_machineEditing) {
      resp = await apiFetch(`/api/machines/${encodeURIComponent(_machineEditing)}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
    } else {
      resp = await apiFetch('/api/machines', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
    }
    if (!resp.ok) {
      const ct = resp.headers.get('content-type') || '';
      let detail = '';
      if (ct.includes('application/json')) {
        try {
          const data = await resp.json();
          // The API reports failures as {"error": ...}; detail/message are
          // fallbacks for FastAPI's own validation responses.
          detail = data.error || data.detail || data.message || '';
        } catch { /* ignore */ }
      }
      if (!detail) {
        try {
          detail = await resp.text();
        } catch {
          detail = '';
        }
      }
      throw new Error(detail
        ? `Could not save machine: ${detail}`
        : `Could not save machine (${resp.status} ${resp.statusText})`);
    }
    _setMachineEditing(null);
    byId('machineForm').hidden = true;
    byId('addMachineBtn').hidden = false;
    await loadMachines(true);
    _renderMachineList();
    setStatus('Machine saved', 'success');
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    save.disabled = false;
  }
}

export function _showAddMachine() {
  _setMachineEditing(null);
  byId('machineFormTitle').textContent = 'Add machine';
  byId('machineName').value = '';
  // Default to the API Claude Code itself uses, rather than the proxy.
  byId('machineProvider').value = 'anthropic';
  byId('machineHost').value = '';
  byId('machineBaseUrl').value = '';
  byId('machineModel').value = '';
  byId('machineApiKey').value = '';
  byId('machineApiKey').placeholder = 'Optional';
  _syncMachineProviderFields();
  byId('machineForm').hidden = false;
  byId('addMachineBtn').hidden = true;
  byId('machineName').focus();
}