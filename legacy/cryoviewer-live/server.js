const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const path = require('path');
const fs = require('fs');
const fsp = require('fs').promises;
const chokidar = require('chokidar');
const crypto = require('crypto');
const security = require('./security');

// ─── Security configuration (see README "Security") ─────────────────────────
// HOST                 bind address (default 127.0.0.1; put a reverse proxy in front)
// CVL_ROOTS            allowed data roots, separated by ':' (watching is disabled without it)
// CVL_AUTH             "user:password" for HTTP Basic auth (or CVL_AUTH_FILE: file with that content)
// CVL_ALLOWED_HOSTS    public names (reverse proxy), comma-separated, accepted as Host/Origin
// CVL_THUMB_CACHE_MB   thumbnail disk cache limit (default 512)
// CVL_INSECURE_NO_AUTH=1  allow a non-loopback bind without auth (not recommended)
const HOST = process.env.HOST || '127.0.0.1';
const ROOTS = security.parseRoots(process.env.CVL_ROOTS);
const AUTH = security.loadAuth(process.env);
const ALLOWED_HOSTS = security.parseAllowedHosts(process.env, HOST);
const TRUSTED_HOSTS = security.parseTrustedHosts(process.env);

if (!security.isLoopbackHost(HOST) && !AUTH && process.env.CVL_INSECURE_NO_AUTH !== '1') {
  console.error(`[Security] Refusing to listen on ${HOST} without authentication.`);
  console.error('[Security] Set CVL_AUTH="user:password" (or CVL_AUTH_FILE), or bind to 127.0.0.1 behind a reverse proxy.');
  process.exit(1);
}
if (!ROOTS.length) console.warn('[Security] CVL_ROOTS is empty: watching is disabled until data roots are configured.');
else console.log(`[Security] Data roots: ${ROOTS.map(r => r.real).join(', ')}`);

const app = express();
app.disable('x-powered-by');
const server = http.createServer(app);
const wss = new WebSocket.Server({
  server,
  maxPayload: 64 * 1024,
  verifyClient: security.wsVerifier({ auth: AUTH, allowedHosts: ALLOWED_HOSTS, trustedHosts: TRUSTED_HOSTS }),
});

app.use(security.httpGuard({ auth: AUTH, allowedHosts: ALLOWED_HOSTS }));
app.use(express.static(path.join(__dirname, 'dist')));

// ─── Config ─────────────────────────────────────────────────────────────────
const THUMB_SIZE = 128;
const THUMB_CACHE_DIR = path.join(__dirname, '.thumb_cache');
const THUMB_SIZES = [64, 128, 256];   // requested sizes are rounded up to one of these
const THUMB_CACHE_MAX_BYTES = (Number(process.env.CVL_THUMB_CACHE_MB) || 512) * 1024 * 1024;
const WATCH_RESTART_MIN_MS = 2000;   // a new watch restarts a full scan: rate-limit it
const STATS_INTERVAL_MS = 2000;
const MAX_CONCURRENT_READS = 8;       // max parallel file reads
const WATCHER_BATCH_DELAY_INIT = 500; // ms batch delay during initial scan
const WATCHER_BATCH_DELAY_LIVE = 200; // ms batch delay after scan complete

// Ensure cache dir (only sync call at startup, before any client connects)
if (!fs.existsSync(THUMB_CACHE_DIR)) fs.mkdirSync(THUMB_CACHE_DIR, { recursive: true });

// ─── Semaphore (limits concurrent async operations) ─────────────────────────
class Semaphore {
  constructor(max) { this.max = max; this.current = 0; this.queue = []; }
  async acquire() {
    if (this.current < this.max) { this.current++; return; }
    return new Promise(resolve => this.queue.push(resolve));
  }
  release() {
    this.current--;
    if (this.queue.length > 0) { this.current++; this.queue.shift()(); }
  }
}
const readSemaphore = new Semaphore(MAX_CONCURRENT_READS);

// ─── State ───────────────────────────────────────────────────────────────────
let currentWatcher = null;
let imageFiles = [];          // sorted by mtime
let imageFileIndex = new Map(); // path → index for O(1) dedup
let watchPattern = '';
let watchActive = false;

// Rate tracking — separated: initial scan vs live
let initialScanDone = false;
let initialScanCount = 0;       // images found during initial scan
let scanCompleteTime = 0;       // Date.now() when scan finished
let liveTimestamps = [];        // timestamps of images found AFTER scan
let lastFoundAt = null;         // Date.now() when last image was detected by watcher

let rateHistory = [];           // { t, liveCount } — only live images
let lastRateHistoryMinute = -1;

// Disk space (cached, refreshed every 10s)
let diskInfo = null;           // { total, used, free, percent }
let watchVolumePath = null;    // real, existing directory inside a data root (set by startWatching)

// Disk usage via statfs only. Never shell out: the path comes from a client-supplied pattern.
async function updateDiskInfo() {
  if (!watchVolumePath) return;
  try {
    const stats = await fsp.statfs(watchVolumePath);
    const total = stats.blocks * stats.bsize;
    const free = stats.bfree * stats.bsize;
    const used = total - free;
    diskInfo = {
      total,
      used,
      free,
      percent: total > 0 ? Math.round((used / total) * 1000) / 10 : 0,
    };
  } catch (e) {
    diskInfo = null; // disk info unavailable
  }
}

// ─── MRC header validation ──────────────────────────────────────────────────
// Only little-endian modes 0/1/2/6 are decoded by this viewer. Anything else (big-endian,
// float16, complex, truncated or non-MRC files) is rejected instead of rendered as garbage,
// and absurd dimensions can no longer trigger huge allocations.
const MRC_BPP = { 0: 1, 1: 2, 2: 4, 6: 2 };
const MAX_MRC_DIM = 65536;

function parseMrcHeader(buf, bytesRead, fileSize) {
  if (bytesRead < 1024) return { error: 'not an MRC file (header shorter than 1024 bytes)' };
  if (buf[212] === 0x11) return { error: 'big-endian MRC files are not supported by this viewer' };
  const nx = buf.readInt32LE(0);
  const ny = buf.readInt32LE(4);
  const nz = buf.readInt32LE(8);
  const mode = buf.readInt32LE(12);
  const mx = buf.readInt32LE(28);
  const cellA = buf.readFloatLE(40);
  const nsymbt = buf.readInt32LE(92);
  const bpp = MRC_BPP[mode];
  if (!bpp) return { error: `MRC mode ${mode} is not supported by this viewer` };
  if (!(nx >= 1 && ny >= 1 && nz >= 1 && nx <= MAX_MRC_DIM && ny <= MAX_MRC_DIM)) {
    return { error: 'invalid MRC dimensions' };
  }
  if (!(nsymbt >= 0)) return { error: 'invalid MRC extended header size' };
  const dataOffset = 1024 + nsymbt;
  if (fileSize < dataOffset + nx * ny * bpp) return { error: 'incomplete MRC file (still being written?)' };
  return { nx, ny, nz, mode, bpp, nsymbt, dataOffset, pixelSize: mx > 0 ? cellA / mx : 0 };
}

async function openMrc(filePath) {
  const fh = await fsp.open(filePath, 'r');
  try {
    const st = await fh.stat();
    const hdr = Buffer.alloc(1024);
    const { bytesRead } = await fh.read(hdr, 0, 1024, 0);
    return { fh, header: parseMrcHeader(hdr, bytesRead, st.size) };
  } catch (e) {
    await fh.close().catch(() => {});
    throw e;
  }
}

// ─── Async MRC Header (1024 bytes, non-blocking) ───────────────────────────
async function readMrcHeaderAsync(filePath) {
  let fh;
  try {
    fh = await fsp.open(filePath, 'r');
    const buf = Buffer.alloc(1024);
    const { bytesRead } = await fh.read(buf, 0, 1024, 0);
    // Listing only needs the header: do not require the data to be complete yet.
    const h = parseMrcHeader(buf, bytesRead, Infinity);
    return h.error ? null : h;
  } catch (e) { return null; }
  finally { if (fh) await fh.close().catch(() => {}); }
}

// ─── Async thumbnail generation (strided reads, semaphore-limited) ──────────
// Returns { width, height, data }, { error } for an invalid/unsupported file, or null on I/O error.
async function generateThumbnailAsync(filePath, size) {
  let fh;
  try {
    const opened = await openMrc(filePath);
    fh = opened.fh;
    if (opened.header.error) return { error: opened.header.error };
    const { nx, ny, mode, bpp, dataOffset } = opened.header;

    const maxDim = Math.max(nx, ny);
    const scale = Math.min(1, size / maxDim);
    const tnx = Math.max(1, Math.round(nx * scale));
    const tny = Math.max(1, Math.round(ny * scale));
    const strideX = nx / tnx;
    const strideY = ny / tny;

    const samples = new Float32Array(tnx * tny);
    const rowBuf = Buffer.alloc(nx * bpp);
    const statSamples = [];
    const statStride = Math.max(1, Math.floor(tnx / 40));

    for (let ty = 0; ty < tny; ty++) {
      const sy = Math.min(ny - 1, Math.floor(ty * strideY));
      await fh.read(rowBuf, 0, nx * bpp, dataOffset + sy * nx * bpp);
      for (let tx = 0; tx < tnx; tx++) {
        const sx = Math.min(nx - 1, Math.floor(tx * strideX));
        let val;
        switch (mode) {
          case 0: val = rowBuf.readInt8(sx); break;
          case 1: val = rowBuf.readInt16LE(sx * 2); break;
          case 2: val = rowBuf.readFloatLE(sx * 4); break;
          case 6: val = rowBuf.readUInt16LE(sx * 2); break;
          default: val = rowBuf.readFloatLE(sx * 4);
        }
        samples[ty * tnx + tx] = val;
        if (tx % statStride === 0) statSamples.push(val);
      }
    }

    statSamples.sort((a, b) => a - b);
    const p02 = statSamples[Math.floor(statSamples.length * 0.02)] || 0;
    const p98 = statSamples[Math.floor(statSamples.length * 0.98)] || 1;
    const range = p98 - p02 || 1;

    const thumb = Buffer.alloc(tnx * tny);
    for (let i = 0; i < tnx * tny; i++) {
      thumb[i] = Math.max(0, Math.min(255, Math.round(((samples[i] - p02) / range) * 255)));
    }
    return { width: tnx, height: tny, data: thumb };
  } catch (e) {
    console.error(`[Thumb] Error ${path.basename(filePath)}:`, e.message);
    return null;
  } finally { if (fh) await fh.close().catch(() => {}); }
}

// ─── Async cached thumbnail ─────────────────────────────────────────────────
// In-flight dedup: if multiple clients request the same thumb simultaneously,
// only one generation runs and all await the same promise.
const thumbInFlight = new Map();

// The cache key includes the file size and mtime, so a thumbnail made from a partially
// written file is regenerated once the file changes.
async function getCachedThumbnailAsync(filePath, size, st) {
  const hash = crypto.createHash('md5')
    .update(`${filePath}:${size}:${st.size}:${st.mtimeMs}`).digest('hex');
  const cachePath = path.join(THUMB_CACHE_DIR, hash + '.raw');
  const metaPath = path.join(THUMB_CACHE_DIR, hash + '.json');

  // Check disk cache (async)
  try {
    const [data, metaRaw] = await Promise.all([fsp.readFile(cachePath), fsp.readFile(metaPath, 'utf8')]);
    const meta = JSON.parse(metaRaw);
    return { width: meta.w, height: meta.h, data };
  } catch (e) { /* cache miss */ }

  // Dedup: if already generating this exact thumbnail, wait for it
  const key = hash;
  if (thumbInFlight.has(key)) return thumbInFlight.get(key);

  const promise = (async () => {
    await readSemaphore.acquire();
    try {
      const thumb = await generateThumbnailAsync(filePath, size);
      if (!thumb || thumb.error) return thumb;
      // Write cache (fire-and-forget, don't block response)
      const meta = JSON.stringify({ w: thumb.width, h: thumb.height });
      Promise.all([fsp.writeFile(cachePath, thumb.data), fsp.writeFile(metaPath, meta)])
        .then(() => accountThumbCache(thumb.data.length + meta.length))
        .catch(() => {});
      return thumb;
    } finally {
      readSemaphore.release();
      thumbInFlight.delete(key);
    }
  })();

  thumbInFlight.set(key, promise);
  return promise;
}

// ─── Thumbnail disk cache bound (bytes) ─────────────────────────────────────
let thumbCacheBytes = 0;
let thumbCachePruning = false;

async function thumbCacheEntries() {
  const names = await fsp.readdir(THUMB_CACHE_DIR);
  const entries = [];
  for (const name of names) {
    try {
      const st = await fsp.stat(path.join(THUMB_CACHE_DIR, name));
      entries.push({ name, size: st.size, mtime: st.mtimeMs });
    } catch { /* removed meanwhile */ }
  }
  return entries;
}

function accountThumbCache(bytes) {
  thumbCacheBytes += bytes;
  if (thumbCacheBytes > THUMB_CACHE_MAX_BYTES && !thumbCachePruning) pruneThumbCache();
}

// Delete the oldest cache files until the cache is below 80 % of its limit.
async function pruneThumbCache() {
  thumbCachePruning = true;
  try {
    const entries = await thumbCacheEntries();
    entries.sort((a, b) => a.mtime - b.mtime);
    let total = entries.reduce((acc, e) => acc + e.size, 0);
    for (const e of entries) {
      if (total <= THUMB_CACHE_MAX_BYTES * 0.8) break;
      await fsp.unlink(path.join(THUMB_CACHE_DIR, e.name)).catch(() => {});
      total -= e.size;
    }
    thumbCacheBytes = total;
  } catch (e) {
    console.error('[Thumb] Cache pruning failed:', e.message);
  } finally {
    thumbCachePruning = false;
  }
}

thumbCacheEntries()
  .then(entries => accountThumbCache(entries.reduce((acc, e) => acc + e.size, 0)))
  .catch(() => {});

// ─── Async MRC data for preview ─────────────────────────────────────────────
// Returns the preview data, { error } for an invalid/unsupported file, or null on I/O error.
async function readMrcDataAsync(filePath) {
  let fh;
  try {
    const opened = await openMrc(filePath);
    fh = opened.fh;
    if (opened.header.error) return { error: opened.header.error };
    const { nx, ny, mode, bpp, dataOffset, pixelSize } = opened.header;

    const downscale = Math.max(1, Math.ceil(Math.max(nx, ny) / 2048));
    const onx = Math.ceil(nx / downscale);
    const ony = Math.ceil(ny / downscale);
    const pixels = new Float32Array(onx * ony);
    const rowBuf = Buffer.alloc(nx * bpp);

    for (let oy = 0; oy < ony; oy++) {
      const sy = Math.min(ny - 1, oy * downscale);
      await fh.read(rowBuf, 0, nx * bpp, dataOffset + sy * nx * bpp);
      for (let ox = 0; ox < onx; ox++) {
        const sx = Math.min(nx - 1, ox * downscale);
        switch (mode) {
          case 0: pixels[oy * onx + ox] = rowBuf.readInt8(sx); break;
          case 1: pixels[oy * onx + ox] = rowBuf.readInt16LE(sx * 2); break;
          case 2: pixels[oy * onx + ox] = rowBuf.readFloatLE(sx * 4); break;
          case 6: pixels[oy * onx + ox] = rowBuf.readUInt16LE(sx * 2); break;
          default: pixels[oy * onx + ox] = rowBuf.readFloatLE(sx * 4);
        }
      }
    }
    return { nx: onx, ny: ony, origNx: nx, origNy: ny, mode, pixelSize, pixels };
  } catch (e) { return null; }
  finally { if (fh) await fh.close().catch(() => {}); }
}

// ─── Broadcast (skip broken sockets) ────────────────────────────────────────
function broadcast(msg) {
  const data = JSON.stringify(msg);
  for (const c of wss.clients) {
    if (c.readyState === WebSocket.OPEN) {
      try { c.send(data); } catch (e) { /* skip dead socket */ }
    }
  }
}

// Safe per-client send
function safeSend(ws, msg) {
  if (ws.readyState === WebSocket.OPEN) {
    try { ws.send(typeof msg === 'string' ? msg : JSON.stringify(msg)); }
    catch (e) { /* skip */ }
  }
}

// ─── Stats ──────────────────────────────────────────────────────────────────
function computeStats() {
  const now = Date.now();

  const lastByMtime = imageFiles.length > 0 ? imageFiles[imageFiles.length - 1] : null;

  // Instantaneous rate: live images found in last 5 minutes
  const INSTANT_WINDOW = 5 * 60 * 1000;
  const recentCount = liveTimestamps.filter(t => now - t < INSTANT_WINDOW).length;
  const rateInstant = Math.round(recentCount / (INSTANT_WINDOW / 3600000));

  // liveTimestamps.length is the exact count of non-initial images
  const liveCount = liveTimestamps.length;
  const actualInitialCount = imageFiles.length - liveCount;

  // Average rate: live images / hours since scan complete
  let rateAvg = 0;
  if (scanCompleteTime > 0 && liveCount > 0) {
    const hoursSinceScan = (now - scanCompleteTime) / 3600000;
    rateAvg = hoursSinceScan > 0 ? Math.round(liveCount / hoursSinceScan) : 0;
  }

  return {
    type: 'stats',
    totalImages: imageFiles.length,
    initialScanCount: actualInitialCount,
    liveCount,
    rateInstant,
    rateAvg,
    lastCreatedTime: lastByMtime ? lastByMtime.mtime : null,
    lastFoundAt,
    timeSinceCreated: lastByMtime ? now - lastByMtime.mtime : null,
    timeSinceFound: lastFoundAt ? now - lastFoundAt : null,
    watchPattern,
    watchActive,
    scanDone: initialScanDone,
    disk: diskInfo,
  };
}

function updateRateHistory() {
  if (!initialScanDone) return;
  const now = Date.now();
  const minute = Math.floor(now / 60000);
  if (minute !== lastRateHistoryMinute) {
    lastRateHistoryMinute = minute;
    rateHistory.push({ t: now, liveCount: liveTimestamps.length });
    if (rateHistory.length > 1440) rateHistory = rateHistory.slice(-1440);
  }
}

// ─── File Watcher ───────────────────────────────────────────────────────────
// `watch` is the result of security.validateWatchPattern() (never a raw client string).
function startWatching(watch) {
  const pattern = watch.pattern;
  if (currentWatcher) { currentWatcher.close(); currentWatcher = null; }

  imageFiles = [];
  imageFileIndex.clear();
  liveTimestamps = [];
  rateHistory = [];
  lastRateHistoryMinute = -1;
  initialScanDone = false;
  initialScanCount = 0;
  scanCompleteTime = 0;
  lastFoundAt = null;
  watchPattern = pattern;
  watchActive = true;

  // Existing directory of the pattern, already resolved and confined to a data root
  watchVolumePath = watch.volumeDir;
  diskInfo = null;
  updateDiskInfo(); // initial check

  console.log(`[Watch] Starting: ${pattern}`);
  broadcast({ type: 'watch_started', pattern });

  const pendingBatch = [];
  let batchTimer = null;
  let chokidarReady = false;
  let initialAddsEmitted = 0;   // sync counter of add events before 'ready'

  function flushBatch() {
    if (pendingBatch.length === 0) return;
    const batch = pendingBatch.splice(0);
    broadcast({ type: 'new_images_batch', images: batch, total: imageFiles.length });
    updateRateHistory();
  }

  currentWatcher = chokidar.watch(pattern, {
    persistent: true,
    ignoreInitial: false,
    followSymlinks: true,
    awaitWriteFinish: { stabilityThreshold: 2000, pollInterval: 1000 },
    usePolling: true,
    interval: 3000,
  });

  currentWatcher.on('add', (filePath) => {
    if (imageFileIndex.has(filePath)) return;

    const ext = path.extname(filePath).toLowerCase();
    const isJpeg = ext === '.jpg' || ext === '.jpeg';
    const isMrc = ext === '.mrc' || ext === '.mrcs';
    if (!isJpeg && !isMrc) return;

    // Tag SYNCHRONOUSLY before any async: was this add emitted before chokidar 'ready'?
    const isFromInitialScan = !chokidarReady;
    if (isFromInitialScan) initialAddsEmitted++;

    (async () => {
      try {
        // Symlinks are followed, but only files that resolve inside a data root are listed.
        if (!(await security.realpathInsideRoots(filePath, ROOTS))) return;
        const stat = await fsp.stat(filePath);
        const now = Date.now();
        const entry = {
          path: filePath,
          name: path.basename(filePath),
          mtime: stat.mtimeMs,
          size: stat.size,
          fileType: isJpeg ? 'jpeg' : 'mrc',
          foundAt: now,
          isInitial: isFromInitialScan,
          nx: 0, ny: 0, pixelSize: 0,
        };

        if (isMrc) {
          const header = await readMrcHeaderAsync(filePath);
          if (header) {
            entry.nx = header.nx;
            entry.ny = header.ny;
            entry.pixelSize = header.pixelSize;
          }
        }

        if (imageFileIndex.has(filePath)) return;
        imageFileIndex.set(filePath, imageFiles.length);
        imageFiles.push(entry);
        lastFoundAt = now;

        // Only live images feed the rate metrics
        if (!isFromInitialScan) {
          liveTimestamps.push(now);
        }

        pendingBatch.push(entry);
        if (batchTimer) clearTimeout(batchTimer);
        batchTimer = setTimeout(flushBatch, initialScanDone ? WATCHER_BATCH_DELAY_LIVE : WATCHER_BATCH_DELAY_INIT);
      } catch (e) {}
    })();
  });

  currentWatcher.on('ready', () => {
    chokidarReady = true;
    initialScanDone = true;
    initialScanCount = initialAddsEmitted;
    scanCompleteTime = Date.now();

    // Sort + reindex whatever we have so far (more entries may arrive from pending async)
    imageFiles.sort((a, b) => a.mtime - b.mtime);
    imageFileIndex.clear();
    imageFiles.forEach((f, i) => imageFileIndex.set(f.path, i));
    flushBatch();
    broadcast({ type: 'scan_complete', total: imageFiles.length, initialScanCount });
    console.log(`[Watch] Scan complete: ${initialAddsEmitted} initial files detected, ${imageFiles.length} processed so far`);
    updateRateHistory();
  });

  currentWatcher.on('error', (err) => console.error('[Watch] Error:', err.message));
}

function stopWatching() {
  if (currentWatcher) { currentWatcher.close(); currentWatcher = null; }
  imageFiles = [];
  imageFileIndex.clear();
  liveTimestamps = [];
  rateHistory = [];
  lastRateHistoryMinute = -1;
  initialScanDone = false;
  initialScanCount = 0;
  scanCompleteTime = 0;
  lastFoundAt = null;
  watchPattern = '';
  watchActive = false;
  watchVolumePath = null;
  diskInfo = null;
  fsp.readdir(THUMB_CACHE_DIR).then(files => {
    for (const f of files) fsp.unlink(path.join(THUMB_CACHE_DIR, f)).catch(() => {});
  }).catch(() => {});
  thumbInFlight.clear();
  thumbCacheBytes = 0;
  broadcast({ type: 'watch_stopped' });
  console.log('[Watch] Stopped — all state cleared');
}

// Stats broadcast interval
setInterval(() => {
  if (wss.clients.size > 0 && watchActive) {
    broadcast(computeStats());
    updateRateHistory();
  }
}, STATS_INTERVAL_MS);

// Disk space refresh (every 10s, async)
setInterval(() => {
  if (watchActive && watchVolumePath) updateDiskInfo();
}, 10000);

// ─── REST API ───────────────────────────────────────────────────────────────

// Resolve the `path` query parameter of a file request. The file must belong to the
// current watch and resolve (realpath) inside a data root. Returns { filePath, real, st },
// or sends an error response and returns null.
async function resolveServedFile(req, res) {
  const filePath = req.query.path;
  if (typeof filePath !== 'string' || !filePath) { res.status(400).end(); return null; }
  const real = await security.realpathInsideRoots(filePath, ROOTS);
  if (!real) { res.status(403).end(); return null; }
  if (!imageFileIndex.has(filePath)) { res.status(404).end(); return null; }
  let st;
  try { st = await fsp.stat(real); } catch { res.status(404).end(); return null; }
  if (!st.isFile()) { res.status(404).end(); return null; }
  return { filePath, real, st };
}

// Private caching with revalidation: the ETag changes with size/mtime, so a file that was
// still being written is not served stale from the browser cache. Returns true for a 304.
function notModified(req, res, st, variant = '') {
  res.set({
    'Cache-Control': 'private, no-cache',
    'ETag': `W/"${st.size.toString(16)}-${Math.floor(st.mtimeMs).toString(16)}${variant}"`,
    'Last-Modified': st.mtime.toUTCString(),
  });
  if (req.fresh) { res.status(304).end(); return true; }
  return false;
}

function sendJpeg(res, real, extraHeaders = {}) {
  res.set({ 'Content-Type': 'image/jpeg', ...extraHeaders });
  const stream = fs.createReadStream(real);
  stream.on('error', () => { if (!res.headersSent) res.status(500); res.end(); });
  stream.pipe(res);
}

// Thumbnail (async, semaphore-limited, deduped)
app.get('/api/thumbnail', async (req, res) => {
  try {
    const f = await resolveServedFile(req, res);
    if (!f) return;
    const wanted = parseInt(req.query.size, 10) || THUMB_SIZE;
    const size = THUMB_SIZES.find(s => s >= wanted) || THUMB_SIZES[THUMB_SIZES.length - 1];
    if (notModified(req, res, f.st, `-${size}`)) return;

    const ext = path.extname(f.filePath).toLowerCase();
    if (ext === '.jpg' || ext === '.jpeg') return sendJpeg(res, f.real);

    // MRC thumbnail (async + semaphore)
    const thumb = await getCachedThumbnailAsync(f.real, size, f.st);
    if (!thumb) return res.status(500).end();
    if (thumb.error) return res.status(422).type('text').send(thumb.error);
    res.set({
      'Content-Type': 'application/octet-stream',
      'X-Width': thumb.width, 'X-Height': thumb.height, 'X-File-Type': 'mrc',
    });
    res.send(thumb.data);
  } catch (e) {
    if (!res.headersSent) res.status(500).end();
  }
});

// Full image data (async)
app.get('/api/image-data', async (req, res) => {
  try {
    const f = await resolveServedFile(req, res);
    if (!f) return;
    if (notModified(req, res, f.st)) return;

    const ext = path.extname(f.filePath).toLowerCase();
    if (ext === '.jpg' || ext === '.jpeg') return sendJpeg(res, f.real, { 'X-File-Type': 'jpeg' });

    // MRC (async + semaphore)
    await readSemaphore.acquire();
    let data;
    try { data = await readMrcDataAsync(f.real); }
    finally { readSemaphore.release(); }
    if (!data) return res.status(500).end();
    if (data.error) return res.status(422).type('text').send(data.error);

    res.set({
      'Content-Type': 'application/octet-stream',
      'X-NX': data.nx, 'X-NY': data.ny, 'X-Orig-NX': data.origNx, 'X-Orig-NY': data.origNy,
      'X-Pixel-Size': data.pixelSize, 'X-Mode': data.mode, 'X-File-Type': 'mrc',
    });
    res.send(Buffer.from(data.pixels.buffer));
  } catch (e) {
    if (!res.headersSent) res.status(500).end();
  }
});

// Paginated image list
app.get('/api/images', (req, res) => {
  const start = parseInt(req.query.start) || 0;
  const count = Math.min(500, parseInt(req.query.count) || 100);
  res.json({ total: imageFiles.length, images: imageFiles.slice(start, start + count) });
});

// Rate history
app.get('/api/rate-history', (req, res) => {
  let data = rateHistory;
  const maxPts = parseInt(req.query.max) || 300;
  if (data.length > maxPts) {
    const step = Math.ceil(data.length / maxPts);
    data = data.filter((_, i) => i % step === 0);
  }
  res.json(data);
});

// ─── WebSocket ──────────────────────────────────────────────────────────────
let lastWatchStart = 0;
wss.on('connection', (ws) => {
  console.log(`[WS] Client connected (total: ${wss.clients.size})`);

  // Send current state
  safeSend(ws, computeStats());

  // Send image list newest-first in chunks — client sees latest data immediately
  const CHUNK = 500;
  let cursor = imageFiles.length;
  function sendNextChunk() {
    if (cursor <= 0 || ws.readyState !== WebSocket.OPEN) {
      if (imageFiles.length > 0) safeSend(ws, { type: 'sync_complete', total: imageFiles.length });
      return;
    }
    const start = Math.max(0, cursor - CHUNK);
    const chunk = imageFiles.slice(start, cursor);
    chunk.reverse(); // newest first within chunk
    safeSend(ws, { type: 'image_list_chunk', images: chunk, total: imageFiles.length });
    cursor = start;
    setImmediate(sendNextChunk);
  }
  sendNextChunk();

  ws.on('message', async (msg) => {
    let data;
    try { data = JSON.parse(msg); } catch (e) { return; }
    try {
      if (data && data.type === 'start_watch') {
        const watch = await security.validateWatchPattern(data.pattern, ROOTS);
        if (!watch.ok) {
          console.warn(`[Watch] Refused pattern: ${watch.error}`);
          safeSend(ws, { type: 'watch_error', message: watch.error });
          return;
        }
        if (Date.now() - lastWatchStart < WATCH_RESTART_MIN_MS) {
          safeSend(ws, { type: 'watch_error', message: 'A watch was just started: wait a few seconds.' });
          return;
        }
        lastWatchStart = Date.now();
        startWatching(watch);
      } else if (data && data.type === 'stop_watch') {
        stopWatching();
      }
    } catch (e) {
      console.error('[WS] Error handling message:', e.message);
    }
  });

  ws.on('close', () => console.log(`[WS] Client disconnected (total: ${wss.clients.size})`));
  ws.on('error', () => {}); // prevent unhandled error crashes
});

// SPA fallback
app.get('*', (req, res) => res.sendFile(path.join(__dirname, 'dist', 'index.html')));

// ─── Graceful shutdown ──────────────────────────────────────────────────────
function shutdown() {
  console.log('\n[Shutdown] Closing...');
  if (currentWatcher) currentWatcher.close();
  wss.clients.forEach(c => c.terminate());
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000); // force exit after 5s
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

const PORT = process.env.PORT === undefined ? 4200 : Number(process.env.PORT);
server.listen(PORT, HOST, () => {
  const { port } = server.address();
  console.log(`[Server] CryoViewer Live listening on http://${HOST.includes(':') ? `[${HOST}]` : HOST}:${port}` +
    (AUTH ? ' (Basic auth enabled)' : ' (no auth)'));
});
