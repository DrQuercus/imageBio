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
  /** Reduce the plane by keeping the low frequencies of its transform, not by block means. */
  fbin?: boolean;
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
  /** Entries of the defect list the reference carries (TIFF tag 65100 of a .gain file), which
   *  is not applied; null when it has none. */
  listed_defects: number | null;
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
  /** Measured on the pixels, a moment after the image is complete. null: not measured (yet);
   *  empty: the image could not be read. */
  metrics: ImageMetrics | null;
}

/** A quick estimate of the CTF of a plane (GET /api/datasets/ctf). Astigmatism is not measured. */
export interface CtfResponse {
  /** False when the spectrum shows no Thon rings: the other fields of the fit are then absent. */
  found: boolean;
  kv: number;
  cs_mm: number;
  amplitude: number;
  /** µm, positive: underfocus. */
  defocus_um?: number;
  /** Resolution (Å) up to which the rings agree with that defocus. */
  fit_resolution?: number;
  correlation?: number;
  score?: number;
  apix?: number;
  /** Resolutions (Å) of the zeros of the fit, coarsest first. */
  zeros?: number[];
}

/** One class of a 2D classification, from RELION's model file. */
export interface ClassInfo {
  /** Plane of the class averages, 0-based. */
  index: number;
  /** Share of the particles, 0 to 1. */
  distribution: number;
  /** Estimated resolution in Å; null for an empty class. */
  resolution: number | null;
}

export interface ClassesResponse {
  model: string | null;
  classes: ClassInfo[] | null;
}

/** Particle coordinates of one image (GET /api/cryo/picks). */
export interface PicksResponse {
  format: "star" | "box" | "cbox" | "cs";
  /** Particles in the answer; `total`: in the file for this image (the answer is bounded). */
  count: number;
  total: number;
  /** Centres in pixels of the image, rows counted from the first row of the file. */
  x: number[];
  y: number[];
  score: number[] | null;
  /** Size of the boxes in pixels, when the file has boxes. */
  box: number | null;
  /** The file names micrographs: it applies to other images too. */
  many: boolean;
  notes: string[];
  picks: string;
  image: { x: number; y: number };
}

export interface ImageMetrics {
  /** Counts per pixel over the exposure, hot pixels left out. */
  mean?: number;
  std?: number;
  /** Power where crystalline ice diffracts (3.4-3.95 Å) over the power on both sides: about 1
   *  on vitreous ice. Absent when the pixel is too coarse to tell. */
  ice_ring?: number;
  /** True for a movie without an integrated image: the mean is scaled from its first frames. */
  estimated?: boolean;
  frames?: number;
  /** Shift of the specimen between the first and the last frames of the movie, and its unit
   *  ("A": ångströms, "px": pixels of the movie when its pixel size is unknown). */
  drift?: number;
  drift_unit?: "A" | "px";
  /** Quick CTF estimate, when the image shows Thon rings: defocus (µm, positive underfocus),
   *  resolution (Å) up to which the rings follow, and how far the match stands above noise. */
  ctf?: { defocus: number; fit: number; score: number };
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
  /** Conditions an alert is firing for at the moment. */
  alerts: AlertCondition[];
  /** Images measured so far, and the mean counts of the brightest of them (the thinnest ice of
   *  the session, or none at all): what the others are compared to. null: too few yet. */
  ice: IceReference;
}

/** What the ice of a session is measured against (see relativeIce in live/store.ts). */
export interface IceReference {
  measured: number;
  /** Mean counts the images are compared to: `i0` when the user gave it, else `session`. */
  reference: number | null;
  /** True when `reference` is the counts of an empty hole: thickness is then absolute. */
  calibrated: boolean;
  /** The counts of the brightest images of the session; null: too few measured yet. */
  session: number | null;
  i0: number | null;
  /** Mean free path in nm (the user's, else the server's); null: relative values only. */
  mfp_nm: number | null;
  mfp_own: number | null;
}

export type AlertCondition = "no_image" | "disk" | "rate_drop" | "scanner";

/** What a user wants to be told about a session; a threshold at null is not watched. */
export interface AlertRules {
  /** Names of the channels notices go to, among those the server offers. */
  channels: string[];
  /** Minutes without a new image. */
  no_image_min: number | null;
  /** Free disk space, percent. */
  disk_free_pct: number | null;
  /** Rate over five minutes as a percentage of the hourly rate. */
  rate_drop_pct: number | null;
}

export interface LiveAlerts {
  rules: AlertRules;
  set_at: number | null;
  /** Channels configured on the server: names and kinds, never addresses. */
  channels: { name: string; kind: "ntfy" | "webhook" | "email" }[];
  firing: { condition: AlertCondition; since: number; notified: boolean; error: string | null }[];
}

export interface AlertTestResult {
  channel: string;
  delivered: boolean;
  error: string | null;
}

export interface LiveHistory {
  bucket: number;
  /** [start of the bucket (s), acquisitions]. Empty buckets are left out. */
  counts: [number, number][];
}
