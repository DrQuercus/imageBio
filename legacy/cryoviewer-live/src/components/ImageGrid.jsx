import React, { useState, useRef, useCallback, useEffect, useMemo, memo } from 'react';

// ─── Single thumbnail tile ──────────────────────────────────────────────────
const ImageTile = memo(function ImageTile({ image, index, onPreview, tileSize, style }) {
  const canvasRef = useRef(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const mountedRef = useRef(true);

  const isJpeg = image.fileType === 'jpeg';
  const thumbUrl = `/api/thumbnail?path=${encodeURIComponent(image.path)}&size=${Math.min(tileSize, 256)}`;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // MRC: fetch binary thumbnail and draw to canvas
  useEffect(() => {
    if (isJpeg || loaded || loading) return;
    setLoading(true);

    const controller = new AbortController();
    fetch(thumbUrl, { signal: controller.signal })
      .then(res => {
        if (!res.ok) throw new Error(res.status);
        const w = parseInt(res.headers.get('X-Width')) || 128;
        const h = parseInt(res.headers.get('X-Height')) || 128;
        return res.arrayBuffer().then(buf => ({ w, h, buf }));
      })
      .then(({ w, h, buf }) => {
        if (!mountedRef.current) return;
        const canvas = canvasRef.current;
        if (!canvas) return;
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        const imgData = ctx.createImageData(w, h);
        const raw = new Uint8Array(buf);
        for (let i = 0; i < raw.length; i++) {
          const off = i * 4;
          imgData.data[off] = raw[i];
          imgData.data[off + 1] = raw[i];
          imgData.data[off + 2] = raw[i];
          imgData.data[off + 3] = 255;
        }
        ctx.putImageData(imgData, 0, 0);
        setLoaded(true);
        setLoading(false);
      })
      .catch((e) => {
        if (e.name === 'AbortError') return;
        if (mountedRef.current) { setLoading(false); setError(true); }
      });

    return () => controller.abort();
  }, [image.path, tileSize, isJpeg]); // eslint-disable-line

  const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b > 1e6 ? (b / 1e6).toFixed(0) + ' MB' : (b / 1e3).toFixed(0) + ' KB';
  const fmtTime = (ms) => new Date(ms).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const dims = image.nx && image.ny ? `${image.nx}×${image.ny} · ` : '';

  return (
    <div className="image-tile" style={style} onClick={() => onPreview(image, index)} title={image.name}>
      {isJpeg ? (
        <img src={thumbUrl} alt="" draggable={false}
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: loaded ? 'block' : 'none' }}
          onLoad={() => setLoaded(true)}
          onError={() => setError(true)} />
      ) : (
        <canvas ref={canvasRef} style={{ width: '100%', height: '100%', display: loaded ? 'block' : 'none' }} />
      )}
      {!loaded && !error && <div className="image-tile-loading">···</div>}
      {error && <div className="image-tile-loading">✕</div>}
      <div className="image-tile-info">
        <span className="name">{image.name}</span>
        <span>{dims}{fmtSize(image.size)} · {fmtTime(image.mtime)}</span>
      </div>
    </div>
  );
});

// ─── Virtual scrolling grid ─────────────────────────────────────────────────
export default function ImageGrid({ images, onPreview, autoScroll, setAutoScroll, showGraph, setShowGraph }) {
  const [sortNewest, setSortNewest] = useState(true);
  const [tileSize, setTileSize] = useState(180);
  const containerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerHeight, setContainerHeight] = useState(600);
  const [containerWidth, setContainerWidth] = useState(800);
  const prevImageCount = useRef(0);

  const GAP = 8;
  const cols = Math.max(1, Math.floor((containerWidth + GAP) / (tileSize + GAP)));
  const rows = Math.ceil(images.length / cols);
  const totalHeight = Math.max(0, rows * (tileSize + GAP) - GAP);

  // Visible range (2 row buffer)
  const BUFFER = 2;
  const firstVisibleRow = Math.max(0, Math.floor(scrollTop / (tileSize + GAP)) - BUFFER);
  const lastVisibleRow = Math.min(rows - 1, Math.ceil((scrollTop + containerHeight) / (tileSize + GAP)) + BUFFER);
  const firstIdx = firstVisibleRow * cols;
  const lastIdx = Math.min(images.length - 1, (lastVisibleRow + 1) * cols - 1);

  // Build visible tiles — no array copy, use index math for reverse
  const visibleTiles = useMemo(() => {
    const tiles = [];
    if (images.length === 0) return tiles;
    for (let vi = firstIdx; vi <= lastIdx; vi++) {
      // If sortNewest: visual index 0 = last image, otherwise normal
      const realIdx = sortNewest ? images.length - 1 - vi : vi;
      if (realIdx < 0 || realIdx >= images.length) continue;
      const row = Math.floor(vi / cols);
      const col = vi % cols;
      tiles.push({
        image: images[realIdx],
        index: realIdx,
        top: row * (tileSize + GAP),
        left: col * (tileSize + GAP),
        key: images[realIdx].path,
      });
    }
    return tiles;
  }, [firstIdx, lastIdx, cols, images, tileSize, sortNewest]);

  // Scroll handler — capture scrollTop synchronously, debounce setState via rAF
  const rafRef = useRef(null);
  const scrollTopRef = useRef(0);
  const onScroll = useCallback((e) => {
    scrollTopRef.current = e.target.scrollTop; // capture immediately
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      setScrollTop(scrollTopRef.current);
      rafRef.current = null;
    });
  }, []);

  // Auto-scroll on new images
  useEffect(() => {
    if (autoScroll && sortNewest && images.length > prevImageCount.current && containerRef.current) {
      containerRef.current.scrollTop = 0;
    }
    prevImageCount.current = images.length;
  }, [images.length, autoScroll, sortNewest]);

  // Resize observer
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new ResizeObserver(([entry]) => {
      setContainerHeight(entry.contentRect.height);
      setContainerWidth(entry.contentRect.width);
    });
    obs.observe(el);
    setContainerHeight(el.clientHeight);
    setContainerWidth(el.clientWidth);
    return () => obs.disconnect();
  }, []);

  return (
    <>
      <div className="grid-toolbar">
        <div className="grid-toolbar-left">
          <span className="grid-label">{images.length.toLocaleString()} image{images.length !== 1 ? 's' : ''}</span>
          <button className="sort-btn" onClick={() => setSortNewest(!sortNewest)}>
            {sortNewest ? '↓ Newest first' : '↑ Oldest first'}
          </button>
          <button className={`sort-btn ${autoScroll ? 'active' : ''}`} onClick={() => setAutoScroll(!autoScroll)}
            style={autoScroll ? { borderColor: 'var(--accent-cyan)', color: 'var(--accent-cyan)' } : {}}>
            {autoScroll ? '⇩ Auto-scroll ON' : '⇩ Auto-scroll OFF'}
          </button>
          <button className="sort-btn" onClick={() => setShowGraph(!showGraph)}>
            {showGraph ? '📈 Hide graph' : '📈 Show graph'}
          </button>
        </div>
        <div className="grid-toolbar-left">
          <span className="grid-label" style={{ fontSize: 10 }}>Tiles</span>
          <input type="range" className="thumb-size-slider" min={100} max={350} step={10}
            value={tileSize} onChange={e => setTileSize(Number(e.target.value))} />
        </div>
      </div>

      <div ref={containerRef} className="virtual-grid-scroll" onScroll={onScroll}>
        <div style={{ height: totalHeight, position: 'relative' }}>
          {visibleTiles.map(tile => (
            <ImageTile
              key={tile.key}
              image={tile.image}
              index={tile.index}
              onPreview={onPreview}
              tileSize={tileSize}
              style={{
                position: 'absolute',
                top: tile.top,
                left: tile.left,
                width: tileSize,
                height: tileSize,
              }}
            />
          ))}
        </div>
      </div>
    </>
  );
}
