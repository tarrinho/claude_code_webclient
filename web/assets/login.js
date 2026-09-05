// Login page scripts — loaded as external script to avoid CSP nonce issues.

document.addEventListener('DOMContentLoaded', function() {
  var form = document.getElementById('loginForm');
  if (form) form.addEventListener('submit', handleLogin);
  var toggle = document.getElementById('themeToggle');
  if (toggle) toggle.addEventListener('click', toggleTheme);
  _initChangelogVersion();
});

var _changelogEl = null;
var _changelogData = null;
var _changelogPopover = null;

function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function _initChangelogVersion() {
  var verEl = _changelogEl || document.getElementById('loginVer');
  if (!verEl || _changelogPopover) return;
  _changelogEl = verEl;
  verEl.textContent = 'loading…';
  // Fetch once, cache for both surfaces
  _fetchChangelogData(function(data) {
    if (data) {
      _changelogData = data;
      verEl.textContent = 'WebConsole · changelog';
    } else {
      verEl.textContent = 'WebConsole';
    }
  });
  verEl.addEventListener('click', function(e) { e.stopPropagation(); _toggleChangelogPopover(verEl); });
  verEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleChangelogPopover(verEl); }
  });
  var escFn = function(e) {
    if (e.key === 'Escape') { _closeChangelogPopover(); }
  };
  verEl.addEventListener('keydown', escFn);
  verEl._changelogEscFn = escFn;
}

function _fetchChangelogData(cb) {
  if (_changelogData) { cb(_changelogData); return; }
  fetch('/api/changelog')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _changelogData = Array.isArray(data) ? data : [];
      if (cb) cb(_changelogData.length ? _changelogData : null);
    })
    .catch(function() { if (cb) cb(null); });
}

function _toggleChangelogPopover(anchorEl) {
  if (_changelogPopover) { _closeChangelogPopover(); return; }
  if (!_changelogData) { _fetchChangelogData(function() { _buildChangelogPopover(anchorEl); }); }
  else { _buildChangelogPopover(anchorEl); }
}

function _buildChangelogPopover(anchorEl) {
  if (!_changelogData) return;
  _changelogPopover = document.createElement('div');
  _changelogPopover.className = 'changelog-popover';
  _changelogPopover.setAttribute('role', 'dialog');
  _changelogPopover.setAttribute('aria-label', 'Changelog');
  var heading = document.createElement('h3');
  heading.textContent = 'Changelog';
  _changelogPopover.appendChild(heading);
  _changelogData.forEach(function(entry) {
    var chapter = document.createElement('div');
    chapter.className = 'changelog-chapter';
    var top = document.createElement('div');
    top.style.cssText = 'margin-bottom:6px';
    top.innerHTML = '<span class="cl-ver">' + esc(entry.version) + '</span><span class="cl-date">' + esc(entry.date) + '</span>';
    chapter.appendChild(top);
    entry.sections.forEach(function(sec) {
      var secHead = document.createElement('div');
      secHead.className = 'cl-section';
      secHead.textContent = sec.type;
      chapter.appendChild(secHead);
      var ul = document.createElement('ul');
      ul.className = 'cl-items';
      sec.items.forEach(function(item) {
        var li = document.createElement('li');
        li.textContent = item;
        ul.appendChild(li);
      });
      chapter.appendChild(ul);
    });
    _changelogPopover.appendChild(chapter);
  });
  // Backdrop
  var backdrop = document.createElement('div');
  backdrop.className = 'changelog-changelog-backdrop';
  backdrop.addEventListener('click', function() { _closeChangelogPopover(); });
  document.body.appendChild(backdrop);
  document.body.appendChild(_changelogPopover);
  // Position under anchor
  var rect = anchorEl.getBoundingClientRect();
  var w = Math.min(420, window.innerWidth - 20);
  _changelogPopover.style.width = w + 'px';
  _changelogPopover.style.top = (rect.bottom + 4) + 'px';
  _changelogPopover.style.right = (window.innerWidth - rect.right) + 'px';
  _changelogPopover._backdrop = backdrop;
}

function _closeChangelogPopover() {
  if (_changelogPopover) {
    if (_changelogPopover._backdrop) _changelogPopover._backdrop.remove();
    _changelogPopover.remove();
    _changelogPopover = null;
  }
  if (_changelogEl && _changelogEl._changelogEscFn) {
    _changelogEl.removeEventListener('keydown', _changelogEl._changelogEscFn);
  }
}

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