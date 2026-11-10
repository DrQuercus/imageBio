import { describe, expect, it } from "vitest";
import {
  averageEnd,
  filterProblem,
  formatResolution,
  ICE_RING,
  NO_CRYO,
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
    const keys = [{}, { t_end: 6 }, { lp: 6 }, { hp: 6 }, { lp: 6, apix: 1 }, { lp: 6, apix: 2 }].map(processingKey);
    expect(new Set(keys).size).toBe(keys.length);
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
