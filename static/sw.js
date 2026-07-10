'use strict';

// Bump this on every deploy that changes cached assets — forces old caches out.
const CACHE_VERSION = 'roadent-v3';
const SHELL_CACHE  = `${CACHE_VERSION}-shell`;
const TILE_CACHE    = `${CACHE_VERSION}-tiles`;
const API_CACHE     = `${CACHE_VERSION}-api`;
const MAX_TILES     = 300; // cap so map-tile cache can't grow unbounded during a demo

const APP_SHELL = [
  '/',
  '/static/manifest.json',
  '/static/icon.svg',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300;0,400;0,500;1,300;1,400&family=Instrument+Sans:wght@400;500;600&display=swap',
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      Promise.allSettled(APP_SHELL.map((url) => cache.add(url).catch(() => null)))
    )
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL_CACHE, TILE_CACHE, API_CACHE]);
      const names = await caches.keys();
      await Promise.all(names.filter((n) => !keep.has(n)).map((n) => caches.delete(n)));
      await self.clients.claim();
    })()
  );
});

async function trimCache(cacheName, maxEntries) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  if (keys.length > maxEntries) {
    await Promise.all(keys.slice(0, keys.length - maxEntries).map((k) => cache.delete(k)));
  }
}

// Cache-first: for the app shell, Leaflet lib, fonts, map tiles — things that rarely change
// and must load with zero network.
async function cacheFirst(request, cacheName, trim) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const res = await fetch(request);
    if (res && (res.ok || res.type === 'opaque')) {
      const cache = await caches.open(cacheName);
      cache.put(request, res.clone());
      if (trim) trimCache(cacheName, MAX_TILES);
    }
    return res;
  } catch (err) {
    return cached || Response.error();
  }
}

// Network-first: always prefer live data/latest file; fall back to cache only when
// the network request itself fails (i.e. genuinely offline).
async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(request);
    if (res && res.ok) cache.put(request, res.clone());
    return res;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

// Strictly network-first for API calls (GET and POST): the live network result —
// success OR an HTTP-level error — is always authoritative and is returned as-is.
// Cache is consulted ONLY inside the catch block, i.e. only when fetch() itself
// threw (a genuine network failure). If there's no cached response either, we
// synthesize a minimal offline JSON so the page gets valid JSON instead of a
// thrown exception. Cache.put()/match() only support GET keys, so writes are
// gated on method; reads against a POST request key simply miss (no entry) and
// fall through to the synthetic response — no special-casing needed.
async function apiNetworkFirst(request) {
  const cache = await caches.open(API_CACHE);
  try {
    const res = await fetch(request);
    if (request.method === 'GET' && res && res.ok) cache.put(request, res.clone());
    return res;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ offline: true, services: [], reply: null }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // All API traffic — GET and POST — strictly network-first, never cache-first.
  if (url.pathname.startsWith('/api/') || url.pathname === '/health') {
    event.respondWith(apiNetworkFirst(request));
    return;
  }

  if (request.method !== 'GET') return; // everything below is shell/asset handling, GET-only

  // Map tiles — immutable images, cache every tile the user has actually seen,
  // evict oldest beyond MAX_TILES
  if (url.hostname.endsWith('tile.openstreetmap.org')) {
    event.respondWith(cacheFirst(request, TILE_CACHE, true));
    return;
  }

  // The page itself and this script — always fetch the latest version while
  // online (so edits during dev/demo prep show up immediately); only serve the
  // cached shell when there's truly no network.
  if (request.mode === 'navigate' || url.pathname === '/sw.js') {
    event.respondWith(networkFirst(request, SHELL_CACHE));
    return;
  }

  // Pinned-version CDN libs, fonts, manifest, icon — safe to cache-first
  if (url.origin === self.location.origin || APP_SHELL.includes(request.url)) {
    event.respondWith(cacheFirst(request, SHELL_CACHE, false));
  }
});
