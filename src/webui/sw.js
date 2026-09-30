const SHELL_CACHE = 'oiapi-shell-v1';
self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(['/gallery', '/icon.svg'])).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== SHELL_CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/gallery') {
    event.respondWith(fetch(request).then(response => {
      if (response.ok) { const copy = response.clone(); caches.open(SHELL_CACHE).then(cache => cache.put('/gallery', copy)); }
      return response;
    }).catch(() => caches.match('/gallery')));
    return;
  }
  event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => {
    if (response.ok && (url.pathname === '/icon.svg' || url.pathname === '/manifest.webmanifest')) {
      const copy = response.clone(); caches.open(SHELL_CACHE).then(cache => cache.put(request, copy));
    }
    return response;
  })));
});
