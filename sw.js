const CACHE_NAME = 'pokequant-offline-v12';
const STATIC_ASSETS = [
  './',
  './index.html',
  './app.py',
  './card_tool.py',
  './manifest.json'
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.map(k => { if (k !== CACHE_NAME) return caches.delete(k); })
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // 1. BYPASS R2 DOWNLOAD: Let index.html handle the massive DB download directly
  if (url.href.includes('mobile_catalog.db') && !url.pathname.includes('/offline-db/')) {
    return; 
  }

  // 2. VIRTUAL DATABASE STREAM
  if (event.request.method === 'GET' && url.pathname.endsWith('/offline-db/mobile_catalog.db')) {
    event.respondWith(serveDatabaseStream().catch(err => {
      return new Response("Stream Error: " + err.message, { status: 500 });
    }));
    return;
  }

  // 3. HARD DISK WRITE BRIDGE (POST)
  if (event.request.method === 'POST' && url.pathname.endsWith('/offline-db/save')) {
      event.respondWith((async () => {
          try {
              const data = await event.request.json();
              const db = await new Promise((res, rej) => {
                  const req = indexedDB.open('PokeQuantDB', 1);
                  req.onsuccess = () => res(req.result);
              });
              await new Promise((res) => {
                  const tx = db.transaction('chunks', 'readwrite');
                  tx.objectStore('chunks').put(data.value, data.key);
                  tx.oncomplete = res;
              });
              return new Response("OK", {status: 200});
          } catch(e) {
              return new Response(e.message, {status: 500});
          }
      })());
      return;
  }

  // 4. HARD DISK READ BRIDGE (GET)
  if (event.request.method === 'GET' && url.pathname.endsWith('/offline-db/load')) {
      const key = url.searchParams.get('key');
      event.respondWith((async () => {
          try {
              const db = await new Promise((res, rej) => {
                  const req = indexedDB.open('PokeQuantDB', 1);
                  req.onsuccess = () => res(req.result);
              });
              const val = await new Promise((res) => {
                  const tx = db.transaction('chunks', 'readonly');
                  const req = tx.objectStore('chunks').get(key);
                  req.onsuccess = () => res(req.result);
                  req.onerror = () => res(null);
              });
              return new Response(JSON.stringify({value: val || null}), {status: 200, headers: {'Content-Type': 'application/json'}});
          } catch(e) {
              return new Response(JSON.stringify({value: null}), {status: 200});
          }
      })());
      return;
  }

  // Abort if not GET (prevents caching other API requests)
  if (event.request.method !== 'GET') return;

  // 5. BYPASS CACHE: Live price deltas must always hit the network
  if (url.href.includes('latest_delta.json') || url.searchParams.has('t')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 6. CACHE EVERYTHING ELSE (Including the Stlite Python Engine from CDN)
  event.respondWith(
    caches.match(event.request).then(cachedResponse => {
      return cachedResponse || fetch(event.request).then(networkResponse => {
        if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      });
    }).catch(err => {
       return new Response("Offline Mode Error: " + err.message, { status: 503 });
    })
  );
});

async function serveDatabaseStream() {
  const db = await new Promise((resolve, reject) => {
    const req = indexedDB.open('PokeQuantDB', 1);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });

  if (!db.objectStoreNames.contains('chunks')) {
    return new Response("Database chunks store not found.", { status: 404 });
  }

  const getChunk = (key) => new Promise((resolve, reject) => {
    try {
      const tx = db.transaction('chunks', 'readonly');
      const req = tx.objectStore('chunks').get(key);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    } catch(e) {
      reject(e);
    }
  });

  const metadata = await getChunk('metadata');
  if (!metadata) return new Response("Metadata not found", { status: 404 });

  let currentIndex = 0;
  
  const stream = new ReadableStream({
    async pull(controller) {
      try {
        if (currentIndex >= metadata.totalChunks) {
          controller.close();
          return;
        }
        const chunk = await getChunk(currentIndex++);
        if (chunk) {
          controller.enqueue(chunk);
        } else {
          controller.close();
        }
      } catch (e) {
        controller.error(e);
      }
    }
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'application/octet-stream'
    }
  });
}