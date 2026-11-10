/** Typed client of the imageBio HTTP API. Every call takes an AbortSignal: requests are
 *  "latest wins", a newer one cancels the previous. */

import type {
  DatasetInfoResponse,
  DirListing,
  Dtype,
  Me,
  Plane,
  PlaneArray,
  PlaneIndex,
  PlaneStats,
  Processing,
  ReaderOptions,
  Region,
  Spectrum,
  SpectrumProfile,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

type Params = Record<string, string | number | boolean | undefined>;

export function buildQuery(params: Params, options?: ReaderOptions): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) q.set(key, String(value));
  }
  for (const [key, value] of Object.entries(options ?? {})) q.set(`opt.${key}`, String(value));
  return q.toString();
}

async function request(url: string, signal?: AbortSignal): Promise<Response> {
  const response = await fetch(url, { signal, headers: { Accept: "application/json, application/octet-stream" } });
  if (response.ok) return response;
  let code = "http_error";
  let message = `HTTP ${response.status}`;
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    code = body.error?.code ?? code;
    message = body.error?.message ?? message;
  } catch {
    // not JSON: keep the generic message
  }
  throw new ApiError(response.status, code, message);
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return (await (await request(url, signal)).json()) as T;
}

const ARRAYS: Record<Dtype, { new (buffer: ArrayBuffer): PlaneArray; BYTES_PER_ELEMENT: number }> = {
  uint8: Uint8Array,
  int8: Int8Array,
  uint16: Uint16Array,
  int16: Int16Array,
  uint32: Uint32Array,
  int32: Int32Array,
  float32: Float32Array,
  float64: Float64Array,
};

type HeaderSource = { get(name: string): string | null };

/** Decode a 2-D array sent as raw bytes (little-endian, C order) from its headers. */
function decodeArray(buffer: ArrayBuffer, headers: HeaderSource) {
  const dtype = headers.get("X-Dtype") as Dtype | null;
  const shape = (headers.get("X-Shape") ?? "").split(",").map(Number);
  const ctor = dtype ? ARRAYS[dtype] : undefined;
  const [height, width] = shape;
  if (!ctor || !dtype || shape.length !== 2 || height === undefined || width === undefined) {
    throw new ApiError(502, "bad_plane", "the server sent a plane that cannot be decoded");
  }
  if (!(width > 0 && height > 0) || buffer.byteLength !== width * height * ctor.BYTES_PER_ELEMENT) {
    throw new ApiError(502, "bad_plane", "the server sent a plane whose size does not match its headers");
  }
  return { data: new ctor(buffer), width, height, dtype };
}

/** Decode the body of a plane response from its headers. */
export function decodePlane(buffer: ArrayBuffer, headers: HeaderSource): Plane {
  const region = (headers.get("X-Region") ?? "").split(",").map(Number);
  const bin = Number(headers.get("X-Bin") ?? "1");
  if (region.length !== 4 || !region.every(Number.isFinite) || !(bin >= 1)) {
    throw new ApiError(502, "bad_plane", "the server sent a plane that cannot be decoded");
  }
  return { ...decodeArray(buffer, headers), bin, region: region as Region };
}

/** Decode the body of a power-spectrum response: a square float32 image. */
export function decodeSpectrum(buffer: ArrayBuffer, headers: HeaderSource): Spectrum {
  const { data, width, height } = decodeArray(buffer, headers);
  if (!(data instanceof Float32Array) || width !== height) {
    throw new ApiError(502, "bad_plane", "the server sent a spectrum that cannot be decoded");
  }
  return { data, side: width };
}

export interface PlaneRequest extends PlaneIndex {
  bin: number;
  region?: Region;
  proc?: Processing;
}

/** A plane, or the mean of frames t..t_end, for the power spectrum. */
export interface SpectrumRequest extends PlaneIndex {
  t_end?: number;
}

export const api = {
  me: (signal?: AbortSignal) => getJson<Me>("/api/me", signal),

  roots: (signal?: AbortSignal) =>
    getJson<{ roots: { name: string; available: boolean }[] }>("/api/fs/roots", signal).then((r) => r.roots),

  list: (path: string, offset = 0, limit = 1000, signal?: AbortSignal) =>
    getJson<DirListing>(`/api/fs/list?${buildQuery({ path, offset, limit })}`, signal),

  info: (path: string, options?: ReaderOptions, signal?: AbortSignal) =>
    getJson<DatasetInfoResponse>(`/api/datasets/info?${buildQuery({ path }, options)}`, signal),

  stats: (path: string, index: PlaneIndex, options?: ReaderOptions, signal?: AbortSignal, proc?: Processing) =>
    getJson<PlaneStats>(`/api/datasets/stats?${buildQuery({ path, ...index, ...proc }, options)}`, signal),

  async plane(path: string, req: PlaneRequest, options?: ReaderOptions, signal?: AbortSignal): Promise<Plane> {
    const [x, y, w, h] = req.region ?? [];
    const query = buildQuery({ path, t: req.t, c: req.c, z: req.z, bin: req.bin, x, y, w, h, ...req.proc }, options);
    const response = await request(`/api/datasets/plane?${query}`, signal);
    return decodePlane(await response.arrayBuffer(), response.headers);
  },

  async spectrum(path: string, req: SpectrumRequest, options?: ReaderOptions, signal?: AbortSignal): Promise<Spectrum> {
    const response = await request(`/api/datasets/spectrum?${buildQuery({ path, ...req }, options)}`, signal);
    return decodeSpectrum(await response.arrayBuffer(), response.headers);
  },

  spectrumProfile: (path: string, req: SpectrumRequest, options?: ReaderOptions, signal?: AbortSignal) =>
    getJson<SpectrumProfile>(`/api/datasets/spectrum/profile?${buildQuery({ path, ...req }, options)}`, signal),
};
