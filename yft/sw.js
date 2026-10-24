const MEDIA_CACHE = 'yft-media-bytes-v1';
const MAX_ENTRY = 120 * 1024 * 1024;
const generations = new Map();

self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function cacheKeys(id, range = '') {
  const url = new URL('/yft/__byte_cache__/' + id, self.location.origin);
  const fullKey = new Request(url.href);
  url.searchParams.set('range', range);
  return { fullKey, rangeKey: new Request(url.href) };
}

async function clearMedia(id) {
  generations.set(id, (generations.get(id) || 0) + 1);
  const cache = await caches.open(MEDIA_CACHE);
  const keys = await cache.keys();
  await Promise.all(keys.filter((key) => new URL(key.url).pathname === '/yft/__byte_cache__/' + id).map((key) => cache.delete(key)));
}

self.addEventListener('message', (event) => {
  if (event.data?.type !== 'CLEAR_MEDIA' || !/^[A-Za-z0-9_-]{11}$/.test(event.data.id || '')) return;
  event.waitUntil(clearMedia(event.data.id).then(() => event.ports?.[0]?.postMessage({ ok: true })));
});

async function report(event, id, response, source) {
  const client = await self.clients.get(event.clientId);
  client?.postMessage({ type: 'YFT_MEDIA_SOURCE', id, source, quality: response.headers.get('X-YFT-Quality') || '' });
}

async function prune(cache) {
  const keys = await cache.keys();
  let total = 0;
  const sizes = [];
  for (const key of keys) {
    const response = await cache.match(key);
    const size = Number(response?.headers.get('Content-Length') || MAX_ENTRY);
    sizes.push({ key, size });
    total += size;
  }
  while (sizes.length > 160 || total > 300 * 1024 * 1024) {
    const oldest = sizes.shift();
    if (!oldest) break;
    await cache.delete(oldest.key);
    total -= oldest.size;
  }
}

async function cachedResponse(response, range, isPartial) {
  const headers = new Headers(response.headers);
  headers.set('X-YFT-Byte-Cache', 'hit');
  const originalStatus = Number(headers.get('X-YFT-Original-Status') || response.status);
  headers.delete('X-YFT-Original-Status');
  if (isPartial || !range) return new Response(response.body, { status: originalStatus, headers });
  const match = /^bytes=(\d*)-(\d*)$/.exec(range);
  if (!match || (!match[1] && !match[2])) return null;
  const blob = await response.blob();
  const start = match[1] ? Number(match[1]) : Math.max(0, blob.size - Number(match[2]));
  const end = Math.min(match[1] && match[2] ? Number(match[2]) : blob.size - 1, blob.size - 1);
  if (start > end || start >= blob.size) {
    return new Response(null, { status: 416, headers: { 'Content-Range': 'bytes */' + blob.size } });
  }
  headers.set('Content-Range', 'bytes ' + start + '-' + end + '/' + blob.size);
  headers.set('Content-Length', String(end - start + 1));
  headers.set('Accept-Ranges', 'bytes');
  return new Response(blob.slice(start, end + 1), { status: 206, headers });
}

function observeConsumedBytes(response, cache, key, id, generation, event) {
  const length = Number(response.headers.get('Content-Length') || 0);
  if (!response.body || length > MAX_ENTRY) return response;
  const reader = response.body.getReader();
  let chunks = [];
  let size = 0;
  let finish;
  const completed = new Promise((resolve) => { finish = resolve; });
  event.waitUntil(completed);
  const body = new ReadableStream({
    async pull(controller) {
      try {
        const part = await reader.read();
        if (!part.done) {
          size += part.value.byteLength;
          if (chunks && size <= MAX_ENTRY) chunks.push(part.value.slice());
          else chunks = null;
          controller.enqueue(part.value);
          return;
        }
        if (chunks && size && (!length || size === length) && (generations.get(id) || 0) === generation) {
          const headers = new Headers(response.headers);
          headers.set('X-YFT-Original-Status', String(response.status));
          headers.set('Content-Length', String(size));
          // Cache.put rejects status 206; restore it when serving these bytes.
          const blob = new Blob(chunks, { type: headers.get('Content-Type') || 'video/mp4' });
          try {
            await cache.put(key, new Response(blob, { status: 200, headers }));
            if ((generations.get(id) || 0) !== generation) await cache.delete(key);
            else await prune(cache);
          } catch { /* Storage quota cannot interrupt the player's stream. */ }
        }
        chunks = null;
        controller.close();
        finish();
      } catch (error) {
        chunks = null;
        controller.error(error);
        finish();
      }
    },
    async cancel(reason) {
      chunks = null;
      await reader.cancel(reason).catch(() => {});
      finish();
    }
  });
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  const match = /^\/yft\/media\/([A-Za-z0-9_-]{11})$/.exec(url.pathname);
  if (!match || url.origin !== self.location.origin) return;
  event.respondWith((async () => {
    const id = match[1];
    const range = request.headers.get('Range') || '';
    const cache = await caches.open(MEDIA_CACHE);
    if (url.searchParams.get('refresh') === '1') await clearMedia(id);
    const generation = generations.get(id) || 0;
    const { fullKey, rangeKey } = cacheKeys(id, range);
    const partial = range && await cache.match(rangeKey);
    const full = !partial && await cache.match(fullKey);
    if (partial || full) {
      const response = await cachedResponse(partial || full, range, Boolean(partial));
      if (response) {
        await report(event, id, response, 'cache');
        return response;
      }
    }
    const response = await fetch(request);
    await report(event, id, response, 'stream');
    const mime = response.headers.get('Content-Type') || '';
    if (![200, 206].includes(response.status) || !/^(video|audio)\//.test(mime)) return response;
    const key = response.status === 206 && range ? rangeKey : fullKey;
    // Observe the player's consumption, with no separate prefetch/download.
    return observeConsumedBytes(response, cache, key, id, generation, event);
  })());
});
