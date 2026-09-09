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
  backendKindLabel,
} from './app.js?v=53';
import {apiFetch} from './api.js?v=1';
import {notifyResult, setStatus} from './server-stats.js?v=1';
import {_transports, loadTransports, populateTransportPicker,
  // The transport group header offers these; see _buildTransportHeader.
  _showEditTransport, _deleteTransport,
  // Check / Init on the transport header -- see _buildTransportHeader.
  _checkTransport, _initTransport} from './transports.js?v=3';

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

// 'claude_code' is the wire protocol: a gateway speaking the Anthropic API at a
// custom base_url still has provider='claude_code'. The server
// classifies this (app.backend_kind) and the Usage tab gates its cost column on
// the same value, so read it rather than re-deriving it here -- two
// implementations agreeing by coincidence is a latent disagreement.
//
// The label text itself used to be a second, separately-maintained copy of
// app.js's own kind->label table (backendKindLabel) -- neither copy got the
// ssh_proxy entry added when that provider shipped, so this one fell through
// to the anthropic/else guess and showed "Claude Code proxy" for an ssh_proxy
// machine. Imported from app.js now instead, the same file this already reads
// _machines, byId, etc. from, so there is exactly one table to update the
// next time a provider type is added.
function _providerLabel(machine) {
  return backendKindLabel(machine.backend_kind);
}

// ── Transport group status ─────────────────────────────────────────────────
// Three states, derived entirely from data already fetched -- no schema
// change, no new endpoint. "Active" means a turn could actually run on this
// group right now; "Disabled" is deliberate (every machine on it turned
// off); "Uninitialized" is everything else -- added but never Checked/Inited,
// or a transport with no machine assigned to it yet.
const TRANSPORT_STATUS_ORDER = {active: 0, uninitialized: 1, disabled: 2};
const TRANSPORT_STATUS_LABEL = {
  active: 'Active', uninitialized: 'Uninitialized', disabled: 'Disabled',
};

export function _transportStatus(machines, tunnelStatusCache = {}) {
  if (!machines.length) return 'uninitialized';
  // enabled defaults true server-side; explicit false is the only way a
  // machine reads as off. Disabled wins over everything else on this group --
  // a deliberately-off backend must never read as "Active" just because its
  // tunnel happens to still be up from before it was disabled.
  if (!machines.some(m => m.enabled !== false)) return 'disabled';
  // Local/direct machines have no tunnel to check -- enabled is the whole
  // answer for them, and every machine here shares that fate (a local group
  // is never split between transported and not).
  const withTransport = machines.filter(m => m.transport_id);
  if (!withTransport.length) return 'active';
  // One shared tunnel per transport (Task 5): starting it for one machine
  // brings the whole connection up for all of them, so the first machine's
  // status speaks for the group.
  const status = tunnelStatusCache[withTransport[0].id];
  return status && status.proxy_ok ? 'active' : 'uninitialized';
}

// ── SSH Tunnel toggle ──────────────────────────────────────────────
let _tunnelStatusCache = {};

export function _setTunnelStatus(status) {
  _tunnelStatusCache = status || {};
  _renderMachineList(); // refresh badges
}

async function _toggleSshTunnel(machineId, badge) {
  const current = _tunnelStatusCache[machineId];
  try {
    const action = current && current.tunnel_up ? 'stop' : 'start';
    await apiFetch(`/api/tunnel/${action}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({machine_id: machineId}),
    });
  } catch (err) {
    // Non-fatal; badge will update on next poll.
  }
}

// Poll tunnel status every 5s when any ssh_proxy machine exists.
let _tunnelPollId = null;

async function _refreshTunnelStatus() {
  try {
    const resp = await apiFetch('/api/tunnel/status');
    if (resp.ok) {
      _tunnelStatusCache = await resp.json();
      _renderMachineList();
    }
  } catch (_) { /* ignore */ }
}

export function _pollTunnelStatus(active) {
  if (active && !_tunnelPollId) {
    _tunnelPollId = setInterval(_refreshTunnelStatus, 5000);
  } else if (!active && _tunnelPollId) {
    clearInterval(_tunnelPollId);
    _tunnelPollId = null;
  }
}

// transports.js dispatches this after Init, or a fully-passing Check, queues
// a tunnel start -- so the badge does not sit on stale data for up to 5s.
// A custom event rather than an import, because machines.js already imports
// FROM transports.js (Edit/Delete/Check/Init) and the reverse import would be
// a cycle. Same decoupling app.js uses for supervisor-map.js's "open this
// chat" callback.
document.addEventListener('wc:tunnel-start-queued', _refreshTunnelStatus);

// Per-machine model state, keyed by machine id: {models, active, default,
// source, reason, endpoint}. Fetched lazily so opening Settings does not
// query every configured backend at once. Exported: app.js's own model-picker
// code reads this Map by .get/.set/.has and never reassigns it, so a plain
// live-binding export is enough -- no setter needed, unlike _activeMachineId.
export const _modelsByMachine = new Map();

function _buildModelSection(machine) {
  const section = document.createElement('div');
  section.className = 'machine-models';

  if (machine.provider !== 'claude_code' && machine.provider !== 'direct') {
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
const SVG_NS = 'http://www.w3.org/2000/svg';

/** One bezier in a wire gutter, from *startY* on the left edge to *endY* on
 *  the right. `live` is the only styling input: a route that carries turns is
 *  solid and accented, one that merely exists is thin and dashed -- a standby
 *  route exists but carries nothing, which is what a dashed line says and a
 *  thin solid one does not. */
function _wire(svg, w, startY, endY, live) {
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', `M0 ${startY} C${w * 0.55} ${startY} ${w * 0.45} ${endY} ${w} ${endY}`);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', live ? 'var(--ok)' : 'var(--line)');
  path.setAttribute('stroke-width', live ? '2.5' : '1.5');
  if (!live) path.setAttribute('stroke-dasharray', '3 3');
  svg.appendChild(path);
}

function _wireSvg(gutter) {
  const base = gutter.getBoundingClientRect();
  if (!base.height) return null;   // panel is hidden; nothing to measure
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${base.width} ${base.height}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  return {svg, base};
}

const _midY = (el, base) => {
  const box = el.getBoundingClientRect();
  return box.top + box.height / 2 - base.top;
};

/** Draw the two wire gutters: From -> Transports, then Transports -> Run On.
 *
 * Two gaps rather than one, because the path a turn takes has two hops and the
 * old single-gutter map could not show where the middle one was -- or that for
 * a direct backend there is no middle hop at all.
 *
 * The second gutter aims at each machine card when a group is expanded and at
 * the group header when it is collapsed: a collapsed group has no cards to aim
 * at, and dropping its wire entirely would make a transport look unreachable
 * rather than merely folded up.
 *
 * Falls back to the original single-gutter behaviour when the transport column
 * is absent, which is the case below 620px where the CSS hides it.
 */
export function _drawMapWires() {
  const g1 = byId('mapWires');
  const g2 = byId('mapWires2');
  const src = document.querySelector('.map-src');
  if (!g1 || !src) return;
  g1.replaceChildren();
  if (g2) g2.replaceChildren();

  const spine = Array.from(document.querySelectorAll('#transportSpine .spine-entry'));
  const cards = Array.from(document.querySelectorAll('#machineList .machine-card'));

  // No transport column rendered (narrow viewport): keep the two-column map.
  if (!g2 || !spine.length) {
    if (!cards.length) return;
    const made = _wireSvg(g1);
    if (!made) return;
    const startY = _midY(src, made.base);
    cards.forEach(card => _wire(
      made.svg, made.base.width, startY,
      card.getBoundingClientRect().top + 22 - made.base.top,
      card.classList.contains('machine-active')));
    g1.appendChild(made.svg);
    return;
  }

  // Gutter 1: the composer to each transport entry.
  const first = _wireSvg(g1);
  if (first) {
    const startY = _midY(src, first.base);
    spine.forEach(entry => _wire(
      first.svg, first.base.width, startY, _midY(entry, first.base),
      entry.classList.contains('spine-entry-current')));
    g1.appendChild(first.svg);
  }

  // Gutter 2: each transport entry to the backends reachable through it.
  const second = _wireSvg(g2);
  if (!second) return;
  spine.forEach(entry => {
    const key = entry.dataset.group;
    const startY = _midY(entry, second.base);
    const header = document.querySelector(
      `#machineList .transport-group-header[data-group="${CSS.escape(key)}"]`);
    // Cards belonging to this group are the siblings between its header and
    // the next one. Reading the DOM rather than re-deriving membership keeps
    // this honest about what is actually on screen, collapsed or not.
    const owned = [];
    for (let el = header && header.nextElementSibling; el; el = el.nextElementSibling) {
      if (el.classList.contains('transport-group-header')) break;
      if (el.classList.contains('machine-card')) owned.push(el);
    }
    const targets = owned.length ? owned : (header ? [header] : []);
    targets.forEach(target => _wire(
      second.svg, second.base.width, startY,
      target.getBoundingClientRect().top
        + (target === header ? target.getBoundingClientRect().height / 2 : 22)
        - second.base.top,
      target.classList.contains('machine-active')));
  });
  g2.appendChild(second.svg);
}

// Why Disable would be refused, or '' when it would succeed.
//
// The server refuses with 409 either way; this only lets the button say so
// before it is pressed. The 409 path still exists for the race where a
// conversation is pinned between render and click.
//
// Only the default case is knowable from the list payload -- pin counts are
// not carried there, so a pinned non-default backend still learns its fate
// from the server. Reporting the obstacle we *can* see beats reporting none.
/** Why this backend cannot be disabled, or '' if it can.
 *
 * Both reasons the server refuses are checked here, so the button is greyed out
 * with the reason in its tooltip rather than accepting a click and reporting a
 * 409. That is not only tidier: a 4xx response is written to the browser
 * console as a failed request no matter how completely the handler deals with
 * it, so a refusal that is entirely expected still reads as an exception, with
 * a stack. The only way to not log it is to not send the request.
 *
 * The 409 handler in _setMachineEnabled stays. This check is derived from a
 * list loaded earlier, so a conversation pinned in another tab in between
 * leaves the button live -- and a real race is exactly what a 409 is for.
 */
function _disableObstacle(m) {
  if (m.active) return 'This is the default backend — make another the default first.';
  const pinned = Number(m.pinned_total) || 0;
  if (pinned > 0) {
    return pinned === 1
      ? 'One conversation is pinned to this backend — repoint or delete it first.'
      : `${pinned} conversations are pinned to this backend — repoint or delete them first.`;
  }
  return '';
}

function _buildMachineCard(m) {
    const card = document.createElement('div');
    card.className = 'machine-card';
    if (m.active) card.classList.add('machine-active');
    if (!m.enabled) card.classList.add('machine-disabled');

    const top = document.createElement('div');
    top.className = 'machine-card-top';

    // Which backend is live was previously a 3px border. It is the single most
    // important fact on this panel, so it is stated in words.
    // Three states now, not two. `m.active` means "is the default" -- the
    // column keeps that meaning deliberately (renaming it would flip a column
    // seven resolvers read); only the wording changed. A shelved backend used
    // to render as STANDBY, indistinguishable from one merely not in use.
    const state = document.createElement('span');
    if (!m.enabled) {
      state.className = 'machine-state machine-state-off';
      state.textContent = 'Inactive';
    } else if (m.active) {
      state.className = 'machine-state machine-state-live';
      state.textContent = 'Default';
    } else {
      state.className = 'machine-state';
      state.textContent = 'Active';
    }
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
    const where = m.provider === 'claude_code'
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

    if (m.enabled && !m.active) {
      const defaultBtn = document.createElement('button');
      defaultBtn.type = 'button';
      defaultBtn.className = 'machine-action';
      defaultBtn.textContent = 'Make default';
      defaultBtn.addEventListener('click', () => _activateMachine(m.id));
      actions.appendChild(defaultBtn);
    }

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'machine-action';
    toggleBtn.textContent = m.enabled ? 'Disable' : 'Enable';
    if (m.enabled) {
      const obstacle = _disableObstacle(m);
      if (obstacle) {
        toggleBtn.disabled = true;
        toggleBtn.title = obstacle;
      }
    }
    toggleBtn.addEventListener('click', () => _setMachineEnabled(m, !m.enabled));
    actions.appendChild(toggleBtn);

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
    return card;
}

// Collapsed groups, by group key ('direct' or a transport id). A plain Set,
// same pattern supervisor-map.js already uses for its own collapsible nodes --
// one convention for "remembers which groups you closed" across this codebase.
let _collapsedGroups = new Set();
// Group keys seeded with their status-based default collapse state exactly
// once. Without this, re-deriving "should this start collapsed" on every
// render would re-collapse a Disabled group the user had just opened back up.
let _seededGroups = new Set();

function _toggleGroupCollapse(key) {
  if (_collapsedGroups.has(key)) _collapsedGroups.delete(key);
  else _collapsedGroups.add(key);
  _renderMachineList();
}

// One transport's group header: collapse toggle, status + count, a
// tunnel-toggle badge -- reusing the existing _toggleSshTunnel, keyed by the
// FIRST machine in the group (since starting the tunnel for one machine on a
// shared transport brings the whole connection up for all of them, per Task
// 5) -- and the transport actions. `key` is 'direct' for the local group or
// the transport's own id; both need a stable identity to collapse by.
function _buildTransportHeader(label, machines, transport, key) {
  const status = _transportStatus(machines, _tunnelStatusCache);
  if (!_seededGroups.has(key)) {
    _seededGroups.add(key);
    // Least urgent, so it starts out of the way; everything else starts open
    // so nothing is hidden on first load.
    if (status === 'disabled') _collapsedGroups.add(key);
  }
  const collapsed = _collapsedGroups.has(key);

  const header = document.createElement('div');
  header.className = `transport-group-header transport-status-${status}`;

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'transport-collapse-toggle';
  toggle.textContent = collapsed ? '▸' : '▾';
  toggle.setAttribute('aria-expanded', String(!collapsed));
  toggle.setAttribute('aria-label', `${collapsed ? 'Expand' : 'Collapse'} ${label}`);
  toggle.addEventListener('click', () => _toggleGroupCollapse(key));
  header.appendChild(toggle);

  const text = document.createElement('span');
  text.className = 'chat-section-label';
  text.textContent = label;
  header.appendChild(text);

  const statusBadge = document.createElement('span');
  statusBadge.className = `transport-status-badge transport-status-badge-${status}`;
  statusBadge.textContent = TRANSPORT_STATUS_LABEL[status];
  header.appendChild(statusBadge);

  const count = document.createElement('span');
  count.className = 'transport-count';
  count.textContent = `${machines.length} machine${machines.length === 1 ? '' : 's'}`;
  header.appendChild(count);

  // The Direct group is local: no SSH tunnel exists for it to toggle.
  if (machines.length && key !== 'direct') {
    const badge = document.createElement('span');
    badge.className = 'machine-badge machine-badge-ssh';
    badge.title = 'Click to start tunnel';
    badge.textContent = 'SSH';
    badge.addEventListener('click', () => _toggleSshTunnel(machines[0].id, badge));
    header.appendChild(badge);
  }
  // Optional because one caller has no transport record to offer: the
  // "(unknown transport)" group exists for machines whose transport_id points
  // at a row that is gone, and there is nothing there to edit or delete.
  if (transport) {
    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'transport-action';
    editBtn.textContent = 'Edit';
    editBtn.setAttribute('aria-label', `Edit transport ${transport.name}`);
    editBtn.addEventListener('click', () => _showEditTransport(transport));
    header.appendChild(editBtn);

    // Check before Init, in that order, because that is the order they should
    // be used: SSH succeeding says nothing about whether the far side can
    // serve a turn, and Check names which of the four prerequisites is
    // missing. Init is the only control here that writes to another host.
    const checkBtn = document.createElement('button');
    checkBtn.type = 'button';
    checkBtn.className = 'transport-action';
    checkBtn.textContent = 'Check';
    checkBtn.title = 'Is the far side ready to serve turns?';
    checkBtn.setAttribute('aria-label', `Check transport ${transport.name}`);
    checkBtn.addEventListener(
      'click', () => _checkTransport(transport, header, checkBtn));
    header.appendChild(checkBtn);

    const initBtn = document.createElement('button');
    initBtn.type = 'button';
    initBtn.className = 'transport-action';
    initBtn.textContent = 'Init';
    initBtn.title = 'Install and start the WebConsole proxy on this host';
    initBtn.setAttribute('aria-label', `Initialise transport ${transport.name}`);
    initBtn.addEventListener(
      'click', () => _initTransport(transport, header, initBtn));
    header.appendChild(initBtn);

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'transport-action transport-action-danger';
    delBtn.textContent = 'Delete';
    delBtn.setAttribute('aria-label', `Delete transport ${transport.name}`);
    delBtn.addEventListener('click', () => _deleteTransport(transport, _renderMachineList));
    header.appendChild(delBtn);
  }
  return header;
}

/** The ordered groups the Backends panel shows, computed once.
 *
 * Both columns render from this. The transport column has to sit in the same
 * order as the Run On list or the wires between them cross, which is a worse
 * picture than the two-column map it replaces -- and a second copy of this
 * ordering would be a copy that drifts. `backendKindLabel` is imported rather
 * than re-tabulated in this file for exactly that reason.
 *
 * Each group: {key, label, spineLabel, machines, transport, status, direct}.
 * `key` is what _collapsedGroups is keyed by and what a spine entry carries in
 * data-group to find its header.
 */
export function _machineGroups() {
  const local = _machines.filter(m => !m.transport_id);
  const byTransport = new Map();
  _machines.forEach(m => {
    if (!m.transport_id) return;
    if (!byTransport.has(m.transport_id)) byTransport.set(m.transport_id, []);
    byTransport.get(m.transport_id).push(m);
  });

  const groups = [];
  // Direct is pinned first regardless of status -- it is where a fresh
  // account's own backend lives, and it is never what someone is hunting
  // for. Everything past it earns its position: real transports sorted by
  // status (Active first, Disabled last -- the ones needing attention over
  // the ones deliberately parked) and alphabetically within a status.
  if (local.length) {
    groups.push({
      key: 'direct', label: 'Direct', spineLabel: 'direct',
      machines: local, transport: null, direct: true,
      status: _transportStatus(local, _tunnelStatusCache),
    });
  }

  // Every known transport gets a header, even one with no backend pointed at
  // it yet -- a transport just created should read as "via <name>" right
  // away, not only once a machine is assigned to it.
  const renderedTransportIds = new Set();
  [..._transports].sort((a, b) => {
    const sa = TRANSPORT_STATUS_ORDER[_transportStatus(byTransport.get(a.id) || [], _tunnelStatusCache)];
    const sb = TRANSPORT_STATUS_ORDER[_transportStatus(byTransport.get(b.id) || [], _tunnelStatusCache)];
    return sa !== sb ? sa - sb : a.name.localeCompare(b.name);
  }).forEach(transport => {
    renderedTransportIds.add(transport.id);
    const machines = byTransport.get(transport.id) || [];
    groups.push({
      key: transport.id, label: `via ${transport.name}`,
      spineLabel: transport.name, machines, transport, direct: false,
      status: _transportStatus(machines, _tunnelStatusCache),
    });
  });

  // A machine pointed at a transport that no longer exists must not silently
  // vanish from the list. Sorted after every real transport, same status
  // ordering, since there is no transport row to prioritise by name.
  [...byTransport.keys()]
    .filter(id => !renderedTransportIds.has(id))
    .sort((a, b) => TRANSPORT_STATUS_ORDER[_transportStatus(byTransport.get(a), _tunnelStatusCache)]
      - TRANSPORT_STATUS_ORDER[_transportStatus(byTransport.get(b), _tunnelStatusCache)])
    .forEach(transportId => {
      const machines = byTransport.get(transportId);
      groups.push({
        key: `unknown:${transportId}`, label: 'via (unknown transport)',
        spineLabel: '(unknown)', machines, transport: null, direct: false,
        status: _transportStatus(machines, _tunnelStatusCache),
      });
    });

  return groups;
}

/** The read-only transport column.
 *
 * Deliberately carries no actions. Edit/Delete/Check/Init live in the Run On
 * group header and nowhere else, so there is one home per action; this column
 * exists to anchor the wires and to be a way of finding a group. Clicking an
 * entry expands its group if collapsed and scrolls to it.
 */
function _renderTransportSpine(groups) {
  const spine = byId('transportSpine');
  if (!spine) return;                  // two-column markup, or a partial page
  spine.replaceChildren();
  groups.forEach(group => {
    const entry = document.createElement('button');
    entry.type = 'button';
    entry.className = 'spine-entry' + (group.direct ? ' spine-entry-direct' : '');
    if (group.machines.some(m => m.active)) entry.classList.add('spine-entry-current');
    entry.dataset.group = group.key;
    const name = document.createElement('b');
    // "direct" rather than a transport name: the absence of a hop is the fact
    // this column exists to state, and the two-column map could not say it.
    name.textContent = group.spineLabel;
    entry.appendChild(name);
    const badge = document.createElement('span');
    badge.className = `transport-status-badge transport-status-badge-${group.status}`;
    badge.textContent = TRANSPORT_STATUS_LABEL[group.status];
    entry.appendChild(badge);
    entry.title = `${group.label} — ${group.machines.length} backend(s)`;
    entry.addEventListener('click', () => _revealGroup(group.key));
    spine.appendChild(entry);
  });
}

/** Expand, scroll to and flash the Run On group a spine entry points at. */
function _revealGroup(key) {
  if (_collapsedGroups.has(key)) {
    _collapsedGroups.delete(key);
    _renderMachineList();              // rebuilds the header we are about to find
  }
  const header = document.querySelector(
    `#machineList .transport-group-header[data-group="${CSS.escape(key)}"]`);
  if (!header) return;
  header.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  header.classList.add('transport-group-flash');
  setTimeout(() => header.classList.remove('transport-group-flash'), 1200);
}

export function _renderMachineList() {
  const list = byId('machineList');
  list.replaceChildren();
  if (!_machines.length && !_transports.length) {
    const empty = document.createElement('div');
    empty.className = 'sidebar-empty';
    empty.textContent = 'No machines yet. Add one below.';
    list.appendChild(empty);
    _renderTransportSpine([]);
    return;
  }

  const groups = _machineGroups();
  groups.forEach(group => {
    const header = _buildTransportHeader(
      group.label, group.machines, group.transport, group.key);
    // How a spine entry finds its partner. Set here rather than inside
    // _buildTransportHeader so that function keeps its current signature.
    header.dataset.group = group.key;
    list.appendChild(header);
    if (!_collapsedGroups.has(group.key)) {
      group.machines.forEach(m => list.appendChild(_buildMachineCard(m)));
    }
  });
  _renderTransportSpine(groups);

  const total = byId('mapTotal');
  if (total) {
    let sum = 0;
    for (const n of _turnsByModel.values()) sum += n;
    total.textContent = sum ? `${sum.toLocaleString()} turns` : 'no turns yet';
  }
  // Layout has to settle before the cards can be measured.
  requestAnimationFrame(_drawMapWires);
}

/** Enable or disable a backend, surfacing the server's refusal in full.
 *
 * The 409 body carries the pinned conversations by name. Rendering them is the
 * point: "8 conversations are pinned" sends the reader hunting through the
 * sidebar for which eight, and making the dependency visible is the whole
 * reason disabling refuses rather than silently repointing those chats.
 */
export async function _setMachineEnabled(machine, enabled) {
  try {
    const resp = await apiFetch(`/api/machines/${encodeURIComponent(machine.id)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      if (resp.status === 409) {
        notifyResult(data.error || 'Cannot disable this backend', 'error');
        return;
      }
      throw new Error(data.error || data.detail || `Failed (${resp.status})`);
    }
    await loadMachines(true);
    _renderMachineList();
    notifyResult(enabled ? `${machine.name} enabled` : `${machine.name} disabled`);
  } catch (error) {
    notifyResult(error.message, 'error');
  }
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
    const machine = _machines.find(m => m.id === id);
    if (machine && machine.transport_id) {
      if (data.ok || data.tunnel_up) {
        notifyResult(`SSH tunnel connected on port ${data.local_port || '?'}`);
      } else {
        notifyResult(
          `SSH tunnel: ${data.error || data.status || 'disconnected'}`,
          'error'
        );
      }
      return;
    }
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

// Provider (through_claude_code | direct) and transport (direct | ssh-proxy)
// are independent dimensions. This function shows/hides fields that only
// matter for certain provider values. Transport is always visible because
// the two dimensions combine to four real configurations.
// Exported: app.js wires this as the 'change' listener on #machineProvider.
export function _syncMachineProviderFields() {
  const provider = byId('machineProvider').value;
  const isDirect = provider === 'direct';
  byId('machineProxyFields').hidden = !isDirect;  // Host field for direct only
  byId('machineAnthropicFields').hidden = !(provider === 'claude_code' || isDirect);
  byId('machineApiKeyFields').hidden = !(provider === 'claude_code' || isDirect);
  byId('machineModel').placeholder = isDirect
    ? 'vllm/Qwen3.6-35B-A3B-NVFP4'
    : 'claude-opus-5';
}

export function _editMachine(id) {
  const m = _machines.find(x => x.id === id);
  if (!m) return;
  _setMachineEditing(id);
  byId('machineFormTitle').textContent = 'Edit machine';
  byId('machineName').value = m.name;
  byId('machineProvider').value = m.provider || 'claude_code';
  byId('machineHost').value = m.host || '';
  byId('machineBaseUrl').value = m.base_url || '';
  byId('machineModel').value = m.model;
  byId('machineApiKey').value = '';
  byId('machineApiKey').placeholder = 'Leave blank to keep current';
  _syncMachineProviderFields();
  populateTransportPicker(m.transport_id || '');
  byId('machineForm').hidden = false;
  byId('addMachineBtn').hidden = true;
  byId('machineName').focus();
}

export async function _saveMachine() {
  const name = byId('machineName').value.trim();
  const provider = byId('machineProvider').value;
  const isClaude = provider === 'claude_code';
  const isDirect = provider === 'direct';
  const host = byId('machineHost').value.trim();
  const base_url = byId('machineBaseUrl').value.trim();
  const model = (byId('machineModel').value || '').trim()
    || (isClaude ? 'claude-opus-5' : 'claude-sonnet-5');
  const api_key = byId('machineApiKey').value.trim() || null;
  const transport_id = byId('machineTransport').value || null;

  if (!name) { byId('machineName').focus(); return; }
  // Host is required for direct provider but optional for claude_code.
  // When a transport is selected the host field is irrelevant — skip validation.
  if (!isClaude && provider !== 'direct' && !host) {
    byId('machineHost').focus(); return;
  }

  const save = byId('saveMachine');
  save.disabled = true;
  try {
    let resp;
    // Only send fields the form actually collects — the server rejects the
    // whole request if the body carries any field outside its allowlist.
    const body = { name, provider, model };
    body.transport_id = transport_id;
    if (isClaude) {
      // Blank means "the default endpoint"; the server fills it in.
      if (base_url) body.base_url = base_url;
      if (api_key !== null) body.api_key = api_key;
    } else {
      // provider === 'direct' — host is required, already validated above.
      body.host = host;
      if (base_url) body.base_url = base_url;
      if (api_key !== null) body.api_key = api_key;
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
  byId('machineProvider').value = 'claude_code';
  byId('machineHost').value = '';
  byId('machineBaseUrl').value = '';
  byId('machineModel').value = '';
  byId('machineApiKey').value = '';
  byId('machineApiKey').placeholder = 'Optional';
  _syncMachineProviderFields();
  populateTransportPicker('');
  byId('machineForm').hidden = false;
  byId('addMachineBtn').hidden = true;
  byId('machineName').focus();
}