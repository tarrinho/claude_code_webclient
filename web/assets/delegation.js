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
    wrap.appendChild(document.createTextNode('no ladder'));
    return wrap;
  }
  wrap.appendChild(document.createTextNode('ladder: '));
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
      throw new Error(data.detail || 'Could not save');
    }
    row[column] = value;
    loadDelegation(true);
  } catch (error) {
    notifyResult(error.message, 'error');
    input.value = row[column] === null ? '' : row[column];
  }
}

/** Flip a task type's operational flag. Reverts the checkbox on a rejected
 *  write -- the same "server truth over local patching" stance specs.js
 *  takes for a rejected status change. */
async function _setOperational(taskType, operational, checkbox) {
  try {
    const response = await apiFetch('/api/delegation/operational', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_type: taskType, operational}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Could not change the operational flag');
    }
    loadDelegation(true);
  } catch (error) {
    checkbox.checked = !operational;
    notifyResult(error.message, 'error');
  }
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

  _renderConfig(payload.config, byId('delegationConfig'));

  const columns = _resolveColumns(payload);

  host.replaceChildren();
  const byType = {};
  (payload.rows || []).forEach(row => {
    (byType[row.task_type] = byType[row.task_type] || []).push(row);
  });

  Object.keys(byType).sort().forEach(taskType => {
    const group = document.createElement('details');
    group.className = 'delegation-group';
    const summary = document.createElement('summary');
    const ladder = (payload.ladders || {})[taskType] || [];
    const live = (payload.operational || []).includes(taskType);
    summary.appendChild(document.createTextNode(
      `${taskType} · ${live ? 'operational' : 'not operational'} · `));
    summary.appendChild(_ladderElement(ladder, byType[taskType]));
    group.appendChild(summary);

    const opRow = document.createElement('label');
    opRow.className = 'delegation-operational';
    const opBox = document.createElement('input');
    opBox.type = 'checkbox';
    opBox.checked = live;
    opBox.setAttribute('aria-label', `${taskType} operational`);
    opBox.addEventListener('change', () => _setOperational(taskType, opBox.checked, opBox));
    opRow.appendChild(opBox);
    opRow.appendChild(document.createTextNode(' operational'));
    group.appendChild(opRow);

    byType[taskType].forEach(row => {
      const line = document.createElement('div');
      line.className = 'delegation-row';
      const name = document.createElement('span');
      name.className = 'delegation-model';
      name.textContent = row.model;
      line.appendChild(name);
      columns.forEach(column => line.appendChild(_cell(row, column)));
      group.appendChild(line);
    });
    host.appendChild(group);
  });

  const count = byId('delegationCount');
  if (count) {
    const total = (payload.rows || []).length;
    count.textContent = `${total} row${total === 1 ? '' : 's'} · `
      + `${(payload.operational || []).length} operational`;
  }
  return (payload.rows || []).length;
}
