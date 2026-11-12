/** Statistics of the images of a session, for the charts of the Live view (pure functions).
 *  The report the server writes holds the same, computed the same way (live/report.py). */

import type { LiveItem, QualityRules } from "../api/types";
import { ctfOf, driftOf, relativeIce, verdict, type Verdict } from "./store";

export interface TimePoint {
  t: number;
  v: number;
  verdict: Verdict | null;
}

/** The defocus asked for (µm, positive), from EPU or SerialEM. */
export function targetDefocus(item: LiveItem): number | null {
  const d = item.meta.epu?.applied_defocus ?? item.meta.mdoc?.target_defocus;
  return typeof d === "number" && Number.isFinite(d) ? Math.abs(d) : null;
}

/** What the charts can show of an image, each a number or null. */
export const MEASURES = {
  ctf_fit: { label: "Résolution du CTF", unit: "Å", get: (item: LiveItem) => ctfOf(item)?.fit ?? null },
  defocus: { label: "Défocus mesuré", unit: "µm", get: (item: LiveItem) => ctfOf(item)?.defocus ?? null },
  astigmatism: { label: "Astigmatisme", unit: "µm", get: (item: LiveItem) => ctfOf(item)?.astigmatism ?? null },
  motion: {
    label: "Mouvement",
    unit: "Å",
    get: (item: LiveItem) => {
      const d = driftOf(item);
      return d && d.unit === "A" ? d.value : null;
    },
  },
  ice_ring: {
    label: "Anneau de glace",
    unit: "",
    get: (item: LiveItem) => (typeof item.metrics?.ice_ring === "number" ? item.metrics.ice_ring : null),
  },
} as const;

export type MeasureKey = keyof typeof MEASURES | "ice";

/** The value of a measure for an image; the ice needs the reference of the session. */
export function measureOf(item: LiveItem, key: MeasureKey, reference: number | null): number | null {
  if (key === "ice") return relativeIce(item, reference);
  const v = MEASURES[key].get(item);
  return v !== null && Number.isFinite(v) ? v : null;
}

/** Complete images with a time and the value, in acquisition order, with their verdict. */
export function timeSeries(
  items: LiveItem[],
  get: (item: LiveItem) => number | null,
  rules: QualityRules,
  reference: number | null,
): TimePoint[] {
  const out: TimePoint[] = [];
  for (const item of items) {
    if (item.state !== "complete" || item.time === null) continue;
    const v = get(item);
    if (v === null || !Number.isFinite(v)) continue;
    out.push({ t: item.time, v, verdict: verdict(item, rules, reference) });
  }
  return out.sort((a, b) => a.t - b.t);
}

/** Linear interpolation between ranks (numpy's default, as the report does). */
export function percentile(sorted: number[], q: number): number {
  if (sorted.length === 0) return NaN;
  const position = q * (sorted.length - 1);
  const low = Math.floor(position);
  const high = Math.min(low + 1, sorted.length - 1);
  return sorted[low]! + (sorted[high]! - sorted[low]!) * (position - low);
}

export interface Histogram {
  edges: number[];
  counts: number[];
  /** Values outside the 1st to 99th percentile, counted apart: one stray image does not
   *  squash the rest. */
  below: number;
  above: number;
}

export function histogram(values: number[], bins = 20): Histogram | null {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const low = percentile(sorted, 0.01);
  const high = percentile(sorted, 0.99);
  if (!(high > low)) return { edges: [low, low], counts: [sorted.length], below: 0, above: 0 };
  const width = (high - low) / bins;
  const counts = new Array<number>(bins).fill(0);
  let below = 0;
  let above = 0;
  for (const v of sorted) {
    if (v < low) below += 1;
    else if (v > high) above += 1;
    else counts[Math.min(bins - 1, Math.floor((v - low) / width))]! += 1;
  }
  return { edges: Array.from({ length: bins + 1 }, (_, k) => low + k * width), counts, below, above };
}

export function summary(values: number[]): { n: number; median: number; p10: number; p90: number } | null {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  return { n: sorted.length, median: percentile(sorted, 0.5), p10: percentile(sorted, 0.1), p90: percentile(sorted, 0.9) };
}

const BUCKETS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400];

/** Images per time bucket (at most `maxBins` of them), with how many were accepted and
 *  rejected: [total, accepted, rejected]. */
export function verdictBins(
  items: LiveItem[],
  rules: QualityRules,
  reference: number | null,
  maxBins = 96,
): { start: number; bucket: number; bins: [number, number, number][] } | null {
  const timed = items.filter((i) => i.state === "complete" && i.time !== null);
  if (!timed.length) return null;
  const times = timed.map((i) => i.time!);
  const first = Math.min(...times);
  const last = Math.max(...times);
  const span = Math.max(last - first, 1);
  const bucket = BUCKETS.find((b) => span / b <= maxBins) ?? BUCKETS[BUCKETS.length - 1]!;
  const start = Math.floor(first / bucket) * bucket;
  const bins = Array.from({ length: Math.floor((last - start) / bucket) + 1 }, () => [0, 0, 0] as [number, number, number]);
  for (const item of timed) {
    const row = bins[Math.floor((item.time! - start) / bucket)]!;
    row[0] += 1;
    const v = verdict(item, rules, reference);
    if (v === "accepted") row[1] += 1;
    else if (v === "rejected") row[2] += 1;
  }
  return { start, bucket, bins };
}

export interface SquareRow {
  name: string;
  images: number;
  accepted: number;
  rejected: number;
  medianFit: number | null;
  medianIce: number | null;
  first: number | null;
}

/** The images of each grid square (from the "GridSquare_<id>" directory EPU writes them in). */
export function squareRows(items: LiveItem[], rules: QualityRules, reference: number | null): SquareRow[] {
  const groups = new Map<string, LiveItem[]>();
  for (const item of items) {
    if (item.state !== "complete") continue;
    const name = item.dir.split("/").reverse().find((part) => /^GridSquare_\d+$/.test(part));
    if (!name) continue;
    const group = groups.get(name);
    if (group) group.push(item);
    else groups.set(name, [item]);
  }
  const median = (values: number[]) => summary(values)?.median ?? null;
  return [...groups.entries()]
    .map(([name, members]) => {
      let accepted = 0;
      let rejected = 0;
      for (const item of members) {
        const v = verdict(item, rules, reference);
        if (v === "accepted") accepted += 1;
        else if (v === "rejected") rejected += 1;
      }
      const times = members.map((i) => i.time).filter((t): t is number => t !== null);
      return {
        name,
        images: members.length,
        accepted,
        rejected,
        medianFit: median(members.map((i) => ctfOf(i)?.fit ?? NaN)),
        medianIce: median(members.map((i) => relativeIce(i, reference) ?? NaN)),
        first: times.length ? Math.min(...times) : null,
      };
    })
    .sort((a, b) => (a.first ?? Infinity) - (b.first ?? Infinity) || a.name.localeCompare(b.name));
}
