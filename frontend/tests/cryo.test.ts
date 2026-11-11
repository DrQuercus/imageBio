import { describe, expect, it } from "vitest";
import type { DirEntry, PicksResponse } from "../src/api/types";
import {
  averageEnd,
  dirname,
  filterProblem,
  formatResolution,
  gainCandidates,
  gainOf,
  groupForDose,
  isPicksFile,
  micrographKey,
  pickCandidates,
  pickDiameter,
  pickOverlay,
  scoreRange,
  ICE_RING,
  keepsGain,
  NO_CRYO,
  ORIENTATION_LABELS,
  processingKey,
  processingOf,
  resolutionAt,
  ringRadius,
  spectrumRings,
  spectrumToRgba,
  spectrumWindow,
} from "../src/viewer/cryo";

describe("frame average", () => {
  it("covers `average` frames from the current one, cut at the end of the movie", () => {
    expect(averageEnd(2, 5, 40)).toBe(6); // frames 3 to 7, counted from 1
    expect(averageEnd(0, 40, 40)).toBe(39);
    expect(averageEnd(38, 5, 40)).toBe(39);
    expect(averageEnd(39, 5, 40)).toBeNull(); // the last frame alone
    expect(averageEnd(3, 1, 40)).toBeNull();
    expect(averageEnd(0, 5, 1)).toBeNull(); // a single image
  });
});

describe("EER fractions per frame from the dose", () => {
  it("aims at one electron per square ångström in each frame", () => {
    // 0.05 e per detector pixel and fraction at 0.75 Å per pixel: 0.0889 e/Å², so 11 fractions
    expect(groupForDose(0.05, 0.75, 1, 1000)).toBe(11);
    expect(groupForDose(0.05, 0.75, 1, 1000, 3)).toBe(34); // 33.75
    // rendered at twice the sampling, the image pixel is half as wide: same detector, same answer
    expect(groupForDose(0.05, 0.375, 2, 1000)).toBe(11);
  });

  it("stays between one fraction and the whole movie", () => {
    expect(groupForDose(5, 1, 1, 100)).toBe(1); // more than 1 e/Å² in a single fraction
    expect(groupForDose(0.001, 1, 1, 100)).toBe(100);
  });

  it("gives up without a usable dose", () => {
    for (const dose of [0, -1, NaN, Infinity]) expect(groupForDose(dose, 1, 1, 100)).toBeNull();
    expect(groupForDose(0.05, 0, 1, 100)).toBeNull();
    expect(groupForDose(0.05, 1, 1, 0)).toBeNull();
  });
});

describe("processing requested from the server", () => {
  const filtered = { ...NO_CRYO, lowpass: 20, highpass: 400, average: 5 };

  it("is empty when nothing is asked", () => {
    expect(processingOf(NO_CRYO, 3, 40, 1.2, true)).toEqual({});
    expect(processingKey({})).toBe(processingKey(processingOf(NO_CRYO, 0, 1, null, false)));
  });

  it("sends the filters and the frame range, not the pixel size the file already has", () => {
    expect(processingOf(filtered, 2, 40, 0.83, true)).toEqual({ t_end: 6, lp: 20, hp: 400 });
  });

  it("sends the pixel size typed by the user, and only with a filter", () => {
    expect(processingOf(filtered, 2, 40, 0.83, false)).toEqual({ t_end: 6, lp: 20, hp: 400, apix: 0.83 });
    expect(processingOf({ ...NO_CRYO, average: 5 }, 2, 40, 0.83, false)).toEqual({ t_end: 6 });
  });

  it("leaves out filters that cannot be applied, and says why", () => {
    expect(processingOf(filtered, 2, 40, null, false)).toEqual({ t_end: 6 });
    expect(filterProblem(filtered, null)).toMatch(/pixel inconnue/);
    const crossed = { ...NO_CRYO, lowpass: 50, highpass: 20 };
    expect(processingOf(crossed, 0, 1, 1, true)).toEqual({});
    expect(filterProblem(crossed, 1)).toMatch(/plus fin/);
    expect(filterProblem(filtered, 0.83)).toBeNull();
    expect(filterProblem(NO_CRYO, null)).toBeNull(); // nothing asked, nothing to complain about
  });

  it("gives different keys to different processings", () => {
    const keys = [{}, { t_end: 6 }, { lp: 6 }, { hp: 6 }, { lp: 6, apix: 1 }, { lp: 6, apix: 2 }, { fbin: true }, { lp: 6, fbin: true }].map(processingKey);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("asks for the reduction in Fourier space when it is chosen", () => {
    expect(processingOf({ ...NO_CRYO, fourierBin: true }, 0, 1, null, false)).toEqual({ fbin: true });
    expect(processingOf({ ...filtered, fourierBin: true }, 2, 40, 0.83, true)).toEqual({ t_end: 6, lp: 20, hp: 400, fbin: true });
  });
});

describe("gain reference", () => {
  const withGain = { ...NO_CRYO, gain: "cryo/s1/ref.gain" };

  it("is sent with its orientation and kind, defaults left out", () => {
    expect(gainOf(NO_CRYO)).toEqual({});
    expect(gainOf(withGain)).toEqual({ gain: "cryo/s1/ref.gain" });
    expect(gainOf({ ...withGain, gainOrient: 6, gainInvert: true })).toEqual({ gain: "cryo/s1/ref.gain", gain_orient: 6, gain_inv: true });
    expect(gainOf({ ...NO_CRYO, gainOrient: 6, gainInvert: true })).toEqual({}); // no file, nothing to orient
    expect(processingOf({ ...withGain, average: 5 }, 0, 40, null, false)).toEqual({ t_end: 4, gain: "cryo/s1/ref.gain" });
  });

  it("changes the cache key with the file, the orientation and the kind", () => {
    const keys = [
      {},
      { gain: "a.gain" },
      { gain: "b.gain" },
      { gain: "a.gain", gain_orient: 6 },
      { gain: "a.gain", gain_inv: true },
      { gain: "a.gain", gain_orient: 6, gain_inv: true },
    ].map(processingKey);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("names the eight orientations of the server", () => {
    expect(ORIENTATION_LABELS).toHaveLength(8);
    expect(new Set(ORIENTATION_LABELS).size).toBe(8);
    expect(ORIENTATION_LABELS[6]).toBe("miroir haut-bas"); // flip_y in cryo/gain.py
    expect(ORIENTATION_LABELS[4]).toBe("miroir gauche-droite"); // flip_x
  });

  it("is kept only for a file of the same kind in the same directory", () => {
    expect(keepsGain("cryo/s1/a.eer", "cryo/s1/b.eer")).toBe(true);
    expect(keepsGain("cryo/s1/a.eer", "cryo/s1/B.EER")).toBe(true);
    expect(keepsGain("cryo/s1/a.eer", "cryo/s1/a_aligned.mrc")).toBe(false); // already corrected
    expect(keepsGain("cryo/s1/a.eer", "cryo/s2/a.eer")).toBe(false);
    expect(keepsGain(null, "cryo/s1/a.eer")).toBe(false);
    expect(dirname("cryo/s1/a.eer")).toBe("cryo/s1");
    expect(dirname("cryo")).toBe("");
  });

  it("is looked for among the readable files with gain in their name", () => {
    const entry = (name: string, kind: DirEntry["kind"] = "file", supported = true): DirEntry => ({
      name,
      path: `cryo/s1/${name}`,
      kind,
      size: 1,
      mtime: 0,
      is_symlink: false,
      supported,
    });
    const entries = [
      entry("20240101_EER_GainReference.gain"),
      entry("K3_gain.tiff"),
      entry("CountRef-gain-ref.MRC"),
      entry("movie_0001.eer"),
      entry("gain_notes.txt", "file", false),
      entry("gain", "dir"),
      entry("FoilHole_gain.eer"),
    ];
    expect(gainCandidates(entries, "cryo/s1/FoilHole_gain.eer").map((e) => e.name)).toEqual([
      "20240101_EER_GainReference.gain",
      "K3_gain.tiff",
      "CountRef-gain-ref.MRC",
    ]);
    // the one the movie names comes first
    expect(gainCandidates(entries, undefined, "K3_gain.tiff")[0]!.name).toBe("K3_gain.tiff");
    expect(gainCandidates(entries, undefined, "absent.gain")).toHaveLength(4);
  });
});

describe("particle coordinates", () => {
  const found = (patch: Partial<PicksResponse> = {}): PicksResponse => ({
    format: "star", count: 3, total: 3, x: [10, 20, 30], y: [1, 2, 3], score: [0.2, 0.9, 0.5], box: null, many: false,
    notes: [], picks: "cryo/a.star", image: { x: 400, y: 300 }, ...patch,
  });

  it("knows a coordinate file by its extension", () => {
    for (const name of ["a.star", "A.STAR", "mic.box", "mic.cbox", "J12_picked.cs"]) expect(isPicksFile(name)).toBe(true);
    for (const name of ["a.mrc", "star", "a.star.bak", "box.txt", "a.csv"]) expect(isPicksFile(name)).toBe(false);
  });

  it("recognises one micrograph under the names programs give it, as the server does", () => {
    const key = micrographKey("cryo/s1/FoilHole_1_Data_2_3_20261110_101500.mrc");
    expect(key).toBe("foilhole_1_data_2_3_20261110_101500");
    for (const other of [
      "MotionCorr/job002/Movies/FoilHole_1_Data_2_3_20261110_101500_fractions.mrc",
      "FoilHole_1_Data_2_3_20261110_101500_EER.eer",
      "012345678901234567890_FoilHole_1_Data_2_3_20261110_101500_fractions_patch_aligned_doseweighted.mrc",
      "FoilHole_1_Data_2_3_20261110_101500_autopick.star",
      "C:\\data\\FoilHole_1_Data_2_3_20261110_101500_Fractions.tiff",
    ]) {
      expect(micrographKey(other)).toBe(key);
    }
    expect(micrographKey("FoilHole_1_Data_2_3_20261110_101501.mrc")).not.toBe(key);
    expect(micrographKey("_fractions.mrc")).toBe("_fractions");
    expect(micrographKey("123_mic.mrc")).toBe("123_mic");
  });

  it("offers the coordinate files that bear the name of the image", () => {
    const entry = (name: string, kind: DirEntry["kind"] = "file"): DirEntry => ({
      name, path: `cryo/s1/${name}`, kind, size: 1, mtime: 0, is_symlink: false, supported: false,
    });
    const entries = [entry("mic_001.box"), entry("mic_001_autopick.star"), entry("mic_002.box"), entry("mic_001.cbox", "dir"), entry("mic_001.txt"), entry("particles.star")];
    expect(pickCandidates(entries, "cryo/s1/mic_001.mrc").map((e) => e.name)).toEqual(["mic_001.box", "mic_001_autopick.star"]);
    expect(pickCandidates(entries, "cryo/s1/mic_003.mrc")).toEqual([]);
  });

  it("draws the particles kept, rows counted from the top or from the bottom", () => {
    const all = pickOverlay(found(), NO_CRYO, 400, 300);
    expect(Array.from(all.points)).toEqual([10, 1, 20, 2, 30, 3]);
    expect(all.diameter).toBe(16); // no box in the file: a twenty-fifth of the width
    const flipped = pickOverlay(found(), { ...NO_CRYO, picksFlipY: true }, 400, 300);
    expect(Array.from(flipped.points)).toEqual([10, 299, 20, 298, 30, 297]);
    const best = pickOverlay(found(), { ...NO_CRYO, picksMinScore: 0.5 }, 400, 300);
    expect(Array.from(best.points)).toEqual([20, 2, 30, 3]); // at the threshold: kept
    // a threshold means nothing for a file without scores
    expect(pickOverlay(found({ score: null }), { ...NO_CRYO, picksMinScore: 0.5 }, 400, 300).points).toHaveLength(6);
    expect(pickOverlay(found({ count: 0, x: [], y: [], score: null }), NO_CRYO, 400, 300).points).toHaveLength(0);
  });

  it("sizes the circles from the user, else the boxes of the file, else the image", () => {
    expect(pickDiameter({ ...NO_CRYO, picksDiameter: 90 }, found({ box: 180 }), 4096)).toBe(90);
    expect(pickDiameter(NO_CRYO, found({ box: 180 }), 4096)).toBe(180);
    expect(pickDiameter(NO_CRYO, found(), 4096)).toBe(164);
    expect(pickDiameter(NO_CRYO, null, 100)).toBe(8);
  });

  it("gives the range of the scores, when there is one", () => {
    expect(scoreRange(found())).toEqual([0.2, 0.9]);
    expect(scoreRange(found({ score: null }))).toBeNull();
    expect(scoreRange(found({ score: [0.5, 0.5] }))).toBeNull(); // nothing to choose between
    expect(scoreRange(null)).toBeNull();
  });
});

describe("power spectrum geometry", () => {
  it("places a period of d Å at side x apix / d pixels from the centre", () => {
    expect(ringRadius(512, 1.25, 10)).toBeCloseTo(64, 10);
    expect(ringRadius(512, 1.25, 2.5)).toBeCloseTo(256, 10); // Nyquist: the edge of the spectrum
    expect(resolutionAt(512, 1.25, 64)).toBeCloseTo(10, 10);
    expect(resolutionAt(512, 1.25, 0)).toBe(Infinity);
    for (const d of [3.7, 8, 20]) expect(resolutionAt(4096, 0.83, ringRadius(4096, 0.83, d))).toBeCloseTo(d, 10);
  });

  it("draws round resolutions inside Nyquist, apart from each other, with the ice ring", () => {
    const rings = spectrumRings(512, 1.25); // Nyquist 2.5 Å
    expect(rings.every((r) => r.radius <= 256)).toBe(true);
    for (const r of rings) expect(r.radius).toBeCloseTo(ringRadius(512, 1.25, r.d), 10);
    const radii = rings.map((r) => r.radius);
    expect([...radii].sort((a, b) => a - b)).toEqual(radii);
    for (let i = 1; i < radii.length; i++) expect(radii[i]! - radii[i - 1]!).toBeGreaterThanOrEqual(512 / 14);
    const ice = rings.filter((r) => r.ice);
    expect(ice).toHaveLength(1);
    expect(ice[0]!.d).toBe(ICE_RING);
    expect(rings.map((r) => r.d)).toContain(10);
  });

  it("leaves the ice ring out when the pixels are too coarse to show it", () => {
    const rings = spectrumRings(512, 2.5); // Nyquist 5 Å
    expect(rings.some((r) => r.ice)).toBe(false);
    expect(rings.every((r) => r.d >= 5)).toBe(true);
    expect(rings.length).toBeGreaterThan(0);
  });
});

describe("power spectrum display", () => {
  it("maps powers to grey levels on a log scale", () => {
    const power = new Float32Array([1, 10, 100, 1000, 0, NaN, Infinity, -1]);
    const rgba = spectrumToRgba(power, [0, 3]);
    const grey = Array.from({ length: power.length }, (_, i) => rgba[4 * i]);
    expect(grey).toEqual([0, 85, 170, 255, 0, 0, 0, 0]);
    for (let i = 0; i < power.length; i++) {
      expect(rgba[4 * i + 1]).toBe(rgba[4 * i]);
      expect(rgba[4 * i + 2]).toBe(rgba[4 * i]);
      expect(rgba[4 * i + 3]).toBe(255);
    }
    expect(Array.from(spectrumToRgba(new Float32Array([1e-9, 1e9]), [0, 3])).filter((_, i) => i % 4 === 0)).toEqual([0, 255]);
  });

  it("windows on the bulk of the values, whatever the peaks do", () => {
    const power = new Float32Array(10000).fill(100);
    power[0] = 1e30; // the zero-frequency peak
    power[1] = 0;
    power[2] = NaN;
    const [lo, hi] = spectrumWindow(power);
    expect(lo).toBeCloseTo(2, 5);
    expect(hi).toBeGreaterThan(lo);
    expect(hi).toBeLessThanOrEqual(3.01);
    expect(spectrumWindow(new Float32Array(16))).toEqual([0, 1]); // nothing usable
    const ramp = Float32Array.from({ length: 1000 }, (_, i) => 10 ** (i / 999)); // log10 from 0 to 1
    const [rlo, rhi] = spectrumWindow(ramp);
    expect(rlo).toBeCloseTo(0.02, 2);
    expect(rhi).toBeCloseTo(0.998, 2);
  });

  it("formats resolutions", () => {
    expect(formatResolution(3.7)).toBe("3.70 Å");
    expect(formatResolution(12.44)).toBe("12.4 Å");
    expect(formatResolution(250)).toBe("250 Å");
    expect(formatResolution(Infinity)).toBe("∞");
  });
});
