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

// ── Auth-aware UI ───────────────────────────────────────────────────────────
// Fetches /auth/me, renders a user chip + logout button, injects the admin
// "Users" nav item for admins, and redirects to /login on 401 for UI pages.

async function fetchAuthState() {
  try {
    const res = await fetch('/auth/me', { credentials: 'same-origin' });
    if (res.status === 401) return { authenticated: false, sso: true };
    if (!res.ok) return { authenticated: false, sso: false };
    const data = await res.json();
    return { authenticated: !!data.email, sso: true, ...data };
  } catch (_e) { return { authenticated: false, sso: false }; }
}

function renderUserChip(me) {
  const host = document.querySelector('.topbar-right') || document.getElementById('topbar-right');
  if (!host || !me || !me.email) return;
  if (host.querySelector('.user-chip')) return;
  const initial = (me.name || me.email || '?').trim().charAt(0).toUpperCase();
  const chip = document.createElement('div');
  chip.className = 'user-chip';
  chip.innerHTML = `
    <button class="user-chip-btn" aria-haspopup="true" aria-expanded="false" title="${me.email}">
      ${me.picture ? `<img src="${me.picture}" alt="" class="user-chip-avatar">` : `<span class="user-chip-avatar user-chip-avatar-fallback">${initial}</span>`}
      <span class="user-chip-email">${me.email}</span>
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <div class="user-chip-menu" hidden>
      <div class="user-chip-menu-hd">
        <div style="font-size:12px;font-weight:600">${me.name || me.email}</div>
        <div style="font-size:11px;color:var(--text-3)">${me.email}</div>
        ${me.is_admin ? '<div style="font-size:10px;margin-top:4px"><span class="badge badge-blue">Admin</span></div>' : ''}
      </div>
      ${me.is_admin ? '<a href="/users" class="user-chip-menu-item">Manage users</a>' : ''}
      <a href="/auth/logout" class="user-chip-menu-item">Sign out</a>
    </div>`;
  host.appendChild(chip);
  const btn  = chip.querySelector('.user-chip-btn');
  const menu = chip.querySelector('.user-chip-menu');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !menu.hasAttribute('hidden');
    if (open) menu.setAttribute('hidden', ''); else menu.removeAttribute('hidden');
    btn.setAttribute('aria-expanded', String(!open));
  });
  document.addEventListener('click', (e) => {
    if (!chip.contains(e.target)) menu.setAttribute('hidden', '');
  });
}

function injectAdminNav(me) {
  if (!me || !me.is_admin) return;
  const nav = document.querySelector('.sidebar-nav');
  if (!nav || nav.querySelector('a[href="/users"]')) return;
  const firstDivider = nav.querySelector('.sidebar-divider');
  const frag = document.createDocumentFragment();
  const divider = document.createElement('div'); divider.className = 'sidebar-divider'; frag.appendChild(divider);
  const label = document.createElement('span'); label.className = 'sidebar-section'; label.textContent = 'Admin'; frag.appendChild(label);
  const link = document.createElement('a');
  link.href = '/users'; link.className = 'nav-item';
  link.innerHTML = `<svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg> Users`;
  if (window.location.pathname.startsWith('/users')) link.classList.add('active');
  frag.appendChild(link);
  if (firstDivider) nav.insertBefore(frag, firstDivider); else nav.appendChild(frag);
}

function installFetchAuthGuard() {
  if (window.__arbiterFetchGuardInstalled) return;
  window.__arbiterFetchGuardInstalled = true;
  const orig = window.fetch;
  window.fetch = async function(...args) {
    const res = await orig.apply(this, args);
    try {
      const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
      // Only redirect on UI pages; leave /v1/* API calls untouched.
      if (res.status === 401 && !url.startsWith('/v1/') && !url.startsWith('/auth/')) {
        const ct = res.headers.get('content-type') || '';
        if (ct.includes('application/json')) {
          const next = encodeURIComponent(window.location.pathname + window.location.search);
          window.location.href = '/login?next=' + next;
        }
      }
    } catch (_e) {}
    return res;
  };
}

async function initAuthUI() {
  installFetchAuthGuard();
  const me = await fetchAuthState();
  if (me && me.authenticated) {
    renderUserChip(me);
    injectAdminNav(me);
  }
}

// ── DOMContentLoaded init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  applyTheme(getSavedTheme());
  initAuthUI();
});
