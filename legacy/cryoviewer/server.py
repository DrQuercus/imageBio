#!/usr/bin/env python3
"""
CryoViewer — cryo-EM image screening tool
Formats : .mrc .mrcs .st .rec .map .eer .tif .tiff .jpg .jpeg .png
Usage   : python3 server.py [file_or_dir] [--port 7473]
"""

import struct, json, base64, io, threading, webbrowser, argparse, traceback
import numpy as np
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

CRYO_EXTS  = {'.mrc', '.mrcs', '.st', '.rec', '.ali', '.map', '.eer'}
IMAGE_EXTS = {'.tif', '.tiff', '.jpg', '.jpeg', '.png'}
ALL_EXTS   = CRYO_EXTS | IMAGE_EXTS

# ──────────────────────────────────────────────────────────────────────────────
# MRC PARSER
# ──────────────────────────────────────────────────────────────────────────────

MRC_MODES = {
    0:  np.int8,
    1:  np.int16,
    2:  np.float32,
    6:  np.uint16,
    12: np.float16,
    16: np.uint8,
    101: None,   # 4-bit packed
}

def parse_mrc(f):
    f.seek(0)
    raw = f.read(1024)
    if len(raw) < 1024:
        raise ValueError("File too small for MRC")
    # Byte order of the whole header: machine stamp (bytes 212-215) first,
    # then fall back on a sanity check of the mode field.
    big = raw[212] == 0x11
    if raw[212] not in (0x11, 0x44):
        mode_le = struct.unpack_from('<i', raw, 12)[0]
        mode_be = struct.unpack_from('>i', raw, 12)[0]
        big = mode_le not in MRC_MODES and mode_be in MRC_MODES
    e = '>' if big else '<'
    nx, ny, nz   = struct.unpack_from(e + '3i', raw, 0)
    mode         = struct.unpack_from(e + 'i',  raw, 12)[0]
    mx, _,  _    = struct.unpack_from(e + '3i', raw, 28)
    xlen         = struct.unpack_from(e + 'f',  raw, 40)[0]
    dmin,dmax,dm = struct.unpack_from(e + '3f', raw, 76)
    nsymbt       = struct.unpack_from(e + 'i',  raw, 92)[0]
    rms          = struct.unpack_from(e + 'f',  raw, 216)[0]
    if nx <= 0 or ny <= 0:
        raise ValueError(f"Bad MRC dims {nx}x{ny}")
    apix   = (xlen / mx) if mx > 0 and xlen > 0 else 1.0
    endian = 'big' if big else 'little'
    offset = 1024 + max(0, nsymbt)
    dtype  = MRC_MODES.get(mode, np.float32)
    return dict(nx=nx, ny=ny, nz=nz, nframes=max(1, nz), mode=mode,
                apix=apix, endian=endian, offset=offset, dtype=dtype,
                dmin=float(dmin), dmax=float(dmax), dmean=float(dm), rms=float(rms))

def read_mrc_frame(f, h, idx):
    nx, ny = h['nx'], h['ny']
    idx = max(0, min(h['nframes'] - 1, idx))
    if h['mode'] == 101:
        fb = (nx * ny + 1) // 2
        f.seek(h['offset'] + idx * fb)
        raw = np.frombuffer(f.read(fb), dtype=np.uint8)
        pix = np.empty(raw.size * 2, dtype=np.uint8)
        pix[0::2] = raw & 0x0F
        pix[1::2] = raw >> 4
        return pix[:nx * ny].reshape(ny, nx).astype(np.float32)
    dtype    = h['dtype']
    itemsize = np.dtype(dtype).itemsize
    nbytes   = nx * ny * itemsize
    f.seek(h['offset'] + idx * nbytes)
    raw = f.read(nbytes)
    if len(raw) < nbytes:
        raise ValueError(f"EOF at frame {idx}")
    # Declare the on-disk byte order in the dtype itself (works on NumPy 1.x and 2.x);
    # astype(float32) then converts to native order.
    file_dtype = np.dtype(dtype).newbyteorder('>' if h['endian'] == 'big' else '<')
    arr = np.frombuffer(raw, dtype=file_dtype)
    return arr.reshape(ny, nx).astype(np.float32)

# ──────────────────────────────────────────────────────────────────────────────
# EER PARSER  (preview only — real decode needs RELION/MotionCor)
# ──────────────────────────────────────────────────────────────────────────────

def parse_eer(f):
    f.seek(0)
    hdr    = f.read(4)
    endian = '<' if hdr[:2] == b'II' else '>'
    ver    = struct.unpack_from(endian + 'H', hdr, 2)[0]
    bigtiff = (ver == 43)
    f.seek(8 if bigtiff else 4)
    ifd = struct.unpack(endian + ('Q' if bigtiff else 'I'), f.read(8 if bigtiff else 4))[0]
    nframes = 0; w = h = 4096
    while ifd and nframes < 20000:
        nframes += 1
        f.seek(ifd)
        try:
            n   = struct.unpack(endian + ('Q' if bigtiff else 'H'), f.read(8 if bigtiff else 2))[0]
            esz = 20 if bigtiff else 12
            for _ in range(min(n, 64)):
                e   = f.read(esz)
                if len(e) < esz: break
                tag = struct.unpack_from(endian + 'H', e, 0)[0]
                if tag == 256: w = struct.unpack_from(endian + 'I', e, 8)[0]
                if tag == 257: h = struct.unpack_from(endian + 'I', e, 8)[0]
            ifd = struct.unpack(endian + ('Q' if bigtiff else 'I'), f.read(8 if bigtiff else 4))[0]
        except Exception:
            break
    return dict(nx=w, ny=h, nframes=max(1, nframes), apix=0.5,
                is_eer=True, mode='eer', dmin=0.0, dmax=1.0, dmean=0.0, rms=0.0)

def read_eer_frame(h, idx):
    rng = np.random.default_rng(idx * 7919)
    return rng.poisson(0.01, (h['ny'], h['nx'])).astype(np.float32)

# ──────────────────────────────────────────────────────────────────────────────
# TIFF / IMAGE LOADER  — handles 8/16/32-bit, RGB, multi-page stacks
# ──────────────────────────────────────────────────────────────────────────────

def load_image_file(path):
    """
    Returns (list_of_float32_arrays, apix).
    list has one element per page/frame.
    """
    ext = Path(path).suffix.lower()

    # ── Try tifffile first for TIFF (16-bit, multi-page, cryo stacks) ──
    if ext in ('.tif', '.tiff'):
        try:
            import tifffile
            with tifffile.TiffFile(str(path)) as tf:
                # pixel size from metadata
                apix = 1.0
                try:
                    page = tf.pages[0]
                    xres = page.tags.get('XResolution')
                    unit = page.tags.get('ResolutionUnit')
                    if xres:
                        v = xres.value
                        res = v[0] / v[1] if isinstance(v, tuple) else float(v)
                        # unit: 1=undefined, 2=inch, 3=cm
                        # cryo-EM TIFFs from IMOD/MotionCor: resolution in pixels/µm or pixels/Å
                        # heuristic: if res > 1000, likely pixels/µm → apix in Å = 1e4/res
                        # if res is small (< 1), raw apix stored directly
                        u = unit.value if unit else 2
                        if res > 0:
                            if u == 1:
                                # no unit — likely Å/pixel stored as 1/apix
                                apix = 1.0 / res if res > 0 else 1.0
                            elif u == 3:
                                # cm: convert res (pixels/cm) → apix (Å/pixel)
                                apix = 1e8 / res  # 1 cm = 1e8 Å
                            else:
                                # inch by default: pixels/inch → not useful for cryo, keep 1.0
                                apix = 1.0
                except Exception:
                    pass

                data = tf.asarray()  # shape: (pages, y, x) or (y, x)
                if data.ndim == 2:
                    data = data[np.newaxis]
                elif data.ndim == 3 and data.shape[-1] in (3, 4):
                    # single RGB/RGBA page (y,x,3|4) → luminance (1,y,x)
                    data = data[..., :3].mean(axis=-1)[np.newaxis]
                elif data.ndim == 4 and data.shape[-1] in (3, 4):
                    # RGB/RGBA stack (n,y,x,3|4) → luminance (n,y,x)
                    data = data[..., :3].mean(axis=-1)

                frames = [data[i].astype(np.float32) for i in range(data.shape[0])]
                return frames, apix
        except ImportError:
            pass  # fall through to PIL
        except Exception as e:
            pass  # fall through to PIL

    # ── PIL fallback (JPEG, PNG, 8-bit TIFF) ──
    from PIL import Image
    img = Image.open(str(path))
    apix = 1.0

    # Try to extract apix from PIL TIFF tags
    try:
        tag_data = getattr(img, 'tag_v2', {})
        xres = tag_data.get(282)
        unit = tag_data.get(296, 2)
        if xres is not None:
            xres = xres[0] / xres[1] if isinstance(xres, (tuple, list)) else float(xres)
            if xres > 0 and unit == 3:
                apix = 1e8 / xres
    except Exception:
        pass

    frames = []
    try:
        while True:
            frame = _pil_to_float32(img)
            frames.append(frame)
            img.seek(img.tell() + 1)
    except EOFError:
        pass

    if not frames:
        frames = [_pil_to_float32(img)]
    return frames, apix

def _pil_to_float32(img):
    from PIL import Image
    mode = img.mode
    if mode in ('I', 'F'):
        return np.array(img, dtype=np.float32)
    if mode == 'I;16':
        arr = np.frombuffer(img.tobytes(), dtype=np.uint16).reshape(img.size[1], img.size[0])
        return arr.astype(np.float32)
    if mode in ('RGB', 'RGBA'):
        img = img.convert('L')
    return np.array(img, dtype=np.float32)

# ──────────────────────────────────────────────────────────────────────────────
# IMAGE PROCESSING
# ──────────────────────────────────────────────────────────────────────────────

def lowpass_filter(img, cutoff_ang, apix):
    """
    Gaussian lowpass in Fourier space — smooth, no ringing, matches CryoSPARC.
    cutoff_ang : resolution cutoff in Å (e.g. 20 = keep features coarser than 20 Å)
    apix       : Å per pixel
    σ chosen so that the Gaussian is 0.5 at freq_cut (FWHM-based).
    """
    if cutoff_ang <= 0 or apix <= 0:
        return img
    ny, nx = img.shape
    freq_cut = apix / cutoff_ang      # cutoff in cycles/pixel (0..0.5)
    freq_cut = min(freq_cut, 0.499)   # never exceed Nyquist

    # σ such that exp(-0.5*(freq_cut/σ)²) = 0.5  →  σ = freq_cut / sqrt(2*ln2)
    sigma = freq_cut / np.sqrt(2.0 * np.log(2.0))

    fy = np.fft.fftfreq(ny).astype(np.float32)
    fx = np.fft.rfftfreq(nx).astype(np.float32)
    FX, FY = np.meshgrid(fx, fy)
    fr2  = FX**2 + FY**2
    mask = np.exp(-0.5 * fr2 / (sigma**2))

    return np.fft.irfft2(np.fft.rfft2(img) * mask, s=(ny, nx)).astype(np.float32)


def auto_contrast(img, lo=2.0, hi=98.0):
    flat = img.ravel()
    step = max(1, len(flat) // 200_000)
    s    = flat[::step]
    vmin = float(np.percentile(s, lo))
    vmax = float(np.percentile(s, hi))
    if vmax <= vmin:
        vmax = vmin + 1.0
    return vmin, vmax


def apply_contrast(img, vmin, vmax, gamma=1.0, invert=False):
    if vmax <= vmin:
        vmax = vmin + 1.0
    arr = np.clip((img - vmin) / (vmax - vmin), 0.0, 1.0)
    if gamma != 1.0:
        arr = arr ** (1.0 / max(0.05, gamma))
    if invert:
        arr = 1.0 - arr
    return (arr * 255.0).astype(np.uint8)


def to_png_b64(arr_u8, max_dim=None):
    from PIL import Image
    img = Image.fromarray(arr_u8, 'L')
    if max_dim:
        w, h = img.size
        s = max(w, h)
        if s > max_dim:
            r  = max_dim / s
            img = img.resize((max(1, int(w*r)), max(1, int(h*r))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'PNG', compress_level=1)
    return base64.b64encode(buf.getvalue()).decode()


def make_histogram(img, bins=256):
    flat = img.ravel()
    step = max(1, len(flat) // 500_000)
    c, e = np.histogram(flat[::step], bins=bins)
    return c.tolist(), e.tolist()

# ──────────────────────────────────────────────────────────────────────────────
# FILE CACHE
# ──────────────────────────────────────────────────────────────────────────────

class CryoFile:
    MAX_CACHE = 24

    def __init__(self, path):
        self.path = Path(path)
        self.ext  = self.path.suffix.lower()
        self._f   = None
        self.hdr  = None
        self._frames  = []    # list of arrays for image files
        self._cache   = {}    # idx → float32 frame
        self._corder  = []
        self._load()

    def _load(self):
        if self.ext in IMAGE_EXTS:
            frames, apix = load_image_file(self.path)
            self._frames = frames
            ny, nx = frames[0].shape
            sample = frames[0]
            self.hdr = dict(nx=nx, ny=ny, nframes=len(frames), apix=apix,
                            mode='img', is_eer=False,
                            dmin=float(sample.min()), dmax=float(sample.max()),
                            dmean=float(sample.mean()), rms=float(sample.std()))
        elif self.ext == '.eer':
            self._f  = open(self.path, 'rb')
            self.hdr = parse_eer(self._f)
        else:
            self._f  = open(self.path, 'rb')
            self.hdr = parse_mrc(self._f)

    @property
    def nframes(self): return max(1, self.hdr['nframes'])
    @property
    def nx(self):      return self.hdr['nx']
    @property
    def ny(self):      return self.hdr['ny']
    @property
    def apix(self):    return max(0.001, float(self.hdr.get('apix', 1.0)))

    def get_frame(self, idx):
        idx = max(0, min(self.nframes - 1, idx))
        if idx in self._cache:
            return self._cache[idx]
        if self._frames:
            frame = self._frames[idx]
        elif self.hdr.get('is_eer'):
            frame = read_eer_frame(self.hdr, idx)
        else:
            frame = read_mrc_frame(self._f, self.hdr, idx)
        if len(self._cache) >= self.MAX_CACHE:
            del self._cache[self._corder.pop(0)]
        self._cache[idx] = frame
        self._corder.append(idx)
        return frame

    def get_average(self, start, end):
        start = max(0, start)
        end   = min(self.nframes, end)
        if start >= end:
            return self.get_frame(start)
        acc = self.get_frame(start).astype(np.float64)
        for i in range(start + 1, end):
            acc += self.get_frame(i)
        return (acc / (end - start)).astype(np.float32)

    def info(self):
        h = self.hdr
        return dict(filename=self.path.name, path=str(self.path),
                    nx=h['nx'], ny=h['ny'], nframes=self.nframes,
                    apix=round(self.apix, 5),
                    mode=str(h.get('mode', '?')),
                    dmin=round(float(h.get('dmin', 0)), 3),
                    dmax=round(float(h.get('dmax', 0)), 3),
                    dmean=round(float(h.get('dmean', 0)), 3),
                    is_eer=bool(h.get('is_eer', False)))

    def close(self):
        if self._f:
            self._f.close()
            self._f = None

# ──────────────────────────────────────────────────────────────────────────────
# APP STATE
# ──────────────────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.files   = {}
        self.order   = []
        self.current = None
        self.cwd     = str(Path.home())

    def open(self, path):
        p = str(Path(path).resolve())
        if p not in self.files:
            self.files[p] = CryoFile(p)
        if p not in self.order:
            self.order.append(p)
        self.current = p
        return self.files[p]

    def scan(self, dirpath):
        d = Path(dirpath).expanduser().resolve()
        found = sorted(
            [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in ALL_EXTS],
            key=lambda x: x.name)
        self.cwd = str(d)
        for f in found:
            p = str(f)
            if p not in self.order:
                self.order.append(p)
        return found

    def list(self):
        return [dict(path=p, name=Path(p).name,
                     loaded=p in self.files, active=p == self.current)
                for p in self.order]

    def browse(self, dirpath):
        d = Path(dirpath).expanduser().resolve()
        if not d.is_dir():
            raise ValueError(f"Not a directory: {d}")
        entries = []
        if d.parent != d:
            entries.append(dict(name='..', path=str(d.parent), kind='dir'))
        for item in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            if item.is_dir():
                entries.append(dict(name=item.name, path=str(item), kind='dir'))
            elif item.suffix.lower() in ALL_EXTS:
                entries.append(dict(name=item.name, path=str(item), kind='file',
                                    ext=item.suffix.lower()[1:],
                                    size=_hsize(item.stat().st_size)))
        return str(d), entries

ST = State()

def _hsize(n):
    for u in ['B','KB','MB','GB']:
        if n < 1024: return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

# ──────────────────────────────────────────────────────────────────────────────
# DEMO
# ──────────────────────────────────────────────────────────────────────────────

def make_demo_frame(lp=20.0, vmin=None, vmax=None, gamma=1.0, invert=False):
    nx = ny = 512; apix = 1.06
    rng = np.random.default_rng(42)
    img = rng.normal(1000.0, 200.0, (ny, nx)).astype(np.float32)
    for _ in range(50):
        cx, cy = rng.integers(20, nx-20), rng.integers(20, ny-20)
        r      = rng.integers(8, 26)
        Y, X   = np.ogrid[:ny, :nx]
        d      = np.sqrt((X-cx)**2 + (Y-cy)**2)
        img   += rng.uniform(-350, -120) * np.exp(-d**2 / (2*(r*0.55)**2))
    # CTF rings
    fy, fx = np.fft.fftfreq(ny), np.fft.rfftfreq(nx)
    FX, FY = np.meshgrid(fx, fy)
    fft    = np.fft.rfft2(img)
    fft   *= 1 + 0.15 * np.sin(np.pi * 1.5e4 * 0.0197 * (FX**2 + FY**2))
    img    = np.fft.irfft2(fft, s=(ny, nx)).astype(np.float32)
    if lp > 0:
        img = lowpass_filter(img, lp, apix)
    dmin, dmax = float(img.min()), float(img.max())
    if vmin is None or vmax is None:
        vmin, vmax = auto_contrast(img, 2, 98)
    u8   = apply_contrast(img, vmin, vmax, gamma, invert)
    cnt, edges = make_histogram(img)
    return dict(
        image     = to_png_b64(u8),
        stats     = dict(min=round(dmin,2), max=round(dmax,2),
                         mean=round(float(img.mean()),2), std=round(float(img.std()),2),
                         vmin=round(float(vmin),2), vmax=round(float(vmax),2),
                         dmin=round(dmin,2), dmax=round(dmax,2)),
        histogram = dict(counts=cnt, edges=edges),
        info      = dict(filename='demo_micrograph.mrc', nx=nx, ny=ny, nframes=40,
                         apix=apix, mode='2', is_eer=False,
                         dmin=round(dmin,2), dmax=round(dmax,2), dmean=round(float(img.mean()),2))
    )

# ──────────────────────────────────────────────────────────────────────────────
# HTTP SERVER
# ──────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a): pass

    def _send(self, body, ct='application/json', status=200):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try: self._send({'error': str(e), 'trace': traceback.format_exc()}, status=500)
            except: pass

    def _route(self):
        parsed = urlparse(self.path)
        p, qs  = parsed.path, parse_qs(parsed.query)
        q      = lambda k, d=None: unquote(qs[k][0]) if k in qs else d
        qf     = lambda k, d=0.0: float(q(k) or d)
        qi     = lambda k, d=0:   int(q(k) or d)

        if p == '/':
            self._send(UI.encode(), 'text/html; charset=utf-8'); return

        # ── browse ──────────────────────────────────────────────────────────
        if p == '/api/browse':
            try:
                cwd, entries = ST.browse(q('dir', ST.cwd))
                ST.cwd = cwd
                self._send(dict(cwd=cwd, entries=entries))
            except Exception as e:
                self._send(dict(error=str(e)), status=400)
            return

        # ── load ─────────────────────────────────────────────────────────────
        if p == '/api/load':
            path = q('path', '')
            if not path:
                self._send(dict(error='no path'), status=400); return
            try:
                cf = ST.open(path)
                self._send(dict(ok=True, info=cf.info(), list=ST.list()))
            except Exception as e:
                self._send(dict(error=str(e), trace=traceback.format_exc()), status=500)
            return

        # ── frame ────────────────────────────────────────────────────────────
        if p == '/api/frame':
            path = q('path', ST.current or '')
            if not path or path not in ST.files:
                self._send(dict(error='File not loaded')); return
            cf  = ST.files[path]
            try:
                idx    = qi('idx')
                gamma  = max(0.05, qf('gamma', 1.0))
                inv    = q('invert', '0') == '1'
                auto   = q('auto',   '1') == '1'
                lp     = qf('lp', 0.0)
                avg    = q('avg', '0') == '1'
                avg_s  = qi('avg_s', 0)
                avg_e  = qi('avg_e', cf.nframes)

                img = cf.get_average(avg_s, avg_e) if avg else cf.get_frame(idx)

                if lp > 0:
                    img = lowpass_filter(img, lp, cf.apix)

                dmin = float(img.min());  dmax = float(img.max())

                if auto:
                    lo  = qf('lo', 2.0);  hi = qf('hi', 98.0)
                    vmin, vmax = auto_contrast(img, lo, hi)
                else:
                    vmin = qf('vmin', dmin)
                    vmax = qf('vmax', dmax)

                u8  = apply_contrast(img, vmin, vmax, gamma, inv)
                cnt, edges = make_histogram(img)

                self._send(dict(
                    image     = to_png_b64(u8),
                    stats     = dict(min=round(dmin,4), max=round(dmax,4),
                                     mean=round(float(img.mean()),4), std=round(float(img.std()),4),
                                     vmin=round(vmin,4), vmax=round(vmax,4),
                                     dmin=round(dmin,4), dmax=round(dmax,4)),
                    histogram = dict(counts=cnt, edges=edges)
                ))
            except Exception as e:
                self._send(dict(error=str(e), trace=traceback.format_exc()), status=500)
            return

        # ── demo ─────────────────────────────────────────────────────────────
        if p == '/api/demo':
            lp     = qf('lp', 20.0)
            vmin_r = q('vmin'); vmax_r = q('vmax')
            vmin   = float(vmin_r) if vmin_r else None
            vmax   = float(vmax_r) if vmax_r else None
            gamma  = max(0.05, qf('gamma', 1.0))
            inv    = q('invert', '0') == '1'
            self._send(make_demo_frame(lp, vmin, vmax, gamma, inv))
            return

        # ── list ─────────────────────────────────────────────────────────────
        if p == '/api/list':
            self._send(dict(list=ST.list())); return

        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

# ──────────────────────────────────────────────────────────────────────────────
# UI  (HTML + CSS + JS)
# ──────────────────────────────────────────────────────────────────────────────

UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>CryoViewer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
/* ── RESET & TOKENS ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#04080b; --bg1:#080e14; --bg2:#0c1520; --bg3:#111e2c;
  --line:#1a2d42; --line2:#22374f;
  --acc:#00e5ff; --acc-dim:rgba(0,229,255,.12); --acc-glow:rgba(0,229,255,.25);
  --amber:#ffb340; --green:#39ff9a; --purple:#b48eff; --red:#ff4d6d;
  --txt:#b8d4e8; --dim:#4d6e87; --dim2:#253d52;
  --mono:'JetBrains Mono',monospace; --sans:'Syne',sans-serif;
}
html,body{height:100vh;overflow:hidden;background:var(--bg0);color:var(--txt);font-family:var(--mono);font-size:12px}

/* ── LAYOUT ── */
#app{display:grid;grid-template-rows:44px 1fr;height:100vh}
#main{display:grid;grid-template-columns:240px 1fr 272px;overflow:hidden;min-height:0}

/* ── TOPBAR ── */
#top{background:var(--bg1);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:6px;padding:0 16px}
.logo{font-family:var(--sans);font-size:17px;font-weight:800;letter-spacing:-1px;color:var(--acc);margin-right:8px;display:flex;align-items:center;gap:8px}
.logo-g{width:26px;height:26px;background:var(--acc);border-radius:6px;display:flex;align-items:center;justify-content:center;color:#000;font-size:14px;font-weight:900}
.t-sep{width:1px;height:20px;background:var(--line);margin:0 2px}
.t-sp{flex:1}
.tbtn{padding:5px 12px;border:1px solid var(--line2);border-radius:4px;background:transparent;color:var(--dim);cursor:pointer;font-family:var(--mono);font-size:10px;letter-spacing:.5px;transition:all .15s;white-space:nowrap}
.tbtn:hover{border-color:var(--acc);color:var(--acc)}
.tbtn.on{background:var(--acc-dim);border-color:var(--acc);color:var(--acc)}
.tbtn.amber{color:var(--amber);border-color:rgba(255,179,64,.3)}
.tbtn.amber.on{background:rgba(255,179,64,.1);border-color:var(--amber)}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
#status-txt{font-size:10px;color:var(--dim)}

/* ── FILE BROWSER (left) ── */
#fbr{background:var(--bg1);border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
.ph{padding:10px 13px 8px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--line);flex-shrink:0;display:flex;align-items:center;gap:6px}
.ph .bdg{background:var(--bg3);color:var(--dim);border-radius:10px;padding:1px 7px;font-size:9px}
#path-bar{display:flex;align-items:center;padding:6px 10px;border-bottom:1px solid var(--line);flex-shrink:0;background:var(--bg2);min-height:34px;gap:2px}
#path-inp{flex:1;background:transparent;border:none;outline:none;color:var(--txt);font-family:var(--mono);font-size:10px;min-width:0}
#path-inp::placeholder{color:var(--dim2)}
.path-go{background:none;border:none;color:var(--dim);cursor:pointer;font-size:15px;padding:2px 4px;transition:color .15s}
.path-go:hover{color:var(--acc)}
#browser-list{flex:1;overflow-y:auto}
#browser-list::-webkit-scrollbar{width:3px}
#browser-list::-webkit-scrollbar-thumb{background:var(--line2)}
.b-item{display:flex;align-items:center;gap:8px;padding:6px 13px;cursor:pointer;border-left:2px solid transparent;transition:background .1s;min-width:0}
.b-item:hover{background:rgba(0,229,255,.04);border-left-color:var(--line2)}
.b-item.act{background:rgba(0,229,255,.08);border-left-color:var(--acc)}
.b-icon{font-size:13px;flex-shrink:0}
.b-name{font-size:11px;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.b-name.dir{color:var(--amber)}
.b-meta{font-size:9px;color:var(--dim);flex-shrink:0;margin-left:auto}
.b-ext-mrc,.b-ext-mrcs,.b-ext-st,.b-ext-rec{color:var(--acc)}
.b-ext-eer{color:var(--purple)}
.b-ext-tif,.b-ext-tiff{color:var(--green)}
.b-ext-jpg,.b-ext-jpeg,.b-ext-png{color:var(--dim)}
.fl-div{height:1px;background:var(--line);margin:4px 0}
.fl-hdr{padding:5px 13px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--dim2)}
.f-item{display:flex;align-items:center;gap:7px;padding:5px 13px 5px 9px;cursor:pointer;border-left:2px solid transparent;transition:background .1s;min-width:0}
.f-item:hover{background:rgba(0,229,255,.04)}
.f-item.act{border-left-color:var(--acc);background:rgba(0,229,255,.06)}
.f-dot{width:6px;height:6px;border-radius:50%;background:var(--line2);flex-shrink:0}
.f-item.act .f-dot{background:var(--acc);box-shadow:0 0 5px var(--acc)}
.f-name{font-size:10px;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── CENTER ── */
#center{display:flex;flex-direction:column;background:#020507;overflow:hidden;min-height:0}

/* viewer toolbar */
#vtbar{background:var(--bg1);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:6px;padding:6px 10px;flex-shrink:0;flex-wrap:wrap}
.fnav{display:flex;align-items:center;gap:2px;background:var(--bg2);border:1px solid var(--line2);border-radius:5px;padding:2px 7px}
.nbtn{background:none;border:none;color:var(--dim);cursor:pointer;font-size:13px;padding:2px 5px;border-radius:3px;transition:all .12s}
.nbtn:hover{color:var(--acc)}
.fctr{font-size:11px;color:var(--txt);min-width:68px;text-align:center}
.pbtn{width:28px;height:28px;border-radius:50%;border:1px solid rgba(0,229,255,.3);background:var(--acc-dim);color:var(--acc);cursor:pointer;font-size:12px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.pbtn:hover{background:rgba(0,229,255,.2)}
.pbtn.on{background:rgba(255,77,109,.15);border-color:var(--red);color:var(--red)}
.fps-w{display:flex;align-items:center;gap:4px}
.lbl9{font-size:9px;color:var(--dim);letter-spacing:.8px}
input.sl-sm{-webkit-appearance:none;width:54px;height:3px;background:var(--line2);border-radius:2px;outline:none;cursor:pointer}
input.sl-sm::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:11px;border-radius:50%;background:var(--acc)}
.tool-sep{width:1px;height:20px;background:var(--line);flex-shrink:0}
.dim-lbl{font-size:9px;color:var(--dim2);margin-left:auto;white-space:nowrap}

/* canvas area */
#cwrap{flex:1;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;background:#020507}

/*
  #stage wraps both canvases and receives the zoom+pan CSS transform.
  The overlay sits exactly on top of viewer via position:absolute.
  Mouse events are captured on #stage so coords are always in image space.
*/
#stage{position:relative;display:inline-block;transform-origin:center center;flex-shrink:0}
#viewer{display:block;image-rendering:auto}
#ruler-cv{position:absolute;top:0;left:0;pointer-events:none;display:block}

/* tools info overlays */
#pix-info{position:absolute;top:10px;right:12px;background:rgba(4,8,11,.88);border:1px solid var(--line);border-radius:5px;padding:5px 10px;font-size:10px;color:var(--dim);pointer-events:none;backdrop-filter:blur(4px);z-index:10}
#pix-info span{color:var(--acc)}
#ruler-readout{position:absolute;bottom:46px;left:50%;transform:translateX(-50%);background:rgba(4,8,11,.92);border:1px solid rgba(255,179,64,.4);border-radius:6px;padding:6px 16px;font-size:11px;color:var(--amber);pointer-events:none;white-space:nowrap;display:none;backdrop-filter:blur(6px);z-index:10}

/* tool buttons overlay */
.tool-btns{position:absolute;bottom:12px;right:12px;display:flex;gap:3px;z-index:10}
.zbtn{width:28px;height:28px;background:rgba(4,8,11,.88);border:1px solid var(--line);border-radius:4px;color:var(--txt);cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .15s;backdrop-filter:blur(4px)}
.zbtn:hover{border-color:var(--acc);color:var(--acc)}
.zbtn.on{border-color:var(--amber);color:var(--amber);background:rgba(255,179,64,.1)}

/* empty state */
#empty-state{position:absolute;display:flex;flex-direction:column;align-items:center;gap:14px;color:var(--dim);text-align:center;z-index:5}
.e-glyph{font-size:56px;opacity:.2}
.e-title{font-family:var(--sans);font-size:16px;color:var(--dim);font-weight:600}
.e-sub{font-size:11px;color:var(--dim2);max-width:220px;line-height:1.6}
.demo-btn{margin-top:4px;padding:9px 24px;background:var(--acc-dim);border:1px solid rgba(0,229,255,.3);border-radius:5px;color:var(--acc);cursor:pointer;font-family:var(--mono);font-size:11px;transition:all .2s}
.demo-btn:hover{background:rgba(0,229,255,.2)}

/* spinner */
#spinner{position:absolute;inset:0;background:rgba(4,8,11,.7);display:flex;align-items:center;justify-content:center;z-index:20;backdrop-filter:blur(3px)}
.spin{width:36px;height:36px;border:2px solid var(--line2);border-top-color:var(--acc);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* frame slider */
#fsl-wrap{background:var(--bg1);border-top:1px solid var(--line);padding:7px 14px 8px;flex-shrink:0}
.fsl-top{display:flex;justify-content:space-between;font-size:9px;color:var(--dim2);margin-bottom:5px;text-transform:uppercase;letter-spacing:.8px}
input#fslider{-webkit-appearance:none;width:100%;height:4px;border-radius:2px;background:var(--line2);outline:none;cursor:pointer}
input#fslider::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--acc);border:2px solid var(--bg0);box-shadow:0 0 8px var(--acc-glow)}

/* ── RIGHT PANEL ── */
#rp{background:var(--bg1);border-left:1px solid var(--line);display:flex;flex-direction:column;overflow-y:auto}
#rp::-webkit-scrollbar{width:3px}
#rp::-webkit-scrollbar-thumb{background:var(--line2)}
.sec{border-bottom:1px solid var(--line)}
.sec-t{padding:10px 14px 7px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--dim);display:flex;align-items:center;gap:6px}
.sec-b{padding:0 14px 14px}
.row{margin-bottom:10px}
.lrow{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;font-size:10px;color:var(--dim)}
.lrow .val{color:var(--txt);font-size:11px}
input.ctrl{-webkit-appearance:none;width:100%;height:3px;background:var(--line2);border-radius:2px;outline:none;cursor:pointer}
input.ctrl::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;border-radius:50%;background:var(--acc);box-shadow:0 0 5px var(--acc-glow)}
input.ctrl.lp::-webkit-slider-thumb{background:var(--purple);box-shadow:0 0 5px rgba(180,142,255,.4)}
.inp-row{display:flex;gap:5px;margin-bottom:8px;align-items:center}
.num-inp{flex:1;background:var(--bg3);border:1px solid var(--line2);border-radius:4px;color:var(--txt);font-family:var(--mono);font-size:10px;padding:4px 7px;outline:none;text-align:right}
.num-inp:focus{border-color:var(--acc)}
.inp-lbl{font-size:9px;color:var(--dim);min-width:28px}
.btn-row{display:flex;gap:5px}
.sbtn{flex:1;padding:5px 4px;background:var(--bg3);border:1px solid var(--line2);border-radius:4px;color:var(--dim);cursor:pointer;font-family:var(--mono);font-size:10px;transition:all .15s;text-align:center}
.sbtn:hover{border-color:var(--acc);color:var(--acc)}
.sbtn.on{background:var(--acc-dim);border-color:var(--acc);color:var(--acc)}
.lp-badge{background:rgba(180,142,255,.12);border:1px solid rgba(180,142,255,.3);color:var(--purple);border-radius:3px;padding:1px 7px;font-size:10px}
.lp-off{color:var(--dim2);font-size:10px}

/* histogram canvas */
#hist-cv{width:100%;height:72px;display:block;cursor:crosshair}
.hist-axis{display:flex;justify-content:space-between;font-size:9px;color:var(--dim2);margin-top:3px}

/* stats grid */
.sgrid{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.sbox{background:var(--bg3);border:1px solid var(--line);border-radius:5px;padding:6px 9px}
.sk{font-size:8px;letter-spacing:1.5px;color:var(--dim);text-transform:uppercase;margin-bottom:2px}
.sv{font-size:13px;color:var(--acc);font-weight:500}

/* file info */
.irow{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--line);font-size:10px}
.irow:last-child{border-bottom:none}
.ik{color:var(--dim)} .iv{color:var(--txt)}

/* ruler panel */
.apix-row{display:flex;gap:5px;align-items:center;margin-top:8px;margin-bottom:8px}
.apix-inp{flex:1;background:var(--bg3);border:1px solid rgba(255,179,64,.3);border-radius:4px;color:var(--amber);font-family:var(--mono);font-size:11px;padding:5px 8px;outline:none}
.apix-inp:focus{border-color:var(--amber)}
.apix-lbl{font-size:10px;color:var(--amber);min-width:58px}
.r-result{margin-top:6px;background:var(--bg3);border:1px solid rgba(255,179,64,.2);border-radius:5px;padding:10px 12px}
.r-val{font-size:18px;font-family:var(--sans);font-weight:700;color:var(--amber)}
.r-sub{font-size:10px;color:var(--dim);margin-top:3px}

/* shortcuts */
.sk-row{display:flex;justify-content:space-between;padding:3px 0;font-size:9px}
.sk-key{background:var(--bg3);border:1px solid var(--line2);border-radius:3px;padding:1px 6px;color:var(--dim)}
.sk-desc{color:var(--dim2)}

/* toast */
#toast{position:fixed;bottom:16px;right:16px;z-index:999;background:var(--bg3);border:1px solid var(--line2);border-radius:7px;padding:9px 15px;font-size:11px;color:var(--txt);transition:opacity .25s,transform .25s;max-width:320px}
#toast.ok{border-color:rgba(57,255,154,.4);color:var(--green)}
#toast.err{border-color:rgba(255,77,109,.4);color:var(--red)}
#toast.off{opacity:0;transform:translateY(6px);pointer-events:none}
.hidden{display:none!important}
</style>
</head>
<body>
<div id="app">

<!-- TOPBAR -->
<div id="top">
  <div class="logo"><div class="logo-g">C</div>CryoViewer</div>
  <button class="tbtn" onclick="loadDemo()">Demo</button>
  <div class="t-sep"></div>
  <button class="tbtn" id="btn-inv" onclick="toggleInvert()">Invert</button>
  <button class="tbtn" id="btn-avg" onclick="toggleAvg()">Avg</button>
  <button class="tbtn" id="btn-lp" onclick="toggleLP()">LP: <span id="lp-top-val">off</span></button>
  <button class="tbtn amber" id="btn-ruler" onclick="setTool('ruler')">&#8596; Ruler</button>
  <button class="tbtn amber" id="btn-pan"   onclick="setTool('pan')">&#9995; Pan</button>
  <button class="tbtn" onclick="autoContrast()">Auto-C</button>
  <div class="t-sp"></div>
  <div class="sdot" id="sdot"></div>&nbsp;
  <span id="status-txt">Ready</span>
</div>

<!-- MAIN -->
<div id="main">

<!-- LEFT: file browser -->
<div id="fbr">
  <div class="ph">Files <span class="bdg" id="fbr-cnt">0</span></div>
  <div id="path-bar">
    <input id="path-inp" type="text" placeholder="Path…"
           onkeydown="if(event.key==='Enter')browseTo(this.value)">
    <button class="path-go" onclick="browseTo(document.getElementById('path-inp').value)">&#8594;</button>
    <button class="path-go" onclick="browseTo('~')">&#8962;</button>
  </div>
  <div id="browser-list"></div>
</div>

<!-- CENTER: viewer -->
<div id="center">

  <!-- viewer toolbar -->
  <div id="vtbar">
    <div class="fnav">
      <button class="nbtn" onclick="prevFrame()">&#9664;</button>
      <span class="fctr" id="fctr">— / —</span>
      <button class="nbtn" onclick="nextFrame()">&#9654;</button>
    </div>
    <button class="pbtn" id="pbtn" onclick="togglePlay()">&#9654;</button>
    <div class="fps-w">
      <span class="lbl9">FPS</span>
      <input type="range" class="sl-sm" id="fps-sl" min="1" max="30" value="8"
             oninput="S.fps=+this.value;document.getElementById('fps-v').textContent=S.fps">
      <span id="fps-v" style="font-size:10px;color:var(--txt);min-width:18px">8</span>
    </div>
    <div class="tool-sep"></div>
    <button class="tbtn amber" id="btn-avgr" onclick="toggleAvgRange()">Avg: OFF</button>
    <div id="avg-wrap" class="hidden" style="display:flex;align-items:center;gap:4px">
      <span class="lbl9">fr</span>
      <input class="num-inp" id="avg-s" type="number" value="0" style="width:46px" oninput="debounceFetch()">
      <span class="lbl9">to</span>
      <input class="num-inp" id="avg-e" type="number" value="1" style="width:46px" oninput="debounceFetch()">
    </div>
    <span class="dim-lbl" id="dim-lbl">—</span>
  </div>

  <!-- canvas stage -->
  <div id="cwrap">
    <div id="empty-state">
      <div class="e-glyph">&#128300;</div>
      <div class="e-title">No file loaded</div>
      <div class="e-sub">Navigate the browser on the left and click a file, or drop one here</div>
      <button class="demo-btn" onclick="loadDemo()">Load synthetic demo</button>
    </div>

    <!-- #stage receives zoom+pan transform; overlay canvas sits on top of viewer -->
    <div id="stage">
      <canvas id="viewer" class="hidden"></canvas>
      <canvas id="ruler-cv" class="hidden"></canvas>
    </div>

    <div id="pix-info">x: <span id="px">—</span> y: <span id="py">—</span> &nbsp; val: <span id="pv">—</span></div>
    <div id="ruler-readout"></div>

    <div class="tool-btns">
      <div class="zbtn" onclick="applyZoom(1.25)" title="Zoom in">+</div>
      <div class="zbtn" onclick="applyZoom(1/1.25)" title="Zoom out">−</div>
      <div class="zbtn" onclick="resetView()" title="Reset">&#8635;</div>
    </div>

    <div id="spinner" class="hidden"><div class="spin"></div></div>
  </div>

  <!-- frame slider -->
  <div id="fsl-wrap">
    <div class="fsl-top"><span>Frame</span><span id="fsl-lbl">—</span></div>
    <input type="range" id="fslider" min="0" max="0" value="0" oninput="onFSlider()">
  </div>
</div>

<!-- RIGHT: controls -->
<div id="rp">

  <!-- CONTRAST -->
  <div class="sec">
    <div class="sec-t">Contrast</div>
    <div class="sec-b">
      <div class="row">
        <div class="lrow">Black point <span class="val" id="vmin-lbl">—</span></div>
        <input type="range" class="ctrl" id="vmin-sl" min="0" max="10000" value="200" oninput="onCSlider('min')">
      </div>
      <div class="row">
        <div class="lrow">White point <span class="val" id="vmax-lbl">—</span></div>
        <input type="range" class="ctrl" id="vmax-sl" min="0" max="10000" value="9800" oninput="onCSlider('max')">
      </div>
      <div class="inp-row">
        <span class="inp-lbl">MIN</span>
        <input class="num-inp" id="vmin-inp" type="number" step="any" oninput="onCInput('min')">
        <span class="inp-lbl">MAX</span>
        <input class="num-inp" id="vmax-inp" type="number" step="any" oninput="onCInput('max')">
      </div>
      <div class="row">
        <div class="lrow">Gamma <span class="val" id="gamma-lbl">1.0</span></div>
        <input type="range" class="ctrl" id="gamma-sl" min="2" max="40" value="10" oninput="onGamma()">
      </div>
      <div class="btn-row">
        <button class="sbtn" onclick="autoContrast()">Auto</button>
        <button class="sbtn" onclick="resetContrast()">Reset</button>
        <button class="sbtn" id="inv-btn" onclick="toggleInvert()">Invert</button>
      </div>
    </div>
  </div>

  <!-- LOWPASS -->
  <div class="sec">
    <div class="sec-t">Lowpass Filter <span id="lp-badge" class="lp-off">OFF</span></div>
    <div class="sec-b">
      <div class="row">
        <div class="lrow">Cutoff <span class="val" id="lp-lbl" style="color:var(--purple)">— Å</span></div>
        <input type="range" class="ctrl lp" id="lp-sl" min="0" max="100" step="1" value="0" oninput="onLPSlider()">
      </div>
      <div class="inp-row">
        <span class="inp-lbl" style="color:var(--purple)">Å</span>
        <input class="num-inp" id="lp-inp" type="number" min="0" max="500" step="1" value="0"
               oninput="onLPInput()" style="border-color:rgba(180,142,255,.3)">
      </div>
      <div style="font-size:9px;color:var(--dim2);margin-bottom:9px;line-height:1.6">
        0 = off &nbsp;·&nbsp; Gaussian rolloff (no ringing) &nbsp;·&nbsp; default: <span style="color:var(--purple)">20 Å</span>
      </div>
      <div class="btn-row">
        <button class="sbtn" onclick="setLP(0)">Off</button>
        <button class="sbtn" onclick="setLP(20)">20 Å</button>
        <button class="sbtn" onclick="setLP(10)">10 Å</button>
        <button class="sbtn" onclick="setLP(5)">5 Å</button>
      </div>
    </div>
  </div>

  <!-- RULER -->
  <div class="sec">
    <div class="sec-t">&#8596; Ruler</div>
    <div class="sec-b" style="padding-top:8px">
      <div style="font-size:10px;color:var(--dim);line-height:1.7">
        Press <kbd style="font-size:9px;background:var(--bg3);border:1px solid var(--line2);border-radius:3px;padding:0 5px">R</kbd>
        or the Ruler button. Click &amp; drag on image.
      </div>
      <div class="apix-row">
        <span class="apix-lbl">Å / pixel</span>
        <input class="apix-inp" id="apix-inp" type="number" min="0.001" step="0.01" value="1.0"
               oninput="S.apix=parseFloat(this.value)||1; updateRuler()">
      </div>
      <div id="r-result" class="r-result hidden">
        <div class="r-val" id="r-ang">—</div>
        <div class="r-sub" id="r-sub">—</div>
      </div>
    </div>
  </div>

  <!-- HISTOGRAM -->
  <div class="sec">
    <div class="sec-t">Histogram</div>
    <div class="sec-b" style="padding-top:8px">
      <canvas id="hist-cv"></canvas>
      <div class="hist-axis"><span id="h-emin">—</span><span id="h-emax">—</span></div>
    </div>
  </div>

  <!-- STATS -->
  <div class="sec">
    <div class="sec-t">Statistics</div>
    <div class="sec-b">
      <div class="sgrid">
        <div class="sbox"><div class="sk">Mean</div><div class="sv" id="s-mean">—</div></div>
        <div class="sbox"><div class="sk">Std</div><div class="sv" id="s-std">—</div></div>
        <div class="sbox"><div class="sk">Min</div><div class="sv" id="s-min">—</div></div>
        <div class="sbox"><div class="sk">Max</div><div class="sv" id="s-max">—</div></div>
      </div>
    </div>
  </div>

  <!-- FILE INFO -->
  <div class="sec">
    <div class="sec-t">File Info</div>
    <div class="sec-b" id="info-panel" style="padding-top:6px">
      <div style="color:var(--dim2);font-size:10px">No file</div>
    </div>
  </div>

  <!-- SHORTCUTS -->
  <div class="sec">
    <div class="sec-t">Shortcuts</div>
    <div class="sec-b" style="padding-top:8px">
      <div class="sk-row"><span class="sk-key">← →</span><span class="sk-desc">Prev / Next frame</span></div>
      <div class="sk-row"><span class="sk-key">Space</span><span class="sk-desc">Play / Pause</span></div>
      <div class="sk-row"><span class="sk-key">A</span><span class="sk-desc">Auto contrast</span></div>
      <div class="sk-row"><span class="sk-key">I</span><span class="sk-desc">Invert</span></div>
      <div class="sk-row"><span class="sk-key">L</span><span class="sk-desc">Toggle lowpass</span></div>
      <div class="sk-row"><span class="sk-key">R</span><span class="sk-desc">Ruler tool</span></div>
      <div class="sk-row"><span class="sk-key">P</span><span class="sk-desc">Pan tool</span></div>
      <div class="sk-row"><span class="sk-key">Esc</span><span class="sk-desc">Default tool</span></div>
      <div class="sk-row"><span class="sk-key">S</span><span class="sk-desc">Toggle avg frames</span></div>
      <div class="sk-row"><span class="sk-key">+ − 0</span><span class="sk-desc">Zoom in / out / reset</span></div>
    </div>
  </div>

</div><!-- /rp -->
</div><!-- /main -->
</div><!-- /app -->
<div id="toast" class="off">—</div>

<script>
'use strict';

// ── STATE ─────────────────────────────────────────────────────────────────────
const S = {
  // file
  file: null, nframes: 1, nx: 0, ny: 0,
  frame: 0, avgMode: false, avgS: 0, avgE: 1,
  isDemo: false,

  // display
  vmin: 0, vmax: 1,
  dmin: 0, dmax: 1,   // full data range (for slider mapping)
  gamma: 1.0, invert: false, autoC: true,
  lp: 0, lp_on: false,
  stats: null, histData: null,
  apix: 1.0,

  // viewport — zoom+pan applied to #stage via CSS transform
  zoom: 1.0, panX: 0, panY: 0,

  // tools: 'default' | 'pan' | 'ruler'
  tool: 'default',

  // pan drag
  panDragging: false, panSX: 0, panSY: 0,  // screen start
  panSPX: 0, panSPY: 0,                    // pan offset start

  // ruler
  rulerP1: null, rulerP2: null, rulerDrag: false,

  // misc
  fps: 8, playing: false, playTimer: null,
  debTimer: null,
};

// ── DOM ───────────────────────────────────────────────────────────────────────
const $viewer   = document.getElementById('viewer');
const $vctx     = $viewer.getContext('2d');
const $rcv      = document.getElementById('ruler-cv');
const $rctx     = $rcv.getContext('2d');
const $histCv   = document.getElementById('hist-cv');
const $histCtx  = $histCv.getContext('2d');
const $stage    = document.getElementById('stage');
const $cwrap    = document.getElementById('cwrap');

// ── TOAST ─────────────────────────────────────────────────────────────────────
let _tt;
function toast(msg, type='', ms=3000) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = type||'';
  clearTimeout(_tt); _tt = setTimeout(() => el.classList.add('off'), ms);
}

// ── LOADING ───────────────────────────────────────────────────────────────────
function busy(v) {
  document.getElementById('spinner').classList.toggle('hidden', !v);
  document.getElementById('status-txt').textContent = v ? 'Loading…' : 'Ready';
}

// ── API ───────────────────────────────────────────────────────────────────────
async function api(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (d.error) throw new Error(d.error);
  return d;
}

// ── VIEWPORT TRANSFORM ────────────────────────────────────────────────────────
function applyTransform() {
  $stage.style.transform = `translate(${S.panX}px,${S.panY}px) scale(${S.zoom})`;
}

function applyZoom(f) {
  S.zoom = f === 0 ? 1.0 : Math.max(0.02, Math.min(20, S.zoom * f));
  applyTransform();
}

function resetView() {
  S.zoom = 1; S.panX = 0; S.panY = 0;
  applyTransform();
}

// Mouse wheel zoom — keep image point under cursor stationary
$cwrap.addEventListener('wheel', e => {
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.12 : 1/1.12;
  const newZoom = Math.max(0.02, Math.min(20, S.zoom * factor));

  // cursor position relative to cwrap centre
  const wr = $cwrap.getBoundingClientRect();
  const cx = e.clientX - wr.left - wr.width/2;
  const cy = e.clientY - wr.top  - wr.height/2;

  // adjust pan so the point under cursor stays fixed
  S.panX = cx - (cx - S.panX) * newZoom / S.zoom;
  S.panY = cy - (cy - S.panY) * newZoom / S.zoom;
  S.zoom = newZoom;
  applyTransform();
}, { passive: false });

// ── TOOL SYSTEM ───────────────────────────────────────────────────────────────
function setTool(name) {
  // toggle off if already active
  if (S.tool === name) name = 'default';
  S.tool = name;

  // update button states
  document.getElementById('btn-ruler').classList.toggle('on', name === 'ruler');
  document.getElementById('btn-pan').classList.toggle('on',   name === 'pan');

  if (name === 'ruler') {
    $stage.style.cursor = 'crosshair';
    $rcv.style.pointerEvents = 'all';
  } else if (name === 'pan') {
    $stage.style.cursor = 'grab';
    $rcv.style.pointerEvents = 'none';
  } else {
    $stage.style.cursor = '';
    $rcv.style.pointerEvents = 'none';
  }
}

// ── COORDINATES ───────────────────────────────────────────────────────────────
// Convert clientX/Y → canvas pixel coords (accounts for zoom+pan via getBCR)
function stageToCanvas(clientX, clientY) {
  const sr = $stage.getBoundingClientRect();
  return {
    x: (clientX - sr.left) / S.zoom,
    y: (clientY - sr.top)  / S.zoom,
  };
}

// ── PAN EVENTS ────────────────────────────────────────────────────────────────
$stage.addEventListener('mousedown', e => {
  if (S.tool !== 'pan') return;
  e.preventDefault();
  S.panDragging = true;
  S.panSX = e.clientX; S.panSY = e.clientY;
  S.panSPX = S.panX;   S.panSPY = S.panY;
  $stage.style.cursor = 'grabbing';
});

window.addEventListener('mousemove', e => {
  if (S.tool === 'pan' && S.panDragging) {
    S.panX = S.panSPX + (e.clientX - S.panSX);
    S.panY = S.panSPY + (e.clientY - S.panSY);
    applyTransform();
  }
  if (S.tool === 'ruler' && S.rulerDrag && S.rulerP1) {
    S.rulerP2 = stageToCanvas(e.clientX, e.clientY);
    redrawRuler();
  }
});

window.addEventListener('mouseup', e => {
  if (S.tool === 'pan' && S.panDragging) {
    S.panDragging = false;
    $stage.style.cursor = 'grab';
  }
  if (S.tool === 'ruler' && S.rulerDrag) {
    S.rulerDrag = false;
    S.rulerP2 = stageToCanvas(e.clientX, e.clientY);
    redrawRuler();
    updateRuler();
  }
});

// ── RULER EVENTS ─────────────────────────────────────────────────────────────
// Ruler canvas gets pointer events only when ruler tool is active
$rcv.addEventListener('mousedown', e => {
  if (S.tool !== 'ruler') return;
  e.preventDefault();
  S.rulerP1 = stageToCanvas(e.clientX, e.clientY);
  S.rulerP2 = { ...S.rulerP1 };
  S.rulerDrag = true;
});

function redrawRuler() {
  $rctx.clearRect(0, 0, $rcv.width, $rcv.height);
  if (!S.rulerP1 || !S.rulerP2) return;
  const { x: x1, y: y1 } = S.rulerP1;
  const { x: x2, y: y2 } = S.rulerP2;
  const dx = x2-x1, dy = y2-y1;
  const len = Math.sqrt(dx*dx + dy*dy);
  if (len < 2) return;

  // All drawing is in canvas pixel space (ruler-cv is same size as viewer)
  $rctx.save();
  $rctx.strokeStyle = 'rgba(255,179,64,.9)';
  $rctx.lineWidth   = 1.5;
  $rctx.setLineDash([6,3]);
  $rctx.beginPath(); $rctx.moveTo(x1,y1); $rctx.lineTo(x2,y2); $rctx.stroke();
  $rctx.setLineDash([]);

  // End caps
  const ex = -dy/len, ey = dx/len; // perpendicular
  const cs = 7;
  for (const [px,py] of [[x1,y1],[x2,y2]]) {
    $rctx.beginPath();
    $rctx.moveTo(px+ex*cs, py+ey*cs);
    $rctx.lineTo(px-ex*cs, py-ey*cs);
    $rctx.stroke();
  }

  // Label
  const mx = (x1+x2)/2, my = (y1+y2)/2;
  const label = `${(len*S.apix).toFixed(1)} Å  (${len.toFixed(1)} px)`;
  $rctx.font = '13px "JetBrains Mono",monospace';
  $rctx.textAlign = 'center'; $rctx.textBaseline = 'bottom';
  const tw = $rctx.measureText(label).width;
  $rctx.fillStyle = 'rgba(4,8,11,.85)';
  $rctx.fillRect(mx-tw/2-5, my-20, tw+10, 18);
  $rctx.fillStyle = 'rgba(255,179,64,.95)';
  $rctx.fillText(label, mx, my-4);
  $rctx.restore();
}

function updateRuler() {
  if (!S.rulerP1 || !S.rulerP2) return;
  const dx = S.rulerP2.x-S.rulerP1.x, dy = S.rulerP2.y-S.rulerP1.y;
  const len = Math.sqrt(dx*dx+dy*dy);
  const ang = (len*S.apix).toFixed(1);
  const nm  = (len*S.apix/10).toFixed(2);

  const ro = document.getElementById('ruler-readout');
  ro.textContent = `${ang} Å  ·  ${nm} nm  ·  ${len.toFixed(1)} px`;
  ro.style.display = 'block';

  const rr = document.getElementById('r-result');
  rr.classList.remove('hidden');
  document.getElementById('r-ang').textContent = `${ang} Å`;
  document.getElementById('r-sub').textContent = `${len.toFixed(1)} px  ·  ${nm} nm`;
}

// ── PIXEL INFO ────────────────────────────────────────────────────────────────
$stage.addEventListener('mousemove', e => {
  const c = stageToCanvas(e.clientX, e.clientY);
  const x = Math.floor(c.x), y = Math.floor(c.y);
  document.getElementById('px').textContent = x;
  document.getElementById('py').textContent = y;
  try {
    const px = $vctx.getImageData(x, y, 1, 1).data[0];
    document.getElementById('pv').textContent = px;
  } catch(e_) {}
});

// ── FILE BROWSER ──────────────────────────────────────────────────────────────
async function browseTo(dir) {
  if (!dir) return;
  try {
    const d = await api('/api/browse?dir=' + encodeURIComponent(dir));
    document.getElementById('path-inp').value = d.cwd;
    renderBrowser(d.entries);
  } catch(e) { toast(e.message, 'err'); }
}

function esc(s) { return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }

function renderBrowser(entries) {
  const cnt  = entries.filter(e => e.kind === 'file').length;
  document.getElementById('fbr-cnt').textContent = cnt;
  let h = '';
  for (const e of entries) {
    if (e.kind === 'dir') {
      const icon = e.name === '..' ? '&#8593;' : '&#128193;';
      h += `<div class="b-item" onclick="browseTo('${esc(e.path)}')">`
         + `<span class="b-icon">${icon}</span><span class="b-name dir">${e.name}</span></div>`;
    }
  }
  const files = entries.filter(e => e.kind === 'file');
  if (files.length) {
    h += '<div class="fl-div"></div>';
    for (const e of files) {
      const act = e.path === S.file ? ' act' : '';
      h += `<div class="b-item${act}" onclick="loadFile('${esc(e.path)}')">`
         + `<span class="b-icon">&#128196;</span>`
         + `<span class="b-name b-ext-${e.ext}">${e.name}</span>`
         + `<span class="b-meta">${e.size||''}</span></div>`;
    }
  }
  document.getElementById('browser-list').innerHTML = h;
}

// ── LOAD FILE ─────────────────────────────────────────────────────────────────
async function loadFile(path) {
  busy(true); S.isDemo = false;
  try {
    const d = await api('/api/load?path=' + encodeURIComponent(path));
    S.file = path; S.frame = 0; S.autoC = true;
    applyInfo(d.info);
    if (d.list) renderLoaded(d.list);
    await fetchFrame();
    showViewer();
    toast(`Loaded: ${d.info.filename}`, 'ok');
    browseTo(document.getElementById('path-inp').value);
  } catch(e) { toast(e.message, 'err'); console.error(e); }
  busy(false);
}

function applyInfo(info) {
  S.nframes = Math.max(1, info.nframes);
  S.nx = info.nx; S.ny = info.ny;
  if (info.apix > 0) {
    S.apix = info.apix;
    document.getElementById('apix-inp').value = info.apix;
  }
  S.avgS = 0; S.avgE = S.nframes;
  document.getElementById('fslider').max = S.nframes - 1;
  document.getElementById('fslider').value = 0;
  document.getElementById('avg-e').value = S.nframes;
  const lbl = `1 / ${S.nframes}`;
  document.getElementById('fctr').textContent = lbl;
  document.getElementById('fsl-lbl').textContent = lbl;
  document.getElementById('dim-lbl').textContent =
    `${info.nx}×${info.ny}  ·  ${S.nframes}fr  ·  ${info.apix}Å/px`;
  renderInfo(info);
}

function showViewer() {
  document.getElementById('empty-state').classList.add('hidden');
  $viewer.classList.remove('hidden');
  $rcv.classList.remove('hidden');
}

function renderLoaded(list) {
  let el = document.getElementById('loaded-sec');
  if (!el) {
    el = document.createElement('div');
    el.id = 'loaded-sec';
    document.getElementById('browser-list').appendChild(el);
  }
  const loaded = list.filter(f => f.loaded);
  if (!loaded.length) { el.innerHTML = ''; return; }
  let h = '<div class="fl-div"></div><div class="fl-hdr">Opened</div>';
  for (const f of loaded) {
    h += `<div class="f-item${f.active?' act':''}" onclick="setActive('${esc(f.path)}')">`
       + `<div class="f-dot"></div><span class="f-name">${f.name}</span></div>`;
  }
  el.innerHTML = h;
}

async function setActive(path) {
  if (path === S.file) return;
  busy(true); S.file = path; S.frame = 0; S.autoC = true;
  try {
    const d = await api('/api/load?path=' + encodeURIComponent(path));
    applyInfo(d.info);
    await fetchFrame();
    showViewer();
    if (d.list) renderLoaded(d.list);
  } catch(e) { toast(e.message, 'err'); }
  busy(false);
}

// ── FETCH FRAME ───────────────────────────────────────────────────────────────
async function fetchFrame() {
  if (!S.file) return;

  if (S.isDemo) {
    // For demo: pass current contrast params so server re-renders correctly
    let url = `/api/demo?lp=${S.lp_on&&S.lp>0?S.lp:0}&gamma=${S.gamma.toFixed(3)}&invert=${S.invert?1:0}`;
    if (!S.autoC) url += `&vmin=${S.vmin}&vmax=${S.vmax}`;
    try {
      const d = await api(url);
      renderImage(d.image);
      receiveStats(d.stats, d.histogram, !S.autoC);
    } catch(e) { toast(e.message,'err'); }
    return;
  }

  let url = `/api/frame?path=${encodeURIComponent(S.file)}`
          + `&idx=${S.frame}&gamma=${S.gamma.toFixed(3)}&invert=${S.invert?1:0}`
          + `&auto=${S.autoC?1:0}&lp=${S.lp_on&&S.lp>0?S.lp:0}`;
  if (!S.autoC) url += `&vmin=${S.vmin}&vmax=${S.vmax}`;
  if (S.avgMode) url += `&avg=1&avg_s=${S.avgS}&avg_e=${S.avgE}`;

  try {
    const d = await api(url);
    renderImage(d.image);
    receiveStats(d.stats, d.histogram, !S.autoC);
  } catch(e) { toast(e.message,'err'); }
}

function debounceFetch(ms=80) {
  clearTimeout(S.debTimer);
  S.debTimer = setTimeout(fetchFrame, ms);
}

// ── RECEIVE STATS (called after every frame fetch) ───────────────────────────
function receiveStats(stats, hist, keepWindow) {
  // stats has: min, max, mean, std, vmin, vmax, dmin, dmax
  S.stats = stats;
  S.dmin  = stats.dmin ?? stats.min;
  S.dmax  = stats.dmax ?? stats.max;

  if (!keepWindow) {
    S.vmin = stats.vmin;
    S.vmax = stats.vmax;
  }

  // Update stat boxes
  document.getElementById('s-mean').textContent = fmt(stats.mean);
  document.getElementById('s-std').textContent  = fmt(stats.std);
  document.getElementById('s-min').textContent  = fmt(stats.min);
  document.getElementById('s-max').textContent  = fmt(stats.max);

  syncContrastUI();

  if (hist) {
    S.histData = hist;
    drawHist(hist.counts, hist.edges);
  }
}

function fmt(v) {
  if (Math.abs(v) >= 10000) return v.toFixed(0);
  if (Math.abs(v) >= 100)   return v.toFixed(1);
  return v.toFixed(3);
}

// ── IMAGE RENDERING ───────────────────────────────────────────────────────────
function renderImage(b64) {
  const img = new Image();
  img.onload = () => {
    $viewer.width  = img.width;
    $viewer.height = img.height;
    $vctx.drawImage(img, 0, 0);
    // ruler canvas same size, sits exactly on top
    $rcv.width  = img.width;
    $rcv.height = img.height;
    $rcv.style.width  = img.width  + 'px';
    $rcv.style.height = img.height + 'px';
    redrawRuler();
  };
  img.src = 'data:image/png;base64,' + b64;
}

// ── DEMO ──────────────────────────────────────────────────────────────────────
async function loadDemo() {
  busy(true);
  S.isDemo = true; S.file = '__demo__'; S.frame = 0; S.autoC = true;
  try {
    const lp = S.lp_on && S.lp > 0 ? S.lp : 0;
    const d  = await api(`/api/demo?lp=${lp}`);
    applyInfo(d.info);
    renderImage(d.image);
    receiveStats(d.stats, d.histogram, false);
    showViewer();
    toast('Demo micrograph loaded', 'ok');
  } catch(e) { toast(e.message,'err'); }
  busy(false);
}

// ── HISTOGRAM ─────────────────────────────────────────────────────────────────
function drawHist(counts, edges) {
  const W = $histCv.offsetWidth || 244, H = 72;
  $histCv.width = W; $histCv.height = H;
  const eMin = edges[0], eMax = edges[edges.length-1];
  const eRange = eMax - eMin || 1;
  const maxC   = Math.max(...counts, 1);

  // background
  $histCtx.fillStyle = '#030609';
  $histCtx.fillRect(0, 0, W, H);

  // window shading
  const wx1 = Math.max(0, ((S.vmin - eMin) / eRange) * W);
  const wx2 = Math.min(W, ((S.vmax - eMin) / eRange) * W);
  $histCtx.fillStyle = 'rgba(0,229,255,.07)';
  $histCtx.fillRect(wx1, 0, Math.max(0, wx2-wx1), H);

  // bars — log scale for better cryo-EM visual
  const bw = W / counts.length;
  for (let i = 0; i < counts.length; i++) {
    const bh = counts[i] > 0 ? (Math.log(counts[i]+1) / Math.log(maxC+1)) * (H-2) : 0;
    const ev = edges[i];
    $histCtx.fillStyle = (ev >= S.vmin && ev <= S.vmax) ? 'rgba(0,229,255,.75)' : 'rgba(26,45,66,.9)';
    $histCtx.fillRect(i*bw, H-bh, Math.max(1, bw-.3), bh);
  }

  // window markers
  $histCtx.strokeStyle = 'rgba(0,229,255,.7)'; $histCtx.lineWidth = 1;
  $histCtx.setLineDash([3,3]);
  for (const wx of [wx1, wx2]) {
    const cx = Math.max(0, Math.min(W, wx));
    $histCtx.beginPath(); $histCtx.moveTo(cx,0); $histCtx.lineTo(cx,H); $histCtx.stroke();
  }
  $histCtx.setLineDash([]);

  document.getElementById('h-emin').textContent = fmt(eMin);
  document.getElementById('h-emax').textContent = fmt(eMax);
}

// ── CONTRAST ──────────────────────────────────────────────────────────────────
// Sliders map 0-10000 → dmin-dmax
function _s2v(sl) {
  if (S.dmax === S.dmin) return S.dmin;
  return S.dmin + (sl / 10000) * (S.dmax - S.dmin);
}
function _v2s(v) {
  if (S.dmax === S.dmin) return 5000;
  return Math.round(((v - S.dmin) / (S.dmax - S.dmin)) * 10000);
}

function onCSlider(which) {
  S.autoC = false;
  const v = _s2v(+document.getElementById(which === 'min' ? 'vmin-sl' : 'vmax-sl').value);
  if (which === 'min') {
    S.vmin = v;
    document.getElementById('vmin-lbl').textContent = fmt(v);
    document.getElementById('vmin-inp').value = v.toFixed(4);
  } else {
    S.vmax = v;
    document.getElementById('vmax-lbl').textContent = fmt(v);
    document.getElementById('vmax-inp').value = v.toFixed(4);
  }
  if (S.histData) drawHist(S.histData.counts, S.histData.edges);
  debounceFetch(80);
}

function onCInput(which) {
  const id  = which === 'min' ? 'vmin-inp' : 'vmax-inp';
  const val = parseFloat(document.getElementById(id).value);
  if (isNaN(val)) return;
  S.autoC = false;
  if (which === 'min') {
    S.vmin = val;
    document.getElementById('vmin-lbl').textContent = fmt(val);
    document.getElementById('vmin-sl').value = _v2s(val);
  } else {
    S.vmax = val;
    document.getElementById('vmax-lbl').textContent = fmt(val);
    document.getElementById('vmax-sl').value = _v2s(val);
  }
  if (S.histData) drawHist(S.histData.counts, S.histData.edges);
  debounceFetch(200);
}

function onGamma() {
  S.gamma = document.getElementById('gamma-sl').value / 10;
  document.getElementById('gamma-lbl').textContent = S.gamma.toFixed(1);
  debounceFetch(80);
}

function syncContrastUI() {
  document.getElementById('vmin-lbl').textContent = fmt(S.vmin);
  document.getElementById('vmax-lbl').textContent = fmt(S.vmax);
  document.getElementById('vmin-inp').value = S.vmin.toFixed(4);
  document.getElementById('vmax-inp').value = S.vmax.toFixed(4);
  document.getElementById('vmin-sl').value  = _v2s(S.vmin);
  document.getElementById('vmax-sl').value  = _v2s(S.vmax);
  if (S.histData) drawHist(S.histData.counts, S.histData.edges);
}

function autoContrast()  { S.autoC = true; fetchFrame(); }
function toggleInvert()  {
  S.invert = !S.invert;
  ['btn-inv','inv-btn'].forEach(id => document.getElementById(id).classList.toggle('on', S.invert));
  fetchFrame();
}
function resetContrast() {
  S.autoC = false; S.vmin = S.dmin; S.vmax = S.dmax; S.gamma = 1.0;
  document.getElementById('gamma-sl').value = 10;
  document.getElementById('gamma-lbl').textContent = '1.0';
  syncContrastUI(); fetchFrame();
}

// ── LOWPASS ───────────────────────────────────────────────────────────────────
function onLPSlider() {
  S.lp = +document.getElementById('lp-sl').value;
  S.lp_on = S.lp > 0;
  document.getElementById('lp-inp').value = S.lp;
  syncLP(); debounceFetch(150);
}
function onLPInput() {
  const v = parseFloat(document.getElementById('lp-inp').value);
  if (isNaN(v) || v < 0) return;
  S.lp = v; S.lp_on = v > 0;
  document.getElementById('lp-sl').value = Math.min(100, v);
  syncLP(); debounceFetch(200);
}
function setLP(v) {
  S.lp = v; S.lp_on = v > 0;
  document.getElementById('lp-sl').value = Math.min(100, v);
  document.getElementById('lp-inp').value = v;
  syncLP(); fetchFrame();
}
function toggleLP() {
  S.lp_on = !S.lp_on;
  if (S.lp_on && S.lp === 0) {
    S.lp = 20;
    document.getElementById('lp-sl').value = 20;
    document.getElementById('lp-inp').value = 20;
  }
  syncLP(); fetchFrame();
}
function syncLP() {
  const on = S.lp_on && S.lp > 0;
  document.getElementById('lp-badge').className = on ? 'lp-badge' : 'lp-off';
  document.getElementById('lp-badge').textContent = on ? `${S.lp} Å` : 'OFF';
  document.getElementById('lp-lbl').textContent = on ? `${S.lp} Å` : '— Å';
  document.getElementById('lp-top-val').textContent = on ? `${S.lp}Å` : 'off';
  document.getElementById('btn-lp').classList.toggle('on', on);
}

// ── FRAME NAV ─────────────────────────────────────────────────────────────────
function setFrame(n) {
  S.frame = Math.max(0, Math.min(S.nframes-1, n));
  document.getElementById('fslider').value = S.frame;
  updateFSL(); fetchFrame();
}
function updateFSL() {
  const lbl = `${S.frame+1} / ${S.nframes}`;
  document.getElementById('fctr').textContent = lbl;
  document.getElementById('fsl-lbl').textContent = lbl;
  const p = S.nframes > 1 ? (S.frame/(S.nframes-1))*100 : 0;
  document.getElementById('fslider').style.background =
    `linear-gradient(to right,rgba(0,229,255,.5) ${p}%,var(--line2) ${p}%)`;
}
function onFSlider() {
  S.frame = +document.getElementById('fslider').value;
  updateFSL(); debounceFetch(60);
}
function prevFrame() { setFrame(S.frame-1); }
function nextFrame() { setFrame(S.frame+1); }

// ── PLAY ──────────────────────────────────────────────────────────────────────
function togglePlay() {
  S.playing = !S.playing;
  const btn = document.getElementById('pbtn');
  btn.innerHTML = S.playing ? '&#9646;&#9646;' : '&#9654;';
  btn.classList.toggle('on', S.playing);
  if (S.playing) _tick(); else clearTimeout(S.playTimer);
}
function _tick() {
  if (!S.playing) return;
  S.playTimer = setTimeout(() => { setFrame(S.frame >= S.nframes-1 ? 0 : S.frame+1); _tick(); }, 1000/S.fps);
}

// ── AVG ───────────────────────────────────────────────────────────────────────
function toggleAvg() {
  S.avgMode = !S.avgMode;
  document.getElementById('btn-avg').classList.toggle('on', S.avgMode);
  fetchFrame();
}
function toggleAvgRange() {
  S.avgMode = !S.avgMode;
  const wrap = document.getElementById('avg-wrap');
  const btn  = document.getElementById('btn-avgr');
  wrap.classList.toggle('hidden', !S.avgMode);
  btn.textContent = `Avg: ${S.avgMode?'ON':'OFF'}`;
  btn.classList.toggle('on', S.avgMode);
  if (S.avgMode) { document.getElementById('avg-e').value = S.nframes; S.avgE = S.nframes; }
  fetchFrame();
}

// ── FILE INFO ─────────────────────────────────────────────────────────────────
function renderInfo(info) {
  const rows = [
    ['File',    info.filename],
    ['Size',    `${info.nx} × ${info.ny}`],
    ['Frames',  info.nframes],
    ['Format',  info.is_eer ? 'EER' : `MRC mode ${info.mode}`],
    ['Å/pixel', info.apix],
  ];
  document.getElementById('info-panel').innerHTML =
    rows.map(([k,v]) => `<div class="irow"><span class="ik">${k}</span><span class="iv">${v}</span></div>`).join('');
}

// ── DRAG & DROP (whole page) ──────────────────────────────────────────────────
document.body.addEventListener('dragover',  e => e.preventDefault());
document.body.addEventListener('dragenter', e => e.preventDefault());
document.body.addEventListener('drop', async e => {
  e.preventDefault();
  const files = [...(e.dataTransfer.files||[])];
  for (const f of files) {
    const ext = '.' + f.name.split('.').pop().toLowerCase();
    if (ALL_EXTS.has(ext)) {
      // Upload file binary → server
      await uploadFile(f); break;
    }
  }
});

const ALL_EXTS = new Set(['.mrc','.mrcs','.st','.rec','.ali','.map','.eer','.tif','.tiff','.jpg','.jpeg','.png']);

async function uploadFile(file) {
  busy(true);
  try {
    const buf  = await file.arrayBuffer();
    const resp = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) },
      body: buf,
    });
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    S.file = d.key; S.frame = 0; S.autoC = true; S.isDemo = false;
    applyInfo(d.info);
    if (d.list) renderLoaded(d.list);
    await fetchFrame();
    showViewer();
    toast(`Loaded: ${file.name}`, 'ok');
  } catch(e) { toast(e.message, 'err'); }
  busy(false);
}

// ── KEYBOARD ──────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const tag = document.activeElement.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  switch (e.key) {
    case 'ArrowLeft': case 'ArrowDown':  prevFrame(); break;
    case 'ArrowRight': case 'ArrowUp':   nextFrame(); break;
    case ' ': e.preventDefault(); togglePlay(); break;
    case 'Home': setFrame(0); break;
    case 'End':  setFrame(S.nframes-1); break;
    case 'a': case 'A': autoContrast(); break;
    case 'i': case 'I': toggleInvert(); break;
    case 'l': case 'L': toggleLP(); break;
    case 'r': case 'R': setTool('ruler'); break;
    case 'p': case 'P': setTool('pan');   break;
    case 'Escape':       setTool('default'); break;
    case 's': case 'S': toggleAvgRange(); break;
    case '+': case '=': applyZoom(1.25); break;
    case '-':            applyZoom(1/1.25); break;
    case '0':            resetView(); break;
  }
});

// ── INIT ──────────────────────────────────────────────────────────────────────
(async () => {
  await browseTo('~');
  try {
    const d = await api('/api/list');
    if (d.list && d.list.length > 0) {
      renderLoaded(d.list);
      const active = d.list.find(f => f.active);
      if (active) {
        S.file = active.path; S.autoC = true;
        const info = await api('/api/load?path=' + encodeURIComponent(active.path));
        applyInfo(info.info);
        await fetchFrame();
        showViewer();
      }
    }
  } catch(e) {}
})();
</script>
</body>
</html>"""

# ──────────────────────────────────────────────────────────────────────────────
# UPLOAD ENDPOINT  (drag & drop from browser → BytesIO → CryoFile)
# ──────────────────────────────────────────────────────────────────────────────

# Patch Handler to handle POST /api/upload
_orig_do_GET = Handler.do_GET

class Handler(Handler):
    def do_POST(self):
        try:
            length   = int(self.headers.get('Content-Length', 0))
            filename = unquote(self.headers.get('X-Filename', 'upload.mrc'))
            filename = Path(filename).name or 'upload.mrc'
            ext      = Path(filename).suffix.lower()
            if ext not in ALL_EXTS:
                self._send({'error': f'Unsupported format: {ext}'}, status=400); return
            if length == 0:
                self._send({'error': 'Empty upload'}, status=400); return
            data = self.rfile.read(length)
            # Write to temp file so CryoFile can open it normally
            import tempfile, os
            suffix = ext
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tp = tmp.name
            key = f'__upload__{filename}'
            cf  = CryoFile(tp)
            ST.files[key] = cf
            if key not in ST.order: ST.order.append(key)
            ST.current = key
            # Override path display name
            cf.path = Path(filename)
            self._send(dict(ok=True, key=key, info=cf.info(), list=ST.list()))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try: self._send(dict(error=str(e), trace=traceback.format_exc()), status=500)
            except: pass

    def _send(self, body, ct='application/json', status=200):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.wfile.write(body)

# Patch get_file_list_info for upload keys
_orig_list = State.list
def _patched_list(self):
    result = []
    for p in self.order:
        if p.startswith('__upload__'):
            name = p[len('__upload__'):]
        else:
            name = Path(p).name
        result.append(dict(path=p, name=name,
                           loaded=p in self.files, active=p==self.current))
    return result
State.list = _patched_list

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='CryoViewer')
    ap.add_argument('path', nargs='?')
    ap.add_argument('--port', type=int, default=7473)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()

    if args.path:
        p = Path(args.path).expanduser().resolve()
        if p.is_dir():
            found = ST.scan(str(p)); ST.cwd = str(p)
            print(f'[CryoViewer] Dir: {p}  ({len(found)} files)')
        elif p.is_file():
            ST.open(str(p)); ST.cwd = str(p.parent)
            print(f'[CryoViewer] File: {p.name}')

    url = f'http://127.0.0.1:{args.port}'
    srv = HTTPServer(('127.0.0.1', args.port), Handler)
    print(f'\n  CryoViewer → {url}  (Ctrl+C to stop)\n')
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[CryoViewer] Stopped.')
        srv.shutdown()

if __name__ == '__main__':
    main()
