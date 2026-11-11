import { describe, expect, it } from "vitest";
import { ApiError, buildQuery, decodePlane, decodeSpectrum } from "../src/api/client";
import { formatLink, parseLink } from "../src/state/url";

const headers = (h: Record<string, string>) => ({ get: (name: string) => h[name] ?? null });

describe("plane decoding", () => {
  it("decodes every dtype from its headers", () => {
    const u16 = new Uint16Array([1, 2, 3, 4, 5, 65535]);
    const plane = decodePlane(u16.buffer, headers({ "X-Dtype": "uint16", "X-Shape": "2,3", "X-Bin": "1", "X-Region": "0,0,3,2" }));
    expect(plane.width).toBe(3);
    expect(plane.height).toBe(2);
    expect(Array.from(plane.data)).toEqual([1, 2, 3, 4, 5, 65535]);
    expect(plane.data).toBeInstanceOf(Uint16Array);
    const f32 = new Float32Array([1.5, -2.25]);
    const binned = decodePlane(f32.buffer, headers({ "X-Dtype": "float32", "X-Shape": "1,2", "X-Bin": "4", "X-Region": "8,16,8,4" }));
    expect(binned.bin).toBe(4);
    expect(binned.region).toEqual([8, 16, 8, 4]);
    expect(Array.from(binned.data)).toEqual([1.5, -2.25]);
    const i8 = decodePlane(new Int8Array([-1, 127]).buffer, headers({ "X-Dtype": "int8", "X-Shape": "1,2", "X-Bin": "1", "X-Region": "0,0,2,1" }));
    expect(Array.from(i8.data)).toEqual([-1, 127]);
  });

  it("refuses a body that does not match its headers", () => {
    const body = new Uint8Array(5).buffer;
    const bad = [
      { "X-Dtype": "uint16", "X-Shape": "2,3", "X-Bin": "1", "X-Region": "0,0,3,2" },
      { "X-Dtype": "uint8", "X-Shape": "5", "X-Bin": "1", "X-Region": "0,0,5,1" },
      { "X-Dtype": "complex64", "X-Shape": "1,5", "X-Bin": "1", "X-Region": "0,0,5,1" },
      { "X-Shape": "1,5", "X-Bin": "1", "X-Region": "0,0,5,1" },
    ];
    for (const h of bad) expect(() => decodePlane(body, headers(h as Record<string, string>))).toThrow(ApiError);
  });

  it("decodes a power spectrum, which must be a square float32 image", () => {
    const power = new Float32Array([1, 2, 3, 4]);
    const spectrum = decodeSpectrum(power.buffer, headers({ "X-Dtype": "float32", "X-Shape": "2,2" }));
    expect(spectrum.side).toBe(2);
    expect(Array.from(spectrum.data)).toEqual([1, 2, 3, 4]);
    expect(() => decodeSpectrum(power.buffer, headers({ "X-Dtype": "float32", "X-Shape": "1,4" }))).toThrow(ApiError);
    expect(() => decodeSpectrum(power.buffer, headers({ "X-Dtype": "uint16", "X-Shape": "2,4" }))).toThrow(ApiError);
  });
});

describe("queries", () => {
  it("omits undefined parameters and prefixes reader options", () => {
    const q = buildQuery({ path: "cryo/a b.mrc", t: 3, x: undefined }, { group: 4, superres: 1 });
    expect(q).toBe("path=cryo%2Fa+b.mrc&t=3&opt.group=4&opt.superres=1");
  });
});

describe("links", () => {
  it("round-trips a full view", () => {
    const state = {
      path: "cryo/session 1/movie.eer",
      index: { t: 7, c: 0, z: 2 },
      options: { group: 40, superres: 1 },
      view: { zoom: 0.4375, cx: 2048.5, cy: 1000 },
      display: { min: -12.5, max: 480, gamma: 1.4, lut: "fire", invert: true, auto: false },
      apix: 0.83,
      cryo: {
        lowpass: 20, highpass: 500, average: 5, spectrum: true, gain: "cryo/session 1/ref.gain", gainOrient: 6, gainInvert: true,
        fourierBin: true, picks: "cryo/job 5/particles.star", picksFlipY: true, picksDiameter: 180, picksMinScore: -0.25,
      },
      gallery: true,
    };
    const link = formatLink(state);
    expect(link.startsWith("#path=cryo%2Fsession+1%2Fmovie.eer")).toBe(true);
    expect(parseLink(link)).toEqual(state);
  });

  it("keeps links short for default settings", () => {
    const link = formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, display: { auto: true, gamma: 1, lut: "grays", invert: false, min: 0, max: 1 } });
    expect(link).toBe("#path=a.mrc");
    expect(formatLink({ index: { t: 0, c: 0, z: 0 }, options: {} })).toBe("");
    const off = { lowpass: null, highpass: null, average: 1, spectrum: false, gain: null, gainOrient: 0, gainInvert: false };
    expect(formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, cryo: { ...off, gainOrient: 3 } })).toBe("#path=a.mrc");
    expect(formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, cryo: { ...off, gain: "r/g.gain" } })).toBe("#path=a.mrc&gain=r%2Fg.gain");
    expect(formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, cryo: off })).toBe("#path=a.mrc");
    expect(formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, cryo: { ...off, lowpass: 7.5 } })).toBe("#path=a.mrc&lp=7.5");
    // the settings of the picks mean nothing without a coordinate file
    const noPicks = { ...off, picks: null, picksFlipY: true, picksDiameter: 64, picksMinScore: 0.3 };
    expect(formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, cryo: noPicks })).toBe("#path=a.mrc");
    expect(formatLink({ path: "a.mrc", index: { t: 0, c: 0, z: 0 }, options: {}, cryo: { ...noPicks, picks: "r/a.box", picksMinScore: 0 } })).toBe(
      "#path=a.mrc&picks=r%2Fa.box&pfy=1&pd=64&pms=0",
    );
    expect(parseLink("#path=a.mrc&pfy=1&pd=64").cryo).toBeUndefined();
    expect(parseLink("#path=a.mrc&picks=r%2Fa.box&pd=-3&pms=x").cryo).toEqual({ picks: "r/a.box" });
  });

  it("ignores garbage", () => {
    const state = parseLink("#path=x.mrc&t=-3&z=abc&zoom=0&cx=1&cy=2&min=5&max=1&gamma=-1&apix=nan&opt.=1&lp=-4&hp=x&avg=2.5&ps=yes&go=6&gi=1");
    expect(state).toEqual({ path: "x.mrc", index: { t: 0, c: 0, z: 0 }, options: {} });
    expect(parseLink("")).toEqual({ index: { t: 0, c: 0, z: 0 }, options: {} });
  });
});
