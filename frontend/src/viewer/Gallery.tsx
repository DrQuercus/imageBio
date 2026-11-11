/** Every plane of a stack side by side: particles, class averages, the sections of a volume.
 *  Only the tiles in view exist in the page; each one is a small picture made by the server. */

import { memo, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { ClassInfo, ReaderOptions } from "../api/types";
import { gridLayout, tilePosition } from "../live/store";
import { classLabel, type GalleryAxis, galleryOrder, type GallerySort } from "./gallery";

const GAP = 6;

interface TileProps {
  src: string;
  plane: number;
  label: string;
  top: number;
  left: number;
  size: number;
  current: boolean;
  onOpen: (plane: number) => void;
}

const Tile = memo(function Tile({ src, plane, label, top, left, size, current, onOpen }: TileProps) {
  const [failed, setFailed] = useState(false);
  // positions are set through the style object (CSSOM), which the page's CSP allows
  return (
    <button className={`gallery-tile${current ? " current" : ""}`} style={{ top, left, width: size, height: size }}
      data-testid="gallery-tile" data-plane={plane} title={`Plan ${plane + 1}${label ? ` · ${label}` : ""}`} onClick={() => onOpen(plane)}>
      {failed ? <span className="tile-wait">aperçu indisponible</span> : <img src={src} alt="" loading="lazy" draggable={false} onError={() => setFailed(true)} />}
      <span className="gallery-label">
        {plane + 1}
        {label ? ` · ${label}` : ""}
      </span>
    </button>
  );
});

interface Props {
  path: string;
  options: ReaderOptions;
  axis: GalleryAxis;
  count: number;
  /** Plane shown in the viewer: outlined in the gallery. */
  current: number;
  classes: ClassInfo[] | null;
  sort: GallerySort;
  tile: number;
  onOpen: (plane: number) => void;
}

export function Gallery({ path, options, axis, count, current, classes, sort, tile, onOpen }: Props) {
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

  const order = useMemo(() => galleryOrder(count, classes, sort), [count, classes, sort]);
  const byIndex = useMemo(() => new Map((classes ?? []).map((c) => [c.index, c])), [classes]);
  const optionsKey = JSON.stringify(options);
  const layout = gridLayout(count, box.width, box.height, scrollTop, tile, GAP);
  const tiles = [];
  for (let position = layout.first; position <= layout.last; position++) {
    const plane = order[position];
    if (plane === undefined) continue;
    const { top, left } = tilePosition(position, layout.columns, tile, GAP);
    tiles.push(
      <Tile key={`${optionsKey}|${plane}`} src={api.thumbUrl(path, { [axis]: plane }, tile, options)} plane={plane}
        label={classLabel(byIndex.get(plane))} top={top} left={left} size={tile} current={plane === current} onOpen={onOpen} />,
    );
  }

  return (
    <div className="grid-scroll gallery" ref={scrollRef} onScroll={onScroll} data-testid="gallery" data-count={count}>
      <div className="grid-canvas" style={{ height: layout.height }}>
        {tiles}
      </div>
    </div>
  );
}
