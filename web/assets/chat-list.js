// Search, grouping, rendering, and actions for conversation sidebars.

// Whether a conversation or session is more than one minute stale — its title
// should render muted so it reads as out of date at a glance.
function _stale(updatedAt) {
  if (!updatedAt) return false;
  const then = new Date(updatedAt).getTime();
  return (Date.now() - then) > 60_000;
}

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
    onRemoveCli,
    onReorder,
    onClearSupervisor,
    onDismissAgent,
    onOpenSupervisor,
    onAddToSupervisor,
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
  // A set, not an id: several conversations can be mid-turn at once now that a
  // turn survives the user looking somewhere else.
  let activeTurnIds = new Set();
  // Conversations whose reply landed while the user was elsewhere. Kept here
  // rather than in the database: "have I read this" is per-browser, and
  // updated_at already tells us when something changed.
  let unreadIds = new Set();
  // Conversations whose turn has finished and nobody has sent a new prompt
  // since. Distinct from unread: unread tracks "have I looked", this tracks
  // "is it done" -- true even for the chat you are currently viewing, and
  // cleared the moment you send into it again rather than by opening it.
  let endedIds = new Set();
  let historyEntries = [];
  // {waiting: [...], working: [...]} from GET /api/supervisor.
  let supervisor = {waiting: [], working: []};
  let dragging = null;

  // Persist the order of one section. Sends the whole section rather than a
  // single moved id: the server writes it as one transaction, so a drop cannot
  // half-apply and leave an order the user never chose.
  function commitOrder(container, sectionKey) {
    if (!onReorder) return;
    // Sections that are not collapsed share one container, so selecting every
    // row in it swept Recent into a drag made inside Favourites -- silently
    // converting a recency-ordered section to a manual one. Only the rows
    // belonging to this section are sent.
    const ids = [...container.querySelectorAll('.chat-item[data-chat-id]')]
      .filter(node => !sectionKey || node.dataset.section === sectionKey)
      .map(node => node.dataset.chatId);
    if (ids.length) onReorder(ids);
  }

  // Move a conversation one slot within its section. Drag is unusable on a
  // phone, and this console is used from one.
  function nudge(chatId, delta) {
    const row = document.querySelector(`.chat-item[data-chat-id="${CSS.escape(chatId)}"]`);
    if (!row || !row.parentElement) return;
    const siblings = [...row.parentElement.querySelectorAll('.chat-item[data-chat-id]')]
      .filter(node => node.dataset.section === row.dataset.section);
    const index = siblings.indexOf(row);
    const next = index + delta;
    if (index < 0 || next < 0 || next >= siblings.length) return;
    if (delta < 0) row.parentElement.insertBefore(row, siblings[next]);
    else row.parentElement.insertBefore(siblings[next], row);
    commitOrder(row.parentElement, row.dataset.section);
  }

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
      title.className = 'chat-title' + (_stale(chat.updated_at) ? ' stale' : '');
      title.textContent = chat.title;
      title.title = chat.title;
      open.appendChild(title);

      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const metaParts = [formatTime(chat.updated_at)];
      // last_model_used, not chat.model: the latter is the routing override
      // and stays empty until a user sets one, so this line showed nothing
      // for every conversation running on a default.
      if (chat.last_model_used) metaParts.push(chat.last_model_used);
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
      item.dataset.section = label;

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-open';
      open.dataset.action = 'open';
      open.dataset.chatId = chat.id;
      // Archived rows stay clickable. Disabling them meant the only way to read
      // an archived conversation was to restore it first -- mutating state just
      // to look at something.

      const stale = _stale(chat.updated_at);
      const title = document.createElement('div');
      title.className = 'chat-title' + (stale ? ' stale' : '');
      title.textContent = chat.title;
      title.title = chat.title;
      if (activeTurnIds.has(chat.id)) {
        const dot = document.createElement('span');
        dot.className = 'chat-running';
        dot.setAttribute('aria-label', 'Response in progress');
        dot.title = 'Response in progress';
        title.prepend(dot);
      } else if (chat.terminal_busy) {
        // Work is happening in a terminal linked to this conversation. Worth a
        // dot -- it was showing nothing at all -- but a different one, because
        // there is no stream here to open.
        const dot = document.createElement('span');
        dot.className = 'chat-terminal-busy';
        dot.setAttribute('aria-label', 'Working in its terminal');
        dot.title = 'Working in the terminal session running this conversation';
        title.prepend(dot);
      } else if (unreadIds.has(chat.id)) {
        const mark = document.createElement('span');
        mark.className = 'chat-unread';
        mark.setAttribute('aria-label', 'New reply');
        mark.title = 'Replied while you were elsewhere';
        title.prepend(mark);
      } else if (endedIds.has(chat.id)) {
        // Lowest tier: running, terminal work and an unread reply all already
        // say enough about this conversation's state, so this only shows when
        // none of them apply -- a quiet "it finished" for a chat you already
        // know about.
        const mark = document.createElement('span');
        mark.className = 'chat-ended';
        mark.setAttribute('aria-label', 'Finished responding');
        mark.title = 'Finished responding';
        title.prepend(mark);
      } else if (!chat.queued) {
        // Nothing running, nothing happening in a linked terminal, no unread
        // reply, no just-finished marker, and nothing queued to send once
        // something else wraps up -- genuinely nothing outstanding. Checked
        // last and gated on !chat.queued specifically because queued is
        // otherwise independent of this chain (rendered as its own badge
        // below): a chat with prompts waiting to send is still "waiting for
        // tasks to end" even when none of the tiers above apply to it right
        // now, so it must not read as free.
        //
        // Deliberately does NOT check for a pending, unanswered question --
        // that would mean reading each chat's transcript or live terminal on
        // every poll, for every chat, which is the kind of per-poll cost that
        // was already reported as making the whole page slow. A chat left
        // free here that actually has an old unanswered question sitting in
        // it is the accepted gap; opening it still shows the question bar as
        // normal regardless of what the sidebar icon said.
        const mark = document.createElement('span');
        mark.className = 'chat-free';
        mark.setAttribute('aria-label', 'Nothing outstanding');
        mark.title = 'Nothing running or queued for this conversation';
        title.prepend(mark);
      }
      if (chat.queued) {
        const queued = document.createElement('span');
        queued.className = 'chat-queued';
        queued.textContent = String(chat.queued);
        queued.setAttribute('aria-label', `${chat.queued} prompts queued`);
        queued.title = `${chat.queued} prompt${chat.queued > 1 ? 's' : ''} waiting to send`;
        title.append(queued);
      }
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const metaParts = [formatTime(chat.updated_at)];
      // Same fix as the other render path above: last_model_used reflects
      // what actually answered, chat.model is a routing override that is
      // usually unset.
      metaParts.push(chat.last_model_used || '—');
      if (chat.archived) metaParts.push('archived');
      meta.textContent = metaParts.join(' · ');
      meta.title = `Last model: ${chat.last_model_used || '—'}`;
      open.append(title, meta);

      const actions = document.createElement('div');
      actions.className = 'chat-actions';

      // Favouriting is one click on the row. It was previously the first item
      // in the ⋯ menu, which made the most-used action the hardest to reach.
      const favourite = document.createElement('button');
      favourite.type = 'button';
      favourite.className = 'chat-action chat-favourite';
      favourite.dataset.action = 'pin';
      favourite.dataset.chatId = chat.id;
      favourite.textContent = chat.pinned ? '★' : '☆';
      favourite.title = chat.pinned ? 'Remove from favourites' : 'Add to favourites';
      favourite.setAttribute('aria-pressed', String(Boolean(chat.pinned)));
      favourite.setAttribute(
        'aria-label',
        `${chat.pinned ? 'Remove' : 'Add'} ${chat.title} ${chat.pinned ? 'from' : 'to'} favourites`);
      if (chat.pinned) favourite.classList.add('on');
      actions.appendChild(favourite);

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
        // Favouriting moved to a star on the row itself; keeping it here too
        // would be two controls doing one job, which is how this menu grew.
        makeButton('Move up', 'move-up', chat.id, chat.title),
        makeButton('Move down', 'move-down', chat.id, chat.title),
        // List-level, but it belongs where the ordering controls are.
        makeButton('Reset list order', 'reset-order', chat.id, chat.title),
        makeButton('Rename', 'rename', chat.id, chat.title),
        makeButton('Fork', 'fork', chat.id, chat.title),
        makeButton('Export', 'export', chat.id, chat.title),
        makeButton('Continue in terminal', 'terminal', chat.id, chat.title),
        // Placed with the routing actions rather than the ordering ones: it
        // changes where this conversation is watched from, not where it sits
        // in the list. The supervisor list is fetched on click rather than
        // built here, so a sidebar render costs no request.
        makeButton('Add to supervisor', 'add-to-supervisor', chat.id, chat.title),
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
      // Dragging reorders within this section only, so moving a favourite
      // cannot silently reshuffle the rest of the list.
      item.draggable = true;
      item.addEventListener('dragstart', event => {
        dragging = item;
        item.classList.add('dragging');
        event.dataTransfer.effectAllowed = 'move';
        // Firefox needs data set or the drag never starts.
        event.dataTransfer.setData('text/plain', chat.id);
      });
      item.addEventListener('dragend', () => {
        item.classList.remove('dragging');
        dragging = null;
        commitOrder(target, label);
      });
      item.addEventListener('dragover', event => {
        if (!dragging || dragging === item || dragging.parentElement !== target) return;
        if (dragging.dataset.section !== item.dataset.section) return;
        event.preventDefault();
        const box = item.getBoundingClientRect();
        const below = event.clientY > box.top + box.height / 2;
        target.insertBefore(dragging, below ? item.nextSibling : item);
      });

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
      name.className = 'chat-title' + (_stale(session.status_updated_at) ? ' stale' : '');
      name.textContent = session.name;
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const metaParts = [];
      metaParts.push(session.kind === 'interactive' ? 'Terminal' : 'Web');
      metaParts.push(session.model || '—');
      // A session whose process has exited stays listed: its transcript is
      // still readable and worth resuming. It is marked rather than hidden.
      const ended = session.live === false;
      if (ended) metaParts.push('ended');
      meta.textContent = metaParts.join(' · ');
      if (session.model) meta.title = `Last model: ${session.model}`;
      if (ended) item.classList.add('session-ended');
      title.append(name, meta);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chat-action';
      button.dataset.action = 'resume-cli';
      button.dataset.sessionId = session.id;
      button.textContent = '↗';
      button.setAttribute('aria-label', `Open ${session.name} in WebConsole`);
      item.append(title, button);
      // Removal is offered only for ended sessions. A live one has a running
      // process behind it, and the server refuses to delete its record anyway.
      if (ended) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'chat-action chat-action-remove';
        remove.dataset.action = 'remove-cli';
        remove.dataset.sessionId = session.id;
        remove.textContent = '×';
        remove.title = 'Remove this ended session from the list';
        remove.setAttribute('aria-label', `Remove ended session ${session.name}`);
        item.append(remove);
      }
      details.appendChild(item);
    });
  }

  // Past conversations, read from transcripts on disk. The live registry in
  // ~/.claude/sessions only knows about sessions that are still running, so
  // without this the sidebar could never show a finished conversation -- the
  // reason old work appeared to have vanished.
  function renderHistory(list, entries, alreadyShown) {
    const items = entries.filter(entry => !alreadyShown.has(entry.session_id));
    if (!items.length) return;
    const details = makeDisclosure('History', items.length, false);
    list.appendChild(details);

    items.forEach(entry => {
      const item = document.createElement('div');
      item.className = 'chat-item';

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-open';
      open.dataset.action = 'read-transcript';
      open.dataset.sessionId = entry.session_id;

      const name = document.createElement('div');
      name.className = 'chat-title';
      name.textContent = entry.title || entry.session_id;
      name.title = entry.title || entry.session_id;

      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const when = entry.updated_at
        ? new Date(entry.updated_at * 1000).toLocaleString()
        : '';
      meta.textContent = [when, 'read-only'].filter(Boolean).join(' · ');

      open.append(name, meta);

      const resume = document.createElement('button');
      resume.type = 'button';
      resume.className = 'chat-action';
      resume.dataset.action = 'resume-cli';
      resume.dataset.sessionId = entry.session_id;
      resume.textContent = '↗';
      resume.title = 'Continue this conversation here';
      resume.setAttribute(
        'aria-label', `Continue ${entry.title || entry.session_id} in WebConsole`);

      item.append(open, resume);
      details.appendChild(item);
    });
  }

  // Agents waiting on the user, above everything else. This section is the
  // answer to "is anything blocked on me" -- which is otherwise only knowable
  // by opening every conversation and every terminal in turn.
  function renderSupervisor(list, state) {
    const waiting = state.waiting || [];
    const working = state.working || [];
    const updated = state.updated || [];
    // `waiting` is the attention feed: an agent that asked for something, one
    // that reported it is stuck, and -- since Pedro's rule change -- one whose
    // work has ended. Those are the two moments worth interrupting for.
    //
    // What is deliberately NOT in it is an agent mid-flow. Output arriving is
    // not a summons; it used to be treated as one whenever the text happened to
    // end with a colon, and a badge that fires on prose is a badge that gets
    // ignored -- which costs the real asks buried among them. `updated` remains
    // the quiet bucket for output that needs nothing.
    const nothingToShow = !waiting.length && !working.length && !updated.length;

    const heading = document.createElement('button');
    heading.className = 'chat-section-label supervisor-label';
    heading.type = 'button';
    heading.dataset.action = 'open-supervisor';
    heading.appendChild(document.createTextNode('Supervisor'));
    if (waiting.length) {
      const badge = document.createElement('span');
      badge.className = 'supervisor-badge';
      badge.textContent = String(waiting.length);
      // "needs you" rather than "waiting for you": the feed now also holds
      // agents that have finished, and those are not waiting on anything.
      const finished = waiting.filter((e) => e.reason === 'done').length;
      const blocked = waiting.length - finished;
      badge.title = [
        blocked ? `${blocked} need${blocked === 1 ? 's' : ''} an answer` : '',
        finished ? `${finished} finished` : '',
      ].filter(Boolean).join(' · ');
      heading.appendChild(badge);
    }
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'chat-action supervisor-open';
    open.dataset.action = 'open-supervisor';
    open.textContent = '↗';
    open.title = 'Open the supervisor';
    open.setAttribute('aria-label', 'Open the supervisor');
    heading.appendChild(open);

    if (waiting.length || updated.length) {
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'chat-action supervisor-clear';
      clear.dataset.action = 'clear-supervisor';
      clear.textContent = '✕';
      clear.title = 'Clear all alerts';
      clear.setAttribute('aria-label', 'Clear all alerts');
      heading.appendChild(clear);
    }
    list.appendChild(heading);
    if (nothingToShow) return;

    waiting.forEach(entry => {
      const item = document.createElement('div');
      // Deliberately not draggable: these rows are a view onto other sections,
      // and a drop here would ask the reorder handler to place a conversation
      // in a container that does not own the ordering.
      item.className = 'chat-item supervisor-item'
        + (entry.reason === 'failed' ? ' failed' : '');

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-open';
      open.dataset.action = 'jump-agent';
      open.dataset.agentKind = entry.kind;
      open.dataset.agentId = entry.id;

      const title = document.createElement('div');
      title.className = 'chat-title';
      const dot = document.createElement('span');
      dot.className = 'supervisor-dot';
      dot.setAttribute('aria-label', 'Waiting for you');
      title.append(dot, document.createTextNode(entry.title || entry.id));
      // A "?" only where there is a question to answer. Every row in this
      // section is waiting on you, so a mark on all of them would say nothing;
      // what it distinguishes is the ones that asked something from the ones
      // that failed, stalled, or simply stopped talking.
      if (entry.question) {
        const asks = document.createElement('span');
        asks.className = 'supervisor-asks';
        asks.textContent = '?';
        asks.title = 'This one asked you a question';
        asks.setAttribute('aria-label', 'Has a question');
        title.appendChild(asks);
      }
      title.title = entry.title || entry.id;

      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      meta.textContent = [
        entry.kind === 'session' ? 'terminal' : 'web',
        // A failure is not a question and must not read like one -- "needs an
        // answer" next to a dead endpoint tells you to go and type something.
        // Nor is a finished task: `done` is the outcome the user was waiting
        // for, and labelling it as a question would send them off to answer
        // nothing.
        entry.reason === 'failed' ? 'failed'
          : entry.reason === 'blocked' ? 'blocked'
            : entry.reason === 'done' ? 'finished' : 'needs an answer',
        entry.since ? formatTime(entry.since) : '',
      ].filter(Boolean).join(' · ');

      open.append(title, meta);
      if (entry.preview) {
        const preview = document.createElement('div');
        preview.className = 'chat-snippet';
        preview.textContent = entry.preview;
        preview.title = entry.preview;
        open.appendChild(preview);
      }
      item.appendChild(open);

      // Dismiss this one row. The heading's ✕ clears everything at once, which
      // is the only control there was: silencing one agent you have dealt with
      // meant silencing the rest, including questions still unanswered.
      const label = entry.title || entry.id;
      const dismiss = document.createElement('button');
      dismiss.type = 'button';
      dismiss.className = 'chat-action supervisor-dismiss';
      dismiss.dataset.action = 'dismiss-agent';
      dismiss.dataset.agentKind = entry.kind;
      dismiss.dataset.agentId = entry.id;
      dismiss.textContent = '✕';
      dismiss.title = `Remove ${label} from the highlights`;
      dismiss.setAttribute('aria-label', `Remove ${label} from the highlights`);
      item.appendChild(dismiss);

      list.appendChild(item);
    });

    const quiet = [];
    if (working.length) quiet.push(`${working.length} working`);
    if (updated.length) quiet.push(`${updated.length} with new output`);
    if (quiet.length) {
      const note = document.createElement('div');
      note.className = 'supervisor-note';
      note.textContent = `${quiet.join(' · ')} · nothing needed`;
      list.appendChild(note);
    }
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
      renderSupervisor(list, supervisor);
      renderSection(list, 'Favourites', groups.pinned, currentId);
      renderSection(list, 'Recent', groups.recent, currentId);
      renderSection(list, 'Archived', groups.archived, currentId, true);
      if (extraHits.length) renderSearchResults(list, extraHits, currentId);
      renderCli(list, cli);
      // A conversation already listed as a live session or as a chat of its own
      // must not appear a second time under History.
      const alreadyShown = new Set([
        ...cliSessions.map(s => s.sessionId).filter(Boolean),
        ...chats.map(c => c.session_id).filter(Boolean),
      ]);
      renderHistory(list, historyEntries, alreadyShown);

      if (!cli.length && !filtered.length && !extraHits.length && !historyEntries.length) {
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

  function setHistory(entries) {
    historyEntries = Array.isArray(entries) ? entries : [];
    render();
  }

  function setSupervisor(state) {
    supervisor = {
      waiting: Array.isArray(state?.waiting) ? state.waiting : [],
      working: Array.isArray(state?.working) ? state.working : [],
    };
    render();
  }

  function setActiveTurns(ids) {
    const next = new Set(ids || []);
    if (next.size === activeTurnIds.size
        && [...next].every(id => activeTurnIds.has(id))) return;
    activeTurnIds = next;
    render();
  }

  function setUnread(ids) {
    const next = new Set(ids || []);
    if (next.size === unreadIds.size
        && [...next].every(id => unreadIds.has(id))) return;
    unreadIds = next;
    render();
  }

  function setEnded(ids) {
    const next = new Set(ids || []);
    if (next.size === endedIds.size
        && [...next].every(id => endedIds.has(id))) return;
    endedIds = next;
    render();
  }

  // Single-id removal, not setEnded([]) or setEnded(withoutThisId): the
  // caller (clearEndedFlag, on sending into a chat) only knows the one id
  // that just stopped being "ended", not the current full set.
  function clearEnded(chatId) {
    if (!endedIds.has(chatId)) return;
    endedIds.delete(chatId);
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
    if (action === 'open-supervisor') return onOpenSupervisor?.();
    if (action === 'add-to-supervisor') {
      return onAddToSupervisor?.(button.dataset.chatId, button);
    }
    if (action === 'clear-supervisor') return onClearSupervisor?.();
    if (action === 'dismiss-agent') {
      // Sits inside the row, whose own click opens the conversation. Without
      // this the row would open the very chat you asked to stop being shown.
      event.stopPropagation();
      const {agentKind, agentId} = button.dataset;
      return onDismissAgent?.(agentKind, agentId);
    }
    if (action === 'jump-agent') {
      // A supervisor row is a pointer at something listed elsewhere: a web
      // chat opens, a terminal session resumes into one.
      const {agentKind, agentId} = button.dataset;
      return agentKind === 'session' ? onResumeCli(agentId) : onSelect(agentId);
    }
    if (action === 'read-transcript') {
      // The viewer mounts itself from transcript.js; an event keeps the two
      // modules decoupled rather than reaching across for a handle.
      document.dispatchEvent(new CustomEvent('wc:open-transcript', {
        detail: {sessionId: button.dataset.sessionId},
      }));
      return;
    }
    if (action === 'remove-cli') return onRemoveCli?.(button.dataset.sessionId);
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
    if (action === 'move-up' || action === 'move-down') {
      nudge(button.dataset.chatId, action === 'move-up' ? -1 : 1);
      return;
    }
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
    setActiveTurns,
    setUnread,
    setEnded,
    clearEnded,
    setHistory,
    setSupervisor,
  };
}
