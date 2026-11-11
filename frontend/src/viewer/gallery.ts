/** The gallery of a stack, pure logic: which axis is laid out, in which order, with which labels. */

import type { ClassInfo, DatasetInfo } from "../api/types";

export type GalleryAxis = "t" | "z";

/** The axis a gallery lays out: the images of a stack, else the sections of a volume. null: the
 *  file has one plane, or is a colour picture. */
export function galleryAxis(info: Pick<DatasetInfo, "shape" | "rgb">): GalleryAxis | null {
  if (info.rgb) return null;
  if (info.shape.t > 1) return "t";
  return info.shape.z > 1 ? "z" : null;
}

export type GallerySort = "index" | "population";

/** The planes in display order. By population: the classes that received the most particles
 *  first, as RELION's own display sorts them; planes the table does not mention come last. */
export function galleryOrder(count: number, classes: ClassInfo[] | null, sort: GallerySort): number[] {
  const order = Array.from({ length: count }, (_, k) => k);
  if (sort !== "population" || !classes) return order;
  const share = new Map(classes.map((c) => [c.index, c.distribution]));
  return order.sort((a, b) => (share.get(b) ?? -1) - (share.get(a) ?? -1) || a - b);
}

/** What is written under the average of a class: its share of the particles and its resolution. */
export function classLabel(info: ClassInfo | undefined): string {
  if (!info) return "";
  const share = `${(info.distribution * 100).toFixed(info.distribution < 0.1 ? 1 : 0)} %`;
  return info.resolution !== null ? `${share} · ${info.resolution.toFixed(1)} Å` : share;
}

/** Bytes a whole stack takes once read; the server exports it to Fiji in one file up to
 *  MAX_EXPORT_BYTES (service/export.py), beyond which the file itself is to be opened. */
export const MAX_EXPORT_BYTES = 2 ** 30;

export function stackBytes(info: Pick<DatasetInfo, "shape" | "dtype">): number {
  const bits = Number(/(\d+)$/.exec(info.dtype)?.[1] ?? 8);
  return info.shape.x * info.shape.y * info.shape.t * info.shape.z * info.shape.c * (bits / 8);
}

/** Whether a file name is that of RELION class averages, which a model file describes. */
export function isClassAverages(path: string): boolean {
  return path.toLowerCase().endsWith("_classes.mrcs");
}
