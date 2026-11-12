/** Live mode, pure logic: following a session by its changes, laying out the grid of tiles,
 *  wording durations and sizes, and the link that designates a session. */

import type { AlertCondition, LiveItem, QualityRules } from "../api/types";

/** Exposures known so far, by id. */
export type ItemMap = ReadonlyMap<number, LiveItem>;

/** Apply a page of changes: new and changed exposures replace what was known, deleted ones go.
 *  Returns the same map when nothing changed, so that nothing re-renders. */
export function mergeItems(known: ItemMap, changes: LiveItem[]): ItemMap {
  if (!changes.length) return known;
  const next = new Map(known);
  for (const item of changes) {
    if (item.state === "deleted") next.delete(item.id);
    else next.set(item.id, item);
  }
  return next;
}

/** When an exposure was taken, or failing that, when it was first seen. */
export function itemTime(item: LiveItem): number {
  return item.time ?? item.first_seen;
}

/** Exposures in acquisition order (oldest first); ties and unknown times fall back on names. */
export function orderItems(known: ItemMap): LiveItem[] {
  return [...known.values()].sort(
    (a, b) => itemTime(a) - itemTime(b) || (a.name < b.name ? -1 : a.name > b.name ? 1 : a.id - b.id),
  );
}

export interface GridLayout {
  columns: number;
  rows: number;
  /** Height of the whole grid, in pixels: what the scroll bar represents. */
  height: number;
  /** Positions (in display order) of the first and last tile to draw; last < first: none. */
  first: number;
  last: number;
}

/** Which tiles of a grid of `count` square tiles are worth drawing: those in view, plus
 *  `buffer` rows above and below. Only these exist in the page, whatever the session's size. */
export function gridLayout(
  count: number,
  width: number,
  viewHeight: number,
  scrollTop: number,
  tile: number,
  gap = 8,
  buffer = 2,
): GridLayout {
  const pitch = tile + gap;
  const columns = Math.max(1, Math.floor((width + gap) / pitch));
  const rows = Math.ceil(count / columns);
  const height = Math.max(0, rows * pitch - gap);
  if (!count) return { columns, rows, height, first: 0, last: -1 };
  const firstRow = Math.max(0, Math.floor(scrollTop / pitch) - buffer);
  const lastRow = Math.min(rows - 1, Math.ceil((scrollTop + viewHeight) / pitch) + buffer);
  return { columns, rows, height, first: firstRow * columns, last: Math.min(count - 1, (lastRow + 1) * columns - 1) };
}

/** Top-left corner of the tile at `position` (display order). */
export function tilePosition(position: number, columns: number, tile: number, gap = 8): { top: number; left: number } {
  return { top: Math.floor(position / columns) * (tile + gap), left: (position % columns) * (tile + gap) };
}

/** "il y a 12 s", "il y a 3 min 05 s", "il y a 2 h 10 min". */
export function formatAgo(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `il y a ${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `il y a ${m} min ${String(s % 60).padStart(2, "0")} s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `il y a ${h} h ${String(m % 60).padStart(2, "0")} min`;
  return `il y a ${Math.floor(h / 24)} j`;
}

/** How worrying the time since the last image is: fine under 2 minutes, late under 10. */
export function freshness(seconds: number | null): "none" | "fresh" | "late" | "stalled" {
  if (seconds === null) return "none";
  return seconds < 120 ? "fresh" : seconds < 600 ? "late" : "stalled";
}

export function formatBytes(bytes: number): string {
  const units = ["o", "Ko", "Mo", "Go", "To", "Po"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit++;
  }
  return `${value >= 100 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

/** What a tile says under its picture: dose and defocus when the metadata gave them. */
export function itemDetails(item: LiveItem): string {
  const parts: string[] = [];
  const epu = item.meta.epu;
  const mdoc = item.meta.mdoc;
  const defocus = epu?.applied_defocus ?? mdoc?.defocus;
  const dose = epu?.dose ?? mdoc?.dose;
  if (typeof defocus === "number") parts.push(`${defocus.toFixed(1)} µm`);
  if (typeof dose === "number") parts.push(`${dose.toFixed(1)} e⁻/Å²`);
  if (typeof mdoc?.tilt_angle === "number") parts.push(`${mdoc.tilt_angle.toFixed(1)}°`);
  return parts.join(" · ");
}

/** Ice thickness in nm, when the mean free path is known. */
export function iceThickness(item: LiveItem, reference: number | null, mfp: number | null): number | null {
  const ice = relativeIce(item, reference);
  return ice === null || !mfp ? null : ice * mfp;
}

/** The ice of an image as written in the corner of its tile: nanometres when the mean free
 *  path is known, the relative value otherwise; null: nothing to write. */
export function iceLabel(item: LiveItem, reference: number | null, mfp: number | null = null): string | null {
  const ice = relativeIce(item, reference);
  if (ice === null) return null;
  return mfp ? `glace ${Math.round(ice * mfp)} nm` : `glace ${ice.toFixed(2)}`;
}

/** How much ice the beam went through, compared with the brightest images of the session:
 *  ln(I₀ / I), where I is the mean of the image and I₀ the session's reference. 0: as bright as
 *  the reference; 1: 2.7 times fewer counts. To be multiplied by the mean free path of the
 *  microscope (300 to 400 nm) for a thickness. null: not measured, or no reference yet. */
export function relativeIce(item: LiveItem, reference: number | null): number | null {
  const mean = item.metrics?.mean;
  if (typeof mean !== "number" || !(mean > 0) || reference === null || !(reference > 0)) return null;
  return Math.log(reference / mean);
}

/** Above this ice-ring ratio the ice is probably crystalline (what cryoSPARC users reject at). */
export const ICE_RING_LIMIT = 1.2;
/** Relative ice above which an image has a tenth of the counts of the reference: a grid bar,
 *  or ice too thick to see through. */
export const DARK_ABOVE = Math.log(10);
/** Relative ice under which an image is as bright as an empty hole (within 5 %). Only said
 *  when the reference is the counts of an empty hole, not the brightest images of the session. */
export const EMPTY_BELOW = 0.05;

export type ImageFlag = "ice_ring" | "dark" | "empty";

export const FLAG_LABELS: Record<ImageFlag, { short: string; long: string }> = {
  ice_ring: { short: "glace cristalline", long: "Anneau de glace marqué : glace cristalline ou contamination" },
  dark: { short: "très sombre", long: "Dix fois moins de coups que la référence : barreau de grille ou glace très épaisse" },
  empty: { short: "trou vide", long: "Autant de coups qu'un trou vide : pas de glace" },
};

/** What is worth a mark on the tile of an image. `calibrated`: the reference is the counts of
 *  an empty hole (given by the user), so an image as bright as it is one. */
export function imageFlags(item: LiveItem, reference: number | null, calibrated = false): ImageFlag[] {
  const flags: ImageFlag[] = [];
  const ring = item.metrics?.ice_ring;
  if (typeof ring === "number" && ring > ICE_RING_LIMIT) flags.push("ice_ring");
  const ice = relativeIce(item, reference);
  if (ice !== null && ice > DARK_ABOVE) flags.push("dark");
  if (ice !== null && calibrated && ice < EMPTY_BELOW) flags.push("empty");
  return flags;
}

/** The CTF of an image: the fit of the program the server ran on it when there is one (it
 *  knows astigmatism), else the quick estimate made on every image. */
export interface ShownCtf {
  defocus: number;
  /** Resolution (Å) up to which the rings follow; a program may not give it. */
  fit: number | null;
  /** Astigmatism (µm); null: not measured (the quick estimate does not). */
  astigmatism: number | null;
  source: "program" | "estimate";
}

export function ctfOf(item: LiveItem): ShownCtf | null {
  const fitted = item.processing?.ctf;
  if (fitted && !("error" in fitted)) {
    return { defocus: fitted.defocus, fit: fitted.fit ?? null, astigmatism: fitted.astigmatism, source: "program" };
  }
  const quick = item.metrics?.ctf;
  return quick ? { defocus: quick.defocus, fit: quick.fit, astigmatism: null, source: "estimate" } : null;
}

/** How far the specimen moved during the exposure: the path a motion correction followed
 *  frame by frame when the server ran one, else the shift between the first and the last
 *  frames measured on the movie. */
export interface ShownDrift {
  value: number;
  unit: "A" | "px";
  /** True: a path, summed over all the frames; false: one shift, from start to end. */
  path: boolean;
}

export function driftOf(item: LiveItem): ShownDrift | null {
  const motion = item.processing?.motion;
  if (motion && !("error" in motion)) return { value: motion.total, unit: motion.unit, path: true };
  const m = item.metrics;
  return m && typeof m.drift === "number" ? { value: m.drift, unit: m.drift_unit === "A" ? "A" : "px", path: false } : null;
}

const STEP_NAMES = { motion: "Alignement du film", ctf: "CTF" } as const;

/** What the programs of the server gave for an image, one per line; a step that failed says
 *  why, in the words of the server. */
export function programLines(item: LiveItem): string[] {
  const p = item.processing;
  if (!p) return [];
  const lines: string[] = [];
  if (p.error) lines.push(`Programmes : ${p.error}`);
  if (p.motion) {
    if ("error" in p.motion) lines.push(`${STEP_NAMES.motion} : ${p.motion.error}`);
    else {
      const unit = p.motion.unit === "A" ? "Å" : "px";
      lines.push(`Mouvement sur ${p.motion.frames} frames : ${p.motion.total.toFixed(1)} ${unit}, dont ${p.motion.early.toFixed(1)} au début`);
    }
  }
  if (p.ctf) {
    if ("error" in p.ctf) lines.push(`${STEP_NAMES.ctf} : ${p.ctf.error}`);
    else {
      const fit = typeof p.ctf.fit === "number" ? `, ajusté jusqu'à ${p.ctf.fit.toFixed(1)} Å` : "";
      lines.push(`CTF ajusté : défocus ${p.ctf.defocus_u.toFixed(2)} et ${p.ctf.defocus_v.toFixed(2)} µm, astigmatisme ${p.ctf.astigmatism.toFixed(3)} µm${fit}`);
      if (typeof p.ctf.thickness === "number") lines.push(`Épaisseur d'après le CTF : ${Math.round(p.ctf.thickness)} nm`);
    }
  }
  return lines;
}

/** The measurements of an image, one per line, for the tooltip of its tile. */
export function metricsLines(item: LiveItem, reference: number | null, mfp: number | null = null): string[] {
  const m = item.metrics;
  const programs = programLines(item);
  if (!m) return programs;
  if (typeof m.mean !== "number") return ["Image illisible : pas de mesure", ...programs];
  const lines = [`Coups moyens : ${m.mean.toFixed(m.mean < 100 ? 2 : 0)}${m.estimated ? " (estimés sur les premières frames)" : ""}`];
  const ice = relativeIce(item, reference);
  if (ice !== null) lines.push(`Glace relative : ${ice.toFixed(2)} (ln I₀/I)`);
  if (ice !== null && mfp) lines.push(`Épaisseur de glace : ${Math.round(ice * mfp)} nm`);
  if (typeof m.ice_ring === "number") lines.push(`Anneau de glace : ${m.ice_ring.toFixed(2)}`);
  if (m.ctf) {
    const target = item.meta.epu?.applied_defocus ?? item.meta.mdoc?.defocus;
    const asked = typeof target === "number" ? ` (visé ${Math.abs(target).toFixed(1)})` : "";
    lines.push(`Défocus mesuré : ${m.ctf.defocus.toFixed(2)} µm${asked}`);
    lines.push(`Anneaux de Thon retrouvés jusqu'à ${m.ctf.fit.toFixed(1)} Å`);
  }
  if (typeof m.drift === "number") lines.push(`Dérive entre le début et la fin : ${m.drift.toFixed(1)} ${m.drift_unit === "A" ? "Å" : "px"}`);
  return [...lines, ...programs];
}

// ── order and choice of the tiles ────────────────────────────────────────────

export type SortKey = "time" | "ice" | "ring" | "fit" | "focus" | "defocus" | "dose" | "drift";

export const SORT_LABELS: Record<SortKey, string> = {
  time: "Heure",
  ice: "Glace",
  ring: "Anneau de glace",
  fit: "Résolution du CTF",
  focus: "Défocus mesuré",
  defocus: "Défocus visé",
  dose: "Dose",
  drift: "Dérive",
};

/** The value an image is sorted on; null: it has none (not measured, no metadata). */
export function sortValue(item: LiveItem, key: SortKey, reference: number | null): number | null {
  switch (key) {
    case "time":
      return itemTime(item);
    case "ice":
      return relativeIce(item, reference);
    case "ring":
      return item.metrics?.ice_ring ?? null;
    case "fit":
      return ctfOf(item)?.fit ?? null;
    case "focus":
      return ctfOf(item)?.defocus ?? null;
    case "defocus": {
      const d = item.meta.epu?.applied_defocus ?? item.meta.mdoc?.defocus;
      return typeof d === "number" ? Math.abs(d) : null;
    }
    case "dose": {
      const d = item.meta.epu?.dose ?? item.meta.mdoc?.dose;
      return typeof d === "number" ? d : null;
    }
    case "drift":
      return driftOf(item)?.value ?? null;
  }
}

/** The images in display order. `items` come in acquisition order; images without the value
 *  sorted on go last, whatever the direction, in acquisition order. */
export function arrangeItems(items: LiveItem[], key: SortKey, descending: boolean, reference: number | null): LiveItem[] {
  if (key === "time") return descending ? [...items].reverse() : items;
  const valued: [number, number, LiveItem][] = [];
  const rest: LiveItem[] = [];
  items.forEach((item, position) => {
    const value = sortValue(item, key, reference);
    if (value === null || !Number.isFinite(value)) rest.push(item);
    else valued.push([value, position, item]);
  });
  valued.sort((a, b) => (descending ? b[0] - a[0] : a[0] - b[0]) || a[1] - b[1]);
  return [...valued.map((v) => v[2]), ...rest];
}

// ── acceptance: what the user of the session calls a good image ───────────────

export const NO_RULES: QualityRules = { fit_max: null, ice_max: null, ring_max: null, drift_max: null, defocus_min: null, defocus_max: null };

export function hasRules(rules: QualityRules): boolean {
  return Object.values(rules).some((v) => v !== null);
}

/** What a rejection is about: the threshold an image is beyond. */
export type RejectionCode = "fit" | "ice" | "ring" | "drift" | "defocus";

/** Why an image is rejected: one reason per threshold it is beyond. A measurement the image
 *  does not have is not held against it. The server judges the same way for its reports
 *  (live/quality.py); both are tested on the cases of backend/tests/fixtures/verdict_cases.json. */
export function rejectionReasons(item: LiveItem, rules: QualityRules, reference: number | null): { code: RejectionCode; text: string }[] {
  const m = item.metrics;
  if (!m) return [];
  const why: { code: RejectionCode; text: string }[] = [];
  const ctf = ctfOf(item);
  if (rules.fit_max !== null && ctf && ctf.fit !== null && ctf.fit > rules.fit_max) {
    why.push({ code: "fit", text: `anneaux de Thon jusqu'à ${ctf.fit.toFixed(1)} Å seulement (seuil ${rules.fit_max})` });
  }
  const ice = relativeIce(item, reference);
  if (rules.ice_max !== null && ice !== null && ice > rules.ice_max) {
    why.push({ code: "ice", text: `glace ${ice.toFixed(2)} (seuil ${rules.ice_max})` });
  }
  if (rules.ring_max !== null && typeof m.ice_ring === "number" && m.ice_ring > rules.ring_max) {
    why.push({ code: "ring", text: `anneau de glace ${m.ice_ring.toFixed(2)} (seuil ${rules.ring_max})` });
  }
  const drift = driftOf(item);
  if (rules.drift_max !== null && drift && drift.unit === "A" && drift.value > rules.drift_max) {
    why.push({ code: "drift", text: `${drift.path ? "mouvement" : "dérive"} ${drift.value.toFixed(1)} Å (seuil ${rules.drift_max})` });
  }
  if (ctf && ((rules.defocus_min !== null && ctf.defocus < rules.defocus_min) || (rules.defocus_max !== null && ctf.defocus > rules.defocus_max))) {
    why.push({ code: "defocus", text: `défocus ${ctf.defocus.toFixed(2)} µm hors de ${rules.defocus_min ?? "…"} à ${rules.defocus_max ?? "…"}` });
  }
  return why;
}

/** Why an image is rejected, one sentence per threshold it is beyond. */
export function rejections(item: LiveItem, rules: QualityRules, reference: number | null): string[] {
  return rejectionReasons(item, rules, reference).map((r) => r.text);
}

export type Verdict = "accepted" | "rejected";

/** The verdict on an image; null: nothing to judge it on (no threshold, or not measured yet). */
export function verdict(item: LiveItem, rules: QualityRules, reference: number | null): Verdict | null {
  if (item.state !== "complete" || typeof item.metrics?.mean !== "number" || !hasRules(rules)) return null;
  return rejections(item, rules, reference).length ? "rejected" : "accepted";
}

/** How many images were accepted and rejected; null when nothing was judged. */
export function qualitySummary(items: LiveItem[], rules: QualityRules, reference: number | null): { accepted: number; rejected: number } | null {
  let accepted = 0;
  let rejected = 0;
  for (const item of items) {
    const v = verdict(item, rules, reference);
    if (v === "accepted") accepted += 1;
    else if (v === "rejected") rejected += 1;
  }
  return accepted + rejected ? { accepted, rejected } : null;
}

export type ItemFilter = "all" | "flagged" | "clean" | "accepted" | "rejected";

export const FILTER_LABELS: Record<ItemFilter, string> = {
  all: "Toutes",
  flagged: "Marquées",
  clean: "Sans marque",
  accepted: "Acceptées",
  rejected: "Rejetées",
};

/** The images a filter keeps: those with a mark (crystalline ice, very dark, empty hole), those
 *  that were measured and have none, or those the thresholds accept or reject. */
export function filterItems(
  items: LiveItem[],
  filter: ItemFilter,
  reference: number | null,
  calibrated: boolean,
  rules: QualityRules = NO_RULES,
): LiveItem[] {
  if (filter === "all") return items;
  if (filter === "accepted" || filter === "rejected") return items.filter((item) => verdict(item, rules, reference) === filter);
  return items.filter((item) => {
    if (item.state !== "complete") return false;
    const marked = imageFlags(item, reference, calibrated).length > 0;
    return filter === "flagged" ? marked : !marked && typeof item.metrics?.mean === "number";
  });
}

// ── map of the acquisitions ──────────────────────────────────────────────────

export interface StagePoint {
  item: LiveItem;
  /** Stage position, µm. */
  x: number;
  y: number;
}

/** The images whose stage position is known (EPU writes it with each image). */
export function stagePoints(items: LiveItem[]): StagePoint[] {
  const points: StagePoint[] = [];
  for (const item of items) {
    const stage = item.meta.epu?.stage;
    if (stage && typeof stage.x === "number" && typeof stage.y === "number" && Number.isFinite(stage.x) && Number.isFinite(stage.y)) {
      points.push({ item, x: stage.x, y: stage.y });
    }
  }
  return points;
}

/** Where the points go in a drawing of `width` x `height` pixels: same scale on both axes,
 *  `pad` pixels of margin, the stage's y axis pointing up. Also the length in µm of a scale
 *  bar and its length in pixels. */
export function fitStage(points: StagePoint[], width: number, height: number, pad = 24): { xy: [number, number][]; scale: number } {
  if (!points.length || width <= 2 * pad || height <= 2 * pad) return { xy: [], scale: 0 };
  let x0 = Infinity;
  let x1 = -Infinity;
  let y0 = Infinity;
  let y1 = -Infinity;
  for (const p of points) {
    x0 = Math.min(x0, p.x);
    x1 = Math.max(x1, p.x);
    y0 = Math.min(y0, p.y);
    y1 = Math.max(y1, p.y);
  }
  const dx = x1 - x0;
  const dy = y1 - y0;
  if (dx === 0 && dy === 0) return { xy: points.map(() => [width / 2, height / 2]), scale: 0 }; // one place
  // pixels per µm: the tighter of the two axes decides
  const scale = Math.min(dx > 0 ? (width - 2 * pad) / dx : Infinity, dy > 0 ? (height - 2 * pad) / dy : Infinity);
  const left = (width - dx * scale) / 2;
  const top = (height - dy * scale) / 2;
  return { xy: points.map((p) => [left + (p.x - x0) * scale, top + (y1 - p.y) * scale]), scale };
}

/** What the grid can show: the acquisitions, or the overview images EPU takes on the way. */
export const CATEGORY_VIEWS = [
  { key: "data", label: "Acquisitions", categories: "data,image" },
  { key: "foilhole", label: "Trous", categories: "foilhole" },
  { key: "gridsquare", label: "Carrés", categories: "gridsquare" },
  { key: "atlas", label: "Atlas", categories: "atlas" },
] as const;

export type CategoryView = (typeof CATEGORY_VIEWS)[number]["key"];

/** Median relative ice of the most recent measured images, and the share of them with a
 *  marked ice ring. null: no reference, or nothing measured. */
export function iceSummary(items: LiveItem[], reference: number | null, last = 50): { median: number; ring: number; count: number } | null {
  const values: number[] = [];
  let rings = 0;
  for (let k = items.length - 1; k >= 0 && values.length < last; k--) {
    const item = items[k];
    const ice = item ? relativeIce(item, reference) : null;
    if (!item || ice === null) continue;
    values.push(ice);
    if ((item.metrics?.ice_ring ?? 0) > ICE_RING_LIMIT) rings += 1;
  }
  if (values.length === 0) return null;
  values.sort((a, b) => a - b);
  const mid = values.length >> 1;
  const upper = values[mid] ?? 0;
  const median = values.length % 2 ? upper : ((values[mid - 1] ?? upper) + upper) / 2;
  return { median, ring: rings / values.length, count: values.length };
}

/** What each alert condition is called in the interface. */
export const ALERT_LABELS: Record<AlertCondition, string> = {
  no_image: "plus d'image",
  disk: "disque presque plein",
  rate_drop: "débit en chute",
  scanner: "surveillance en échec",
};

/** A threshold as typed in a field: empty is "not watched", anything else must be a number
 *  within the bounds the server accepts. Returns undefined for what cannot be sent. */
export function parseThreshold(text: string, min: number, max: number): number | null | undefined {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= min && value <= max ? value : undefined;
}

/** The session designated by the address: "#live=<directory>" ("#live" alone: none chosen yet).
 *  null: the address is not a Live one. */
export function parseLiveLink(hash: string): { dir: string | null } | null {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  if (!q.has("live")) return null;
  return { dir: q.get("live") || null };
}

export function formatLiveLink(dir: string | null): string {
  const q = new URLSearchParams();
  q.set("live", dir ?? "");
  return `#${q.toString()}`;
}

/** Bars of an acquisition histogram: one per bucket from the first to the last, empty ones
 *  included, limited to the most recent `maxBars`. */
export function historyBars(counts: [number, number][], bucket: number, maxBars: number): { start: number; values: number[] } {
  if (!counts.length) return { start: 0, values: [] };
  const last = counts[counts.length - 1]![0];
  const first = Math.max(counts[0]![0], last - (maxBars - 1) * bucket);
  const values = new Array<number>(Math.round((last - first) / bucket) + 1).fill(0);
  for (const [start, n] of counts) {
    const k = Math.round((start - first) / bucket);
    if (k >= 0 && k < values.length) values[k] = n;
  }
  return { start: first, values };
}
