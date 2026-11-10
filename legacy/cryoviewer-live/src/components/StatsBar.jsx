import React, { useState, useEffect, memo } from 'react';

function fmtBytes(b) {
  if (b == null) return '—';
  if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB';
  if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB';
  return (b / 1e3).toFixed(0) + ' KB';
}

function diskBarColor(pct) {
  if (pct < 60) return 'var(--accent-green)';
  if (pct < 80) return 'var(--accent-yellow, #f0ad4e)';
  if (pct < 90) return 'var(--accent-orange, #e67e22)';
  return 'var(--accent-red, #e74c3c)';
}

export default memo(function StatsBar({ stats }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const fmtDuration = (ms) => {
    if (ms == null) return '—';
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s ago`;
    return `${Math.floor(m / 60)}h ${m % 60}m ago`;
  };

  const beacon = (ms) => {
    if (ms == null) return '';
    if (ms < 120000) return 'green';
    if (ms < 600000) return 'orange';
    return 'red';
  };

  const fmtClock = (ts) => ts ? new Date(ts).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';

  const timeSinceCreated = stats.lastCreatedTime ? now - stats.lastCreatedTime : null;
  const timeSinceFound = stats.lastFoundAt ? now - stats.lastFoundAt : null;
  const disk = stats.disk;

  return (
    <div className="stats-bar">
      <div className="stat-card">
        <span className="stat-label">Images</span>
        <span className="stat-value">{(stats.totalImages || 0).toLocaleString()}</span>
        <span className="stat-sub">
          {stats.scanDone
            ? `${(stats.initialScanCount || 0).toLocaleString()} initial · ${(stats.liveCount || 0).toLocaleString()} live`
            : 'scanning...'}
        </span>
      </div>

      <div className="stat-card">
        <span className="stat-label">Instant Rate</span>
        <span className="stat-value rate">{stats.rateInstant ?? '—'}</span>
        <span className="stat-sub">img/h (5 min)</span>
      </div>

      <div className="stat-card">
        <span className="stat-label">Avg Rate</span>
        <span className="stat-value">{stats.scanDone ? (stats.rateAvg ?? 0) : '—'}</span>
        <span className="stat-sub">img/h (since scan)</span>
      </div>

      <div className="stat-card">
        <span className="stat-label">Last Created</span>
        <div className="status-beacon-row">
          {timeSinceCreated != null && <div className={`status-beacon ${beacon(timeSinceCreated)}`}/>}
          <span className="stat-value" style={{fontSize: 16}}>{fmtDuration(timeSinceCreated)}</span>
        </div>
        <span className="stat-sub">{fmtClock(stats.lastCreatedTime)}</span>
      </div>

      <div className="stat-card">
        <span className="stat-label">Last Found</span>
        <div className="status-beacon-row">
          {timeSinceFound != null && <div className={`status-beacon ${beacon(timeSinceFound)}`}/>}
          <span className="stat-value" style={{fontSize: 16}}>{fmtDuration(timeSinceFound)}</span>
        </div>
        <span className="stat-sub">{fmtClock(stats.lastFoundAt)}</span>
      </div>

      {/* Disk space */}
      <div className="stat-card">
        <span className="stat-label">Disk Usage</span>
        {disk ? (
          <>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
              <span className="stat-value" style={{ fontSize: 16, color: diskBarColor(disk.percent) }}>
                {disk.percent}%
              </span>
            </div>
            <div className="disk-bar-bg">
              <div className="disk-bar-fill" style={{ width: `${Math.min(100, disk.percent)}%`, background: diskBarColor(disk.percent) }} />
            </div>
            <span className="stat-sub">{fmtBytes(disk.free)} free / {fmtBytes(disk.total)}</span>
          </>
        ) : (
          <span className="stat-value" style={{ fontSize: 14, color: 'var(--text-dim)' }}>—</span>
        )}
      </div>

      <div className="stat-card">
        <span className="stat-label">Status</span>
        <span className="stat-value" style={{fontSize: 14, color: stats.watchActive ? 'var(--accent-green)' : 'var(--text-dim)'}}>
          {stats.watchActive
            ? (stats.scanDone ? '● WATCHING' : '◌ SCANNING')
            : '○ IDLE'}
        </span>
        <span className="stat-sub" style={{maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 10}}>
          {stats.watchPattern || '—'}
        </span>
      </div>
    </div>
  );
});
