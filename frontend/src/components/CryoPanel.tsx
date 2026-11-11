import { useEffect, useState } from "react";
import { api, isAbort } from "../api/client";
import type { DatasetInfo, DirEntry, GainCheck, PicksResponse, ReaderOptions } from "../api/types";
import {
  averageEnd,
  type CryoSettings,
  dirname,
  formatResolution,
  gainCandidates,
  gainOf,
  groupForDose,
  LOWPASS_PRESETS,
  ORIENTATION_LABELS,
  pickCandidates,
  pickDiameter,
  scoreRange,
} from "../viewer/cryo";

interface FieldProps {
  value: number | null;
  onCommit: (value: number | null) => void;
  label: string;
  testid: string;
  min?: number;
  max?: number;
  integer?: boolean;
  placeholder?: string;
}

/** A number typed freely and applied on Enter or when the field is left: every value applied
 *  costs a computation on the server, the digits typed on the way must not. */
function NumberField({ value, onCommit, label, testid, min, max, integer, placeholder }: FieldProps) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));
  useEffect(() => setDraft(value === null ? "" : String(value)), [value]);
  const commit = () => {
    const text = draft.trim().replace(",", ".");
    let next: number | null = text === "" ? null : Number(text);
    if (next !== null) {
      if (!Number.isFinite(next) || next <= 0) next = value; // not a usable number: keep the previous one
      else {
        if (integer) next = Math.round(next);
        if (min !== undefined) next = Math.max(min, next);
        if (max !== undefined) next = Math.min(max, next);
      }
    }
    setDraft(next === null ? "" : String(next));
    if (next !== value) onCommit(next);
  };
  return (
    <input inputMode="decimal" value={draft} placeholder={placeholder} aria-label={label} data-testid={testid}
      onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()} />
  );
}

interface PicksProps {
  path: string;
  /** Width of the image in pixels: the default size of the circles follows it. */
  width: number;
  cryo: CryoSettings;
  onChange: (patch: Partial<CryoSettings>) => void;
  /** What the server read in the chosen file for this image; null: not read (yet). */
  picks: PicksResponse | null;
  failure: string | null;
}

/** Particle coordinates over the image: the files next to it that bear its name are offered;
 *  any other is chosen in the file list on the left. */
function PicksSection({ path, width, cryo, onChange, picks, failure }: PicksProps) {
  const [candidates, setCandidates] = useState<DirEntry[]>([]);
  const dir = dirname(path);
  useEffect(() => {
    const controller = new AbortController();
    api
      .list(dir, 0, 1000, controller.signal)
      .then((l) => !controller.signal.aborted && setCandidates(pickCandidates(l.entries, path)), () => setCandidates([]));
    return () => controller.abort();
  }, [dir, path]);

  const range = scoreRange(picks);
  const name = cryo.picks?.split("/").pop() ?? "";
  return (
    <>
      <div className="field-label">Pointés</div>
      {!cryo.picks && candidates.length === 0 && (
        <div className="hint" data-testid="picks-hint">Cliquez un fichier .star, .box, .cbox ou .cs dans la liste de gauche.</div>
      )}
      {!cryo.picks &&
        candidates.map((c) => (
          <button key={c.path} className="wide" data-testid="picks-candidate" title={c.path} onClick={() => onChange({ picks: c.path, picksMinScore: null })}>
            {c.name}
          </button>
        ))}
      {cryo.picks && (
        <>
          <div className="chosen" title={cryo.picks}>
            <span className="name" data-testid="picks-file">{name}</span>
            <button aria-label="Retirer les pointés" data-testid="picks-clear" onClick={() => onChange({ picks: null, picksMinScore: null })}>×</button>
          </div>
          {failure && <div className="note warn" data-testid="picks-status">{failure}</div>}
          {!failure && !picks && <div className="note" data-testid="picks-status">Lecture…</div>}
          {picks && (
            <div className="note" data-testid="picks-status">
              {picks.count.toLocaleString("fr-FR")} particule{picks.count > 1 ? "s" : ""} sur cette image
              {picks.total > picks.count ? ` (${picks.total.toLocaleString("fr-FR")} dans le fichier)` : ""}
            </div>
          )}
          {picks?.notes.map((note) => (
            <div key={note} className="note warn" data-testid="picks-note">{note}</div>
          ))}
          <label className="check" title="Les lignes sont comptées depuis le haut de l'image telle qu'elle est enregistrée ; certains programmes comptent depuis le bas.">
            <input type="checkbox" checked={cryo.picksFlipY} data-testid="picks-flip" onChange={(e) => onChange({ picksFlipY: e.target.checked })} />
            Retourner Y
          </label>
          <label className="threshold">
            <span>Diamètre</span>
            <NumberField value={cryo.picksDiameter} onCommit={(v) => onChange({ picksDiameter: v })} label="Diamètre des cercles (pixels)"
              testid="picks-diameter" min={2} max={4096} placeholder={String(Math.round(pickDiameter(cryo, picks, width)))} />
            <span>px</span>
          </label>
          {range && (
            <label className="threshold" title="Masquer les particules sous ce score">
              <span>Score ≥</span>
              <input type="range" min={range[0]} max={range[1]} step={(range[1] - range[0]) / 100} data-testid="picks-score"
                value={cryo.picksMinScore ?? range[0]} onChange={(e) => onChange({ picksMinScore: Number(e.target.value) })} />
              <span>{(cryo.picksMinScore ?? range[0]).toFixed(2)}</span>
            </label>
          )}
        </>
      )}
    </>
  );
}

interface GainProps {
  path: string;
  etag: string;
  options: ReaderOptions;
  cryo: CryoSettings;
  onChange: (patch: Partial<CryoSettings>) => void;
  /** Name of the gain file given by the movie itself (EER acquisition metadata), if any. */
  named: string | null;
}

/** Choice of a gain reference among the files next to the movie, and the check of its
 *  orientation: nothing in the files says how a reference is turned relative to the frames. */
function GainSection({ path, etag, options, cryo, onChange, named }: GainProps) {
  const [candidates, setCandidates] = useState<DirEntry[]>([]);
  const [check, setCheck] = useState<GainCheck | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const dir = dirname(path);
  const optionsKey = JSON.stringify(options);

  // gain files in the directory of the movie and one level up
  useEffect(() => {
    const controller = new AbortController();
    const dirs = dirname(dir) ? [dir, dirname(dir)] : [dir];
    Promise.all(dirs.map((d) => api.list(d, 0, 1000, controller.signal).then((l) => l.entries, () => [])))
      .then((lists) => !controller.signal.aborted && setCandidates(gainCandidates(lists.flat(), path, named ?? undefined)));
    return () => controller.abort();
  }, [dir, path, named]);

  useEffect(() => {
    setCheck(null);
    setFailure(null);
    if (!cryo.gain) return;
    const controller = new AbortController();
    api
      .gainCheck(path, gainOf(cryo), options, controller.signal)
      .then(setCheck)
      .catch((e) => !isAbort(e) && setFailure(e instanceof Error ? e.message : String(e)));
    return () => controller.abort();
    // options are compared by value
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, etag, optionsKey, cryo.gain, cryo.gainOrient, cryo.gainInvert]);

  const known = candidates.some((c) => c.path === cryo.gain);
  if (!candidates.length && !cryo.gain) return null; // nothing to offer here

  let status = <div className="note" data-testid="gain-status">Vérification de l'orientation…</div>;
  if (failure) {
    status = <div className="note warn" data-testid="gain-status">{failure}</div>;
  } else if (check && !check.confident) {
    status = (
      <div className="note warn" data-testid="gain-status">
        Orientation non vérifiable sur ce film : trop peu de dose, ou un gain sans motif.
      </div>
    );
  } else if (check && !check.current_is_best) {
    status = (
      <div className="note warn" data-testid="gain-status">
        Ce gain n'est pas enregistré dans l'orientation du film : il l'aplanit mieux en « {ORIENTATION_LABELS[check.best]} ».{" "}
        <button data-testid="gain-fix" onClick={() => onChange({ gainOrient: check.best })}>Corriger</button>
      </div>
    );
  } else if (check && !check.improves) {
    status = (
      <div className="note warn" data-testid="gain-status">
        Ce gain n'aplanit pas l'image : est-ce bien celui de ce détecteur ?
      </div>
    );
  } else if (check) {
    status = (
      <div className="note" data-testid="gain-status">
        Orientation vérifiée sur {check.frames} frame{check.frames > 1 ? "s" : ""} · {check.defects} pixel{check.defects > 1 ? "s" : ""} défectueux
      </div>
    );
  }

  return (
    <>
      <div className="field-label">Référence de gain</div>
      <select className={`wide ${cryo.gain ? "on" : ""}`} value={cryo.gain ?? ""} aria-label="Référence de gain" data-testid="gain-select"
        onChange={(e) => onChange({ gain: e.target.value || null, gainOrient: 0, gainInvert: false })}>
        <option value="">Aucune</option>
        {candidates.map((c) => (
          <option key={c.path} value={c.path}>
            {dirname(c.path) === dir ? c.name : `../${c.name}`}
            {c.name === named ? " (nommé par le film)" : ""}
          </option>
        ))}
        {cryo.gain && !known && <option value={cryo.gain}>{cryo.gain.slice(cryo.gain.lastIndexOf("/") + 1)}</option>}
      </select>
      {cryo.gain && (
        <>
          <select className="wide" value={cryo.gainOrient} aria-label="Orientation du gain" data-testid="gain-orient"
            onChange={(e) => onChange({ gainOrient: Number(e.target.value) })}>
            {ORIENTATION_LABELS.map((label, k) => (
              <option key={label} value={k}>Orientation : {label}</option>
            ))}
          </select>
          <label className="check">
            <input type="checkbox" checked={cryo.gainInvert} data-testid="gain-invert"
              onChange={(e) => onChange({ gainInvert: e.target.checked })} />
            image de coups à inverser (champ plat)
          </label>
          {status}
        </>
      )}
    </>
  );
}

interface Props {
  /** Logical path and version of the file on screen. */
  path: string;
  etag: string;
  info: DatasetInfo;
  cryo: CryoSettings;
  onChange: (patch: Partial<CryoSettings>) => void;
  /** Current frame (0-based). */
  t: number;
  /** Å per pixel in effect, or null when unknown. */
  apix: number | null;
  /** Why the filters are not applied, or null. */
  problem: string | null;
  options: ReaderOptions;
  onOptions: (options: ReaderOptions) => void;
  /** Particles read in the coordinate file for this image, and why they could not be. */
  picks: PicksResponse | null;
  picksError: string | null;
}

/** Cryo-EM tools: resolution filters, frame average, EER grouping, picks, power spectrum. */
export function CryoPanel({ path, etag, info, cryo, onChange, t, apix, problem, options, onOptions, picks, picksError }: Props) {
  const frames = info.shape.t;
  const end = averageEnd(t, cryo.average, frames);
  const fractions = typeof info.metadata.n_fractions === "number" ? info.metadata.n_fractions : null;
  const group = typeof info.metadata.group === "number" ? info.metadata.group : null;
  const custom = cryo.lowpass !== null && !LOWPASS_PRESETS.includes(cryo.lowpass);
  const setGroup = (v: number | null) => {
    const { group: _dropped, ...rest } = options;
    onOptions(v === null ? rest : { ...rest, group: v });
  };
  // dose of one fraction, in electrons per detector pixel, when the movie gives it
  const dose = info.metadata.dose_per_fraction as { value?: unknown; unit?: unknown } | null | undefined;
  const sampling = typeof info.metadata.sampling === "number" ? info.metadata.sampling : 1;
  const doseGroup =
    dose && typeof dose.value === "number" && dose.unit === "e/pixel" && apix && fractions !== null
      ? groupForDose(dose.value, apix, sampling, fractions)
      : null;
  const acquisition = info.metadata.acquisition as Record<string, unknown> | null | undefined;
  const namedGain = typeof acquisition?.eerGainReference === "string" ? acquisition.eerGainReference : null;

  return (
    <section className="panel" data-testid="cryo">
      <div className="panel-title">Cryo</div>

      <div className="field-label">Passe-bas</div>
      <div className="buttons tight">
        <button className={cryo.lowpass === null ? "on" : ""} data-testid="lp-off" onClick={() => onChange({ lowpass: null })}>Aucun</button>
        {LOWPASS_PRESETS.map((d) => (
          <button key={d} className={cryo.lowpass === d ? "on" : ""} data-testid={`lp-${d}`} onClick={() => onChange({ lowpass: d })}>{d} Å</button>
        ))}
        <label className={`unit ${custom ? "on" : ""}`}>
          <NumberField value={cryo.lowpass} onCommit={(v) => onChange({ lowpass: v })} label="Passe-bas en ångströms" testid="lp-input" placeholder="autre" />
          Å
        </label>
      </div>

      <div className="field-label">Passe-haut</div>
      <div className="buttons tight">
        <button className={cryo.highpass === null ? "on" : ""} data-testid="hp-off" onClick={() => onChange({ highpass: null })}>Aucun</button>
        <label className={`unit ${cryo.highpass !== null ? "on" : ""}`}>
          <NumberField value={cryo.highpass} onCommit={(v) => onChange({ highpass: v })} label="Passe-haut en ångströms" testid="hp-input" placeholder="500" />
          Å
        </label>
      </div>
      {problem && <div className="warn note" data-testid="cryo-problem">{problem}</div>}
      {!problem && apix && (cryo.lowpass !== null || cryo.highpass !== null) && (
        <div className="note" data-testid="cryo-filter">
          Filtre gaussien, gain ½ à la coupure · Nyquist {formatResolution(2 * apix)}
        </div>
      )}

      {frames > 1 && (
        <>
          <div className="field-label">Moyenne de frames</div>
          <div className="buttons tight">
            <button className={cryo.average <= 1 ? "on" : ""} data-testid="avg-off" onClick={() => onChange({ average: 1 })}>Aucune</button>
            <label className={`unit ${cryo.average > 1 ? "on" : ""}`}>
              <NumberField value={cryo.average > 1 ? cryo.average : null} onCommit={(v) => onChange({ average: v ?? 1 })}
                label="Nombre de frames moyennées" testid="avg-input" integer min={1} max={frames} placeholder="n" />
              frames
            </label>
            <button data-testid="avg-all" title="Moyenne de toutes les frames à partir de la frame courante"
              onClick={() => onChange({ average: frames })}>Toutes</button>
          </div>
          <div className="note" data-testid="avg-range">
            {end !== null ? `Moyenne des frames ${t + 1} à ${end + 1} (sur ${frames})` : `Frame ${t + 1} seule`}
          </div>
        </>
      )}

      {info.reader === "eer" && fractions !== null && group !== null && (
        <>
          <div className="field-label">Fractions EER par frame</div>
          <div className="buttons tight">
            <label className="unit">
              <NumberField value={group} label="Fractions sommées par frame" testid="eer-group" integer min={1} max={fractions}
                onCommit={setGroup} />
              / {fractions}
            </label>
            <button data-testid="eer-dose" disabled={doseGroup === null} onClick={() => setGroup(doseGroup)}
              title={doseGroup === null
                ? "Demande la dose par fraction (métadonnées du film) et la taille de pixel"
                : `Environ 1 électron par Å² dans chaque frame : ${doseGroup} fraction${doseGroup > 1 ? "s" : ""}`}>
              ≈ 1 e⁻/Å²
            </button>
          </div>
        </>
      )}

      <GainSection path={path} etag={etag} options={options} cryo={cryo} onChange={onChange} named={namedGain} />
      <PicksSection path={path} width={info.shape.x} cryo={cryo} onChange={onChange} picks={picks} failure={picksError} />

      <div className="buttons">
        <button className={cryo.spectrum ? "on" : ""} data-testid="spectrum-toggle" title="Spectre de puissance (P)"
          onClick={() => onChange({ spectrum: !cryo.spectrum })}>Spectre de puissance</button>
      </div>
    </section>
  );
}
