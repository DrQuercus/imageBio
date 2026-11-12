import { useEffect, useRef } from "react";
import type { PlaneStats } from "../api/types";
import type { DisplaySettings } from "../state/url";
import { LUT_LABELS } from "../viewer/luts";

interface Props {
  stats: PlaneStats | null;
  display: DisplaySettings;
  rgb: boolean;
  customLut: boolean;
  onChange: (patch: Partial<DisplaySettings>) => void;
  onLutFile: (file: File) => void;
  /** The statistics are those of the overview, reduced this many times (a filter is on). */
  reducedBy?: number;
}

const STEPS = 1000;

export function formatValue(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(3);
  return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6)));
}

/** Contrast window, gamma, colour table and the histogram of the plane. Everything here is
 *  applied on the GPU: no request is made when a control moves. */
export function ContrastPanel({ stats, display, rgb, customLut, onChange, onLutFile, reducedBy = 1 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // the sliders cover the robust range, widened to the window when it lies outside
  const lo = Math.min(stats?.histogram.lo ?? display.min, display.min);
  const hi = Math.max(stats?.histogram.hi ?? display.max, display.max);
  const span = hi - lo || 1;
  const toSlider = (v: number) => Math.round(((v - lo) / span) * STEPS);
  const fromSlider = (s: number) => lo + (s / STEPS) * span;

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    ctx.fillStyle = "#05080c";
    ctx.fillRect(0, 0, w, h);
    if (!stats) return;
    const { counts, lo: hLo, hi: hHi } = stats.histogram;
    const x = (v: number) => ((v - hLo) / (hHi - hLo || 1)) * w;
    ctx.fillStyle = "rgba(0, 200, 255, 0.10)";
    ctx.fillRect(x(display.min), 0, x(display.max) - x(display.min), h);
    const peak = Math.log1p(Math.max(1, ...counts));
    const bar = w / counts.length;
    counts.forEach((count, i) => {
      const value = hLo + ((i + 0.5) / counts.length) * (hHi - hLo);
      const height = (Math.log1p(count) / peak) * (h - 2); // log scale: tails stay visible
      ctx.fillStyle = value >= display.min && value <= display.max ? "#19c8ff" : "#2a3f55";
      ctx.fillRect(i * bar, h - height, Math.max(1, bar), height);
    });
    ctx.strokeStyle = "#19c8ff";
    ctx.setLineDash([3, 3]);
    for (const v of [display.min, display.max]) {
      ctx.beginPath();
      ctx.moveTo(x(v), 0);
      ctx.lineTo(x(v), h);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }, [stats, display.min, display.max]);

  const setMin = (v: number) => onChange({ min: Math.min(v, display.max - span / STEPS), auto: false });
  const setMax = (v: number) => onChange({ max: Math.max(v, display.min + span / STEPS), auto: false });

  return (
    <section className="panel" data-testid="contrast">
      <div className="panel-title">Contraste</div>
      <canvas ref={canvasRef} className="histogram" width={256} height={72} data-testid="histogram" />
      <div className="hist-range">
        <span>{stats ? formatValue(stats.histogram.lo) : "—"}</span>
        <span>{stats ? formatValue(stats.histogram.hi) : "—"}</span>
      </div>
      {reducedBy > 1 && (
        <div className="note" data-testid="stats-level" title="Pour suivre un filtre qui bouge, l'histogramme et la fenêtre automatique viennent de la vue d'ensemble filtrée">
          Histogramme de la vue réduite {reducedBy}×
        </div>
      )}
      <label className="slider">
        <span>Noir</span>
        <input type="range" min={0} max={STEPS} value={toSlider(display.min)} data-testid="min-slider"
          onChange={(e) => setMin(fromSlider(Number(e.target.value)))} />
      </label>
      <label className="slider">
        <span>Blanc</span>
        <input type="range" min={0} max={STEPS} value={toSlider(display.max)} data-testid="max-slider"
          onChange={(e) => setMax(fromSlider(Number(e.target.value)))} />
      </label>
      <div className="pair">
        <input type="number" step="any" value={formatValue(display.min)} aria-label="Valeur du noir" data-testid="min-input"
          onChange={(e) => Number.isFinite(e.target.valueAsNumber) && setMin(e.target.valueAsNumber)} />
        <input type="number" step="any" value={formatValue(display.max)} aria-label="Valeur du blanc" data-testid="max-input"
          onChange={(e) => Number.isFinite(e.target.valueAsNumber) && setMax(e.target.valueAsNumber)} />
      </div>
      <label className="slider">
        <span>Gamma {display.gamma.toFixed(2)}</span>
        <input type="range" min={20} max={400} value={Math.round(display.gamma * 100)} data-testid="gamma-slider"
          onChange={(e) => onChange({ gamma: Number(e.target.value) / 100 })} />
      </label>
      <div className="buttons">
        <button className={display.auto ? "on" : ""} data-testid="auto" title="Fenêtre automatique (A)"
          onClick={() => onChange({ auto: true })}>Auto</button>
        <button title="Toute la plage robuste" disabled={!stats}
          onClick={() => stats && onChange({ min: stats.robust[0], max: stats.robust[1], gamma: 1, auto: false })}>Plage</button>
        <button className={display.invert ? "on" : ""} data-testid="invert" title="Inverser (I)"
          onClick={() => onChange({ invert: !display.invert })}>Inverser</button>
      </div>
      {!rgb && (
        <div className="lut">
          <select value={customLut ? "custom" : display.lut} aria-label="Palette" data-testid="lut"
            onChange={(e) => e.target.value !== "custom" && onChange({ lut: e.target.value })}>
            {Object.entries(LUT_LABELS).map(([key, label]) => (
              <option key={key} value={key}>{label}</option>
            ))}
            {customLut && <option value="custom">Fichier .lut</option>}
          </select>
          <label className="file-button" title="Importer une palette ImageJ (.lut)">
            .lut
            <input type="file" accept=".lut,.txt" hidden
              onChange={(e) => { const f = e.target.files?.[0]; if (f) onLutFile(f); e.target.value = ""; }} />
          </label>
        </div>
      )}
    </section>
  );
}
