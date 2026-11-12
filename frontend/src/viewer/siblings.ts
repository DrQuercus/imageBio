/** The images of a folder, to go from one to the next (keys N and B): pure functions. */

import type { DirEntry } from "../api/types";
import { isPicksFile } from "./cryo";

/** Logical paths of the files of a listing that the viewer can show, in the order of the
 *  listing (the server sorts names naturally: image_9 before image_10). Coordinate files are
 *  left out: they are laid over an image, not shown. */
export function siblingImages(entries: DirEntry[]): string[] {
  return entries.filter((e) => e.kind === "file" && e.supported && !isPicksFile(e.name)).map((e) => e.path);
}

/** The image `delta` places from `current` in `list`, or null at either end (no wrapping: going
 *  past the last image of a session and back to the first would be easy to miss). */
export function neighbour(list: string[], current: string, delta: number): string | null {
  const at = list.indexOf(current);
  if (at < 0) return null;
  return list[at + delta] ?? null;
}
