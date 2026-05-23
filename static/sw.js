/* Arbiter — Service Worker
 * -----------------------------------------------------------------------------
 * Strategy:
 *   - HTML pages          → network-first, fall back to cached shell when offline.
 *   - Static assets       → stale-while-revalidate (CSS/JS/fonts/icons).
 *   - API & auth requests → network-only (never cache user data).
 * -----------------------------------------------------------------------------
 * Bumping CACHE_VERSION will evict every cache entry on next activate.
 */

const CACHE_VERSION = 'arbiter-v1.15';
const STATIC_CACHE  = `${CACHE_VERSION}-static`;
const RUNTIME_CACHE = `${CACHE_VERSION}-runtime`;

// Files we want available offline (the "app shell").
const PRECACHE_URLS = [
  '/static/arbiter.css',
  '/static/arbiter.js',
  '/static/manifest.webmanifest',
  '/static/icons/arbiter-icon.svg',
  '/static/icons/arbiter-192.png',
  '/static/icons/arbiter-512.png',
  '/static/offline.html',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => !k.startsWith(CACHE_VERSION)).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Helpers ─────────────────────────────────────────────────────────────────
function isApiRequest(url) {
  const p = url.pathname;
  return (
    p.startsWith('/api/') ||
    p.startsWith('/auth/') ||
    p.startsWith('/v1/') ||
    p.startsWith('/logs/') ||
    p.startsWith('/settings/') ||
    p.startsWith('/cloudflare/') ||
    p === '/dashboard/stats'
  );
}

function isStaticAsset(url) {
  const p = url.pathname;
  return (
    p.startsWith('/static/') ||
    p === '/manifest.webmanifest' ||
    p === '/favicon.ico' ||
    /\.(?:css|js|woff2?|ttf|otf|png|jpe?g|gif|webp|svg|ico|map)$/i.test(p)
  );
}

async function networkFirst(event) {
  try {
    const fresh = await fetch(event.request);
    // Cache successful HTML for offline fallback.
    if (fresh && fresh.ok && fresh.type === 'basic') {
      const clone = fresh.clone();
      caches.open(RUNTIME_CACHE).then((c) => c.put(event.request, clone)).catch(() => {});
    }
    return fresh;
  } catch (_) {
    const cached = await caches.match(event.request);
    if (cached) return cached;
    const offline = await caches.match('/static/offline.html');
    return offline || new Response('Offline', { status: 503, statusText: 'Offline' });
  }
}

async function staleWhileRevalidate(event) {
  const cache  = await caches.open(STATIC_CACHE);
  const cached = await cache.match(event.request);
  const fetchPromise = fetch(event.request)
    .then((res) => {
      if (res && res.ok) cache.put(event.request, res.clone()).catch(() => {});
      return res;
    })
    .catch(() => cached);
  return cached || fetchPromise;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;          // don't cache POSTs/DELETEs
  let url;
  try { url = new URL(req.url); } catch (_) { return; }
  if (url.origin !== self.location.origin) return;  // CDN libs handled by browser

  // 1. APIs & auth → network only (never cache user-bearing responses).
  if (isApiRequest(url)) {
    event.respondWith(fetch(req).catch(() => new Response(
      JSON.stringify({ error: 'offline' }),
      { status: 503, headers: { 'Content-Type': 'application/json' } }
    )));
    return;
  }

  // 2. Static assets → SWR.
  if (isStaticAsset(url)) {
    event.respondWith(staleWhileRevalidate(event));
    return;
  }

  // 3. HTML / everything else → network-first with offline fallback.
  event.respondWith(networkFirst(event));
});

// Allow page to ask the SW to update immediately.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
