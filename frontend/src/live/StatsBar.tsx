import { useEffect, useState } from "react";
import type { LiveSummary } from "../api/types";
import { formatAgo, formatBytes, freshness } from "./store";

interface Props {
  summary: LiveSummary;
  /** Relative ice of the last measured images (store.ts, iceSummary); null: nothing to say yet. */
  ice: { median: number; ring: number; count: number } | null;
  /** Images the acceptance thresholds accepted and rejected; null: none judged. */
  quality: { accepted: number; rejected: number } | null;
  /** performance.now() when the summary arrived: the server's clock is carried forward from it. */
  receivedAt: number;
}

const clock = (seconds: number) =>
  new Date(seconds * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** The session at a glance: how many images, how fast, since when nothing came, disk left. */
export function StatsBar({ summary, ice, quality, receivedAt }: Props) {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const serverNow = summary.now + (performance.now() - receivedAt) / 1000;
  const since = summary.last ? serverNow - summary.last.time : null;
  const state = freshness(since);
  const { scanner, disk } = summary;
  const diskLevel = disk?.percent == null ? "" : disk.percent >= 90 ? "stalled" : disk.percent >= 80 ? "late" : "fresh";
  const status = !summary.watch.active
    ? "arrêtée"
    : scanner.error
      ? "erreur"
      : scanner.cycle_at === null
        ? "premier passage…"
        : "en cours";

  return (
    <div className="stats" data-testid="stats">
      <div className="stat">
        <span className="stat-label">Images</span>
        <span className="stat-value" data-testid="stat-acquired">{summary.acquired.toLocaleString("fr-FR")}</span>
        <span className="stat-sub" data-testid="stat-incoming">
          {summary.incoming ? `${summary.incoming} en cours d'écriture` : "toutes complètes"}
        </span>
      </div>
      <div className="stat">
        <span className="stat-label">Débit</span>
        <span className="stat-value accent" data-testid="stat-rate">{summary.rate.last_5_min}</span>
        <span className="stat-sub">images/h sur 5 min · {summary.rate.last_hour} dans l'heure</span>
      </div>
      <div className={`stat${summary.alerts.includes("no_image") || summary.alerts.includes("rate_drop") ? " alerting" : ""}`}>
        <span className="stat-label">Dernière image</span>
        <span className="stat-value small" data-testid="stat-last">
          <span className={`beacon ${state}`} />
          {since === null ? "—" : formatAgo(since)}
        </span>
        <span className="stat-sub">{summary.last ? clock(summary.last.time) : "aucune"}</span>
      </div>
      <div className="stat" title="Glace traversée, par rapport aux images les plus claires de la session : ln(I₀/I) sur les coups moyens, médiane des dernières images mesurées. À multiplier par le libre parcours moyen du microscope (300 à 400 nm) pour une épaisseur. « Anneau » : part de ces images dont l'anneau de glace cristalline dépasse 1,2.">
        <span className="stat-label">Glace</span>
        <span className="stat-value small" data-testid="stat-ice">
          {!ice ? "—" : summary.ice.mfp_nm ? `${Math.round(ice.median * summary.ice.mfp_nm)} nm` : ice.median.toFixed(2)}
        </span>
        <span className="stat-sub" data-testid="stat-ice-sub">
          {ice
            ? `sur ${ice.count} image${ice.count > 1 ? "s" : ""} · anneau ${Math.round(ice.ring * 100)} %`
            : summary.ice.measured
              ? `${summary.ice.measured} image${summary.ice.measured > 1 ? "s" : ""} mesurée${summary.ice.measured > 1 ? "s" : ""} : il en faut 20`
              : "pas encore mesurée"}
        </span>
      </div>
      {quality && (
        <div className="stat" title="Images qui passent les seuils d'acceptation de la session, parmi celles qui ont été mesurées">
          <span className="stat-label">Acceptées</span>
          <span className="stat-value small" data-testid="stat-quality">
            {Math.round((100 * quality.accepted) / (quality.accepted + quality.rejected))} %
          </span>
          <span className="stat-sub" data-testid="stat-quality-sub">
            {quality.rejected} rejetée{quality.rejected > 1 ? "s" : ""} sur {quality.accepted + quality.rejected}
          </span>
        </div>
      )}
      <div className={`stat${summary.alerts.includes("disk") ? " alerting" : ""}`}>
        <span className="stat-label">Disque</span>
        {disk && disk.percent !== null ? (
          <>
            <span className={`stat-value small ${diskLevel}`} data-testid="stat-disk">{disk.percent} %</span>
            <progress className={`disk ${diskLevel}`} max={100} value={disk.percent} />
            <span className="stat-sub">{formatBytes(disk.free)} libres sur {formatBytes(disk.total)}</span>
          </>
        ) : (
          <span className="stat-value small">—</span>
        )}
      </div>
      <div className="stat">
        <span className="stat-label">Surveillance</span>
        <span className={`stat-value small ${scanner.error ? "stalled" : summary.watch.active ? "fresh" : ""}`} data-testid="stat-status">
          {status}
        </span>
        <span className="stat-sub" title={scanner.error ?? undefined}>
          {scanner.error ??
            (scanner.cycle_at !== null
              ? `passage ${formatAgo(serverNow - scanner.cycle_at)}${scanner.pending ? ` · ${scanner.pending} fichier(s) suivis` : ""}`
              : "")}
        </span>
      </div>
    </div>
  );
}
