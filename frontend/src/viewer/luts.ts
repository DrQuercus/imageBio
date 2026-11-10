/** Lookup tables: 256 RGBA entries each. The contrast window maps data to 0..255, the table
 *  gives the colour. Applied on the GPU, so changing one costs no request. */

export type Lut = Uint8Array; // 256 * 4 bytes, RGBA

type Rgb = readonly [number, number, number];

function fromFunction(f: (i: number) => Rgb): Lut {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const [r, g, b] = f(i);
    out.set([Math.round(r), Math.round(g), Math.round(b), 255], i * 4);
  }
  return out;
}

/** Linear interpolation of control points spread evenly over 0..255 (as ImageJ does). */
function interpolate(r: readonly number[], g: readonly number[], b: readonly number[]): Lut {
  const n = r.length;
  const scale = n / 256;
  const at = (v: readonly number[], i: number) => v[Math.min(i, n - 1)] ?? 0;
  return fromFunction((i) => {
    const i1 = Math.floor(i * scale);
    const f = i * scale - i1;
    const mix = (v: readonly number[]) => (1 - f) * at(v, i1) + f * at(v, i1 + 1);
    return [mix(r), mix(g), mix(b)];
  });
}

/** Interpolation between colour stops at even positions, end points included. */
function stops(colors: readonly Rgb[]): Lut {
  const last = colors.length - 1;
  return fromFunction((i) => {
    const p = (i / 255) * last;
    const k = Math.min(Math.floor(p), last - 1);
    const f = p - k;
    const a = colors[k] ?? [0, 0, 0];
    const b = colors[k + 1] ?? a;
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  });
}

const FIRE = {
  r: [0, 0, 1, 25, 49, 73, 98, 122, 146, 162, 173, 184, 195, 207, 217, 229, 240, 252, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255],
  g: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 14, 35, 57, 79, 101, 117, 133, 147, 161, 175, 190, 205, 219, 234, 248, 255, 255, 255, 255],
  b: [0, 61, 96, 130, 165, 192, 220, 227, 210, 181, 151, 122, 93, 64, 35, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 35, 98, 160, 223, 255],
};

const ICE = {
  r: [0, 0, 0, 0, 0, 0, 19, 29, 50, 48, 79, 112, 134, 158, 186, 201, 217, 229, 242, 250, 250, 250, 250, 251, 250, 250, 250, 250, 251, 251, 243, 230],
  g: [156, 165, 176, 184, 190, 196, 193, 184, 171, 162, 146, 125, 107, 93, 81, 87, 92, 97, 95, 93, 93, 90, 85, 69, 64, 54, 47, 35, 19, 0, 4, 0],
  b: [140, 147, 158, 166, 170, 176, 209, 220, 234, 225, 236, 246, 250, 251, 250, 250, 245, 230, 230, 222, 202, 180, 163, 142, 123, 114, 106, 94, 84, 64, 26, 27],
};

const VIRIDIS: Rgb[] = [
  [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142],
  [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37],
];

/** Grays, with the two ends flagged: blue = at or below the window, red = at or above. */
function hilo(): Lut {
  const lut = fromFunction((i) => [i, i, i]);
  lut.set([0, 0, 255, 255], 0);
  lut.set([255, 0, 0, 255], 255 * 4);
  return lut;
}

export const LUTS: Record<string, () => Lut> = {
  grays: () => fromFunction((i) => [i, i, i]),
  fire: () => interpolate(FIRE.r, FIRE.g, FIRE.b),
  ice: () => interpolate(ICE.r, ICE.g, ICE.b),
  viridis: () => stops(VIRIDIS),
  hilo,
  red: () => fromFunction((i) => [i, 0, 0]),
  green: () => fromFunction((i) => [0, i, 0]),
  blue: () => fromFunction((i) => [0, 0, i]),
  cyan: () => fromFunction((i) => [0, i, i]),
  magenta: () => fromFunction((i) => [i, 0, i]),
  yellow: () => fromFunction((i) => [i, i, 0]),
};

export const LUT_LABELS: Record<string, string> = {
  grays: "Gris", fire: "Fire", ice: "Ice", viridis: "Viridis", hilo: "HiLo",
  red: "Rouge", green: "Vert", blue: "Bleu", cyan: "Cyan", magenta: "Magenta", yellow: "Jaune",
};

/** Parse an ImageJ .lut file: 768 raw bytes (256 reds, greens, blues), the same after a 32-byte
 *  "ICOL" header, or text with 256 lines of "r g b" (an optional index column and header line). */
export function parseLutFile(buffer: ArrayBuffer): Lut {
  const bytes = new Uint8Array(buffer);
  const planar = (offset: number): Lut =>
    fromFunction((i) => [bytes[offset + i] ?? 0, bytes[offset + 256 + i] ?? 0, bytes[offset + 512 + i] ?? 0]);
  if (bytes.length === 768) return planar(0);
  if (bytes.length >= 800 && String.fromCharCode(...bytes.subarray(0, 4)) === "ICOL") return planar(32);
  const rows = new TextDecoder()
    .decode(bytes)
    .split(/\r?\n/)
    .map((line) => line.trim().split(/[\s,;]+/).filter(Boolean).map(Number))
    .filter((cols) => cols.length >= 3 && cols.every(Number.isFinite));
  if (rows.length !== 256) throw new Error("ce fichier n'est pas une palette ImageJ (.lut) de 256 couleurs");
  return fromFunction((i) => {
    const cols = rows[i] ?? [];
    const [r = 0, g = 0, b = 0] = cols.length >= 4 ? cols.slice(1) : cols;
    return [clampByte(r), clampByte(g), clampByte(b)];
  });
}

function clampByte(v: number): number {
  return Math.min(255, Math.max(0, v));
}
