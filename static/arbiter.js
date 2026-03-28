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

// ── DOMContentLoaded init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  applyTheme(getSavedTheme());
});
