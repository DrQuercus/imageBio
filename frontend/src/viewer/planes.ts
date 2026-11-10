/** Loading planes for display: one request per channel, a small byte-bounded cache on the
 *  client (replaying a movie or going back to a plane costs no request). */

import { api } from "../api/client";
import type { Plane, PlaneIndex, ReaderOptions, Region } from "../api/types";

const CACHE_BYTES = 256 * 2 ** 20;

const cache = new Map<string, Plane>(); // insertion order = least recently used first
let cachedBytes = 0;

function remember(key: string, plane: Plane): void {
  const old = cache.get(key);
  if (old) {
    cachedBytes -= old.data.byteLength;
    cache.delete(key);
  }
  if (plane.data.byteLength > CACHE_BYTES / 4) return;
  cache.set(key, plane);
  cachedBytes += plane.data.byteLength;
  for (const [k, v] of cache) {
    if (cachedBytes <= CACHE_BYTES) break;
    cache.delete(k);
    cachedBytes -= v.data.byteLength;
  }
}

export interface PlaneSet {
  key: string; // identifies file version, options, plane, bin and region
  planeKey: string; // identifies file version, options and plane only
  planes: Plane[]; // one per channel shown
  width: number;
  height: number;
  bin: number;
  region: Region;
}

export function planeKeyOf(etag: string, options: ReaderOptions, index: PlaneIndex, channels: number[]): string {
  return `${etag}|${JSON.stringify(options)}|${index.t},${index.z}|${channels.join(",")}`;
}

/** Load the given channels of plane (t, z) at `bin`, optionally cropped to `region`. */
export async function loadPlanes(
  path: string,
  etag: string,
  options: ReaderOptions,
  index: PlaneIndex,
  channels: number[],
  bin: number,
  region: Region | undefined,
  signal: AbortSignal,
): Promise<PlaneSet> {
  const planeKey = planeKeyOf(etag, options, index, channels);
  const key = `${path}|${planeKey}|${bin}|${region?.join(",") ?? "full"}`;
  const planes = await Promise.all(
    channels.map(async (c) => {
      const k = `${key}|c${c}`;
      const hit = cache.get(k);
      if (hit) {
        cache.delete(k);
        cache.set(k, hit); // most recently used
        return hit;
      }
      const plane = await api.plane(path, { t: index.t, c, z: index.z, bin, region }, options, signal);
      remember(k, plane);
      return plane;
    }),
  );
  const first = planes[0];
  if (!first) throw new Error("no channel requested");
  return { key, planeKey, planes, width: first.width, height: first.height, bin: first.bin, region: first.region };
}

/** The value of each channel at an image position, or null outside the set. */
export function valuesAt(set: PlaneSet, x: number, y: number): number[] | null {
  const tx = Math.floor((x - set.region[0]) / set.bin);
  const ty = Math.floor((y - set.region[1]) / set.bin);
  if (tx < 0 || ty < 0 || tx >= set.width || ty >= set.height) return null;
  return set.planes.map((p) => Number(p.data[ty * set.width + tx]));
}

export function toFloat32(plane: Plane): Float32Array {
  return plane.data instanceof Float32Array ? plane.data : new Float32Array(plane.data);
}
