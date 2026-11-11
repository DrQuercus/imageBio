import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { IceReference } from "../api/types";
import { parseThreshold } from "./store";

interface Props {
  watchId: number;
  ice: IceReference;
  onError: (message: string) => void;
}

/** What turns mean counts into an ice thickness: the counts of an empty hole (read in the
 *  tooltip of a tile taken in one) and the mean free path of the microscope. Without them the
 *  ice is given relative to the brightest images of the session. */
export function IcePanel({ watchId, ice, onError }: Props) {
  const [i0, setI0] = useState("");
  const [mfp, setMfp] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setI0(ice.i0?.toString() ?? "");
    setMfp(ice.mfp_own?.toString() ?? "");
    // the fields follow what the server holds, not every summary that arrives
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchId, ice.i0, ice.mfp_own]);

  const parsedI0 = parseThreshold(i0, 1e-6, 1e12);
  const parsedMfp = parseThreshold(mfp, 50, 5000);
  const valid = parsedI0 !== undefined && parsedMfp !== undefined;
  const save = () => {
    if (!valid) return;
    api.live
      .setIce(watchId, parsedI0, parsedMfp)
      .then(() => setSaved(true))
      .catch((e) => onError(e instanceof Error ? e.message : String(e)));
  };

  return (
    <section className="panel" data-testid="ice">
      <div className="panel-title">Glace</div>
      <label className={`threshold${parsedI0 === undefined ? " invalid" : ""}`} title="Coups moyens d'une image prise dans un trou vide (info-bulle d'une tuile). Vide : les images les plus claires de la session servent de référence.">
        <span>Trou vide (I₀)</span>
        <input inputMode="decimal" value={i0} placeholder={ice.session ? ice.session.toFixed(1) : "—"} aria-label="Coups moyens d'un trou vide" data-testid="ice-i0"
          onChange={(e) => { setSaved(false); setI0(e.target.value); }} />
        <span>coups</span>
      </label>
      <label className={`threshold${parsedMfp === undefined ? " invalid" : ""}`} title="Libre parcours moyen des électrons dans la glace pour ce microscope et ce diaphragme (300 à 400 nm en général). Vide : valeur du serveur, sinon glace relative.">
        <span>Libre parcours</span>
        <input inputMode="decimal" value={mfp} placeholder={ice.mfp_nm ? String(ice.mfp_nm) : "—"} aria-label="Libre parcours moyen (nm)" data-testid="ice-mfp"
          onChange={(e) => { setSaved(false); setMfp(e.target.value); }} />
        <span>nm</span>
      </label>
      <div className="buttons">
        <button onClick={save} disabled={!valid} data-testid="ice-save">Enregistrer</button>
        {saved && <span className="note" data-testid="ice-saved">enregistré</span>}
      </div>
      <div className="note" data-testid="ice-mode">
        {ice.calibrated ? "Épaisseur absolue : comparée au trou vide." : "Glace relative aux images les plus claires de la session."}
      </div>
    </section>
  );
}
