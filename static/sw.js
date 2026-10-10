// Service worker приложения Gerchik.
// Оболочка (страница, манифест, иконки) — из сети, при отсутствии сети — из
// кэша: приложение открывается и без связи, а данные на экране помечаются
// как устаревшие самим дашбордом (запросы /api/ не кэшируются никогда —
// старые цифры не должны выдаваться за актуальные).
const CACHE_NAME = 'gerchik-v2';
const SHELL = ['/', '/manifest.json', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE_NAME).then(c => c.put(e.request, copy)); }
      return r;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});

// Уведомление от сервера (Web Push).
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Gerchik', {
    body: d.body || '',
    tag: d.tag || 'gerchik',
    renotify: true,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: {url: d.url || '/'}
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
      for (const c of list) { if ('focus' in c) return c.focus(); }
      return clients.openWindow(target);
    })
  );
});
