import { useEffect, useState } from "react";
import type { DatasetInfo, ReaderOptions } from "../api/types";
import { averageEnd, type CryoSettings, formatResolution, LOWPASS_PRESETS } from "../viewer/cryo";

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

interface Props {
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
}

/** Cryo-EM tools: resolution filters, frame average, EER grouping, power spectrum. */
export function CryoPanel({ info, cryo, onChange, t, apix, problem, options, onOptions }: Props) {
  const frames = info.shape.t;
  const end = averageEnd(t, cryo.average, frames);
  const fractions = typeof info.metadata.n_fractions === "number" ? info.metadata.n_fractions : null;
  const group = typeof info.metadata.group === "number" ? info.metadata.group : null;
  const custom = cryo.lowpass !== null && !LOWPASS_PRESETS.includes(cryo.lowpass);

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
                onCommit={(v) => {
                  const { group: _dropped, ...rest } = options;
                  onOptions(v === null ? rest : { ...rest, group: v });
                }} />
              / {fractions}
            </label>
          </div>
        </>
      )}

      <div className="buttons">
        <button className={cryo.spectrum ? "on" : ""} data-testid="spectrum-toggle" title="Spectre de puissance (P)"
          onClick={() => onChange({ spectrum: !cryo.spectrum })}>Spectre de puissance</button>
      </div>
    </section>
  );
}
