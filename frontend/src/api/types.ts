/** Shapes of the backend JSON (see backend/src/imagebio/core/model.py). */

export interface AxisCalibration {
  value: number;
  unit: string;
  source: "header" | "extended_header" | "metadata" | "sidecar" | "user" | "default";
  known: boolean;
  angstrom: number | null;
}

export interface Calibration {
  x: AxisCalibration;
  y: AxisCalibration;
  z: AxisCalibration | null;
  t: AxisCalibration | null;
}

export interface DatasetInfo {
  format: string;
  reader: string;
  shape: { t: number; c: number; z: number; y: number; x: number };
  dtype: Dtype;
  stack_kind: "image" | "movie" | "stack" | "tilt_series" | "volume" | "hyperstack";
  rgb: boolean;
  channels: { name: string | null; color: [number, number, number] | null }[];
  calibration: Calibration;
  metadata: Record<string, unknown>;
  warnings: string[];
}

export interface DatasetInfoResponse {
  path: string;
  file: { name: string; size: number; mtime: number; etag: string };
  info: DatasetInfo;
}

export interface PlaneStats {
  min: number;
  max: number;
  mean: number;
  std: number;
  n_finite: number;
  n_nan: number;
  n_posinf: number;
  n_neginf: number;
  robust: [number, number];
  auto: [number, number];
  histogram: { lo: number; hi: number; counts: number[] };
}

export interface DirEntry {
  name: string;
  path: string;
  kind: "dir" | "file" | "broken" | "other";
  size: number;
  mtime: number;
  is_symlink: boolean;
  supported: boolean;
}

export interface DirListing {
  path: string;
  parent: string | null;
  entries: DirEntry[];
  total: number;
  offset: number;
  limit: number;
}

export interface Me {
  user: string;
  groups: string[];
  auth_mode: "none" | "proxy";
}

export type Dtype = "uint8" | "int8" | "uint16" | "int16" | "uint32" | "int32" | "float32" | "float64";

export type PlaneArray =
  | Uint8Array
  | Int8Array
  | Uint16Array
  | Int16Array
  | Uint32Array
  | Int32Array
  | Float32Array
  | Float64Array;

/** x, y, w, h in full-resolution image pixels. */
export type Region = [number, number, number, number];

/** A decoded plane as served by /api/datasets/plane. */
export interface Plane {
  data: PlaneArray;
  width: number;
  height: number;
  dtype: Dtype;
  bin: number;
  region: Region;
}

export type ReaderOptions = Record<string, string | number | boolean>;

export interface PlaneIndex {
  t: number;
  c: number;
  z: number;
}

/** What the server does to a plane before cropping and binning it (service/planes.py). */
export interface Processing {
  /** The plane is the mean of frames t..t_end (inclusive). */
  t_end?: number;
  /** Low-pass and high-pass cut-offs, as resolutions in Å. */
  lp?: number;
  hp?: number;
  /** Å per pixel given by the user, for a file that does not store its pixel size. */
  apix?: number;
}

/** A power spectrum: side x side values, zero frequency at [side / 2, side / 2]. */
export interface Spectrum {
  data: Float32Array;
  side: number;
}

/** Rotational average of a power spectrum; frequencies in cycles per pixel (0 to 0.5). */
export interface SpectrumProfile {
  side: number;
  frequency: number[];
  power: number[];
}
