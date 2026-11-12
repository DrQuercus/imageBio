import { useState } from "react";
import { formatLength } from "../viewer/view";
import { type Profile, profileRange, profileText } from "../viewer/profile";
import type { Ruler } from "../viewer/ViewerCanvas";
import { formatValue } from "./ContrastPanel";
import { NumberField } from "./NumberField";

const CHART_W = 260;
const CHART_H = 84;
const CHANNEL_COLOURS = ["#19c8ff", "#ff5d73", "#4dffa6", "#5d8bff"];
const RGB_COLOURS = ["#ff5d73", "#4dffa6", "#5d8bff"];

/** The profile as SVG polylines, one per channel; gaps where samples are missing. */
function ProfileChart({ profile }: { profile: Profile }) {
  const range = profileRange(profile);
  if (!range) return <div className="note" data-testid="profile-chart">Aucune valeur sous la règle.</div>;
  const [lo, hi] = range;
  const span = hi - lo || 1;
  const x = (d: number) => (d / (profile.length || 1)) * (CHART_W - 2) + 1;
  const y = (v: number) => CHART_H - 2 - ((v - lo) / span) * (CHART_H - 4);
  const colours = profile.values.length === 3 ? RGB_COLOURS : CHANNEL_COLOURS;
  return (
    <svg className="profile-chart" viewBox={`0 0 ${CHART_W} ${CHART_H}`} role="img" data-testid="profile-chart"
      data-samples={profile.at.length} data-min={lo} data-max={hi} aria-label="Profil d'intensité le long de la règle">
      {profile.values.map((channel, c) => {
        // one polyline per run of finite values
        const runs: string[][] = [[]];
        channel.forEach((v, k) => {
          if (Number.isFinite(v)) runs.at(-1)!.push(`${x(profile.at[k]!).toFixed(1)},${y(v).toFixed(1)}`);
          else if (runs.at(-1)!.length) runs.push([]);
        });
        return runs
          .filter((run) => run.length > 1)
          .map((run, r) => <polyline key={`${c}-${r}`} points={run.join(" ")} fill="none" stroke={colours[c % colours.length]} strokeWidth={1.2} />);
      })}
    </svg>
  );
}

interface Props {
  ruler: Ruler | null;
  profile: Profile | null;
  apix: number | null;
  /** Diameter of the circle drawn around the cursor, in Å, and whether it is shown. */
  circle: number;
  circleOn: boolean;
  onCircle: (diameter: number) => void;
  onCircleOn: (on: boolean) => void;
}

/** The ruler as in the first CryoViewer (its length large, in Å), the intensity along it,
 *  and the circle of a chosen diameter that follows the cursor. */
export function MeasurePanel({ ruler, profile, apix, circle, circleOn, onCircle, onCircleOn }: Props) {
  const [copied, setCopied] = useState(false);
  const pixels = ruler ? Math.hypot(ruler.x1 - ruler.x0, ruler.y1 - ruler.y0) : 0;
  const angle = ruler ? (Math.atan2(-(ruler.y1 - ruler.y0), ruler.x1 - ruler.x0) * 180) / Math.PI : 0;
  const copy = () => {
    if (!profile) return;
    void navigator.clipboard?.writeText(profileText(profile, apix)).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <>
      <div className="circle-row">
        <button className={circleOn ? "on" : ""} disabled={!apix} data-testid="circle-toggle"
          title={apix ? "Un cercle de ce diamètre suit le curseur : taille d'une particule, d'un trou, d'une boîte (C)" : "Demande la taille de pixel"}
          onClick={() => onCircleOn(!circleOn)}>Cercle</button>
        <label className="unit">
          Ø
          <NumberField value={circle} onCommit={(v) => v !== null && onCircle(v)} label="Diamètre du cercle en ångströms"
            testid="circle-diameter" min={1} max={1e6} />
          Å
        </label>
      </div>
      {ruler && pixels > 0 && (
        <div className="measure" data-testid="measure">
          <div className="measure-value" data-testid="measure-value">{apix ? formatLength(pixels * apix) : `${pixels.toFixed(1)} px`}</div>
          <div className="measure-sub">
            {apix ? `${pixels.toFixed(1)} px · ` : ""}angle {angle.toFixed(0)}°
            {profile && profile.bin > 1 ? ` · valeurs moyennées ${profile.bin}×${profile.bin} (zoomez pour la pleine résolution)` : ""}
          </div>
          {profile && (
            <>
              <ProfileChart profile={profile} />
              <div className="profile-axis">
                <span>{formatValue(profileRange(profile)?.[0] ?? 0)} – {formatValue(profileRange(profile)?.[1] ?? 0)}</span>
                <button data-testid="profile-copy" title="Copier les valeurs (distance, intensité) pour un tableur" onClick={copy}>
                  {copied ? "Copié" : "Copier"}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}
