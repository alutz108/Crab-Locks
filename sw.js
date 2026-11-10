// Crab Locks service worker: opens instantly from the phone's saved copy,
// then refreshes in the background. The app shows a banner when something new is ready.
const CACHE = 'crab-locks-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const req = e.request; if (req.method !== 'GET') return;
  const u = new URL(req.url); if (u.origin !== location.origin) return;
  if (u.searchParams.has('check')) return; // update checks always go to the network
  const p = u.pathname;
  if (req.mode === 'navigate' && u.searchParams.has('v')) { e.respondWith(networkFirst(req)); return; } // Refresh / Update buttons
  if (req.mode === 'navigate' || p.endsWith('/index.html') || p.endsWith('/data/index.json')) { e.respondWith(staleWhileRevalidate(req, e)); return; }
  if (p.includes('/data/') || p.includes('/icons/') || p.endsWith('.webmanifest')) { e.respondWith(cacheFirst(req)); return; }
});
async function staleWhileRevalidate(req, e){
  const c = await caches.open(CACHE);
  const key = new Request(new URL(req.url).pathname);
  const cached = await c.match(key);
  const net = fetch(req).then(r => { if (r.ok) c.put(key, r.clone()); return r; }).catch(() => null);
  if (cached){ e.waitUntil(net); return cached; }
  return (await net) || new Response('Offline', { status: 503 });
}
async function networkFirst(req){
  const c = await caches.open(CACHE), key = new Request(new URL(req.url).pathname);
  try{ const r = await fetch(req, { cache:'no-store' }); if (r.ok) c.put(key, r.clone()); return r; }
  catch(e){ return (await c.match(key)) || new Response('Offline', { status: 503 }); }
}
async function cacheFirst(req){
  const c = await caches.open(CACHE);
  const hit = await c.match(req); if (hit) return hit;
  const r = await fetch(req);
  if (r.ok){
    c.put(req, r.clone());
    // drop older copies of the same data file (different ?v=)
    const path = new URL(req.url).pathname;
    for (const k of await c.keys()){ const ku = new URL(k.url); if (ku.pathname === path && ku.search !== new URL(req.url).search) c.delete(k); }
  }
  return r;
}
