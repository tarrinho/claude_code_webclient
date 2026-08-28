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

function highlightSnippet(snippet, query) {
  if (!snippet || !query) return snippet;
  const idx = snippet.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return snippet;
  const before = snippet.slice(0, idx);
  const match = snippet.slice(idx, idx + query.length);
  const after = snippet.slice(idx + query.length);
  return `${before}<mark>${match}</mark>${after}`;
}

export function groupChats(chats) {
  return {
    pinned: chats.filter(chat => !chat.archived && chat.pinned),
    recent: chats.filter(chat => !chat.archived && !chat.pinned),
    archived: chats.filter(chat => chat.archived),
  };
}

function makeButton(label, action, chatId) {
  const button = document.createElement('button');
  button.type = 'button';
  button.dataset.action = action;
  button.dataset.chatId = chatId;
  button.textContent = label;
  button.setAttribute('role', 'menuitem');
  return button;
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
  let _searchMode = 'title';

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
    heading.textContent = `Search results · ${results.length}`;
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
        snippetEl.innerHTML = highlightSnippet(chat.snippet, query || messageQuery);
        meta.appendChild(snippetEl);
      }

      open.appendChild(meta);
      item.appendChild(open);
      list.appendChild(item);
    });
  }

  function renderSection(list, label, chats, currentId) {
    if (!chats.length) return;
    const heading = document.createElement('div');
    heading.className = 'chat-section-label';
    heading.textContent = `${label} · ${chats.length}`;
    list.appendChild(heading);

    chats.forEach(chat => {
      const item = document.createElement('div');
      item.className = `chat-item${chat.id === currentId ? ' active' : ''}`;
      item.dataset.chatId = chat.id;

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-open';
      open.dataset.action = 'open';
      open.dataset.chatId = chat.id;
      open.disabled = Boolean(chat.archived);

      const title = document.createElement('div');
      title.className = 'chat-title';
      title.textContent = chat.title;
      title.title = chat.title;
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      let metaParts = [formatTime(chat.updated_at)];
      if (chat.model) metaParts.push(chat.model);
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
      menu.append(
        makeButton(`${chat.pinned ? 'Unpin' : 'Pin'} ${chat.title}`, 'pin', chat.id),
        makeButton(`Rename ${chat.title}`, 'rename', chat.id),
        makeButton(`Fork ${chat.title}`, 'fork', chat.id),
        makeButton(`Export ${chat.title}`, 'export', chat.id),
        makeButton(`${chat.archived ? 'Restore' : 'Archive'} ${chat.title}`, chat.archived ? 'restore' : 'archive', chat.id),
        makeButton(`Delete ${chat.title}`, 'delete', chat.id),
      );
      actions.append(trigger, menu);
      item.append(open, actions);
      list.appendChild(item);
    });
  }

  function renderCli(list, sessions) {
    if (!sessions.length) return;
    const heading = document.createElement('div');
    heading.className = 'chat-section-label';
    heading.textContent = `CLI Sessions · ${sessions.length}`;
    list.appendChild(heading);
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
      list.appendChild(item);
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

    lists.forEach(list => {
      list.replaceChildren();
      renderCli(list, cli);
      // When message search has results, show those instead of regular chat groups
      if (messageResults.length) {
        renderSearchResults(list, messageResults, currentId);
      } else {
        renderSection(list, 'Pinned', groups.pinned, currentId);
        renderSection(list, 'Recent', groups.recent, currentId);
        renderSection(list, 'Archived', groups.archived, currentId);
        if (!cli.length && !filtered.length) {
          const empty = document.createElement('div');
          empty.className = 'sidebar-empty';
          empty.textContent = query ? 'No matching sessions' : 'No sessions yet';
          list.appendChild(empty);
        }
      }
    });
  }

  function setQuery(value) {
    query = value.trim().toLowerCase();
    searchInputs.forEach(input => { input.value = value; });
    // If in message search mode, debounce and call the API
    if (_searchMode === 'message' && value.trim().length >= 2) {
      messageQuery = value.trim();
      if (messageDebounce) clearTimeout(messageDebounce);
      messageDebounce = setTimeout(() => {
        if (messageCallback) {
          messageCallback(messageQuery);
        }
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

  function setSearchMode(mode) {
    _searchMode = mode;
    messageResults = [];
    messageQuery = '';
    query = '';
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

  return {render, setQuery, setCliSessions, closeMenus, setOnMessageSearch, setSearchMode};
}
