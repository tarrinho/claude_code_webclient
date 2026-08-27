// Shared HTTP helpers for the WebConsole frontend.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function apiFetch(url, options = {}) {
  const response = await fetch(url, {...options, credentials: 'same-origin'});
  if (response.status === 401) {
    window.location.assign('/login');
    throw new ApiError('Session expired', 401);
  }
  return response;
}

export async function jsonRequest(url, options = {}) {
  const response = await apiFetch(url, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(data.error || 'Request failed', response.status);
  }
  return response.status === 204 ? null : response.json();
}

export async function downloadMarkdown(chat) {
  const response = await apiFetch(`/api/chats/${encodeURIComponent(chat.id)}/export`);
  if (!response.ok) {
    throw new ApiError('Could not export conversation', response.status);
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  const disposition = response.headers.get('content-disposition') || '';
  const match = disposition.match(/filename="([^"]+)"/i);
  anchor.href = url;
  anchor.download = match ? match[1] : `${chat.title}.md`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
