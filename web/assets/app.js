// Versioned like the <script> tags below (?v=N), for a reason those tags do
// not have to deal with: a bare specifier ('./chat-list.js') is its own cache
// key, untouched by bumping app.js's own ?v= on its <script> tag. Bumping
// app.js changed nothing about how long a browser keeps a cached chat-list.js
// -- which is why editing that file could leave a page rendering with stale
// logic indefinitely, no matter how many times app.js itself was reloaded.
// Bump the number here whenever the imported file's behaviour changes.
import {apiFetch, downloadMarkdown} from './api.js?v=1';
import {createChatListController} from './chat-list.js?v=4';
import {createConversationController, parseTimestamp, prefersAutoFocus} from './conversation.js?v=6';
import {_closeSupervisorPicker, openSupervisorPicker, openSupervisorPane, closeSupervisorPane} from './orchestrator.js?v=1';
import {_syncAlertToggle, toggleAlerts, refreshSupervisor, dismissAgent, clearSupervisor, markAgentSeen, startSupervisorPolling} from './device-alerts.js?v=2';
import {renderVoiceSettingsFields, collectVoiceSettingsFields} from './voice-settings.js?v=7';

// Exported for orchestrator.js/device-alerts.js, which need this live app state
// but are also loaded standalone (own <script type="module">) and so cannot
// see app.js's top-level scope any other way. A circular import back to the
// file that imports them is safe here because both are only read inside
// function bodies in the consumers, never at their own module top level --
// by the time either runs (after DOMContentLoaded), every module involved has
// finished its own top-level evaluation.
export const state = {
  chats: [],
  currentChat: null,
  streamState: 'ready',
  // Element to restore focus to when a dialog/menu closes. A property here
  // rather than its own top-level `let`, because orchestrator.js needs to write
  // it too, and an imported binding for a bare `let` is read-only -- only a
  // shared object's properties can be assigned across modules.
  previousFocus: null,
};
window.state = state;
// Expose a refresh hook for voice handoff and other external consumers.
window.__webConsoleRefresh = async (chatId) => {
  try {
    await refreshChats();
    if (chatId && chatId === state.currentChat?.id) {
      conversationController?.refreshCurrent();
    }
  } catch {}
};

// Exported for machines.js, same circular-import argument as `state` above --
// machines.js only reads these inside function bodies, never at its own
// module top level, so by the time any of it runs, this module has already
// finished its own top-level evaluation.
export const byId = id => document.getElementById(id);
export const storageGet = key => { try { return localStorage.getItem(key); } catch { return null; } };
export const storageSet = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
const storageRemove = key => { try { localStorage.removeItem(key); } catch {} };
let dialogMode = 'create';
let dialogChat = null;
let dialogVoiceMode = false;
// Reassigned once in loadInitialData-adjacent setup; exported for the same
// reason and under the same safety argument as `state` above.
export let listController;
let conversationController;
// Exported read-only: server-stats.js only reads this to decide whether a
// result reports as a toast or a status line, never reassigns it, so unlike
// listController/state it needs no setter.
export let settingsVisible = false;
// A bare `let` gives an importer a live *read*, but reassigning an imported
// binding is illegal (it is read-only from the importing side) -- the same
// restriction `previousFocus` above exists to work around. machines.js needs
// to write these two, so it goes through the setters just below instead of
// assigning the identifiers directly.
export let _activeMachineId = null;
export function _setActiveMachineId(id) { _activeMachineId = id; }
// An array's *contents* can be mutated in place (`.length = 0; .push(...)`)
// without ever reassigning the binding, so this one stays a plain exported
// `let` -- machines.js mutates it rather than replacing it.
export let _machines = [];
export let _machineEditing = null;
export function _setMachineEditing(id) { _machineEditing = id; }
let _currentTab = 'backends';
export let _modelOptions = [];
// Last GET /api/models payload: what the active machine reports it serves.
export let _servedModels = [];
// model id -> turns in the current window. The map draws traffic, so it needs
// the same numbers the Usage tab reports rather than a second source of truth.
export let _turnsByModel = new Map();
export let _modelsSource = null;
// Last payload from GET /api/settings. Save compares against it so a field
// cleared to "" is recognised as a change and actually sent.
let _loadedSettings = {};
let _searchDebounce = null;
let _usageData = null;          // last GET /api/usage payload
let _usageFetchedFor = null;    // range the payload was fetched for
let _skillsData = null;          // last successful /api/skills payload
let _skillsFetchedFor = null;    // chat id the payload was fetched for
let _skillDebounce = null;
let _mapLoading = false;         // guard against double-click opening twice

// Exported for orchestrator.js, which reports add-to-orchestrator results with it.
export function showToast(message, type = '') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  byId('toastRegion').appendChild(toast);
  setTimeout(() => toast.remove(), 4500);
}

// Debug-mode knob wrapper.  When the user flips the toggle in Settings → App
// the setting persists to the DB, so the state survives a page reload.
// When debug_console is OFF, all console.log calls become no-ops so the
// browser console stays quiet.  console.error and console.warn are always
// enabled because they signal real problems.
export function debugLog(...args) {
  // Read the knob each call so a late toggle (or a setting that loaded
  // after the initial script eval) still takes effect without reload.
  const el = byId('debugConsole');
  if (el && el.getAttribute('aria-pressed') === 'true') {
    console.log(...args);
  }
}

// Exported for usage.js, so both places a turn's time is rendered agree on
// the same rule rather than growing a second implementation.
export function formatTime(iso) {
  const date = parseTimestamp(iso);
  if (!date) return '';
  const diff = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return date.toLocaleDateString();
}

export function formatAbsoluteTime(iso) {
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
  state.previousFocus = document.activeElement;
  byId('sidebar').inert = false;
  byId('sidebar').classList.add('open');
  byId('sidebar').setAttribute('aria-hidden', 'false');
  byId('menuBtn').setAttribute('aria-expanded', 'true');
  byId('sidebarOverlay').style.display = 'block';
  // Same reasoning as prefersAutoFocus in conversation.js: focusing a text
  // input opens the on-screen keyboard on Android, which then covers most of
  // the sidebar the user just opened to browse -- on the one gesture (tapping
  // the menu icon) that is unambiguously not a request to type. A pointer
  // device keeps the focus, since there the keyboard costs nothing and typing
  // straight into search is the point.
  if (prefersAutoFocus()) byId('chatSearch').focus();
}

function closeSidebar() {
  if (!byId('sidebar').classList.contains('open')) return;
  byId('sidebar').classList.remove('open');
  byId('sidebar').setAttribute('aria-hidden', 'true');
  byId('sidebar').inert = true;
  byId('menuBtn').setAttribute('aria-expanded', 'false');
  byId('sidebarOverlay').style.display = 'none';
  if (state.previousFocus && document.body.contains(state.previousFocus)) state.previousFocus.focus();
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

function openChatDialog(mode, chat = state.currentChat, options = {}) {
  dialogMode = mode;
  dialogChat = chat;
  dialogVoiceMode = Boolean(options.voiceMode);
  state.previousFocus = document.activeElement;
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
  // Voice-mode toggle: only visible when editing an existing chat.
  // Create uses the separate "create from voice" path (dialogVoiceMode).
  if (editing) {
    byId('voiceModeLabel').hidden = false;
    byId('chatVoiceModeToggle').checked = Boolean(chat?.voice_mode);
  } else {
    byId('voiceModeLabel').hidden = true;
  }
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
  dialogVoiceMode = false;
  byId('chatVoiceModeToggle').checked = false;
  if (state.previousFocus && document.body.contains(state.previousFocus)) state.previousFocus.focus();
}

function openSettingsDialog() {
  state.previousFocus = document.activeElement;
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
  // The Server tab polls while it is open; closing the dialog is leaving it.
  stopServerPolling();
  if (state.previousFocus && document.body.contains(state.previousFocus)) state.previousFocus.focus();
}

async function _openMap() {
  const panel = byId('supervisorMapPanel');
  if (!panel) return;
  if (_mapLoading) return;        // guard against double-click
  if (!panel.hidden) { _closeMap(); return; }
  _mapLoading = true;
  // Remove attribute and set property to guarantee open state
  panel.removeAttribute('hidden');
  panel.hidden = false;
  // Hide main content while panel is open — main creates a stacking context
  // that visually covers the panel's SVG nodes, making them unclickable.
  const main = document.querySelector('main');
  if (main) main.hidden = true;

  try {
    const res = await fetch('/api/supervisor-map', {credentials: 'same-origin'});
    if (!res.ok) throw new Error('Data unavailable');
    const data = await res.json();
    const { renderSupervisorMap, closeSupervisorMap: closeMap } = await import('./supervisor-map.js?v=3');
    if (closeMap) closeMap();
    renderSupervisorMap(data);
  } catch {
    const { closeSupervisorMap } = await import('./supervisor-map.js?v=3');
    if (closeSupervisorMap) closeSupervisorMap();
    byId('mapStatusEmpty').textContent = 'Connection error.';
    byId('mapStatusEmpty').hidden = false;
  } finally {
    _mapLoading = false;
  }
}

async function _closeMap() {
  const panel = byId('supervisorMapPanel');
  if (panel) panel.hidden = true;
  // Restore main content
  const main = document.querySelector('main');
  if (main) main.hidden = false;
  const { closeSupervisorMap } = await import('./supervisor-map.js?v=3');
  closeSupervisorMap();
}

function _switchTab(tab) {
  _currentTab = tab;
  document.querySelectorAll('.settings-tab').forEach(t => {
    const active = t.dataset.tab === tab;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
    t.tabIndex = active ? 0 : -1;
  });
  const map = {
    backends: 'panelBackends', usage: 'panelUsage', stats: 'panelStats',
    server: 'panelServer', skills: 'panelSkills', app: 'panelApp',
  };
  const activeId = map[tab] || 'panelBackends';
  ['panelBackends', 'panelUsage', 'panelStats', 'panelServer', 'panelSkills',
   'panelApp'].forEach(id => {
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
  // Same reasoning as usage: always refetch rather than show numbers from
  // before the turns the operator just ran.
  if (tab === 'stats') loadStats(true);
  // A live host reading is stale the moment it is drawn, so this one is never
  // served from cache at all -- and it keeps refreshing while it is on screen,
  // which is the whole point of a "live" reading. Stopped on the way out of the
  // tab so a closed panel is not polling /proc in the background for ever.
  if (tab === 'server') {
    loadServer();
    startServerPolling();
  } else {
    stopServerPolling();
  }
  if (tab === 'skills') loadSkills();
}

// ── Usage ─────────────────────────────────────────────────────────────────────────
import { _renderUsage, loadUsage } from './usage.js?v=1';
// The same rows the Usage tab sums, kept in time order. Rendered by stats.js,
// which owns the SVG; this only fetches and reports failure.

let _statsFetchedFor = null;

async function loadStats(force = false) {
  const body = byId('statsBody');
  if (!body) return;
  const range = byId('statsRange')?.value || '30';
  const bucket = byId('statsBucket')?.value || 'day';
  const key = `${range}:${bucket}`;
  if (!force && _statsFetchedFor === key) return;

  body.replaceChildren(...Array.from({length: 2}, () => {
    const row = document.createElement('div');
    row.className = 'skill-skeleton';
    return row;
  }));
  const count = byId('statsCount');
  if (count) count.textContent = 'Loading…';
  try {
    const resp = await apiFetch(
      `/api/usage/series?days=${encodeURIComponent(range)}` +
      `&bucket=${encodeURIComponent(bucket)}`);
    if (!resp.ok) throw new Error('Could not load statistics');
    const payload = await resp.json();
    // Imported lazily: the charts are a rarely-opened tab, and the module is
    // dead weight in the initial parse for every other page load.
    const {renderStats} = await import('./stats.js');
    renderStats(body, payload);
    _statsFetchedFor = key;
    if (count) {
      const turns = (payload.series || []).reduce((sum, r) => sum + (r.requests || 0), 0);
      const periods = new Set((payload.series || []).map(r => r.bucket)).size;
      // The bucket key is a wire value, not a word: pluralising it directly
      // rendered "28 halfhours".
      const noun = {halfhour: '30-minute slot', hour: 'hour', day: 'day', month: 'month'}[bucket]
        || bucket;
      count.textContent = turns
        ? `${turns.toLocaleString()} turns across ${periods} ${noun}${periods === 1 ? '' : 's'}`
        : '';
    }
  } catch (error) {
    _statsFetchedFor = null;
    if (count) count.textContent = '';
    const notice = document.createElement('div');
    notice.className = 'skills-notice';
    notice.textContent = error.message;
    body.replaceChildren(notice);
  }
}

// ── Server statistics ─────────────────────────────────────────────────────────
// Host health rather than model spend. Two requests because they answer
import { startServerPolling, stopServerPolling, loadServer, notifyResult, setStatus } from './server-stats.js?v=1';
export { notifyResult, setStatus };

import { _renderSkillSkeleton, _renderSkills, loadSkills } from './skills.js?v=1';

import { loadMachines, _activateMachine, _editMachine, _saveMachine, _showAddMachine, _syncMachineProviderFields, _modelsByMachine, _renderMachineList,
  // Lives in machines.js, which owns the canvas; called from here when the
  // Backends tab becomes visible. Was a bare cross-module reference.
  _drawMapWires } from './machines.js?v=6';

import { loadTransports, _showAddTransport, _cancelTransportForm, _testTransportForm, _saveTransport } from './transports.js?v=3';

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
    const webconsoleUrl = (byId('webconsoleUrl')?.value || '').trim();
    if (webconsoleUrl !== _loadedSettings?.webconsole_url) {
      body.webconsole_url = webconsoleUrl || '';
    }
    const debugPressed = byId('debugConsole')?.getAttribute('aria-pressed') === 'true';
    if (debugPressed !== _loadedSettings?.debug_console) {
      body.debug_console = debugPressed;
    }
    collectVoiceSettingsFields(body, _loadedSettings);
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
        body: JSON.stringify({title, description: description || null, voice_mode: dialogVoiceMode}),
      });
      if (!response.ok) throw new Error('Could not create conversation');
      const data = await response.json();
      closeDialog();
      await refreshChats();
      await selectChat(data.id);
    } else {
      const editedId = dialogChat.id;
      const voiceMode = byId('chatVoiceModeToggle').checked;
      const response = await apiFetch(`/api/chats/${encodeURIComponent(editedId)}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title, description, voice_mode: voiceMode}),
      });
      if (!response.ok) throw new Error('Could not save conversation');
      closeDialog();
      await refreshChats();
      if (state.currentChat?.id === editedId) await selectChat(editedId);
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

// Per-conversation "last time I looked", so a reply that arrived while the user
// was in another conversation can be marked. Compared against updated_at, which
// a finished turn already bumps -- no schema change, and it is a per-browser
// question anyway.
const seenKey = id => `wc_seen_${id}`;

function markSeen(chatId, updatedAt) {
  if (chatId && updatedAt) storageSet(seenKey(chatId), updatedAt);
}

function unreadChatIds(chats) {
  return chats
    .filter(chat => {
      if (chat.id === state.currentChat?.id) return false;
      const seen = storageGet(seenKey(chat.id));
      // Never opened is not unread: otherwise every conversation in the sidebar
      // lights up on a new browser.
      return seen ? String(chat.updated_at) !== seen : false;
    })
    .map(chat => chat.id);
}

// "Ended" tracks a running -> not-running transition, not a snapshot: a chat
// that has simply never run must not show it, only one that just stopped.
// Both keys live in localStorage rather than a JS Set kept across polls, so
// the transition still gets caught after a full reload -- close the tab while
// a turn is running, reopen once it has finished, and the first poll still
// compares against "it was running last time this browser looked".
const wasRunningKey = id => `wc_was_running_${id}`;
const endedKey = id => `wc_ended_${id}`;

function updateEndedTracking(chats) {
  for (const chat of chats) {
    const wasRunning = storageGet(wasRunningKey(chat.id)) === '1';
    if (wasRunning && !chat.running) storageSet(endedKey(chat.id), '1');
    storageSet(wasRunningKey(chat.id), chat.running ? '1' : '0');
  }
  return chats.filter(chat => storageGet(endedKey(chat.id)) === '1').map(chat => chat.id);
}

// Sending into a chat answers "is it done" before the next poll would, so the
// mark is dropped here rather than left to linger for up to CHAT_POLL_MS.
function clearEndedFlag(chatId) {
  if (!chatId) return;
  storageRemove(endedKey(chatId));
  listController.clearEnded(chatId);
}

// Bumped on every call, and used to drop a response that resolves after a
// later call's already has. Nothing here awaited a prior refreshChats()
// before starting another -- the 6s poll (CHAT_POLL_MS), a handful of
// post-action calls, and this poll racing the tab regaining visibility can
// all have two in flight at once, and this host runs under enough real
// concurrent load (several agent sessions, background turns, sysstats) that
// request latency genuinely varies request to request. Without this, a
// slower *older* response landing after a faster *newer* one silently wins
// -- observed live: a chat's marker correctly went running -> ended, then
// reverted to running two polls later, with the server (checked in
// logs/webconsole.log) having recorded only the one turn the whole time.
// The state never went backward there -- an earlier, slower response just
// arrived last and overwrote the newer one that had already rendered it
// correctly, reading as a highlight appearing and then disappearing.
let _refreshSeq = 0;

async function refreshChats() {
  const mySeq = ++_refreshSeq;
  const response = await apiFetch('/api/chats');
  if (!response.ok) throw new Error('Could not load conversations');
  const data = await response.json();
  if (mySeq !== _refreshSeq) return; // superseded by a later call; drop it
  state.chats = data.chats || [];
  // Which conversations are busy is server state now -- a turn outlives the tab
  // that started it, so the open page cannot know on its own.
  listController.setActiveTurns(state.chats.filter(c => c.running).map(c => c.id));
  listController.setUnread(unreadChatIds(state.chats));
  listController.setEnded(updateEndedTracking(state.chats));
  listController.render(state.chats, state.currentChat?.id);
}

function updateCurrentUi(chat) {
  // The conversation name sits in the strip, ahead of its directory, so the
  // two read as name-then-location on one line. The topbar keeps the product
  // name rather than swapping between the two.
  byId('topbarTitle').textContent = 'WebConsole';
  byId('workspaceName').textContent = chat.title;
  byId('workspaceName').title = chat.title;
  // Same name, repeated by the composer: the strip scrolls out of view on a
  // long conversation and a phone keyboard covers the rest of the screen, so
  // "which chat am I typing into" has nothing left to answer it up there.
  byId('composerChatName').textContent = chat.title;
  byId('composerChatName').title = chat.title;
  byId('workspaceStrip').style.display = 'flex';
  byId('editChatBtn').hidden = false;
  // Only a chat linked to a CLI session has a transcript to refresh from.
  byId('syncBtn').hidden = !chat.session_id;
  byId('composerArea').style.display = 'block';
  // Mic and live-conversation belong to voice chats only, and
  // voice-conversation.js cannot hear that the open chat changed.
  window.voiceConversation?.refreshControls?.();
  storageSet('wc_last_chat', chat.id);
  markSeen(chat.id, chat.updated_at);
  listController.setUnread(unreadChatIds(state.chats));
  listController.render(state.chats, chat.id);
  // chat.model is the routing PIN and empty for almost every conversation;
  // last_model_used is what actually answered its last turn. Falling back to
  // it is what stops this label staying hidden while a real model (Qwen,
  // Opus, whatever) is running -- the pin still wins when a user set one,
  // since that names what the NEXT turn will use rather than the last one.
  updateModelDisplay(chat.model || chat.last_model_used);
  populateBackendPicker(chat);
  ensurePinnedModels(chat);
  refreshQuestion();
  startQuestionPolling();
  startAutoAnswerPolling();
  // The pickers show this conversation's own routing, not a blank slate: both
  // are persisted per conversation, so two chats can sit on different backends.
  populateModelPicker(chat);
}

// The one place backend_kind maps to display text. Used to live here *and*
// as a second, separately-maintained copy in machines.js (_BACKEND_KIND_LABELS)
// -- neither one got updated when ssh_proxy shipped, so an ssh_proxy machine
// showed as "Claude Code proxy" in Settings (machines.js's copy fell through
// to its anthropic/else guess) and as the raw string "ssh-proxy" in the
// per-chat Backend picker (this copy's `|| machine.backend_kind` fallback).
// machines.js now imports this instead of keeping its own table.
export function backendKindLabel(kind) {
  // 'anthropic'/'anthropic-compatible' are what shared.py's backend_kind()
  // still actually emits as committed today -- a separate, still-uncommitted
  // session rename would replace those with 'direct', but until that lands
  // every real machine needs these old keys or its label falls back to a
  // raw, unlabeled string. Kept alongside the new keys, not instead of them.
  return {
    'anthropic': 'Anthropic API',
    'anthropic-compatible': 'Anthropic-compatible',
    'through_claude_code': 'Thru claude code',
    'direct': 'API connection',
    'ssh-proxy': 'SSH proxy',
    'proxy': 'Claude Code proxy',
    'ssh_proxy': 'SSH proxy',  // legacy provider value, kept for old rows
  }[kind] || kind || '';
}

function _machineLabel(machine) {
  const kind = backendKindLabel(machine.backend_kind);
  // Don't repeat yourself: a machine literally named "Anthropic API" would
  // otherwise render as "Anthropic API · Anthropic API".
  return kind && kind !== machine.name ? `${machine.name} · ${kind}` : machine.name;
}

/** Fill the backend picker with the owner's machines, selecting this chat's. */
function populateBackendPicker(chat) {
  const picker = byId('conversationBackend');
  if (!picker) return;
  const active = _machines.find(m => m.active);
  const follow = document.createElement('option');
  follow.value = '';
  // Naming the machine makes "Follow active" concrete rather than mysterious.
  follow.textContent = active ? `Follow active · ${active.name}` : 'Follow active';
  const options = [follow];
  _machines.forEach(machine => {
    const option = document.createElement('option');
    option.value = machine.id;
    option.textContent = _machineLabel(machine);
    options.push(option);
  });
  picker.replaceChildren(...options);
  // A pin to a machine that no longer exists falls back to following, which is
  // what the server does too.
  const pinned = chat?.ai_machine_id || '';
  picker.value = _machines.some(m => m.id === pinned) ? pinned : '';
}

async function setConversationRouting(fields, describe) {
  const chat = state.currentChat;
  if (!chat) return;
  try {
    const response = await apiFetch(`/api/chats/${encodeURIComponent(chat.id)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(fields),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || 'Could not update this conversation');
    }
    Object.assign(chat, fields);
    const stored = findChat(chat.id);
    if (stored) Object.assign(stored, fields);
    showToast(describe);
  } catch (error) {
    showToast(error.message, 'error');
    // Put the control back to the stored value rather than leaving it showing
    // a change that did not happen.
    populateBackendPicker(chat);
    populateModelPicker(chat);
  }
}

// ── Pending question from the linked terminal session ─────────────────────────
// A question asked in the terminal blocks that session until somebody chooses.
// Showing it here with every option, and delivering the choice, means the user
// does not have to go and find the terminal to unblock it.
// How often the sidebar re-reads which conversations are busy. A background
// turn has no other way to reach the dots: nothing streams to a page that is
// looking at a different conversation.
const CHAT_POLL_MS = 6000;
// Held and guarded like the other pollers. It was a bare setInterval inside the
// DOMContentLoaded block, which only failed to accumulate because that block
// happens to run once — a property of where the call sat, not of the code. Any
// future re-invocation of that setup would have silently doubled the poll with
// no handle left to stop it.
let _chatPollTimer = null;
const QUESTION_POLL_MS = 4000;
let _questionTimer = null;
let _questionState = null;
let _answering = false;

// ── Auto-answer: arm/disarm and the last-ten log ────────────────────────────
//
// Polled on its own timer rather than folded into refreshQuestion: the two are
// unrelated states (one is "is something waiting", the other is "is this chat
// allowed to answer for itself"), and a chat with the knob off should not pay
// for a question lookup it does not use.
const AUTO_ANSWER_POLL_MS = 5000;
let _autoAnswerTimer = null;
let _autoAnswerEnabled = false;
let _autoAnswerRecommend = false;
let _autoAnswerLog = [];

// The icon's three states, cycled in this order by one click each. 'off' and
// 'on' are the original two-state behaviour; 'recommend' is additive -- same
// icon, same click gesture, a further click out past 'on'. Modelled as one
// derived string rather than the two raw booleans everywhere else in this
// file, so the cycle and the rendering both have one place that enumerates
// the three states instead of four `if` branches each guessing which
// combination of (enabled, recommend) is reachable.
function _autoAnswerMode() {
  if (!_autoAnswerEnabled) return 'off';
  return _autoAnswerRecommend ? 'recommend' : 'on';
}

const _AUTO_ANSWER_NEXT_MODE = {off: 'on', on: 'recommend', recommend: 'off'};

// Bumped at the start of every call and compared after the fetch resolves.
// toggleAutoAnswer() awaits its own call to this function right after its
// PUT, but the 5s poll can have an older call already in flight when that
// happens -- with no guard, that older GET (issued before the click) landing
// after the newer one would overwrite the just-confirmed state with what the
// server had before it, flipping the icon back for up to one more poll
// interval. Comparing seq after every await this function makes is what
// lets a call recognise it is no longer the latest and stand down instead of
// rendering something stale over something current.
let _autoAnswerFetchSeq = 0;

async function refreshAutoAnswer() {
  const chat = state.currentChat;
  const toggle = byId('autoAnswerToggle');
  const info = byId('autoAnswerInfo');
  const seq = ++_autoAnswerFetchSeq;
  if (!chat || !chat.session_id) {
    // No session to answer on behalf of: the server-side watcher only polls
    // chats that carry one, so showing an armed-looking toggle here would be a
    // control that looks live and can never fire.
    if (toggle) toggle.hidden = true;
    if (info) info.hidden = true;
    closeAutoAnswerMenu();
    return;
  }
  let data;
  try {
    const response = await apiFetch(
      `/api/chats/${encodeURIComponent(chat.id)}/auto-answer`);
    if (seq !== _autoAnswerFetchSeq) return; // superseded while the fetch was in flight
    if (!response.ok) { if (toggle) toggle.hidden = true; if (info) info.hidden = true; return; }
    data = await response.json();
    if (seq !== _autoAnswerFetchSeq) return; // superseded while parsing the body
  } catch {
    if (seq !== _autoAnswerFetchSeq) return;
    if (toggle) toggle.hidden = true;
    if (info) info.hidden = true;
    return;
  }
  _autoAnswerEnabled = Boolean(data.enabled);
  _autoAnswerRecommend = Boolean(data.accept_recommended);
  _autoAnswerLog = Array.isArray(data.log) ? data.log : [];
  if (toggle) {
    toggle.hidden = false;
    const mode = _autoAnswerMode();
    toggle.dataset.mode = mode;
    toggle.setAttribute('aria-pressed', String(mode !== 'off'));
    // The glyph itself changes for 'recommend', not just its colour: this
    // state also judges which answer is best, not only whether to approve,
    // so it reads as a different capability rather than the same one in a
    // different shade.
    toggle.textContent = mode === 'recommend' ? '🧠' : '🤖';
    toggle.title = {
      off: 'Auto-approve permission prompts',
      on: 'Auto-approving permission prompts — click to also accept recommended answers',
      recommend: 'Auto-approving permission prompts and recommended answers — click to turn off',
    }[mode];
  }
  if (info) info.hidden = false;
  if (!byId('autoAnswerMenu')?.hidden) renderAutoAnswerMenu();
}

function startAutoAnswerPolling() {
  if (_autoAnswerTimer) return;
  // A hidden tab is not being read, and this is one of several pollers a
  // single open tab runs; skipping the fetch (not the timer) while hidden
  // costs nothing and the next visible tick catches up immediately.
  _autoAnswerTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshAutoAnswer();
  }, AUTO_ANSWER_POLL_MS);
  refreshAutoAnswer();
}

async function toggleAutoAnswer() {
  const chat = state.currentChat;
  if (!chat) return;
  const toggle = byId('autoAnswerToggle');
  const nextMode = _AUTO_ANSWER_NEXT_MODE[_autoAnswerMode()];
  const nextEnabled = nextMode !== 'off';
  const nextRecommend = nextMode === 'recommend';
  if (toggle) toggle.disabled = true;
  try {
    const response = await apiFetch(
      `/api/chats/${encodeURIComponent(chat.id)}/auto-answer`,
      {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: nextEnabled, accept_recommended: nextRecommend}),
      },
    );
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || 'Could not change auto-answer');
    }
    _autoAnswerEnabled = nextEnabled;
    _autoAnswerRecommend = nextRecommend;
    showToast({
      off: 'Auto-approve turned off',
      on: 'Auto-approving permission prompts for this conversation',
      recommend: 'Also accepting recommended answers to questions in this conversation',
    }[nextMode]);
    await refreshAutoAnswer();
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (toggle) toggle.disabled = false;
  }
}

// ── The last-ten popover ─────────────────────────────────────────────────────
// Mirrors #lastCommandMenu in conversation.js: outside click and Escape close
// it and return focus to the button that opened it.

function closeAutoAnswerMenu() {
  const menu = byId('autoAnswerMenu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  byId('autoAnswerInfo')?.setAttribute('aria-expanded', 'false');
  document.removeEventListener('click', _onDocumentClickForAutoAnswerMenu, true);
  // A row's tooltip has no meaning once the row it points at is gone.
  closeAutoAnswerTooltip();
}

function _onDocumentClickForAutoAnswerMenu(event) {
  const menu = byId('autoAnswerMenu');
  const info = byId('autoAnswerInfo');
  if (menu?.contains(event.target) || info?.contains(event.target)) return;
  closeAutoAnswerMenu();
}

function openAutoAnswerMenu() {
  const menu = byId('autoAnswerMenu');
  if (!menu) return;
  renderAutoAnswerMenu();
  menu.hidden = false;
  byId('autoAnswerInfo')?.setAttribute('aria-expanded', 'true');
  document.addEventListener('click', _onDocumentClickForAutoAnswerMenu, true);
  menu.focus();
}

function toggleAutoAnswerMenu() {
  if (byId('autoAnswerMenu')?.hidden) openAutoAnswerMenu();
  else closeAutoAnswerMenu();
}

function renderAutoAnswerMenu() {
  const menu = byId('autoAnswerMenu');
  if (!menu) return;
  // The 5s auto-answer poll calls this again while the menu is left open, and
  // replaceChildren below destroys whichever row the tooltip is anchored to.
  // Left open across that, the tooltip stayed on screen pointing at a
  // detached node and could never re-position, and the anchor identity check
  // in _toggleAutoAnswerTooltip could never match the freshly built row
  // again -- so a second click on the same-looking row reopened it instead
  // of closing it, for the rest of the menu's time open.
  closeAutoAnswerTooltip();
  // replaceChildren + createElement throughout: every row quotes a prompt read
  // off someone's terminal, and that text must never reach markup.
  menu.replaceChildren();
  const heading = document.createElement('h3');
  heading.textContent = 'Last auto-answers';
  menu.appendChild(heading);

  if (!_autoAnswerLog.length) {
    const empty = document.createElement('p');
    empty.className = 'auto-answer-empty';
    empty.textContent = 'No auto-answers yet.';
    menu.appendChild(empty);
    return;
  }

  _autoAnswerLog.forEach(entry => {
    const row = document.createElement('div');
    row.className = 'auto-answer-row';

    const head = document.createElement('div');
    head.className = 'auto-answer-row-head';

    const outcome = document.createElement('span');
    outcome.className = 'auto-answer-row-outcome ' + (entry.outcome || '');
    outcome.textContent = entry.outcome === 'answered'
      ? `Answered “${entry.label || ''}”`
      : 'Skipped';
    head.appendChild(outcome);

    const when = document.createElement('span');
    when.textContent = entry.at ? formatTime(entry.at) : '';
    head.appendChild(when);
    row.appendChild(head);

    const prompt = document.createElement('p');
    prompt.className = 'auto-answer-row-prompt';
    // A skip's reason is the more useful line: it says why nothing was
    // pressed, which is what a skip entry exists to explain.
    const full = entry.outcome === 'skipped'
      ? (entry.reason || '')
      : (entry.prompt || '');
    prompt.textContent = full;
    // CSS clips this to two lines; the full text is already the whole node's
    // textContent regardless, so the click handler has nothing further to
    // fetch -- it only has to stop reading it as clipped.
    prompt.tabIndex = 0;
    prompt.setAttribute('role', 'button');
    prompt.setAttribute('aria-label', 'Show the full text');
    prompt.addEventListener('click', event => {
      event.stopPropagation();
      _toggleAutoAnswerTooltip(prompt, full);
    });
    prompt.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      _toggleAutoAnswerTooltip(prompt, full);
    });
    row.appendChild(prompt);

    menu.appendChild(row);
  });
}

// ── Full-text tooltip for a clipped log row ─────────────────────────────────
// position:fixed and appended to <body>, not into #autoAnswerMenu: the menu
// clips its own content with overflow-y:auto so its own children scroll,
// which would also clip a tooltip nested inside it the moment the row it
// belongs to scrolls near an edge.

let _autoAnswerTooltipAnchor = null;

function _autoAnswerTooltipEl() {
  let el = byId('autoAnswerTooltip');
  if (!el) {
    el = document.createElement('div');
    el.id = 'autoAnswerTooltip';
    el.className = 'auto-answer-tooltip';
    el.setAttribute('role', 'tooltip');
    el.hidden = true;
    document.body.appendChild(el);
  }
  return el;
}

function closeAutoAnswerTooltip() {
  const el = byId('autoAnswerTooltip');
  if (!el || el.hidden) return;
  el.hidden = true;
  _autoAnswerTooltipAnchor = null;
  document.removeEventListener('click', _onDocumentClickForAutoAnswerTooltip, true);
}

function _onDocumentClickForAutoAnswerTooltip(event) {
  const el = byId('autoAnswerTooltip');
  if (el?.contains(event.target)) return;
  // Also leaves any row alone, current or not: this listener runs on the
  // capture phase, ahead of a row's own bubble-phase click handler, so
  // without this a click meant to close the open tooltip closed it here
  // first and then _toggleAutoAnswerTooltip's own anchor check -- now seeing
  // no anchor at all -- read that as "reopen" and undid the close in the same
  // click.
  if (event.target.closest?.('.auto-answer-row-prompt')) return;
  closeAutoAnswerTooltip();
}

function _toggleAutoAnswerTooltip(anchor, text) {
  // A second click on the same row closes it rather than re-showing it --
  // otherwise there is no way to dismiss it without clicking elsewhere first.
  if (_autoAnswerTooltipAnchor === anchor) {
    closeAutoAnswerTooltip();
    return;
  }
  const el = _autoAnswerTooltipEl();
  el.textContent = text;
  const width = Math.min(320, window.innerWidth - 16);
  el.style.width = `${width}px`;
  // Measured after unhiding rather than estimated from text length: a wrong
  // guess at height picks the wrong side to flip to, which is worse than not
  // flipping at all.
  el.hidden = false;
  _autoAnswerTooltipAnchor = anchor;
  const rect = anchor.getBoundingClientRect();
  const height = el.getBoundingClientRect().height;
  el.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
  // Flips above the row when there is not enough room below, the same escape
  // hatch stats.js's chart tooltip uses to stay inside the viewport.
  el.style.top = rect.bottom + height + 6 > window.innerHeight
    ? `${Math.max(8, rect.top - height - 6)}px`
    : `${rect.bottom + 6}px`;
  document.addEventListener('click', _onDocumentClickForAutoAnswerTooltip, true);
}

// Escape is handled by the single global keydown chain near the end of this
// file, not here -- a second listener on the menu itself would fire first on
// bubble, close the menu, and then the global handler's own now-stale "menu
// hidden?" check would fall through to closeSidebar() on the same keypress.

// Questions the user has declined. Without this the bar comes straight back:
// the poll re-reads the transcript every 4s, and a question that was cancelled
// rather than answered may still have no tool_result recorded against it, so
// `pending` stays true. Keyed by the tool_use id, which is unique per question,
// with the text as a fallback for a payload that somehow has no id -- scoped by
// chat either way, so a fallback key cannot silence another conversation.
const _dismissedQuestions = new Set();
// Questions where the escape was delivered and the prompt stayed open anyway.
// The control is not offered again for these, because the key was accepted: a
// second one is not a retry, it reaches whatever the session went on to do.
// Held in state rather than by leaving the button disabled -- the poll rebuilds
// this bar every 4s and would quietly hand back a control that must not be
// pressed, which is a lock that only looks like one.
const _escapeDelivered = new Set();
// This page stays open for days. A cap keeps a long session from accumulating
// keys forever; Sets iterate in insertion order, so the oldest goes first.
const DISMISSED_MAX = 200;

function _questionKey(data, chatId) {
  if (!data || !chatId) return '';
  const first = (data.questions || [])[0] || {};
  const identity = data.id || first.question || '';
  return identity ? `${chatId}:${identity}` : '';
}

function _remember(set, key) {
  if (!key) return;
  set.add(key);
  while (set.size > DISMISSED_MAX) {
    set.delete(set.values().next().value);
  }
}

function _clearQuestion() {
  _questionState = null;
  const bar = byId('questionBar');
  if (bar) bar.hidden = true;
}

function _renderQuestion(data) {
  const bar = byId('questionBar');
  if (!bar) return;
  // A poll landing mid-answer rebuilt the bar with every control enabled
  // again, so a terminal slower than the 4s poll could take a second click on
  // top of the first one still in flight. An interaction in flight owns the bar
  // until it settles.
  if (_answering) return;
  if (!data || !data.pending) { _clearQuestion(); return; }
  const chatId = state.currentChat ? state.currentChat.id : '';
  const key = _questionKey(data, chatId);
  if (_dismissedQuestions.has(key)) {
    _clearQuestion();
    return;
  }
  const first = (data.questions || [])[0] || {};
  byId('questionTag').textContent = first.header || 'Question';
  byId('questionAsk').textContent = first.question || 'A question is waiting';
  // Assigned for both branches below, unlike the answerable-only assignment it
  // replaces: declining is offered in both, so both need the payload the
  // dismiss handler keys on.
  _questionState = data;
  const blocked = _escapeDelivered.has(key);
  _renderDismiss(Boolean(data.answerable), blocked);

  // Descriptions come from the tool call; the option list comes from the live
  // terminal, which offers more than the call declared (free text, "Chat about
  // this"). Match them up by label so each button keeps its explanation.
  const described = new Map(
    (first.options || []).map(option => [option.label, option.description]),
  );
  const box = byId('questionOptions');
  const note = byId('questionNote');
  note.classList.remove('qo-error');

  if (!data.answerable) {
    box.replaceChildren();
    (first.options || []).forEach(option => {
      const shown = document.createElement('div');
      shown.className = 'question-option';
      shown.appendChild(_qoText('qo-label', option.label));
      if (option.description) shown.appendChild(_qoText('qo-desc', option.description));
      box.appendChild(shown);
    });
    note.textContent = data.reason || 'This one can only be answered at its terminal.';
    bar.hidden = false;
    return;
  }

  box.replaceChildren();
  (data.options || []).forEach(option => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'question-option';
    if (option.selected) button.classList.add('qo-current');
    button.appendChild(_qoText('qo-label', `${option.index}. ${option.label}`));
    const description = described.get(option.label);
    if (description) button.appendChild(_qoText('qo-desc', description));
    button.addEventListener('click', () => _answerQuestion(option));
    box.appendChild(button);
  });
  note.textContent = blocked
    ? 'Esc was delivered and the prompt stayed open — answer it here, or close '
      + 'it at the terminal.'
    : 'Choosing sends the answer to the terminal session.';
  bar.hidden = false;
}

/** Label the way out for what it can actually do in this state.
 *
 * Answerable, it presses Esc at the terminal and the session stops waiting.
 * Unanswerable, there is no channel to press anything through, so it hides the
 * bar here and the prompt stays open where it is -- a different act, and saying
 * "Don't answer" for both would promise the session was unblocked when it is
 * still sitting there.
 *
 * *blocked* is the third state: an escape that was accepted and changed
 * nothing. The control goes away rather than inviting a press that would land
 * somewhere else entirely.
 */
function _renderDismiss(answerable, blocked) {
  const button = byId('questionDismiss');
  if (!button) return;
  button.disabled = Boolean(blocked);
  button.textContent = answerable ? "Don't answer" : 'Hide';
  if (blocked) {
    button.title = 'Esc was already delivered and the prompt stayed open — '
      + 'close it at the terminal.';
  } else {
    button.title = answerable
      ? 'Closes the prompt at the terminal without choosing (sends Esc)'
      : 'Hides this here. The prompt stays open at its own terminal.';
  }
}

function _qoText(className, text) {
  const span = document.createElement('span');
  span.className = className;
  span.textContent = text;
  return span;
}

async function _answerQuestion(option) {
  const chat = state.currentChat;
  if (!chat || _answering) return;
  _answering = true;
  const buttons = [...document.querySelectorAll('.question-option')];
  buttons.forEach(button => { button.disabled = true; });
  // The way out goes with them: an Esc delivered while an answer is being
  // navigated would land between the arrow keys and the Enter.
  const dismiss = byId('questionDismiss');
  if (dismiss) dismiss.disabled = true;
  const note = byId('questionNote');
  note.classList.remove('qo-error');
  note.textContent = `Answering “${option.label}”…`;
  try {
    const response = await apiFetch(
      `/api/chats/${encodeURIComponent(chat.id)}/question`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index: option.index}),
      },
    );
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || 'Could not answer');
    }
    showToast(`Answered “${option.label}”`);
    _clearQuestion();
    await refreshQuestion();
  } catch (error) {
    note.textContent = error.message;
    note.classList.add('qo-error');
    buttons.forEach(button => { button.disabled = false; });
    if (dismiss) dismiss.disabled = false;
  } finally {
    _answering = false;
  }
}

/** Close the prompt without answering it.
 *
 * Two different acts behind one button, because the honest one depends on
 * whether the terminal can be reached at all -- see _renderDismiss.
 */
async function _dismissQuestion() {
  const chat = state.currentChat;
  const data = _questionState;
  if (!chat || !data || _answering) return;
  const key = _questionKey(data, chat.id);
  const button = byId('questionDismiss');
  const note = byId('questionNote');

  if (!data.answerable) {
    // Nothing to deliver, so nothing is claimed: hide it and say where the
    // prompt still is.
    _remember(_dismissedQuestions, key);
    _clearQuestion();
    showToast('Hidden here — still open at its terminal');
    return;
  }

  _answering = true;
  const options = [...document.querySelectorAll('.question-option')];
  button.disabled = true;
  options.forEach(option => { option.disabled = true; });
  note.classList.remove('qo-error');
  note.textContent = 'Closing the prompt without answering…';
  try {
    const response = await apiFetch(
      `/api/chats/${encodeURIComponent(chat.id)}/question`, {method: 'DELETE'});
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const failure = new Error(payload.error || 'Could not close the prompt');
      failure.delivered = payload.delivered === true;
      throw failure;
    }
    _remember(_dismissedQuestions, key);
    showToast('Left unanswered');
    _clearQuestion();
    await refreshQuestion();
  } catch (error) {
    note.textContent = error.message;
    note.classList.add('qo-error');
    // The options come back either way: if the prompt is still open, answering
    // it is still possible and is now the only thing that will unblock it.
    options.forEach(option => { option.disabled = false; });
    // The Esc does not. A key that never arrived is safe to send again; one
    // that arrived and left the prompt open is not a retry -- it would reach
    // whatever the session moved on to and interrupt that instead. Recorded
    // rather than merely left disabled, because the next poll re-renders.
    if (error.delivered) {
      _remember(_escapeDelivered, key);
      note.textContent += ' Nothing further is sent from here — close it at the terminal.';
    } else {
      button.disabled = false;
    }
  } finally {
    _answering = false;
  }
}

async function refreshQuestion() {
  const chat = state.currentChat;
  if (!chat) { _clearQuestion(); return; }
  try {
    const response = await apiFetch(
      `/api/chats/${encodeURIComponent(chat.id)}/question`);
    if (!response.ok) { _clearQuestion(); return; }
    _renderQuestion(await response.json());
  } catch {
    _clearQuestion();
  }
}

function startQuestionPolling() {
  if (_questionTimer) return;
  // Same reasoning as startAutoAnswerPolling: a hidden tab skips the fetch,
  // not the timer, so it is caught up the moment the tab is visible again.
  _questionTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshQuestion();
  }, QUESTION_POLL_MS);
  refreshQuestion();
}

async function selectChat(id) {
  const chat = findChat(id);
  if (!chat || chat.archived) return;
  closeSidebar();
  closeSupervisorPane();
  try {
    await conversationController.selectChat(chat);
  } catch (error) {
    showToast(error.message, 'error');
  }
  startTranscriptSync();
  markAgentSeen('chat', chat.id);
  if (chat.session_id) markAgentSeen('session', chat.session_id);
}

// ── Live transcript sync ────────────────────────────────────────────────────────────

// A chat linked to a CLI session keeps moving in the terminal after it is
// opened here. Polling the sync endpoint keeps the two views level; the server
// reads from a stored byte offset, so a poll on a 20 MB transcript costs
// nothing once it has caught up.
const SYNC_INTERVAL_MS = 5000;
// Mirrors ACTIVE_STATES in conversation.js, which is module-private. Kept here
// rather than exported so the sync does not reach into the renderer's internals.
const SYNC_BUSY_STATES = new Set(['connecting', 'thinking', 'retrying', 'responding']);
let _syncTimer = null;
let _syncing = false;

function stopTranscriptSync() {
  if (_syncTimer) {
    clearInterval(_syncTimer);
    _syncTimer = null;
  }
}

function startTranscriptSync() {
  stopTranscriptSync();
  // Only linked chats have a transcript to follow; polling anything else would
  // be a request every five seconds that can never return a message.
  if (!state.currentChat?.session_id) return;
  _syncTimer = setInterval(() => {
    // A hidden tab is not watching this conversation update live; the next
    // visible tick, or the reload that opening the tab back up triggers,
    // catches it up.
    if (document.visibilityState === 'visible') syncTranscript();
  }, SYNC_INTERVAL_MS);
}

// How often every OTHER conversation is followed. The five-second sync above
// only ever covers the conversation on screen, so a chat whose terminal was
// busy kept its old updated_at and sat in the sidebar looking idle until you
// opened it -- the list was being refreshed faithfully, the rows behind it
// were stale. Slower than the open conversation on purpose: this is about a
// list being honest, not about watching a reply arrive.
const SYNC_ALL_MS = 30000;
let _syncAllTimer = null;
let _syncingAll = false;

function stopBackgroundSync() {
  if (_syncAllTimer) {
    clearInterval(_syncAllTimer);
    _syncAllTimer = null;
  }
}

/** Follow every linked conversation, and redraw only if something moved. */
async function syncAllConversations() {
  // A slow sweep must not stack up behind itself: on a machine with many
  // linked conversations one pass can outlast the interval.
  if (_syncingAll) return 0;
  _syncingAll = true;
  try {
    const response = await apiFetch('/api/chats/sync', {method: 'POST'});
    // apiFetch resolves for 4xx/5xx, so an unchecked response would make a
    // failed sweep look like a quiet one.
    if (!response.ok) return 0;
    const data = await response.json();
    const changed = Object.keys(data.changed || {});
    if (!changed.length) return 0;
    // The rows moved underneath the list, so re-read it rather than guessing
    // what the new order is.
    await refreshChats();
    // If the conversation on screen was one of them, pull its new turns in
    // too -- otherwise the sidebar would show activity the page does not.
    if (state.currentChat && changed.includes(state.currentChat.id)) {
      await syncTranscript();
    }
    return changed.length;
  } catch {
    // Offline or a dropped request: the next sweep is thirty seconds away and
    // nothing here is worth interrupting the user for.
    return 0;
  } finally {
    _syncingAll = false;
  }
}

function startBackgroundSync() {
  stopBackgroundSync();
  _syncAllTimer = setInterval(() => {
    // Same reasoning as startTranscriptSync: nobody is reading a hidden
    // tab's sidebar, so the sweep waits for the tab to be visible again.
    if (document.visibilityState === 'visible') syncAllConversations();
  }, SYNC_ALL_MS);
}

async function syncTranscript({announce = false} = {}) {
  const chatId = state.currentChat?.id;
  if (!chatId || !state.currentChat?.session_id) {
    if (announce) showToast('This conversation is not linked to a CLI session');
    return 0;
  }
  // A slow poll must not stack on the next tick, and must never fire while a
  // turn is streaming -- the reply would be replaced mid-render.
  if (_syncing || SYNC_BUSY_STATES.has(state.streamState)) return 0;
  _syncing = true;
  try {
    const response = await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/sync`, {method: 'POST'});
    if (!response.ok) throw new Error('Could not refresh history');
    const data = await response.json();
    const count = (data.messages || []).length;
    if (count) await conversationController.refreshCurrent();
    if (announce) {
      showToast(count ? `${count} new message${count === 1 ? '' : 's'}` : 'Already up to date');
    }
    return count;
  } catch (error) {
    if (announce) showToast(error.message, 'error');
    return 0;
  } finally {
    _syncing = false;
  }
}

function showWelcome() {
  conversationController?.persistDraft();
  // No chat is open, so nothing to follow. Left running, the timer would poll
  // a chat the user has already navigated away from.
  stopTranscriptSync();
  state.currentChat = null;
  byId('topbarTitle').textContent = 'WebConsole';
  byId('workspaceName').textContent = '';
  byId('composerChatName').textContent = '';
  byId('workspaceStrip').style.display = 'none';
  const lastBar = byId('lastCommandBar');
  if (lastBar) lastBar.hidden = true;
  byId('editChatBtn').hidden = true;
  byId('syncBtn').hidden = true;
  byId('autoAnswerToggle').hidden = true;
  byId('autoAnswerInfo').hidden = true;
  closeAutoAnswerMenu();
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
    } else if (action === 'reset-order') {
      // An empty order clears every placement and returns the list to recency.
      const response = await apiFetch('/api/chats/order', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order: []}),
      });
      if (!response.ok) throw new Error('Could not reset the order');
      await refreshChats();
      showToast('List order reset');
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
      if (data.webconsole_url) byId('webconsoleUrl').value = data.webconsole_url;
      if (data.debug_console !== undefined) {
        const el = byId('debugConsole');
        if (el) el.setAttribute('aria-pressed', String(data.debug_console));
      }
      // The model dropdown is repopulated inside renderVoiceSettingsFields
      // from the chosen backend's own `models`, so this callback no longer
      // touches it. It used to call a populateModelOptions that is private
      // to voice-settings.js and was never imported here, so changing the
      // voice backend raised "populateModelOptions is not defined" and the
      // list never updated -- and the re-GET it did first could not have
      // helped anyway, since /api/settings derives the options from the
      // stored backend id rather than the newly selected one.
      renderVoiceSettingsFields(data, () => {
        populateModelPicker();
      });
      // The global default is only a fallback for when no backend is active;
      // it is still worth offering in the picker.
      _modelOptions = [data.default_model];
      populateModelPicker();
      return data;
    }
    // A non-2xx is a failure too, and returning {} for it is what makes the
    // settings panel show defaults that look like the server's answer.
    console.error('loadSettings: /api/settings returned', response.status);
  } catch (error) {
    // rules.md §12: a fetch error either surfaces or is logged, never both
    // swallowed and defaulted. No toast, because this runs on load and a
    // banner on every page open would be worse than a console line -- but the
    // silence itself was the bug: {} is indistinguishable from a server that
    // genuinely has nothing configured.
    console.error('loadSettings failed', error);
  }
  return {};
}

// Session heartbeat: call POST /api/ping every 5 minutes to keep the
// cookie alive. Warn the user 3 minutes before expiry so they can
// save their work before being redirected.
let _pingTimer = null;
const PING_INTERVAL = 5 * 60 * 1000;  // 5 min
const WARN_THRESHOLD = 3 * 60 * 1000;  // 3 min remaining

async function _pingLoop() {
  try {
    const resp = await apiFetch('/api/ping', { method: 'POST' });
    if (resp.ok) {
      const data = await resp.json();
      const remaining = data.session_ttl_remaining || 0;
      if (remaining < 180) {
        // < 3 min: show a banner
        _showExpiryWarning(remaining);
      } else if (_expiryBanner && _expiryBanner.style.display !== 'none') {
        // Re-appeared above threshold: hide banner
        _expiryBanner.style.display = 'none';
      }
    }
  } catch {
    /* ping failure is non-fatal — session may just be expired */
  }
}

let _expiryBanner = null;

function _showExpiryWarning(remaining) {
  if (!_expiryBanner) {
    _expiryBanner = document.createElement('div');
    _expiryBanner.id = 'sessionWarningBanner';
    _expiryBanner.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b91c1c;color:#fff;text-align:center;padding:8px;z-index:9999;font-size:14px;cursor:pointer;';
    _expiryBanner.textContent = `Session expires in ${remaining}s — click or wait to stay.`;
    _expiryBanner.addEventListener('click', async () => {
      try {
        await apiFetch('/api/ping', { method: 'POST' });
        _expiryBanner.style.display = 'none';
      } catch { /* ignore */ }
    });
    document.body.appendChild(_expiryBanner);
  }
  _expiryBanner.textContent = `Session expires in ${remaining}s — click to stay.`;
  _expiryBanner.style.display = 'block';
}

function _startPingLoop() {
  _pingTimer = setInterval(_pingLoop, PING_INTERVAL);
}

function _stopPingLoop() {
  if (_pingTimer) {
    clearInterval(_pingTimer);
    _pingTimer = null;
  }
}

function populateModelPicker(chat = state.currentChat) {
  const picker = byId('conversationModel');
  if (!picker) return;
  // A conversation pinned to a backend must be offered THAT backend's models,
  // not the active one's: offering the active machine's list would put ids in
  // the picker the pinned backend has never served.
  const pinnedId = chat?.ai_machine_id || '';
  const pinnedEntry = pinnedId ? _modelsByMachine.get(pinnedId) : null;
  const current = chat?.model || '';
  // Offer the configured default and fallback first, then whatever the active
  // machine actually serves. This used to read a hardcoded datalist out of the
  // DOM and, worse, every model id harvested from old transcripts -- so it
  // offered models the current backend has never served and the turn failed.
  // Only the models this backend is set to offer. An empty active list means
  // every served model is offered, so the feature stays opt-in.
  const source = pinnedEntry || _modelsSource;
  const active = source?.active || [];
  const catalogue = pinnedEntry ? (pinnedEntry.models || []) : _servedModels;
  const served = catalogue
    .map(model => model.id)
    .filter(id => !active.length || active.includes(id));
  // The global default is only the fallback for when no backend is active.
  // Offering it alongside a backend's own list put a model in the picker that
  // the active backend had been told not to offer -- verified in a browser:
  // untick claude-sonnet-5 and it stayed selectable because it happened to be
  // the global default.
  const globals = served.length ? [] : _modelOptions;
  // Keep whatever this chat already uses, so a model that was later
  // deactivated stays selectable rather than silently becoming Automatic --
  // hiding a model must never break a conversation already using it.
  const chatModel = chat?.model;
  const models = [...globals, ...served, chatModel, current];

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

/** Load a pinned backend's model list on demand, then refresh the picker. */
async function ensurePinnedModels(chat) {
  const pinned = chat?.ai_machine_id;
  if (!pinned || _modelsByMachine.has(pinned)) return;
  await loadModelsFor(pinned);
  if (state.currentChat?.id === chat.id) populateModelPicker(chat);
}

// Open the Backends tab: machines first so the cards exist, then the models
// each one serves. Only Anthropic-protocol backends publish a list.
async function loadBackends() {
  await loadMachines();
  await loadTransports();
  await loadTurnCounts();
  _renderMachineList();
  await Promise.all(
    _machines
      .filter(machine => machine.provider === 'claude_code')
      .map(machine => loadModelsFor(machine.id)),
  );
}

// Traffic is what makes the map worth reading: without it the panel says where
// a turn will go but never where turns have gone, which is the comparison that
// exposes a default nobody actually uses.
// Same helper as machines.js's own _bareModel -- duplicated rather than
// imported, since it is pure string manipulation with no state to keep in
// sync. Its absence here previously threw a ReferenceError on the first loop
// iteration below, silently caught by loadTurnCounts's own catch block, which
// reset _turnsByModel to empty on every call -- so the Turns column always
// read "never" regardless of how much traffic a model actually had.
function _bareModel(id) {
  return id.slice(id.lastIndexOf('/') + 1);
}

async function loadTurnCounts() {
  try {
    const response = await apiFetch('/api/usage?days=30');
    if (!response.ok) return;
    const data = await response.json();
    // Grouped by the bare model name, summing. Two things make that necessary:
    // the same model appears once per provider, so a Map built straight from
    // the rows drops all but the last; and a turn run here records the id the
    // backend serves ("azure_ai/gpt-5.6-sol") while one imported from a
    // terminal transcript records what the CLI reported ("gpt-5.6-sol").
    // Keyed on the full id, most of the map read "never" beside models with
    // thousands of turns.
    _turnsByModel = new Map();
    for (const row of data.totals || []) {
      const key = _bareModel(row.model || '');
      _turnsByModel.set(key, (_turnsByModel.get(key) || 0) + (Number(row.requests) || 0));
    }
  } catch {
    // Traffic is an enrichment; a backend list without it is still usable.
    _turnsByModel = new Map();
  }
}

export async function loadModelsFor(machineId, force = false) {
  // The guard covers the fetch, which is the only expensive part. Callers that
  // need the derived state refreshed on a cache hit -- _activateMachine, where
  // the models are already loaded but which machine is active has changed --
  // call _refreshServedModels() themselves.
  if (!force && _modelsByMachine.has(machineId)) return;
  try {
    const url = `/api/models?machine_id=${encodeURIComponent(machineId)}${force ? '&force=1' : ''}`;
    const response = await apiFetch(url);
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
  } catch (err) {
    // Session expired redirects to /login (apiFetch). Do not overwrite
    // the in-memory state so the existing content or the cached "Loading
    // models…" stays on screen rather than vanishing on redirect.
    if (err instanceof Error && err.message === 'Session expired') return;
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
export function _refreshServedModels() {
  const active = _machines.find(machine => machine.active);
  const entry = active ? _modelsByMachine.get(active.id) : null;
  _servedModels = entry ? entry.models : [];
  _modelsSource = entry;
  _syncModelSuggestions();
  populateModelPicker();
}

export async function _toggleModelOffered(machine, modelId) {
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

export async function _setModelDefault(machine, modelId) {
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
    const runState = byId('runState');
    if (runState) { runState.dataset.state = 'loading'; runState.textContent = 'Loading…'; }
    const settings = await loadSettings();
    await refreshChats();
    await refreshSessions();
    await loadMachines();
    // Only the active backend's models are needed to fill the picker at boot;
    // the rest load when the Backends tab is opened.
    const active = _machines.find(machine => machine.active);
    if (active && active.provider === 'claude_code') await loadModelsFor(active.id);
    await refreshSupervisor();
    startSupervisorPolling();
    const lastId = storageGet('wc_last_chat');
    const last = findChat(lastId);
    if (last && !last.archived) await selectChat(last.id);
    else showWelcome();
  } catch (error) {
    if (error.message !== 'Session expired') showToast(error.message, 'error');
  } finally {
    const runState = byId('runState');
    if (runState) { runState.dataset.state = 'ready'; runState.textContent = 'Ready'; }
  }
}

async function logout() {
  try {
    await fetch('/logout', {method: 'POST', credentials: 'same-origin'});
  } catch (error) {
    // Navigating away regardless is right -- the user asked to leave. Logging
    // is not optional though: if this call never lands the session is still
    // live on the server while the UI has said "logged out", and swallowing it
    // leaves no trace of the one failure that matters here.
    console.error('logout request failed; the server session may still be live',
                  error);
  }
  window.location.assign('/login');
}

document.addEventListener('DOMContentLoaded', () => {
  applySavedTheme();
  byId('themeToggle').addEventListener('click', toggleTheme);
  byId('alertToggle')?.addEventListener('click', toggleAlerts);
  _syncAlertToggle();
  byId('menuBtn').addEventListener('click', openSidebar);
  byId('sidebarCloseBtn').addEventListener('click', closeSidebar);
  byId('sidebarOverlay').addEventListener('click', closeSidebar);
  byId('logoutBtn').addEventListener('click', logout);
  byId('editChatBtn').addEventListener('click', () => openChatDialog('edit'));
  byId('syncBtn').addEventListener('click', () => syncTranscript({announce: true}));
  byId('autoAnswerToggle').addEventListener('click', toggleAutoAnswer);
  byId('autoAnswerInfo').addEventListener('click', toggleAutoAnswerMenu);
  byId('questionDismiss')?.addEventListener('click', _dismissQuestion);
  byId('settingsBtn').addEventListener('click', openSettingsDialog);
  byId('orchestratorBtn')?.addEventListener('click', openSupervisorPane);
  byId('orchestratorPaneTitle')?.addEventListener('click', openSupervisorPane);
  byId('orchestratorPaneClose')?.addEventListener('click', closeSupervisorPane);
  byId('supervisorMapBtn')?.addEventListener('click', _openMap);
  byId('supervisorMapClose')?.addEventListener('click', _closeMap);
  byId('settingsCancel').addEventListener('click', closeSettingsDialog);
  byId('settingsForm').addEventListener('submit', saveSettings);
  byId('settingsDialog').addEventListener('click', event => { if (event.target === byId('settingsDialog')) closeSettingsDialog(); });
  byId('settingsSave').addEventListener('click', saveSettings);
  byId('debugConsole')?.addEventListener('click', () => {
    const el = byId('debugConsole');
    const on = el.getAttribute('aria-pressed') === 'true';
    el.setAttribute('aria-pressed', String(!on));
  });
  byId('conversationModel')?.addEventListener('change', event => {
    const model = event.target.value || null;
    setConversationRouting(
      {model},
      model ? `This conversation will use ${model}` : 'Model set to automatic',
    );
  });
  byId('conversationBackend')?.addEventListener('change', async event => {
    const machineId = event.target.value || null;
    const machine = _machines.find(m => m.id === machineId);
    await setConversationRouting(
      {ai_machine_id: machineId},
      machine
        ? `This conversation will use ${machine.name}`
        : 'This conversation follows the active backend',
    );
    // The new backend serves a different catalogue, so the model list has to
    // follow. Clear a pinned model the new backend does not serve rather than
    // sending it an id it will reject.
    const chat = state.currentChat;
    if (!chat) return;
    await ensurePinnedModels(chat);
    const entry = machineId ? _modelsByMachine.get(machineId) : _modelsSource;
    const offered = (entry?.models || _servedModels).map(m => m.id);
    if (chat.model && offered.length && !offered.includes(chat.model)) {
      await setConversationRouting(
        {model: null},
        `${machine ? machine.name : 'This backend'} does not serve ${chat.model}; model set to automatic`,
      );
    }
    populateModelPicker(chat);
  });
  // Measured geometry goes stale on resize; redraw rather than leave a wire
  // pointing at where a card used to be.
  window.addEventListener('resize', () => {
    if (settingsVisible && _currentTab === 'backends') _drawMapWires();
  });
  byId('addMachineBtn').addEventListener('click', _showAddMachine);
  byId('cancelMachine').addEventListener('click', () => { byId('machineForm').hidden = true; byId('addMachineBtn').hidden = false; _machineEditing = null; });
  byId('saveMachine').addEventListener('click', _saveMachine);
  byId('machineProvider').addEventListener('change', _syncMachineProviderFields);
  byId('addTransportBtn').addEventListener('click', _showAddTransport);
  byId('cancelTransport').addEventListener('click', _cancelTransportForm);
  byId('testTransport').addEventListener('click', _testTransportForm);
  byId('saveTransport').addEventListener('click', () => _saveTransport(_renderMachineList));
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
  // Both controls refetch: the server does the bucketing, so the client has no
  // finer data lying around to re-slice.
  byId('statsRange')?.addEventListener('change', event => {
    // Pick the slot width the new range can actually show evolution at. A day
    // grouped by day is a single point, and 24 hourly points hide the shape of
    // a busy afternoon; 48 half-hours show it. Only ever adjusted on a range
    // change, so an explicit choice of grouping is never overridden.
    const bucketSelect = byId('statsBucket');
    const suggested = {'1': 'halfhour', '7': 'hour', '30': 'day', 'all': 'month'};
    const next = suggested[event.target.value];
    if (bucketSelect && next) bucketSelect.value = next;
    loadStats(true);
  });
  byId('statsBucket')?.addEventListener('change', () => loadStats(true));
  byId('serverRange')?.addEventListener('change', event => {
    // Same reasoning as the statistics range above: match the slot width to
    // the span, or a day of samples collapses into one point.
    const bucketSelect = byId('serverBucket');
    const suggested = {'1': 'halfhour', '7': 'hour', '30': 'day'};
    const next = suggested[event.target.value];
    if (bucketSelect && next) bucketSelect.value = next;
    loadServer();
  });
  byId('serverBucket')?.addEventListener('change', () => loadServer());
  byId('skillSearch')?.addEventListener('input', event => {
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
    onClearSupervisor: clearSupervisor,
    onDismissAgent: dismissAgent,
    // Same destination as the topbar control, reachable from the section it
    // belongs to. Opens in the conversation area rather than navigating away.
    onOpenSupervisor: openSupervisorPane,
    onAddToSupervisor: openSupervisorPicker,
    // One drag is one write: the server takes the whole ordered section and
    // applies it in a transaction, so a drop cannot half-apply.
    onReorder: async ids => {
      try {
        const response = await apiFetch('/api/chats/order', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({order: ids}),
        });
        // apiFetch resolves for 4xx/5xx too, so without this check a rejected
        // save looked identical to a successful one.
        if (!response.ok) throw new Error('Save rejected');
        // state.chats still holds the old order; any later render would redraw
        // from it and visibly undo the move even though the server saved it.
        await refreshChats();
      } catch {
        showToast('Could not save the new order', 'error');
        await refreshChats();
      }
    },
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

  // The sidebar dots come from GET /api/chats now, not from #runState. That
  // attribute describes the conversation on screen, which was the same thing
  // only while a turn could not outlive its viewer: with background turns it
  // would clear every other conversation's dot the moment this one settled.
  // A poll keeps the dots honest for turns nobody is watching.
  if (!_chatPollTimer) {
    _chatPollTimer = setInterval(() => {
      // Same reasoning as the other pollers below: a hidden tab's sidebar
      // dots are not being looked at, so the fetch waits for visibility.
      if (document.visibilityState === 'visible') refreshChats().catch(() => {});
    }, CHAT_POLL_MS);
  }

  // That poll re-reads the list; this one makes the list worth re-reading, by
  // following the conversations nobody is looking at. Guarded the same way,
  // and started once with an immediate first pass so a page opened after a
  // long absence does not show a stale list for its first half minute.
  if (!_syncAllTimer) {
    startBackgroundSync();
    syncAllConversations();
  }

  conversationController = createConversationController({
    state,
    elements: {
      messages: byId('messagesArea'), composerInput: byId('composerInput'),
      modelPicker: byId('conversationModel'),
      sendButton: byId('sendBtn'), retryButton: byId('retryBtn'),
      jumpButton: byId('jumpToLatest'), runState: byId('runState'),
      composerStatus: byId('composerStatus'),
      queueBar: byId('queueBar'), queueList: byId('queueList'),
      queueTag: byId('queueTag'), queueNote: byId('queueNote'),
      queueClose: byId('queueClose'), queueToggle: byId('queueToggle'),
      queueToggleTop: byId('queueToggleTop'),
      queueBackdrop: byId('queueBackdrop'),
      queueBackdrop: byId('queueBackdrop'),
      lastCommandBar: byId('lastCommandBar'),
      lastCommandText: byId('lastCommandText'),
      lastCommandWhen: byId('lastCommandWhen'),
      lastCommandGlyph: byId('lastCommandGlyph'),
      lastCommandMenu: byId('lastCommandMenu'),
    },
    apiFetch, storageGet, storageSet, storageRemove, showToast,
    onChatLoaded: updateCurrentUi,
    refreshChats,
    onPromptSent: clearEndedFlag,
  });

  document.querySelectorAll('.new-chat-btn').forEach(button => button.addEventListener('click', () => openChatDialog('create')));
  document.querySelectorAll('.voice-new-chat-btn').forEach(button => button.addEventListener('click', () => openChatDialog('create', undefined, {voiceMode: true})));

  document.addEventListener('keydown', event => {
    trapDialogFocus(event);
    if (event.key === 'Escape') {
      // Checked first because it is the only dialog that can sit over another,
      // being opened from a row menu rather than the topbar.
      if (document.getElementById('supervisorPickDialog')) _closeSupervisorPicker();
      else if (byId('settingsDialog').classList.contains('open')) closeSettingsDialog();
      else if (byId('chatDialog').classList.contains('open')) closeDialog();
      else if (!byId('autoAnswerTooltip')?.hidden) closeAutoAnswerTooltip();
      else if (!byId('autoAnswerMenu')?.hidden) {
        closeAutoAnswerMenu();
        byId('autoAnswerInfo')?.focus();
      }
      else if (_changelogPopoverEl) _changelogPopoverEl.hidePopover();
      else closeSidebar();
    }
  });

  // Version click — open changelog popover
  var verEl = byId('ver');
  if (verEl) {
    verEl.addEventListener('click', function(e) { e.stopPropagation(); toggleChangelogPopover(verEl); });
    verEl.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleChangelogPopover(verEl); }
    });
  }

  loadInitialData();
  // Start session heartbeat after everything is loaded.
  _startPingLoop();
});

// ── Changelog popover ────────────────────────────────────────────────────
var _changelogData = null;
var _changelogPopoverEl = null;

function toggleChangelogPopover(anchorEl) {
  if (_changelogPopoverEl) { _changelogPopoverEl.hidePopover(); return; }
  if (!_changelogData) {
    apiFetch('/api/changelog')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        _changelogData = Array.isArray(data) ? data : [];
        _showChangelogPopover(anchorEl);
      })
      .catch(function() { /* silent */ });
  } else {
    _showChangelogPopover(anchorEl);
  }
}

function _showChangelogPopover(anchorEl) {
  if (!_changelogData) return;

  if (_changelogPopoverEl) {
    _changelogPopoverEl.remove();
    _changelogPopoverEl = null;
  }

  var dialog = document.createElement('div');
  dialog.className = 'changelog-popover';
  dialog.setAttribute('popover', 'manual');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-label', 'Changelog');

  var heading = document.createElement('h3');
  heading.textContent = 'Changelog';
  dialog.appendChild(heading);

  _changelogData.forEach(function(entry) {
    var chapter = document.createElement('div');
    chapter.className = 'changelog-chapter';

    var top = document.createElement('div');
    top.style.cssText = 'margin-bottom:6px';
    var verSpan = document.createElement('span');
    verSpan.className = 'cl-ver';
    verSpan.textContent = entry.version;
    var dateSpan = document.createElement('span');
    dateSpan.className = 'cl-date';
    dateSpan.textContent = entry.date;
    top.append(verSpan, dateSpan);
    chapter.appendChild(top);

    entry.sections.forEach(function(sec) {
      var secHead = document.createElement('div');
      secHead.className = 'cl-section';
      secHead.textContent = sec.type;
      chapter.appendChild(secHead);

      var ul = document.createElement('ul');
      ul.className = 'cl-items';
      sec.items.forEach(function(item) {
        var li = document.createElement('li');
        li.textContent = item;
        ul.appendChild(li);
      });
      chapter.appendChild(ul);
    });

    dialog.appendChild(chapter);
  });

  // Click outside to close
  var backdrop = document.createElement('div');
  backdrop.className = 'changelog-changelog-backdrop';
  backdrop.addEventListener('click', function() { dialog.hidePopover(); });
  document.body.appendChild(backdrop);

  // Open and clean up
  document.body.appendChild(dialog);
  dialog.showPopover();
  _changelogPopoverEl = dialog;

  // Remove backdrop on close
  var checkClose = setInterval(function() {
    if (!document.body.contains(backdrop)) {
      clearInterval(checkClose);
      return;
    }
    if (!dialog.getPopoverState() || dialog.getPopoverState() === 'closed') {
      dialog.remove();
      backdrop.remove();
      clearInterval(checkClose);
      _changelogPopoverEl = null;
    }
  }, 200);
}
