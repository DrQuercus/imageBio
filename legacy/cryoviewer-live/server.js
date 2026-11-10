const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const path = require('path');
const fs = require('fs');
const fsp = require('fs').promises;
const chokidar = require('chokidar');
const cors = require('cors');
const crypto = require('crypto');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'dist')));

// ─── Config ─────────────────────────────────────────────────────────────────
const THUMB_SIZE = 128;
const THUMB_CACHE_DIR = path.join(__dirname, '.thumb_cache');
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
let watchVolumePath = null;    // resolved dir from watch pattern

async function updateDiskInfo() {
  if (!watchVolumePath) return;
  try {
    // Use statfs (Node 18.15+)
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
    // Fallback: try df command
    try {
      const { exec } = require('child_process');
      const result = await new Promise((resolve, reject) => {
        exec(`df -B1 "${watchVolumePath}"`, (err, stdout) => {
          if (err) return reject(err);
          resolve(stdout);
        });
      });
      const lines = result.trim().split('\n');
      if (lines.length >= 2) {
        const parts = lines[1].split(/\s+/);
        const total = parseInt(parts[1]) || 0;
        const used = parseInt(parts[2]) || 0;
        const free = parseInt(parts[3]) || 0;
        diskInfo = { total, used, free, percent: total > 0 ? Math.round((used / total) * 1000) / 10 : 0 };
      }
    } catch (e2) { /* disk info unavailable */ }
  }
}

// ─── Async MRC Header (1024 bytes, non-blocking) ───────────────────────────
async function readMrcHeaderAsync(filePath) {
  let fh;
  try {
    fh = await fsp.open(filePath, 'r');
    const buf = Buffer.alloc(1024);
    const { bytesRead } = await fh.read(buf, 0, 1024, 0);
    if (bytesRead < 1024) return null;
    const nx = buf.readInt32LE(0);
    const ny = buf.readInt32LE(4);
    const mode = buf.readInt32LE(12);
    const mx = buf.readInt32LE(28);
    const cellA = buf.readFloatLE(40);
    const nsymbt = buf.readInt32LE(92);
    return { nx, ny, nz: buf.readInt32LE(8), mode, pixelSize: mx > 0 ? cellA / mx : 0, nsymbt };
  } catch (e) { return null; }
  finally { if (fh) await fh.close().catch(() => {}); }
}

// ─── Async thumbnail generation (strided reads, semaphore-limited) ──────────
async function generateThumbnailAsync(filePath, size) {
  let fh;
  try {
    fh = await fsp.open(filePath, 'r');
    const hdr = Buffer.alloc(1024);
    await fh.read(hdr, 0, 1024, 0);

    const nx = hdr.readInt32LE(0);
    const ny = hdr.readInt32LE(4);
    const mode = hdr.readInt32LE(12);
    const nsymbt = hdr.readInt32LE(92);
    const bpp = { 0: 1, 1: 2, 2: 4, 6: 2 }[mode] || 4;
    const dataOffset = 1024 + nsymbt;

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

async function getCachedThumbnailAsync(filePath, size) {
  const hash = crypto.createHash('md5').update(filePath + ':' + size).digest('hex');
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
      if (!thumb) return null;
      // Write cache (fire-and-forget, don't block response)
      fsp.writeFile(cachePath, thumb.data).catch(() => {});
      fsp.writeFile(metaPath, JSON.stringify({ w: thumb.width, h: thumb.height })).catch(() => {});
      return thumb;
    } finally {
      readSemaphore.release();
      thumbInFlight.delete(key);
    }
  })();

  thumbInFlight.set(key, promise);
  return promise;
}

// ─── Async MRC data for preview ─────────────────────────────────────────────
async function readMrcDataAsync(filePath) {
  let fh;
  try {
    fh = await fsp.open(filePath, 'r');
    const hdr = Buffer.alloc(1024);
    await fh.read(hdr, 0, 1024, 0);

    const nx = hdr.readInt32LE(0);
    const ny = hdr.readInt32LE(4);
    const mode = hdr.readInt32LE(12);
    const nsymbt = hdr.readInt32LE(92);
    const mx = hdr.readInt32LE(28);
    const cellA = hdr.readFloatLE(40);
    const pixelSize = mx > 0 ? cellA / mx : 0;
    const bpp = { 0: 1, 1: 2, 2: 4, 6: 2 }[mode] || 4;
    const dataOffset = 1024 + nsymbt;

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
function startWatching(pattern) {
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

  // Resolve volume path from glob pattern — take the part before any glob chars
  const globIdx = pattern.search(/[\*\?\[\{]/);
  const basePath = globIdx > 0 ? pattern.substring(0, globIdx) : pattern;
  watchVolumePath = path.dirname(basePath) || '/';
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

// Async file existence check
async function fileExists(p) {
  try { await fsp.access(p, fs.constants.R_OK); return true; }
  catch { return false; }
}

// Thumbnail (async, semaphore-limited, deduped)
app.get('/api/thumbnail', async (req, res) => {
  try {
    const filePath = req.query.path;
    const size = Math.min(256, parseInt(req.query.size) || THUMB_SIZE);
    if (!filePath) return res.status(400).end();
    if (!(await fileExists(filePath))) return res.status(404).end();

    const ext = path.extname(filePath).toLowerCase();
    if (ext === '.jpg' || ext === '.jpeg') {
      res.set({ 'Content-Type': 'image/jpeg', 'Cache-Control': 'public, max-age=86400' });
      const stream = fs.createReadStream(filePath);
      stream.on('error', () => { if (!res.headersSent) res.status(500); res.end(); });
      stream.pipe(res);
      return;
    }

    // MRC thumbnail (async + semaphore)
    const thumb = await getCachedThumbnailAsync(filePath, size);
    if (!thumb) return res.status(500).end();
    res.set({
      'Content-Type': 'application/octet-stream',
      'X-Width': thumb.width, 'X-Height': thumb.height, 'X-File-Type': 'mrc',
      'Cache-Control': 'public, max-age=86400',
    });
    res.send(thumb.data);
  } catch (e) {
    if (!res.headersSent) res.status(500).end();
  }
});

// Full image data (async)
app.get('/api/image-data', async (req, res) => {
  try {
    const filePath = req.query.path;
    if (!filePath) return res.status(400).end();
    if (!(await fileExists(filePath))) return res.status(404).end();

    const ext = path.extname(filePath).toLowerCase();
    if (ext === '.jpg' || ext === '.jpeg') {
      res.set({ 'Content-Type': 'image/jpeg', 'X-File-Type': 'jpeg', 'Cache-Control': 'public, max-age=3600' });
      const stream = fs.createReadStream(filePath);
      stream.on('error', () => { if (!res.headersSent) res.status(500); res.end(); });
      stream.pipe(res);
      return;
    }

    // MRC (async + semaphore)
    await readSemaphore.acquire();
    let data;
    try { data = await readMrcDataAsync(filePath); }
    finally { readSemaphore.release(); }
    if (!data) return res.status(500).end();

    res.set({
      'Content-Type': 'application/octet-stream',
      'X-NX': data.nx, 'X-NY': data.ny, 'X-Orig-NX': data.origNx, 'X-Orig-NY': data.origNy,
      'X-Pixel-Size': data.pixelSize, 'X-Mode': data.mode, 'X-File-Type': 'mrc',
      'Cache-Control': 'public, max-age=3600',
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

  ws.on('message', (msg) => {
    try {
      const data = JSON.parse(msg);
      if (data.type === 'start_watch') startWatching(data.pattern);
      else if (data.type === 'stop_watch') stopWatching();
    } catch (e) {}
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

const PORT = process.env.PORT || 4200;
server.listen(PORT, '0.0.0.0', () => {
  console.log(`\n  ╔════════════════════════════════════════╗`);
  console.log(`  ║  CryoViewer Live — running on :${PORT}    ║`);
  console.log(`  ╚════════════════════════════════════════╝\n`);
});
