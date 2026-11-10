import React, { useRef, useEffect, useCallback, useState, memo } from 'react';

const VIEWS = [
  { id: 'rate', label: 'Rate' },
  { id: 'cumul', label: 'Cumulative' },
  { id: 'heatmap', label: 'Heatmap' },
];

export default memo(function RateGraph() {
  const canvasRef = useRef(null);
  const dataRef = useRef([]);
  const [view, setView] = useState('rate');

  const fetchAndDraw = useCallback(async () => {
    try {
      const res = await fetch('/api/rate-history?max=600');
      const data = await res.json();
      dataRef.current = data;
      draw(data);
    } catch (e) {}
  }, [view]); // eslint-disable-line

  // ─── Drawing ──────────────────────────────────────────────────
  const draw = useCallback((data) => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const W = rect.width;
    const H = rect.height;
    ctx.clearRect(0, 0, W, H);

    if (!data || data.length < 2) {
      ctx.fillStyle = '#4a5f74';
      ctx.font = '11px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText('Waiting for live images...', W / 2, H / 2);
      return;
    }

    if (view === 'rate') drawRate(ctx, data, W, H);
    else if (view === 'cumul') drawCumulative(ctx, data, W, H);
    else if (view === 'heatmap') drawHeatmap(ctx, data, W, H);
  }, [view]);

  // ─── Rate view ────────────────────────────────────────────────
  function drawRate(ctx, data, W, H) {
    const rates = [];
    for (let i = 1; i < data.length; i++) {
      const dt = (data[i].t - data[i - 1].t) / 3600000;
      const dc = data[i].liveCount - data[i - 1].liveCount;
      rates.push({ t: data[i].t, rate: dt > 0 ? Math.max(0, dc / dt) : 0 });
    }
    if (rates.length < 1) return;

    const maxRate = Math.max(1, ...rates.map(r => r.rate));
    const tMin = rates[0].t, tMax = rates[rates.length - 1].t, tRange = tMax - tMin || 1;
    const PAD_L = 45, PAD_R = 10, PAD_T = 10, PAD_B = 24;
    const plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;

    drawGrid(ctx, W, H, PAD_L, PAD_R, PAD_T, PAD_B, plotH, maxRate, rates, tMin, tRange, plotW);

    // Area
    ctx.beginPath();
    ctx.moveTo(PAD_L, PAD_T + plotH);
    for (let i = 0; i < rates.length; i++) {
      ctx.lineTo(PAD_L + ((rates[i].t - tMin) / tRange) * plotW, PAD_T + plotH - (rates[i].rate / maxRate) * plotH);
    }
    ctx.lineTo(PAD_L + plotW, PAD_T + plotH);
    ctx.closePath();
    ctx.fillStyle = 'rgba(54, 215, 183, 0.08)';
    ctx.fill();

    // Line
    ctx.beginPath();
    for (let i = 0; i < rates.length; i++) {
      const x = PAD_L + ((rates[i].t - tMin) / tRange) * plotW;
      const y = PAD_T + plotH - (rates[i].rate / maxRate) * plotH;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#36d7b7';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Dot on last point
    const last = rates[rates.length - 1];
    const lx = PAD_L + plotW, ly = PAD_T + plotH - (last.rate / maxRate) * plotH;
    ctx.fillStyle = '#36d7b7';
    ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI * 2); ctx.fill();

    ctx.fillStyle = '#4a5f74';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText('live img/h', PAD_L + 4, PAD_T + 12);
  }

  // ─── Cumulative view ──────────────────────────────────────────
  function drawCumulative(ctx, data, W, H) {
    const PAD_L = 45, PAD_R = 10, PAD_T = 10, PAD_B = 24;
    const plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
    const maxVal = Math.max(1, ...data.map(d => d.liveCount));
    const tMin = data[0].t, tMax = data[data.length - 1].t, tRange = tMax - tMin || 1;

    drawGrid(ctx, W, H, PAD_L, PAD_R, PAD_T, PAD_B, plotH, maxVal, data, tMin, tRange, plotW);

    // Area
    ctx.beginPath();
    ctx.moveTo(PAD_L, PAD_T + plotH);
    for (const d of data) {
      ctx.lineTo(PAD_L + ((d.t - tMin) / tRange) * plotW, PAD_T + plotH - (d.liveCount / maxVal) * plotH);
    }
    ctx.lineTo(PAD_L + ((data[data.length - 1].t - tMin) / tRange) * plotW, PAD_T + plotH);
    ctx.closePath();
    ctx.fillStyle = 'rgba(77, 156, 247, 0.08)';
    ctx.fill();

    // Line
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = PAD_L + ((data[i].t - tMin) / tRange) * plotW;
      const y = PAD_T + plotH - (data[i].liveCount / maxVal) * plotH;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#4d9cf7';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Dot
    const last = data[data.length - 1];
    const lx = PAD_L + plotW, ly = PAD_T + plotH - (last.liveCount / maxVal) * plotH;
    ctx.fillStyle = '#4d9cf7';
    ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI * 2); ctx.fill();

    ctx.fillStyle = '#4a5f74';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(`cumul: ${last.liveCount} live`, PAD_L + 4, PAD_T + 12);
  }

  // ─── Heatmap view ────────────────────────────────────────────
  function drawHeatmap(ctx, data, W, H) {
    if (data.length < 2) return;

    const PAD_L = 45, PAD_R = 10, PAD_T = 22, PAD_B = 24;
    const plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
    const tMin = data[0].t, tMax = data[data.length - 1].t;
    const tRange = tMax - tMin || 1;

    // Build 5-min buckets
    const BUCKET_MS = 5 * 60 * 1000;
    const bucketStart = Math.floor(tMin / BUCKET_MS) * BUCKET_MS;
    const bucketEnd = Math.ceil(tMax / BUCKET_MS) * BUCKET_MS;
    const nBuckets = Math.max(1, Math.ceil((bucketEnd - bucketStart) / BUCKET_MS));
    const buckets = new Array(nBuckets).fill(0);

    // Fill buckets from cumulative diff
    for (let i = 1; i < data.length; i++) {
      const dc = data[i].liveCount - data[i - 1].liveCount;
      if (dc <= 0) continue;
      const bi = Math.floor((data[i].t - bucketStart) / BUCKET_MS);
      if (bi >= 0 && bi < nBuckets) buckets[bi] += dc;
    }

    const maxBucket = Math.max(1, ...buckets);

    // Draw cells
    const cellW = plotW / nBuckets;
    const barH = Math.min(plotH, 40);
    const barY = PAD_T + (plotH - barH) / 2;

    for (let i = 0; i < nBuckets; i++) {
      const x = PAD_L + i * cellW;
      const intensity = buckets[i] / maxBucket;

      if (buckets[i] === 0) {
        // Gap — dark with subtle indicator
        ctx.fillStyle = '#0d1117';
        ctx.fillRect(x, barY, Math.max(1, cellW - 0.5), barH);
        // Red tick mark for gap
        ctx.fillStyle = 'rgba(231, 76, 60, 0.5)';
        ctx.fillRect(x, barY + barH - 2, Math.max(1, cellW - 0.5), 2);
      } else {
        // Green gradient: low = dark green, high = bright green
        const g = Math.round(100 + intensity * 155);
        const r = Math.round(20 + intensity * 20);
        const a = 0.3 + intensity * 0.7;
        ctx.fillStyle = `rgba(${r}, ${g}, 80, ${a})`;
        ctx.fillRect(x, barY, Math.max(1, cellW - 0.5), barH);
      }
    }

    // Border
    ctx.strokeStyle = 'var(--border, #1e2d3d)';
    ctx.lineWidth = 0.5;
    ctx.strokeRect(PAD_L, barY, plotW, barH);

    // Time labels
    ctx.fillStyle = '#4a5f74';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    const nLabels = Math.min(8, nBuckets);
    for (let i = 0; i < nLabels; i++) {
      const bi = Math.floor(i * (nBuckets - 1) / (nLabels - 1));
      const t = bucketStart + bi * BUCKET_MS;
      const x = PAD_L + (bi + 0.5) * cellW;
      const d = new Date(t);
      ctx.fillText(`${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`, x, H - 6);
    }

    // Label + legend
    ctx.textAlign = 'left';
    ctx.fillStyle = '#4a5f74';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText('5-min buckets', PAD_L + 4, PAD_T - 6);

    // Count gaps
    const gaps = buckets.filter(b => b === 0).length;
    if (gaps > 0) {
      ctx.textAlign = 'right';
      ctx.fillStyle = 'rgba(231, 76, 60, 0.8)';
      ctx.fillText(`${gaps} gap${gaps > 1 ? 's' : ''}`, W - PAD_R, PAD_T - 6);
    }

    // Legend bar
    const legendW = 60, legendH = 6, legendX = PAD_L + 100, legendY = PAD_T - 11;
    for (let i = 0; i < legendW; i++) {
      const pct = i / legendW;
      const g = Math.round(100 + pct * 155);
      ctx.fillStyle = `rgba(30, ${g}, 80, ${0.3 + pct * 0.7})`;
      ctx.fillRect(legendX + i, legendY, 1, legendH);
    }
    ctx.fillStyle = '#4a5f74';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText('0', legendX - 8, legendY + 5);
    ctx.textAlign = 'left';
    ctx.fillText(maxBucket, legendX + legendW + 3, legendY + 5);
  }

  // ─── Shared grid helper ───────────────────────────────────────
  function drawGrid(ctx, W, H, PAD_L, PAD_R, PAD_T, PAD_B, plotH, maxVal, points, tMin, tRange, plotW) {
    ctx.strokeStyle = '#1e2d3d';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
      const y = PAD_T + (plotH / 4) * i;
      ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
    }

    ctx.fillStyle = '#4a5f74';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const val = Math.round(maxVal * (1 - i / 4));
      ctx.fillText(val, PAD_L - 6, PAD_T + (plotH / 4) * i + 3);
    }

    ctx.textAlign = 'center';
    const nLabels = Math.min(6, points.length);
    for (let i = 0; i < nLabels; i++) {
      const idx = Math.floor((points.length - 1) * i / (nLabels - 1));
      const x = PAD_L + ((points[idx].t - tMin) / tRange) * plotW;
      const d = new Date(points[idx].t);
      ctx.fillText(`${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`, x, H - 4);
    }
  }

  // ─── Effects ──────────────────────────────────────────────────
  useEffect(() => {
    fetchAndDraw();
    const id = setInterval(fetchAndDraw, 10000);
    return () => clearInterval(id);
  }, [fetchAndDraw]);

  useEffect(() => {
    draw(dataRef.current);
  }, [view, draw]);

  useEffect(() => {
    const obs = new ResizeObserver(() => draw(dataRef.current));
    if (canvasRef.current) obs.observe(canvasRef.current.parentElement);
    return () => obs.disconnect();
  }, [draw]);

  return (
    <div className="rate-graph-container">
      <div className="graph-tabs">
        {VIEWS.map(v => (
          <button key={v.id} className={`graph-tab ${view === v.id ? 'active' : ''}`}
            onClick={() => setView(v.id)}>{v.label}</button>
        ))}
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} />
    </div>
  );
});
