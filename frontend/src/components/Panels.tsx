import type { DatasetInfoResponse, PlaneIndex, PlaneStats } from "../api/types";
import type { HoverInfo } from "../viewer/ViewerCanvas";
import { formatValue } from "./ContrastPanel";

const AXES: { key: keyof PlaneIndex; size: "t" | "c" | "z"; label: string }[] = [
  { key: "t", size: "t", label: "Frame" },
  { key: "z", size: "z", label: "Coupe" },
  { key: "c", size: "c", label: "Canal" },
];

interface StackProps {
  info: DatasetInfoResponse["info"];
  index: PlaneIndex;
  onChange: (index: PlaneIndex) => void;
  playing: boolean;
  onPlay: (playing: boolean) => void;
  fps: number;
  onFps: (fps: number) => void;
  /** Last frame of the average shown (it starts at the current frame), or null. */
  averageEnd: number | null;
}

/** Sliders for the axes that have more than one plane, and the player. */
export function StackControls({ info, index, onChange, playing, onPlay, fps, onFps, averageEnd }: StackProps) {
  const axes = AXES.filter((a) => info.shape[a.size] > 1 && !(a.key === "c" && info.rgb));
  if (!axes.length) return null;
  const canPlay = info.shape.t > 1 || info.shape.z > 1;
  return (
    <section className="panel" data-testid="stack">
      <div className="panel-title">Pile</div>
      {axes.map((a) => (
        <label key={a.key} className="slider">
          <span data-testid={`${a.key}-label`}>
            {a.key === "t" && averageEnd !== null
              ? `Frames ${index.t + 1}–${averageEnd + 1} / ${info.shape.t}`
              : `${a.label} ${index[a.key] + 1} / ${info.shape[a.size]}`}
          </span>
          <input type="range" min={0} max={info.shape[a.size] - 1} value={index[a.key]} data-testid={`${a.key}-slider`}
            onChange={(e) => onChange({ ...index, [a.key]: Number(e.target.value) })} />
        </label>
      ))}
      {canPlay && (
        <div className="buttons">
          <button className={playing ? "on" : ""} data-testid="play" title="Lecture (Espace)" onClick={() => onPlay(!playing)}>
            {playing ? "❚❚ Pause" : "▶ Lecture"}
          </button>
          <label className="fps">
            <input type="number" min={1} max={60} value={fps} aria-label="Images par seconde"
              onChange={(e) => onFps(Math.min(60, Math.max(1, e.target.valueAsNumber || 1)))} />
            img/s
          </label>
        </div>
      )}
    </section>
  );
}

const SOURCES: Record<string, string> = {
  header: "en-tête du fichier",
  extended_header: "en-tête étendu",
  metadata: "métadonnées",
  sidecar: "fichier annexe",
  user: "saisie",
  default: "inconnue",
};

interface InfoProps {
  response: DatasetInfoResponse;
  stats: PlaneStats | null;
  userApix: number | null;
  onUserApix: (apix: number | null) => void;
}

/** What is known about the file, including where its pixel size comes from. */
export function InfoPanel({ response, stats, userApix, onUserApix }: InfoProps) {
  const { info, file } = response;
  const cal = info.calibration.x;
  const s = info.shape;
  const dims = [`${s.x} × ${s.y}`, s.z > 1 && `Z ${s.z}`, s.t > 1 && `T ${s.t}`, s.c > 1 && `C ${s.c}`].filter(Boolean).join(" · ");
  return (
    <section className="panel" data-testid="info">
      <div className="panel-title">Fichier</div>
      <dl>
        <dt>Nom</dt>
        <dd title={response.path}>{file.name}</dd>
        <dt>Dimensions</dt>
        <dd>{dims}</dd>
        <dt>Type</dt>
        <dd>{info.format.toUpperCase()} · {info.dtype} · {info.stack_kind}</dd>
        <dt>Pixel</dt>
        <dd data-testid="pixel-size" className={cal.known ? "" : "warn"}>
          {cal.known ? `${formatValue(cal.value)} ${cal.unit}/px (${SOURCES[cal.source] ?? cal.source})` : "taille inconnue"}
        </dd>
        {!cal.known && (
          <>
            <dt>Å/px</dt>
            <dd>
              <input type="number" step="any" min={0} placeholder="à saisir" value={userApix ?? ""} data-testid="user-apix"
                aria-label="Taille de pixel en ångströms"
                onChange={(e) => onUserApix(e.target.valueAsNumber > 0 ? e.target.valueAsNumber : null)} />
            </dd>
          </>
        )}
        {stats && (
          <>
            <dt>Moyenne</dt>
            <dd data-testid="mean-std">{formatValue(stats.mean)} ± {formatValue(stats.std)}</dd>
            <dt>Min / max</dt>
            <dd>{formatValue(stats.min)} / {formatValue(stats.max)}</dd>
            {stats.n_nan + stats.n_posinf + stats.n_neginf > 0 && (
              <>
                <dt>Non finis</dt>
                <dd className="warn">{stats.n_nan} NaN, {stats.n_posinf + stats.n_neginf} Inf</dd>
              </>
            )}
          </>
        )}
      </dl>
      {info.warnings.length > 0 && (
        <ul className="warnings" data-testid="warnings">
          {info.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

interface StatusProps {
  hover: HoverInfo | null;
  zoom: number | null;
  level: number;
  ruler: string | null;
}

export function StatusBar({ hover, zoom, level, ruler }: StatusProps) {
  return (
    <footer className="status" data-testid="status">
      <span data-testid="hover-xy">{hover ? `x ${hover.x}  y ${hover.y}` : "x —  y —"}</span>
      <span data-testid="hover-value">
        {hover && hover.values.length ? hover.values.map(formatValue).join(", ") : "—"}
        {hover && hover.bin > 1 ? ` (moyenne ${hover.bin}×${hover.bin})` : ""}
      </span>
      {ruler && <span data-testid="ruler-length">{ruler}</span>}
      <span className="spacer" />
      <span data-testid="level">{level > 1 ? `binning ${level}×` : "pleine résolution"}</span>
      <span data-testid="zoom">{zoom ? `${(zoom * 100).toFixed(zoom < 0.1 ? 1 : 0)} %` : ""}</span>
    </footer>
  );
}
