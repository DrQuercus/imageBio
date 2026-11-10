/** Cryo mode, pure logic: what is asked of the server (frame average, resolution filters) and
 *  the geometry of a power spectrum (where a resolution lies, which rings to draw). */

import type { DirEntry, Processing } from "../api/types";

export interface CryoSettings {
  /** Low-pass cut-off in Å (detail finer than this is removed), or null. */
  lowpass: number | null;
  /** High-pass cut-off in Å (variations coarser than this are removed), or null. */
  highpass: number | null;
  /** Number of consecutive frames averaged, starting at the current one. 1: no average. */
  average: number;
  /** Show the power spectrum next to the image. */
  spectrum: boolean;
  /** Gain reference applied to the frames (logical path), or null. */
  gain: string | null;
  /** Orientation the gain file is stored in: 0 to 7, see ORIENTATION_LABELS. */
  gainOrient: number;
  /** The gain file is a flat-field counts image, to be inverted. */
  gainInvert: boolean;
}

export const NO_CRYO: CryoSettings = {
  lowpass: null,
  highpass: null,
  average: 1,
  spectrum: false,
  gain: null,
  gainOrient: 0,
  gainInvert: false,
};

/** The eight orientations of a gain reference, in the order of the server (cryo/gain.py). */
export const ORIENTATION_LABELS = [
  "telle quelle",
  "rotation de 90°",
  "rotation de 180°",
  "rotation de 270°",
  "miroir gauche-droite",
  "transposée",
  "miroir haut-bas",
  "anti-transposée",
];

/** Low-pass cut-offs offered as buttons, in Å. */
export const LOWPASS_PRESETS = [20, 10, 5];

/** The diffuse ring of vitreous ice, in Å. */
export const ICE_RING = 3.7;

/** Last frame (inclusive) of the average that starts at frame `t`, or null when a single
 *  frame is shown. The window is cut at the end of the movie. */
export function averageEnd(t: number, average: number, sizeT: number): number | null {
  if (!(average > 1) || sizeT < 2) return null;
  const end = Math.min(t + Math.floor(average) - 1, sizeT - 1);
  return end > t ? end : null;
}

/** Why the filters asked for cannot be applied, as a message for the user, or null. */
export function filterProblem(cryo: CryoSettings, apix: number | null): string | null {
  if (cryo.lowpass === null && cryo.highpass === null) return null;
  if (!apix) return "Taille de pixel inconnue : saisissez-la (panneau Fichier) pour filtrer en Å.";
  if (cryo.lowpass !== null && cryo.highpass !== null && cryo.lowpass >= cryo.highpass) {
    return "Le passe-bas doit être plus fin (plus petit en Å) que le passe-haut.";
  }
  return null;
}

/** The processing to request for frame `t`.
 *
 *  `apix` is the pixel size in effect and `apixFromFile` tells whether the server already knows
 *  it: a value typed by the user has to be sent along. Filters that cannot be applied
 *  (see filterProblem) are left out rather than sent to fail. */
export function processingOf(
  cryo: CryoSettings,
  t: number,
  sizeT: number,
  apix: number | null,
  apixFromFile: boolean,
): Processing {
  const proc: Processing = {};
  const end = averageEnd(t, cryo.average, sizeT);
  if (end !== null) proc.t_end = end;
  if (filterProblem(cryo, apix) === null && apix) {
    if (cryo.lowpass !== null) proc.lp = cryo.lowpass;
    if (cryo.highpass !== null) proc.hp = cryo.highpass;
    if ((proc.lp !== undefined || proc.hp !== undefined) && !apixFromFile) proc.apix = apix;
  }
  Object.assign(proc, gainOf(cryo));
  return proc;
}

/** The gain part of a request: nothing without a gain, defaults left out. */
export function gainOf(cryo: CryoSettings): Pick<Processing, "gain" | "gain_orient" | "gain_inv"> {
  if (!cryo.gain) return {};
  const gain: Pick<Processing, "gain" | "gain_orient" | "gain_inv"> = { gain: cryo.gain };
  if (cryo.gainOrient) gain.gain_orient = cryo.gainOrient;
  if (cryo.gainInvert) gain.gain_inv = true;
  return gain;
}

/** Identifies a processing in cache keys. */
export function processingKey(proc: Processing): string {
  const gain = proc.gain ? `${proc.gain_orient ?? 0}${proc.gain_inv ? "i" : ""}:${proc.gain}` : "";
  return `${proc.t_end ?? ""};${proc.lp ?? ""};${proc.hp ?? ""};${proc.apix ?? ""};${gain}`;
}

/** Directory of a logical path ("root/a/b.eer" gives "root/a"). */
export function dirname(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut < 0 ? "" : path.slice(0, cut);
}

const extension = (path: string) => /\.[^./]+$/.exec(path)?.[0].toLowerCase() ?? "";

/** Whether a gain chosen for `previous` may be kept for `next`: only for a file of the same
 *  kind in the same directory. Applied to an already corrected image, it would correct twice. */
export function keepsGain(previous: string | null, next: string): boolean {
  return previous !== null && dirname(previous) === dirname(next) && extension(previous) === extension(next);
}

/** The files of a listing that look like gain references: "gain" in the name (.gain,
 *  *_gain.tiff, *-gain-ref.MRC) and a format that can be read. */
export function gainCandidates(entries: DirEntry[], except?: string): DirEntry[] {
  return entries.filter((e) => e.kind === "file" && e.supported && e.path !== except && /gain/i.test(e.name));
}

// ── power spectrum ───────────────────────────────────────────────────────────

/** Distance from the centre, in spectrum pixels, of a period of `d` Å. */
export function ringRadius(side: number, apix: number, d: number): number {
  return (side * apix) / d;
}

/** Resolution in Å at `radius` spectrum pixels from the centre (Infinity at the centre). */
export function resolutionAt(side: number, apix: number, radius: number): number {
  return radius > 0 ? (side * apix) / radius : Infinity;
}

export interface Ring {
  /** Resolution in Å. */
  d: number;
  /** Radius in spectrum pixels. */
  radius: number;
  ice: boolean;
}

const RING_CANDIDATES = [100, 50, 30, 20, 15, 10, 8, 6, 5, 4, 3, 2.5, 2, 1.5, 1.2, 1];

/** The resolution rings to draw on a spectrum of `side` pixels: round values inside Nyquist,
 *  far enough from each other to stay readable, and the ice ring when it is in range. */
export function spectrumRings(side: number, apix: number, minGap = side / 14): Ring[] {
  const nyquist = side / 2;
  const rings: Ring[] = [];
  const iceRadius = ringRadius(side, apix, ICE_RING);
  const ice = iceRadius <= nyquist;
  let last = minGap / 2; // nothing too close to the centre either
  for (const d of RING_CANDIDATES) {
    const radius = ringRadius(side, apix, d);
    if (radius > nyquist) break;
    if (radius - last < minGap) continue;
    if (ice && Math.abs(radius - iceRadius) < minGap) continue; // the ice ring takes this place
    rings.push({ d, radius, ice: false });
    last = radius;
  }
  if (ice) rings.push({ d: ICE_RING, radius: iceRadius, ice: true });
  return rings.sort((a, b) => a.radius - b.radius);
}

/** Value at fraction `q` (0 to 1) of an ascending array. */
function quantile(sorted: Float32Array, q: number): number {
  if (!sorted.length) return 0;
  return sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))))]!;
}

/** Display window of a power spectrum, on a log10 scale: from the bulk of the background to
 *  just under the strongest peaks. Zero or non-finite powers are ignored. */
export function spectrumWindow(power: Float32Array): [number, number] {
  const stride = Math.max(1, Math.floor(power.length / 262144));
  const logs = new Float32Array(Math.ceil(power.length / stride));
  let n = 0;
  for (let i = 0; i < power.length; i += stride) {
    const v = power[i]!;
    if (v > 0 && Number.isFinite(v)) logs[n++] = Math.log10(v);
  }
  if (!n) return [0, 1];
  const sorted = logs.subarray(0, n).sort();
  const lo = quantile(sorted, 0.02);
  const hi = quantile(sorted, 0.998);
  return hi > lo ? [lo, hi] : [lo, lo + 1];
}

/** Grey levels (RGBA bytes) of a power spectrum, log scale, for a canvas of the same size. */
export function spectrumToRgba(power: Float32Array, window: [number, number]): Uint8ClampedArray<ArrayBuffer> {
  const [lo, hi] = window;
  const scale = 255 / (hi - lo || 1);
  const out = new Uint8ClampedArray(power.length * 4);
  for (let i = 0; i < power.length; i++) {
    const v = power[i]!;
    const g = v > 0 && Number.isFinite(v) ? (Math.log10(v) - lo) * scale : 0;
    out[4 * i] = out[4 * i + 1] = out[4 * i + 2] = g; // clamped to 0..255 by the array
    out[4 * i + 3] = 255;
  }
  return out;
}

/** A resolution in Å as text: 12.4 Å, 3.70 Å. */
export function formatResolution(d: number): string {
  if (!Number.isFinite(d)) return "∞";
  return `${d >= 100 ? d.toFixed(0) : d >= 10 ? d.toFixed(1) : d.toFixed(2)} Å`;
}
