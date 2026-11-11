/** The exposures of a session as a grid of thumbnails. Only the tiles in view exist in the
 *  page: a session of fifty thousand images scrolls like one of fifty. */

import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { LiveItem, QualityRules } from "../api/types";
import { FLAG_LABELS, gridLayout, iceLabel, imageFlags, itemDetails, itemTime, metricsLines, rejections, tilePosition, verdict } from "./store";

const GAP = 8;

const clock = (seconds: number) =>
  new Date(seconds * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

interface TileProps {
  item: LiveItem;
  top: number;
  left: number;
  size: number;
  /** Mean counts the images of the session are compared to; null: none yet. */
  reference: number | null;
  /** The reference is the counts of an empty hole; and the mean free path in nm, if known. */
  calibrated: boolean;
  mfp: number | null;
  rules: QualityRules;
  onOpen: (path: string) => void;
}

const Tile = memo(function Tile({ item, top, left, size, reference, calibrated, mfp, rules, onOpen }: TileProps) {
  const [failed, setFailed] = useState(false);
  const incoming = item.state !== "complete";
  const details = itemDetails(item);
  const flags = incoming ? [] : imageFlags(item, reference, calibrated);
  const ice = incoming ? null : iceLabel(item, reference, mfp);
  const judged = verdict(item, rules, reference);
  const why = judged === "rejected" ? rejections(item, rules, reference) : [];
  const title = [item.name, item.note, ...metricsLines(item, reference, mfp), ...why.map((w) => `Rejetée : ${w}`)].filter(Boolean).join("\n");
  // positions are set through the style object (CSSOM), which the page's CSP allows
  return (
    <button
      className={`tile${incoming ? " incoming" : ""}${judged === "rejected" ? " rejected" : ""}`}
      style={{ top, left, width: size, height: size }}
      title={title}
      data-testid="tile"
      data-name={item.name}
      data-state={item.state}
      data-flags={flags.join(" ")}
      data-verdict={judged ?? ""}
      onClick={() => !incoming && onOpen(item.path)}
    >
      {incoming ? (
        <span className="tile-wait">{item.note ?? "en cours d'écriture"}</span>
      ) : failed ? (
        <span className="tile-wait">aperçu indisponible</span>
      ) : (
        <img src={api.live.thumbUrl(item.path)} alt="" loading="lazy" draggable={false} onError={() => setFailed(true)} />
      )}
      <span className="tile-info">
        <span className="tile-name">{item.name}</span>
        <span>
          {clock(itemTime(item))}
          {details ? ` · ${details}` : ""}
          {!incoming && !item.verified && <span className="tile-flag" title="Indexé depuis un dossier au repos, sans contrôle du fichier"> · non vérifié</span>}
        </span>
      </span>
      {ice && <span className="tile-ice">{ice}</span>}
      {(flags.length > 0 || judged === "rejected") && (
        <span className="tile-marks">
          {judged === "rejected" && <span className="tile-mark rejected" title={why.join("\n")}>rejetée</span>}
          {flags.map((flag) => (
            <span key={flag} className={`tile-mark ${flag}`} title={FLAG_LABELS[flag].long}>
              {FLAG_LABELS[flag].short}
            </span>
          ))}
        </span>
      )}
    </button>
  );
});

interface Props {
  /** In the order they are shown. */
  items: LiveItem[];
  tile: number;
  reference: number | null;
  calibrated: boolean;
  mfp: number | null;
  rules: QualityRules;
  onOpen: (path: string) => void;
}

export function ImageGrid({ items, tile, reference, calibrated, mfp, rules, onOpen }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });
  const [scrollTop, setScrollTop] = useState(0);
  const frame = useRef(0);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setBox({ width: el.clientWidth, height: el.clientHeight }));
    observer.observe(el);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame.current);
    };
  }, []);

  const onScroll = () => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => setScrollTop(scrollRef.current?.scrollTop ?? 0));
  };

  const layout = gridLayout(items.length, box.width, box.height, scrollTop, tile, GAP);
  const tiles = [];
  for (let position = layout.first; position <= layout.last; position++) {
    const item = items[position];
    if (!item) continue;
    const { top, left } = tilePosition(position, layout.columns, tile, GAP);
    tiles.push(<Tile key={item.id} item={item} top={top} left={left} size={tile} reference={reference} calibrated={calibrated} mfp={mfp} rules={rules} onOpen={onOpen} />);
  }

  return (
    <div className="grid-scroll" ref={scrollRef} onScroll={onScroll} data-testid="grid" data-count={items.length}>
      <div className="grid-canvas" style={{ height: layout.height }}>
        {tiles}
      </div>
      {items.length === 0 && <div className="grid-empty">Aucune image pour l'instant.</div>}
    </div>
  );
}
