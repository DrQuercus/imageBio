/** Live mode, pure logic: following a session by its changes, laying out the grid of tiles,
 *  wording durations and sizes, and the link that designates a session. */

import type { AlertCondition, LiveItem } from "../api/types";

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
