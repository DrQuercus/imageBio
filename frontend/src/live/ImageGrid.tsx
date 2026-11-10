/** The exposures of a session as a grid of thumbnails. Only the tiles in view exist in the
 *  page: a session of fifty thousand images scrolls like one of fifty. */

import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { LiveItem } from "../api/types";
import { gridLayout, itemDetails, itemTime, tilePosition } from "./store";

const GAP = 8;

const clock = (seconds: number) =>
  new Date(seconds * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

interface TileProps {
  item: LiveItem;
  top: number;
  left: number;
  size: number;
  onOpen: (path: string) => void;
}

const Tile = memo(function Tile({ item, top, left, size, onOpen }: TileProps) {
  const [failed, setFailed] = useState(false);
  const incoming = item.state !== "complete";
  const details = itemDetails(item);
  // positions are set through the style object (CSSOM), which the page's CSP allows
  return (
    <button
      className={`tile${incoming ? " incoming" : ""}`}
      style={{ top, left, width: size, height: size }}
      title={`${item.name}${item.note ? `\n${item.note}` : ""}`}
      data-testid="tile"
      data-name={item.name}
      data-state={item.state}
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
    </button>
  );
});

interface Props {
  /** In acquisition order, oldest first. */
  items: LiveItem[];
  newestFirst: boolean;
  tile: number;
  onOpen: (path: string) => void;
}

export function ImageGrid({ items, newestFirst, tile, onOpen }: Props) {
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
    const item = items[newestFirst ? items.length - 1 - position : position];
    if (!item) continue;
    const { top, left } = tilePosition(position, layout.columns, tile, GAP);
    tiles.push(<Tile key={item.id} item={item} top={top} left={left} size={tile} onOpen={onOpen} />);
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
