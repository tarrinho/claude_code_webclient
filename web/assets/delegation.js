// delegation.js — Settings > Delegation: the spec 2.6 benchmark matrix.
//
// Each row is one (model, task_type) pair with its five measured columns.
// The ladder shown per task type is DERIVED server-side by walking the table;
// it is never stored, so the page cannot show a ladder the generator would
// not produce.
//
// Every value below that comes from the database (model id, task_type,
// column values) is written with .textContent / .value, never innerHTML --
// same rule specs.js follows for spec titles and paths, because a model id
// or task type rendered straight into HTML is a stored-XSS hole.
import {apiFetch} from './api.js?v=2741508';
import {notifyResult} from './server-stats.js?v=1383946';

const byId = id => document.getElementById(id);

// Fallback only. `GET /api/delegation` names the true list in
// `editable_columns` (routes/delegation.py's `_EDITABLE`, which is one of
// three coordinated Python copies -- see that module's docstring); this
// array exists solely for an older cached response that predates that field,
// so the table still renders instead of coming up broken or empty. Every
// live render derives its columns from the response via `_resolveColumns`.
const _DEFAULT_COLUMNS = ['accuracy', 'n', 'cost_per_1m_tokens', 'median_latency_s', 'max_context'];

/** The columns to render for one `GET /api/delegation` response.
 *
 *  Reads `payload.editable_columns` rather than hardcoding a fourth copy of
 *  the five measured column names (spec 11 already flags three Python
 *  copies of this list, kept in sync by a test; this used to be an
 *  uncovered fourth). Falls back to `_DEFAULT_COLUMNS` when the field is
 *  missing, not an array, or empty -- an older cached response, from before
 *  this field existed, must still render the ordinary table rather than a
 *  broken or empty one. */
function _resolveColumns(payload) {
  const cols = payload && Array.isArray(payload.editable_columns)
    ? payload.editable_columns
    : null;
  return cols && cols.length ? cols : _DEFAULT_COLUMNS;
}

/** The server's reason for refusing a write, or `fallback` if it gave none.
 *
 *  This app's error contract is `{"error": "..."}` -- `app.py`'s
 *  `handle_http_exception` serialises every `HTTPException` that way, so
 *  `detail` is the FastAPI-side name and never reaches the browser. This
 *  file read `data.detail` in both of its write paths, so every refusal the
 *  server took care to explain arrived as `undefined` and was replaced by a
 *  generic fallback: a rejected cell edit said "Could not save" instead of
 *  naming the broken invariant and the column, which spec 1.1 requires it to
 *  name. `app.js` already reads `data.error`; this matches it.
 *
 *  `detail` is still read as a second choice rather than dropped: a response
 *  from a plain FastAPI error path that never reached the custom handler
 *  carries that shape, and a real reason under either key beats a fallback. */
function _errorMessage(data, fallback) {
  const body = data && typeof data === 'object' ? data : {};
  const reason = body.error || body.detail;
  return typeof reason === 'string' && reason.trim() ? reason : fallback;
}

/** Pure: the status band's plain-English first line. No DOM, so it can be
 *  read straight out of a `GET /api/delegation` response's `operational`
 *  and `ladders` fields (the latter's key count is the task-type total --
 *  same scoping the ladders/blockers fields already use, so a type with no
 *  rows yet does not inflate the count). */
function _statusHeadline(operationalCount, totalCount) {
  if (totalCount <= 0) return 'No task types are in the table yet.';
  const noun = `task type${totalCount === 1 ? '' : 's'}`;
  if (operationalCount <= 0) {
    return `Nothing is routing. 0 of ${totalCount} ${noun} operational.`;
  }
  if (operationalCount >= totalCount) {
    return `Everything is routing. ${operationalCount} of ${totalCount} ${noun} operational.`;
  }
  return `${operationalCount} of ${totalCount} ${noun} operational.`;
}

/** Pure: how many of a set of rows' editable cells actually carry a
 *  measurement, out of how many exist -- the status band's "measured cells
 *  out of total" fact. `null`/`undefined` both count as TBD, matching the
 *  rest of this file's "empty cell means unmeasured" convention. */
function _measuredCellsSummary(rows, columns) {
  const list = Array.isArray(rows) ? rows : [];
  const cols = Array.isArray(columns) ? columns : [];
  let measured = 0;
  list.forEach(row => {
    cols.forEach(column => {
      if (row[column] !== null && row[column] !== undefined) measured += 1;
    });
  });
  return {measured, total: list.length * cols.length};
}

/** Pure: the display lines for one task type's blocker entry (the
 *  `payload.blockers[taskType]` shape routes/delegation.py's
 *  `_blockers_by_task_type` returns: `{policy, data}`). The policy reason
 *  (if any) comes first, then every data-invariant problem -- an operator
 *  fixing data still needs to see a live policy hold, and vice versa.
 *  Returns `[]` for a clean or already-operational type, never a placeholder
 *  string, so the caller can render nothing instead of an empty warning. */
function _blockerLines(entry) {
  if (!entry) return [];
  const lines = [];
  if (entry.policy) lines.push(entry.policy);
  (Array.isArray(entry.data) ? entry.data : []).forEach(line => {
    if (line) lines.push(line);
  });
  return lines;
}

/** The `warnings` channel: wrong, but not blocking. Today this is a
 *  worst-case path over spec 5.1's ceiling while the enforcement knob is off.
 *  Kept separate from `_blockerLines` on purpose -- "this is why you cannot
 *  flip" and "this is over the ceiling and we are not stopping you" are
 *  different sentences, and merging them would make turning the knob off read
 *  as the breach having gone away. */
function _warningLines(entry) {
  if (!entry) return [];
  return (Array.isArray(entry.warnings) ? entry.warnings : []).filter(Boolean);
}

/** All five measured fields for one row, formatted for a hover tooltip.
 *  Spec 9.2: "Hover tooltips on rung values in the settings page show all
 *  five measured fields for the current task type ... the same five the
 *  section 3 tooltip shows, from the same row." Assigned to `.title`, a DOM
 *  property, not parsed as HTML -- the same escaping guarantee `.textContent`
 *  has. */
function _rungTooltipText(row) {
  const shown = value => (value === null || value === undefined ? 'TBD' : value);
  return [
    `accuracy: ${shown(row.accuracy)}`,
    `n: ${shown(row.n)}`,
    `cost_per_1m_tokens: ${shown(row.cost_per_1m_tokens)}`,
    `median_latency_s: ${shown(row.median_latency_s)}`,
    `max_context: ${shown(row.max_context)}`,
  ].join(' · ');
}

/** The ladder for one task type, each rung its own element carrying the
 *  five-field tooltip from the row it came from -- not a plain text run, so
 *  each rung can hold its own `title`. */
function _ladderElement(ladder, rowsForType) {
  const wrap = document.createElement('span');
  wrap.className = 'delegation-ladder';
  if (!ladder.length) {
    wrap.appendChild(document.createTextNode('no ladder yet'));
    return wrap;
  }
  ladder.forEach((model, i) => {
    if (i > 0) wrap.appendChild(document.createTextNode(' → '));
    const rung = document.createElement('span');
    rung.className = 'delegation-rung';
    rung.textContent = model;
    const row = rowsForType.find(r => r.model === model);
    rung.title = row ? _rungTooltipText(row)
      : 'no row in the table for this rung';
    wrap.appendChild(rung);
  });
  return wrap;
}

/** One editable cell: an input bound to one (model, task_type, column). */
function _cell(row, column) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'delegation-cell';
  input.value = row[column] === null || row[column] === undefined ? '' : row[column];
  input.setAttribute('aria-label', `${column} for ${row.model} on ${row.task_type}`);
  input.addEventListener('change', () => _saveRow(row, column, input));
  return input;
}

async function _saveRow(row, column, input) {
  const raw = input.value.trim();
  // An empty cell is TBD, which is NOT zero: TBD makes the row ineligible,
  // zero is a real measurement and for cost means free.
  const value = raw === '' ? null : Number(raw);
  if (raw !== '' && Number.isNaN(value)) {
    notifyResult(`${column} must be a number or empty`, 'error');
    input.value = row[column] === null ? '' : row[column];
    return;
  }
  try {
    const response = await apiFetch('/api/delegation/row', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: row.model, task_type: row.task_type, [column]: value}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(_errorMessage(data, 'Could not save'));
    }
    row[column] = value;
    loadDelegation(true);
  } catch (error) {
    notifyResult(error.message, 'error');
    input.value = row[column] === null ? '' : row[column];
  }
}

/** Flip a task type's operational flag from its card's `toggle-knob`.
 *  Reverts `aria-pressed` on a rejected write -- the same "server truth over
 *  local patching" stance specs.js takes for a rejected status change, now
 *  expressed through the knob's own state attribute rather than a
 *  checkbox's `.checked`. */
async function _setOperational(taskType, operational, knob) {
  knob.setAttribute('aria-pressed', String(operational));
  try {
    const response = await apiFetch('/api/delegation/operational', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_type: taskType, operational}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(_errorMessage(data, 'Could not change the operational flag'));
    }
    loadDelegation(true);
  } catch (error) {
    knob.setAttribute('aria-pressed', String(!operational));
    notifyResult(error.message, 'error');
  }
}

/** The standard toggle-knob (web/index.html:558, :588 -- `.app-setting-row`'s
 *  App-tab settings), built here rather than reused from markup because one
 *  is needed per card and each must carry its own task type in its click
 *  handler's closure. */
function _operationalKnob(taskType, live) {
  const knob = document.createElement('button');
  knob.type = 'button';
  knob.className = 'toggle-knob';
  knob.setAttribute('aria-pressed', String(live));
  knob.setAttribute('aria-label', `${taskType} operational`);
  const track = document.createElement('span');
  track.className = 'knob-track';
  const thumb = document.createElement('span');
  thumb.className = 'knob-thumb';
  track.appendChild(thumb);
  knob.appendChild(track);
  const label = document.createElement('span');
  label.className = 'knob-label';
  knob.appendChild(label);
  knob.addEventListener('click', () => {
    const next = knob.getAttribute('aria-pressed') !== 'true';
    _setOperational(taskType, next, knob);
  });
  return knob;
}

/** The blocker box for one card: one line per policy/data reason from
 *  `_blockerLines`, or nothing at all -- CSS hides `.delegation-blocker`
 *  when it has no children, so a clean or already-operational type shows no
 *  empty box. */
function _blockerElement(entry) {
  const wrap = document.createElement('div');
  wrap.className = 'delegation-blocker';
  _blockerLines(entry).forEach(text => {
    const p = document.createElement('p');
    p.className = 'delegation-blocker-line';
    p.textContent = text;
    wrap.appendChild(p);
  });
  _warningLines(entry).forEach(text => {
    const p = document.createElement('p');
    p.className = 'delegation-blocker-line delegation-warning-line';
    p.textContent = `not enforced: ${text}`;
    wrap.appendChild(p);
  });
  return wrap;
}

/** The model-rows table for one card: one row per (model, task_type), one
 *  editable cell per measured column. */
function _modelTable(rowsForType, columns) {
  const table = document.createElement('table');
  table.className = 'delegation-model-table';
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  const modelHead = document.createElement('th');
  modelHead.textContent = 'Model';
  headRow.appendChild(modelHead);
  columns.forEach(column => {
    const th = document.createElement('th');
    th.textContent = column;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  rowsForType.forEach(row => {
    const tr = document.createElement('tr');
    const modelCell = document.createElement('td');
    modelCell.className = 'delegation-model-name';
    modelCell.textContent = row.model;
    tr.appendChild(modelCell);
    columns.forEach(column => {
      const td = document.createElement('td');
      td.appendChild(_cell(row, column));
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

/** One card for one task type: heading + operational knob, the derived
 *  ladder (readable, each rung still carrying its tooltip), any blocker
 *  reason, and the editable model-rows table. */
function _card(taskType, rowsForType, payload) {
  const card = document.createElement('div');
  card.className = 'delegation-card';

  const head = document.createElement('div');
  head.className = 'delegation-card-head';
  const heading = document.createElement('h4');
  heading.className = 'delegation-card-title';
  heading.textContent = taskType;
  head.appendChild(heading);
  const live = (payload.operational || []).includes(taskType);
  head.appendChild(_operationalKnob(taskType, live));
  card.appendChild(head);

  const ladderLine = document.createElement('p');
  ladderLine.className = 'delegation-ladder-line';
  const ladderLabel = document.createElement('span');
  ladderLabel.className = 'delegation-ladder-label';
  ladderLabel.textContent = 'Ladder: ';
  ladderLine.appendChild(ladderLabel);
  const ladder = (payload.ladders || {})[taskType] || [];
  ladderLine.appendChild(_ladderElement(ladder, rowsForType));
  card.appendChild(ladderLine);

  const blockerEntry = (payload.blockers || {})[taskType];
  card.appendChild(_blockerElement(blockerEntry));

  card.appendChild(_modelTable(rowsForType, _resolveColumns(payload)));

  return card;
}

// ── Read-only config overview (spec 9.2's first sentence) ──────────────────
//
// The kill switch (9.1), the tunables from 5 and 10, and the cost ceiling
// (2.7). Read only, by operator ruling on this task: the matrix cells are
// the only editable part of this page. Where the server has no single
// source for a value it says so in `note` rather than a number being
// invented here.
const _CONFIG_SECTIONS = [
  ['attempts_and_caps', 'Attempts and caps'],
  ['cost_ceiling', 'Cost ceiling'],
  ['observability', 'Observability'],
];

function _configItemText(item) {
  if (item.value === null || item.value === undefined) {
    return item.note || 'not available';
  }
  return Array.isArray(item.value) ? item.value.join(', ') : String(item.value);
}

function _renderConfig(config, host) {
  if (!host) return;
  host.replaceChildren();
  if (!config) return;

  const killSwitch = document.createElement('div');
  killSwitch.className = 'delegation-config-section';
  const ksTitle = document.createElement('h4');
  ksTitle.textContent = `Kill switch (spec ${config.kill_switch.section})`;
  killSwitch.appendChild(ksTitle);
  const ksLine = document.createElement('p');
  ksLine.textContent = config.kill_switch.available ? 'Available'
    : (config.kill_switch.note || 'not available');
  killSwitch.appendChild(ksLine);
  host.appendChild(killSwitch);

  _CONFIG_SECTIONS.forEach(([key, label]) => {
    const section = config[key];
    if (!section) return;
    const box = document.createElement('div');
    box.className = 'delegation-config-section';
    const heading = document.createElement('h4');
    heading.textContent = `${label} (spec ${section.section})`;
    box.appendChild(heading);
    const list = document.createElement('dl');
    Object.keys(section).forEach(itemKey => {
      if (itemKey === 'section') return;
      const item = section[itemKey];
      const dt = document.createElement('dt');
      dt.textContent = itemKey.replace(/_/g, ' ');
      const dd = document.createElement('dd');
      dd.textContent = _configItemText(item);
      if (item.source) dd.title = `source: ${item.source}`;
      list.appendChild(dt);
      list.appendChild(dd);
    });
    box.appendChild(list);
    host.appendChild(box);
  });
}

/** The status band: the plain-English headline plus the compact facts row
 *  (measured coverage, latency ceiling, tree budget). Reads the same
 *  `config` block `_renderConfig` shows in full further down the page --
 *  this is the condensed version an operator checks first. */
/** Turn spec 5.1's combined latency ceiling into a blocking invariant, or
 *  back off. Global rather than per-card: the ceiling is one number for every
 *  task type, so a per-card knob would imply nine independent settings.
 *
 *  Same optimistic-then-revert shape as `_setOperational`: the knob shows the
 *  requested state immediately and snaps back if the server refuses, which it
 *  will when enabling enforcement would invalidate an already-operational
 *  type. */
async function _setCeilingEnforcement(enabled, knob) {
  knob.setAttribute('aria-pressed', String(enabled));
  try {
    const res = await fetch('/api/delegation/ceiling-enforcement', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      knob.setAttribute('aria-pressed', String(!enabled));
      showToast(_errorMessage(
        data, 'Could not change latency ceiling enforcement'), 'error');
      return;
    }
    await _refreshDelegation();
  } catch (err) {
    knob.setAttribute('aria-pressed', String(!enabled));
    showToast('Could not change latency ceiling enforcement', 'error');
  }
}

/** The status band's enforcement knob, built from the same `toggle-knob`
 *  component the per-card operational knobs use. */
function _ceilingKnob(state) {
  const wrap = document.createElement('div');
  wrap.className = 'delegation-ceiling-knob';
  const label = document.createElement('span');
  label.className = 'delegation-fact';
  // Carries the ceiling's VALUE as well as its state, because this replaces
  // the plain `latency ceiling: 1500s` fact rather than sitting beside it --
  // two facts both opening with "latency ceiling" read as two unrelated
  // settings, and the knob looked like it governed something else.
  const value = state.ceiling_s === null || state.ceiling_s === undefined
    ? 'latency ceiling' : `latency ceiling: ${state.ceiling_s}s`;
  label.textContent = state.enabled
    ? `${value} — enforced`
    : `${value} — reported, not enforced`;
  const knob = document.createElement('button');
  knob.type = 'button';
  knob.className = 'toggle-knob';
  knob.setAttribute('aria-pressed', String(Boolean(state.enabled)));
  knob.setAttribute('aria-label', 'enforce the combined latency ceiling');
  knob.title = state.enabled
    ? 'A task type whose worst-case path exceeds the ceiling cannot go '
      + 'operational, and a leaf is stopped before a stage that would exceed it.'
    : 'Breaches are computed and shown, but do not block. Off by default: the '
      + 'ceiling is derived from the most expensive operational task type and '
      + 'nothing is operational yet.';
  const track = document.createElement('span');
  track.className = 'knob-track';
  const thumb = document.createElement('span');
  thumb.className = 'knob-thumb';
  track.appendChild(thumb);
  knob.appendChild(track);
  knob.addEventListener('click', () => {
    _setCeilingEnforcement(
      knob.getAttribute('aria-pressed') !== 'true', knob);
  });
  wrap.appendChild(label);
  wrap.appendChild(knob);
  return wrap;
}

function _renderStatus(payload) {
  const line = byId('delegationStatusLine');
  const facts = byId('delegationFacts');
  const totalTypes = Object.keys(payload.ladders || {}).length;
  const opCount = (payload.operational || []).length;
  if (line) line.textContent = _statusHeadline(opCount, totalTypes);
  if (!facts) return;
  facts.replaceChildren();

  const columns = _resolveColumns(payload);
  const {measured, total} = _measuredCellsSummary(payload.rows || [], columns);
  const cfg = payload.config || {};
  const ceiling = cfg.attempts_and_caps && cfg.attempts_and_caps.combined_latency_ceiling_s
    ? cfg.attempts_and_caps.combined_latency_ceiling_s.value : null;
  const budget = cfg.cost_ceiling && cfg.cost_ceiling.budget_usd_per_tree
    ? cfg.cost_ceiling.budget_usd_per_tree.value : null;

  // The ceiling fact is rendered by `_ceilingKnob` when the server sent the
  // enforcement state, so it is omitted here rather than printed twice.
  const items = [`${measured} of ${total} cells measured`];
  if (!payload.ceiling_enforcement) {
    items.push(ceiling === null || ceiling === undefined
      ? 'latency ceiling: not available' : `latency ceiling: ${ceiling}s`);
  }
  items.push(budget === null || budget === undefined
    ? 'tree budget: not available' : `tree budget: $${budget}`);
  items.forEach(text => {
    const span = document.createElement('span');
    span.className = 'delegation-fact';
    span.textContent = text;
    facts.appendChild(span);
  });
  // Rendered only when the server sent the field, so an older payload shows
  // the band unchanged rather than a knob defaulting to a state it never
  // reported.
  if (payload.ceiling_enforcement) {
    facts.appendChild(_ceilingKnob(payload.ceiling_enforcement));
  }
}

/** "⟳ Refresh" row: same disable/status-text/timeout shape as Backends'
 *  `_refreshBackends` (app.js), against this panel's own button and status
 *  ids rather than a shared helper -- the two panels reload different data
 *  through different functions. */
async function _refreshDelegation() {
  const btn = byId('delegationRefreshBtn');
  const status = byId('delegationRefreshStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Refreshing…';
  try {
    await loadDelegation(true);
    if (status) status.textContent = 'Refreshed';
  } catch (error) {
    if (status) status.textContent = 'Refresh failed';
  } finally {
    if (btn) btn.disabled = false;
    setTimeout(() => { if (status) status.textContent = ''; }, 3000);
  }
}
byId('delegationRefreshBtn')?.addEventListener('click', _refreshDelegation);

export async function loadDelegation(force = false) {
  const host = byId('delegationMatrix');
  if (!host) return null;
  if (!force && host.children.length) return null;

  let payload;
  try {
    const response = await apiFetch('/api/delegation');
    if (!response.ok) throw new Error('Could not load the delegation table');
    payload = await response.json();
  } catch (error) {
    notifyResult(error.message, 'error');
    return null;
  }

  _renderStatus(payload);
  _renderConfig(payload.config, byId('delegationConfig'));

  host.replaceChildren();
  const byType = {};
  (payload.rows || []).forEach(row => {
    (byType[row.task_type] = byType[row.task_type] || []).push(row);
  });

  Object.keys(byType).sort().forEach(taskType => {
    host.appendChild(_card(taskType, byType[taskType], payload));
  });

  return (payload.rows || []).length;
}
