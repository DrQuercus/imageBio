import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { MicroscopeSource, MicroscopeState } from "../api/types";
import { parseThreshold } from "./store";

interface Props {
  watchId: number;
  microscope: MicroscopeState;
  onError: (message: string) => void;
}

const SOURCES: Record<MicroscopeSource, string> = {
  session: "réglé pour la session",
  root: "dossier de données",
  server: "serveur",
};

const FIELDS = [
  { key: "kv", label: "Tension", unit: "kV", min: 20, max: 1500, testid: "microscope-kv" },
  { key: "cs_mm", label: "Cs", unit: "mm", min: 0, max: 20, testid: "microscope-cs" },
  { key: "amplitude", label: "Contraste amp.", unit: "", min: 0, max: 0.5, testid: "microscope-amp" },
] as const;

type Key = (typeof FIELDS)[number]["key"];
const draftsOf = (m: MicroscopeState): Record<Key, string> => ({
  kv: m.own.kv?.toString() ?? "",
  cs_mm: m.own.cs_mm?.toString() ?? "",
  amplitude: m.own.amplitude?.toString() ?? "",
});

/** The microscope of the session: what the CTF of its images is estimated with. The voltage an
 *  acquisition file gives comes first; empty fields take the value of the data root or of the
 *  server. Changing a value measures again the images that used another one. */
export function MicroscopePanel({ watchId, microscope, onError }: Props) {
  const [drafts, setDrafts] = useState(draftsOf(microscope));
  const [note, setNote] = useState<string | null>(null);
  const ownKey = JSON.stringify(microscope.own);
  useEffect(() => {
    setDrafts(draftsOf(microscope));
    // the fields follow what the server holds, not every summary that arrives
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchId, ownKey]);

  const parsed = Object.fromEntries(FIELDS.map((f) => [f.key, parseThreshold(drafts[f.key], f.min, f.max)])) as Record<Key, number | null | undefined>;
  const valid = FIELDS.every((f) => parsed[f.key] !== undefined);
  const save = () => {
    if (!valid) return;
    api.live
      .setMicroscope(watchId, parsed.kv ?? null, parsed.cs_mm ?? null, parsed.amplitude ?? null)
      .then((m) => setNote(m.remeasure ? `enregistré ; ${m.remeasure} image(s) mesurée(s) à nouveau` : "enregistré"))
      .catch((e) => onError(e instanceof Error ? e.message : String(e)));
  };

  return (
    <section className="panel" data-testid="microscope">
      <div className="panel-title">Microscope</div>
      {FIELDS.map((f) => (
        <label key={f.key} className={`threshold${parsed[f.key] === undefined ? " invalid" : ""}`} title={`En vigueur : ${microscope[f.key].value} ${f.unit} (${SOURCES[microscope[f.key].source]})`}>
          <span>{f.label}</span>
          <input inputMode="decimal" value={drafts[f.key]} placeholder={String(microscope[f.key].value)} aria-label={`${f.label}${f.unit ? ` (${f.unit})` : ""}`} data-testid={f.testid}
            onChange={(e) => { setNote(null); setDrafts((d) => ({ ...d, [f.key]: e.target.value })); }} />
          <span>{f.unit}</span>
        </label>
      ))}
      <div className="buttons">
        <button onClick={save} disabled={!valid} data-testid="microscope-save">Enregistrer</button>
        {note && <span className="note" data-testid="microscope-note">{note}</span>}
      </div>
      <div className="note" data-testid="microscope-source">
        {microscope.kv.value} kV ({SOURCES[microscope.kv.source]}). La tension lue dans les fichiers d'acquisition passe avant.
      </div>
    </section>
  );
}
