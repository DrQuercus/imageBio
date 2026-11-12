/** Intensity along the ruler, read from the pixels the page already holds: pure functions. */

import { type PlaneSet, valuesAt } from "./planes";

export interface Segment {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface Profile {
  /** Length of the segment, in image pixels. */
  length: number;
  /** Distance of each sample from the start, in image pixels. */
  at: number[];
  /** One list per channel; NaN where a sample falls outside the pixels held. */
  values: number[][];
  /** Bin of the pixels read: 1 at full resolution, N for means of N x N pixels. */
  bin: number;
}

export const MAX_SAMPLES = 2000;

/** Values along `segment`, one sample per pixel of `set` (nearest pixel), both ends
 *  included. Null for a segment of no length. */
export function lineProfile(set: PlaneSet, segment: Segment, maxSamples = MAX_SAMPLES): Profile | null {
  const dx = segment.x1 - segment.x0;
  const dy = segment.y1 - segment.y0;
  const length = Math.hypot(dx, dy);
  if (!(length > 0) || !Number.isFinite(length)) return null;
  const n = Math.max(2, Math.min(maxSamples, Math.floor(length / set.bin) + 1));
  const channels = set.planes.length;
  const values: number[][] = Array.from({ length: channels }, () => new Array<number>(n));
  const at = new Array<number>(n);
  for (let k = 0; k < n; k++) {
    const f = k / (n - 1);
    at[k] = f * length;
    const v = valuesAt(set, Math.floor(segment.x0 + f * dx), Math.floor(segment.y0 + f * dy));
    for (let c = 0; c < channels; c++) values[c]![k] = v ? (v[c] ?? Number.NaN) : Number.NaN;
  }
  return { length, at, values, bin: set.bin };
}

/** Whether both ends of `segment` lie in the region `set` holds. */
export function setCovers(set: PlaneSet, segment: Segment): boolean {
  const [x, y, w, h] = set.region;
  const inside = (px: number, py: number) => px >= x && py >= y && px < x + w && py < y + h;
  return inside(segment.x0, segment.y0) && inside(segment.x1, segment.y1);
}

/** Smallest and largest finite values of a profile, or null when it holds none. */
export function profileRange(profile: Profile): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const channel of profile.values) {
    for (const v of channel) {
      if (Number.isFinite(v)) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
  }
  return lo <= hi ? [lo, hi] : null;
}

/** The profile as tab-separated text (distance in Å when the pixel size is known, else in
 *  pixels; one column per channel), to paste into a spreadsheet. */
export function profileText(profile: Profile, apix: number | null): string {
  const unit = apix ? "distance_A" : "distance_px";
  const names = profile.values.length === 1 ? ["value"] : profile.values.map((_, c) => ["red", "green", "blue"][c] ?? `c${c}`);
  const rows = [[unit, ...names].join("\t")];
  profile.at.forEach((d, k) => {
    const distance = apix ? d * apix : d;
    rows.push([Number(distance.toPrecision(6)), ...profile.values.map((channel) => channel[k])].join("\t"));
  });
  return `${rows.join("\n")}\n`;
}
