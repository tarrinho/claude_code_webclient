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

/** One section reordered so the conversations doing something lead.
 *
 *  The sidebar already marks these with a running dot, but the order came
 *  straight from the server -- placement, then recency -- so the conversation
 *  you were waiting on could sit anywhere in the section, including below the
 *  fold.
 *
 *  Two properties this must keep, both load-bearing:
 *
 *  - **Stable.** Within "active" and within "idle", the incoming order
 *    survives untouched, so the manual placement and recency the server sorted
 *    by still decide everything the float does not. A plain `sort` with a
 *    boolean comparator is not guaranteed to do this across engines for the
 *    equal case; partitioning is stable by construction.
 *  - **Not in place.** The caller holds the same array `groupChats` returned
 *    and hands it to other readers, so sorting it here would reorder it for
 *    them too.
 *
 *  Takes a predicate rather than reading the activity itself: the two signals
 *  live in different places (`activeTurnIds` is a Set in the controller's
 *  closure, `terminal_busy` arrives on the row), and a function reaching for
 *  both could not be tested without a DOM.
 */
export function floatActive(chats, isActive) {
  const active = [];
  const idle = [];
  for (const chat of chats) (isActive(chat) ? active : idle).push(chat);
  return active.concat(idle);
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
  // Conversations that asked the user something or reported a blocker.
  //
  // Derived from the `waiting` bucket of GET /api/orchestrator, which already
  // classifies web conversations -- the browser was receiving this and using
  // it only for the Orchestrator section's badge, so the one signal worth
  // interrupting for never reached the row it belongs to.
  //
  // This replaced an "unread" set keyed on updated_at, which fired whenever a
  // conversation changed while the user was elsewhere. That is output
  // arriving, and renderSupervisor's own comment already said output is not a
  // summons: a marker that fires on every reply is one that gets ignored,
  // which costs the real asks buried among them.
  let waitingIds = new Set();
  // Conversations whose turn has finished and nobody has sent a new prompt
  // since. Distinct from needing an answer: that tracks "is a person
  // required", this tracks "is it done" -- true even for the chat you are
  // currently viewing, and cleared the moment you send into it again rather
  // than by opening it.
  let endedIds = new Set();
  let historyEntries = [];
  // {waiting: [...], working: [...]} from GET /api/orchestrator.
  let orchestrator = {waiting: [], working: []};
  let dragging = null;
  // Stable dot elements keyed by chat-id so they persist across renders
  // instead of being created/destroyed every poll cycle.  Only attributes
  // (class, aria-label, title, animation-delay) change, never the element
  // itself — this prevents the visible flash the old replaceChildren+rebuild
  // pattern caused every time a chat's running state flipped.
  const _chatDots = new Map();

  // "Doing something right now", from the two signals the running dots use.
  // Kept in one place so the order and the dots can never disagree.
  const _isActive = chat =>
    activeTurnIds.has(chat.id) || Boolean(chat.terminal_busy);

  // Persist the order of one section. Sends the whole section rather than a
  // single moved id: the server writes it as one transaction, so a drop cannot
  // half-apply and leave an order the user never chose.
  function commitOrder(container, sectionKey) {
    if (!onReorder) return;
    // Sections that are not collapsed share one container, so selecting every
    // row in it swept Recent into a drag made inside Favourites -- silently
    // converting a recency-ordered section to a manual one. Only the rows
    // belonging to this section are sent.
    //
    // Floated rows are excluded for the same reason, one step further on. An
    // active conversation sits at the top of Favourites only while it is busy,
    // so persisting the order as displayed would write that temporary slot in
    // as its permanent `position` -- a placement the user never chose, applied
    // to whichever conversations happened to be running when somebody dragged
    // an unrelated row. Leaving them out keeps the placement they already had;
    // they drop back into it when they go quiet.
    const ids = [...container.querySelectorAll('.chat-item[data-chat-id]')]
      .filter(node => !sectionKey || node.dataset.section === sectionKey)
      .filter(node => node.dataset.floated !== '1')
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

  // Open upwards when the menu would otherwise hang below the fold. Measured
  // after `.open` is set, because a display:none element has no height to
  // measure -- which is also why this cannot be done in CSS: the decision
  // needs the menu's rendered height against the space left under its trigger.
  //
  // Reported as "the 3 dots isn't working": on a 390x680 phone, a row low in
  // the sidebar opened its menu at y=637 with a height of 391, so it ended at
  // 1028 -- 348px past the bottom of the screen. It was open, and invisible.
  //
  // Flipped only when above is genuinely roomier, so a row near the top (where
  // flipping would push the menu off the *other* edge) keeps opening downward
  // and relies on the max-height cap instead.
  function placeMenu(button, menu) {
    menu.classList.remove('flip-up');
    const rect = menu.getBoundingClientRect();
    const trigger = button.getBoundingClientRect();
    const overflowsBelow = rect.bottom > window.innerHeight;
    const roomAbove = trigger.top;
    const roomBelow = window.innerHeight - trigger.bottom;
    if (overflowsBelow && roomAbove > roomBelow) menu.classList.add('flip-up');
  }

  let renderDeferred = false;

  function closeMenus(restoreFocus = false) {
    const had = document.querySelectorAll('.chat-menu.open').length > 0;
    for (const menu of document.querySelectorAll('.chat-menu.open')) {
      menu.classList.remove('open');
      menu.classList.remove('flip-up');
      const trigger = menu.previousElementSibling;
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }
    if (restoreFocus && openTrigger) openTrigger.focus();
    openTrigger = null;
    // Draw whatever arrived while the menu was open. After the loop above, so
    // the guard in render() sees no open menu and does not defer again.
    if (had && renderDeferred) render();
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
      // Marks a row whose position on screen came from the activity float
      // rather than from the stored order, so commitOrder can leave its
      // placement alone. Read off the DOM because that is what commitOrder
      // walks; recomputing _isActive there could disagree with what was
      // actually painted if the turn ended between render and drop.
      if (_isActive(chat)) item.dataset.floated = '1';

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
      // Persist dot elements across renders: _chatDots survives replaceChildren()
      // because the Map holds references that persist when the DOM rows are wiped
      // and rebuilt.  Each render either reuses an existing dot (updating class,
      // aria-label, title, and animation-delay in-place) or creates a new one and
      // stores it.  Stale entries are removed by pruneDots once the list has
      // finished rendering, so the Map does not grow unbounded.
      //
      // Keyed by list *and* chat, never by chat alone. There are two lists --
      // #chatList for mobile and #chatListDesktop -- and render() loops over
      // both, but a DOM node exists in exactly one place: `title.prepend(dot)`
      // moves the element rather than copying it. With one element per chat the
      // second list to render stole the dot from the first, so whichever list
      // came last in the array was the only one that ever showed an indicator.
      // Measured on a live page before this fix: 0 dots in #chatList against 2
      // in #chatListDesktop, which meant the running indicator was simply
      // absent on the phone layout.
      const dotKey = `${list.id}:${chat.id}`;
      let dot = null;

      // Order matters, and this tier is deliberately first. A conversation
      // that asked a question and then started another turn still needs the
      // person -- ranking "running" above it would hide the one signal the
      // sidebar exists to surface behind the most common state there is.
      if (waitingIds.has(chat.id)) {
        dot = _chatDots.get(dotKey);
        if (!dot) {
          dot = document.createElement('span');
          _chatDots.set(dotKey, dot);
        }
        dot.className = 'chat-needs-answer';
        dot.setAttribute('aria-label', 'Needs an answer');
        dot.title = 'Asked you something, or reported it is stuck';
        dot.removeAttribute('style');
        title.prepend(dot);
      // Running here and running in its own terminal are one state, not two.
      // Pedro's rule: if it is working, just pulse. The grey ring that used to
      // mark terminal work made "something is happening" read two different
      // ways depending on where it happened, which is a distinction the
      // sidebar reader does not have to care about.
      } else if (activeTurnIds.has(chat.id) || chat.terminal_busy) {
        const inTerminal = !activeTurnIds.has(chat.id);
        dot = _chatDots.get(dotKey);
        if (!dot) {
          dot = document.createElement('span');
          _chatDots.set(dotKey, dot);
        }
        dot.className = 'chat-running';
        dot.setAttribute('aria-label', 'Working');
        dot.title = inTerminal
          ? 'Working in the terminal session running this conversation'
          : 'Response in progress';
        // Kept from the original: a shared start offset so every pulse on the
        // page beats together rather than shimmering out of phase.
        dot.style.animationDelay = `-${(Date.now() % 1200) / 1000}s`;
        title.prepend(dot);
      } else if (endedIds.has(chat.id)) {
        dot = _chatDots.get(dotKey);
        if (dot) {
          dot.className = 'chat-ended';
          dot.setAttribute('aria-label', 'Finished responding');
          dot.title = 'Finished responding';
          dot.removeAttribute('style');
        } else {
          dot = document.createElement('span');
          dot.className = 'chat-ended';
          dot.setAttribute('aria-label', 'Finished responding');
          dot.title = 'Finished responding';
          _chatDots.set(dotKey, dot);
        }
        title.prepend(dot);
      } else if (!chat.queued) {
        dot = _chatDots.get(dotKey);
        if (dot) {
          dot.className = 'chat-free';
          dot.setAttribute('aria-label', 'Nothing outstanding');
          dot.title = 'Nothing running or queued for this conversation';
          dot.removeAttribute('style');
        } else {
          dot = document.createElement('span');
          dot.className = 'chat-free';
          dot.setAttribute('aria-label', 'Nothing outstanding');
          dot.title = 'Nothing running or queued for this conversation';
          _chatDots.set(dotKey, dot);
        }
        title.prepend(dot);
      }

      if (chat.queued) {
        const held = chat.queued_held || 0;
        const queued = document.createElement('span');
        queued.className = 'chat-queued';
        // dataset, not a class swap: styling stays in CSS (mirrors
        // .queue-bar[data-held="yes"] in the open-conversation panel) rather
        // than being decided here.
        queued.dataset.held = held ? 'yes' : 'no';
        queued.textContent = String(chat.queued);
        queued.setAttribute(
          'aria-label',
          held
            ? `${held} of ${chat.queued} queued prompts held — needs Send or Discard`
            : `${chat.queued} prompts queued`,
        );
        queued.title = held
          ? `${held} held (its predecessor's turn failed) of ${chat.queued} waiting`
          : `${chat.queued} prompt${chat.queued > 1 ? 's' : ''} waiting to send`;
        title.append(queued);
      }
      // Voice-mode chats get a mic icon on the right side, after queued but
      // before degraded (degraded is the highest-trust warning and stays last).
      if (chat.voice_mode) {
        const mic = document.createElement('span');
        mic.className = 'chat-voice';
        mic.textContent = '🎙';
        mic.setAttribute('aria-label', 'Voice conversation');
        mic.title = 'Voice conversation';
        title.append(mic);
      }
      // Independent of the leading-dot chain above (running/terminal-busy/
      // needs-answer/running/ended/free): a chat can be actively running and
      // still carry a degraded flag from an earlier turn's silent write
      // failure -- the two facts do not exclude each other, so this is a
      // second, trailing marker rather than another tier in that chain.
      if (chat.degraded) {
        const warn = document.createElement('span');
        warn.className = 'chat-degraded';
        warn.textContent = '⚠';
        warn.setAttribute('aria-label', 'Some state for this conversation may be stale');
        warn.title = chat.degraded_reason
          || 'A background write failed for this conversation; check the server logs.';
        title.append(warn);
      }
      // The terminal session this conversation is attached to, under the name
      // that session calls itself. The sidebar's "CLI Sessions" section can
      // never show these: /api/sessions drops any session already linked to a
      // chat, so once every session has been opened in the console that
      // section renders nothing and the registry's names disappear from the
      // page entirely. The row that represents the session is the honest
      // place for its name.
      //
      // The same name on several rows is not a duplicate -- those chats share
      // one terminal, and this is the only thing on the page that says so.
      // The server omits it when the title already contains it.
      if (chat.session_name) {
        const tag = document.createElement('span');
        tag.className = 'chat-session-name';
        tag.textContent = chat.session_name;
        tag.title = `Running in terminal session "${chat.session_name}"`;
        title.append(tag);
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
        // in the list. The orchestrator list is fetched on click rather than
        // built here, so a sidebar render costs no request.
        makeButton('Add to orchestrator', 'add-to-orchestrator', chat.id, chat.title),
        makeButton(
          chat.archived ? 'Restore' : 'Archive',
          chat.archived ? 'restore' : 'archive',
          chat.id,
          chat.title,
        ),
      );
      // Standby / Wake buttons — only shown when the chat has a linked session.
      // Inserted before the delete separator so they sit with routing actions.
      if (chat.session_id) {
        menu.append(
          makeButton('Standby', 'standby', chat.id, chat.title),
          makeButton('Wake', 'wake', chat.id, chat.title),
        );
        // Standby hidden when already on standby; Wake hidden when not on standby.
        menu.querySelectorAll('[data-action="standby"]').forEach(btn => {
          btn.hidden = Boolean(chat.standby_reason);
        });
        menu.querySelectorAll('[data-action="wake"]').forEach(btn => {
          btn.hidden = !chat.standby_reason;
        });
      }
      // Delete is irreversible, so it is set apart rather than sitting flush
      // against the routing controls above.
      menu.append(
        separator,
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

  // Drop dot elements for rows this list no longer shows, so _chatDots stays
  // bounded and a stale-class dot cannot come back when a chat is deleted or
  // filtered away.
  //
  // Runs once per list, after every section of that list has rendered. It used
  // to run at the end of renderSection, which meant each section pruned away
  // the entries belonging to the sections that had not rendered yet -- with
  // Favourites, Recent and Archived, rendering Favourites deleted Recent's
  // dots, which were then rebuilt from scratch moments later. Elements that
  // are rebuilt lose the animation-delay continuity the map exists to
  // preserve, so the cleanup was quietly defeating its own purpose.
  function pruneDots(list) {
    const onScreen = new Set(
      [...list.querySelectorAll('.chat-item[data-chat-id]')]
        .map(el => `${list.id}:${el.dataset.chatId}`)
    );
    const prefix = `${list.id}:`;
    _chatDots.forEach((_, key) => {
      if (key.startsWith(prefix) && !onScreen.has(key)) _chatDots.delete(key);
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
    heading.className = 'chat-section-label orchestrator-label';
    heading.type = 'button';
    heading.dataset.action = 'open-orchestrator';
    heading.appendChild(document.createTextNode('Orchestrator'));
    if (waiting.length) {
      const badge = document.createElement('span');
      badge.className = 'orchestrator-badge';
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
    open.className = 'chat-action orchestrator-open';
    open.dataset.action = 'open-orchestrator';
    open.textContent = '↗';
    open.title = 'Open the orchestrator';
    open.setAttribute('aria-label', 'Open the orchestrator');
    heading.appendChild(open);

    if (waiting.length || updated.length) {
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'chat-action orchestrator-clear';
      clear.dataset.action = 'clear-orchestrator';
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
      item.className = 'chat-item orchestrator-item'
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
      dot.className = 'orchestrator-dot';
      dot.setAttribute('aria-label', 'Waiting for you');
      title.append(dot, document.createTextNode(entry.title || entry.id));
      // A "?" only where there is a question to answer. Every row in this
      // section is waiting on you, so a mark on all of them would say nothing;
      // what it distinguishes is the ones that asked something from the ones
      // that failed, stalled, or simply stopped talking.
      if (entry.question) {
        const asks = document.createElement('span');
        asks.className = 'orchestrator-asks';
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
      dismiss.className = 'chat-action orchestrator-dismiss';
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
      note.className = 'orchestrator-note';
      note.textContent = `${quiet.join(' · ')} · nothing needed`;
      list.appendChild(note);
    }
  }

  function render(chats = lastChats, currentId = lastCurrentId) {
    lastChats = chats;
    lastCurrentId = currentId;
    // Not while a menu is open. This rebuilds every row, so an open ⋯ menu is
    // destroyed along with the row it hangs off -- and refreshChats polls
    // every 6 seconds, so a menu survived somewhere between 0 and 6 seconds
    // and a poll landing just after the click made it look as though clicking
    // did nothing at all. Measured on the live page: open, then gone inside
    // 2 seconds with the trigger detached from the document.
    //
    // Deferred rather than dropped: the list keeps its newest data in
    // lastChats and draws it the moment the menu closes, so nothing is lost
    // and the sidebar is at most one menu-interaction stale.
    if (document.querySelector('.chat-menu.open')) {
      renderDeferred = true;
      return;
    }
    renderDeferred = false;
    // Hide voice temp child chats — ephemeral, rendered inside the overlay,
    // and clicking them would replace the parent, which is wrong.
    const visible = chats.filter(c => !c.is_temporary);
    const filtered = filterChats(visible, query);
    const groups = groupChats(filtered);
    // Favourites only, deliberately. Recent is already recency-ordered, so a
    // running conversation is near the top there by definition, and Archived
    // is a place you go to look something up rather than to watch it work.
    groups.pinned = floatActive(groups.pinned, _isActive);
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
      renderSupervisor(list, orchestrator);
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
      pruneDots(list);

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
    orchestrator = {
      waiting: Array.isArray(state?.waiting) ? state.waiting : [],
      working: Array.isArray(state?.working) ? state.working : [],
    };
    // The same feed drives the per-row dot, but NOT all of it.
    //
    // `waiting` is the attention feed, and it holds three reasons: "asks",
    // "blocked" and "done". The first two need a person. The third does not --
    // it is finished work, which earns the quiet .chat-ended arrow and not a
    // summons. Measured against the live database on 2026-09-21: 61 of 64
    // conversations were in this bucket and 54 of them were "done", so taking
    // the bucket whole marked almost every row and reproduced the noise this
    // change exists to remove, in a new colour.
    //
    // Only `kind === "chat"` entries: the bucket also carries CLI/terminal
    // sessions, whose ids are session ids and would never match a chat id --
    // filtering by kind says that on purpose rather than relying on the sets
    // happening not to collide.
    waitingIds = new Set(
      orchestrator.waiting
        .filter(entry => entry && entry.kind === 'chat' && entry.id
                      && entry.reason !== 'done')
        .map(entry => entry.id),
    );
    render();
  }

  function setActiveTurns(ids) {
    const next = new Set(ids || []);
    if (next.size === activeTurnIds.size
        && [...next].every(id => activeTurnIds.has(id))) return;
    activeTurnIds = next;
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
    if (button) {
      const action = button.dataset.action;
      if (action === 'open') return onSelect(button.dataset.chatId);
      if (action === 'resume-cli') return onResumeCli(button.dataset.sessionId);
      if (action === 'open-orchestrator') return onOpenSupervisor?.();
      if (action === 'add-to-orchestrator') {
        return onAddToSupervisor?.(button.dataset.chatId, button);
      }
      if (action === 'clear-orchestrator') return onClearSupervisor?.();
      if (action === 'dismiss-agent') {
        // Sits inside the row, whose own click opens the conversation. Without
        // this the row would open the very chat you asked to stop being shown.
        event.stopPropagation();
        const {agentKind, agentId} = button.dataset;
        return onDismissAgent?.(agentKind, agentId);
      }
      if (action === 'jump-agent') {
        // A orchestrator row is a pointer at something listed elsewhere: a web
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
          placeMenu(button, menu);
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
      return;
    }
    // No button — fall back to opening the row itself if clicked on a chat-item.
    const row = event.target.closest('.chat-item[data-chat-id]');
    if (row) return onSelect(row.dataset.chatId);
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
    setEnded,
    clearEnded,
    setHistory,
    setSupervisor,
  };
}
