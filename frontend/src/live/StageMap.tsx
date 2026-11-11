/** Where on the grid each image was taken: the stage positions EPU records, coloured by the
 *  verdict of the acceptance thresholds. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { LiveItem, QualityRules } from "../api/types";
import { niceLength } from "../viewer/view";
import { fitStage, rejections, stagePoints, verdict } from "./store";

interface Props {
  items: LiveItem[];
  rules: QualityRules;
  reference: number | null;
  onOpen: (path: string) => void;
}

export function StageMap({ items, rules, reference, onOpen }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setBox({ width: el.clientWidth, height: el.clientHeight }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const points = useMemo(() => stagePoints(items), [items]);
  const { xy, scale } = useMemo(() => fitStage(points, box.width, box.height), [points, box.width, box.height]);
  const bar = scale > 0 ? niceLength(120 / scale) : 0; // µm, about 120 pixels long

  return (
    <div className="stage-map" ref={boxRef} data-testid="stage-map" data-points={points.length}>
      {points.length === 0 && <div className="grid-empty">Aucune position de platine dans les métadonnées de ces images.</div>}
      <svg width={box.width} height={box.height} role="img" aria-label="Carte des acquisitions">
        {xy.map(([x, y], k) => {
          const item = points[k]!.item;
          const v = verdict(item, rules, reference);
          const why = v === "rejected" ? rejections(item, rules, reference) : [];
          return (
            <circle key={item.id} cx={x} cy={y} r={5} className={`stage-point ${v ?? "unjudged"}`} data-testid="stage-point" data-verdict={v ?? ""}
              onClick={() => item.state === "complete" && onOpen(item.path)}>
              <title>{[item.name, ...why].join("\n")}</title>
            </circle>
          );
        })}
        {bar > 0 && (
          <g className="stage-bar">
            <rect x={16} y={box.height - 18} width={bar * scale} height={3} />
            <text x={16} y={box.height - 24}>{bar >= 1000 ? `${bar / 1000} mm` : `${bar} µm`}</text>
          </g>
        )}
      </svg>
    </div>
  );
}
