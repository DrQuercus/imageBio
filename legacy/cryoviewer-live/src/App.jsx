import React, { useState, useEffect, useRef, useCallback } from 'react';
import StatsBar from './components/StatsBar';
import RateGraph from './components/RateGraph';
import ImageGrid from './components/ImageGrid';
import PreviewModal from './components/PreviewModal';

function getWsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}`;
}

export default function App() {
  const [wsConnected, setWsConnected] = useState(false);
  const [stats, setStats] = useState({
    totalImages: 0, initialScanCount: 0, liveCount: 0,
    rateInstant: 0, rateAvg: 0,
    lastCreatedTime: null, lastFoundAt: null,
    timeSinceCreated: null, timeSinceFound: null,
    watchPattern: '', watchActive: false, scanDone: false, disk: null,
  });
  const [images, setImages] = useState([]);
  const [pathInput, setPathInput] = useState('');
  const [previewImage, setPreviewImage] = useState(null);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [autoScroll, setAutoScroll] = useState(true);
  const [showGraph, setShowGraph] = useState(true);
  const [watchError, setWatchError] = useState('');

  const wsRef = useRef(null);
  const reconnectRef = useRef(null);
  const backoffRef = useRef(1000);        // reconnect backoff, starts at 1s
  const pathSetRef = useRef(new Set());   // O(1) dedup for image paths
  const mountedRef = useRef(true);

  // ─── WebSocket with exponential backoff ────────────────────────────
  const connectWs = useCallback(() => {
    if (!mountedRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN ||
        wsRef.current?.readyState === WebSocket.CONNECTING) return;

    try {
      const ws = new WebSocket(getWsUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) return;
        setWsConnected(true);
        backoffRef.current = 1000;
        // Clear stale local state on (re)connect — server will resend everything
        setImages([]);
        pathSetRef.current.clear();
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setWsConnected(false);
        wsRef.current = null;
        const delay = backoffRef.current;
        backoffRef.current = Math.min(delay * 2, 15000);
        reconnectRef.current = setTimeout(connectWs, delay);
      };

      ws.onerror = () => { try { ws.close(); } catch(e) {} };

      ws.onmessage = (event) => {
        if (!mountedRef.current) return;
        try {
          const data = JSON.parse(event.data);
          switch (data.type) {
            case 'stats':
              setStats(data);
              // Sync pathInput from server state (for new/reconnecting clients)
              if (data.watchPattern) setPathInput(data.watchPattern);
              break;

            case 'image_list_chunk':
              setImages(prev => {
                const added = [];
                for (const img of data.images) {
                  if (!pathSetRef.current.has(img.path)) {
                    pathSetRef.current.add(img.path);
                    added.push(img);
                  }
                }
                return added.length > 0 ? [...prev, ...added] : prev;
              });
              break;

            case 'scan_complete':
            case 'sync_complete':
              setImages(prev => [...prev].sort((a, b) => a.mtime - b.mtime));
              break;

            case 'new_images_batch':
              setImages(prev => {
                const added = [];
                for (const img of data.images) {
                  if (!pathSetRef.current.has(img.path)) {
                    pathSetRef.current.add(img.path);
                    added.push(img);
                  }
                }
                if (added.length === 0) return prev;
                const merged = [...prev, ...added];
                merged.sort((a, b) => a.mtime - b.mtime);
                return merged;
              });
              break;

            case 'watch_error':
              setWatchError(data.message || 'Watch refused by the server.');
              break;

            case 'watch_started':
              setWatchError('');
              setPathInput(data.pattern);
              setStats(prev => ({ ...prev, watchActive: true, watchPattern: data.pattern }));
              break;

            case 'watch_stopped':
              setStats({
                totalImages: 0, initialScanCount: 0, liveCount: 0,
                rateInstant: 0, rateAvg: 0,
                lastCreatedTime: null, lastFoundAt: null,
                timeSinceCreated: null, timeSinceFound: null,
                watchPattern: '', watchActive: false, scanDone: false, disk: null,
              });
              setImages([]);
              pathSetRef.current.clear();
              setPathInput('');
              break;
          }
        } catch (e) { /* malformed message, skip */ }
      };
    } catch (e) {
      // WebSocket constructor can throw if URL is invalid
      reconnectRef.current = setTimeout(connectWs, backoffRef.current);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connectWs();
    return () => {
      mountedRef.current = false;
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      if (wsRef.current) { try { wsRef.current.close(); } catch(e) {} }
    };
  }, [connectWs]);

  // ─── Actions ───────────────────────────────────────────────────────
  const sendWs = (msg) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      try { wsRef.current.send(JSON.stringify(msg)); } catch(e) {}
    }
  };

  const startWatch = () => {
    if (!pathInput.trim()) return;
    setWatchError('');
    // Clear local state for fresh watch
    setImages([]);
    pathSetRef.current.clear();
    sendWs({ type: 'start_watch', pattern: pathInput.trim() });
  };

  const stopWatch = () => {
    sendWs({ type: 'stop_watch' });
    setImages([]);
    pathSetRef.current.clear();
    setPathInput('');
    setPreviewImage(null);
    setPreviewIndex(-1);
    setStats({
      totalImages: 0, initialScanCount: 0, liveCount: 0,
      rateInstant: 0, rateAvg: 0,
      lastCreatedTime: null, lastFoundAt: null,
      timeSinceCreated: null, timeSinceFound: null,
      watchPattern: '', watchActive: false, scanDone: false, disk: null,
    });
  };

  const openPreview = useCallback((image, index) => {
    setPreviewImage(image);
    setPreviewIndex(index);
  }, []);

  const closePreview = useCallback(() => {
    setPreviewImage(null);
    setPreviewIndex(-1);
  }, []);

  const navigatePreview = useCallback((delta) => {
    setPreviewIndex(prev => {
      const next = prev + delta;
      if (next < 0 || next >= images.length) return prev;
      setPreviewImage(images[next]);
      return next;
    });
  }, [images]);

  return (
    <div className="app-container">
      <header className="header">
        <div className="header-logo">
          <svg viewBox="0 0 28 28" fill="none" width="28" height="28">
            <circle cx="14" cy="14" r="12" stroke="#4d9cf7" strokeWidth="1.5"/>
            <circle cx="14" cy="14" r="6" stroke="#36d7b7" strokeWidth="1.5"/>
            <circle cx="14" cy="14" r="2" fill="#36d7b7"/>
            <line x1="14" y1="2" x2="14" y2="7" stroke="#4d9cf7" strokeWidth="1"/>
            <line x1="14" y1="21" x2="14" y2="26" stroke="#4d9cf7" strokeWidth="1"/>
            <line x1="2" y1="14" x2="7" y2="14" stroke="#4d9cf7" strokeWidth="1"/>
            <line x1="21" y1="14" x2="26" y2="14" stroke="#4d9cf7" strokeWidth="1"/>
          </svg>
          <h1>CryoViewer <span>Live</span></h1>
        </div>
        <div className="header-status">
          <div className="ws-indicator">
            <div className={`ws-dot ${wsConnected ? '' : 'disconnected'}`}/>
            <span>{wsConnected ? 'Connected' : 'Reconnecting...'}</span>
          </div>
        </div>
      </header>

      <div className="path-bar">
        <label>Watch Path</label>
        <input className="path-input" type="text" value={pathInput}
          onChange={e => setPathInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && startWatch()}
          placeholder="/srv/glacios/session1/**/*.jpg"/>
        {stats.watchActive
          ? <button className="btn btn-danger" onClick={stopWatch}>■ Stop</button>
          : <button className="btn btn-primary" onClick={startWatch}>▶ Watch</button>}
      </div>
      {watchError && <div className="path-error" role="alert">{watchError}</div>}

      <StatsBar stats={stats}/>

      {showGraph && stats.watchActive && <RateGraph/>}

      <div className="main-content">
        {images.length === 0 && !stats.watchActive ? (
          <div className="empty-state">
            <svg viewBox="0 0 48 48" fill="none" width="48" height="48">
              <rect x="6" y="10" width="36" height="28" rx="3" stroke="currentColor" strokeWidth="2"/>
              <path d="M6 30l10-8 8 6 8-10 10 8" stroke="currentColor" strokeWidth="2" fill="none"/>
              <circle cx="16" cy="20" r="3" stroke="currentColor" strokeWidth="2"/>
            </svg>
            <p>Enter a glob pattern to start monitoring</p>
            <p style={{fontSize:11,color:'var(--text-dim)'}}>e.g. /srv/glacios/collecte1/**/*.jpg</p>
          </div>
        ) : images.length === 0 && stats.watchActive ? (
          <div className="empty-state">
            <p>Scanning for images...</p>
            <p style={{fontSize:11,color:'var(--text-dim)',fontFamily:'var(--font-mono)'}}>{stats.watchPattern}</p>
          </div>
        ) : (
          <ImageGrid images={images} onPreview={openPreview} autoScroll={autoScroll} setAutoScroll={setAutoScroll}
            showGraph={showGraph} setShowGraph={setShowGraph}/>
        )}
      </div>

      {previewImage && (
        <PreviewModal image={previewImage} index={previewIndex} total={images.length}
          onClose={closePreview} onNavigate={navigatePreview}/>
      )}
    </div>
  );
}
