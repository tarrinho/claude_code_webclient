// ── Supervisor ────────────────────────────────────────────────────────────────

import {apiFetch} from './api.js?v=1';
import {state, showToast} from './app.js?v=30';

// This file is loaded two ways at once: as its own <script type="module"> in
// index.html, and via `import` from app.js for the pane/picker functions it
// needs directly. Neither loading path shares app.js's own `const byId`
// (ES modules do not share top-level scope across files), so it needs its own.
const byId = id => document.getElementById(id);

// ── Supervisor pane ───────────────────────────────────────────────────────────
// The supervisor is a full page of its own. Framing it in the conversation area
// rather than navigating to it keeps the sidebar in view -- which is the point,
// since that is where you see who is waiting -- and leaving does not cost a
// reload of the whole console.
// What the conversation area looked like before the pane took over.
let _paneReturn = {messages: '', composer: ''};

// ── Adding a conversation to a supervisor ────────────────────────────────────
// Built in JS rather than as markup in index.html. It reuses the same
// .dialog-backdrop / .dialog classes as the other dialogs so it looks and
// behaves identically, but four sessions are editing that file at once and a
// dialog nobody else needs is not worth a conflict in it.

export function _closeSupervisorPicker() {
  document.getElementById('supervisorPickDialog')?.remove();
  if (state.previousFocus && document.body.contains(state.previousFocus)) state.previousFocus.focus();
}

async function _addChatToSupervisor(supervisorId, chatId, title) {
  try {
    const response = await apiFetch(
      `/api/supervisors/${encodeURIComponent(supervisorId)}/members`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        // kind is sent even though every member ends up a chat: the same
        // endpoint takes 'session' from the supervisor's own picker, and the
        // server is what decides how to resolve it.
        body: JSON.stringify({members: [{kind: 'chat', ref_id: chatId}]}),
      },
    );
    // apiFetch resolves for 4xx as well as 2xx, so a rejected add would
    // otherwise report success and silently do nothing -- which is exactly how
    // conversation reordering appeared to save and did not.
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || data.detail || 'Could not add to the supervisor');
    }
    const result = await response.json();
    if (result.added?.length) showToast(`Added to ${title}`);
    else if (result.already_members?.length) showToast(`Already in ${title}`);
    else showToast(result.failed?.[0]?.error || 'Nothing was added', 'error');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    _closeSupervisorPicker();
  }
}

export async function openSupervisorPicker(chatId) {
  state.previousFocus = document.activeElement;
  _closeSupervisorPicker();

  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop open';
  backdrop.id = 'supervisorPickDialog';
  backdrop.setAttribute('role', 'dialog');
  backdrop.setAttribute('aria-modal', 'true');
  backdrop.setAttribute('aria-label', 'Add this conversation to a supervisor');

  const panel = document.createElement('div');
  panel.className = 'dialog';
  const heading = document.createElement('h2');
  heading.textContent = 'Add to supervisor';
  const help = document.createElement('p');
  help.textContent = 'Loading supervisors…';
  panel.append(heading, help);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);

  // Clicking the backdrop closes, matching every other dialog here. The check
  // keeps a click inside the panel from closing it.
  backdrop.addEventListener('click', event => {
    if (event.target === backdrop) _closeSupervisorPicker();
  });

  let supervisors = [];
  try {
    const response = await apiFetch('/api/supervisors');
    if (!response.ok) throw new Error('Could not load supervisors');
    supervisors = (await response.json()).supervisors || [];
  } catch (error) {
    help.textContent = error.message;
    return;
  }

  if (!supervisors.length) {
    // Says what to do next rather than presenting an empty box, which reads
    // as a failure when it is simply the first run.
    help.textContent = 'No supervisors yet. Create one on the supervisor page first.';
    return;
  }

  help.textContent = 'Pick the supervisor that should watch this conversation.';
  const list = document.createElement('div');
  list.className = 'chat-menu open';
  list.style.position = 'static';
  supervisors.forEach(supervisor => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = supervisor.title || 'Untitled supervisor';
    button.setAttribute('role', 'menuitem');
    button.addEventListener('click', () => _addChatToSupervisor(
      supervisor.id, chatId, supervisor.title || 'the supervisor'));
    list.appendChild(button);
  });
  panel.appendChild(list);
  list.querySelector('button')?.focus();
}

export function openSupervisorPane() {
  const pane = byId('supervisorPane');
  const frame = byId('supervisorFrame');
  if (!pane || !frame) {
    // No pane in this markup: fall back to the page rather than doing nothing.
    window.location.href = 'supervisor.html';
    return;
  }
  // Loaded on first open and left loaded afterwards, so reopening is instant
  // and the supervisor keeps its state.
  if (frame.getAttribute('src') !== 'supervisor.html') {
    frame.setAttribute('src', 'supervisor.html');
  }
  // Hide all other .main children (via the hidden attribute) so the supervisor
  // fills the entire right-side panel — full height, not squeezed into the
  // bottom-right corner below the still-visible topbar / messages / composer.
  _paneReturn = new Map();
  const main = pane.parentElement;
  for (const child of main?.children ?? []) {
    if (child !== pane && !child.hidden) {
      child.hidden = true;
      _paneReturn.set(child.id, true);
    }
  }
  pane.hidden = false;
  pane.classList.add('open');
  byId('supervisorPaneClose')?.focus();
}

export function closeSupervisorPane() {
  const pane = byId('supervisorPane');
  if (!pane || pane.hidden) return;
  pane.hidden = true;
  pane.classList.remove('open');
  // Restore the .main children that were hidden when the pane opened.
  for (const child of pane.parentElement?.children ?? []) {
    if (child !== pane && _paneReturn.has(child.id)) {
      child.hidden = false;
      _paneReturn.delete(child.id);
    }
  }
}

// Note: startSupervisorPolling lives in device-alerts.js alongside
// refreshSupervisor — supervisor.js only owns the pane/picker UI.