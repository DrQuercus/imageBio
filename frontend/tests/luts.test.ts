import { describe, expect, it } from "vitest";
import { LUTS, parseLutFile } from "../src/viewer/luts";

const rgb = (lut: Uint8Array, i: number) => Array.from(lut.subarray(i * 4, i * 4 + 3));

describe("lookup tables", () => {
  it("all have 256 opaque entries", () => {
    for (const [name, make] of Object.entries(LUTS)) {
      const lut = make();
      expect(lut.length, name).toBe(1024);
      for (let i = 0; i < 256; i++) expect(lut[i * 4 + 3]).toBe(255);
    }
  });

  it("grays is the identity and hilo flags both ends", () => {
    const grays = LUTS.grays!();
    expect(rgb(grays, 0)).toEqual([0, 0, 0]);
    expect(rgb(grays, 137)).toEqual([137, 137, 137]);
    const hilo = LUTS.hilo!();
    expect(rgb(hilo, 0)).toEqual([0, 0, 255]);
    expect(rgb(hilo, 255)).toEqual([255, 0, 0]);
    expect(rgb(hilo, 100)).toEqual([100, 100, 100]);
  });

  it("fire matches ImageJ at its control points", () => {
    const fire = LUTS.fire!();
    expect(rgb(fire, 0)).toEqual([0, 0, 0]);
    expect(rgb(fire, 8)).toEqual([0, 0, 61]); // control point 1
    expect(rgb(fire, 128)).toEqual([240, 79, 0]); // control point 16
    expect(rgb(fire, 255)[0]).toBe(255);
  });

  it("viridis runs from purple to yellow", () => {
    const v = LUTS.viridis!();
    expect(rgb(v, 0)).toEqual([68, 1, 84]);
    expect(rgb(v, 255)).toEqual([253, 231, 37]);
  });
});

describe(".lut files", () => {
  const ramp = <T,>(f: (i: number) => T): T[] => Array.from({ length: 256 }, (_, i) => f(i));

  it("reads 768 raw bytes", () => {
    const bytes = new Uint8Array([...ramp((i) => i), ...ramp((i) => 255 - i), ...ramp(() => 7)]);
    const lut = parseLutFile(bytes.buffer);
    expect(rgb(lut, 10)).toEqual([10, 245, 7]);
  });

  it("reads the NIH Image variant with a header", () => {
    const header = new Uint8Array(32);
    header.set([73, 67, 79, 76]); // "ICOL"
    const bytes = new Uint8Array([...header, ...ramp((i) => i), ...ramp(() => 1), ...ramp(() => 2)]);
    expect(rgb(parseLutFile(bytes.buffer), 200)).toEqual([200, 1, 2]);
  });

  it("reads text tables with or without an index column", () => {
    const three = ramp((i) => `${i} ${255 - i} 0`).join("\n");
    const four = "Index\tRed\tGreen\tBlue\n" + ramp((i) => `${i}\t${i}\t0\t${255 - i}`).join("\r\n");
    expect(rgb(parseLutFile(new TextEncoder().encode(three).buffer as ArrayBuffer), 3)).toEqual([3, 252, 0]);
    expect(rgb(parseLutFile(new TextEncoder().encode(four).buffer as ArrayBuffer), 3)).toEqual([3, 0, 252]);
  });

  it("rejects anything else", () => {
    expect(() => parseLutFile(new Uint8Array(100).buffer)).toThrow();
    expect(() => parseLutFile(new TextEncoder().encode("hello\nworld").buffer as ArrayBuffer)).toThrow();
  });
});
