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
    } else if (action === 'archive' || action === 'restore') {
      const archived = action === 'archive';
      await patchChat(chat, {archived}, archived ? 'Conversation archived' : 'Conversation restored');
      if (archived && state.currentChat?.id === id) {
        storageRemove('wc_last_chat');
        showWelcome();
      }
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

async function loadInitialData() {
  try {
    await refreshChats();
    const response = await apiFetch('/api/sessions');
    if (response.ok) {
      const sessions = (await response.json()).sessions || [];
      listController.setCliSessions(sessions.filter(item => !item.webchat));
      listController.render(state.chats, null);
    }
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
  });

  conversationController = createConversationController({
    state,
    elements: {
      messages: byId('messagesArea'), composerInput: byId('composerInput'),
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
      if (byId('chatDialog').classList.contains('open')) closeDialog();
      else closeSidebar();
    }
  });

  loadInitialData();
});
