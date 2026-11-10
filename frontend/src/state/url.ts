/** The state of a view in the URL fragment, so that a link reproduces what its author saw. */

import type { PlaneIndex, ReaderOptions } from "../api/types";
import type { View } from "../viewer/view";

export interface DisplaySettings {
  min: number;
  max: number;
  gamma: number;
  invert: boolean;
  lut: string;
  /** Follow the automatic window of each plane instead of a fixed one. */
  auto: boolean;
}

export interface LinkState {
  path?: string;
  index: PlaneIndex;
  options: ReaderOptions;
  view?: View;
  display?: Partial<DisplaySettings>;
  /** Pixel size set by the user, in Å, when the file does not give one. */
  apix?: number;
}

const num = (value: string | null): number | undefined => {
  if (value === null || value === "") return undefined;
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
};

const int = (value: string | null): number => {
  const n = num(value);
  return n !== undefined && Number.isInteger(n) && n >= 0 ? n : 0;
};

function optionValue(text: string): string | number | boolean {
  if (text === "true" || text === "false") return text === "true";
  return /^-?\d+(\.\d+)?$/.test(text) ? Number(text) : text;
}

export function parseLink(hash: string): LinkState {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const state: LinkState = { index: { t: int(q.get("t")), c: int(q.get("c")), z: int(q.get("z")) }, options: {} };
  const path = q.get("path");
  if (path) state.path = path;
  for (const [key, value] of q) {
    if (key.startsWith("opt.") && key.length > 4) state.options[key.slice(4)] = optionValue(value);
  }
  const zoom = num(q.get("zoom"));
  const cx = num(q.get("cx"));
  const cy = num(q.get("cy"));
  if (zoom !== undefined && zoom > 0 && cx !== undefined && cy !== undefined) state.view = { zoom, cx, cy };
  const display: Partial<DisplaySettings> = {};
  const min = num(q.get("min"));
  const max = num(q.get("max"));
  if (min !== undefined && max !== undefined && max > min) Object.assign(display, { min, max, auto: false });
  const gamma = num(q.get("gamma"));
  if (gamma !== undefined && gamma > 0) display.gamma = gamma;
  if (q.get("lut")) display.lut = q.get("lut") as string;
  if (q.get("inv") === "1") display.invert = true;
  if (Object.keys(display).length) state.display = display;
  const apix = num(q.get("apix"));
  if (apix !== undefined && apix > 0) state.apix = apix;
  return state;
}

const short = (value: number): string => String(Number(value.toPrecision(7)));

export function formatLink(state: LinkState): string {
  const q = new URLSearchParams();
  if (!state.path) return "";
  q.set("path", state.path);
  for (const axis of ["t", "c", "z"] as const) if (state.index[axis]) q.set(axis, String(state.index[axis]));
  for (const [key, value] of Object.entries(state.options)) q.set(`opt.${key}`, String(value));
  if (state.view) {
    q.set("zoom", short(state.view.zoom));
    q.set("cx", short(state.view.cx));
    q.set("cy", short(state.view.cy));
  }
  const d = state.display;
  if (d) {
    if (d.auto === false && d.min !== undefined && d.max !== undefined) {
      q.set("min", short(d.min));
      q.set("max", short(d.max));
    }
    if (d.gamma !== undefined && d.gamma !== 1) q.set("gamma", short(d.gamma));
    if (d.lut && d.lut !== "grays") q.set("lut", d.lut);
    if (d.invert) q.set("inv", "1");
  }
  if (state.apix) q.set("apix", short(state.apix));
  return `#${q.toString()}`;
}
