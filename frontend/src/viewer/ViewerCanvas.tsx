/** The image canvas: WebGL drawing, levels of detail, pan/zoom, pixel readout, ruler. */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { isAbort } from "../api/client";
import type { DatasetInfo, PlaneIndex, Processing, ReaderOptions } from "../api/types";
import type { DisplaySettings } from "../state/url";
import type { PickOverlay } from "./cryo";
import { type GpuLayer, Renderer } from "./gl";
import type { Lut } from "./luts";
import { loadPlanes, type PlaneSet, planeKeyOf, toFloat32, valuesAt } from "./planes";
import {
  baseBin,
  detailRequest,
  fitView,
  formatLength,
  imageToScreen,
  niceLength,
  panBy,
  regionContains,
  screenToImage,
  type Size,
  type View,
  zoomAt,
} from "./view";

export interface HoverInfo {
  x: number;
  y: number;
  values: number[];
  /** 1 when the values are stored pixels, N when they are means of N x N pixels. */
  bin: number;
}

export interface Ruler {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export type Tool = "pan" | "ruler";

interface Props {
  path: string;
  etag: string;
  info: DatasetInfo;
  index: PlaneIndex;
  options: ReaderOptions;
  /** Frame average and filters applied by the server (compared by value). */
  proc: Processing;
  display: DisplaySettings;
  lut: Lut;
  view: View | null;
  onViewChange: (view: View) => void;
  tool: Tool;
  ruler: Ruler | null;
  onRulerChange: (ruler: Ruler | null) => void;
  /** Å per image pixel, or null when unknown. */
  apix: number | null;
  /** Particle coordinates to draw over the image, or null. */
  picks: PickOverlay | null;
  onHover: (info: HoverInfo | null) => void;
  /** Called when a plane is on screen (the frame player waits for it). */
  onShown: (index: PlaneIndex) => void;
  onError: (message: string) => void;
  onLevel: (bin: number) => void;
}

interface Loaded {
  set: PlaneSet;
  layer: GpuLayer;
}

const DETAIL_DELAY_MS = 120;

export function ViewerCanvas(props: Props) {
  const { path, etag, info, index, options, proc, display, lut, view, tool, ruler, apix, picks } = props;
  const wrapRef = useRef<HTMLDivElement>(null);
  const glRef = useRef<HTMLCanvasElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<Renderer | null>(null);
  const baseRef = useRef<Loaded | null>(null);
  const detailRef = useRef<Loaded | null>(null);
  const frameRef = useRef(0);
  const [viewport, setViewport] = useState<Size | null>(null);
  const [baseVersion, setBaseVersion] = useState(0);
  const callbacks = useRef(props);
  callbacks.current = props;

  const image = useMemo<Size>(() => ({ width: info.shape.x, height: info.shape.y }), [info.shape.x, info.shape.y]);
  const channels = useMemo(() => (info.rgb ? [0, 1, 2] : [index.c]), [info.rgb, index.c]);
  const planeKey = planeKeyOf(etag, options, index, channels, proc);

  // ── drawing ────────────────────────────────────────────────────────────────
  const state = useRef({ view, viewport, display, ruler, apix, picks, planeKey, rgb: info.rgb, image });
  state.current = { view, viewport, display, ruler, apix, picks, planeKey, rgb: info.rgb, image };
  const [drawnPicks, setDrawnPicks] = useState(0);

  // ── value under the cursor ─────────────────────────────────────────────────
  // The last pointer position is kept: when another plane arrives under a cursor that does
  // not move (player, filter, frame average), the readout must follow, not show the old value.
  const pointer = useRef<[number, number] | null>(null);
  const hover = useCallback((sx: number, sy: number) => {
    const s = state.current;
    if (!s.view || !s.viewport) return;
    const [ix, iy] = screenToImage(s.view, s.viewport, sx, sy);
    const x = Math.floor(ix);
    const y = Math.floor(iy);
    if (x < 0 || y < 0 || x >= s.image.width || y >= s.image.height) {
      callbacks.current.onHover(null);
      return;
    }
    const detail = detailRef.current?.set.planeKey === s.planeKey ? detailRef.current.set : null;
    const base = baseRef.current?.set.planeKey === s.planeKey ? baseRef.current.set : null;
    for (const set of [detail, base]) {
      const values = set ? valuesAt(set, x, y) : null;
      if (set && values) {
        callbacks.current.onHover({ x, y, values, bin: set.bin });
        return;
      }
    }
    callbacks.current.onHover({ x, y, values: [], bin: 1 });
  }, []);
  const refreshHover = useCallback(() => {
    if (pointer.current) hover(...pointer.current);
  }, [hover]);

  const draw = useCallback(() => {
    cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(() => {
      const s = state.current;
      const renderer = rendererRef.current;
      if (!renderer || !s.view || !s.viewport) return;
      const dpr = window.devicePixelRatio || 1;
      renderer.resize(s.viewport, dpr);
      const layers: GpuLayer[] = [];
      if (baseRef.current) layers.push(baseRef.current.layer);
      // the detail is only valid for the plane it was loaded from
      if (detailRef.current && detailRef.current.set.planeKey === baseRef.current?.set.planeKey) {
        layers.push(detailRef.current.layer);
      }
      renderer.draw({
        view: s.view,
        viewport: s.viewport,
        layers,
        min: s.display.min,
        max: s.display.max,
        gamma: s.display.gamma,
        invert: s.display.invert,
        rgb: s.rgb,
      });
      const drawn = drawOverlay(overlayRef.current, s.view, s.viewport, dpr, s.ruler, s.apix, s.picks);
      setDrawnPicks(drawn);
    });
  }, []);

  // ── renderer and viewport size ─────────────────────────────────────────────
  useLayoutEffect(() => {
    const canvas = glRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    let renderer: Renderer;
    try {
      renderer = new Renderer(canvas);
    } catch (e) {
      callbacks.current.onError(e instanceof Error ? e.message : String(e));
      return;
    }
    rendererRef.current = renderer;
    const observer = new ResizeObserver(() => {
      const rect = wrap.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) setViewport({ width: rect.width, height: rect.height });
    });
    observer.observe(wrap);
    // Hooks for the end-to-end tests and for diagnostics in the console.
    (window as unknown as Record<string, unknown>).__imagebio = {
      readPixel: (x: number, y: number) => renderer.readPixel(x, y, window.devicePixelRatio || 1),
      toScreen: (x: number, y: number) =>
        state.current.view && state.current.viewport
          ? imageToScreen(state.current.view, state.current.viewport, x, y)
          : null,
      levels: () => ({ base: baseRef.current?.set.bin ?? null, detail: detailRef.current?.set.bin ?? null }),
      path: () => callbacks.current.path,
    };
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frameRef.current);
      renderer.deleteLayer(baseRef.current?.layer);
      renderer.deleteLayer(detailRef.current?.layer);
      baseRef.current = detailRef.current = null;
      renderer.dispose();
      rendererRef.current = null;
    };
  }, []);

  // first view of a file: fit it to the window
  useEffect(() => {
    if (!view && viewport) callbacks.current.onViewChange(fitView(image, viewport));
  }, [view, viewport, image]);

  useEffect(() => {
    rendererRef.current?.setLut(lut);
    draw();
  }, [lut, draw]);

  useEffect(draw, [draw, view, viewport, display, ruler, apix, picks]);

  // ── overview: the whole plane, binned ──────────────────────────────────────
  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer) return;
    const controller = new AbortController();
    loadPlanes(path, etag, options, index, channels, proc, baseBin(image), undefined, controller.signal)
      .then((set) => {
        if (controller.signal.aborted) return;
        const layer = renderer.createLayer(set.planes.map(toFloat32), set.width, set.height, set.region, set.bin);
        renderer.deleteLayer(baseRef.current?.layer);
        baseRef.current = { set, layer };
        if (detailRef.current && detailRef.current.set.planeKey !== set.planeKey) {
          renderer.deleteLayer(detailRef.current.layer);
          detailRef.current = null;
        }
        setBaseVersion((v) => v + 1);
        draw();
        refreshHover();
        callbacks.current.onShown(index);
      })
      .catch((e) => {
        if (!isAbort(e)) callbacks.current.onError(e instanceof Error ? e.message : String(e));
      });
    return () => controller.abort();
    // options, channels and processing are compared by value: planeKey holds them all
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, planeKey, image, draw]);

  // ── detail: the visible region at the resolution the screen can show ───────
  useEffect(() => {
    const renderer = rendererRef.current;
    const base = baseRef.current;
    if (!renderer || !base || !view || !viewport || base.set.planeKey !== planeKey) return;
    const dpr = window.devicePixelRatio || 1;
    const wanted = detailRequest(view, viewport, image, dpr);
    const visible = detailRequest(view, viewport, image, dpr, 0);
    const level = wanted ? Math.min(wanted.bin, base.set.bin) : base.set.bin;
    callbacks.current.onLevel(level);
    if (!wanted || !visible || wanted.bin >= base.set.bin) return; // the overview is enough
    const current = detailRef.current;
    if (
      current &&
      current.set.planeKey === planeKey &&
      current.set.bin === wanted.bin &&
      regionContains(current.set.region, visible.region)
    ) {
      return; // still covers what is on screen
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      loadPlanes(path, etag, options, index, channels, proc, wanted.bin, wanted.region, controller.signal)
        .then((set) => {
          if (controller.signal.aborted) return;
          const layer = renderer.createLayer(set.planes.map(toFloat32), set.width, set.height, set.region, set.bin);
          renderer.deleteLayer(detailRef.current?.layer);
          detailRef.current = { set, layer };
          draw();
          refreshHover();
        })
        .catch((e) => {
          if (!isAbort(e)) callbacks.current.onError(e instanceof Error ? e.message : String(e));
        });
    }, DETAIL_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, viewport, baseVersion, planeKey, path, image, draw]);

  // ── interaction ────────────────────────────────────────────────────────────
  const drag = useRef<{ kind: Tool; x: number; y: number } | null>(null);

  const local = (e: { clientX: number; clientY: number }): [number, number] => {
    const rect = wrapRef.current?.getBoundingClientRect();
    return rect ? [e.clientX - rect.left, e.clientY - rect.top] : [0, 0];
  };

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const s = state.current;
      if (!s.view || !s.viewport) return;
      const [sx, sy] = local(e);
      callbacks.current.onViewChange(zoomAt(s.view, s.viewport, Math.exp(-e.deltaY * 0.0015), sx, sy));
    };
    wrap.addEventListener("wheel", onWheel, { passive: false }); // must be able to preventDefault
    return () => wrap.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 || !view || !viewport) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const [sx, sy] = local(e);
    drag.current = { kind: tool, x: sx, y: sy };
    if (tool === "ruler") {
      const [x, y] = screenToImage(view, viewport, sx, sy);
      props.onRulerChange({ x0: x, y0: y, x1: x, y1: y });
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const [sx, sy] = local(e);
    const d = drag.current;
    if (d && view && viewport) {
      if (d.kind === "pan") {
        props.onViewChange(panBy(view, sx - d.x, sy - d.y));
        drag.current = { ...d, x: sx, y: sy };
      } else if (ruler) {
        const [x, y] = screenToImage(view, viewport, sx, sy);
        props.onRulerChange({ ...ruler, x1: x, y1: y });
      }
    }
    pointer.current = [sx, sy];
    hover(sx, sy);
  };

  const onPointerUp = (e: React.PointerEvent) => {
    drag.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };

  return (
    <div
      ref={wrapRef}
      className={`viewer tool-${tool}`}
      data-testid="viewer"
      data-picks={picks ? picks.points.length / 2 : undefined}
      data-picks-drawn={picks ? drawnPicks : undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => {
        pointer.current = null;
        props.onHover(null);
      }}
      onDoubleClick={() => viewport && props.onViewChange(fitView(image, viewport))}
    >
      <canvas ref={glRef} className="viewer-gl" data-testid="viewer-canvas" />
      <canvas ref={overlayRef} className="viewer-overlay" data-testid="viewer-overlay" />
    </div>
  );
}

/** Particles, ruler and scale bar, drawn in screen space: constant line width and text size
 *  at any zoom. Returns the number of particles that fell in the window. */
function drawOverlay(
  canvas: HTMLCanvasElement | null,
  view: View,
  viewport: Size,
  dpr: number,
  ruler: Ruler | null,
  apix: number | null,
  picks: PickOverlay | null,
): number {
  if (!canvas) return 0;
  const w = Math.round(viewport.width * dpr);
  const h = Math.round(viewport.height * dpr);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return 0;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, viewport.width, viewport.height);
  ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

  let drawn = 0;
  if (picks && picks.points.length) {
    // one circle per particle at its real size; a dot when the circle would be a speck
    const radius = (picks.diameter / 2) * view.zoom;
    const [ox, oy] = imageToScreen(view, viewport, 0, 0);
    ctx.strokeStyle = "#4dffa6";
    ctx.fillStyle = "#4dffa6";
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    for (let k = 0; k < picks.points.length; k += 2) {
      const sx = ox + (picks.points[k] ?? 0) * view.zoom;
      const sy = oy + (picks.points[k + 1] ?? 0) * view.zoom;
      if (sx < -radius || sy < -radius || sx > viewport.width + radius || sy > viewport.height + radius) continue;
      drawn += 1;
      if (radius < 2) {
        ctx.rect(sx - 1, sy - 1, 2, 2);
      } else {
        ctx.moveTo(sx + radius, sy);
        ctx.arc(sx, sy, radius, 0, 2 * Math.PI);
      }
    }
    if (radius < 2) ctx.fill();
    else ctx.stroke();
  }

  if (apix) {
    // scale bar: a round length close to 120 screen pixels
    const length = niceLength((120 / view.zoom) * apix);
    const px = (length / apix) * view.zoom;
    const x = 16;
    const y = viewport.height - 20;
    ctx.fillStyle = "rgba(5, 8, 12, 0.6)";
    ctx.fillRect(x - 6, y - 22, px + 12, 30);
    ctx.fillStyle = "#fff";
    ctx.fillRect(x, y, px, 3);
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillText(formatLength(length), x + px / 2, y - 3);
  }

  if (ruler) {
    const [x0, y0] = imageToScreen(view, viewport, ruler.x0, ruler.y0);
    const [x1, y1] = imageToScreen(view, viewport, ruler.x1, ruler.y1);
    const pixels = Math.hypot(ruler.x1 - ruler.x0, ruler.y1 - ruler.y0);
    if (pixels > 0) {
      ctx.strokeStyle = "#ffb340";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 3]);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
      ctx.setLineDash([]);
      const len = Math.hypot(x1 - x0, y1 - y0) || 1;
      const nx = (-(y1 - y0) / len) * 6;
      const ny = ((x1 - x0) / len) * 6;
      for (const [px, py] of [[x0, y0], [x1, y1]] as const) {
        ctx.beginPath();
        ctx.moveTo(px + nx, py + ny);
        ctx.lineTo(px - nx, py - ny);
        ctx.stroke();
      }
      const label = rulerLabel(pixels, apix);
      const tx = (x0 + x1) / 2;
      const ty = (y0 + y1) / 2 - 10;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = "rgba(5, 8, 12, 0.85)";
      ctx.fillRect(tx - tw / 2 - 5, ty - 14, tw + 10, 18);
      ctx.fillStyle = "#ffb340";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText(label, tx, ty + 2);
    }
  }
  return drawn;
}

export function rulerLabel(pixels: number, apix: number | null): string {
  const px = `${pixels.toFixed(1)} px`;
  return apix ? `${formatLength(pixels * apix)}  (${px})` : px;
}
