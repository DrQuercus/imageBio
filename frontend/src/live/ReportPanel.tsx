import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ReportInfo, ReportListing } from "../api/types";
import { FileBrowser } from "../components/FileBrowser";

interface Props {
  watchId: number;
  /** Logical path of the watched directory: the atlas is looked for from its parent. */
  session: string;
  /** From the summary: a new report shows up without asking (one made by itself). */
  last: number | null;
  onError: (message: string) => void;
}

const TRIGGERS: Record<ReportInfo["trigger"], string> = {
  manual: "demandé",
  stop: "fin de surveillance",
  idle: "collecte silencieuse",
};

const when = (t: number) =>
  new Date(t * 1000).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

/** The quality report of the session: made now, or by itself when the watch ends; each one
 *  to read, or to download whole (the zip) for the library of collections. */
export function ReportPanel({ watchId, session, last, onError }: Props) {
  const [listing, setListing] = useState<ReportListing | null>(null);
  const [busy, setBusy] = useState(false);
  const [choosing, setChoosing] = useState(false);
  const [asked, setAsked] = useState<boolean | null>(null); // what was just asked, until the server says
  const fail = (e: unknown) => onError(e instanceof Error ? e.message : String(e));

  useEffect(() => {
    const controller = new AbortController();
    api.live.reports(watchId, controller.signal).then(setListing).catch((e) => !controller.signal.aborted && fail(e));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchId, last]);

  const makeNow = () => {
    setBusy(true);
    api.live
      .makeReport(watchId)
      .then(() => api.live.reports(watchId))
      .then(setListing)
      .catch(fail)
      .finally(() => setBusy(false));
  };
  const setAtlas = (path: string | null) => {
    setChoosing(false);
    api.live.setAtlas(watchId, path).then(setListing).catch(fail);
  };
  const atlas = listing?.atlas;
  const used = atlas?.chosen ?? atlas?.found ?? null;
  const setAuto = (auto: boolean) => {
    setAsked(auto);
    api.live
      .setReportAuto(watchId, auto)
      .then(setListing)
      .catch(fail)
      .finally(() => setAsked(null));
  };

  return (
    <section className="panel" data-testid="report">
      <div className="panel-title">Rapport de qualité</div>
      <div className="buttons">
        <button onClick={makeNow} disabled={busy} data-testid="report-now">{busy ? "Rapport en cours…" : "Rapport maintenant"}</button>
      </div>
      <label className="check" title="Un rapport est écrit quand la surveillance s'arrête, et quand la collecte ne reçoit plus d'image depuis un moment">
        <input type="checkbox" checked={asked ?? listing?.auto ?? true} disabled={!listing} onChange={(e) => setAuto(e.target.checked)} data-testid="report-auto" />
        <span>
          à la fin de la surveillance
          {listing && listing.idle_min > 0 ? `, et après ${listing.idle_min} min sans image` : ""}
        </span>
      </label>
      <div className="atlas-choice" data-testid="report-atlas">
        <span className="field-label">Atlas</span>
        {used ? (
          <span>
            <a href={`/#path=${encodeURIComponent(used.path)}`} title={used.path} data-testid="report-atlas-name">{used.name}</a>
            {atlas?.chosen ? " (choisi)" : " (trouvé dans la session)"}
          </span>
        ) : (
          <span className="note" data-testid="report-atlas-name">aucun dans la session</span>
        )}
        <span className="links">
          <button onClick={() => setChoosing(true)} disabled={!listing} data-testid="atlas-choose"
            title="Montrer dans les rapports l'atlas d'une autre session (EPU le garde souvent à part)">Choisir…</button>
          {atlas?.chosen && (
            <button onClick={() => setAtlas(null)} data-testid="atlas-clear"
              title={atlas.found ? `Revenir à ${atlas.found.name}, trouvé dans la session` : "Ne plus montrer d'atlas"}>Retirer</button>
          )}
        </span>
      </div>
      {choosing && (
        <div className="modal" role="dialog" aria-label="Choisir l'atlas" data-testid="atlas-dialog">
          <div className="modal-box">
            <div className="panel-title">Choisir l'atlas des rapports</div>
            <p className="note">L'image de toute la grille à basse résolution (EPU : Atlas/Atlas_1.mrc). Cliquez-la.</p>
            <div className="modal-browser">
              <FileBrowser current={session} onOpen={(path) => setAtlas(path)} onError={onError} />
            </div>
            <div className="buttons">
              <button onClick={() => setChoosing(false)} data-testid="atlas-cancel">Annuler</button>
            </div>
          </div>
        </div>
      )}
      <ul className="reports" data-testid="report-list">
        {listing?.reports.map((r) => (
          <li key={r.id} data-testid="report-row" data-id={r.id}>
            <span>{when(r.created_at)} · {TRIGGERS[r.trigger]} · {r.exposures} image{r.exposures > 1 ? "s" : ""}</span>
            <span className="links">
              <a href={api.live.reportUrl(watchId, r.id, "html", true)} target="_blank" rel="noopener noreferrer" data-testid="report-view">Lire</a>
              <a href={api.live.reportUrl(watchId, r.id, "zip")} download data-testid="report-zip" title="La page, les chiffres (JSON) et une ligne par image (CSV)">ZIP</a>
              <a href={api.live.reportUrl(watchId, r.id, "html")} download>HTML</a>
              <a href={api.live.reportUrl(watchId, r.id, "json")} download>JSON</a>
              <a href={api.live.reportUrl(watchId, r.id, "csv")} download>CSV</a>
            </span>
          </li>
        ))}
      </ul>
      {listing && !listing.reports.length && <div className="note">Aucun rapport pour l'instant.</div>}
    </section>
  );
}
