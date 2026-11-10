/** View geometry: pure functions, no DOM.
 *
 *  A view is the image point shown at the centre of the viewport and a zoom factor in CSS pixels
 *  per image pixel. Image coordinates are continuous: pixel (i, j) covers [i, i+1) x [j, j+1).
 */

import type { Region } from "../api/types";

export interface View {
  zoom: number;
  cx: number;
  cy: number;
}

export interface Size {
  width: number;
  height: number;
}

export const MIN_ZOOM = 1 / 512;
export const MAX_ZOOM = 64;
export const MAX_BIN = 64;
/** Largest overview (whole image) kept on the GPU, in texels. */
export const BASE_MAX_TEXELS = 1 << 21;
/** Largest detail (visible region) requested, in texels. */
export const DETAIL_MAX_TEXELS = 1 << 22;

export function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

export function imageToScreen(view: View, viewport: Size, x: number, y: number): [number, number] {
  return [(x - view.cx) * view.zoom + viewport.width / 2, (y - view.cy) * view.zoom + viewport.height / 2];
}

export function screenToImage(view: View, viewport: Size, sx: number, sy: number): [number, number] {
  return [(sx - viewport.width / 2) / view.zoom + view.cx, (sy - viewport.height / 2) / view.zoom + view.cy];
}

/** The view that shows the whole image, with a small margin. */
export function fitView(image: Size, viewport: Size, margin = 0.96): View {
  const zoom = Math.min(viewport.width / image.width, viewport.height / image.height) * margin;
  return { zoom: clamp(zoom || 1, MIN_ZOOM, MAX_ZOOM), cx: image.width / 2, cy: image.height / 2 };
}

/** Zoom by `factor`, keeping the image point under the screen point (sx, sy) where it is. */
export function zoomAt(view: View, viewport: Size, factor: number, sx: number, sy: number): View {
  const zoom = clamp(view.zoom * factor, MIN_ZOOM, MAX_ZOOM);
  const [ix, iy] = screenToImage(view, viewport, sx, sy);
  return {
    zoom,
    cx: ix - (sx - viewport.width / 2) / zoom,
    cy: iy - (sy - viewport.height / 2) / zoom,
  };
}

export function panBy(view: View, dxScreen: number, dyScreen: number): View {
  return { ...view, cx: view.cx - dxScreen / view.zoom, cy: view.cy - dyScreen / view.zoom };
}

function pow2Floor(value: number): number {
  return 2 ** Math.floor(Math.log2(Math.max(1, value)));
}

/** Bin factor of the overview: the smallest power of two that keeps it under BASE_MAX_TEXELS. */
export function baseBin(image: Size, maxTexels = BASE_MAX_TEXELS): number {
  let bin = 1;
  while (bin < MAX_BIN && Math.ceil(image.width / bin) * Math.ceil(image.height / bin) > maxTexels) bin *= 2;
  return bin;
}

/** The bin at which image pixels are no smaller than device pixels: binning further than this
 *  would lose visible detail, binning less would only transfer pixels the screen cannot show. */
export function idealBin(zoom: number, devicePixelRatio: number): number {
  return clamp(pow2Floor(1 / (zoom * devicePixelRatio)), 1, MAX_BIN);
}

export interface DetailRequest {
  bin: number;
  region: Region;
}

/** The region to request for the current view, or null when nothing of the image is visible.
 *
 *  The visible part is grown by `margin` on each side (small pans need no new request), aligned
 *  on the bin grid (so its blocks coincide with those of the overview) and binned at the ideal
 *  bin, or coarser when that would exceed `maxTexels`.
 */
export function detailRequest(
  view: View,
  viewport: Size,
  image: Size,
  devicePixelRatio: number,
  margin = 0.25,
  maxTexels = DETAIL_MAX_TEXELS,
): DetailRequest | null {
  const [left, top] = screenToImage(view, viewport, 0, 0);
  const [right, bottom] = screenToImage(view, viewport, viewport.width, viewport.height);
  const growX = (right - left) * margin;
  const growY = (bottom - top) * margin;
  const x0 = clamp(left - growX, 0, image.width);
  const y0 = clamp(top - growY, 0, image.height);
  const x1 = clamp(right + growX, 0, image.width);
  const y1 = clamp(bottom + growY, 0, image.height);
  if (x1 <= x0 || y1 <= y0) return null;
  let bin = idealBin(view.zoom, devicePixelRatio);
  for (;;) {
    const ax0 = Math.floor(x0 / bin) * bin;
    const ay0 = Math.floor(y0 / bin) * bin;
    const ax1 = Math.min(image.width, Math.ceil(x1 / bin) * bin);
    const ay1 = Math.min(image.height, Math.ceil(y1 / bin) * bin);
    const texels = Math.ceil((ax1 - ax0) / bin) * Math.ceil((ay1 - ay0) / bin);
    if (texels <= maxTexels || bin >= MAX_BIN) return { bin, region: [ax0, ay0, ax1 - ax0, ay1 - ay0] };
    bin *= 2;
  }
}

/** The plane index brought back inside a dataset of the given shape. */
export function clampIndex<T extends { t: number; c: number; z: number }>(index: T, shape: { t: number; c: number; z: number }): T {
  const t = clamp(index.t, 0, Math.max(0, shape.t - 1));
  const c = clamp(index.c, 0, Math.max(0, shape.c - 1));
  const z = clamp(index.z, 0, Math.max(0, shape.z - 1));
  return t === index.t && c === index.c && z === index.z ? index : { ...index, t, c, z };
}

/** Whether `inner` lies inside `outer`. */
export function regionContains(outer: Region, inner: Region): boolean {
  return (
    inner[0] >= outer[0] &&
    inner[1] >= outer[1] &&
    inner[0] + inner[2] <= outer[0] + outer[2] &&
    inner[1] + inner[3] <= outer[1] + outer[3]
  );
}

/** A "nice" length (1, 2 or 5 times a power of ten) close to `target`. */
export function niceLength(target: number): number {
  if (!(target > 0) || !Number.isFinite(target)) return 1;
  const power = 10 ** Math.floor(Math.log10(target));
  const mantissa = target / power;
  return (mantissa >= 5 ? 5 : mantissa >= 2 ? 2 : 1) * power;
}

/** A length in Å as text, in the most readable unit. */
export function formatLength(angstrom: number): string {
  const abs = Math.abs(angstrom);
  const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));
  if (abs >= 1e7) return `${fmt(angstrom / 1e7)} mm`;
  if (abs >= 1e4) return `${fmt(angstrom / 1e4)} µm`;
  if (abs >= 100) return `${fmt(angstrom / 10)} nm`;
  return `${fmt(angstrom)} Å`;
}
