/** Charts of a session (SVG drawn from numbers): over time, histograms, verdicts per time bucket,
 *  and a table per grid square. The report the server writes holds the same charts. */

import { useMemo } from "react";
import type { LiveItem, QualityRules } from "../api/types";
import { hasRules } from "./store";
import { histogram, MEASURES, measureOf, squareRows, summary, targetDefocus, timeSeries, verdictBins, type MeasureKey, type TimePoint } from "./stats";

const W = 600;
const WIDE = 1200; // the chart across the view: as wide, so that its text is the size of the others'
const H = 170;
const PAD = { left: 48, right: 10, top: 10, bottom: 24 };

interface Ticks {
  values: number[];
  /** Decimals that tell the ticks apart. */
  digits: number;
}

function niceTicks(low: number, high: number, count = 4): Ticks {
  if (!(high > low)) return { values: [low], digits: 2 };
  const raw = (high - low) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const mantissa = [1, 2, 2.5, 5, 10].find((m) => m * magnitude >= raw) ?? 10;
  const step = mantissa * magnitude;
  const values: number[] = [];
  for (let v = Math.ceil(low / step) * step; v <= high + step * 1e-9 && values.length < 10; v += step) values.push(Number(v.toFixed(10)));
  // enough decimals for the step (0.25 needs two, 0.005 three), none for whole steps
  const digits = Math.max(0, Math.min(6, -Math.floor(Math.log10(magnitude * (mantissa === 10 ? 10 : 1)) + 1e-9) + (mantissa === 2.5 ? 1 : 0)));
  return { values, digits };
}

function padded(values: number[], share = 0.05): [number, number] {
  let low = Infinity;
  let high = -Infinity;
  for (const v of values) {
    if (v < low) low = v;
    if (v > high) high = v;
  }
  if (!Number.isFinite(low)) return [0, 1];
  const margin = (high - low) * share || Math.abs(low) * 0.1 || 1;
  return [low - margin, high + margin];
}

const label = (v: number) => (Math.abs(v) >= 100 || Number.isInteger(v) ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2)).replace(".", ",");
const tick = (v: number, digits: number) => v.toFixed(digits).replace(".", ",");

function clock(t: number, days: boolean): string {
  const d = new Date(t * 1000);
  const time = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  return days ? `${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })} ${time}` : time;
}

function timeTicks(low: number, high: number): { t: number; text: string }[] {
  const span = Math.max(high - low, 1);
  const step = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400].find((s) => span / s <= 6) ?? 86400;
  const days = new Date(low * 1000).toDateString() !== new Date(high * 1000).toDateString();
  const out: { t: number; text: string }[] = [];
  for (let t = Math.ceil(low / step) * step; t <= high && out.length < 10; t += step) out.push({ t, text: clock(t, days) });
  return out;
}

interface Scale {
  x: (v: number) => number;
  y: (v: number) => number;
  w: number;
  h: number;
}

function scale([x0, x1]: [number, number], [y0, y1]: [number, number], w = W, h = H): Scale {
  return {
    x: (v) => PAD.left + ((v - x0) / (x1 - x0 || 1)) * (w - PAD.left - PAD.right),
    y: (v) => h - PAD.bottom - ((v - y0) / (y1 - y0 || 1)) * (h - PAD.top - PAD.bottom),
    w,
    h,
  };
}

function Axes({ s, xRange, yRange, time }: { s: Scale; xRange: [number, number]; yRange: [number, number]; time: boolean }) {
  const xTicks = niceTicks(xRange[0], xRange[1], 5);
  const xs = time ? timeTicks(xRange[0], xRange[1]) : xTicks.values.map((t) => ({ t, text: tick(t, xTicks.digits) }));
  const yTicks = niceTicks(yRange[0], yRange[1]);
  return (
    <g className="axes">
      {yTicks.values.map((t) => (
        <g key={`y${t}`}>
          <line x1={PAD.left} x2={s.w - PAD.right} y1={s.y(t)} y2={s.y(t)} className="grid-line" />
          <text x={PAD.left - 4} y={s.y(t) + 3} textAnchor="end">{tick(t, yTicks.digits)}</text>
        </g>
      ))}
      {xs.map(({ t, text }) => (
        <text key={`x${t}`} x={s.x(t)} y={s.h - PAD.bottom + 14} textAnchor="middle">{text}</text>
      ))}
      <line x1={PAD.left} x2={s.w - PAD.right} y1={s.h - PAD.bottom} y2={s.h - PAD.bottom} className="axis-line" />
    </g>
  );
}

/** A measure over time, one dot per image, coloured by its verdict; `extra`: grey dots behind. */
export function TimeChart({ title, unit, points, extra = [], testid }: { title: string; unit: string; points: TimePoint[]; extra?: TimePoint[]; testid: string }) {
  if (!points.length) return null;
  const all = [...points, ...extra];
  const xRange = padded(all.map((p) => p.t), 0.01);
  const yRange = padded(all.map((p) => p.v));
  const s = scale(xRange, yRange);
  const r = points.length > 400 ? 1.6 : 2.4;
  return (
    <figure className="chart" data-testid={testid} data-points={points.length}>
      <figcaption>{title}{unit ? ` (${unit})` : ""}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
        <Axes s={s} xRange={xRange} yRange={yRange} time />
        {extra.map((p, k) => <circle key={`e${k}`} cx={s.x(p.t)} cy={s.y(p.v)} r={r + 0.6} className="pt target" />)}
        {points.map((p, k) => <circle key={k} cx={s.x(p.t)} cy={s.y(p.v)} r={r} className={`pt ${p.verdict ?? "unjudged"}`} />)}
      </svg>
    </figure>
  );
}

export function HistogramChart({ title, unit, values, testid }: { title: string; unit: string; values: number[]; testid: string }) {
  const h = useMemo(() => histogram(values), [values]);
  const stats = useMemo(() => summary(values), [values]);
  if (!h || !stats) return null;
  const flat = h.edges.length < 2 || !(h.edges[h.edges.length - 1]! > h.edges[0]!);
  const xRange: [number, number] = flat ? [h.edges[0]! - 1, h.edges[0]! + 1] : [h.edges[0]!, h.edges[h.edges.length - 1]!];
  const yRange: [number, number] = [0, Math.max(...h.counts) * 1.1 || 1];
  const s = scale(xRange, yRange);
  const outside = [h.below ? `${h.below} en dessous` : "", h.above ? `${h.above} au-dessus` : ""].filter(Boolean).join(", ");
  return (
    <figure className="chart small" data-testid={testid} data-n={stats.n}>
      <figcaption>
        {title}{unit ? ` (${unit})` : ""} · médiane {label(stats.median)}, {stats.n} image{stats.n > 1 ? "s" : ""}
        {outside ? ` · hors du graphique : ${outside}` : ""}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
        <Axes s={s} xRange={xRange} yRange={yRange} time={false} />
        {flat ? (
          <rect x={s.x(h.edges[0]!) - 4} y={s.y(h.counts[0]!)} width={8} height={s.y(0) - s.y(h.counts[0]!)} className="bar" />
        ) : (
          h.counts.map((c, k) =>
            c ? <rect key={k} x={s.x(h.edges[k]!) + 0.5} y={s.y(c)} width={Math.max(0.5, s.x(h.edges[k + 1]!) - s.x(h.edges[k]!) - 1)} height={s.y(0) - s.y(c)} className="bar" /> : null,
          )
        )}
      </svg>
    </figure>
  );
}

function VerdictBars({ items, rules, reference }: { items: LiveItem[]; rules: QualityRules; reference: number | null }) {
  const data = useMemo(() => verdictBins(items, rules, reference), [items, rules, reference]);
  if (!data) return null;
  const xRange: [number, number] = [data.start, data.start + data.bucket * data.bins.length];
  const yRange: [number, number] = [0, Math.max(1, ...data.bins.map((b) => b[0])) * 1.1];
  const s = scale(xRange, yRange, WIDE, H);
  const width = Math.max(0.6, s.x(data.start + data.bucket) - s.x(data.start) - 0.8);
  const minutes = data.bucket / 60;
  return (
    <figure className="chart wide" data-testid="chart-verdicts">
      <figcaption>Images par tranche de {minutes >= 60 ? `${minutes / 60} h` : `${minutes} min`}{hasRules(rules) ? ", acceptées et rejetées" : ""}</figcaption>
      <svg viewBox={`0 0 ${WIDE} ${H}`} role="img" aria-label="Images dans le temps">
        <Axes s={s} xRange={xRange} yRange={yRange} time />
        {data.bins.map(([total, accepted, rejected], k) => {
          const x = s.x(data.start + k * data.bucket) + 0.4;
          const parts: [number, string][] = [[accepted, "accepted"], [rejected, "rejected"], [total - accepted - rejected, "unjudged"]];
          let base = 0;
          return (
            <g key={k}>
              {parts.map(([n, cls]) => {
                if (n <= 0) return null;
                const rect = <rect key={cls} x={x} y={s.y(base + n)} width={width} height={s.y(base) - s.y(base + n)} className={`bar ${cls}`} />;
                base += n;
                return rect;
              })}
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

const HISTOGRAMS: { key: MeasureKey; label: string; unit: string }[] = [
  { key: "ctf_fit", label: MEASURES.ctf_fit.label, unit: "Å" },
  { key: "defocus", label: MEASURES.defocus.label, unit: "µm" },
  { key: "ice", label: "Glace relative", unit: "ln I₀/I" },
  { key: "motion", label: MEASURES.motion.label, unit: "Å" },
  { key: "astigmatism", label: MEASURES.astigmatism.label, unit: "µm" },
  { key: "ice_ring", label: MEASURES.ice_ring.label, unit: "" },
];

/** Every chart of a session, for the images shown (filters apply). */
export function StatsView({ items, rules, reference }: { items: LiveItem[]; rules: QualityRules; reference: number | null }) {
  const defocus = useMemo(() => timeSeries(items, (i) => measureOf(i, "defocus", reference), rules, reference), [items, rules, reference]);
  const target = useMemo(() => timeSeries(items, targetDefocus, rules, reference), [items, rules, reference]);
  const fit = useMemo(() => timeSeries(items, (i) => measureOf(i, "ctf_fit", reference), rules, reference), [items, rules, reference]);
  const ice = useMemo(() => timeSeries(items, (i) => measureOf(i, "ice", reference), rules, reference), [items, rules, reference]);
  const values = useMemo(
    () =>
      Object.fromEntries(
        HISTOGRAMS.map(({ key }) => [key, items.filter((i) => i.state === "complete").map((i) => measureOf(i, key, reference)).filter((v): v is number => v !== null)]),
      ) as Record<MeasureKey, number[]>,
    [items, reference],
  );
  const squares = useMemo(() => squareRows(items, rules, reference), [items, rules, reference]);
  const judged = hasRules(rules);
  const empty = !defocus.length && !fit.length && !ice.length && HISTOGRAMS.every(({ key }) => !values[key].length);
  return (
    <div className="stats-view" data-testid="stats-view">
      <div className="legend">
        {judged ? (
          <>
            <span><i className="accepted" />acceptée</span>
            <span><i className="rejected" />rejetée</span>
            <span><i className="unjudged" />non jugée</span>
          </>
        ) : (
          <span><i className="unjudged" />image (aucun seuil réglé)</span>
        )}
        {target.length > 0 && <span><i className="target" />défocus visé</span>}
      </div>
      <VerdictBars items={items} rules={rules} reference={reference} />
      {empty && <div className="hint">Aucune image mesurée pour l'instant : les graphiques viendront avec les mesures.</div>}
      <div className="charts">
        <TimeChart title="Défocus mesuré" unit="µm" points={defocus} extra={target} testid="chart-defocus" />
        <TimeChart title="Résolution du CTF" unit="Å" points={fit} testid="chart-fit" />
        <TimeChart title="Glace relative" unit="ln I₀/I" points={ice} testid="chart-ice" />
        {HISTOGRAMS.map(({ key, label: title, unit }) => (
          <HistogramChart key={key} title={title} unit={unit} values={values[key]} testid={`hist-${key}`} />
        ))}
      </div>
      {squares.length > 0 && (
        <table className="squares" data-testid="squares">
          <thead>
            <tr><th>Carré de grille</th><th>Images</th>{judged && <th>Acceptées</th>}<th>CTF médian (Å)</th><th>Glace médiane</th></tr>
          </thead>
          <tbody>
            {squares.slice(0, 60).map((sq) => (
              <tr key={sq.name}>
                <td className="mono">{sq.name}</td>
                <td>{sq.images}</td>
                {judged && <td>{sq.accepted}/{sq.accepted + sq.rejected}</td>}
                <td>{sq.medianFit === null ? "—" : label(sq.medianFit)}</td>
                <td>{sq.medianIce === null ? "—" : label(sq.medianIce)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
