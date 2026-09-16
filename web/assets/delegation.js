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

const COLUMNS = ['accuracy', 'n', 'cost_per_1m_tokens', 'median_latency_s', 'max_context'];

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
    summary.textContent = `${taskType} · ${live ? 'operational' : 'not operational'}`
      + (ladder.length ? ` · ladder: ${ladder.join(' → ')}` : ' · no ladder');
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
      COLUMNS.forEach(column => line.appendChild(_cell(row, column)));
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
