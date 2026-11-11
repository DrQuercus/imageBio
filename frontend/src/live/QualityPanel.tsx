import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { QualityRules } from "../api/types";
import { parseThreshold } from "./store";

interface Props {
  watchId: number;
  rules: QualityRules;
  /** Mean free path in nm, when known: the ice threshold is then also shown in nm. */
  mfp: number | null;
  onError: (message: string) => void;
}

const FIELDS: { key: keyof QualityRules; label: string; unit: string; min: number; max: number; testid: string; title: string }[] = [
  { key: "fit_max", label: "CTF jusqu'à", unit: "Å", min: 1, max: 50, testid: "quality-fit", title: "Les anneaux de Thon doivent être retrouvés au moins jusqu'à cette résolution" },
  { key: "ice_max", label: "Glace ≤", unit: "rel.", min: 0.01, max: 10, testid: "quality-ice", title: "Glace relative ln(I₀/I) à ne pas dépasser" },
  { key: "ring_max", label: "Anneau ≤", unit: "", min: 1, max: 100, testid: "quality-ring", title: "Rapport de l'anneau de glace à ne pas dépasser (1,2 : glace cristalline)" },
  { key: "drift_max", label: "Dérive ≤", unit: "Å", min: 0.1, max: 1000, testid: "quality-drift", title: "Dérive entre le début et la fin du film (si le serveur la mesure)" },
  { key: "defocus_min", label: "Défocus ≥", unit: "µm", min: 0, max: 50, testid: "quality-defocus-min", title: "Défocus mesuré le plus faible accepté" },
  { key: "defocus_max", label: "Défocus ≤", unit: "µm", min: 0, max: 50, testid: "quality-defocus-max", title: "Défocus mesuré le plus fort accepté" },
];

type Drafts = Record<keyof QualityRules, string>;
const draftsOf = (rules: QualityRules): Drafts =>
  Object.fromEntries(FIELDS.map((f) => [f.key, rules[f.key]?.toString() ?? ""])) as Drafts;

/** The thresholds an image must pass to be accepted. They are kept by the server with the
 *  session; the judgement is made here, on the measurements each tile already has. */
export function QualityPanel({ watchId, rules, mfp, onError }: Props) {
  const [drafts, setDrafts] = useState<Drafts>(draftsOf(rules));
  const [saved, setSaved] = useState(false);
  const key = JSON.stringify(rules);
  // the fields follow what the server holds, not every summary that arrives
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setDrafts(draftsOf(rules)), [watchId, key]);

  const parsed = FIELDS.map((f) => parseThreshold(drafts[f.key], f.min, f.max));
  const valid = parsed.every((v) => v !== undefined);
  const save = () => {
    if (!valid) return;
    const next = Object.fromEntries(FIELDS.map((f, k) => [f.key, parsed[k] ?? null])) as unknown as QualityRules;
    api.live
      .setQuality(watchId, next)
      .then(() => setSaved(true))
      .catch((e) => onError(e instanceof Error ? e.message : String(e)));
  };
  const ice = parseThreshold(drafts.ice_max, 0.01, 10);

  return (
    <section className="panel" data-testid="quality">
      <div className="panel-title">Seuils d'acceptation</div>
      {FIELDS.map((f, k) => (
        <label key={f.key} className={`threshold${parsed[k] === undefined ? " invalid" : ""}`} title={f.title}>
          <span>{f.label}</span>
          <input inputMode="decimal" value={drafts[f.key]} placeholder="—" aria-label={`${f.label} ${f.unit}`} data-testid={f.testid}
            onChange={(e) => { setSaved(false); setDrafts((d) => ({ ...d, [f.key]: e.target.value })); }} />
          <span>{f.key === "ice_max" && mfp && typeof ice === "number" ? `≈ ${Math.round(ice * mfp)} nm` : f.unit}</span>
        </label>
      ))}
      <div className="buttons">
        <button onClick={save} disabled={!valid} data-testid="quality-save">Enregistrer</button>
        {saved && <span className="note" data-testid="quality-saved">enregistré</span>}
      </div>
    </section>
  );
}
