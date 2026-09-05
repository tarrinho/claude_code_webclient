// Shared HTTP helpers for the WebConsole frontend.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function redirectToLogin() {
  if (window.__webConsoleRedirecting) return;
  window.__webConsoleRedirecting = true;
  window.location.replace('/login');
}

function getCsrfToken() {
  var m = document.cookie.match(/(?:^|;\s*)wc_csrf=([^;]*)/);
  return m ? m[1] : '';
}

export async function apiFetch(url, options = {}) {
  var csrf = getCsrfToken();
  if (csrf) {
    options.headers = { ...options.headers, 'X-CSRF-Token': csrf };
  }
  const response = await fetch(url, { ...options, credentials: 'same-origin' });

  // Session expiry: 401 from the app returns JSON, 303 from AuthMiddleware
  // redirects to /login which Caddy may 404 (no cookie session). Catch both.
  if (response.status === 401 || response.status === 303) {
    redirectToLogin();
    throw new ApiError('Session expired', response.status);
  }
  return response;
}

export async function jsonRequest(url, options = {}) {
  const response = await apiFetch(url, options);
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      // Body may be HTML (Caddy error page, proxy intercept).
      // Try to extract the body text for a readable message.
      try {
        const text = await response.text();
        // If it's an HTML error page, pull out a <title> or first <h1>.
        const titleMatch = text.match(/<title[^>]*>([^<]+)<\/title>/i);
        const h1Match = text.match(/<h1[^>]*>([^<]+)<\/h1>/i);
        const snippet = h1Match ? h1Match[1] : titleMatch ? titleMatch[1] : text.slice(0, 200);
        if (snippet.trim() && /<|html|caddy/i.test(snippet)) {
          throw new ApiError('Could not complete request (server returned error page)', response.status);
        }
        throw new ApiError(snippet.trim() || 'Request failed', response.status);
      } catch { /* ignore */ }
    }
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
