import { describe, expect, it } from "vitest";
import type { DirEntry } from "../src/api/types";
import type { PlaneSet } from "../src/viewer/planes";
import { lineProfile, profileRange, profileText, setCovers } from "../src/viewer/profile";
import { neighbour, siblingImages } from "../src/viewer/siblings";

const entry = (name: string, kind: DirEntry["kind"] = "file", supported = true): DirEntry => ({
  name, path: `r/s/${name}`, kind, size: 1, mtime: 0, is_symlink: false, supported,
});

describe("next and previous image of a folder", () => {
  const listing = [
    entry("Images-Disc1", "dir"),
    entry("a_9.mrc"),
    entry("a_10.mrc"),
    entry("a_10.xml", "file", false),
    entry("a_10_autopick.star"),
    entry("a_11.tiff"),
    entry("dead link", "broken", false),
  ];
  it("keeps the images, in the order of the listing", () => {
    expect(siblingImages(listing)).toEqual(["r/s/a_9.mrc", "r/s/a_10.mrc", "r/s/a_11.tiff"]);
  });
  it("moves by one, and stops at the ends", () => {
    const list = siblingImages(listing);
    expect(neighbour(list, "r/s/a_10.mrc", 1)).toBe("r/s/a_11.tiff");
    expect(neighbour(list, "r/s/a_10.mrc", -1)).toBe("r/s/a_9.mrc");
    expect(neighbour(list, "r/s/a_11.tiff", 1)).toBeNull();
    expect(neighbour(list, "r/s/a_9.mrc", -1)).toBeNull();
    expect(neighbour(list, "r/s/gone.mrc", 1)).toBeNull();
  });
});

/** A plane set over the region [x, y, w, h] of an image, binned `bin` times, value = f(x, y)
 *  of the top-left image pixel of each block. */
function setOf(region: [number, number, number, number], bin: number, f: (x: number, y: number) => number, channels = 1): PlaneSet {
  const [x0, y0, w, h] = region;
  const width = Math.ceil(w / bin);
  const height = Math.ceil(h / bin);
  const planes = Array.from({ length: channels }, (_, c) => {
    const data = new Float32Array(width * height);
    for (let j = 0; j < height; j++) for (let i = 0; i < width; i++) data[j * width + i] = f(x0 + i * bin, y0 + j * bin) + 1000 * c;
    return { data, width, height, dtype: "float32" as const, bin, region };
  });
  return { key: "k", planeKey: "p", planes, width, height, bin, region };
}

describe("intensity along the ruler", () => {
  const gradient = setOf([0, 0, 64, 48], 1, (x, y) => 100 * y + x);

  it("reads one sample per pixel, both ends included", () => {
    const p = lineProfile(gradient, { x0: 4.5, y0: 10.5, x1: 34.5, y1: 10.5 })!;
    expect(p.length).toBe(30);
    expect(p.at).toHaveLength(31);
    expect(p.values[0]![0]).toBe(1004);
    expect(p.values[0]![30]).toBe(1034);
    expect(p.values[0]!.every((v, k) => v === 1004 + k)).toBe(true);
    expect(profileRange(p)).toEqual([1004, 1034]);
    // vertical: steps of 100
    const down = lineProfile(gradient, { x0: 2.5, y0: 1.5, x1: 2.5, y1: 4.5 })!;
    expect(down.values[0]).toEqual([102, 202, 302, 402]);
  });

  it("samples a binned set at its own pitch, and marks what falls outside", () => {
    const binned = setOf([0, 0, 64, 48], 4, (x) => x);
    const p = lineProfile(binned, { x0: 0, y0: 0, x1: 40, y1: 0 })!;
    expect(p.bin).toBe(4);
    expect(p.at).toHaveLength(11);
    expect(p.values[0]![10]).toBe(40);
    const out = lineProfile(binned, { x0: 60, y0: 2, x1: 70, y1: 2 })!;
    expect(Number.isNaN(out.values[0]!.at(-1)!)).toBe(true);
    expect(setCovers(binned, { x0: 60, y0: 2, x1: 70, y1: 2 })).toBe(false);
    expect(setCovers(binned, { x0: 1, y0: 2, x1: 63.9, y1: 47 })).toBe(true);
    expect(lineProfile(binned, { x0: 3, y0: 3, x1: 3, y1: 3 })).toBeNull();
    // a very long segment is capped
    expect(lineProfile(setOf([0, 0, 64, 48], 1, () => 0), { x0: 0, y0: 0, x1: 63, y1: 47 }, 10)!.at).toHaveLength(10);
  });

  it("is written as text for a spreadsheet, in Å when the pixel size is known", () => {
    const p = lineProfile(gradient, { x0: 0.5, y0: 0.5, x1: 2.5, y1: 0.5 })!;
    expect(profileText(p, 1.5)).toBe("distance_A\tvalue\n0\t0\n1.5\t1\n3\t2\n");
    expect(profileText(p, null).split("\n")[0]).toBe("distance_px\tvalue");
    const rgb = lineProfile(setOf([0, 0, 8, 8], 1, (x) => x, 3), { x0: 0.5, y0: 0.5, x1: 1.5, y1: 0.5 })!;
    expect(profileText(rgb, null)).toBe("distance_px\tred\tgreen\tblue\n0\t0\t1000\t2000\n1\t1\t1001\t2001\n");
  });
});
