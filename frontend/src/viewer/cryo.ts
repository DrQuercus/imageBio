/** Cryo mode, pure logic: what is asked of the server (frame average, resolution filters) and
 *  the geometry of a power spectrum (where a resolution lies, which rings to draw). */

import type { DirEntry, PicksResponse, Processing } from "../api/types";

export interface CryoSettings {
  /** Low-pass cut-off in Å (detail finer than this is removed), or null. */
  lowpass: number | null;
  /** High-pass cut-off in Å (variations coarser than this are removed), or null. */
  highpass: number | null;
  /** The filters are kept but not applied (key L: the image before and after). */
  filtersOff: boolean;
  /** Sharp filter profile (Butterworth of order 4) instead of the Gaussian one. */
  sharp: boolean;
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
  /** Reduced views are made by Fourier cropping instead of means of blocks. */
  fourierBin: boolean;
  /** Coordinate file drawn over the image (logical path), or null. */
  picks: string | null;
  /** Count the rows of the coordinates from the bottom of the image instead of the top. */
  picksFlipY: boolean;
  /** Diameter of the circles in image pixels; null: the boxes of the file, or a default. */
  picksDiameter: number | null;
  /** Particles under this score are hidden; null: all are shown. */
  picksMinScore: number | null;
}

export const NO_CRYO: CryoSettings = {
  lowpass: null,
  highpass: null,
  filtersOff: false,
  sharp: false,
  average: 1,
  spectrum: false,
  gain: null,
  gainOrient: 0,
  gainInvert: false,
  fourierBin: false,
  picks: null,
  picksFlipY: false,
  picksDiameter: null,
  picksMinScore: null,
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

/** Cut-offs offered as buttons, in Å. */
export const LOWPASS_PRESETS = [30, 20, 10, 5];
export const HIGHPASS_PRESETS = [2000, 1000, 500];

/** Positions of a filter slider: the cut-off moves on a logarithmic scale. */
export const SLIDER_STEPS = 1000;

/** The range of the slider of a filter, in Å, for an image of `apix` Å per pixel (null:
 *  unknown, 1 is assumed). A low-pass finer than Nyquist removes nothing the image holds; a
 *  high-pass is of use from a few hundred Å, where ice and illumination vary. */
export function filterRange(kind: "lowpass" | "highpass", apix: number | null): [number, number] {
  const nyquist = 2 * (apix && apix > 0 ? apix : 1);
  return kind === "lowpass"
    ? [niceAngstrom(nyquist), Math.max(200, niceAngstrom(25 * nyquist))]
    : [Math.max(50, niceAngstrom(10 * nyquist)), Math.max(5000, niceAngstrom(1000 * nyquist))];
}

/** A cut-off rounded to two significant digits: 3.7, 12, 250 Å. The slider then goes through
 *  about ninety values per decade, which the caches of the server and the page can hold. */
export function niceAngstrom(d: number): number {
  return Number(d.toPrecision(2));
}

/** The next cut-off of two significant digits above (`direction` 1) or below (-1) `d`, kept
 *  in the range: what an arrow key does to a slider. One position of the slider is too small
 *  a step once values are rounded, the thumb would come back where it was. */
export function stepAngstrom(d: number, direction: 1 | -1, [lo, hi]: [number, number]): number {
  if (!(d > 0) || !Number.isFinite(d)) return direction > 0 ? lo : hi;
  // the step of the second digit, taken below d when going down (10 -> 9.9, not 9)
  const decade = Math.floor(Math.log10(direction > 0 ? d : d * (1 - 1e-9)));
  const next = niceAngstrom(d + direction * 10 ** (decade - 1));
  return Math.min(hi, Math.max(lo, next));
}

/** The bin of the statistics to ask for. While a filter slider is held, those of the overview:
 *  they follow it at once. Otherwise those of the full plane: the overview has lost the fine
 *  detail, and its window would saturate the image seen at full resolution (ADR 013). */
export function statsLevel(proc: Processing, adjusting: boolean, overviewBin: number): number {
  return adjusting && (proc.lp !== undefined || proc.hp !== undefined) ? overviewBin : 1;
}

/** The cut-off at a slider position. */
export function sliderToAngstrom(position: number, [lo, hi]: [number, number]): number {
  const f = Math.min(1, Math.max(0, position / SLIDER_STEPS));
  return Math.min(hi, Math.max(lo, niceAngstrom(lo * (hi / lo) ** f)));
}

/** The slider position of a cut-off; values outside the range sit at its ends. */
export function angstromToSlider(d: number, [lo, hi]: [number, number]): number {
  if (!(d > 0)) return 0;
  const f = Math.log(d / lo) / Math.log(hi / lo);
  return Math.round(Math.min(1, Math.max(0, f)) * SLIDER_STEPS);
}

/** The diffuse ring of vitreous ice, in Å. */
export const ICE_RING = 3.7;

/** Last frame (inclusive) of the average that starts at frame `t`, or null when a single
 *  frame is shown. The window is cut at the end of the movie. */
export function averageEnd(t: number, average: number, sizeT: number): number | null {
  if (!(average > 1) || sizeT < 2) return null;
  const end = Math.min(t + Math.floor(average) - 1, sizeT - 1);
  return end > t ? end : null;
}

/** Number of EER fractions to sum per frame for about `target` e/Å² in each frame.
 *
 *  `dosePerFraction` is in electrons per detector pixel, `apix` the size in Å of a pixel of the
 *  image as it is rendered and `sampling` the rendering factor (2 or 4 in super-resolution: a
 *  detector pixel is then `sampling` image pixels wide). Null when the dose cannot be used. */
export function groupForDose(
  dosePerFraction: number,
  apix: number,
  sampling: number,
  fractions: number,
  target = 1,
): number | null {
  const perSquareAngstrom = dosePerFraction / (apix * sampling) ** 2;
  if (!(perSquareAngstrom > 0) || !Number.isFinite(perSquareAngstrom) || !(fractions >= 1)) return null;
  return Math.min(fractions, Math.max(1, Math.round(target / perSquareAngstrom)));
}

/** Why the filters asked for cannot be applied, as a message for the user, or null. */
export function filterProblem(cryo: CryoSettings, apix: number | null): string | null {
  if ((cryo.lowpass === null && cryo.highpass === null) || cryo.filtersOff) return null;
  if (!apix) return "Taille de pixel inconnue : saisissez-la (panneau Fichier) pour filtrer en Å.";
  if (cryo.lowpass !== null && cryo.highpass !== null && cryo.lowpass >= cryo.highpass) {
    return "Le passe-bas doit être plus fin (plus petit en Å) que le passe-haut.";
  }
  return null;
}

/** The processing to request for frame `t`.
 *
 *  `apix` is the pixel size in effect and `apixFromFile` tells whether it is the file's own,
 *  which the server knows: a value typed by the user (even one that replaces the file's) has
 *  to be sent along. Filters that cannot be applied
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
  if (filterProblem(cryo, apix) === null && apix && !cryo.filtersOff) {
    if (cryo.lowpass !== null) proc.lp = cryo.lowpass;
    if (cryo.highpass !== null) proc.hp = cryo.highpass;
    if (proc.lp !== undefined || proc.hp !== undefined) {
      if (!apixFromFile) proc.apix = apix;
      if (cryo.sharp) proc.sharp = true;
    }
  }
  Object.assign(proc, gainOf(cryo));
  if (cryo.fourierBin) proc.fbin = true;
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
  return `${proc.t_end ?? ""};${proc.lp ?? ""};${proc.hp ?? ""};${proc.apix ?? ""};${gain};${proc.fbin ? "f" : ""};${proc.sharp ? "s" : ""}`;
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
export function gainCandidates(entries: DirEntry[], except?: string, named?: string): DirEntry[] {
  const found = entries.filter((e) => e.kind === "file" && e.supported && e.path !== except && /gain/i.test(e.name));
  // the reference the movie itself names (EER acquisition metadata) comes first
  return named ? [...found.filter((e) => e.name === named), ...found.filter((e) => e.name !== named)] : found;
}

// ── particle coordinates ─────────────────────────────────────────────────────

/** Coordinate files the server reads (cryo/picks.py). */
export const PICK_EXTENSIONS = [".star", ".box", ".cbox", ".cs"];

export function isPicksFile(name: string): boolean {
  return PICK_EXTENSIONS.includes(extension(name));
}

/** What programs add to the name of a micrograph along its processing, as on the server. */
const NAME_SUFFIXES = [
  "_patch_aligned_doseweighted", "_rigid_aligned_doseweighted", "_patch_aligned", "_rigid_aligned",
  "_doseweighted", "_fractions", "_frames", "_movie", "_autopick", "_manualpick", "_eer", "_dw",
];

/** What two names of the same micrograph have in common (see micrograph_key on the server). */
export function micrographKey(name: string): string {
  let stem = name.replace(/\\/g, "/").split("/").pop() ?? "";
  if (stem.includes(".")) stem = stem.slice(0, stem.lastIndexOf("."));
  stem = stem.replace(/^\d{15,}_/, "").toLowerCase();
  for (let changed = true; changed; ) {
    changed = false;
    for (const suffix of NAME_SUFFIXES) {
      if (stem.endsWith(suffix) && stem.length > suffix.length) {
        stem = stem.slice(0, -suffix.length);
        changed = true;
      }
    }
  }
  return stem;
}

/** The coordinate files of a listing that are about the image `path`: same micrograph name. */
export function pickCandidates(entries: DirEntry[], path: string): DirEntry[] {
  const key = micrographKey(path);
  return entries.filter((e) => e.kind === "file" && isPicksFile(e.name) && micrographKey(e.name) === key);
}

/** Diameter of the circles, in image pixels: the user's, else the boxes of the file, else a
 *  twenty-fifth of the image width. */
export function pickDiameter(cryo: CryoSettings, picks: PicksResponse | null, imageWidth: number): number {
  if (cryo.picksDiameter && cryo.picksDiameter > 0) return cryo.picksDiameter;
  if (picks?.box && picks.box > 0) return picks.box;
  return Math.max(8, Math.round(imageWidth / 25));
}

export interface PickOverlay {
  /** Centres in image pixels, as drawn: x0, y0, x1, y1… */
  points: Float32Array;
  diameter: number;
}

/** The particles to draw: those above the score asked for, rows counted from the top or,
 *  flipped, from the bottom of an image `imageHeight` pixels high. */
export function pickOverlay(picks: PicksResponse, cryo: CryoSettings, imageWidth: number, imageHeight: number): PickOverlay {
  const min = picks.score && cryo.picksMinScore !== null ? cryo.picksMinScore : null;
  const points = new Float32Array(picks.count * 2);
  let n = 0;
  for (let k = 0; k < picks.count; k++) {
    if (min !== null && (picks.score?.[k] ?? 0) < min) continue;
    points[n++] = picks.x[k] ?? 0;
    points[n++] = cryo.picksFlipY ? imageHeight - (picks.y[k] ?? 0) : (picks.y[k] ?? 0);
  }
  return { points: points.subarray(0, n), diameter: pickDiameter(cryo, picks, imageWidth) };
}

/** Lowest and highest score of the particles, or null when the file has none. */
export function scoreRange(picks: PicksResponse | null): [number, number] | null {
  if (!picks?.score || picks.score.length === 0) return null;
  let lo = Infinity;
  let hi = -Infinity;
  for (const s of picks.score) {
    if (s < lo) lo = s;
    if (s > hi) hi = s;
  }
  return hi > lo ? [lo, hi] : null;
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
