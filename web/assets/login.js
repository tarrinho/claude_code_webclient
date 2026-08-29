// Login page scripts — loaded as external script to avoid CSP nonce issues.

document.addEventListener('DOMContentLoaded', function() {
  var form = document.getElementById('loginForm');
  if (form) form.addEventListener('submit', handleLogin);
  var toggle = document.getElementById('themeToggle');
  if (toggle) toggle.addEventListener('click', toggleTheme);
});

function toggleTheme() {
  const html = document.documentElement;
  const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  try { localStorage.setItem('wc_theme', next); } catch (e) {}
}
(function() {
  try {
    var saved = localStorage.getItem('wc_theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (e) {}
})();

async function handleLogin(e) {
  e.preventDefault();
  var btn = document.getElementById('submitBtn');
  var errEl = document.getElementById('errorMsg');
  var user = document.getElementById('username').value;
  var pass = document.getElementById('password').value;

  btn.disabled = true;
  btn.textContent = 'Signing in...';
  errEl.classList.remove('visible');

  try {
    var resp = await fetch('/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: user, password: pass}),
      credentials: 'same-origin',
    });
    var data = await resp.json();
    if (resp.ok) {
      window.location.href = '/';
    } else {
      errEl.textContent = data.error || 'Login failed';
      errEl.classList.add('visible');
    }
  } catch (err) {
    errEl.textContent = 'Network error — check connection';
    errEl.classList.add('visible');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
}