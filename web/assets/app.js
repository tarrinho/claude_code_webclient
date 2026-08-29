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
let _currentTab = 'machines';
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
  _switchTab('machines');
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
  const map = { machines: 'panelMachines', models: 'panelModels', usage: 'panelUsage', skills: 'panelSkills', app: 'panelApp' };
  const activeId = map[tab] || 'panelMachines';
  ['panelMachines', 'panelModels', 'panelUsage', 'panelSkills', 'panelApp'].forEach(id => {
    const el = byId(id);
    if (el) el.hidden = id !== activeId;
  });
  // The footer Save button only writes the Models and App fields. Machines save
  // through their own form and Skills is read-only, so showing it there offered
  // a control that silently did nothing.
  const save = byId('settingsSave');
  if (save) save.hidden = tab === 'machines' || tab === 'skills' || tab === 'usage';
  if (tab === 'machines') _renderMachineList();
  if (tab === 'models') loadModels();
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
    const cost = row.cost_usd === null || row.cost_usd === undefined
      ? _cell('—', 'usage-num usage-muted',
              row.cost_note || 'Not available for this backend.')
      : _cell(`$${Number(row.cost_usd).toFixed(2)}`, 'usage-num');
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
    provider.textContent = m.provider === 'anthropic' ? 'Anthropic API' : 'Proxy';
    top.appendChild(provider);

    card.appendChild(top);

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
    // Compare against what was loaded rather than testing truthiness: an empty
    // string is a legitimate value meaning "clear this", and gating on
    // truthiness made the field impossible to clear from the UI.
    const defaultModel = byId('defaultModel').value.trim();
    if (defaultModel !== (_loadedSettings.default_model || '')) {
      body.default_model = defaultModel;
    }
    const fallbackModel = byId('fallbackModel').value.trim();
    if (fallbackModel !== (_loadedSettings.fallback_model || '')) {
      body.fallback_model = fallbackModel;
    }
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
      byId('defaultModel').value = data.default_model || '';
      byId('fallbackModel').value = data.fallback_model || '';
      byId('ver').textContent = data.version || '';
      if (data.session_ttl_s) byId('sessionTtl').value = data.session_ttl_s;
      if (data.turn_timeout_s) byId('turnTimeout').value = data.turn_timeout_s;
      if (data.prompt_max) byId('promptMax').value = data.prompt_max;
      _modelOptions = [data.default_model, data.fallback_model];
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
  const served = _servedModels.map(model => model.id);
  // Keep whatever this chat already uses, so an unlisted model stays selectable
  // instead of silently falling back to Automatic.
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

async function loadModels() {
  const status = byId('modelsStatus');
  if (status) status.textContent = 'Loading…';
  try {
    const response = await apiFetch('/api/models');
    if (!response.ok) throw new Error('Could not load models');
    const data = await response.json();
    _servedModels = data.models || [];
    _modelsSource = data;
  } catch {
    _servedModels = [];
    _modelsSource = {source: 'error', reason: 'Could not load the model list.'};
  }
  _renderModelsList();
  _syncModelSuggestions();
  populateModelPicker();
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

function _renderModelsList() {
  const list = byId('modelsList');
  const status = byId('modelsStatus');
  if (!list) return;
  list.replaceChildren();

  if (status) {
    const count = _servedModels.length;
    if (_modelsSource?.source === 'endpoint') {
      status.textContent = `${count} model${count === 1 ? '' : 's'} from ${_modelsSource.endpoint}`;
      status.className = 'models-status';
    } else {
      // Say why these are guesses. The old page showed a frozen list with no
      // hint that it might not match the backend at all.
      status.textContent = _modelsSource?.reason
        ? `${_modelsSource.reason} Showing built-in suggestions.`
        : 'Showing built-in suggestions.';
      status.className = 'models-status models-status-warn';
    }
  }

  if (!_servedModels.length) {
    const empty = document.createElement('div');
    empty.className = 'sidebar-empty';
    empty.textContent = 'No models to show.';
    list.appendChild(empty);
    return;
  }

  const currentDefault = byId('defaultModel')?.value.trim();
  _servedModels.forEach(model => {
    const row = document.createElement('div');
    row.className = 'model-item';

    const name = document.createElement('span');
    name.className = 'model-item-id';
    name.textContent = model.id;
    row.appendChild(name);

    if (model.display_name && model.display_name !== model.id) {
      const display = document.createElement('span');
      display.className = 'model-item-name';
      display.textContent = model.display_name;
      row.appendChild(display);
    }

    if (model.id === currentDefault) {
      const badge = document.createElement('span');
      badge.className = 'machine-badge';
      badge.textContent = 'Default';
      row.appendChild(badge);
    }

    const actions = document.createElement('div');
    actions.className = 'model-item-actions';
    [['Set default', 'defaultModel'], ['Set fallback', 'fallbackModel']].forEach(
      ([label, target]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'machine-action';
        button.textContent = label;
        button.addEventListener('click', () => {
          byId(target).value = model.id;
          // Save is the footer button on this tab, so re-render to show the
          // badge moving without implying the change is already persisted.
          _renderModelsList();
        });
        actions.appendChild(button);
      },
    );
    row.appendChild(actions);
    list.appendChild(row);
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
    await loadModels();
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
  byId('refreshModels').addEventListener('click', loadModels);
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
