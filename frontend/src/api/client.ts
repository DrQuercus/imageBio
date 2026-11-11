/** Typed client of the imageBio HTTP API. Every call takes an AbortSignal: requests are
 *  "latest wins", a newer one cancels the previous. */

import type {
  DatasetInfoResponse,
  DirListing,
  Dtype,
  AlertRules,
  AlertTestResult,
  ClassesResponse,
  CtfResponse,
  GainCheck,
  IceReference,
  LiveAlerts,
  LiveHistory,
  LivePage,
  LiveSummary,
  LiveWatch,
  Me,
  PicksResponse,
  Plane,
  PlaneArray,
  PlaneIndex,
  PlaneStats,
  QualityRules,
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

type Method = "GET" | "POST" | "PUT" | "DELETE";

async function request(url: string, signal?: AbortSignal, method: Method = "GET"): Promise<Response> {
  const response = await fetch(url, { method, signal, headers: { Accept: "application/json, application/octet-stream" } });
  if (response.ok) return response;
  let code = "http_error";
  let message = `HTTP ${response.status}`;
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    code = body.error?.code ?? code;
    message = body.error?.message ?? message;
  } catch (e) {
    // A request cancelled while its error body was being read is a cancellation, not a
    // failure of the server: it must not surface as "HTTP 400". Anything else: not JSON,
    // keep the generic message.
    if (signal?.aborted || isAbort(e)) throw new DOMException("The request was cancelled", "AbortError");
  }
  throw new ApiError(response.status, code, message);
}

async function getJson<T>(url: string, signal?: AbortSignal, method: Method = "GET"): Promise<T> {
  return (await (await request(url, signal, method)).json()) as T;
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

/** A plane, or the mean of frames t..t_end, gain-corrected or not, for the power spectrum. */
export interface SpectrumRequest extends PlaneIndex, Pick<Processing, "t_end" | "gain" | "gain_orient" | "gain_inv"> {}

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

  /** The CTF of a plane; `apix` only for a file that does not know its pixel size. */
  ctf: (path: string, req: SpectrumRequest & { apix?: number }, options?: ReaderOptions, signal?: AbortSignal) =>
    getJson<CtfResponse>(`/api/datasets/ctf?${buildQuery({ path, ...req }, options)}`, signal),

  gainCheck: (
    path: string,
    gain: Pick<Processing, "gain" | "gain_orient" | "gain_inv">,
    options?: ReaderOptions,
    signal?: AbortSignal,
  ) => getJson<GainCheck>(`/api/cryo/gain/check?${buildQuery({ path, ...gain }, options)}`, signal),

  /** Address of an ImageJ TIFF to open in Fiji: the plane as shown, or the whole stack. */
  exportUrl: (path: string, index: PlaneIndex, options: ReaderOptions, proc: Processing, scope: "plane" | "stack") =>
    scope === "stack"
      ? `/api/datasets/export?${buildQuery({ path, scope }, options)}`
      : `/api/datasets/export?${buildQuery({ path, ...index, ...proc }, options)}`,

  /** Address of the small picture of one plane (a tile of the gallery of a stack). */
  thumbUrl: (path: string, index: { t?: number; z?: number }, size: number, options: ReaderOptions) =>
    `/api/datasets/thumb?${buildQuery({ path, ...index, size }, options)}`,

  /** RELION's table of the classes whose averages are in `path`, when there is one. */
  classes: (path: string, signal?: AbortSignal) =>
    getJson<ClassesResponse>(`/api/cryo/classes?${buildQuery({ path })}`, signal),

  /** The particles a coordinate file holds for an image. */
  picks: (path: string, picks: string, options: ReaderOptions, signal?: AbortSignal) =>
    getJson<PicksResponse>(`/api/cryo/picks?${buildQuery({ path, picks }, options)}`, signal),

  live: {
    watches: (signal?: AbortSignal) =>
      getJson<{ watches: LiveWatch[] }>("/api/live/watches", signal).then((r) => r.watches),

    /** Watch a directory; watching it again returns the same watch. */
    start: (path: string, signal?: AbortSignal) =>
      getJson<LiveWatch>(`/api/live/watches?${buildQuery({ path })}`, signal, "POST"),

    stop: (id: number, signal?: AbortSignal) => getJson<LiveWatch>(`/api/live/watches/${id}`, signal, "DELETE"),

    /** Stop a watch and take it off the list; the index of its directory goes once nobody watches it. */
    forget: (id: number, signal?: AbortSignal) =>
      getJson<LiveWatch>(`/api/live/watches/${id}?${buildQuery({ forget: true })}`, signal, "DELETE"),

    summary: (id: number, signal?: AbortSignal) => getJson<LiveSummary>(`/api/live/watches/${id}`, signal),

    /** Exposures that changed since sequence number `since`. */
    items: (id: number, since: number, limit: number, signal?: AbortSignal, category?: string) =>
      getJson<LivePage>(`/api/live/watches/${id}/items?${buildQuery({ since, limit, category })}`, signal),

    /** Replace the acceptance thresholds of a watch; a null threshold is not judged. */
    setQuality: (id: number, rules: QualityRules, signal?: AbortSignal) =>
      getJson<QualityRules>(
        `/api/live/watches/${id}/quality?${buildQuery({
          fit: rules.fit_max ?? undefined,
          ice: rules.ice_max ?? undefined,
          ring: rules.ring_max ?? undefined,
          drift: rules.drift_max ?? undefined,
          defocus_min: rules.defocus_min ?? undefined,
          defocus_max: rules.defocus_max ?? undefined,
        })}`,
        signal,
        "PUT",
      ),

    /** The counts of an empty hole and the mean free path (nm) of a watch; null forgets one. */
    setIce: (id: number, i0: number | null, mfp: number | null, signal?: AbortSignal) =>
      getJson<IceReference>(`/api/live/watches/${id}/ice?${buildQuery({ i0: i0 ?? undefined, mfp: mfp ?? undefined })}`, signal, "PUT"),

    history: (id: number, bucket: number, signal?: AbortSignal) =>
      getJson<LiveHistory>(`/api/live/watches/${id}/history?${buildQuery({ bucket })}`, signal),

    alerts: (id: number, signal?: AbortSignal) => getJson<LiveAlerts>(`/api/live/watches/${id}/alerts`, signal),

    /** Replace the alert rules of a watch; thresholds left at null are not watched. */
    setAlerts: (id: number, rules: AlertRules, signal?: AbortSignal) =>
      getJson<LiveAlerts>(
        `/api/live/watches/${id}/alerts?${buildQuery({
          channels: rules.channels.join(","),
          no_image: rules.no_image_min ?? undefined,
          disk_free: rules.disk_free_pct ?? undefined,
          rate_drop: rules.rate_drop_pct ?? undefined,
        })}`,
        signal,
        "PUT",
      ),

    /** Send a test notice to the channels of a watch. */
    testAlerts: (id: number, signal?: AbortSignal) =>
      getJson<{ results: AlertTestResult[] }>(`/api/live/watches/${id}/alerts/test`, signal, "POST").then((r) => r.results),

    /** Address of the thumbnail of a file, for an <img>. */
    thumbUrl: (path: string, size = 256) => `/api/live/thumb?${buildQuery({ path, size })}`,
  },
};
