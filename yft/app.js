(() => {
  'use strict';

  const API = '/yft/api';
  const DB_NAME = 'yft-device-library';
  const DB_VERSION = 1;
  const STORE = 'downloads';
  const SEARCH_KEY = 'yft-search-history-v1';
  const RECENT_KEY = 'yft-recently-played-v1';
  const $ = (id) => document.getElementById(id);
  const state = { results: [], activeTrack: null, retryCount: 0, localPlayback: false, objectUrl: '', expectedSrc: '', searchTimer: 0, searchRequest: 0, toastTimer: 0, playEpoch: 0, recovering: false, mediaSource: '' };
  const downloadedRecords = new Map();
  let dbPromise;

  const searchInput = $('searchInput');
  const searchStatus = $('searchStatus');
  const searchHistory = $('searchHistory');
  const resultsSection = $('resultsSection');
  const resultsList = $('resultsList');
  const recentList = $('recentList');
  const downloadsList = $('downloadsList');
  const player = $('player');

  function safeJson(value, fallback) {
    try { return JSON.parse(value); } catch { return fallback; }
  }

  function historyItems() { return safeJson(localStorage.getItem(SEARCH_KEY) || '[]', []); }
  function recentItems() { return safeJson(localStorage.getItem(RECENT_KEY) || '[]', []); }

  function validDownload(record) { return record?.blob instanceof Blob && record.blob.size > 0; }

  function showPlayerPage() {
    $('searchPage').hidden = false;
    $('downloadsPage').hidden = true;
    document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === 'search'));
  }

  async function reportDeviceDownloads(items) {
    let device = localStorage.getItem('yft-device-id');
    if (!device) { device = crypto.randomUUID(); localStorage.setItem('yft-device-id', device); }
    await fetch(`${API}/device/downloads`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device, items: items.filter(validDownload).map((record) => ({ id: record.id, size: record.blob.size, mime: record.blob.type, downloadedAt: record.downloadedAt })) })
    }).catch(() => {});
  }

  function db() {
    if (!('indexedDB' in window)) return Promise.reject(new Error('IndexedDB is unavailable in this browser.'));
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
          const database = req.result;
          if (!database.objectStoreNames.contains(STORE)) database.createObjectStore(STORE, { keyPath: 'id' });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error || new Error('Could not open local downloads.'));
      });
    }
    return dbPromise;
  }

  function transactionDone(tx) {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error('IndexedDB write failed.'));
      tx.onabort = () => reject(tx.error || new Error('IndexedDB write was interrupted.'));
    });
  }

  async function dbAll() {
    const database = await db();
    return new Promise((resolve, reject) => {
      const tx = database.transaction(STORE, 'readonly');
      const req = tx.objectStore(STORE).getAll();
      req.onsuccess = () => resolve((req.result || []).sort((a, b) => (b.downloadedAt || 0) - (a.downloadedAt || 0)));
      req.onerror = () => reject(req.error || new Error('Could not read downloads.'));
    });
  }

  async function dbGet(id) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const req = database.transaction(STORE, 'readonly').objectStore(STORE).get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error || new Error('Could not read this download.'));
    });
  }

  async function dbPut(record) {
    const database = await db();
    const tx = database.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(record);
    await transactionDone(tx);
  }

  async function dbDelete(id) {
    const database = await db();
    const tx = database.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).delete(id);
    await transactionDone(tx);
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    if (!value) return '';
    const minutes = Math.floor(value / 60);
    const remainder = Math.floor(value % 60).toString().padStart(2, '0');
    return `${minutes}:${remainder}`;
  }

  function formatSize(bytes) {
    if (!bytes) return '0 KB';
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / 1024 / 1024).toFixed(bytes > 100 * 1024 * 1024 ? 0 : 1)} MB`;
  }

  function fallbackThumb(id) {
    return id ? `https://i.ytimg.com/vi/${encodeURIComponent(id)}/mqdefault.jpg` : '';
  }

  function toast(message) {
    const node = $('toast');
    node.textContent = message;
    node.classList.add('is-visible');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove('is-visible'), 2600);
  }

  function setPlaybackStatus(message, error = false) {
    const node = $('playbackStatus');
    node.textContent = message || '';
    node.classList.toggle('is-error', Boolean(error));
  }

  function addSearchHistory(query) {
    const clean = String(query || '').trim();
    if (!clean) return;
    const next = [clean, ...historyItems().filter((item) => String(item).toLocaleLowerCase() !== clean.toLocaleLowerCase())].slice(0, 10);
    localStorage.setItem(SEARCH_KEY, JSON.stringify(next));
    renderSearchHistory();
  }

  function renderSearchHistory() {
    const rows = historyItems();
    searchHistory.replaceChildren();
    if (!rows.length || document.activeElement !== searchInput) {
      searchHistory.hidden = true;
      return;
    }
    rows.forEach((query) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'history-chip';
      const label = document.createElement('span');
      label.textContent = query;
      const remove = document.createElement('b');
      remove.className = 'remove';
      remove.textContent = '×';
      remove.setAttribute('aria-label', `Remove ${query} from recent searches`);
      chip.append(label, remove);
      chip.addEventListener('click', (event) => {
        if (event.target === remove) {
          event.preventDefault();
          event.stopPropagation();
          const updated = historyItems().filter((value) => value !== query);
          localStorage.setItem(SEARCH_KEY, JSON.stringify(updated));
          renderSearchHistory();
          return;
        }
        searchInput.value = query;
        searchHistory.hidden = true;
        doSearch(query, true);
        searchInput.blur();
      });
      searchHistory.append(chip);
    });
    searchHistory.hidden = false;
  }

  function recordPlayed(track) {
    const normalized = normalizeTrack(track);
    const next = [normalized, ...recentItems().filter((item) => item.id !== normalized.id)].slice(0, 10);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    renderRecent();
  }

  function normalizeTrack(item) {
    return {
      id: String(item.id || '').trim(),
      title: String(item.title || 'Untitled'),
      artist: String(item.artist || 'Unknown artist'),
      thumb: String(item.thumb || item.thumbnail || fallbackThumb(item.id)),
      duration: Number(item.duration || 0),
      views: Number(item.views || 0)
    };
  }

  function trackRow(trackInput, options = {}) {
    const track = normalizeTrack(trackInput);
    const row = document.createElement('article');
    row.className = 'track-row';
    const image = document.createElement('img');
    image.className = 'track-thumb';
    image.alt = '';
    image.loading = 'lazy';
    image.src = track.thumb || fallbackThumb(track.id);
    image.onerror = () => { image.onerror = null; image.src = fallbackThumb(track.id); };
    const copy = document.createElement('div');
    copy.className = 'track-copy';
    const title = document.createElement('strong');
    title.textContent = track.title;
    const artist = document.createElement('span');
    artist.textContent = track.artist;
    const detail = document.createElement('small');
    if (options.downloaded) detail.textContent = `${formatSize(options.size || 0)} · ${new Date(options.downloadedAt || Date.now()).toLocaleDateString()}`;
    else if (track.duration) detail.textContent = formatDuration(track.duration);
    copy.append(title, artist);
    if (detail.textContent) copy.append(detail);

    const actions = document.createElement('div');
    actions.className = 'track-actions';
    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'action-button primary';
    play.textContent = '▶';
    play.title = options.downloaded ? 'Play saved video' : 'Play stream';
    play.setAttribute('aria-label', `${play.title}: ${track.title}`);
    play.addEventListener('click', () => options.downloaded ? playDownloaded(options.record) : playTrack(track));
    actions.append(play);

    if (options.downloaded) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'action-button';
      remove.textContent = '×';
      remove.title = 'Delete this download';
      remove.setAttribute('aria-label', `Delete ${track.title}`);
      remove.addEventListener('click', async () => {
        await dbDelete(track.id);
        if (state.activeTrack?.id === track.id && state.localPlayback) stopPlayer();
        await renderDownloads();
        toast('Download deleted from this device.');
      });
      actions.append(remove);
    } else {
      const download = document.createElement('button');
      download.type = 'button';
      download.className = 'action-button download-action';
      download.textContent = '↓';
      download.title = 'Download to this device';
      download.setAttribute('aria-label', `Download ${track.title}`);
      download.addEventListener('click', () => downloadTrack(track, download));
      actions.append(download);
    }
    row.append(image, copy, actions);
    return row;
  }

  async function renderRecent() {
    const items = recentItems().filter((item) => item && item.id);
    recentList.replaceChildren(...items.map((item) => trackRow(item, { recent: true })));
    $('recentEmpty').hidden = items.length > 0;
    $('clearRecent').hidden = items.length === 0;
  }

  async function renderDownloads() {
    try {
      const items = await dbAll();
      downloadedRecords.clear();
      items.forEach((record) => downloadedRecords.set(record.id, record));
      downloadsList.replaceChildren(...items.map((record) => trackRow(record, { downloaded: true, size: record.blob?.size || record.size || 0, downloadedAt: record.downloadedAt, record })));
      $('downloadsEmpty').hidden = items.length > 0;
      $('downloadCount').textContent = String(items.length);
      $('downloadSummaryTitle').textContent = `${items.length} ${items.length === 1 ? 'track' : 'tracks'} saved here`;
      const total = items.reduce((sum, item) => sum + (item.blob?.size || item.size || 0), 0);
      $('downloadSummaryMeta').textContent = `${formatSize(total)} stored on this device · synced nowhere`;
      reportDeviceDownloads(items);
    } catch (error) {
      downloadsList.replaceChildren();
      $('downloadsEmpty').hidden = false;
      $('downloadsEmpty').querySelector('p').textContent = error.message || 'Could not read local downloads.';
      toast(error.message || 'Could not open IndexedDB.');
    }
  }

  async function doSearch(rawQuery, fromHistory = false, commitHistory = true) {
    const query = String(rawQuery || '').trim();
    if (!query) {
      resultsSection.hidden = true;
      $('searchStatus').textContent = '';
      return;
    }
    if (commitHistory) addSearchHistory(query);
    if (commitHistory || document.activeElement !== searchInput) searchHistory.hidden = true;
    resultsSection.hidden = false;
    $('resultsTitle').textContent = `Results for “${query}”`;
    resultsList.replaceChildren();
    $('resultsEmpty').hidden = true;
    searchStatus.textContent = 'Searching YouTube…';
    const requestId = ++state.searchRequest;
    try {
      const response = await fetch(`${API}/search?q=${encodeURIComponent(query)}&limit=20`, { headers: { Accept: 'application/json' } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.error || 'Search failed.');
      if (requestId !== state.searchRequest) return;
      state.results = Array.isArray(data.items) ? data.items.map(normalizeTrack).filter((item) => item.id) : [];
      resultsList.replaceChildren(...state.results.map((track) => trackRow(track)));
      $('resultsEmpty').hidden = state.results.length > 0;
      searchStatus.textContent = `${state.results.length} results`;
    } catch (error) {
      if (requestId !== state.searchRequest) return;
      searchStatus.textContent = error.message || 'Search is unavailable right now.';
      $('resultsEmpty').hidden = false;
    }
  }

  async function requestQuality(track, force = false) {
    try {
      const response = await fetch(`${API}/resolve?id=${encodeURIComponent(track.id)}${force ? '&refresh=1' : ''}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not resolve this track.');
      if (state.activeTrack?.id === track.id) $('qualityBadge').textContent = data.quality || 'Auto';
      return data;
    } catch (error) {
      if (state.activeTrack?.id === track.id) setPlaybackStatus(error.message || 'Could not resolve the stream.', true);
      return null;
    }
  }

  async function playTrack(trackInput, opts = {}) {
    const track = normalizeTrack(trackInput);
    if (!track.id) return;
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = '';
    state.activeTrack = track;
    const epoch = ++state.playEpoch;
    state.recovering = false;
    state.mediaSource = '';
    state.retryCount = opts.retryCount || 0;
    state.localPlayback = false;
    $('playerTitle').textContent = track.title;
    $('playerArtist').textContent = track.artist;
    $('qualityBadge').textContent = 'Auto';
    showPlayerPage();
    $('playerPanel').hidden = false;
    $('playerPlaceholder').hidden = true;
    $('player').poster = track.thumb || fallbackThumb(track.id);
    $('player').removeAttribute('src');
    $('player').load();
    state.expectedSrc = '';
    setPlaybackStatus('Opening a private server relay…');

    if (!opts.forceLive) {
      const saved = await dbGet(track.id).catch(() => null);
      if (epoch !== state.playEpoch) return;
      if (validDownload(saved)) {
        playDownloaded(saved, { verified: true });
        return;
      }
    }

    if (opts.refresh) {
      const fresh = await requestQuality(track, true);
      if (!fresh || epoch !== state.playEpoch) return;
    }

    // Assign the same-origin relay synchronously from the user action. The
    // service worker may satisfy it from the local byte cache; otherwise the
    // YFT server resolves and relays the selected YouTube stream.
    player.src = `/yft/media/${encodeURIComponent(track.id)}?rev=${Date.now()}`;
    state.expectedSrc = player.src;
    player.load();
    const playPromise = player.play();
    if (playPromise?.then) {
      playPromise.then(() => {
        if (epoch === state.playEpoch) setPlaybackStatus(state.mediaSource === 'cache' ? 'Playing cached stream bytes from this device.' : 'Streaming through YFT.');
      }).catch((error) => {
        if (epoch === state.playEpoch && error.name === 'NotAllowedError') setPlaybackStatus('Tap play on the video to start.');
      });
    }
  }

  async function playDownloaded(record, options = {}) {
    const requested = record;
    if (!options.verified) record = await dbGet(record?.id).catch(() => null);
    if (!validDownload(record)) {
      toast('The local file is missing. Opening a fresh stream.');
      if (requested?.id) playTrack(requested, { forceLive: true });
      return;
    }
    const track = normalizeTrack(record);
    showPlayerPage();
    ++state.playEpoch;
    state.recovering = false;
    state.activeTrack = track;
    state.retryCount = 0;
    state.localPlayback = true;
    $('playerTitle').textContent = track.title;
    $('playerArtist').textContent = track.artist;
    $('qualityBadge').textContent = 'On device';
    $('playerPanel').hidden = false;
    $('playerPlaceholder').hidden = true;
    $('player').poster = track.thumb || fallbackThumb(track.id);
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = URL.createObjectURL(record.blob);
    player.src = state.objectUrl;
    state.expectedSrc = state.objectUrl;
    player.load();
    setPlaybackStatus('Playing the saved file from IndexedDB.');
    try { await player.play(); } catch { setPlaybackStatus('Tap play on the video to start the saved file.'); }
  }

  function stopPlayer() {
    ++state.playEpoch;
    player.pause();
    player.removeAttribute('src');
    player.load();
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = '';
    state.expectedSrc = '';
    state.localPlayback = false;
  }

  async function clearStreamCache(id) {
    const registration = await navigator.serviceWorker?.ready.catch(() => null);
    const worker = navigator.serviceWorker?.controller || registration?.active;
    if (!worker) return;
    await new Promise((resolve) => {
      const channel = new MessageChannel();
      const timeout = setTimeout(resolve, 1200);
      channel.port1.onmessage = () => { clearTimeout(timeout); resolve(); };
      worker.postMessage({ type: 'CLEAR_MEDIA', id }, [channel.port2]);
    });
  }

  async function recoverPlayback() {
    const track = state.activeTrack;
    if (!track) return;
    if (state.recovering) return;
    if (state.retryCount >= 2) {
      setPlaybackStatus('This video could not be played. Try another track or retry.', true);
      return;
    }
    const wasLocal = state.localPlayback;
    state.recovering = true;
    state.retryCount += 1;
    const retryCount = state.retryCount;
    if (wasLocal) {
      if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
      state.objectUrl = '';
      toast('The saved copy could not play. Trying a fresh stream.');
    } else {
      await clearStreamCache(track.id);
      toast('Cleared the failed cached bytes; resolving a fresh stream.');
    }
    setPlaybackStatus('Retrying with fresh bytes…');
    await playTrack(track, { retryCount, forceLive: true, refresh: true });
  }

  const recoverOnce = () => {
    if (!state.activeTrack || !player.currentSrc || state.expectedSrc !== player.currentSrc) return;
    recoverPlayback().catch((error) => setPlaybackStatus(error.message || 'Playback retry failed.', true));
  };
  player.addEventListener('error', recoverOnce);
  player.addEventListener('loadedmetadata', () => {
    if (player.videoWidth && player.videoHeight) player.closest('.player-screen').style.aspectRatio = `${player.videoWidth} / ${player.videoHeight}`;
  });
  player.addEventListener('playing', () => {
    if (state.activeTrack) recordPlayed(state.activeTrack);
  });

  async function downloadTrack(trackInput, button) {
    const track = normalizeTrack(trackInput);
    if (button) button.disabled = true;
    const previousLabel = $('playbackStatus').textContent;
    if (state.activeTrack?.id === track.id) setPlaybackStatus('Preparing a server download…');
    try {
      const existing = await dbGet(track.id).catch(() => null);
      if (validDownload(existing)) {
        await renderDownloads();
        toast('This video is already saved on this device.');
        return;
      }
      const startResponse = await fetch(`${API}/download`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(track)
      });
      const startData = await startResponse.json();
      if (!startResponse.ok) throw new Error(startData.detail || startData.error || 'Could not start download.');
      let status;
      const deadline = Date.now() + 12 * 60 * 1000;
      do {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        const response = await fetch(`${API}/download/status?id=${encodeURIComponent(startData.jobId)}`, { cache: 'no-store' });
        status = await response.json();
        if (!response.ok) throw new Error(status.detail || 'Download status was lost.');
        if (state.activeTrack?.id === track.id) setPlaybackStatus(status.progress ? `Downloading ${status.progress}…` : 'Downloading to a temporary server file…');
        if (status.state === 'failed') throw new Error(status.error || 'yt-dlp could not download this video.');
        if (Date.now() > deadline) throw new Error('Download timed out. The temporary server file will expire automatically.');
      } while (status.state !== 'ready');

      const fileResponse = await fetch(`${API}/download/file?id=${encodeURIComponent(startData.jobId)}`, { cache: 'no-store' });
      if (!fileResponse.ok) throw new Error('Could not retrieve the completed download.');
      const blob = await fileResponse.blob();
      if (!blob.size) throw new Error('The downloaded file was empty.');
      const record = { ...track, blob, size: blob.size, mime: blob.type || 'video/mp4', downloadedAt: Date.now(), quality: status.quality || '' };
      await dbPut(record);
      const verified = await dbGet(track.id);
      if (!validDownload(verified) || verified.blob.size !== blob.size) throw new Error('IndexedDB did not retain the complete file.');
      // Acknowledge only after IndexedDB confirms the real Blob write.
      let acknowledged = false;
      try {
        const ack = await fetch(`${API}/download/ack`, {
          method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ jobId: startData.jobId })
        });
        acknowledged = ack.ok;
      } catch { /* The real file is committed locally; TTL handles an interrupted acknowledgement. */ }
      await renderDownloads();
      toast(acknowledged ? `${track.title} saved to this device.` : 'Saved locally. The server copy will expire automatically.');
      if (state.activeTrack?.id === track.id) setPlaybackStatus('Download saved as a Blob in IndexedDB.');
    } catch (error) {
      if (state.activeTrack?.id === track.id) setPlaybackStatus(error.message || 'Download failed.', true);
      toast(error.message || 'Download failed.');
    } finally {
      if (button) button.disabled = false;
      if (!state.activeTrack || state.activeTrack.id !== track.id) searchStatus.textContent = previousLabel;
    }
  }

  $('playerDownload').addEventListener('click', () => state.activeTrack && downloadTrack(state.activeTrack, $('playerDownload')));
  $('searchForm').addEventListener('submit', (event) => {
    event.preventDefault();
    clearTimeout(state.searchTimer);
    doSearch(searchInput.value);
    searchInput.blur();
  });
  searchInput.addEventListener('focus', renderSearchHistory);
  searchInput.addEventListener('input', () => {
    $('searchClear').hidden = !searchInput.value;
    renderSearchHistory();
    clearTimeout(state.searchTimer);
    if (searchInput.value.trim().length >= 2) state.searchTimer = setTimeout(() => doSearch(searchInput.value, false, false), 420);
    else if (!searchInput.value.trim()) { resultsSection.hidden = true; searchStatus.textContent = ''; }
  });
  searchInput.addEventListener('keydown', (event) => { if (event.key === 'Escape') searchHistory.hidden = true; });
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-block')) searchHistory.hidden = true; });
  $('searchClear').addEventListener('click', () => { searchInput.value = ''; $('searchClear').hidden = true; searchInput.focus(); renderSearchHistory(); resultsSection.hidden = true; searchStatus.textContent = ''; });
  $('clearRecent').addEventListener('click', () => { localStorage.removeItem(RECENT_KEY); renderRecent(); });

  document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', async () => {
    const downloads = tab.dataset.tab === 'downloads';
    document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('is-active', item === tab));
    $('searchPage').hidden = downloads;
    $('downloadsPage').hidden = !downloads;
    if (downloads) await renderDownloads();
  }));

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/yft/sw.js', { scope: '/yft/', updateViaCache: 'none' }).catch(() => {});
    navigator.serviceWorker.addEventListener('message', (event) => {
      const data = event.data || {};
      if (data.type !== 'YFT_MEDIA_SOURCE' || data.id !== state.activeTrack?.id || state.localPlayback) return;
      state.mediaSource = data.source;
      $('qualityBadge').textContent = [data.quality || 'Auto', data.source === 'cache' ? 'Cached' : 'Live'].join(' · ');
      if (data.source === 'cache') setPlaybackStatus('Playing cached stream bytes from this device.');
    });
  }
  renderRecent();
  renderDownloads();
})();
