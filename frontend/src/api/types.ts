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
  /** Gain reference (logical path), the orientation it is stored in (0 to 7) and whether it
   *  is a flat-field counts image to invert. */
  gain?: string;
  gain_orient?: number;
  gain_inv?: boolean;
}

/** Answer of /api/cryo/gain/check: which orientation of a gain flattens a movie best. */
export interface GainCheck {
  orientations: { index: number; name: string; score: number | null }[];
  best: number;
  best_name: string;
  current: number;
  current_is_best: boolean;
  /** Score of the runner-up over the score of the best. */
  ratio: number | null;
  confident: boolean;
  uncorrected: number | null;
  /** Whether the best orientation makes the image flatter than no gain at all. */
  improves: boolean;
  frames: number;
  defects: number;
  gain_shape: { x: number; y: number };
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
  /** null where the power overflowed. */
  power: (number | null)[];
}

// ── Live mode (backend/src/imagebio/live/service.py) ─────────────────────────

/** "User follows directory": kept by the server across restarts. */
export interface LiveWatch {
  id: number;
  /** Logical path of the watched directory. */
  path: string;
  active: boolean;
  created_at: number;
  stopped_at: number | null;
}

export type MemberState = "pending" | "complete" | "deleted";

/** One exposure of a session: the image, its movie, preview and metadata together. */
export interface LiveItem {
  id: number;
  /** Number of the last change to this exposure: clients ask for what changed since a number. */
  seq: number;
  name: string;
  dir: string;
  /** Logical path of the file a tile is drawn from; opens in the viewer. */
  path: string;
  category: "data" | "image" | "gridsquare" | "foilhole" | "atlas";
  state: "incoming" | "complete" | "deleted";
  role: "image" | "movie" | "preview";
  size: number | null;
  /** Acquisition time, seconds since the epoch, and where it comes from. */
  time: number | null;
  time_source: "xml" | "mdoc" | "name" | "mtime" | null;
  first_seen: number;
  completed_at: number | null;
  /** False: indexed from a directory at rest, without the completeness test. */
  verified: boolean;
  note: string | null;
  members: Partial<Record<"image" | "movie" | "preview" | "xml" | "mdoc", MemberState>>;
  meta: {
    epu?: { pixel_size?: number; dose?: number; applied_defocus?: number; [key: string]: unknown };
    mdoc?: { tilt_angle?: number; dose?: number; defocus?: number; [key: string]: unknown };
    image?: { x: number; y: number; frames: number; dtype: string; pixel_size: number | null };
    movie?: { x: number; y: number; frames: number; dtype: string; pixel_size: number | null };
  };
}

export interface LivePage {
  items: LiveItem[];
  /** Ask for the next changes with since = seq. */
  seq: number;
  more: boolean;
  head: number;
}

export interface LiveSummary {
  watch: LiveWatch;
  /** Server time, seconds since the epoch: "how long ago" is computed against it. */
  now: number;
  seq: number;
  scanner: {
    running: boolean;
    cycle_at: number | null;
    full_at: number | null;
    cycle_ms: number | null;
    error: string | null;
    /** Files being followed until they are complete. */
    pending: number;
  };
  counts: Record<string, Partial<Record<"incoming" | "complete", number>>>;
  acquired: number;
  incoming: number;
  /** Arrivals while watching, per hour. */
  rate: { last_5_min: number; last_hour: number };
  last: { name: string; time: number; completed_at: number | null } | null;
  disk: { total: number; free: number; percent: number | null } | null;
}

export interface LiveHistory {
  bucket: number;
  /** [start of the bucket (s), acquisitions]. Empty buckets are left out. */
  counts: [number, number][];
}
