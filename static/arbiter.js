/* Arbiter — Shared JS */

// ── Theme management ────────────────────────────────────────────────────────
const THEME_KEY = 'arbiter-theme';

function getSystemTheme() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function getSavedTheme() {
  return localStorage.getItem(THEME_KEY) || 'system';
}
function resolveTheme(pref) {
  return pref === 'system' ? getSystemTheme() : pref;
}
function applyTheme(pref) {
  const resolved = resolveTheme(pref);
  document.documentElement.setAttribute('data-theme', resolved);
  const btn = document.getElementById('theme-btn');
  if (btn) {
    const icon = resolved === 'dark' ? '☀️' : '🌙';
    const label = pref === 'system' ? 'System' : (resolved === 'dark' ? 'Light mode' : 'Dark mode');
    btn.querySelector('.theme-icon').textContent = icon;
    btn.querySelector('.theme-label').textContent = label;
  }
}
function toggleTheme() {
  const cur = getSavedTheme();
  const next = cur === 'dark' ? 'light' : cur === 'light' ? 'system' : 'dark';
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}

// Apply theme immediately to avoid flash
applyTheme(getSavedTheme());
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (getSavedTheme() === 'system') applyTheme('system');
});

// ── Sidebar toggle (mobile) ─────────────────────────────────────────────────
function initSidebar() {
  const sidebar = document.getElementById('sidebar');
  const toggle  = document.getElementById('sidebar-toggle');
  if (!sidebar || !toggle) return;
  toggle.addEventListener('click', () => sidebar.classList.toggle('open'));
  document.addEventListener('click', e => {
    if (sidebar.classList.contains('open') && !sidebar.contains(e.target) && e.target !== toggle) {
      sidebar.classList.remove('open');
    }
  });
}

// ── Toast notifications ─────────────────────────────────────────────────────
function toast(msg, type = 'info', duration = 3000) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<span>${msg}</span>`;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(20px)'; setTimeout(() => el.remove(), 200); }, duration);
}

// ── Shared badge helpers ────────────────────────────────────────────────────
function statusBadge(status) {
  const map = { healthy: ['badge-green','Healthy'], degraded: ['badge-yellow','Degraded'], unavailable: ['badge-red','Unavailable'] };
  const [cls, label] = map[status] || ['badge-gray','Unknown'];
  return `<span class="badge ${cls}"><span class="badge-dot"></span>${label}</span>`;
}
function scoreBar(score) {
  const pct = Math.max(0, Math.min(100, Math.round((score + 1) / 2 * 100)));
  const color = pct >= 60 ? 'var(--green)' : pct >= 30 ? 'var(--yellow)' : 'var(--red)';
  return `<div class="score-wrap"><div class="score-track"><div class="score-fill" style="width:${pct}%;background:${color}"></div></div><span style="font-size:10px;color:${color};min-width:26px">${pct}%</span></div>`;
}
function miniBar(used, limit) {
  if (!limit) return `<span style="font-size:11px;color:var(--text-3)">–</span>`;
  const p = Math.min(100, Math.round(used / limit * 100));
  const color = p < 50 ? 'var(--green)' : p < 80 ? 'var(--yellow)' : 'var(--red)';
  return `<div class="mini-bar-wrap"><div class="mini-bar-meta"><span>${used.toLocaleString()}</span><span>${limit.toLocaleString()}</span></div><div class="mini-track"><div class="mini-fill" style="width:${p}%;background:${color}"></div></div></div>`;
}

// ── Auth / user info ─────────────────────────────────────────────────────────
async function initAuth() {
  try {
    const res = await fetch('/auth/me');
    const data = await res.json();

    if (!data.enabled) return; // OAuth not configured — no login required

    if (!data.authenticated) {
      // Redirect to login (preserve current URL so we can come back)
      window.location.replace('/login?next=' + encodeURIComponent(window.location.pathname));
      return;
    }

    // Inject user info into sidebar footer
    const footer = document.querySelector('.sidebar-footer');
    if (!footer) return;

    const avatar = data.picture
      ? `<img src="${data.picture}" style="width:24px;height:24px;border-radius:50%;object-fit:cover;" alt="" />`
      : `<span style="width:24px;height:24px;border-radius:50%;background:var(--accent);display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:#fff;">${(data.name||data.email||'?')[0].toUpperCase()}</span>`;

    const userEl = document.createElement('div');
    userEl.className = 'user-info';
    userEl.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 12px;border-top:1px solid var(--border-1);margin-top:8px;';
    userEl.innerHTML = `
      ${avatar}
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;font-weight:500;color:var(--text-1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${data.name || data.email}</div>
        <div style="font-size:10px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${data.email}</div>
      </div>
      <a href="/auth/logout" title="Sign out" style="color:var(--text-3);text-decoration:none;flex-shrink:0;" onmouseenter="this.style.color='var(--red)'" onmouseleave="this.style.color='var(--text-3)'">
        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
      </a>
    `;
    footer.appendChild(userEl);
  } catch (e) {
    // Network error — don't block the page
  }
}

// ── DOMContentLoaded init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  applyTheme(getSavedTheme());
  initAuth();
});
