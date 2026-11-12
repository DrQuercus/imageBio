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
  /** Every group of the user: those of the proxy and those given in the settings page. */
  groups: string[];
  /** The groups given in the settings page. */
  groups_given: string[];
  /** May open the administration of the settings page. */
  admin: boolean;
  auth_mode: "none" | "proxy";
  roots: { name: string; available: boolean }[];
}

/** A data root as the settings page edits it (paths are the server's: administrators only). */
export interface RootSetting {
  name: string;
  path: string;
  groups: string[];
  kv?: number | null;
  cs_mm?: number | null;
  amplitude_contrast?: number | null;
  /** Read only: whether the server can read the directory. */
  available?: boolean;
}

export interface ChannelSetting {
  name: string;
  kind: "ntfy" | "webhook" | "email";
  url?: string;
  to?: string[];
}

export interface MicroscopeSetting {
  kv: number;
  cs_mm: number;
  amplitude_contrast: number;
}

export interface LiveSetting {
  report_after_idle_min: number;
  mean_free_path_nm: number | null;
  metrics: boolean;
  ctf: boolean;
  drift: boolean;
}

export type SettingSource = "file" | "interface";

export interface AccountRow {
  name: string;
  /** Groups given in the settings page. */
  groups: string[];
  /** Groups the proxy gave the last time the user came (since the server started). */
  proxy_groups: string[];
  /** Why the user is an administrator: named in the file, by a group of the file, given in the
   *  settings page; null: not one. */
  admin: "file" | "group" | "interface" | null;
  seen: number | null;
  /** Personal keys still valid (ADR 015). */
  keys: number;
}

/** A personal key (ADR 015), without the key itself: it is shown once, when it is made. */
export interface KeyInfo {
  id: string;
  name: string;
  /** The first characters of the key, to tell keys apart. */
  hint: string;
  created: number;
  expires: number;
  last_used: number | null;
}

export interface MadeKey extends KeyInfo {
  /** The key itself: in this answer only. */
  key: string;
}

export interface LogEntry {
  at: number;
  by: string;
  version?: number;
  what: string;
}

export interface AdminState {
  version: number;
  updated_at: number | null;
  updated_by: string | null;
  sections: {
    roots: { source: SettingSource; value: RootSetting[] };
    microscope: { source: SettingSource; value: MicroscopeSetting; file: MicroscopeSetting; own: Partial<MicroscopeSetting> };
    live: { source: SettingSource; value: LiveSetting; file: LiveSetting; own: Partial<LiveSetting> };
    alerts: { source: SettingSource; value: ChannelSetting[]; email: boolean };
  };
  root_parents: string[];
  accounts: AccountRow[];
  groups: string[];
  configuration: Record<string, Record<string, unknown> | string | null>;
}

export type SettingSection = keyof AdminState["sections"];

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
  /** Reduce the plane by keeping the low frequencies of its transform, not by block means.
   *  A filtered view is always reduced that way (ADR 013). */
  fbin?: boolean;
  /** Sharp filter profile (Butterworth of order 4) instead of the Gaussian one. */
  sharp?: boolean;
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
    epu?: {
      pixel_size?: number;
      dose?: number;
      applied_defocus?: number;
      /** Stage position, µm. */
      stage?: { x?: number; y?: number };
      [key: string]: unknown;
    };
    mdoc?: { tilt_angle?: number; dose?: number; defocus?: number; [key: string]: unknown };
    image?: { x: number; y: number; frames: number; dtype: string; pixel_size: number | null };
    movie?: { x: number; y: number; frames: number; dtype: string; pixel_size: number | null };
  };
  /** Measured on the pixels, a moment after the image is complete. null: not measured (yet);
   *  empty: the image could not be read. */
  metrics: ImageMetrics | null;
  /** What the programs declared on the server gave (motion correction, CTF), when the user of
   *  a watch asked for them. null: not run (yet). */
  processing: ProgramResults | null;
}

/** A step that gave no result says why, in a few words. */
export interface ProgramError {
  error: string;
}

/** Frame shifts of a motion correction, summed up: path of the specimen over the movie, over
 *  its first frames, largest step between two frames ("A": ångströms, "px": pixels). */
export interface MotionResult {
  total: number;
  early: number;
  max_step: number;
  frames: number;
  unit: "A" | "px";
}

/** The fit of a CTF program: defocus along both axes and their mean (µm), astigmatism (µm) and
 *  its angle (°), resolution (Å) up to which the rings were fitted. */
export interface CtfFit {
  defocus: number;
  defocus_u: number;
  defocus_v: number;
  astigmatism: number;
  angle?: number;
  phase?: number;
  cc?: number;
  fit?: number;
  tilt?: number;
  /** Sample thickness, nm (CTFFIND 5). */
  thickness?: number;
}

export interface ProgramResults {
  motion?: MotionResult | ProgramError;
  ctf?: CtfFit | ProgramError;
  seconds?: number;
  /** The programs could not be run at all. */
  error?: string;
}

/** Whether the server has programs to run on the exposures, whether this watch asked for
 *  them, and how far they are. */
export interface ProcessingState {
  available: boolean;
  steps: ("motion" | "ctf")[];
  on: boolean;
  done: number;
  waiting: number;
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
  /** Acceptance thresholds of the watch; null: not judged. */
  quality: QualityRules;
  processing: ProcessingState;
  /** What the CTF of this session is estimated with (the voltage of a file comes first). */
  microscope: MicroscopeState;
  /** Enough to notice that a report was made (one made by itself, at the end of a silence). */
  reports: { count: number; last: number | null; auto: boolean };
}

/** Where a microscope parameter of a session comes from. */
export type MicroscopeSource = "session" | "root" | "server";

export interface MicroscopeState {
  kv: { value: number; source: MicroscopeSource };
  cs_mm: { value: number; source: MicroscopeSource };
  amplitude: { value: number; source: MicroscopeSource };
  /** What was set for this session; null: not set. */
  own: { kv: number | null; cs_mm: number | null; amplitude: number | null };
  set_at: number | null;
  /** After a change: how many images are measured again. */
  remeasure?: number;
}

/** One quality report of a watch. */
export interface ReportInfo {
  id: number;
  created_at: number;
  trigger: "manual" | "stop" | "idle";
  bytes: number;
  exposures: number;
  newest: number | null;
}

export interface AtlasRef {
  name: string;
  /** Logical path. */
  path: string;
}

export interface ReportListing {
  /** Whether a report is made when the watch ends. */
  auto: boolean;
  /** Minutes of silence after which a report is made; 0: never. */
  idle_min: number;
  reports: ReportInfo[];
  /** The atlas the reports show: the one chosen for this watch, else the one found in the
   *  session (EPU's stitched Atlas_1), else none. */
  atlas: { chosen: AtlasRef | null; found: AtlasRef | null };
}

export type ReportKind = "html" | "json" | "csv" | "zip";

/** What the user of a watch calls a good image (live/quality.py). */
export interface QualityRules {
  /** Thon rings must be found at least this far (Å). */
  fit_max: number | null;
  /** Relative ice, ln(I₀/I), not to be exceeded. */
  ice_max: number | null;
  ring_max: number | null;
  /** Drift between the first and last frames, Å. */
  drift_max: number | null;
  /** Bounds of the measured defocus, µm. */
  defocus_min: number | null;
  defocus_max: number | null;
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
