import {apiFetch, downloadMarkdown} from './api.js';
import {createChatListController} from './chat-list.js';
import {createConversationController, parseTimestamp} from './conversation.js';

const state = {
  chats: [],
  currentChat: null,
  streamState: 'ready',
};

const byId = id => document.getElementById(id);
const storageGet = key => { try { return localStorage.getItem(key); } catch { return null; } };
const storageSet = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
const storageRemove = key => { try { localStorage.removeItem(key); } catch {} };
let previousFocus = null;
let dialogMode = 'create';
let dialogChat = null;
let listController;
let conversationController;
let settingsVisible = false;
let _activeMachineId = null;
let _machines = [];
let _machineEditing = null;
let _currentTab = 'backends';
let _modelOptions = [];
// Last GET /api/models payload: what the active machine reports it serves.
let _servedModels = [];
let _modelsSource = null;
// Last payload from GET /api/settings. Save compares against it so a field
// cleared to "" is recognised as a change and actually sent.
let _loadedSettings = {};
let _searchDebounce = null;
let _usageData = null;          // last GET /api/usage payload
let _usageFetchedFor = null;    // range the payload was fetched for
let _skillsData = null;          // last successful /api/skills payload
let _skillsFetchedFor = null;    // chat id the payload was fetched for
let _skillFilter = '';
let _skillDebounce = null;
const _collapsedSkillGroups = new Set();

function showToast(message, type = '') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  byId('toastRegion').appendChild(toast);
  setTimeout(() => toast.remove(), 4500);
}

function formatTime(iso) {
  const date = parseTimestamp(iso);
  if (!date) return '';
  const diff = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return date.toLocaleDateString();
}

function formatAbsoluteTime(iso) {
  return parseTimestamp(iso)?.toLocaleString() || '';
}

function applySavedTheme() {
  const theme = storageGet('wc_theme') || 'dark';
  document.documentElement.dataset.theme = theme;
  byId('themeToggle').setAttribute('aria-pressed', String(theme === 'light'));
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  byId('themeToggle').setAttribute('aria-pressed', String(next === 'light'));
  storageSet('wc_theme', next);
}

function openSidebar() {
  previousFocus = document.activeElement;
  byId('sidebar').inert = false;
  byId('sidebar').classList.add('open');
  byId('sidebar').setAttribute('aria-hidden', 'false');
  byId('menuBtn').setAttribute('aria-expanded', 'true');
  byId('sidebarOverlay').style.display = 'block';
  byId('chatSearch').focus();
}

function closeSidebar() {
  if (!byId('sidebar').classList.contains('open')) return;
  byId('sidebar').classList.remove('open');
  byId('sidebar').setAttribute('aria-hidden', 'true');
  byId('sidebar').inert = true;
  byId('menuBtn').setAttribute('aria-expanded', 'false');
  byId('sidebarOverlay').style.display = 'none';
  if (previousFocus && document.body.contains(previousFocus)) previousFocus.focus();
}

function focusableIn(container) {
  return [...container.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')];
}

function trapDialogFocus(event) {
  if (event.key !== 'Tab') return;
  const dialog = byId('chatDialog');
  if (!dialog.classList.contains('open')) return;
  const focusable = focusableIn(dialog);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function openChatDialog(mode, chat = state.currentChat) {
  dialogMode = mode;
  dialogChat = chat;
  previousFocus = document.activeElement;
  const editing = mode === 'edit';
  const deleting = mode === 'delete';
  byId('dialogTitle').textContent = deleting ? 'Delete conversation' : editing ? 'Edit conversation' : 'New conversation';
  byId('dialogHelp').textContent = deleting
    ? `Delete “${chat.title}”? Its workspace files will be kept.`
    : editing ? 'Update how this workspace appears in the list.' : 'Name the workspace so it is easy to find later.';
  byId('chatFields').hidden = deleting;
  byId('chatTitleInput').required = !deleting;
  byId('chatTitleInput').value = editing && chat ? chat.title : '';
  byId('chatDescriptionInput').value = editing && chat ? (chat.description || '') : '';
  const save = byId('dialogSave');
  save.textContent = deleting ? 'Delete conversation' : editing ? 'Save changes' : 'Create conversation';
  save.classList.toggle('btn-danger', deleting);
  byId('chatDialog').classList.add('open');
  setTimeout(() => (deleting ? save : byId('chatTitleInput')).focus(), 0);
}

function closeDialog() {
  const dialog = byId('chatDialog');
  if (!dialog.classList.contains('open')) return;
  dialog.classList.remove('open');
  dialogChat = null;
  if (previousFocus && document.body.contains(previousFocus)) previousFocus.focus();
}

function openSettingsDialog() {
  previousFocus = document.activeElement;
  byId('settingsStatus').textContent = '';
  byId('settingsDialog').classList.add('open');
  settingsVisible = true;
  _machineEditing = null;
  byId('machineForm').hidden = true;
  _switchTab('backends');
}

function closeSettingsDialog() {
  byId('settingsDialog').classList.remove('open');
  settingsVisible = false;
  _machineEditing = null;
  if (previousFocus && document.body.contains(previousFocus)) previousFocus.focus();
}

function _switchTab(tab) {
  _currentTab = tab;
  document.querySelectorAll('.settings-tab').forEach(t => {
    const active = t.dataset.tab === tab;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
    t.tabIndex = active ? 0 : -1;
  });
  const map = { backends: 'panelBackends', usage: 'panelUsage', skills: 'panelSkills', app: 'panelApp' };
  const activeId = map[tab] || 'panelBackends';
  ['panelBackends', 'panelUsage', 'panelSkills', 'panelApp'].forEach(id => {
    const el = byId(id);
    if (el) el.hidden = id !== activeId;
  });
  // The footer Save button only writes the Models and App fields. Machines save
  // through their own form and Skills is read-only, so showing it there offered
  // a control that silently did nothing.
  const save = byId('settingsSave');
  // Only the App tab has fields the footer Save writes. Backends save through
  // their own controls, Skills and Usage are read-only.
  if (save) save.hidden = tab !== 'app';
  if (tab === 'backends') loadBackends();
  // Always refetch: usage is checked right after running turns, so a cached
  // payload from earlier in the session would show stale numbers.
  if (tab === 'usage') loadUsage(true);
  if (tab === 'skills') loadSkills();
}

// ── Usage ─────────────────────────────────────────────────────────────────────────

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

function _renderUsage() {
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

  body.replaceChildren(frag);
}

async function loadUsage(force = false) {
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

// Report the outcome of an action inside whichever surface the user is looking
// at. A toast renders in .toast-region (z-index 300) while the settings dialog
// is .dialog-backdrop (z-index 400), so a toast raised from Settings is painted
// underneath the modal overlay -- the result appeared to land on the page
// behind. #settingsStatus is the dialog's own aria-live region, so it is both
// visible and announced.
function notifyResult(message, type = '') {
  if (settingsVisible) setStatus(message, type === 'error' ? 'error' : 'success');
  else showToast(message, type);
}

function setStatus(text, type) {
  const el = byId('settingsStatus');
  el.textContent = text;
  el.className = type ? `toast ${type}` : '';
  if (type === 'success') setTimeout(() => { el.textContent = ''; el.className = ''; }, 2000);
}

// ── Skills ────────────────────────────────────────────────────────────────────────

function _skillsNotice(text) {
  const notice = document.createElement('div');
  notice.className = 'skills-notice';
  notice.textContent = text;
  return notice;
}

/** Placeholder rows so the panel does not flash empty while fetching. */
function _renderSkillSkeleton() {
  const list = byId('skillsList');
  if (!list) return;
  const rows = Array.from({length: 5}, () => {
    const row = document.createElement('div');
    row.className = 'skill-skeleton';
    return row;
  });
  list.replaceChildren(...rows);
  byId('skillsCount').textContent = 'Loading…';
}

function _skillMatches(skill, needle) {
  if (!needle) return true;
  return skill.name.toLowerCase().includes(needle)
    || (skill.description || '').toLowerCase().includes(needle);
}

/** One collapsed/expandable card. Long descriptions stay behind a disclosure. */
function _buildSkillCard(skill, isPlugin) {
  const item = document.createElement('li');
  item.className = skill.active ? 'skill-card skill-card-active' : 'skill-card';

  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.className = 'skill-summary-row';

  const heading = document.createElement('span');
  heading.className = 'skill-name';
  // The group header already names the plugin, so drop the redundant prefix.
  heading.textContent = isPlugin ? skill.name.split(':').slice(1).join(':') : skill.name;
  summary.appendChild(heading);

  if (skill.active) {
    const badge = document.createElement('span');
    badge.className = 'skill-badge skill-active';
    badge.textContent = 'Active';
    summary.appendChild(badge);
  }

  const line = document.createElement('span');
  line.className = 'skill-summary';
  line.textContent = skill.summary || 'No description provided.';
  summary.appendChild(line);

  const full = document.createElement('p');
  full.className = 'skill-description';
  full.textContent = skill.description || 'No description provided.';

  details.append(summary, full);
  item.appendChild(details);
  return item;
}

function _renderSkills() {
  const list = byId('skillsList');
  if (!list || !_skillsData) return;
  const needle = _skillFilter.trim().toLowerCase();
  const all = _skillsData.skills || [];
  const sources = _skillsData.sources || [];
  const shown = all.filter(skill => _skillMatches(skill, needle));

  // Count line: absolute totals when browsing, match count when filtering.
  const count = byId('skillsCount');
  if (needle) {
    count.textContent = `${shown.length} of ${all.length} skills`;
  } else {
    const active = _skillsData.active_count || 0;
    count.textContent = active
      ? `${all.length} skills · ${active} active`
      : `${all.length} skills`;
  }

  // Which conversation the "Active" badges refer to.
  const session = byId('skillsSession');
  if (_skillsData.session_id) {
    const title = state.currentChat?.title;
    session.textContent = title
      ? `Activity shown for “${title}”.`
      : 'Activity shown for the current conversation.';
    session.hidden = false;
  } else {
    session.textContent = 'Open a conversation to see which skills it has used.';
    session.hidden = false;
  }

  if (!all.length) {
    list.replaceChildren(_skillsNotice('No skills found. Add one under ~/.claude/skills.'));
    return;
  }
  if (!shown.length) {
    list.replaceChildren(_skillsNotice(`No skills match “${_skillFilter.trim()}”.`));
    return;
  }

  const groups = sources.map(source => {
    const items = shown.filter(skill => skill.source === source.id);
    if (!items.length) return null;

    const section = document.createElement('section');
    section.className = 'skill-group';

    // Filtering always expands, so matches are never hidden behind a collapse.
    const collapsed = !needle && _collapsedSkillGroups.has(source.id);
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'skill-group-head';
    head.setAttribute('aria-expanded', String(!collapsed));
    head.addEventListener('click', () => {
      if (_collapsedSkillGroups.has(source.id)) _collapsedSkillGroups.delete(source.id);
      else _collapsedSkillGroups.add(source.id);
      _renderSkills();
    });

    const caret = document.createElement('span');
    caret.className = 'skill-group-caret';
    caret.setAttribute('aria-hidden', 'true');
    caret.textContent = '▸';
    const label = document.createElement('span');
    label.className = 'skill-group-label';
    label.textContent = source.label;
    const tally = document.createElement('span');
    tally.className = 'skill-group-count';
    tally.textContent = needle ? `${items.length} of ${source.count}` : String(source.count);
    head.append(caret, label, tally);
    section.appendChild(head);

    if (!collapsed) {
      const items_el = document.createElement('ul');
      items_el.className = 'skill-group-items';
      const isPlugin = source.id.startsWith('plugin:');
      // Active skills first, so session activity is visible without scrolling.
      const ordered = [...items].sort((a, b) => Number(b.active) - Number(a.active));
      ordered.forEach(skill => items_el.appendChild(_buildSkillCard(skill, isPlugin)));
      section.appendChild(items_el);
    }
    return section;
  }).filter(Boolean);

  list.replaceChildren(...groups);
}

async function loadSkills(force = false) {
  const list = byId('skillsList');
  if (!list) return;
  const chatId = state.currentChat?.id || '';
  // Reuse the payload unless the conversation changed -- activity is per session.
  if (!force && _skillsData && _skillsFetchedFor === chatId) {
    _renderSkills();
    return;
  }
  _renderSkillSkeleton();
  try {
    const query = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : '';
    const response = await apiFetch(`/api/skills${query}`);
    if (!response.ok) throw new Error('Could not load skills');
    _skillsData = await response.json();
    _skillsFetchedFor = chatId;
    _renderSkills();
  } catch (error) {
    _skillsData = null;
    _skillsFetchedFor = null;
    byId('skillsCount').textContent = '';
    byId('skillsSession').hidden = true;
    list.replaceChildren(_skillsNotice(error.message));
  }
}

// ── Machines ──────────────────────────────────────────────────────────────────────

async function loadMachines() {
  try {
    const resp = await apiFetch('/api/machines');
    if (resp.ok) _machines = (await resp.json()).machines || [];
  } catch { _machines = []; }
  // Check for a stored active machine
  const stored = storageGet('wc_active_machine');
  if (stored && _machines.some(m => m.id === stored)) {
    _activeMachineId = stored;
  }
}

// 'anthropic' is the wire protocol, not the vendor: a gateway speaking the
// Anthropic API at a custom base_url is still provider='anthropic'. Say which
// it actually is, since that is what decides how a backend behaves.
function _providerLabel(machine) {
  if (machine.provider !== 'anthropic') return 'Claude Code proxy';
  const url = (machine.base_url || '').trim();
  if (!url || url.includes('api.anthropic.com')) return 'Anthropic API';
  return 'Anthropic-compatible';
}

// Per-machine model state, keyed by machine id: {models, active, default,
// source, reason, endpoint}. Fetched lazily so opening Settings does not
// query every configured backend at once.
const _modelsByMachine = new Map();

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
  } else {
    // Never present a guess as the real list -- say why it is a guess.
    status.classList.add('models-status-warn');
    status.textContent = entry.reason
      ? `${entry.reason} Showing built-in suggestions.`
      : 'Showing built-in suggestions.';
  }
  toolbar.appendChild(status);

  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'machine-action';
  refresh.textContent = 'Refresh';
  refresh.addEventListener('click', () => loadModelsFor(machine.id, true));
  toolbar.appendChild(refresh);
  section.appendChild(toolbar);

  if (!entry) return section;

  const list = document.createElement('div');
  list.className = 'models-list';
  // An empty active list means every served model is offered. Rendering that
  // as all-checked keeps the feature opt-in; rendering it as none-checked
  // would imply the picker is empty, which it is not.
  const offersAll = entry.active.length === 0;
  entry.models.forEach(model => {
    list.appendChild(_buildModelRow(machine, model, entry, offersAll));
  });
  section.appendChild(list);

  const hint = document.createElement('p');
  hint.className = 'machine-hint';
  hint.textContent = offersAll
    ? 'All models are offered. Tick a subset to narrow the picker.'
    : 'Ticked models are offered when starting a turn; ● is the default for new chats.';
  section.appendChild(hint);
  return section;
}

function _buildModelRow(machine, model, entry, offersAll) {
  const row = document.createElement('div');
  row.className = 'model-item';

  const offered = document.createElement('input');
  offered.type = 'checkbox';
  offered.checked = offersAll || entry.active.includes(model.id);
  offered.setAttribute('aria-label', `Offer ${model.id}`);
  offered.addEventListener('change', () => _toggleModelOffered(machine, model.id));
  row.appendChild(offered);

  const isDefault = document.createElement('input');
  isDefault.type = 'radio';
  isDefault.name = `default-model-${machine.id}`;
  isDefault.checked = entry.default === model.id;
  isDefault.setAttribute('aria-label', `Default to ${model.id}`);
  isDefault.addEventListener('change', () => _setModelDefault(machine, model.id));
  row.appendChild(isDefault);

  const name = document.createElement('span');
  name.className = 'model-item-id';
  name.textContent = model.id;
  name.title = model.id;
  row.appendChild(name);

  if (model.display_name && model.display_name !== model.id) {
    const display = document.createElement('span');
    display.className = 'model-item-name';
    display.textContent = model.display_name;
    row.appendChild(display);
  }
  return row;
}

function _renderMachineList() {
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

    const name = document.createElement('span');
    name.className = 'machine-name';
    name.textContent = m.name;
    if (m.active) {
      const badge = document.createElement('span');
      badge.className = 'machine-badge';
      badge.textContent = 'Active';
      top.appendChild(badge);
    }
    top.appendChild(name);

    const meta = document.createElement('div');
    meta.className = 'machine-meta';
    // Anthropic machines are identified by their endpoint; host/port only
    // describe the transport and would read as noise on the card.
    const where = m.provider === 'anthropic'
      ? (m.base_url || 'https://api.anthropic.com')
      : m.host;
    meta.textContent = `${where}${m.model ? ' · ' + m.model : ''}`;
    top.appendChild(meta);

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
}

async function _activateMachine(id) {
  try {
    const resp = await apiFetch(`/api/machines/${encodeURIComponent(id)}/activate`, {method: 'POST'});
    if (!resp.ok) throw new Error('Could not activate machine');
    await loadMachines();
    _activeMachineId = id;
    storageSet('wc_active_machine', id);
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
    if (_activeMachineId === id) _activeMachineId = null;
    await loadMachines();
    _renderMachineList();
    notifyResult('Machine deleted');
  } catch (error) {
    notifyResult(error.message, 'error');
  }
}

// Anthropic machines are configured by endpoint, proxy machines by host, so
// only one of the two field groups is ever relevant.
function _syncMachineProviderFields() {
  const provider = byId('machineProvider').value;
  const isAnthropic = provider === 'anthropic';
  byId('machineProxyFields').hidden = isAnthropic;
  byId('machineAnthropicFields').hidden = !isAnthropic;
  byId('machineApiKeyHint').hidden = !isAnthropic;
  byId('machineModel').placeholder = isAnthropic ? 'claude-opus-5' : 'claude-sonnet-5';
}

function _editMachine(id) {
  const m = _machines.find(x => x.id === id);
  if (!m) return;
  _machineEditing = id;
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

async function _saveMachine() {
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
    } else {
      body.host = host;
    }
    if (api_key !== null) body.api_key = api_key;
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
    _machineEditing = null;
    byId('machineForm').hidden = true;
    byId('addMachineBtn').hidden = false;
    await loadMachines();
    _renderMachineList();
    setStatus('Machine saved', 'success');
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    save.disabled = false;
  }
}

function _showAddMachine() {
  _machineEditing = null;
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

// ── Settings save ───────────────────────────────────────────────────────────────────

async function saveSettings(event) {
  if (event) event.preventDefault();
  const save = byId('settingsSave');
  save.disabled = true;
  try {
    const body = {};
    // The default model is per-backend now and saves through the Backends tab.
    // App tab settings
    const sessionTtl = parseInt(byId('sessionTtl')?.value);
    if (sessionTtl) body.session_ttl = sessionTtl;
    const turnTimeout = parseInt(byId('turnTimeout')?.value);
    if (turnTimeout) body.turn_timeout = turnTimeout;
    const promptMax = parseInt(byId('promptMax')?.value);
    if (promptMax) body.prompt_max = promptMax;
    if (!Object.keys(body).length) {
      setStatus('No changes to save', 'success');
      save.disabled = false;
      return;
    }
    const resp = await apiFetch('/api/settings', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || data.detail || 'Could not save settings');
    }
    // Re-read so the inputs and the model picker show what was actually
    // stored. Without this the picker kept the pre-save options until a full
    // page reload, which looked like the save had not taken effect.
    await loadSettings();
    setStatus('Settings saved', 'success');
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    save.disabled = false;
  }
}

async function saveChatDialog(event) {
  event.preventDefault();
  const save = byId('dialogSave');
  save.disabled = true;
  try {
    if (dialogMode === 'delete') {
      const response = await apiFetch(`/api/chats/${encodeURIComponent(dialogChat.id)}`, {method: 'DELETE'});
      if (!response.ok) throw new Error('Could not delete conversation');
      const deletedId = dialogChat.id;
      const deletingActive = state.currentChat?.id === deletedId;
      storageRemove(`wc_draft_${deletedId}`);
      if (deletingActive) {
        state.currentChat = null;
        if (storageGet('wc_last_chat') === deletedId) storageRemove('wc_last_chat');
        showWelcome();
      }
      closeDialog();
      await refreshChats();
      showToast('Conversation deleted');
      return;
    }

    const title = byId('chatTitleInput').value.trim();
    const description = byId('chatDescriptionInput').value.trim();
    if (!title) {
      byId('chatTitleInput').focus();
      return;
    }
    if (dialogMode === 'create') {
      const response = await apiFetch('/api/chats', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title, description: description || null}),
      });
      if (!response.ok) throw new Error('Could not create conversation');
      const data = await response.json();
      closeDialog();
      await refreshChats();
      await selectChat(data.id);
    } else {
      const response = await apiFetch(`/api/chats/${encodeURIComponent(dialogChat.id)}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title, description}),
      });
      if (!response.ok) throw new Error('Could not save conversation');
      closeDialog();
      await refreshChats();
      if (state.currentChat?.id === dialogChat.id) await selectChat(dialogChat.id);
    }
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    save.disabled = false;
  }
}

function findChat(id) {
  return state.chats.find(chat => chat.id === id);
}

async function refreshChats() {
  const response = await apiFetch('/api/chats');
  if (!response.ok) throw new Error('Could not load conversations');
  state.chats = (await response.json()).chats || [];
  listController.render(state.chats, state.currentChat?.id);
}

function updateCurrentUi(chat) {
  byId('topbarTitle').textContent = chat.title;
  byId('workspaceStrip').style.display = 'flex';
  byId('workspacePath').textContent = chat.work_dir;
  byId('workspacePath').title = chat.work_dir;
  byId('editChatBtn').hidden = false;
  byId('composerArea').style.display = 'block';
  storageSet('wc_last_chat', chat.id);
  listController.render(state.chats, chat.id);
  updateModelDisplay(chat.model);
  const picker = byId('conversationModel');
  if (picker) picker.value = '';
  populateModelPicker();
}

async function selectChat(id) {
  const chat = findChat(id);
  if (!chat || chat.archived) return;
  closeSidebar();
  try {
    await conversationController.selectChat(chat);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function showWelcome() {
  conversationController?.persistDraft();
  state.currentChat = null;
  byId('topbarTitle').textContent = 'WebConsole';
  byId('workspaceStrip').style.display = 'none';
  byId('editChatBtn').hidden = true;
  byId('composerArea').style.display = 'none';
  const area = byId('messagesArea');
  area.replaceChildren();
  const empty = document.createElement('div');
  empty.className = 'empty-state';
  const icon = document.createElement('div');
  icon.className = 'icon';
  icon.textContent = '⌁';
  const title = document.createElement('strong');
  title.textContent = 'Start from a workspace';
  const text = document.createElement('p');
  text.textContent = 'Create or choose a conversation to begin.';
  const button = document.createElement('button');
  button.className = 'btn-primary';
  button.type = 'button';
  button.textContent = 'New conversation';
  button.addEventListener('click', () => openChatDialog('create'));
  empty.append(icon, title, text, button);
  area.appendChild(empty);
  listController?.render(state.chats, null);
}

async function patchChat(chat, body, success) {
  const response = await apiFetch(`/api/chats/${encodeURIComponent(chat.id)}`, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error('Could not update conversation');
  await refreshChats();
  showToast(success);
}

// Hand a conversation back to a terminal. `claude --resume <id>` resolves the
// session from anywhere -- verified: a session created in one directory resumes
// from another and keeps appending to its original transcript, it does not fork.
// The `cd` is still worth including so Claude runs where the conversation's
// files are.
async function copyTerminalCommand(chat) {
  if (!chat.session_id) {
    showToast('This conversation has no session yet — send a message first.');
    return;
  }
  const command = `cd ${chat.work_dir} && claude --resume ${chat.session_id}`;
  try {
    await navigator.clipboard.writeText(command);
    showToast('Terminal command copied');
  } catch {
    // Clipboard needs a secure context and permission; showing the command is
    // still useful when it is unavailable.
    showToast(command);
  }
}

async function handleChatAction(action, id) {
  const chat = findChat(id);
  if (!chat) return;
  try {
    if (action === 'pin') {
      await patchChat(chat, {pinned: !chat.pinned}, chat.pinned ? 'Conversation unpinned' : 'Conversation pinned');
    } else if (action === 'rename') {
      openChatDialog('edit', chat);
    } else if (action === 'export') {
      await downloadMarkdown(chat);
    } else if (action === 'fork') {
      await forkChat(chat);
    } else if (action === 'archive' || action === 'restore') {
      const archived = action === 'archive';
      await patchChat(chat, {archived}, archived ? 'Conversation archived' : 'Conversation restored');
      if (archived && state.currentChat?.id === id) {
        storageRemove('wc_last_chat');
        showWelcome();
      }
    } else if (action === 'terminal') {
      await copyTerminalCommand(chat);
    } else if (action === 'delete') {
      openChatDialog('delete', chat);
    }
  } catch (error) {
    showToast(action === 'export' ? 'Could not export conversation. Try again.' : error.message, 'error');
  }
}

async function resumeCliSession(sessionId) {
  try {
    const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, {method: 'POST'});
    if (!response.ok) throw new Error('Could not open session');
    const data = await response.json();
    closeSidebar();
    await refreshChats();
    await selectChat(data.id);
    showToast(`Opened session “${data.title}”`);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function removeCliSession(sessionId) {
  try {
    const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {method: 'DELETE'});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || 'Could not remove session');
    }
    await refreshSessions();
    showToast('Session removed');
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function forkChat(chat) {
  try {
    const response = await apiFetch(`/api/chats/${encodeURIComponent(chat.id)}/fork`, {method: 'POST'});
    if (!response.ok) throw new Error('Could not fork conversation');
    const data = await response.json();
    await refreshChats();
    closeSidebar();
    showToast(`Forked as “${data.title}”`);
    await selectChat(data.id);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function _clearSearch() {
  if (_searchDebounce) {
    clearTimeout(_searchDebounce);
    _searchDebounce = null;
  }
  listController.setQuery('');
}

async function loadSettings() {
  try {
    const response = await apiFetch('/api/settings');
    if (response.ok) {
      const data = await response.json();
      _loadedSettings = data;
      byId('ver').textContent = data.version || '';
      if (data.session_ttl_s) byId('sessionTtl').value = data.session_ttl_s;
      if (data.turn_timeout_s) byId('turnTimeout').value = data.turn_timeout_s;
      if (data.prompt_max) byId('promptMax').value = data.prompt_max;
      // The global default is only a fallback for when no backend is active;
      // it is still worth offering in the picker.
      _modelOptions = [data.default_model];
      populateModelPicker();
      return data;
    }
  } catch {}
  return {};
}

function populateModelPicker() {
  const picker = byId('conversationModel');
  if (!picker) return;
  const current = picker.value;
  // Offer the configured default and fallback first, then whatever the active
  // machine actually serves. This used to read a hardcoded datalist out of the
  // DOM and, worse, every model id harvested from old transcripts -- so it
  // offered models the current backend has never served and the turn failed.
  // Only the models this backend is set to offer. An empty active list means
  // every served model is offered, so the feature stays opt-in.
  const active = _modelsSource?.active || [];
  const served = _servedModels
    .map(model => model.id)
    .filter(id => !active.length || active.includes(id));
  // Keep whatever this chat already uses, so a model that was later
  // deactivated stays selectable rather than silently becoming Automatic --
  // hiding a model must never break a conversation already using it.
  const chatModel = state.currentChat?.model;
  const models = [..._modelOptions, ...served, chatModel, current];

  picker.replaceChildren();
  const automatic = document.createElement('option');
  automatic.value = '';
  automatic.textContent = 'Automatic';
  picker.appendChild(automatic);
  [...new Set(models.filter(Boolean))].forEach(model => {
    const option = document.createElement('option');
    option.value = model;
    option.textContent = model;
    picker.appendChild(option);
  });
  picker.value = current;
}

// Open the Backends tab: machines first so the cards exist, then the models
// each one serves. Only Anthropic-protocol backends publish a list.
async function loadBackends() {
  await loadMachines();
  _renderMachineList();
  await Promise.all(
    _machines
      .filter(machine => machine.provider === 'anthropic')
      .map(machine => loadModelsFor(machine.id)),
  );
}

async function loadModelsFor(machineId, force = false) {
  if (!force && _modelsByMachine.has(machineId)) return;
  try {
    const response = await apiFetch(`/api/models?machine_id=${encodeURIComponent(machineId)}`);
    if (!response.ok) throw new Error('Could not load models');
    const data = await response.json();
    _modelsByMachine.set(machineId, {
      models: data.models || [],
      active: data.active || [],
      default: data.default || '',
      source: data.source,
      reason: data.reason,
      endpoint: data.endpoint,
    });
  } catch {
    _modelsByMachine.set(machineId, {
      models: [],
      active: [],
      default: '',
      source: 'error',
      reason: 'Could not load the model list.',
    });
  }
  _refreshServedModels();
  _renderMachineList();
}

// The picker follows the active machine, so that is the entry it reads.
function _refreshServedModels() {
  const active = _machines.find(machine => machine.active);
  const entry = active ? _modelsByMachine.get(active.id) : null;
  _servedModels = entry ? entry.models : [];
  _modelsSource = entry;
  _syncModelSuggestions();
  populateModelPicker();
}

async function _toggleModelOffered(machine, modelId) {
  const entry = _modelsByMachine.get(machine.id);
  if (!entry) return;
  const offersAll = entry.active.length === 0;
  // Narrowing from "everything" starts from the full served list, so
  // unticking one model does not silently drop all the others.
  const current = offersAll ? entry.models.map(model => model.id) : [...entry.active];
  const index = current.indexOf(modelId);
  if (index === -1) current.push(modelId);
  else current.splice(index, 1);

  // Unticking everything means "offer everything" rather than an empty picker.
  const next = current.length === entry.models.length || current.length === 0
    ? []
    : current;
  // The server rejects a default outside the offered set, and it would be
  // unpickable anyway, so move it rather than sending a request that fails.
  let nextDefault = entry.default;
  if (next.length && nextDefault && !next.includes(nextDefault)) {
    nextDefault = next[0];
  }
  await _saveMachineModels(machine, next, nextDefault);
}

async function _setModelDefault(machine, modelId) {
  const entry = _modelsByMachine.get(machine.id);
  if (!entry) return;
  // Choosing a default implies offering it.
  const next = entry.active.length && !entry.active.includes(modelId)
    ? [...entry.active, modelId]
    : entry.active;
  await _saveMachineModels(machine, next, modelId);
}

async function _saveMachineModels(machine, active, defaultModel) {
  try {
    const response = await apiFetch(
      `/api/machines/${encodeURIComponent(machine.id)}/models`,
      {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({active, default: defaultModel || null}),
      },
    );
    if (!response.ok) {
      let detail = '';
      try {
        const data = await response.json();
        detail = data.error || data.detail || '';
      } catch { /* fall through to the generic message */ }
      throw new Error(detail || 'Could not save the model selection');
    }
    const saved = await response.json();
    const entry = _modelsByMachine.get(machine.id);
    entry.active = saved.active || [];
    entry.default = saved.default || '';
    // The machine card shows the default in its meta line.
    const listed = _machines.find(item => item.id === machine.id);
    if (listed && entry.default) listed.model = entry.default;
    _refreshServedModels();
    _renderMachineList();
    notifyResult('Model selection saved', 'success');
  } catch (error) {
    notifyResult(error.message, 'error');
    // Re-read rather than leave the checkboxes showing a state that failed.
    await loadModelsFor(machine.id, true);
  }
}

// The two model inputs are free text on purpose: a gateway will accept ids it
// does not advertise, so the list narrows typing without forbidding anything.
function _syncModelSuggestions() {
  const list = byId('modelSuggestions');
  if (!list) return;
  list.replaceChildren();
  _servedModels.forEach(model => {
    const option = document.createElement('option');
    option.value = model.id;
    if (model.display_name && model.display_name !== model.id) {
      option.label = model.display_name;
    }
    list.appendChild(option);
  });
}

async function updateModelDisplay(model) {
  const label = byId('modelLabel');
  if (model) {
    const short = model.replace(/^claude-/, '').replace(/-.*$/, '');
    label.textContent = `Model: ${model}`;
    label.title = `Using ${model}`;
    label.hidden = false;
  } else {
    label.hidden = true;
  }
}

// Reload the CLI session list. Extracted from loadInitialData so removing a
// dead session can refresh the sidebar without a full page reload.
async function refreshSessions() {
  const response = await apiFetch('/api/sessions');
  if (!response.ok) return;
  const sessions = (await response.json()).sessions || [];
  listController.setCliSessions(sessions.filter(item => !item.webchat));
  // The `model` on a session is archaeology -- the last model that old
  // transcript used -- and never a statement that the backend still serves it.
  // Harvesting it into the picker turned a historical record into a menu of
  // offers, which is how a dead model id got selected and every turn failed.
  populateModelPicker();
  await refreshHistory();
  listController.render(state.chats, state.currentChat?.id ?? null);
}

// Past conversations, read from transcripts rather than the live registry.
// ~/.claude/sessions only lists sessions that are still running, so finished
// work is invisible to /api/sessions no matter how recent it is.
async function refreshHistory() {
  try {
    const response = await apiFetch('/api/transcripts?limit=50');
    if (!response.ok) return;
    listController.setHistory((await response.json()).transcripts || []);
  } catch {
    // History is supplementary; the live list must still render without it.
  }
}

async function loadInitialData() {
  try {
    const settings = await loadSettings();
    await refreshChats();
    await refreshSessions();
    await loadMachines();
    // Only the active backend's models are needed to fill the picker at boot;
    // the rest load when the Backends tab is opened.
    const active = _machines.find(machine => machine.active);
    if (active && active.provider === 'anthropic') await loadModelsFor(active.id);
    const lastId = storageGet('wc_last_chat');
    const last = findChat(lastId);
    if (last && !last.archived) await selectChat(last.id);
    else showWelcome();
  } catch (error) {
    if (error.message !== 'Session expired') showToast(error.message, 'error');
  }
}

async function logout() {
  try { await fetch('/logout', {method: 'POST', credentials: 'same-origin'}); } catch {}
  window.location.assign('/login');
}

document.addEventListener('DOMContentLoaded', () => {
  applySavedTheme();
  byId('themeToggle').addEventListener('click', toggleTheme);
  byId('menuBtn').addEventListener('click', openSidebar);
  byId('sidebarCloseBtn').addEventListener('click', closeSidebar);
  byId('sidebarOverlay').addEventListener('click', closeSidebar);
  byId('logoutBtn').addEventListener('click', logout);
  byId('editChatBtn').addEventListener('click', () => openChatDialog('edit'));
  byId('settingsBtn').addEventListener('click', openSettingsDialog);
  byId('settingsCancel').addEventListener('click', closeSettingsDialog);
  byId('settingsForm').addEventListener('submit', saveSettings);
  byId('settingsDialog').addEventListener('click', event => { if (event.target === byId('settingsDialog')) closeSettingsDialog(); });
  byId('settingsSave').addEventListener('click', saveSettings);
  byId('conversationModel')?.addEventListener('change', event => {
    const model = event.target.value;
    if (model) showToast(`Next turn will use ${model}`);
  });
  byId('addMachineBtn').addEventListener('click', _showAddMachine);
  byId('cancelMachine').addEventListener('click', () => { byId('machineForm').hidden = true; byId('addMachineBtn').hidden = false; _machineEditing = null; });
  byId('saveMachine').addEventListener('click', _saveMachine);
  byId('machineProvider').addEventListener('change', _syncMachineProviderFields);
  const settingsTabs = Array.from(document.querySelectorAll('.settings-tab'));
  settingsTabs.forEach((tab, index) => {
    tab.addEventListener('click', () => _switchTab(tab.dataset.tab));
    // WAI-ARIA roving tabindex: arrows move between tabs, Home/End jump to ends.
    tab.addEventListener('keydown', event => {
      const offsets = {ArrowRight: 1, ArrowLeft: -1};
      let next = null;
      if (event.key in offsets) {
        next = (index + offsets[event.key] + settingsTabs.length) % settingsTabs.length;
      } else if (event.key === 'Home') {
        next = 0;
      } else if (event.key === 'End') {
        next = settingsTabs.length - 1;
      }
      if (next === null) return;
      event.preventDefault();
      _switchTab(settingsTabs[next].dataset.tab);
      settingsTabs[next].focus();
    });
  });
  byId('usageRange')?.addEventListener('change', () => loadUsage());
  byId('skillSearch')?.addEventListener('input', event => {
    _skillFilter = event.target.value;
    clearTimeout(_skillDebounce);
    _skillDebounce = setTimeout(_renderSkills, 120);
  });
  byId('dialogCancel').addEventListener('click', closeDialog);
  byId('chatForm').addEventListener('submit', saveChatDialog);
  byId('chatDialog').addEventListener('click', event => { if (event.target === byId('chatDialog')) closeDialog(); });
  listController = createChatListController({
    lists: [byId('chatList'), byId('chatListDesktop')],
    searchInputs: [byId('chatSearch'), byId('chatSearchDesktop')],
    formatTime,
    formatAbsoluteTime,
    onSelect: selectChat,
    onAction: handleChatAction,
    onResumeCli: resumeCliSession,
    onRemoveCli: removeCliSession,
  });
  listController.setOnMessageSearch(async (q) => {
    try {
      const response = await apiFetch('/api/chats/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: q}),
      });
      if (!response.ok) throw new Error('Search failed');
      const data = await response.json();
      // Hand the hits to the controller as message results. Passing them to
      // render() treated them as the chat list, so they were re-filtered by
      // title and dropped -- which is why message search showed nothing.
      listController.setMessageResults(data.results || []);
    } catch {
      listController.setMessageResults([]);
    }
  });

  // conversation.js owns the turn lifecycle and writes it to #runState. Observing
  // that attribute keeps the sidebar dot in sync without reaching into its
  // internals or adding a callback across the module boundary.
  const runStateEl = byId('runState');
  if (runStateEl) {
    const ACTIVE = new Set(['connecting', 'thinking', 'responding', 'retrying']);
    new MutationObserver(() => {
      const running = ACTIVE.has(runStateEl.dataset.state);
      listController.setActiveTurn(running ? (state.currentChat?.id ?? null) : null);
    }).observe(runStateEl, {attributes: true, attributeFilter: ['data-state']});
  }

  conversationController = createConversationController({
    state,
    elements: {
      messages: byId('messagesArea'), composerInput: byId('composerInput'),
      modelPicker: byId('conversationModel'),
      sendButton: byId('sendBtn'), retryButton: byId('retryBtn'),
      jumpButton: byId('jumpToLatest'), runState: byId('runState'),
      composerStatus: byId('composerStatus'),
    },
    apiFetch, storageGet, storageSet, storageRemove, showToast,
    onChatLoaded: updateCurrentUi,
    refreshChats,
  });

  document.querySelectorAll('.new-chat-btn').forEach(button => button.addEventListener('click', () => openChatDialog('create')));

  document.addEventListener('keydown', event => {
    trapDialogFocus(event);
    if (event.key === 'Escape') {
      if (byId('settingsDialog').classList.contains('open')) closeSettingsDialog();
      else if (byId('chatDialog').classList.contains('open')) closeDialog();
      else closeSidebar();
    }
  });

  loadInitialData();
});
