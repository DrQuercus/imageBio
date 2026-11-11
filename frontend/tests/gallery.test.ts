import { describe, expect, it } from "vitest";
import type { ClassInfo } from "../src/api/types";
import { classLabel, galleryAxis, galleryOrder, isClassAverages } from "../src/viewer/gallery";

const shape = (t: number, z: number) => ({ shape: { x: 64, y: 64, t, z, c: 1 }, rgb: false });

describe("the gallery of a stack", () => {
  it("lays out the images of a stack, else the sections of a volume", () => {
    expect(galleryAxis(shape(40, 1))).toBe("t");
    expect(galleryAxis(shape(1, 200))).toBe("z");
    expect(galleryAxis(shape(5, 3))).toBe("t");
    expect(galleryAxis(shape(1, 1))).toBeNull();
    expect(galleryAxis({ ...shape(1, 9), rgb: true })).toBeNull();
  });

  it("orders the classes by number, or the most populated first", () => {
    const classes: ClassInfo[] = [
      { index: 0, distribution: 0.1, resolution: 8 },
      { index: 1, distribution: 0.5, resolution: 6 },
      { index: 2, distribution: 0, resolution: null },
      { index: 3, distribution: 0.5, resolution: 7 },
    ];
    expect(galleryOrder(5, classes, "index")).toEqual([0, 1, 2, 3, 4]);
    // equal shares keep their numbers in order; a plane the table does not know comes last
    expect(galleryOrder(5, classes, "population")).toEqual([1, 3, 0, 2, 4]);
    expect(galleryOrder(3, null, "population")).toEqual([0, 1, 2]);
    expect(galleryOrder(0, classes, "population")).toEqual([]);
  });

  it("labels a class with its share of the particles and its resolution", () => {
    expect(classLabel({ index: 0, distribution: 0.412, resolution: 7.85 })).toBe("41 % · 7.8 Å");
    expect(classLabel({ index: 0, distribution: 0.034, resolution: 12 })).toBe("3.4 % · 12.0 Å");
    expect(classLabel({ index: 0, distribution: 0, resolution: null })).toBe("0.0 %");
    expect(classLabel(undefined)).toBe("");
  });

  it("knows the class averages of RELION by their name", () => {
    expect(isClassAverages("cryo/Class2D/job008/run_it025_classes.mrcs")).toBe(true);
    expect(isClassAverages("cryo/RUN_IT025_CLASSES.MRCS")).toBe(true);
    expect(isClassAverages("cryo/particles.mrcs")).toBe(false);
    expect(isClassAverages("cryo/run_it025_classes.mrc")).toBe(false);
  });
});
