import React, { useState, useEffect, useRef, useCallback, memo } from 'react';

// ─── Gaussian blur (separable) ──────────────────────────────────────────────
function gaussianBlur(src, w, h, radius) {
  if (radius <= 0) return src;
  const r = Math.min(radius, 30);
  const size = r * 2 + 1;
  const sigma = r / 2.5;
  const kernel = new Float32Array(size);
  let sum = 0;
  for (let i = 0; i < size; i++) {
    const x = i - r;
    kernel[i] = Math.exp(-(x * x) / (2 * sigma * sigma));
    sum += kernel[i];
  }
  for (let i = 0; i < size; i++) kernel[i] /= sum;

  const temp = new Float32Array(w * h);
  const out = new Float32Array(w * h);

  for (let y = 0; y < h; y++) {
    const row = y * w;
    for (let x = 0; x < w; x++) {
      let s = 0;
      for (let k = 0; k < size; k++) {
        s += src[row + Math.min(w - 1, Math.max(0, x + k - r))] * kernel[k];
      }
      temp[row + x] = s;
    }
  }
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      let s = 0;
      for (let k = 0; k < size; k++) {
        s += temp[Math.min(h - 1, Math.max(0, y + k - r)) * w + x] * kernel[k];
      }
      out[y * w + x] = s;
    }
  }
  return out;
}

export default memo(function PreviewModal({ image, index, total, onClose, onNavigate }) {
  const isJpeg = image.fileType === 'jpeg';

  // Shared state
  const containerRef = useRef(null);
  const overlayCanvasRef = useRef(null); // for ruler on both modes
  const [zoom, setZoom] = useState(1);
  const [panOffset, setPanOffset] = useState({ x: 0, y: 0 });
  const isPanningRef = useRef(false);
  const panStartRef = useRef({ x: 0, y: 0 });
  const panOffsetStartRef = useRef({ x: 0, y: 0 });

  const [brightness, setBrightness] = useState(0);
  const [contrast, setContrast] = useState(1);
  const [invert, setInvert] = useState(false);
  const [rulerActive, setRulerActive] = useState(false);
  const [rulerStart, setRulerStart] = useState(null);
  const [rulerEnd, setRulerEnd] = useState(null);
  const isDrawingRulerRef = useRef(false);

  // MRC-only state
  const mrcCanvasRef = useRef(null);
  const rawRef = useRef(null);
  const [imgMeta, setImgMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lpRadius, setLpRadius] = useState(0);
  const processedBitmapRef = useRef(null);
  const processedParamsRef = useRef('');

  // JPEG-only state
  const [jpegNaturalSize, setJpegNaturalSize] = useState(null);

  // ─── Load image ────────────────────────────────────────────────────
  useEffect(() => {
    setLoading(true);
    rawRef.current = null;
    processedBitmapRef.current = null;
    processedParamsRef.current = '';
    setRulerStart(null);
    setRulerEnd(null);
    setLpRadius(0);
    setJpegNaturalSize(null);

    if (isJpeg) {
      // JPEG: preload to get natural dimensions, then fit
      const img = new Image();
      img.onload = () => {
        setJpegNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
        setLoading(false);
        if (containerRef.current) {
          const cw = containerRef.current.clientWidth;
          const ch = containerRef.current.clientHeight;
          const fitZoom = Math.min(cw / img.naturalWidth, ch / img.naturalHeight) * 0.95;
          setZoom(fitZoom);
          setPanOffset({
            x: (cw - img.naturalWidth * fitZoom) / 2,
            y: (ch - img.naturalHeight * fitZoom) / 2,
          });
        }
      };
      img.onerror = () => setLoading(false);
      img.src = `/api/image-data?path=${encodeURIComponent(image.path)}`;
    } else {
      // MRC: fetch binary float32
      const controller = new AbortController();
      fetch(`/api/image-data?path=${encodeURIComponent(image.path)}`, { signal: controller.signal })
        .then(res => {
          if (!res.ok) throw new Error('fail');
          const nx = parseInt(res.headers.get('X-NX'));
          const ny = parseInt(res.headers.get('X-NY'));
          const origNx = parseInt(res.headers.get('X-Orig-NX'));
          const origNy = parseInt(res.headers.get('X-Orig-NY'));
          const pixelSize = parseFloat(res.headers.get('X-Pixel-Size'));
          return res.arrayBuffer().then(buf => ({ nx, ny, origNx, origNy, pixelSize, buf }));
        })
        .then(({ nx, ny, origNx, origNy, pixelSize, buf }) => {
          rawRef.current = { nx, ny, pixels: new Float32Array(buf) };
          setImgMeta({ nx, ny, origNx, origNy, pixelSize });
          setLoading(false);
          if (containerRef.current) {
            const cw = containerRef.current.clientWidth;
            const ch = containerRef.current.clientHeight;
            const fitZoom = Math.min(cw / nx, ch / ny) * 0.95;
            setZoom(fitZoom);
            setPanOffset({ x: (cw - nx * fitZoom) / 2, y: (ch - ny * fitZoom) / 2 });
          }
        })
        .catch(() => setLoading(false));
      return () => controller.abort();
    }
  }, [image.path, isJpeg]);

  // ─── MRC: process image ────────────────────────────────────────────
  const processImage = useCallback(() => {
    const raw = rawRef.current;
    if (!raw) return null;
    const paramsKey = `${lpRadius}:${brightness}:${contrast}:${invert}`;
    if (paramsKey === processedParamsRef.current && processedBitmapRef.current) return processedBitmapRef.current;

    const { nx, ny, pixels } = raw;
    let processed = lpRadius > 0 ? gaussianBlur(pixels, nx, ny, lpRadius) : pixels;

    const sampleStride = Math.max(1, Math.floor(processed.length / 5000));
    const samples = [];
    for (let i = 0; i < processed.length; i += sampleStride) samples.push(processed[i]);
    samples.sort((a, b) => a - b);
    const p02 = samples[Math.floor(samples.length * 0.02)];
    const p98 = samples[Math.floor(samples.length * 0.98)];
    const range = p98 - p02 || 1;

    const imgData = new ImageData(nx, ny);
    const d = imgData.data;
    for (let i = 0; i < nx * ny; i++) {
      let v = ((processed[i] - p02) / range) * 255;
      v = (v - 128) * contrast + 128 + brightness;
      v = Math.max(0, Math.min(255, v));
      if (invert) v = 255 - v;
      const off = i * 4;
      d[off] = v; d[off + 1] = v; d[off + 2] = v; d[off + 3] = 255;
    }

    processedBitmapRef.current = imgData;
    processedParamsRef.current = paramsKey;
    return imgData;
  }, [lpRadius, brightness, contrast, invert]);

  // ─── MRC: render to canvas ─────────────────────────────────────────
  const renderMrcCanvas = useCallback(() => {
    const canvas = mrcCanvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container || isJpeg) return;

    const dpr = window.devicePixelRatio || 1;
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    canvas.width = cw * dpr;
    canvas.height = ch * dpr;
    canvas.style.width = cw + 'px';
    canvas.style.height = ch + 'px';

    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cw, ch);

    const imgData = processImage();
    if (!imgData) return;

    const off = new OffscreenCanvas(imgData.width, imgData.height);
    off.getContext('2d').putImageData(imgData, 0, 0);

    ctx.imageSmoothingEnabled = zoom < 1;
    ctx.save();
    ctx.translate(panOffset.x, panOffset.y);
    ctx.scale(zoom, zoom);
    ctx.drawImage(off, 0, 0);
    ctx.restore();
  }, [processImage, zoom, panOffset, isJpeg]);

  // ─── Ruler overlay (shared for both modes) ─────────────────────────
  const renderRulerOverlay = useCallback(() => {
    const canvas = overlayCanvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const dpr = window.devicePixelRatio || 1;
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    canvas.width = cw * dpr;
    canvas.height = ch * dpr;
    canvas.style.width = cw + 'px';
    canvas.style.height = ch + 'px';

    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cw, ch);

    if (!rulerStart || !rulerEnd) return;

    const sx = rulerStart.x * zoom + panOffset.x;
    const sy = rulerStart.y * zoom + panOffset.y;
    const ex = rulerEnd.x * zoom + panOffset.x;
    const ey = rulerEnd.y * zoom + panOffset.y;

    ctx.save();
    ctx.strokeStyle = '#ff4444';
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();

    ctx.setLineDash([]);
    ctx.fillStyle = '#ff4444';
    ctx.beginPath(); ctx.arc(sx, sy, 4, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(ex, ey, 4, 0, Math.PI * 2); ctx.fill();

    const dx = rulerEnd.x - rulerStart.x;
    const dy = rulerEnd.y - rulerStart.y;
    const distPx = Math.sqrt(dx * dx + dy * dy);
    const ps = imgMeta?.pixelSize || 0;
    const label = ps > 0
      ? `${(distPx * ps).toFixed(1)} Å (${distPx.toFixed(0)} px)`
      : `${distPx.toFixed(0)} px`;

    ctx.font = '12px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(label, (sx + ex) / 2, (sy + ey) / 2 - 10);
    ctx.restore();
  }, [zoom, panOffset, rulerStart, rulerEnd, imgMeta]);

  // ─── Render scheduling via rAF ─────────────────────────────────────
  const rafRef = useRef(null);
  const scheduleRender = useCallback(() => {
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      if (!isJpeg) renderMrcCanvas();
      renderRulerOverlay();
    });
  }, [renderMrcCanvas, renderRulerOverlay, isJpeg]);

  useEffect(() => { scheduleRender(); }, [scheduleRender]);

  useEffect(() => {
    const obs = new ResizeObserver(() => scheduleRender());
    if (containerRef.current) obs.observe(containerRef.current);
    return () => obs.disconnect();
  }, [scheduleRender]);

  // ─── Mouse handlers ────────────────────────────────────────────────
  const getImageCoords = (e) => {
    const rect = (overlayCanvasRef.current || containerRef.current).getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    return { ix: (cx - panOffset.x) / zoom, iy: (cy - panOffset.y) / zoom, cx, cy };
  };

  const handleMouseDown = (e) => {
    if (rulerActive) {
      const { ix, iy } = getImageCoords(e);
      setRulerStart({ x: ix, y: iy });
      setRulerEnd({ x: ix, y: iy });
      isDrawingRulerRef.current = true;
    } else {
      isPanningRef.current = true;
      panStartRef.current = { x: e.clientX, y: e.clientY };
      panOffsetStartRef.current = { ...panOffset };
    }
  };

  const handleMouseMove = (e) => {
    if (isDrawingRulerRef.current) {
      const { ix, iy } = getImageCoords(e);
      setRulerEnd({ x: ix, y: iy });
    } else if (isPanningRef.current) {
      setPanOffset({
        x: panOffsetStartRef.current.x + (e.clientX - panStartRef.current.x),
        y: panOffsetStartRef.current.y + (e.clientY - panStartRef.current.y),
      });
    }
  };

  const handleMouseUp = () => { isPanningRef.current = false; isDrawingRulerRef.current = false; };

  const handleWheel = (e) => {
    e.preventDefault();
    const rect = (overlayCanvasRef.current || containerRef.current).getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    const factor = e.deltaY > 0 ? 0.9 : 1.1;
    const nz = Math.max(0.01, Math.min(50, zoom * factor));
    setPanOffset({
      x: cx - (cx - panOffset.x) * (nz / zoom),
      y: cy - (cy - panOffset.y) * (nz / zoom),
    });
    setZoom(nz);
  };

  // Keyboard
  useEffect(() => {
    const h = (e) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'ArrowLeft') onNavigate(-1);
      if (e.key === 'ArrowRight') onNavigate(1);
      if (e.key === 'r' || e.key === 'R') setRulerActive(p => !p);
      if (e.key === 'i' || e.key === 'I') setInvert(p => !p);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose, onNavigate]);

  const rulerDist = rulerStart && rulerEnd ? (() => {
    const dx = rulerEnd.x - rulerStart.x, dy = rulerEnd.y - rulerStart.y;
    const px = Math.sqrt(dx * dx + dy * dy);
    return { px, ang: px * (imgMeta?.pixelSize || 0) };
  })() : null;

  const resetView = () => {
    const container = containerRef.current;
    if (!container) return;
    const cw = container.clientWidth, ch = container.clientHeight;
    let iw, ih;
    if (isJpeg && jpegNaturalSize) { iw = jpegNaturalSize.w; ih = jpegNaturalSize.h; }
    else if (rawRef.current) { iw = rawRef.current.nx; ih = rawRef.current.ny; }
    else return;
    const fz = Math.min(cw / iw, ch / ih) * 0.95;
    setZoom(fz);
    setPanOffset({ x: (cw - iw * fz) / 2, y: (ch - ih * fz) / 2 });
  };

  // JPEG CSS filter string
  const jpegFilter = isJpeg
    ? `brightness(${1 + brightness / 128}) contrast(${contrast}) ${invert ? 'invert(1)' : ''}`
    : '';

  // Dimensions for display
  const dispNx = isJpeg ? jpegNaturalSize?.w : imgMeta?.origNx;
  const dispNy = isJpeg ? jpegNaturalSize?.h : imgMeta?.origNy;

  return (
    <div className="modal-overlay" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-content">
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <div className="nav-arrows">
              <button className="nav-btn" onClick={() => onNavigate(-1)} disabled={index <= 0}>←</button>
              <button className="nav-btn" onClick={() => onNavigate(1)} disabled={index >= total - 1}>→</button>
            </div>
            <span className="modal-title">{image.name}</span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {index + 1} / {total}
            </span>
            {isJpeg && <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--accent-cyan)', background: 'var(--bg-tertiary)', padding: '2px 6px', borderRadius: 4 }}>JPEG</span>}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <div className="modal-meta">
              {dispNx && dispNy && <span>{dispNx} × {dispNy}</span>}
              {!isJpeg && imgMeta?.pixelSize > 0 && <span>{imgMeta.pixelSize.toFixed(3)} Å/px</span>}
              <span>{(zoom * 100).toFixed(0)}%</span>
            </div>
            <button className="modal-close" onClick={onClose}>✕</button>
          </div>
        </div>

        <div className="modal-body">
          {/* ─── Image area ──────────────────────────── */}
          <div className="preview-canvas-container" ref={containerRef}
            onMouseDown={handleMouseDown} onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp} onMouseLeave={handleMouseUp} onWheel={handleWheel}
            style={{ cursor: rulerActive ? 'crosshair' : (isPanningRef.current ? 'grabbing' : 'grab') }}>

            {loading && (
              <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 13, zIndex: 1 }}>
                Loading...
              </div>
            )}

            {/* JPEG: native img with CSS transforms */}
            {isJpeg && !loading && jpegNaturalSize && (
              <img
                src={`/api/image-data?path=${encodeURIComponent(image.path)}`}
                alt={image.name}
                draggable={false}
                style={{
                  position: 'absolute',
                  top: 0, left: 0,
                  transformOrigin: '0 0',
                  transform: `translate(${panOffset.x}px, ${panOffset.y}px) scale(${zoom})`,
                  filter: jpegFilter,
                  imageRendering: zoom > 2 ? 'pixelated' : 'auto',
                  pointerEvents: 'none',
                }}
              />
            )}

            {/* MRC: canvas with pixel-level processing */}
            {!isJpeg && <canvas ref={mrcCanvasRef} style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }} />}

            {/* Ruler overlay (shared) */}
            <canvas ref={overlayCanvasRef} style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none', zIndex: 2 }} />
          </div>

          {/* ─── Controls Panel ──────────────────────── */}
          <div className="preview-controls">
            {/* LP Filter — MRC only */}
            {!isJpeg && (
              <div className="control-group">
                <span className="control-label">Low-Pass Filter</span>
                <div className="control-row">
                  <label>Radius</label>
                  <input type="range" min={0} max={30} step={1} value={lpRadius}
                    onChange={e => setLpRadius(Number(e.target.value))} />
                  <span className="value">{lpRadius} px</span>
                </div>
              </div>
            )}

            {/* Display — shared (CSS filter for JPEG, pixel for MRC) */}
            <div className="control-group">
              <span className="control-label">Display</span>
              <div className="control-row">
                <label>Bright.</label>
                <input type="range" min={-128} max={128} step={1} value={brightness}
                  onChange={e => setBrightness(Number(e.target.value))} />
                <span className="value">{brightness}</span>
              </div>
              <div className="control-row">
                <label>Contr.</label>
                <input type="range" min={0.1} max={5} step={0.1} value={contrast}
                  onChange={e => setContrast(Number(e.target.value))} />
                <span className="value">{contrast.toFixed(1)}×</span>
              </div>
              <div className="toggle-row">
                <input type="checkbox" id="invert" checked={invert} onChange={e => setInvert(e.target.checked)} />
                <label htmlFor="invert">Invert (I)</label>
              </div>
            </div>

            {/* Ruler */}
            <div className="control-group">
              <span className="control-label">Ruler Tool</span>
              <div className="toggle-row">
                <input type="checkbox" id="ruler" checked={rulerActive} onChange={e => setRulerActive(e.target.checked)} />
                <label htmlFor="ruler">Enable ruler (R)</label>
              </div>
              {rulerDist && (
                <div className="ruler-info">
                  <strong>{rulerDist.px.toFixed(1)}</strong> px
                  {imgMeta?.pixelSize > 0 && <><br /><strong>{rulerDist.ang.toFixed(1)}</strong> Å</>}
                </div>
              )}
              {rulerStart && (
                <button className="btn btn-sm" style={{ marginTop: 6, background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)' }}
                  onClick={() => { setRulerStart(null); setRulerEnd(null); }}>Clear ruler</button>
              )}
            </div>

            {/* View */}
            <div className="control-group">
              <span className="control-label">View</span>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <button className="btn btn-sm" style={{ background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)' }} onClick={resetView}>Fit</button>
                <button className="btn btn-sm" style={{ background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)' }} onClick={() => setZoom(1)}>1:1</button>
                <button className="btn btn-sm" style={{ background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', border: '1px solid var(--border)' }} onClick={() => {
                  setLpRadius(0); setBrightness(0); setContrast(1); setInvert(false); resetView();
                }}>Reset</button>
              </div>
            </div>

            {/* File info */}
            <div className="control-group">
              <span className="control-label">File Info</span>
              <div className="ruler-info">
                <strong>{image.name}</strong><br />
                {dispNx && dispNy && <>{dispNx} × {dispNy}<br /></>}
                {!isJpeg && imgMeta?.pixelSize > 0 && <>{imgMeta.pixelSize.toFixed(4)} Å/px<br /></>}
                {(image.size / 1e6).toFixed(1)} MB · {isJpeg ? 'JPEG' : 'MRC'}<br />
                {new Date(image.mtime).toLocaleString('fr-FR')}
              </div>
            </div>

            {/* Shortcuts */}
            <div className="control-group">
              <span className="control-label">Shortcuts</span>
              <div className="ruler-info" style={{ lineHeight: 1.8 }}>
                <strong>←→</strong> Nav · <strong>Scroll</strong> Zoom · <strong>Drag</strong> Pan<br />
                <strong>R</strong> Ruler · <strong>I</strong> Invert · <strong>Esc</strong> Close
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
});
