/** The state of a view in the URL fragment, so that a link reproduces what its author saw. */

import type { PlaneIndex, ReaderOptions } from "../api/types";
import type { CryoSettings } from "../viewer/cryo";
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
  /** Cryo mode: filters, frame average, power spectrum. */
  cryo?: Partial<CryoSettings>;
  /** The planes of the stack side by side instead of one plane in the viewer. */
  gallery?: boolean;
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
  const cryo: Partial<CryoSettings> = {};
  const lp = num(q.get("lp"));
  const hp = num(q.get("hp"));
  const avg = num(q.get("avg"));
  if (lp !== undefined && lp > 0) cryo.lowpass = lp;
  if (hp !== undefined && hp > 0) cryo.highpass = hp;
  if (avg !== undefined && Number.isInteger(avg) && avg > 1) cryo.average = avg;
  if (q.get("ps") === "1") cryo.spectrum = true;
  const gain = q.get("gain");
  if (gain) {
    cryo.gain = gain;
    const orient = num(q.get("go"));
    if (orient !== undefined && Number.isInteger(orient) && orient >= 1 && orient <= 7) cryo.gainOrient = orient;
    if (q.get("gi") === "1") cryo.gainInvert = true;
  }
  const picks = q.get("picks");
  if (picks) {
    cryo.picks = picks;
    if (q.get("pfy") === "1") cryo.picksFlipY = true;
    const diameter = num(q.get("pd"));
    if (diameter !== undefined && diameter > 0) cryo.picksDiameter = diameter;
    const score = num(q.get("pms"));
    if (score !== undefined) cryo.picksMinScore = score;
  }
  if (Object.keys(cryo).length) state.cryo = cryo;
  if (q.get("gal") === "1") state.gallery = true;
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
  const cryo = state.cryo;
  if (cryo) {
    if (cryo.lowpass) q.set("lp", short(cryo.lowpass));
    if (cryo.highpass) q.set("hp", short(cryo.highpass));
    if (cryo.average && cryo.average > 1) q.set("avg", String(cryo.average));
    if (cryo.spectrum) q.set("ps", "1");
    if (cryo.gain) {
      q.set("gain", cryo.gain);
      if (cryo.gainOrient) q.set("go", String(cryo.gainOrient));
      if (cryo.gainInvert) q.set("gi", "1");
    }
    if (cryo.picks) {
      q.set("picks", cryo.picks);
      if (cryo.picksFlipY) q.set("pfy", "1");
      if (cryo.picksDiameter) q.set("pd", short(cryo.picksDiameter));
      if (cryo.picksMinScore !== null && cryo.picksMinScore !== undefined) q.set("pms", short(cryo.picksMinScore));
    }
  }
  if (state.gallery) q.set("gal", "1");
  return `#${q.toString()}`;
}
