// Search, grouping, rendering, and actions for conversation sidebars.

export function filterChats(chats, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...chats];
  return chats.filter(chat =>
    (chat.title || '').toLowerCase().includes(needle) ||
    (chat.description || '').toLowerCase().includes(needle) ||
    (chat.work_dir || '').toLowerCase().includes(needle)
  );
}

export function filterSearchResults(results, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...results];
  return results.filter(r =>
    (r.title || '').toLowerCase().includes(needle) ||
    (r.snippet || '').toLowerCase().includes(needle)
  );
}

// Returns a DocumentFragment, never an HTML string: the snippet is message
// content (both the user's prompt and the model's reply), so interpolating it
// into innerHTML made any message containing markup a stored XSS. The wc_csrf
// cookie is readable by JS by design, so script execution here would hand over
// the whole API.
function highlightSnippet(snippet, query) {
  const fragment = document.createDocumentFragment();
  if (!snippet) return fragment;

  const idx = query ? snippet.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (idx === -1) {
    fragment.appendChild(document.createTextNode(snippet));
    return fragment;
  }

  const mark = document.createElement('mark');
  mark.textContent = snippet.slice(idx, idx + query.length);
  fragment.appendChild(document.createTextNode(snippet.slice(0, idx)));
  fragment.appendChild(mark);
  fragment.appendChild(document.createTextNode(snippet.slice(idx + query.length)));
  return fragment;
}

export function groupChats(chats) {
  return {
    pinned: chats.filter(chat => !chat.archived && chat.pinned),
    recent: chats.filter(chat => !chat.archived && !chat.pinned),
    archived: chats.filter(chat => chat.archived),
  };
}

// The visible label is the bare verb; the conversation title goes to
// aria-label. Both used to live in textContent, so the menu rendered as six
// lines each restating the full title ("Pin <title>", "Rename <title>"…),
// which is unreadable for anything but the shortest names.
function makeButton(label, action, chatId, chatTitle, extraClass) {
  const button = document.createElement('button');
  button.type = 'button';
  button.dataset.action = action;
  button.dataset.chatId = chatId;
  button.textContent = label;
  button.setAttribute('role', 'menuitem');
  button.setAttribute('aria-label', `${label} ${chatTitle}`);
  if (extraClass) button.className = extraClass;
  return button;
}

// A collapsed section: keeps archived conversations and CLI sessions reachable
// without letting them push the active list off screen.
function makeDisclosure(label, count, open) {
  const details = document.createElement('details');
  details.className = 'chat-group';
  details.open = open;
  const summary = document.createElement('summary');
  summary.className = 'chat-section-label';
  summary.textContent = `${label} · ${count}`;
  details.appendChild(summary);
  return details;
}

export function createChatListController(dependencies) {
  const {
    lists,
    searchInputs,
    formatTime,
    formatAbsoluteTime,
    onSelect,
    onAction,
    onResumeCli,
  } = dependencies;

  let query = '';
  let cliSessions = [];
  let messageResults = [];
  let messageQuery = '';
  let messageDebounce = null;
  let messageCallback = null;
  let lastChats = [];
  let lastCurrentId = null;
  let openTrigger = null;
  let activeTurnId = null;

  function closeMenus(restoreFocus = false) {
    for (const menu of document.querySelectorAll('.chat-menu.open')) {
      menu.classList.remove('open');
      const trigger = menu.previousElementSibling;
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }
    if (restoreFocus && openTrigger) openTrigger.focus();
    openTrigger = null;
  }

  function renderSearchResults(list, results, currentId) {
    if (!results.length) return;
    const heading = document.createElement('div');
    heading.className = 'chat-section-label';
    heading.textContent = `In messages · ${results.length}`;
    list.appendChild(heading);

    results.forEach(chat => {
      const item = document.createElement('div');
      item.className = `chat-item${chat.id === currentId ? ' active' : ''}`;
      item.dataset.chatId = chat.id;

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-open';
      open.dataset.action = 'open';
      open.dataset.chatId = chat.id;

      const title = document.createElement('div');
      title.className = 'chat-title';
      title.textContent = chat.title;
      title.title = chat.title;
      open.appendChild(title);

      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const metaParts = [formatTime(chat.updated_at)];
      if (chat.model) metaParts.push(chat.model);
      meta.textContent = metaParts.join(' · ');

      // Display snippet with highlighting
      if (chat.snippet) {
        const snippetEl = document.createElement('div');
        snippetEl.className = 'chat-snippet';
        snippetEl.appendChild(highlightSnippet(chat.snippet, query || messageQuery));
        meta.appendChild(snippetEl);
      }

      open.appendChild(meta);
      item.appendChild(open);
      list.appendChild(item);
    });
  }

  function renderSection(list, label, chats, currentId, collapsed = false) {
    if (!chats.length) return;
    let target = list;
    if (collapsed) {
      const details = makeDisclosure(label, chats.length, false);
      list.appendChild(details);
      target = details;
    } else {
      const heading = document.createElement('div');
      heading.className = 'chat-section-label';
      heading.textContent = `${label} · ${chats.length}`;
      list.appendChild(heading);
    }

    chats.forEach(chat => {
      const item = document.createElement('div');
      item.className = `chat-item${chat.id === currentId ? ' active' : ''}`;
      if (chat.archived) item.classList.add('archived');
      item.dataset.chatId = chat.id;

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-open';
      open.dataset.action = 'open';
      open.dataset.chatId = chat.id;
      // Archived rows stay clickable. Disabling them meant the only way to read
      // an archived conversation was to restore it first -- mutating state just
      // to look at something.

      const title = document.createElement('div');
      title.className = 'chat-title';
      title.textContent = chat.title;
      title.title = chat.title;
      if (chat.id === activeTurnId) {
        const dot = document.createElement('span');
        dot.className = 'chat-running';
        dot.setAttribute('aria-label', 'Response in progress');
        dot.title = 'Response in progress';
        title.prepend(dot);
      }
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const metaParts = [formatTime(chat.updated_at)];
      if (chat.model) metaParts.push(chat.model);
      if (chat.archived) metaParts.push('archived');
      meta.textContent = metaParts.join(' · ');
      if (chat.model) meta.title = `Last model: ${chat.model}`;
      open.append(title, meta);

      const actions = document.createElement('div');
      actions.className = 'chat-actions';
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'chat-action';
      trigger.dataset.action = 'menu';
      trigger.dataset.chatId = chat.id;
      trigger.textContent = '⋯';
      trigger.title = 'Conversation actions';
      trigger.setAttribute('aria-label', `Actions for ${chat.title}`);
      trigger.setAttribute('aria-haspopup', 'menu');
      trigger.setAttribute('aria-expanded', 'false');

      const menu = document.createElement('div');
      menu.className = 'chat-menu';
      menu.setAttribute('role', 'menu');
      const separator = document.createElement('div');
      separator.className = 'chat-menu-sep';
      separator.setAttribute('role', 'separator');
      menu.append(
        makeButton(chat.pinned ? 'Unpin' : 'Pin', 'pin', chat.id, chat.title),
        makeButton('Rename', 'rename', chat.id, chat.title),
        makeButton('Fork', 'fork', chat.id, chat.title),
        makeButton('Export', 'export', chat.id, chat.title),
        makeButton(
          chat.archived ? 'Restore' : 'Archive',
          chat.archived ? 'restore' : 'archive',
          chat.id,
          chat.title,
        ),
        separator,
        // Delete is irreversible, so it is set apart rather than sitting flush
        // against Export as one more equal-weight choice.
        makeButton('Delete', 'delete', chat.id, chat.title, 'danger'),
      );
      actions.append(trigger, menu);
      item.append(open, actions);
      target.appendChild(item);
    });
  }

  function renderCli(list, sessions) {
    if (!sessions.length) return;
    const details = makeDisclosure('CLI Sessions', sessions.length, false);
    list.appendChild(details);
    sessions.forEach(session => {
      const item = document.createElement('div');
      item.className = 'chat-item';
      const title = document.createElement('div');
      title.className = 'chat-open';
      const name = document.createElement('div');
      name.className = 'chat-title';
      name.textContent = session.name;
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const metaParts = [];
      metaParts.push(session.kind === 'interactive' ? 'Terminal' : 'Web');
      if (session.model) metaParts.push(session.model);
      meta.textContent = metaParts.join(' · ');
      if (session.model) meta.title = `Last model: ${session.model}`;
      title.append(name, meta);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chat-action';
      button.dataset.action = 'resume-cli';
      button.dataset.sessionId = session.id;
      button.textContent = '↗';
      button.setAttribute('aria-label', `Open ${session.name} in WebConsole`);
      item.append(title, button);
      details.appendChild(item);
    });
  }

  function render(chats = lastChats, currentId = lastCurrentId) {
    lastChats = chats;
    lastCurrentId = currentId;
    const filtered = filterChats(chats, query);
    const groups = groupChats(filtered);
    const cli = query ? cliSessions.filter(session =>
      (session.name || '').toLowerCase().includes(query) ||
      (session.cwd || '').toLowerCase().includes(query)
    ) : cliSessions;

    // Message hits are appended below the title matches, never in place of
    // them: replacing the list meant a message search hid every conversation
    // whose title did not also match, including the one you were reading.
    const titleIds = new Set(filtered.map(chat => chat.id));
    const extraHits = messageResults.filter(chat => !titleIds.has(chat.id));

    lists.forEach(list => {
      list.replaceChildren();
      renderSection(list, 'Pinned', groups.pinned, currentId);
      renderSection(list, 'Recent', groups.recent, currentId);
      renderSection(list, 'Archived', groups.archived, currentId, true);
      if (extraHits.length) renderSearchResults(list, extraHits, currentId);
      renderCli(list, cli);

      if (!cli.length && !filtered.length && !extraHits.length) {
        const empty = document.createElement('div');
        empty.className = 'sidebar-empty';
        empty.textContent = query ? 'No matching sessions' : 'No sessions yet';
        list.appendChild(empty);
      }
    });
  }

  function setQuery(value) {
    query = value.trim().toLowerCase();
    searchInputs.forEach(input => { input.value = value; });
    // One search, not two modes. Titles filter as you type; message bodies are
    // queried in the background and appended. The old mode toggle only existed
    // in the mobile sidebar, so desktop could never reach message search at all.
    if (value.trim().length >= 2) {
      messageQuery = value.trim();
      if (messageDebounce) clearTimeout(messageDebounce);
      messageDebounce = setTimeout(() => {
        if (messageCallback) messageCallback(messageQuery);
      }, 300);
    } else {
      messageResults = [];
      messageQuery = '';
      if (messageDebounce) {
        clearTimeout(messageDebounce);
        messageDebounce = null;
      }
    }
    render();
  }

  function setOnMessageSearch(callback) {
    messageCallback = callback;
  }

  // Receives FTS hits from the search endpoint. Previously the caller passed
  // these straight to render() as if they were the chat list, so they were
  // re-filtered by title and silently discarded -- message search never worked.
  function setMessageResults(results) {
    messageResults = Array.isArray(results) ? results : [];
    render();
  }

  function setActiveTurn(chatId) {
    if (activeTurnId === chatId) return;
    activeTurnId = chatId;
    render();
  }

  function setCliSessions(sessions) {
    cliSessions = sessions;
  }

  function handleClick(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'open') return onSelect(button.dataset.chatId);
    if (action === 'resume-cli') return onResumeCli(button.dataset.sessionId);
    if (action === 'menu') {
      const menu = button.nextElementSibling;
      const opening = !menu.classList.contains('open');
      closeMenus();
      if (opening) {
        menu.classList.add('open');
        button.setAttribute('aria-expanded', 'true');
        openTrigger = button;
        menu.querySelector('button')?.focus();
      }
      return;
    }
    closeMenus();
    onAction(action, button.dataset.chatId);
  }

  lists.forEach(list => list.addEventListener('click', handleClick));
  searchInputs.forEach(input => input.addEventListener('input', () => setQuery(input.value)));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.querySelector('.chat-menu.open')) {
      event.preventDefault();
      closeMenus(true);
    }
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.chat-actions')) closeMenus();
  });

  return {
    render,
    setQuery,
    setCliSessions,
    closeMenus,
    setOnMessageSearch,
    setMessageResults,
    setActiveTurn,
  };
}
