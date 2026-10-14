// Hestia app shell service worker v4: cache-first for shell only, network for dynamic endpoints.
const CACHE = 'hestia-shell-v4';
const SHELL = ['/app', '/clips', '/manifest.json', '/icon.png', '/icon-192.png', '/icon-512.png', '/sw.js'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== self.location.origin) return;
  // Dynamic backend routes must always go to network, never cached by the shell service worker.
  if (u.pathname.startsWith('/api/') ||
      u.pathname.startsWith('/v1/') ||
      u.pathname === '/status' ||
      u.pathname === '/health' ||
      u.pathname.startsWith('/memory/') ||
      u.pathname.startsWith('/maintenance/') ||
      u.pathname.startsWith('/queue') ||
      u.pathname.startsWith('/nfc') ||
      u.pathname.startsWith('/whelp')) {
    return;
  }
  // Only intercept exact static shell assets.
  if (!SHELL.includes(u.pathname)) return;

  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copy));
    return res;
  }).catch(() => caches.match('/app'))));
});
