// Transcript rendering, drafts, and deterministic SSE stream lifecycle.

const STREAM_LABELS = {
  ready: 'Ready',
  connecting: 'Connecting…',
  thinking: 'Thinking…',
  retrying: 'Retrying…',
  responding: 'Responding…',
  stopped: 'Stopped',
  failed: 'Failed',
};
const ACTIVE_STATES = new Set(['connecting', 'thinking', 'retrying', 'responding']);

export function parseTimestamp(iso) {
  if (!iso) return null;
  const value = /Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function renderSafeText(container, text) {
  container.replaceChildren();
  String(text).split(/```/).forEach((part, index) => {
    if (index % 2) {
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      const firstNewline = part.indexOf('\n');
      code.textContent = firstNewline >= 0 ? part.slice(firstNewline + 1) : part;
      pre.appendChild(code);
      container.appendChild(pre);
    } else if (part) {
      container.appendChild(document.createTextNode(part));
    }
  });
}

export function createConversationController(dependencies) {
  const {
    state,
    elements,
    apiFetch,
    storageGet,
    storageSet,
    storageRemove,
    showToast,
    onChatLoaded,
    refreshChats,
  } = dependencies;

  let abortController = null;
  let lastAttempt = null;
  let following = true;

  function draftKey(chatId) { return `wc_draft_${chatId}`; }

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

  function setStreamState(next, detail = '') {
    state.streamState = next;
    const label = detail || STREAM_LABELS[next];
    elements.runState.textContent = label;
    elements.runState.dataset.state = next;
    elements.composerStatus.textContent = next === 'ready' ? '' : label;
    const active = ACTIVE_STATES.has(next);
    elements.sendButton.classList.toggle('stop', active);
    elements.sendButton.textContent = active ? '■' : '➜';
    elements.sendButton.setAttribute('aria-label', active ? 'Stop response' : 'Send message');
    elements.composerInput.disabled = active;
    elements.retryButton.style.display = ['failed', 'stopped'].includes(next) && lastAttempt ? 'inline' : 'none';
  }

  function autoResize() {
    elements.composerInput.style.height = 'auto';
    elements.composerInput.style.height = `${Math.min(elements.composerInput.scrollHeight, 150)}px`;
  }

  function isNearBottom() {
    const area = elements.messages;
    return area.scrollHeight - area.scrollTop - area.clientHeight < 96;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      elements.messages.scrollTop = elements.messages.scrollHeight;
      following = true;
      elements.jumpButton.hidden = true;
    });
  }

  function followNewContent(shouldFollow) {
    if (shouldFollow || following) scrollToBottom();
    else elements.jumpButton.hidden = false;
  }

  function createMessage(role, content, time) {
    const row = document.createElement('article');
    row.className = `message ${role}`;
    const avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.textContent = role === 'user' ? 'U' : '✶';
    avatar.setAttribute('aria-hidden', 'true');
    const body = document.createElement('div');
    body.className = 'message-body';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    renderSafeText(bubble, content || '');
    body.appendChild(bubble);
    if (time) {
      const stamp = document.createElement('div');
      stamp.className = 'msg-time';
      stamp.textContent = formatTime(time);
      stamp.title = parseTimestamp(time)?.toLocaleString() || '';
      body.appendChild(stamp);
    }
    if (role === 'assistant' && content) {
      const tools = document.createElement('div');
      tools.className = 'message-tools';
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(content);
          copy.textContent = 'Copied';
          setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
        } catch {
          showToast('Could not copy response', 'error');
        }
      });
      tools.appendChild(copy);
      body.appendChild(tools);
    }
    row.append(avatar, body);
    return row;
  }

  function renderMessages(messages) {
    elements.messages.replaceChildren();
    if (!messages.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      const strong = document.createElement('strong');
      strong.textContent = 'This workspace is ready';
      const text = document.createElement('p');
      text.textContent = 'Ask Claude to inspect, explain, or change something.';
      empty.append(strong, text);
      elements.messages.appendChild(empty);
    } else {
      messages.forEach(message => elements.messages.appendChild(
        createMessage(message.role, message.content, message.created_at)
      ));
    }
    scrollToBottom();
  }

  function persistDraft() {
    if (state.currentChat?.id) storageSet(draftKey(state.currentChat.id), elements.composerInput.value);
  }

  function restoreDraft(chatId) {
    elements.composerInput.value = storageGet(draftKey(chatId)) || '';
    autoResize();
  }

  async function selectChat(chat) {
    if (ACTIVE_STATES.has(state.streamState) && chat.id !== state.currentChat?.id) {
      showToast('Stop the current response before switching conversations.');
      return false;
    }
    persistDraft();
    const response = await apiFetch(`/api/chats/${encodeURIComponent(chat.id)}`);
    if (!response.ok) throw new Error('Could not open conversation');
    const data = await response.json();
    state.currentChat = data.chat;
    renderMessages(data.messages || []);
    restoreDraft(chat.id);
    setStreamState('ready');
    onChatLoaded(data.chat);
    elements.composerInput.focus();
    return true;
  }

  async function refreshCurrent() {
    if (!state.currentChat) return;
    const response = await apiFetch(`/api/chats/${encodeURIComponent(state.currentChat.id)}`);
    if (!response.ok) return;
    const data = await response.json();
    state.currentChat = data.chat;
    renderMessages(data.messages || []);
    onChatLoaded(data.chat);
  }

  async function send(forcedContent) {
    if (ACTIVE_STATES.has(state.streamState)) return stop();
    const chatId = state.currentChat?.id;
    const content = (forcedContent ?? elements.composerInput.value).trim();
    if (!chatId || !content) return;

    const model = elements.modelPicker?.value || null;
    lastAttempt = {chatId, content, model};
    elements.retryButton.style.display = 'none';
    const empty = elements.messages.querySelector('.empty-state');
    if (empty) empty.remove();
    elements.messages.appendChild(createMessage('user', content, new Date().toISOString()));
    scrollToBottom();
    setStreamState('connecting');
    abortController = new AbortController();

    let assistantRow = null;
    let assistantBubble = null;
    let fullText = '';
    let accepted = false;
    let succeeded = false;

    try {
      const response = await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/stream`, {
        method: 'POST',
        signal: abortController.signal,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content, model}),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || 'Could not start response');
      }
      accepted = true;
      elements.composerInput.value = '';
      storageRemove(draftKey(chatId));
      autoResize();
      setStreamState('thinking');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let streamCompleted = false;
      streamLoop: while (true) {
        const result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, {stream: true});
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop();
        for (const block of blocks) {
          const line = block.split('\n').find(value => value.startsWith('data: '));
          if (!line) continue;
          let event;
          try { event = JSON.parse(line.slice(6)); } catch { continue; }
          if (event.type === 'text') {
            const shouldFollow = isNearBottom();
            if (!assistantRow) {
              assistantRow = createMessage('assistant', '', '');
              assistantBubble = assistantRow.querySelector('.msg-bubble');
              elements.messages.appendChild(assistantRow);
            }
            fullText += event.content || '';
            renderSafeText(assistantBubble, fullText);
            setStreamState('responding');
            followNewContent(shouldFollow);
          } else if (event.type === 'status' && event.status === 'api_retry') {
            const delay = event.retry_delay_ms ? Math.ceil(event.retry_delay_ms / 1000) : null;
            const detail = `Retry ${event.attempt || '?'}/${event.max_retries || '?'}${delay ? ` in ${delay}s` : ''}…`;
            setStreamState('retrying', detail);
          } else if (event.type === 'error') {
            throw new Error(event.error || 'Claude failed');
          } else if (event.type === 'done') {
            streamCompleted = true;
            break streamLoop;
          }
        }
      }
      if (!streamCompleted) throw new Error('Response stream ended before completion');
      succeeded = true;
    } catch (error) {
      if (error.name === 'AbortError') {
        if (!fullText) {
          elements.composerInput.value = content;
          storageSet(draftKey(chatId), content);
          autoResize();
        }
        setStreamState('stopped');
        showToast('Response stopped');
      } else {
        if (!accepted || !fullText) {
          elements.composerInput.value = content;
          storageSet(draftKey(chatId), content);
          autoResize();
        }
        setStreamState('failed');
        showToast(error.message, 'error');
      }
    } finally {
      abortController = null;
      if (assistantRow && !fullText) assistantRow.remove();
      await refreshChats();
      if (succeeded) {
        lastAttempt = null;
        if (elements.modelPicker) elements.modelPicker.value = '';
        setStreamState('ready');
        await refreshCurrent();
      }
      elements.composerInput.focus();
    }
  }

  function stop() {
    if (abortController) abortController.abort();
  }

  function retry() {
    if (lastAttempt && state.currentChat?.id === lastAttempt.chatId) {
      if (elements.modelPicker) elements.modelPicker.value = lastAttempt.model || '';
      send(lastAttempt.content);
    }
  }

  function destroy() {
    if (abortController) abortController.abort();
  }

  elements.composerInput.addEventListener('input', () => {
    autoResize();
    persistDraft();
  });
  elements.composerInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  elements.sendButton.addEventListener('click', () => send());
  elements.retryButton.addEventListener('click', retry);
  elements.messages.addEventListener('scroll', () => {
    following = isNearBottom();
    elements.jumpButton.hidden = following;
  }, {passive: true});
  elements.jumpButton.addEventListener('click', scrollToBottom);

  setStreamState('ready');
  return {selectChat, send, stop, retry, restoreDraft, persistDraft, refreshCurrent, destroy, setStreamState};
}
