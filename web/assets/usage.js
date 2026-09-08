import {apiFetch} from './api.js?v=1';
import {formatTime, formatAbsoluteTime} from './app.js?v=44';

// This file is loaded as its own <script type="module"> in index.html and
// does not share app.js's own `const byId` (ES modules do not share top-level
// scope across files), so it needs its own -- same pattern as
// orchestrator.js/device-alerts.js/server-stats.js/skills.js.
const byId = id => document.getElementById(id);
let _usageData = null;          // last GET /api/usage payload
let _usageFetchedFor = null;    // range the payload was fetched for

/** Compact a token count, keeping the exact value for the title attribute. */
function _abbrev(n) {
  const value = Number(n) || 0;
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)} B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)} K`;
  return String(value);
}

function _cell(text, className, title) {
  const cell = document.createElement('span');
  cell.className = className;
  cell.textContent = text;
  if (title) cell.title = title;
  return cell;
}

function _buildOriginBreakdown(data) {
  const rows = data.by_origin || [];
  if (!rows.length) return null;
  const section = document.createElement('section');
  section.className = 'usage-origin';
  const heading = document.createElement('h4');
  heading.textContent = 'Where these turns came from';
  section.appendChild(heading);

  const LABELS = {
    web: 'This website',
    // Asked here, executed there. Neither plain label is true, so it gets its
    // own: this is the case where a request on a conversation with a live
    // terminal is typed into that terminal instead of run by the server.
    'web-routed': 'Asked here, ran in its terminal',
    terminal: 'Terminal sessions (including agents)',
  };
  rows.forEach(row => {
    const total = (row.input_tokens || 0) + (row.output_tokens || 0);
    const unsplit = row.unsplit_tokens || 0;
    // The comparable number is what is left once re-counted context is removed.
    const comparable = Math.max(0, total - unsplit);

    const item = document.createElement('div');
    item.className = 'usage-origin-row';
    const name = document.createElement('span');
    name.className = 'usage-origin-name';
    name.textContent = LABELS[row.origin] || row.origin;
    const figure = document.createElement('span');
    figure.className = 'usage-origin-figure';
    figure.textContent = `${row.requests} req · ${_abbrev(comparable)} tokens`;
    figure.title = `${comparable.toLocaleString()} tokens`;
    item.append(name, figure);
    section.appendChild(item);

    if (unsplit) {
      const note = document.createElement('p');
      note.className = 'usage-origin-note';
      note.textContent =
        `Plus ${_abbrev(unsplit)} tokens not counted above. ` +
        (row.unsplit_note || '');
      note.title = `${unsplit.toLocaleString()} tokens excluded`;
      section.appendChild(note);
    }
  });
  return section;
}

function _buildSessionBreakdown(data) {
  const rows = data.by_session || [];
  if (!rows.length) return null;
  const section = document.createElement('section');
  section.className = 'usage-origin';
  const heading = document.createElement('h4');
  heading.textContent = 'Terminal usage by session';
  section.appendChild(heading);
  const note = document.createElement('p');
  note.className = 'usage-origin-note';
  note.textContent =
    'Named so a surprising total is explainable. An agent session working on ' +
    'your behalf can spend orders of magnitude more than anything typed by hand.';
  section.appendChild(note);

  rows.forEach(row => {
    const item = document.createElement('div');
    item.className = 'usage-origin-row';
    const name = document.createElement('span');
    name.className = 'usage-origin-name';
    // textContent: a conversation title is user-supplied.
    name.textContent = row.title || `session ${String(row.session_id).slice(0, 8)}`;
    name.title = row.session_id || '';
    const figure = document.createElement('span');
    figure.className = 'usage-origin-figure';
    const total = (row.input_tokens || 0) + (row.output_tokens || 0);
    figure.textContent = `${row.requests} req · ${_abbrev(total)}`;
    figure.title = `${total.toLocaleString()} tokens`;
    if (row.context_unsplit) {
      const flag = document.createElement('span');
      flag.className = 'usage-unsplit-flag';
      flag.textContent = 'context not split';
      flag.title =
        'This model reports no cache breakdown, so each turn counts the whole ' +
        'conversation again rather than new tokens.';
      figure.appendChild(flag);
    }
    item.append(name, figure);
    section.appendChild(item);
  });
  return section;
}

export function _renderUsage() {
  const body = byId('usageBody');
  if (!body || !_usageData) return;
  const totals = _usageData.totals || [];
  const recent = _usageData.recent || [];
  const overall = _usageData.overall || {};

  const count = byId('usageCount');
  count.textContent = overall.requests
    ? `${overall.requests} requests · ${_abbrev(overall.input_tokens)} in · ${_abbrev(overall.output_tokens)} out`
    : 'No requests yet';

  if (!totals.length) {
    // An empty range is not the same as zero usage; say which it is.
    const notice = document.createElement('div');
    notice.className = 'skills-notice';
    notice.textContent = _usageData.days
      ? `No turns recorded in the last ${_usageData.days} days.`
      : 'No turns recorded yet. Usage is collected from now on.';
    body.replaceChildren(notice);
    return;
  }

  // Where the turns came from, and which session spent it. Without this the
  // page reported one figure dominated by adopted agent sessions and presented
  // it as the operator's own usage: a day spent working from a phone showed
  // hundreds of millions of "terminal" tokens that belonged to the agents.
  const originBlock = _buildOriginBreakdown(_usageData);
  const sessionBlock = _buildSessionBreakdown(_usageData);

  const frag = document.createDocumentFragment();

  const table = document.createElement('div');
  table.className = 'usage-table';
  const head = document.createElement('div');
  head.className = 'usage-row usage-head';
  head.append(
    _cell('Model', 'usage-model'), _cell('Reqs', 'usage-num'),
    _cell('Input', 'usage-num'), _cell('Output', 'usage-num'),
    _cell('Cost', 'usage-num'),
  );
  table.appendChild(head);

  totals.forEach(row => {
    const line = document.createElement('div');
    line.className = 'usage-row';
    // The badge is a sibling of the ellipsised name, not a child: nested in the
    // clipped element it disappeared for any model with a long id.
    const name = document.createElement('span');
    name.className = 'usage-model';
    name.appendChild(_cell(row.model, 'usage-name', row.model));
    if (row.errors) {
      name.appendChild(_cell(`${row.errors} failed`, 'usage-errors'));
    }
    // The dash carries its own explanation, preferring Claude Code's own
    // verdict on the cost basis over anything we infer from the base URL.
    const cost = row.cost_usd === null || row.cost_usd === undefined
      ? _cell('—', 'usage-num usage-muted',
              row.cost_note || 'Not available for this backend.')
      : _cell(`$${Number(row.cost_usd).toFixed(2)}`, 'usage-num',
              row.cost_basis_unknown
                ? 'Claude Code reported the cost basis as unknown.'
                : undefined);
    line.append(
      name,
      _cell(String(row.requests), 'usage-num'),
      _cell(_abbrev(row.input_tokens), 'usage-num', `${row.input_tokens} tokens`),
      _cell(_abbrev(row.output_tokens), 'usage-num', `${row.output_tokens} tokens`),
      cost,
    );
    table.appendChild(line);
  });
  frag.appendChild(table);

  if (recent.length) {
    const heading = document.createElement('div');
    heading.className = 'chat-section-label';
    heading.textContent = `Recent turns · ${recent.length}`;
    frag.appendChild(heading);

    const list = document.createElement('ul');
    list.className = 'usage-recent';
    recent.forEach(turn => {
      const item = document.createElement('li');
      item.className = turn.is_error ? 'usage-turn usage-turn-error' : 'usage-turn';
      item.append(
        _cell(formatTime(turn.created_at), 'usage-when',
              formatAbsoluteTime(turn.created_at)),
        _cell(turn.chat_title || 'deleted conversation', 'usage-chat',
              turn.chat_title || 'The conversation has since been deleted.'),
        _cell(turn.model, 'usage-turn-model', turn.model),
        _cell(`${_abbrev(turn.input_tokens)} → ${_abbrev(turn.output_tokens)}`,
              'usage-num', `${turn.input_tokens} in, ${turn.output_tokens} out`),
      );
      if (turn.is_error) item.appendChild(_cell('failed', 'usage-errors'));
      list.appendChild(item);
    });
    frag.appendChild(list);
  }

  const foot = document.createElement('p');
  foot.className = 'skills-session';
  foot.textContent = _usageData.retention_days
    ? `Kept for ${_usageData.retention_days} days.`
    : 'Kept indefinitely.';
  frag.appendChild(foot);

  // Ahead of the per-model table: "who spent this" is the question a
  // surprising total raises first, and the model breakdown cannot answer it.
  if (originBlock) frag.insertBefore(originBlock, frag.firstChild);
  if (sessionBlock) frag.appendChild(sessionBlock);

  body.replaceChildren(frag);
}

export async function loadUsage(force = false) {
  const body = byId('usageBody');
  if (!body) return;
  const range = byId('usageRange')?.value || '30';
  if (!force && _usageData && _usageFetchedFor === range) {
    _renderUsage();
    return;
  }
  const rows = Array.from({length: 4}, () => {
    const row = document.createElement('div');
    row.className = 'skill-skeleton';
    return row;
  });
  body.replaceChildren(...rows);
  byId('usageCount').textContent = 'Loading…';
  try {
    const resp = await apiFetch(`/api/usage?days=${encodeURIComponent(range)}`);
    if (!resp.ok) throw new Error('Could not load usage');
    _usageData = await resp.json();
    _usageFetchedFor = range;
    _renderUsage();
  } catch (error) {
    _usageData = null;
    _usageFetchedFor = null;
    byId('usageCount').textContent = '';
    const notice = document.createElement('div');
    notice.className = 'skills-notice';
    notice.textContent = error.message;
    body.replaceChildren(notice);
  }
}