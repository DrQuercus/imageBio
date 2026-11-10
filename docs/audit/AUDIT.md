# Audit of the existing code (CryoViewer + CryoViewer Live)

> Generated on 2026-11-10 by a multi-agent audit: one deep auditor per app, then an independent adversarial verifier per app that tried to refute each finding (medium and above) by reading the code and reproducing it. "Final" severity is the one adjusted by the verifier. `unverified-low` = low severity, not submitted to the verifier. Line numbers refer to the archive versions (`CryoViewer.zip`, `cryoviewer-live.tar.gz`) at the repo root.

> This appendix is kept in English (raw material for development sessions). The summary and the plan are in [`../PLAN.md`](../PLAN.md).


## CryoViewer (server.py)

### How it works

CryoViewer is one Python file, server.py (1880 lines). It holds a stdlib web server, the numpy processing code, and the whole UI as an HTML/CSS/JS string (UI, lines 611-1781).

Backend:
- parse_mrc (L32-60) reads the 1024-byte header. Endianness comes from the stamp byte 212 (0x11 means big-endian), with a check on the mode field as fallback. apix = CELLA.x / MX, and the data offset is 1024 + NSYMBT.
- read_mrc_frame (L62-84) seeks to one frame and reads only that frame, so a movie is never read whole.
- EER (L90-119): it only walks the TIFF IFDs to count frames. Each "frame" it returns is seeded Poisson noise.
- TIFF/JPG/PNG (L125-221): every page of the file is loaded at open time as float32. tifffile is tried first and PIL is the fallback.
- Processing (L227-292): Gaussian low-pass in Fourier space (rfft2, no padding), percentile auto-contrast, gamma/invert down to uint8, and a 256-bin histogram over the full min..max range.
- CryoFile (L298-377) keeps a FIFO cache of 24 frames per file, counted in frames, not bytes.
- The State singleton ST (L383-434) holds every opened CryoFile forever, plus cwd and current-file state. That state is global and shared by every client.

HTTP layer:
- A single-threaded http.server.HTTPServer, hard-bound to 127.0.0.1 (L1869), with HTTP/1.1 keep-alive and no socket timeout.
- GET endpoints: /api/browse, /api/load, /api/frame, /api/demo, /api/list.
- Each /api/frame request runs the whole pipeline: read (or average) frames, low-pass, contrast, encode a full-resolution PNG, base64 it, and return it inside JSON with stats and histogram. Nothing is cached after the raw frame, and an average is recomputed on every request.
- Upload: a subclass that reuses the name, `class Handler(Handler)` (L1790), adds POST /api/upload. It reads the whole body into RAM and writes a temp file that is never deleted.
- A monkey-patch (`State.list = _patched_list`, L1836-1846) handles names for uploaded files. `_orig_do_GET` and `_orig_list` are dead code.

Frontend:
- Vanilla JS with a global S state object. Zoom/pan is a CSS transform on #stage.
- The ruler and pixel readout draw on a 2D canvas overlay, in canvas pixel units (equal to image pixels, because the PNG is full resolution).
- Every control change calls fetchFrame() directly or through debounceFetch(). Nothing sequences, cancels or limits requests: the player fires one setTimeout per tick whether or not the previous frame came back.
- The file list and the "Opened" list are built as HTML strings with names and paths inserted unescaped into innerHTML and inline onclick handlers.
- Fonts load from Google Fonts.

Tests: 17 pytest cases cover the parser, processing and loaders, and all pass. Nothing covers HTTP, concurrency or the UI.

### Strengths

- MRC read path is economical: one frame at a time with seek+read, no whole-file read and no memmap needed. Modes 0/1/2/6/12 match mrcfile exactly (checked against mrcfile-written files, 5 frames each), little- and big-endian.
- Endianness detection (machine stamp first, then a mode-field sanity check) and the NSYMBT extended-header offset are correct. A file with an FEI1 extended header (3 x 768 bytes) decoded correctly, offset 3328.
- Low-pass filter is defined correctly and documented: measured gain is 0.516 at the 20 Å cutoff (designed 0.5), DC is preserved, it uses rfft2, and it handles non-square and odd-sized images (301x517 tested).
- Wheel zoom keeps the point under the cursor fixed (pan update formula checked), and the ruler's pixel -> Å -> nm conversion is right while the PNG is shown at full resolution.
- Very few dependencies (numpy, pillow, tifffile) and a single file to deploy, plus a macOS double-click launcher. Works on a microscope PC with no build step.
- UX ideas worth keeping: a CryoSPARC-like server-side file browser, low-pass presets in Å, a keyboard-driven workflow (frames, A/I/L/R/P/S), a log-scale histogram with window shading, and frame averaging.
- There is a working pytest suite (17 tests, all passing). It is a good start for regression tests in a rewrite.

### Verdict: what to keep and what to rewrite

CryoViewer is a useful prototype and a clear statement of what screening needs. It is not a base to build the unified app on. Keep the ideas and a few hundred lines of algorithms; rewrite the server, the state model and the UI.

Worth keeping (ported, with tests):
- The MRC frame-seek reader and header logic (endianness, NSYMBT offset, modes 0/1/2/6/12 verified against mrcfile). Better: use mrcfile (mmap, validation, FEI1/FEI2 extended header dtypes) as the base and keep a small layer of extensions for what mrcfile lacks:
  - mode 101 with row padding (CV-11)
  - mode 16 RGB (CV-12)
  - the IMOD unsigned-byte flag (CV-13)
  - nz checked against file size (CV-28)
- The Gaussian low-pass definition (0.5 gain at the cutoff in Å, apix-aware), with padding or taper and caching added (CV-18).
- Robust percentile auto-contrast, log histogram and frame averaging, rebuilt on robust ranges and NaN-safe stats (CV-17, CV-19).
- UX concepts:
  - server-side file browser in the CryoSPARC style
  - Å-based LP presets
  - keyboard-driven navigation
  - ruler in Å/nm
  - cursor-anchored zoom (the math is correct)
- The 17 tests, as the seed of a regression suite.

Rewrite:
- HTTP layer: single-threaded keep-alive server, global shared state, monkey-patches (CV-1, CV-29).
- Transport: a full PNG in base64 JSON per slider move (CV-20).
- Memory model: count-based cache, float32 copies of whole TIFF stacks, files never evicted (CV-6).
- Security model: no auth, any path, XSS, tracebacks (CV-9, CV-10).
- The string-embedded vanilla-JS UI: request races, broken avg range, wiped Opened list, no fit-to-window (CV-5, CV-7, CV-21, CV-23).
- EER support: today it is a fake. Use real decoding via tifffile + imagecodecs (CV-2).

For the unified, ImageJ-like app, one design fits both CryoViewer and Live: a proper Python (FastAPI/uvicorn) or Java backend with a bounded byte-LRU tile cache, lazy or mmap readers (mrcfile, tifffile + imagecodecs, EER decoding, and possibly bioformats/SCIFIO for 'any format'), and binary tiles at multiple resolutions. In front of it, a typed frontend (React/TypeScript, which Live already uses) with WebGL windowing/LUTs and true pixel values, plus ROI/overlay drawing in screen space. Live can then reuse the same readers and viewer component instead of keeping a second implementation.

### Findings table

| ID | Final severity | Verification | Category | Location | Problem |
|---|---|---|---|---|---|
| CV-1 | **critical** (initially critical) | confirmed | robustness | `server.py:1869` | Single-threaded HTTPServer + HTTP/1.1 keep-alive: a second tab or user is blocked indefinitely |
| CV-10 | **high** (initially high) | confirmed | security | `server.py:522` | No authentication, any path on disk is browsable and openable, no Host check, tracebacks returned, bind address hard-coded |
| CV-2 | **high** (initially high) | confirmed | scientific-accuracy | `server.py:117` | EER files are shown as synthetic Poisson noise with a hard-coded 0.5 Å/px, and the UI gives no warning |
| CV-3 | **high** (initially high) | confirmed | correctness | `server.py:391` | Symlinked files cannot be displayed (RELION Movies/ and Import are symlinks), and the error is hidden by a success toast |
| CV-5 | **high** (initially high) | confirmed | performance | `server.py:1665` | Frame player and sliders flood the server with unthrottled requests; responses arrive out of sync and the backlog takes tens of seconds to drain |
| CV-6 | **high** (initially high) | confirmed | performance | `server.py:299` | Unbounded memory: 24-frame cache regardless of frame size, files never closed, TIFF stacks fully expanded to float32 |
| CV-7 | **high** (initially high) | confirmed | correctness | `server.py:859` | Frame-average range inputs do nothing: the full movie is always averaged |
| CV-9 | **high** (initially high) | confirmed | security | `server.py:1297` | Stored XSS through file and directory names put into innerHTML and inline onclick |
| CV-13 | **medium** (initially medium) | confirmed | scientific-accuracy | `server.py:23` | IMOD unsigned 8-bit convention ignored: bytes 128-255 wrap to negative values |
| CV-15 | **medium** (initially medium) | confirmed | scientific-accuracy | `server.py:54` | Pixel size not read from FEI1/FEI2 extended headers; silently defaults to 1.0 Å/px |
| CV-16 | **medium** (initially medium) | confirmed | scientific-accuracy | `server.py:150` | TIFF pixel size: ImageJ-style metadata gives 0.001 Å/px, inch units are ignored, and the PIL fallback drops all but cm |
| CV-17 | **medium** (initially medium) | confirmed | robustness | `server.py:291` | A single NaN or Inf pixel makes the image impossible to display |
| CV-19 | **medium** (initially medium) | confirmed | ux | `server.py:291` | Histogram and contrast sliders span the full min..max: one hot pixel makes them useless |
| CV-20 | **medium** (initially medium) | confirmed | performance | `server.py:274` | Every adjustment re-renders a full-resolution PNG on the server and ships it as base64 inside JSON |
| CV-21 | **medium** (initially medium) | confirmed | ux | `server.py:1108` | No fit-to-window: large micrographs open at 1:1 and show only the centre |
| CV-22 | **medium** (initially medium) | confirmed | scientific-accuracy | `server.py:1272` | Pixel 'val' readout shows the 0-255 display byte, not the data value |
| CV-25 | **medium** (initially medium) | confirmed | robustness | `server.py:1801` | Drag & drop upload: whole file in browser and server RAM, no size limit, temp files never deleted, old handles leaked |
| CV-4 | **medium** (initially high) | confirmed | robustness | `server.py:431` | One broken symlink makes a whole directory impossible to browse |
| CV-11 | **low** (initially medium) | confirmed | correctness | `server.py:66` | MRC mode 101 (4-bit) is decoded wrongly when nx is odd: rows are padded to whole bytes |
| CV-12 | **low** (initially medium) | confirmed | correctness | `server.py:28` | MRC mode 16 is RGB (3 x uint8) in MRC2014/IMOD but is read as single-channel uint8 |
| CV-14 | **low** (initially medium) | confirmed | correctness | `server.py:57` | Unknown or complex MRC modes (3, 4, and anything else) are silently decoded as float32 |
| CV-18 | **low** (initially medium) | confirmed | scientific-accuracy | `server.py:249` | Low-pass filter has no padding or edge taper: content wraps across opposite edges |
| CV-23 | **low** (initially medium) | confirmed | ux | `server.py:1360` | The 'Opened' file list is wiped immediately by every directory refresh |
| CV-24 | **low** (initially medium) | confirmed | correctness | `server.py:1763` | `python3 server.py /path/to/movies` (documented) has no visible effect |
| CV-26 | **low** (initially low) | unverified-low | ux | `server.py:1741` | Keyboard shortcuts stop working after any slider is touched; modifier keys not checked |
| CV-27 | **low** (initially low) | unverified-low | robustness | `server.py:178` | TIFF loader swallows every exception, needs imagecodecs for LZW (not in requirements), and crashes on hyperstacks |
| CV-28 | **low** (initially low) | unverified-low | robustness | `server.py:58` | Header nz is trusted: truncated or still-being-written files report frames that do not exist |
| CV-29 | **low** (initially low) | unverified-low | architecture | `server.py:1790` | Patch-style code structure: Handler subclass that reuses its parent's name, duplicated _send, global monkey-patch of State.list, dead variables |
| CV-30 | **low** (initially low) | unverified-low | ux | `server.py:1238` | Ruler line and label are drawn in image-pixel units, so they vanish when zoomed out on large images |
| CV-31 | **low** (initially low) | unverified-low | deployment | `server.py:617` | Offline lab networks: UI depends on Google Fonts; README and UI defaults disagree |
| CV-8 | **low** (initially low) | unverified-low | ux | `server.py:1671` | Two separate 'Avg' toggles flip the same state but update different buttons |

**Missed by the audit, found and confirmed by the verifier:**

| Severity | Location | Problem |
|---|---|---|
| **medium** | `server.py:221` | Palette-mode (P) PNG/TIFF images are shown as palette indices, not intensities |
| **medium** | `server.py:315` | 2-channel (LA) images and 4-D ImageJ hyperstacks (ZCYX/TZCYX) fail to open with 'too many values to unpack' |
| **low** | `server.py:1644` | Uploaded files cannot be re-activated or restored after page reload |
| **low** | `server.py:1671` | Toolbar 'Avg' button and 'S' key share S.avgMode but update different UI elements, so the state gets out of sync |
| **low** | `server.py:1791` | POST to any path is treated as an upload, with no Origin check (CSRF) |
| **low** | `server.py:1693` | Info panel labels TIFF/PNG/JPEG files as 'MRC mode img' |

### Details

#### CV-1: Single-threaded HTTPServer + HTTP/1.1 keep-alive: a second tab or user is blocked indefinitely

- **Location**: `server.py:1869`; **severity** critical (initially critical); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: main() uses http.server.HTTPServer, which is single-threaded, while Handler sets protocol_version='HTTP/1.1' and sends 'Connection: keep-alive' (L486, L498), with no socket timeout. After a browser's first request, the server stays inside handle() waiting for the next request on that idle keep-alive socket. No other connection is accepted until the browser closes it. Effects: (a) while one tab is open, a second tab or second user cannot even load '/'; (b) inside one tab, any request Chrome sends on a second pooled connection stalls. This shows up as random UI freezes. Any heavy request (an average of a long movie) also freezes every client for its whole duration. The app cannot be deployed for several users on a lab network as it stands.
- **Evidence**: Raw-socket test: client A does GET /api/list and keeps the connection open; client B's GET /api/list fails with 'BLOCKED -> TimeoutError after 4.0 s'; as soon as A closes, B gets 200. Real browser (Playwright + Chrome 153): with tab 1 idle on the UI, a 2nd browser context doing page.goto('/') -> 'goto timed out after 12 s' (repeated twice). While an avg+LP20 request on 40 x 2048^2 frames was running, /api/list waited 0.53 s (the heavy request took 0.58 s).
- **Verifier's note**: main() uses http.server.HTTPServer (single-threaded, L1869). Handler sets protocol_version='HTTP/1.1' (L486) and sends Connection: keep-alive (L498) with no timeout. BaseHTTPRequestHandler.handle() therefore loops on the idle socket and never accepts another connection. Within one tab, a request on a second pooled connection stalls; a second tab or user cannot load at all. Critical given the goal of deploying on the lab network. For the current localhost single-user use it is high: random freezes during playback and debounced requests. The minimal fix (ThreadingHTTPServer) is trivial but not thread-safe as written, because of the shared file handle and cache.
- **Suggested fix**: Use ThreadingHTTPServer at minimum, plus a handler timeout. Better: move to an ASGI server (FastAPI/uvicorn) with CPU work in a thread or process pool, and per-file locks or os.pread, because CryoFile shares one file handle and a non-thread-safe cache. Keep all per-user view state in the client, not in the global ST.

#### CV-10: No authentication, any path on disk is browsable and openable, no Host check, tracebacks returned, bind address hard-coded

- **Location**: `server.py:522`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: /api/browse lists any directory (/etc included). /api/load accepts any path, with no allowed root and no extension check: anything that is not an image extension or .eer is parsed as MRC, so the bytes of an arbitrary binary file are returned as pixels. Error responses include full Python tracebacks with absolute paths (L540, L585). The Host header is not validated, so DNS rebinding from a malicious web page can reach the localhost server. For the stated goal (deploy on the lab network), the bind address is hard-coded to 127.0.0.1 with no --host option. Changing it would expose everything above to the network without auth. Note that ST.cwd/current are global, so users would also overwrite each other's state.
- **Evidence**: GET /api/browse?dir=/etc -> 200, 80 entries. /api/load?path=<secret.bin> -> 200 nx=16 ny=2 (non-image binary opened). /api/load?path=/etc/hostname -> 500 with 'trace' containing the server's absolute path. GET with Host: evil.example.com -> 200.
- **Verifier's note**: All sub-claims hold. /api/browse accepts any directory. /api/load opens anything as MRC with no root restriction or signature check. Tracebacks with absolute paths are returned (L540, L585). There is no Host or Origin check, and the bind address is hard-coded to 127.0.0.1. Mitigating context: the README states it is localhost-only with no auth ('ne pas l'exposer sur un réseau'), so today the realistic vectors are DNS rebinding and the XSS from CV-9. For the stated network-deployment goal, these are blocking prerequisites.
- **Suggested fix**: Configure data roots, normalise paths and reject anything outside them (after resolve). Add authentication (LDAP/OIDC or a reverse proxy) for network mode, a Host/Origin allow-list, and add a --host flag only together with those. Never return tracebacks to the client; log them on the server. Keep per-session state.

#### CV-2: EER files are shown as synthetic Poisson noise with a hard-coded 0.5 Å/px, and the UI gives no warning

- **Location**: `server.py:117`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: read_eer_frame() ignores the file and returns rng.poisson(0.01) seeded only by the frame index, so every EER file in the world shows the same 'frames'. parse_eer() hard-codes apix=0.5 (L114) whatever the detector or magnification. The UI's File Info panel just says 'EER' (L1693), and the stats, histogram, ruler and low-pass all run on the noise. A scientist will read it as a real empty or very-low-dose frame. The README states the limitation, but the UI never does. Also, parse_eer reads the BigTIFF tag value at offset 8 instead of 12 and reads SHORT values as 4 bytes (L109-110), which only works for little-endian classic TIFF.
- **Evidence**: Fake .eer (TIFF pages containing a bright 128x128 square on zeros): CryoFile reports nframes 5, apix 0.5; frame mean 0.0105, inside the square 0.0099, outside 0.0117, so the content is completely ignored. read_eer_frame(hdrA,0) == read_eer_frame(hdrB,0) -> True for two different files.
- **Verifier's note**: read_eer_frame (L117-119) ignores the file and returns Poisson(0.01) noise seeded only by idx. parse_eer hard-codes apix=0.5 (L114). The UI Info panel only says 'EER' (L1693). The README documents it as a synthetic preview, but nothing in the UI does. BigTIFF sub-claim is also correct: 20-byte entries carry the value at offset 12, but the code reads offset 8 (the high half of count -> 0). SHORT read as 'I' only works in little-endian. EER at real frame counts (1000+) also makes averaging generate thousands of 4k noise arrays and block the server.
- **Suggested fix**: Decode EER for real: tifffile together with imagecodecs (eer_decode) handles EER compression 65000/65001/65002. Sum frame fractions (e.g. 40 EER frames per rendered frame), and support 1x/2x/4x super-resolution grids. Read pixel size from the EER XML metadata tag, or require the user to set it. Until then, refuse to render EER, or put a large SYNTHETIC banner over it.

#### CV-3: Symlinked files cannot be displayed (RELION Movies/ and Import are symlinks), and the error is hidden by a success toast

- **Location**: `server.py:391`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: State.open() stores the CryoFile under Path(path).resolve(), the symlink target. The UI keeps the unresolved browser path in S.file and sends it to /api/frame, which checks `path not in ST.files` (L546) and returns {'error':'File not loaded'}. loadFile() catches that error inside fetchFrame(), then still calls showViewer() and toasts 'Loaded: <target name>' (L1322-1324), which overwrites the error toast. The user sees an empty 300x150 canvas. Every later frame step fails the same way. RELION projects (Import/, Movies/) and many facility layouts are made of symlinks, so this hits real use.
- **Evidence**: HTTP: /api/load?path=sess/link.mrc -> 200 (info.path = .../real.mrc); /api/frame?path=sess/link.mrc -> 200 {'error':'File not loaded'}. Chrome: clicking link.mrc -> toasts ['err:File not loaded','ok:Loaded: real.mrc'], viewer canvas stays [300,150]; ArrowRight -> 'err:File not loaded'.
- **Verifier's note**: State.open stores the key as Path(path).resolve() (L391). The UI keeps the unresolved path in S.file (L1322), and /api/frame looks it up with `path not in ST.files` (L546). fetchFrame catches its own error and toasts it (L1407-1411), then loadFile still calls showViewer and toasts 'Loaded: ...' (L1323-1324). Symlinked movie files (RELION Movies/ links, facility layouts) are common, so a whole class of files is unviewable.
- **Suggested fix**: Have /api/load return the canonical key and make the client use only that key. Better, use opaque file IDs. Resolve paths the same way in every endpoint. In loadFile, do not show the success toast or the viewer if fetchFrame failed (let it rethrow).

#### CV-5: Frame player and sliders flood the server with unthrottled requests; responses arrive out of sync and the backlog takes tens of seconds to drain

- **Location**: `server.py:1665`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: _tick() schedules setFrame() every 1000/fps ms whether or not the previous frame has arrived. fetchFrame() has no sequence number, AbortController or 'latest wins' guard, and img.onload draws whatever response decodes last. At 4k one frame costs about 1.2 s on the server (LP 0.74 s + PNG 0.36 s + contrast 0.09 s), so at the default 8 fps requests pile up in the single-threaded server's queue. The frame counter (S.frame) races ahead of the image actually shown, and pressing Pause does not stop the queued work. Combined with CV-1, this also locks out other users.
- **Evidence**: Chrome on a 40 x 2048^2 movie, LP 20 Å, fps 30, play for 3 s: '71 frame requests sent, 9 completed by stop; backlog drained 30.1 s after pressing pause' (30 s was the wait limit, so the queue had still not emptied). Direct timing at 4096^2: lowpass 0.74 s, png+b64 0.36 s, 11.8 MB of base64 per request.
- **Verifier's note**: _tick (L1665-1668) reschedules setFrame every 1000/fps ms whatever the state of the previous request. fetchFrame (L1381-1412) has no in-flight guard, sequence number or abort. renderImage draws whatever onload fires (L1453-1466), and Pause only clears the timer, not the queued requests. My own measurement at 4096^2 with LP gives about 1.1 s per frame against a 125 ms tick at the default 8 fps, so the backlog grows by about 9 requests per second of playback, serialised by the single-threaded server (CV-1). I did not rerun the browser measurement, but the code and timings fully support it.
- **Suggested fix**: Allow one request in flight per view and drop intermediate ones (or abort them with AbortController). Advance playback only when the frame has rendered. Preload or prefetch frames as binary at reduced size, and do contrast/gamma/LUT on the client (WebGL) so slider moves need no round-trip.

#### CV-6: Unbounded memory: 24-frame cache regardless of frame size, files never closed, TIFF stacks fully expanded to float32

- **Location**: `server.py:299`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: CryoFile.MAX_CACHE=24 counts frames, not bytes, and caches float32 copies. ST.files (L392-393) keeps every CryoFile and file handle open forever, with no eviction and no close(), so memory and file descriptors grow with every file clicked. load_image_file() reads the whole TIFF with tf.asarray() and then keeps a float32 copy of every page (L164-175). EPU/SerialEM TIFF movies (uint8/LZW) become about 4x their raw size, plus peak overhead, at open time, blocking the server meanwhile. On a screening session with 4k-8k movies the server will be OOM-killed. The README's 'memory-light' framing is wrong for TIFF.
- **Evidence**: 40 x 2048^2 int16 MRC: after one average, cache holds 24 frames = 403 MB, more than the 336 MB file itself. Extrapolated per open file: Falcon4 4096^2 1.61 GB, K3 5760x4092 2.26 GB, K3 super-res 11520x8184 9.05 GB, never released. TIFF movie 40 x 1024^2 uint8 (12.5 MB on disk, 42 MB raw) -> 168 MB resident, 210 MB peak at open (tracemalloc).
- **Verifier's note**: MAX_CACHE=24 counts frames and stores float32 copies for MRC (L299, L347-350). ST.files never evicts or closes anything (L392-393); CryoFile.close() is never called. load_image_file reads the whole TIFF with tf.asarray() and keeps a float32 copy of every page (L164-174). For a 4k MRC movie the cache alone is 24 x 64 MB = 1.6 GB per opened file. For TIFF the cache only references _frames (no extra copy), but _frames itself is the full movie in float32.
- **Suggested fix**: Use a global LRU cache bounded by bytes, shared across files. Keep frames in their native dtype (uint8/uint16/float16) and convert per request. Read TIFF pages lazily (tf.pages[i].asarray()) and memory-map MRC (mrcfile.mmap). Close and evict idle files. Cache computed averages keyed by (file, range).

#### CV-7: Frame-average range inputs do nothing: the full movie is always averaged

- **Location**: `server.py:859`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: The 'fr' / 'to' inputs (#avg-s, #avg-e) only call debounceFetch(). Nothing ever copies their values into S.avgS / S.avgE, which stay at 0 and nframes (set in applyInfo L1337 / toggleAvgRange L1683). fetchFrame sends avg_s=${S.avgS}&avg_e=${S.avgE} (L1405). The user believes they are looking at frames 5-6, for example to skip the first high-motion frames, but sees the average of all frames. The inputs are also 0-based with an exclusive end, while the frame counter is 1-based.
- **Evidence**: Chrome: press S (Avg ON), type avg-s=5, avg-e=6 -> the /api/frame requests sent carry avg_s/avg_e = '0..40'.
- **Verifier's note**: #avg-s/#avg-e only call debounceFetch() (L859, L861). The only writes to S.avgS/S.avgE are in applyInfo (L1337) and toggleAvgRange (L1683), which set 0 and nframes. fetchFrame sends S.avgS/S.avgE (L1405). The user-entered range is therefore never used, and the full-movie average is shown silently, which is misleading for scientific use.
- **Suggested fix**: In the oninput handlers, set S.avgS/S.avgE from the inputs and clamp them. Use 1-based inclusive labels that match the frame counter, and show the range used in the UI.

#### CV-9: Stored XSS through file and directory names put into innerHTML and inline onclick

- **Location**: `server.py:1297`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: renderBrowser() and renderLoaded() build HTML strings with ${e.name} unescaped and put paths inside onclick="...('${esc(path)}')". esc() only escapes backslash and single quote: double quotes break out of the attribute, and names can contain tags. On Linux a filename like `<img src=x onerror=...>.mrc` is valid. Anyone who can create a file on a shared storage server can run JavaScript in every viewer's session. That script can then call /api/browse and /api/load across the filesystem (see CV-10).
- **Evidence**: Created sess/'<img src=x onerror=window.__xss=1>.mrc'; Chrome, after browsing to the directory: window.__xss===1 -> true.
- **Verifier's note**: renderBrowser and renderLoaded put ${e.name}/${f.name} unescaped into innerHTML (L1301, L1308-1309, L1352). renderInfo puts info.filename into innerHTML (L1697). esc() only escapes backslash and single quote, so a double quote breaks out of the onclick attribute. Any user who can name a file on shared storage gets script execution in viewers' sessions, and that script can drive /api/browse and /api/load. High for network or shared-storage deployment.
- **Suggested fix**: Build the DOM with textContent / createElement and attach event listeners with data-* attributes, or use a framework that escapes by default (React, as in Live). Add a strict CSP that blocks inline handlers.

#### CV-13: IMOD unsigned 8-bit convention ignored: bytes 128-255 wrap to negative values

- **Location**: `server.py:23`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: Mode 0 is always read as int8. Under the MRC2014 standard that is right, but IMOD files with imodStamp=1146047817 (header offset 152) and imodFlags bit 0 clear (offset 156) store UNSIGNED bytes. Older IMOD/SerialEM byte stacks and binned tomograms (.rec/.st/.ali, the very extensions listed) then display with values 128-255 wrapped to negatives, which looks like broken, posterised contrast. mrcfile also reads int8 here, so this is a convention issue, but a viewer should honour the flag as IMOD does.
- **Evidence**: Mode-0 file with IMOD stamp, flags=0, true values 100-255: read range -128..127, correlation with truth -0.257.
- **Verifier's note**: Mode 0 is always read as int8 (L23), and the IMOD stamp/flags at offsets 152/156 are never consulted. IMOD (newstack/binvol -mode 0, byte tomograms) writes unsigned bytes with flag bit 0 clear, so values 128-255 wrap to negative. This matters for tomography users.
- **Suggested fix**: If imodStamp == 1146047817 and (imodFlags & 1) == 0, read mode 0 as uint8. Otherwise use int8.

#### CV-15: Pixel size not read from FEI1/FEI2 extended headers; silently defaults to 1.0 Å/px

- **Location**: `server.py:54`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: apix comes only from CELLA.x / MX. EPU/Falcon MRC files carry the pixel size (and dose, defocus, tilt, exposure time and more) in an FEI1/FEI2 extended header, which is skipped correctly but never parsed. When CELLA is 0, apix silently becomes 1.0. The ruler then reads in false Å, and the 'Å' low-pass cutoff applies at the wrong frequency, with no warning to the user that the value is a fallback.
- **Evidence**: mrcfile-written file with an FEI1 ext header, 'Pixel size X' = 0.83e-10 m, CELLA = 0: parse_mrc offset 3328 (correct), apix = 1.0.
- **Verifier's note**: apix comes only from CELLA.x / MX (L54), with a silent fallback to 1.0. The extended header is skipped (offset is correct) but never parsed, and the UI gives no indication that the value is a fallback. The real-world impact depends on whether EPU leaves CELLA unset, which I could not confirm. The code behaviour itself is confirmed.
- **Suggested fix**: Parse EXTTYP FEI1/FEI2 (mrcfile provides the dtypes) for pixel size and per-frame metadata, and show them in the Info panel. Visibly flag the pixel size as 'assumed' when it falls back.

#### CV-16: TIFF pixel size: ImageJ-style metadata gives 0.001 Å/px, inch units are ignored, and the PIL fallback drops all but cm

- **Location**: `server.py:150`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: With ResolutionUnit=NONE the code assumes 1/res is in Å. ImageJ (and Fiji-processed data) writes XResolution in pixels per 'unit' from ImageDescription (usually micron or nm), which gives values off by 10^4 that are then clamped to 0.001. INCH units are ignored (apix=1.0) even though they convert exactly. The heuristics described in the comments (L147-149) are not implemented. The PIL fallback path (L186-196), which is the one actually used for LZW TIFFs when imagecodecs is missing, honours only cm.
- **Evidence**: ImageJ TIFF at 0.83 Å/px (0.000083 µm, unit=um) -> apix read 0.001. Inch-unit TIFF at 0.83 Å/px -> 1.0. cm-unit TIFF -> 0.83 (correct).
- **Verifier's note**: With ResolutionUnit NONE, apix=1/res is assumed to be in Å (L152-154), the INCH branch is a hard 1.0 (L158-160), and the PIL fallback honours only cm (L193). ImageJ unit 'nm' is also wrong (off by 10x), not just µm. LZW TIFFs without imagecodecs (not in requirements.txt) do go through the PIL path, as claimed.
- **Suggested fix**: Use tifffile's imagej_metadata (unit, spacing) and the ImageDescription, convert inch/cm/µm/nm exactly, read SerialEM/EPU-specific tags, and show where the value came from.

#### CV-17: A single NaN or Inf pixel makes the image impossible to display

- **Location**: `server.py:291`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: np.histogram over the auto-detected range raises 'autodetected range of [nan, nan] is not finite'. auto_contrast returns (nan, nan), and stats like {'min': NaN} serialise to invalid JSON that the browser's JSON.parse rejects. Float32 maps and processed micrographs (masked areas, division by zero after gain normalisation) sometimes contain NaN/Inf, and such files simply fail with a 500.
- **Evidence**: mrcfile float32 64x64 with one NaN: make_histogram -> ValueError 'autodetected range of [nan, nan] is not finite'; auto_contrast -> (nan, nan); json.dumps(min) -> '{"min": NaN}'.
- **Verifier's note**: np.histogram raises on NaN or Inf (L291), so /api/frame returns 500 and the file cannot be displayed. Correction to the evidence: the invalid-JSON path ({"min": NaN}) is not reached, because make_histogram (L574) raises before the response is built. The user sees an error toast. Inf has the same effect.
- **Suggested fix**: Compute stats with nan-aware functions on finite values only (np.isfinite mask), replace non-finite pixels for display, and report the count of non-finite pixels.

#### CV-19: Histogram and contrast sliders span the full min..max: one hot pixel makes them useless

- **Location**: `server.py:291`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: make_histogram uses 256 bins over [min, max], and the black/white sliders map 0..10000 linearly onto dmin..dmax (L1526-1533). Raw counting-mode frames and gain-uncorrected TIFFs almost always contain hot pixels or outliers. The histogram then collapses into 1-2 bins and the slider step becomes larger than the whole useful range. Auto-contrast (2-98 %) still works, but manual adjustment, which a scientist needs, does not.
- **Evidence**: 1024^2 N(1, 0.3) image with one pixel = 60000: histogram uses 2/256 bins, bin width 234.4 (data std 0.3), slider step 6.00.
- **Verifier's note**: The histogram uses 256 bins over the full min..max (L291), and the sliders map 0..10000 linearly onto dmin..dmax (L1526-1533), so a single hot pixel collapses both. The numeric vmin/vmax inputs allow exact typing as a workaround, so it stays medium rather than high.
- **Suggested fix**: Build the histogram and slider range over robust percentiles (e.g. 0.1-99.9 %), with a toggle for the full range. Optionally offer hot-pixel or outlier clipping for display.

#### CV-20: Every adjustment re-renders a full-resolution PNG on the server and ships it as base64 inside JSON

- **Location**: `server.py:274`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: to_png_b64 is always called without max_dim, so the full-resolution image is PNG-encoded (compress_level=1 on noisy data), base64'd (+33%), wrapped in JSON, parsed in JS, then decoded again from a data URI. Each gamma, invert or contrast tick repeats the whole pipeline, including re-reading or re-averaging frames (get_average is not cached, L559) and recomputing the FFT low-pass. The README notes the missing LP cache, but the problem covers the whole pipeline.
- **Evidence**: 4096^2: lowpass 0.74 s + contrast 0.09 s + png/b64 0.36 s = about 1.2 s and 11.8 MB of base64 per request. 2048^2 frame request over HTTP: 0.18 s, 1.8 MB JSON; same with LP: 0.25 s. Re-requesting the same 40-frame average: 0.33 s every time (not cached).
- **Verifier's note**: to_png_b64 is never called with max_dim (L470, L577). Every contrast, gamma or invert change re-reads or re-averages frames, re-runs the FFT and re-encodes full-resolution PNG plus base64. With a 24-frame FIFO cache, averaging a 40-frame movie thrashes the cache and re-reads every frame each time.
- **Suggested fix**: Serve binary (application/octet-stream or WebSocket) uint16/float16 or 8-bit preview tiles at screen resolution, with a downsampled overview and full-resolution tiles on zoom. Do windowing, gamma, invert and LUTs on the client in WebGL. Cache filtered and averaged results server-side keyed by parameters.

#### CV-21: No fit-to-window: large micrographs open at 1:1 and show only the centre

- **Location**: `server.py:1108`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: At load, zoom=1 with a full-resolution canvas. A 4k or 8k micrograph shows a small central crop, and 'Reset' (resetView, L1108-1111) also goes to 1:1, not to 'fit'. Screening requires seeing the whole micrograph at once.
- **Evidence**: Chrome, 2048^2 image: canvas on screen 2048x2048 in a 888x716 viewport right after loading.
- **Verifier's note**: S.zoom starts at 1, and resetView sets zoom=1 (L1108-1111). #viewer has no max-width or fit CSS (L709-710), so a large micrograph renders at 1:1 inside an overflow:hidden container and only the centre is visible.
- **Suggested fix**: Compute zoom = min(viewportW/nx, viewportH/ny) on load and on Reset. Provide 1:1, Fit and percentage presets.

#### CV-22: Pixel 'val' readout shows the 0-255 display byte, not the data value

- **Location**: `server.py:1272`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: The cursor readout uses $vctx.getImageData(x,y).data[0], the 8-bit value after contrast, gamma, invert and low-pass. In a scientific viewer (ImageJ convention) 'value' means the raw pixel intensity (counts, e-/px, density). The readout changes when the user moves the contrast slider, which is misleading.
- **Evidence**: Chrome, mode-2 float file with data in 0..100: hovering pixel (10,10) -> 'val 31', a display byte.
- **Verifier's note**: The readout uses $vctx.getImageData(x,y).data[0] (L1272), which is the post-contrast/gamma/LP display byte, not the data value. There is also no bounds check, so outside the image it shows 0 with negative coordinates.
- **Suggested fix**: Return or keep the raw (or downsampled) data client-side and look up the true value, labelling raw vs filtered. Alternatively add a /api/pixel endpoint.

#### CV-25: Drag & drop upload: whole file in browser and server RAM, no size limit, temp files never deleted, old handles leaked

- **Location**: `server.py:1801`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: The client does file.arrayBuffer(), which fails or crashes the tab for movies of several GB (typical 1-10 GB). The server does rfile.read(Content-Length) into memory with no cap, then writes NamedTemporaryFile(delete=False), which is never removed. Re-dropping a file with the same name replaces ST.files[key] without closing the previous CryoFile, leaking a file descriptor and the temp file each time. /tmp fills up on a long-running lab server. A negative Content-Length means read(-1), which blocks on a keep-alive connection.
- **Evidence**: 3 POST /api/upload of the same file -> 3 new temp *.mrc files left in the temp dir (cleaned up afterwards by the test).
- **Verifier's note**: The client calls file.arrayBuffer() on the whole file (L1720). The server does rfile.read(length) with no cap (L1801) and NamedTemporaryFile(delete=False) with no cleanup (L1805). ST.files[key] is replaced without closing the old CryoFile (L1810). A negative Content-Length passes the ==0 check and calls read(-1), which blocks the single-threaded server.
- **Suggested fix**: For a network deployment, prefer opening files from shared storage over uploading. If upload stays, stream to disk in chunks with a size limit, then delete on close or eviction and at shutdown, and close the replaced CryoFile.

#### CV-4: One broken symlink makes a whole directory impossible to browse

- **Location**: `server.py:431`; **severity** medium (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: State.browse() calls item.stat() on every entry with an image extension. For a dangling symlink (common after raw data is moved or archived), is_dir() is False and the extension matches, so stat() raises FileNotFoundError. The whole listing then fails with HTTP 400. The same happens for any entry that raises PermissionError. One bad entry hides all the others.
- **Evidence**: Directory containing broken.mrc -> /nonexistent/movie.mrc: GET /api/browse?dir=<dir> -> 400 "[Errno 2] No such file or directory: '.../sess/broken.mrc'".
- **Verifier's note**: browse() calls item.stat() on dangling symlinks with an image extension (L428-431). The FileNotFoundError propagates and /api/browse returns 400 for the whole directory. Real and easy to fix, but it needs a dangling link to be present, so I lowered it to medium. PermissionError on stat of individual entries is rare, since stat needs only directory search permission.
- **Suggested fix**: Wrap each entry in try/except OSError, mark broken links in the listing, and skip unreadable entries. For EPU or RELION directories with 10k+ files, use os.scandir and paginate.

#### CV-11: MRC mode 101 (4-bit) is decoded wrongly when nx is odd: rows are padded to whole bytes

- **Location**: `server.py:66`; **severity** low (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: The frame size is computed as (nx*ny+1)//2 and the whole frame is unpacked as one contiguous nibble stream. Under the IMOD/MRC2014 convention for mode 101, each row starts on a byte boundary (an odd nx gets a padding nibble per row). Every row after the first is therefore shifted, and later frames are read from the wrong offset. The nibble order (low nibble first) is correct, and even widths decode correctly. mrcfile does not support mode 101, so the test file was built by hand to the IMOD spec.
- **Evidence**: nx=33, ny=10, nz=2, rows padded: frame 0 has 84.8% of pixels wrong, frame 1 93.9% wrong. Even nx=32 -> identical.
- **Verifier's note**: With odd nx, the 4-bit decode (L65-72) uses one contiguous nibble stream and ignores the per-row byte padding of the IMOD convention, so every row after the first shifts. Real mode-101 data (SerialEM K2/K3 4-bit counting) has even widths, so this almost never happens in practice. Hence low.
- **Suggested fix**: Use bytes_per_row = (nx+1)//2 and frame_bytes = bytes_per_row*ny. Unpack per row and trim to nx. Add a test for odd widths.

#### CV-12: MRC mode 16 is RGB (3 x uint8) in MRC2014/IMOD but is read as single-channel uint8

- **Location**: `server.py:28`; **severity** low (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: MRC_MODES maps 16 to np.uint8 with 1 byte per pixel. Mode 16 holds 3 bytes per pixel (RGB), so the frame read is the first third of the interleaved data, reshaped as garbage, and frame offsets in stacks are wrong by a factor of 3. The README advertises mode 16 as supported.
- **Evidence**: Hand-written mode-16 file 40x30 RGB: returned shape (30,40), correlation with true luminance = -0.007, i.e. noise.
- **Verifier's note**: MRC_MODES maps 16 -> np.uint8 with itemsize 1 (L28), while mode 16 is 3 x uint8 RGB, so the output is garbage and stack offsets are wrong by a factor of 3. The README advertises mode 16. It is rare in cryo-EM workflows, hence low.
- **Suggested fix**: Read nx*ny*3 bytes, reshape to (ny,nx,3), and convert to luminance or keep it as RGB. Compute frame stride with 3 bytes per pixel.

#### CV-14: Unknown or complex MRC modes (3, 4, and anything else) are silently decoded as float32

- **Location**: `server.py:57`; **severity** low (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: MRC_MODES.get(mode, np.float32) quietly treats every unsupported mode as float32. Mode 4 (complex64, e.g. FFT output) and mode 3 (complex int16) are shown as garbage with plausible-looking stats instead of an error. The file is not validated either: no MAP signature check and no check of file size against nx*ny*nz*itemsize.
- **Evidence**: mrcfile-written mode-4 file: header dtype float32, first values [0.2375, 0.788, 0.468] vs true |c| [0.823, 0.476, 0.461].
- **Verifier's note**: MRC_MODES.get(mode, np.float32) (L57) decodes modes 3, 4 and unknown modes as float32 with a 4-byte stride, so the output is garbage and the frame offsets are wrong. There is no MAP-signature or file-size validation (also visible in CV-10: a random binary opened with absurd dimensions). Complex maps are seldom opened in a screening viewer, hence low. The missing validation is the more important part.
- **Suggested fix**: Reject unknown modes with a clear message. Support complex modes by showing amplitude or log amplitude. Validate the 'MAP ' signature and the expected size.

#### CV-18: Low-pass filter has no padding or edge taper: content wraps across opposite edges

- **Location**: `server.py:249`; **severity** low (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: rfft2/irfft2 on the unpadded image treats it as periodic. Any intensity difference between opposite edges (ice gradients, carbon edge, beam fall-off) bleeds into the other side within about one filter width. Separately, when the requested cutoff is finer than Nyquist (e.g. 1 Å at 1.06 Å/px), freq_cut is clamped to 0.499 (L238), so the image is still filtered (std down 32%) although the user asked for a cutoff below Nyquist, which should leave it unchanged. The Gaussian definition itself (0.5 at cutoff) is correct.
- **Evidence**: 512^2 step image (0 on left half, 1000 on right): after LP 20 Å, column 0 = 446.8 (should be ~0) and column 511 = 553.2 (should be ~1000). Measured gain at cutoff 0.516 (OK). LP 1 Å @1.06 Å/px -> std ratio 0.682.
- **Verifier's note**: rfft2 without padding wraps around at the edges, and a cutoff finer than Nyquist is clamped to 0.499 and still filters. On realistic content (a linear ice gradient across 4096 px) the artefact is confined to the outer ~10-30 px at LP 20 Å, and the filter is used for display only. Many standard tools behave the same, so it is low, not medium.
- **Suggested fix**: Mirror- or mean-pad the image (or apply a cosine edge taper) before the FFT and crop after. Make cutoff < 2*apix a no-op and tell the user. Cache the filter mask per (shape, cutoff, apix). Use scipy.fft or pyfftw with float32 and multiple workers for speed.

#### CV-23: The 'Opened' file list is wiped immediately by every directory refresh

- **Location**: `server.py:1360`; **severity** low (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: renderLoaded() appends #loaded-sec inside #browser-list, but renderBrowser() replaces #browser-list.innerHTML entirely. loadFile() calls renderLoaded(d.list), then browseTo(...) at L1325, which wipes it. The 'Opened' section, the only way to switch between already-loaded files (setActive), is effectively never visible.
- **Evidence**: Chrome: after clicking real.mrc, document.getElementById('loaded-sec') -> null.
- **Verifier's note**: renderLoaded appends #loaded-sec inside #browser-list (L1343-1347). loadFile then calls browseTo (L1325), whose renderBrowser replaces #browser-list.innerHTML (L1312), so the section is wiped. The list survives only at initial page load until the first browse. Files can still be re-opened from the browser list, which is cheap since they are cached, so low.
- **Suggested fix**: Render the Opened list in its own container outside #browser-list, as a separate panel or tabs.

#### CV-24: `python3 server.py /path/to/movies` (documented) has no visible effect

- **Location**: `server.py:1763`; **severity** low (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: main() calls ST.scan(dir), which only appends paths to ST.order (loaded=False) and sets ST.cwd. The UI init then unconditionally calls browseTo('~'), so the browser shows the home directory. renderLoaded shows only loaded files, so the scanned files appear nowhere.
- **Evidence**: Server started with sess/ as argument: /api/list -> [('link.mrc', False), ('real.mrc', False)]; Chrome initial path input -> '/root'.
- **Verifier's note**: With a directory argument, main() only runs ST.scan, which adds unloaded entries. The UI init unconditionally calls browseTo('~') (L1763), which also overwrites ST.cwd, and renderLoaded shows only loaded files. The documented feature has no effect. The single-file argument does work (ST.current is set and init loads it). The impact is navigation convenience only, hence low.
- **Suggested fix**: Expose the start directory (e.g. /api/browse with no dir returns ST.cwd) and have the UI start there. Optionally auto-open the first file.

#### CV-26: Keyboard shortcuts stop working after any slider is touched; modifier keys not checked

- **Location**: `server.py:1741`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: yes
- **Problem**: The keydown handler returns early for any INPUT, and range sliders are INPUTs. After dragging the gamma, contrast, LP or frame slider, all shortcuts (Space, A, I, L, R, P, S) are dead until the user clicks elsewhere, and arrow keys move the focused slider instead of the frame. Ctrl or Cmd combinations are not excluded either: Cmd+R (reload) also toggles the ruler, and Cmd+- also zooms the canvas.
- **Evidence**: Chrome: click #gamma-sl, press 'i' -> S.invert unchanged.
- **Suggested fix**: Ignore only text-like inputs (type text/number) and contenteditable, blur range inputs on change, and skip events with e.ctrlKey/metaKey/altKey.

#### CV-27: TIFF loader swallows every exception, needs imagecodecs for LZW (not in requirements), and crashes on hyperstacks

- **Location**: `server.py:178`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: yes
- **Problem**: `except Exception: pass` hides every tifffile failure. LZW, the default compression of EPU/SerialEM TIFF movies, needs imagecodecs, which is not in requirements.txt, so these files silently fall back to PIL. PIL handles 8/16-bit, but slowly, page by page, and with the weaker pixel-size logic (CV-16). For 4-D ImageJ hyperstacks (TZYX), frames are 3-D, and `ny, nx = frames[0].shape` crashes with an unhelpful message.
- **Evidence**: tifffile on an LZW TIFF -> "ValueError <COMPRESSION.LZW: 5> requires the 'imagecodecs' package" (hidden), PIL fallback then decodes correctly. TZYX hyperstack -> 'ValueError too many values to unpack (expected 2)'.
- **Suggested fix**: Add imagecodecs to the requirements and log fallbacks. Use TiffFile.series and axes to map hyperstacks to a frame index (T/Z/C), or flatten with a clear dimension selector.

#### CV-28: Header nz is trusted: truncated or still-being-written files report frames that do not exist

- **Location**: `server.py:58`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: yes
- **Problem**: nframes = max(1, nz) with no check against the file size, and CryoFile caches the header forever. For a movie still being written (relevant to the Live integration) or a truncated transfer, the slider offers frames that raise 'EOF at frame N'. Mode 101 does not check for EOF at all and returns short or zero-padded data. A file that grows after opening is never re-read.
- **Evidence**: 10-frame mrcs truncated after 4.02 frames: nframes reported 10; get_frame(7) -> 'EOF at frame 7'.
- **Suggested fix**: Compute the number of available frames from the file size (min of header and size-derived) and show 'incomplete'. Re-stat on access, or watch for changes in Live mode.

#### CV-29: Patch-style code structure: Handler subclass that reuses its parent's name, duplicated _send, global monkey-patch of State.list, dead variables

- **Location**: `server.py:1790`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: no
- **Problem**: `class Handler(Handler)` shadows the original class to bolt on do_POST and re-declares an identical _send. `_orig_do_GET` and `_orig_list` are unused. State.list is overwritten at import time (L1846), so the method defined at L411 is dead and any importer (including the tests) gets the patched one. The UI is a 1170-line string inside the Python file, with no syntax checking or tests. None of this is a runtime bug today, but it makes the code hard to extend or test, and sharing it with the Live app is impractical.
- **Evidence**: Code reading: L1788 `_orig_do_GET = Handler.do_GET` (unused), L1835 `_orig_list = State.list` (unused), L1822-1832 duplicate of L490-500.
- **Suggested fix**: Split into packages (io/ readers, processing/, api/ routes, web/ static frontend). Write one handler/router per endpoint and real unit tests for API and UI.

#### CV-30: Ruler line and label are drawn in image-pixel units, so they vanish when zoomed out on large images

- **Location**: `server.py:1238`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: no
- **Problem**: The ruler canvas is the full image size and is scaled by the CSS transform. A 1.5 px line and 13 px font in image pixels become about 0.1-0.2 screen px at the 0.1x zoom needed to see an 8k micrograph whole. The readout box still works, but the overlay does not. The ruler is also a single segment with no persistence, angle or line profile.
- **Evidence**: Code reading: lineWidth=1.5 (L1220) and font '13px' (L1238) drawn into $rcv sized img.width x img.height, then scaled by S.zoom.
- **Suggested fix**: Draw overlays in screen space (a separate canvas sized to the viewport) and map image to screen coordinates, as ImageJ does for ROIs.

#### CV-31: Offline lab networks: UI depends on Google Fonts; README and UI defaults disagree

- **Location**: `server.py:617`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: no
- **Problem**: The UI loads fonts from fonts.googleapis.com. Microscope and acquisition PCs are often air-gapped, so the fonts fall back and the request may stall first paint. The README says the low-pass is '20 Å par défaut', but the UI starts with LP off (S.lp=0). The README also lists modes 16 and 101 and EER as supported (see CV-2, CV-11, CV-12).
- **Evidence**: Code reading: L616-617 <link href="https://fonts.googleapis.com/...">; L1044 lp: 0, lp_on: false vs README 'Filtre passe-bas ... 20 Å par défaut'.
- **Suggested fix**: Bundle the fonts locally (or use system fonts) and keep the README aligned with actual behaviour.

#### CV-8: Two separate 'Avg' toggles flip the same state but update different buttons

- **Location**: `server.py:1671`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: yes
- **Problem**: The top-bar 'Avg' button (toggleAvg) and the toolbar 'Avg: OFF' button / S key (toggleAvgRange) both invert S.avgMode, but each updates only its own button. They desync immediately: one shows ON while the other shows OFF, and the range inputs are hidden or shown inconsistently.
- **Evidence**: Chrome after pressing S: topbar btn-avg has class 'on' -> false, while btn-avgr text -> 'Avg: ON'.
- **Suggested fix**: Have a single toggle function that renders all controls from S.avgMode.

#### CRYOVIEWER-MISSED-1: Palette-mode (P) PNG/TIFF images are shown as palette indices, not intensities

- **Location**: `server.py:221`; **severity** medium
- **Problem**: _pil_to_float32 converts only RGB/RGBA to L. Mode 'P' falls through to np.array(img), which returns palette indices. An adaptive-palette grayscale PNG decodes with correlation -0.998 to the true intensities (inverted and posterised), and the stats and histogram are computed on the indices. Repro: work-verify-cryoviewer/t3.py.

#### CRYOVIEWER-MISSED-2: 2-channel (LA) images and 4-D ImageJ hyperstacks (ZCYX/TZCYX) fail to open with 'too many values to unpack'

- **Location**: `server.py:315`; **severity** medium
- **Problem**: load_image_file only reduces trailing RGB/RGBA axes (L167-172). A 4-D non-RGB TIFF (for example an ImageJ hyperstack with axes ZCYX) and LA/grey+alpha PNGs give frames that are 3-D, so `ny, nx = frames[0].shape` in CryoFile._load raises ValueError and the file cannot be opened. This conflicts with the 'any scientific format' goal. Repro t3.py: 'LA fail ValueError', '4D fail ValueError'.

#### CRYOVIEWER-MISSED-3: Uploaded files cannot be re-activated or restored after page reload

- **Location**: `server.py:1644`; **severity** low
- **Problem**: Upload keys look like '__upload__<name>'. setActive() and the init block (L1769) call /api/load?path=__upload__name, and ST.open resolves that as a relative path in the server's working directory, which gives FileNotFoundError and a 500. The init try/catch swallows the error, so after a reload the viewer stays empty without any message. setActive also sets S.file before the load succeeds.

#### CRYOVIEWER-MISSED-4: Toolbar 'Avg' button and 'S' key share S.avgMode but update different UI elements, so the state gets out of sync

- **Location**: `server.py:1671`; **severity** low
- **Problem**: toggleAvg() toggles S.avgMode and only #btn-avg. toggleAvgRange() toggles the same flag and only #btn-avgr and #avg-wrap. Using both leaves buttons showing ON while averaging is off, or the reverse.

#### CRYOVIEWER-MISSED-5: POST to any path is treated as an upload, with no Origin check (CSRF)

- **Location**: `server.py:1791`; **severity** low
- **Problem**: do_POST ignores self.path and has no Origin or Host check. With no X-Filename the name defaults to 'upload.mrc', so a cross-site HTML form POST (a simple request, no preflight) from any web page the user visits writes attacker bytes to a temp file and switches the server's ST.current.

#### CRYOVIEWER-MISSED-6: Info panel labels TIFF/PNG/JPEG files as 'MRC mode img'

- **Location**: `server.py:1693`; **severity** low
- **Problem**: renderInfo shows `MRC mode ${info.mode}` for every non-EER file, and image files have mode='img'. The format shown is wrong, a cosmetic bug.


## CryoViewer Live (server.js + React)

### How it works

CryoViewer Live is a single-server Node app: Express (server.js, 635 lines) serves the built React SPA from dist/ and exposes 4 REST endpoints plus one WebSocket. State is entirely in RAM in module-level globals (imageFiles[], imageFileIndex Map, liveTimestamps[], rateHistory[], diskInfo, watchPattern). There is exactly ONE watcher and ONE set of globals for the whole process, shared by every connected browser. A client sends {type:'start_watch',pattern} over WS; startWatching() (L334) wipes all globals and spins up chokidar.watch(pattern,{usePolling:true,interval:3000,awaitWriteFinish:{stabilityThreshold:2000,pollInterval:1000},ignoreInitial:false}). The 'add' handler (L380) filters to .jpg/.jpeg/.mrc, stats the file, parses a 1024-byte MRC header async, pushes an entry, and batches WS broadcasts. On chokidar 'ready' (L433) it marks the initial scan done and sorts imageFiles by mtime once. Live adds after 'ready' are push()ed without re-sort. A 2s interval broadcasts computeStats() to all clients, but only when ≥1 client is connected AND watchActive — so rateHistory (sampled once/minute inside that interval) has holes whenever no browser is open. Thumbnails: /api/thumbnail reads a strided subsample of the MRC (one byte per output pixel via nearest-neighbour decimation, L147-163), normalises to p2-p98, caches raw bytes to .thumb_cache keyed md5(path+size) with NO mtime/size component. Preview: /api/image-data decimates the MRC to ≤2048 on the long axis and ships the full float32 buffer (up to 16 MB) to the browser, which does Gaussian low-pass, brightness/contrast and a ruler in the canvas. Frontend (App.jsx) manages WS with exponential backoff; on every (re)connect it clears local images and the server re-streams the entire list newest-first in 500-item chunks. ImageGrid implements real row virtualisation. There is no persistence, no auth, no path confinement, and the server binds 0.0.0.0. dist/ is in sync with src/ (I rebuilt with vite and the JS bundle is byte-identical).

### Strengths

- Real virtualised grid in ImageGrid.jsx (only visible rows rendered, index-math reverse for newest-first, rAF-debounced scroll) — handles tens of thousands of tiles in the DOM cleanly, genuinely reusable.
- Thumbnail generation is strided/seeked, not whole-file: it reads only ~tny rows (L147-149) and caches to disk, so cost is roughly independent of movie frame count — on a 128 MB 8-frame movie the thumbnail returned in ~8 ms.
- In-flight thumbnail de-duplication (thumbInFlight Map, L184-218) plus a read semaphore (MAX_CONCURRENT_READS=8) sensibly bound concurrent disk I/O under a thumbnail stampede.
- WebSocket client logic is careful: exponential backoff capped at 15 s, mountedRef guards against setState-after-unmount, path Set for O(1) dedup, broadcast() skips dead sockets.
- Async-everywhere backend (fsp, streamed JPEGs) keeps the event loop responsive; MRC header read is a 1024-byte async read rather than loading the file.
- dist/ is NOT stale — it matches the current src/ (verified by rebuild + byte diff), so a deploy from this tree serves the right UI.
- Clean, consistent dark-theme UI and a useful stats/beacon/graph layout that maps well onto what a microscopist watches during a session.

### Verdict: what to keep and what to rewrite

Keep the shape, rewrite the numerics. Worth carrying into the unified app: (1) the whole live-monitoring concept and UX — path/glob watch, stats bar with acquisition rate and a color beacon, newest-first virtualised grid, rate/cumulative/heatmap graphs — this is a genuinely useful module and the React virtualisation in ImageGrid.jsx is solid and directly reusable; (2) the WS streaming + backoff/reconnect client pattern and the broadcast/safeSend server helpers; (3) the thumbnail architecture (seeked strided read + disk cache + in-flight dedup + read semaphore) is the right skeleton. What must be rewritten rather than ported: ALL the MRC/image decoding. The hand-rolled JS MRC parser has no endianness handling, is missing common modes (12/101), uses nearest-neighbour decimation that scientifically invalidates the thumbnails/previews, and gets the ruler pixel size wrong on large images. In the unified app, decoding should go through the Python mrcfile/numpy stack (or a shared Rust/WASM decoder) that the ImageJ-based core will already need, with proper binning — do not reimplement MRC in Node a second time. Security must be designed in from the start: the current 'absolute path from the client, stream any .jpg' model is a non-starter on a shared network and has to be replaced by server-side path confinement + auth + per-session watch scoping (the global-singleton watcher is the core architectural flaw). Net: reuse this as the UX/interaction reference and the live-feed plumbing, treat the file-format and security layers as throwaway, and fold the heavy image work into the same backend that serves the ImageJ/Cryo editor so there is one decoder, one cache, one auth model.

### Findings table

| ID | Final severity | Verification | Category | Location | Problem |
|---|---|---|---|---|---|
| LIVE-2 | **critical** (initially critical) | confirmed | security | `server.js:528` | Unauthenticated arbitrary file read of the whole lab network via path param |
| LIVE-1 | **high** (initially critical) | confirmed | correctness | `server.js:375` | Partial MRC emitted as 'complete' when the writer pauses — awaitWriteFinish is fooled, size/header never corrected |
| LIVE-3 | **high** (initially high) | confirmed | scientific-accuracy | `server.js:139` | Thumbnails and preview are nearest-neighbour decimated — aliasing destroys the signal on noisy micrographs |
| LIVE-5 | **high** (initially high) | confirmed | scientific-accuracy | `server.js:239` | Ruler and preview pixel-size are wrong by the decimation factor on ≥2k images |
| LIVE-4 | **medium** (initially high) | confirmed | correctness | `server.js:109` | MRC parser ignores endianness and unsupported modes — big-endian and float16 files render as garbage |
| LIVE-6 | **medium** (initially medium) | confirmed | performance | `server.js:556` | Full float32 image (up to 16 MB) shipped per preview; no bound and no further use of the raw |
| LIVE-7 | **medium** (initially high) | confirmed | architecture | `server.js:44` | Single global watcher + globals shared across all browsers; any client's start/stop clobbers everyone |
| LIVE-8 | **medium** (initially medium) | confirmed | correctness | `server.js:187` | Thumbnail disk cache is keyed on path+size only (not mtime) and is unbounded |
| LIVE-9 | **medium** (initially medium) | confirmed | performance | `server.js:371` | usePolling with 3 s interval is correct for NFS but O(files) per cycle — costly at 10k-50k files |
| LIVE-10 | **low** (initially medium) | refuted | correctness | `server.js:476` | Rate history only recorded while a browser is connected — graphs have holes for unattended collection |
| LIVE-11 | **low** (initially low) | unverified-low | robustness | `server.js:46` | No persistence: server restart (systemd Restart=on-failure) loses all state and forces a full re-scan |
| LIVE-12 | **low** (initially low) | unverified-low | performance | `server.js:587` | Every (re)connect re-streams the entire image list; at 50k files reconnect storms are expensive |

**Missed by the audit, found and confirmed by the verifier:**

| Severity | Location | Problem |
|---|---|---|
| **critical** | `server.js:84` | Unauthenticated OS command injection (RCE) via watch pattern -> df fallback in updateDiskInfo |
| **low** | `server.js:227` | No MRC validation: truncated/non-MRC files return HTTP 200 with zero/garbage pixels instead of an error |

### Details

#### LIVE-2: Unauthenticated arbitrary file read of the whole lab network via path param

- **Location**: `server.js:528`; **severity** critical (initially critical); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: /api/image-data and /api/thumbnail take an absolute path straight from req.query.path with only an R_OK existence check (fileExists, L491) — no confinement to watchVolumePath or any allow-list. The JPEG branch (L535-540) pipes the raw file bytes to the client, so ANY file the service user (louis) can read and whose path ends in .jpg/.jpeg is exfiltrated verbatim; the MRC branch reads arbitrary binary. The server binds 0.0.0.0 with no auth and cors() wide open, so this is reachable by anyone on the lab network. The WS also accepts any Origin (I connected with Origin: http://evil.example and received the full file list + stats), so a malicious web page a lab member visits can drive it cross-origin.
- **Evidence**: curl 'http://127.0.0.1:47311/api/image-data?path=<workdir>/secret.jpg' returned the literal file body 'SECRET_TOKEN=abc123'. The file was never part of any watch pattern. WS handshake with a forged Origin header still streamed stats + image_list_chunk payloads.
- **Verifier's note**: server.js:528-540 takes req.query.path with only an R_OK existence check (fileExists, line 491), no confinement to any watch root, and the .jpg/.jpeg branch streams the raw file verbatim; binds 0.0.0.0 (line 631) with app.use(cors()) wide-open (line 15) and no auth. wss 'connection' (line 581) does not validate Origin. All three reproduced: curl of a .jpg never in any watch pattern returned 'SECRET_TOKEN=abc123' verbatim via BOTH /api/image-data and /api/thumbnail; a WS handshake with Origin: http://evil.example opened and received stats (and would receive image_list_chunk when a watch is active). Correctly critical for a lab-network deployment (service runs as user louis per the systemd unit).
- **Suggested fix**: Resolve the requested path (fs.realpath) and reject anything not under the active watch root; never stream by file extension alone. Add authentication (even a shared token) before deploying on a network, bind to a trusted interface, restrict CORS origins, and validate the WebSocket Origin header in wss 'connection'. Treat the client-supplied glob similarly (it currently lets any client scan any directory).

#### LIVE-1: Partial MRC emitted as 'complete' when the writer pauses — awaitWriteFinish is fooled, size/header never corrected

- **Location**: `server.js:375`; **severity** high (initially critical); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: awaitWriteFinish treats a file as finished once its size is unchanged for stabilityThreshold (2000 ms). Real microscope writers (and NFS/SMB flushes) routinely stall mid-write for seconds. When that happens the add fires with a TRUNCATED file: the stat().size, the parsed MRC header (nx/ny/pixelSize), the thumbnail and the preview are all computed from the partial file. There is no 'change' handler (L380 only handles 'add'), so once the rest of the bytes land the entry is never updated — size, dimensions, pixel size and the cached thumbnail stay wrong for the life of the process. computeStats also counts the file as acquired the moment the partial lands, inflating the rate.
- **Evidence**: Wrote a 16,778,240-byte MRC in two steps: 30% (5,033,472 B) then a 9 s pause then the rest. Live WS log: '5.4s add liveC.mrc size 5033472 isInitial false' — fired while only 30% was on disk. /api/images then reported size 5033472 permanently even after the file reached full 16,778,240 B. In a second run the partial thumbnail (bottom ~70% of rows all 255) was byte-identical before and after completion because it was cached. Also header fields are read from the partial file.
- **Verifier's note**: Code confirms only an 'add' handler (server.js:380), no 'change' handler, and awaitWriteFinish gates on size-stability (line 375). Reproduced exactly: wrote a 16,778,240-byte MRC in two chunks (30% then a 9s pause then the rest) into an active watch; the batch add fired at ~5s with size=5033472, and /api/images STILL reported size 5033472 at 20s while the on-disk file was the full 16,778,240 — the entry is never corrected. computeStats also counts it live the moment the partial lands. One correction to the audit's emphasis: nx/ny/pixelSize are read from the first 1024 bytes, so they are correct whenever the pause occurs after the header is on disk (the common case), and there is no crash/data-loss — impact is a permanently wrong size + a stale/truncated cached thumbnail + slightly inflated rate. Real and worth fixing, but requires a mid-write stall and doesn't corrupt the acquisition, so […]
- **Suggested fix**: Do not rely on awaitWriteFinish size-stability for large sequential writes. Gate acceptance on the expected MRC size (1024 + nsymbt + nx*ny*nz*bpp from the header) and re-queue until the file is at least that large; add a chokidar 'change' handler that re-stats, re-reads the header, invalidates the thumbnail cache, and re-broadcasts. Raise stabilityThreshold and verify size==headerExpectedSize before emitting.

#### LIVE-3: Thumbnails and preview are nearest-neighbour decimated — aliasing destroys the signal on noisy micrographs

- **Location**: `server.js:139`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: Both the thumbnail (strideX/strideY, L139-151) and the preview (downscale step, L239-256) downsample by picking one single source pixel and discarding the rest — no averaging/binning. Cryo micrographs are dominated by high-frequency shot noise; point-sampling them aliases noise and throws away the low-frequency particle signal the user is trying to judge. The resulting tiles/previews are essentially noise fields, which makes the 'is this grid square any good' judgement the whole tool exists for unreliable.
- **Evidence**: Generated a 4096² float32 micrograph = low-freq Gaussian 'particles' + unit noise (truth signal saved). Correlation of the 128px strided thumbnail with the true low-freq signal = 0.128; correlation of a proper 32×32 block-mean bin = 0.974. For the 2048 preview: decimated corr 0.135 vs 2×2-binned corr 0.263. The 8-frame counting movie thumbnail collapsed to 4 unique byte values (reads only frame 0 of 8).
- **Verifier's note**: Code confirms pure point-sampling: thumbnail uses strideX/strideY with Math.floor index picks (server.js:139-159) and the preview uses an integer 'downscale' step picking one pixel (server.js:239-256) with no averaging/binning. For shot-noise-dominated micrographs this aliases noise and discards low-frequency signal, undermining the tool's triage purpose; the movie case reads only frame 0 (dataOffset+sy*nx*bpp never accounts for nz>1). The claimed correlation figures are consistent with point-sampling pure-noise data. Mitigating context (why not critical): JPEG previews/atlases that EPU writes are streamed natively and untouched by this, and this is a quick-look monitor with the full assessment done in the main app. Still a genuine scientific-accuracy defect for MRC and cheap to fix, so high.
- **Suggested fix**: Replace strided picks with block-mean (Fourier-crop or real-space binning) downsampling for both thumbnail and preview; for movies, sum/average frames (or document that only frame 0 is shown). This is the single biggest quality win and is cheap.

#### LIVE-5: Ruler and preview pixel-size are wrong by the decimation factor on ≥2k images

- **Location**: `server.js:239`; **severity** high (initially high); **verification**: confirmed; reproduced by the auditor: no
- **Problem**: readMrcDataAsync downsamples the image by integer factor downscale=ceil(max(nx,ny)/2048) (L239) but returns the ORIGINAL pixelSize = cellA/mx (L235) alongside the downsampled pixel grid. The frontend ruler multiplies distance measured in DOWNSAMPLED pixels by that original Å/px (PreviewModal.jsx L237-243, L332-336), so every measurement on a 4k micrograph is off by 2×, on a 6-8k image off by 3-4×. The on-screen 'Å/px' label and any distance a user reads off the ruler are therefore scientifically wrong for exactly the large images cryo users care about.
- **Evidence**: Code path: a 4096² image yields downscale=2, onx=2048; X-Pixel-Size header is cellA/mx = original 0.83 Å/px (verified served value 0.8299). PreviewModal computes distPx on the 2048 grid and does distPx*pixelSize, i.e. half the true length in Å. (2k images with downscale=1 are correct, which hides the bug in casual testing.)
- **Verifier's note**: readMrcDataAsync computes downscale=ceil(max(nx,ny)/2048) (server.js:239) yet returns the ORIGINAL pixelSize=cellA/mx (line 235). The client stores imgMeta.pixelSize = that original value and measures ruler distance in DOWNSAMPLED-grid pixels (PreviewModal.jsx:118-120,237-243,332-336), so distPx*pixelSize understates true length by the downscale factor. Reproduced the server half: a 4096^2 image returned X-NX 2048, X-Orig-NX 4096, X-Pixel-Size 0.8299 (unchanged) — so downscale=2 but Å/px not scaled, making ruler readings 2x short. This hits the COMMON case: standard K3 (5760x4092) and Falcon4 (4096^2) detectors all yield downscale>=2, so most real micrographs measure wrong, silently. Keep high.
- **Suggested fix**: Return an effective pixel size (pixelSize*downscale) in the header, or send downscale and multiply in the client; label the preview with the effective Å/px. Add a unit test asserting ruler Å across downscale factors.

#### LIVE-4: MRC parser ignores endianness and unsupported modes — big-endian and float16 files render as garbage

- **Location**: `server.js:109`; **severity** medium (initially high); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: All header fields are read with readInt32LE/readFloatLE unconditionally (L109-115, L128-133, L229-236); the MRC machine-stamp (bytes 212-215, MACHST) that signals big-endian is never consulted. Older data and some detectors write big-endian MRC → nx/ny/mode/pixelSize come out as nonsense and the server will try to allocate/read absurd row buffers. The bpp table {0:1,1:2,2:4,6:2} (L132,L236) omits mode 12 (float16, now common for compressed EER/movie output) and mode 101 (4-bit); mode 12 falls through to the default bpp=4 and is read as float32, producing wrong pixels and wrong row stride.
- **Evidence**: Served /api/image-data for a big-endian float32 MRC: headers came back X-Orig-NX 131072 (true 512), X-Mode 33554432, X-Pixel-Size 7.27e-49 — fully misparsed. A real float16 (mode 12) file was served with values up to 424484 vs a true mean≈5/sd≈1 (read as float32 over float16 bytes). Confirmed mode 12 absent from the bpp map at server.js:132.
- **Verifier's note**: Code reads all header/data fields with readInt32LE/readFloatLE unconditionally (server.js:109-115,128-133,229-236); MACHST at bytes 212-215 is never consulted, and the bpp map {0:1,1:2,2:4,6:2} (lines 132,236) omits modes 12 (float16) and 101 (uint4), falling through to bpp=4/float32. Reproduced: a big-endian 512x512 mode-2 MRC served X-Orig-NX 131072, X-Mode 33554432, X-Pixel-Size 7.27e-49 — fully misparsed. Both facts are true. Downgraded high->medium for a day-to-day cryo lab: big-endian MRC is essentially extinct in modern TFS/Gatan/EER pipelines, and the per-micrograph files a Live monitor watches are overwhelmingly mode 2 or 6; mode-12 float16 is real and growing but not the common monitored artifact. Real correctness bug, lower frequency.
- **Suggested fix**: Read MACHST and byte-swap when big-endian; extend the bpp/decoder table to modes 12 (float16) and 101 (uint4), and reject/flag unknown modes instead of defaulting to 4-byte float. Reuse the Python mrcfile logic as a reference since the unified app will have it available.

#### LIVE-6: Full float32 image (up to 16 MB) shipped per preview; no bound and no further use of the raw

- **Location**: `server.js:556`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: /api/image-data sends the entire downsampled float32 buffer to the browser (res.send(Buffer.from(data.pixels.buffer)), L556). A 2048×2048 preview = 16,777,216 bytes over the wire for every image opened or navigated to with the arrow keys; on a remote/VPN lab link this is seconds of latency per arrow press. The client re-derives p2/p98 and does an O(n) Gaussian in JS on top. Nothing caches the decoded raw between navigations, so re-opening re-downloads.
- **Evidence**: curl of /api/image-data for the 4k and 2k micrographs both returned size_download=16,777,216 bytes (16 MB); the f16 1k file returned 4,194,304 bytes. Measured ~0.2 s locally — multiply by network RTT for remote use.
- **Verifier's note**: server.js:556 sends Buffer.from(data.pixels.buffer) — the full downsampled float32 buffer. Reproduced: both a 2048^2 and a 4096^2 (downsampled to 2048) image returned exactly 16,777,216 bytes (=2048*2048*4). So every open/arrow-navigation ships 16MB of float32; the client re-derives percentiles and runs an O(n) JS gaussian. Accurate. Medium is right: painful over VPN/remote but fine on a LAN, and no correctness impact.
- **Suggested fix**: Either ship a smaller tile (e.g. 1024) for the first paint and fetch full-res on zoom, or send a pre-normalised 8/16-bit buffer instead of float32 (4×/2× smaller), or move the low-pass server-side. Add a small client LRU of decoded previews for arrow-key navigation.

#### LIVE-7: Single global watcher + globals shared across all browsers; any client's start/stop clobbers everyone

- **Location**: `server.js:44`; **severity** medium (initially high); **verification**: confirmed; reproduced by the auditor: no
- **Problem**: watchPattern, imageFiles and all rate state are process-wide singletons (L44-63). If two people open the app and one types a new path and clicks Watch, startWatching() wipes the globals and re-scans, and stopWatching() even deletes the entire .thumb_cache directory (L467-469) and broadcasts watch_stopped to every connected client — so one user silently resets another user's live session. There is no per-client or per-session isolation, and the client UI gives no indication that the watch is global.
- **Evidence**: server.js L334-348 reset module globals on every start_watch; L452-472 stopWatching clears globals and unlinks every file in THUMB_CACHE_DIR; broadcast(watch_stopped) at L471 hits all wss.clients. WS 'message' handler (L604-610) accepts start/stop from any socket with no ownership check.
- **Verifier's note**: Confirmed: watchPattern/imageFiles/rate state are module globals (server.js:44-63); startWatching wipes them all (334-348); stopWatching clears globals AND unlinks every file in THUMB_CACHE_DIR (467-469) and broadcasts watch_stopped to all wss.clients (471); the WS message handler accepts start_watch/stop_watch from any socket with no ownership/auth check (604-610). So any client's Stop (or a new Watch) resets every connected user and wipes the shared thumb cache. All code claims are accurate. Downgraded high->medium: the shipped deployment is a single systemd service (cryoviewer-live.service) typically pointed at one session path, so the multi-user clobber is a real footgun but not the primary usage, and the impact is disruption/recompute, not data loss. (Note: combined with LIVE-2's lack of auth, an unauthenticated network client can also trigger a full recursive scan of any path — see […]
- **Suggested fix**: Scope watch state per session (keyed by a session id or by pattern) or make the monitored path a server config rather than client-settable; at minimum confirm/notify when one client changes the global watch, and don't wipe the shared thumb cache on stop.

#### LIVE-8: Thumbnail disk cache is keyed on path+size only (not mtime) and is unbounded

- **Location**: `server.js:187`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: yes
- **Problem**: The cache key is md5(filePath + ':' + size) (L187) with no mtime. If a file at a given path is overwritten (re-acquisition, re-processing, or the partial→complete case in LIVE-1), the stale thumbnail is served forever with Cache-Control max-age=86400 (L519). The cache also has no eviction/size cap — over long sessions with 10k-50k files it grows without bound, and it is only ever cleared wholesale on stop_watch (which itself is the LIVE-7 problem).
- **Evidence**: L187 key omits mtime; LIVE-1 reproduction showed the partial-file thumbnail served identically after the file was completed. No LRU/size bound anywhere in server.js (grep found only rateHistory slice-1440).
- **Verifier's note**: Cache key is md5(filePath+':'+size) with no mtime (server.js:187), served with Cache-Control max-age=86400 (line 519), and the cache is only ever cleared wholesale in stopWatching — no LRU/size cap. Reproduced a same-size overwrite: thumbnail md5 was byte-identical before and after rewriting the file's pixels (22ec62da...==22ec62da...), proving the stale thumbnail is served. Unbounded growth over 10k-50k files is true by inspection. Accurate; medium is fair (wrong-but-not-catastrophic, and re-acquisition/overwrite at the same path with same size is the exact trigger, which also compounds LIVE-1).
- **Suggested fix**: Include mtime (and size) in the cache key, or store mtime in the .json meta and invalidate on mismatch; add an LRU/size cap and per-file invalidation instead of directory-wide wipe.

#### LIVE-9: usePolling with 3 s interval is correct for NFS but O(files) per cycle — costly at 10k-50k files

- **Location**: `server.js:371`; **severity** medium (initially medium); **verification**: confirmed; reproduced by the auditor: no
- **Problem**: usePolling:true is the RIGHT choice for NFS/SMB mounts where inotify doesn't fire, and the README's use case is exactly those mounts. But chokidar polling fs.watchFile()s every matched file every 3 s; at 10k-50k micrographs that is 10k-50k stat()s every 3 seconds against a network filesystem, which is heavy load on the storage and the server, and the initial recursive scan of a deep **/*.mrc tree can take a long time during which the UI shows only 'Scanning...'. There is no binaryInterval tuning and no cap on watched file count.
- **Evidence**: server.js L371-378 sets usePolling:true, interval:3000, and a recursive glob is the documented pattern (README). chokidar polling installs a per-file fs.watchFile (node_modules/chokidar/lib/nodefs-handler.js L323). Not load-tested at 50k here, but the mechanism is O(N) stats/cycle by construction.
- **Verifier's note**: server.js:371-378 sets usePolling:true, interval:3000; chokidar polling installs a per-file fs.watchFile, so the cost is O(matched files) stat()s every cycle — true by construction. Not load-tested at 50k here, so the magnitude is a projection, but the mechanism and the recursive-glob default (README) are real, and usePolling is indeed the correct choice for NFS/SMB. Medium is appropriate: a real scaling/IO concern on big network-mounted sessions, no correctness impact, and partly inherent to the NFS requirement.
- **Suggested fix**: Watch directories rather than every file where possible, raise the interval for large sessions, consider a lighter directory-mtime scan for the initial pass, and expose the interval as config. Show scan progress (count so far) rather than a static 'Scanning...'.

#### LIVE-10: Rate history only recorded while a browser is connected — graphs have holes for unattended collection

- **Location**: `server.js:476`; **severity** low (initially medium); **verification**: refuted; reproduced by the auditor: no
- **Problem**: updateRateHistory() (which appends the once-per-minute cumulative sample that RateGraph integrates) is called from the stats interval, but that interval's body only runs when wss.clients.size > 0 && watchActive (L477). Overnight/unattended collection with no browser open records no history, so the rate graph, cumulative curve and heatmap 'gaps' are artifacts of when someone had the tab open, not of the microscope. The instantaneous rate also derives from liveTimestamps that only accrue for files seen after the initial scan, so attaching mid-session shows rate 0 until the next image.
- **Evidence**: server.js L476-481: the setInterval guards broadcast+updateRateHistory behind wss.clients.size>0. updateRateHistory is also called in 'ready' and flushBatch, but the steady once-per-minute cadence that makes the graph continuous is the gated one. RateGraph.jsx derives rate from consecutive rateHistory samples (L52-57).
- **Verifier's note**: The central claim — that unattended (no-browser) collection records no rate history and that graph holes are artifacts of when the tab was open — is false. updateRateHistory is called from flushBatch (server.js:368), which fires on every new-image batch regardless of connected clients, and from 'ready' (446). Reproduced: I started a watch, CLOSED the WS client (0 browsers), then dropped files across minute boundaries with nothing connected, and /api/rate-history accumulated samples: liveCount 0 -> 6 -> 9 at successive minute marks. Only the per-2s filler cadence (which adds points for idle minutes with no new images) is gated behind wss.clients.size>0 at line 477 — and that is cosmetic (missing idle points just interpolate a flat segment; the dc/dt rate stays correct). The 'attach mid-session shows rate 0' sub-claim is also misleading: server liveTimestamps persist across browser reconne […]
- **Suggested fix**: Run the per-minute history sampler unconditionally while watchActive (independent of connected clients); persist rateHistory so a server restart doesn't blank the graph.

#### LIVE-11: No persistence: server restart (systemd Restart=on-failure) loses all state and forces a full re-scan

- **Location**: `server.js:46`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: yes
- **Problem**: All state is in RAM (imageFiles, liveTimestamps, rateHistory). The systemd unit sets Restart=on-failure, so any crash restarts a blank process that isn't even watching anything until a browser reconnects and re-sends start_watch — and the client only re-sends on user action, not automatically. Rate history and the acquisition timeline are lost on every restart. The unit also hardcodes User=louis and WorkingDirectory=/srv/CryoViewerLive.
- **Evidence**: No readFileSync/JSON.parse/sqlite in server.js (grep). cryoviewer-live.service: Restart=on-failure, User=louis, WorkingDirectory=/srv/CryoViewerLive/cryoviewer-live. startWatching is only invoked from the WS 'start_watch' message (L607).
- **Suggested fix**: Persist the active watch pattern and rateHistory to disk and restore on boot so the service resumes watching without a client; make the watch path a config/env value.

#### LIVE-12: Every (re)connect re-streams the entire image list; at 50k files reconnect storms are expensive

- **Location**: `server.js:587`; **severity** low (initially low); **verification**: unverified-low; reproduced by the auditor: no
- **Problem**: On WS connect the server streams the whole imageFiles list newest-first in 500-item chunks (L587-602) and the client clears and rebuilds its list on every onopen (App.jsx L48-51). With flaky lab Wi-Fi and 15 s backoff, a client that reconnects repeatedly re-downloads the full metadata list (path, name, mtime, size… for 50k files = several MB of JSON) each time, and each connected client holds the full array in React state.
- **Evidence**: server.js sendNextChunk loops from imageFiles.length down in CHUNK=500 (L588-601). App.jsx onopen sets images([]) and clears pathSet (L49-50) so the full resync is mandatory. Full file paths are included in every entry (also an info-disclosure of the filesystem layout).
- **Suggested fix**: Support incremental resync (client sends highest mtime/known count; server sends only newer), or paginate via the existing /api/images with a cursor; drop absolute paths from the list payload and reference files by id.

#### LIVE-MISSED-1: Unauthenticated OS command injection (RCE) via watch pattern -> df fallback in updateDiskInfo

- **Location**: `server.js:84`; **severity** critical
- **Problem**: updateDiskInfo() first tries fsp.statfs(watchVolumePath); on failure it runs exec(`df -B1 "${watchVolumePath}"`) (server.js:82-88) with child_process.exec, i.e. via /bin/sh, and watchVolumePath is derived directly from the client-supplied watch pattern with zero sanitization (watchVolumePath = path.dirname(basePath), basePath = the part of the WS start_watch pattern before the first glob char, lines 350-352). start_watch is accepted from any WebSocket with no auth (lines 604-610) on 0.0.0.0. A pattern containing a shell command substitution in a single path segment with no glob metachars (e.g. /nope/`cmd`/z.mrc or /nope/$(cmd)/z.mrc) is kept verbatim by path.dirname; statfs then throws ENOENT on that nonexistent path (I verified statfs throws ENOENT on a nonexistent path on this Node 22), which triggers the df fallback and executes the attacker string under /bin/sh. updateDiskInfo is invoked immediately from startWatching (line 354) and every 10s (line 485), so a single unauthenticated WS message yields remote code execution as the service user (louis). I confirmed reachability (statfs ENOENT -> fallback) and the exact unsanitized shell string the server would run (df -B1 "/nonexistent_abc/INJECTION_SITE"); I did NOT execute a live payload (a safety classifier blocked the live substitution, and executing arbitrary OS commands was unnecessary to confirm the defect). Fix: drop the df shell fallback or use execFile('df',['-B1',watchVolumePath]) with no shell, and validate/confine the pattern.

#### LIVE-MISSED-2: No MRC validation: truncated/non-MRC files return HTTP 200 with zero/garbage pixels instead of an error

- **Location**: `server.js:227`; **severity** low
- **Problem**: readMrcDataAsync (and generateThumbnailAsync) read the 1024-byte header without checking bytesRead (unlike readMrcHeaderAsync, which returns null when bytesRead<1024), and never verify the file is at least 1024 + nsymbt + nx*ny*nz*bpp bytes. A file smaller than 1024 bytes yields a zero-filled header -> nx=ny=0 -> an empty pixel buffer returned with HTTP 200 rather than a 4xx/5xx. I observed /api/image-data?path=/etc/hostname return HTTP 200 (empty body) instead of an error. This is benign on its own but (a) means any arbitrary non-jpg path returns 200 (aiding the LIVE-2 probe), and (b) is the same missing length-check that lets LIVE-1's partial file be served as a valid preview. Low severity, but a cheap guard (validate bytesRead==1024 and size>=expected) would close it.

