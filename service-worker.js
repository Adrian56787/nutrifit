/* NutriFit · Service Worker
 * - Shell de la app: "stale-while-revalidate" → carga instantánea desde caché y se actualiza en segundo plano.
 * - API (GET): "network-first" con caché de respaldo → se puede consultar lo último visto sin conexión.
 * - Las escrituras (POST/PUT/DELETE) siempre van a la red.
 * - Rutas relativas al scope del worker: funciona en la raíz (Flask) o en una subcarpeta (servidor estático). */
const VERSION = 'nutrifit-v5';
const BASE = self.registration.scope;                       // p. ej. https://host/  o  https://host/carpeta/
const SHELL = [BASE, BASE + 'manifest.json', BASE + 'icon-192.png', BASE + 'icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // uno a uno: si falta algún archivo (p. ej. los iconos sin Flask) la instalación no se rompe
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // CDN, OpenFoodFacts… → red directa

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(req));
    return;
  }
  event.respondWith(staleWhileRevalidate(req));
});

async function networkFirst(req) {
  const cache = await caches.open(VERSION);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    return new Response(JSON.stringify({ error: 'Sin conexión' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(VERSION);
  const key = req.mode === 'navigate' ? BASE : req;
  const cached = await cache.match(key, { ignoreSearch: true });
  const network = fetch(req)
    .then((res) => {
      if (res.ok) cache.put(key, res.clone());
      return res;
    })
    .catch(() => null);
  if (cached) {
    network.catch(() => {});
    return cached;
  }
  const res = await network;
  if (res) return res;
  if (req.mode === 'navigate') {
    const shell = await cache.match(BASE);
    if (shell) return shell;
  }
  return new Response('Sin conexión', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
}