/** Power spectrum of the plane on screen: the 2-D spectrum with resolution rings in Å, and its
 *  rotational average. The spectrum is computed by the server on the unfiltered plane. */

import { useEffect, useMemo, useRef, useState } from "react";
import { api, isAbort } from "../api/client";
import type { PlaneIndex, Processing, ReaderOptions, Spectrum, SpectrumProfile } from "../api/types";
import { formatResolution, resolutionAt, type Ring, spectrumRings, spectrumToRgba, spectrumWindow } from "./cryo";

interface Props {
  path: string;
  etag: string;
  index: PlaneIndex;
  /** Frame average and gain of the image shown; its resolution filters do not apply here. */
  proc: Processing;
  options: ReaderOptions;
  /** Å per image pixel, or null when unknown: no ring can be placed then. */
  apix: number | null;
  onError: (message: string) => void;
}

const ZOOMS = [1, 2, 4];
const RING_COLOUR = "rgba(255, 179, 64, 0.75)";
const ICE_COLOUR = "#19c8ff";
const FONT = "11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

const ringLabel = (ring: Ring) => (ring.ice ? `glace ${ring.d} Å` : `${ring.d} Å`);

export function SpectrumView({ path, etag, index, proc, options, apix, onError }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLCanvasElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const profileRef = useRef<HTMLCanvasElement>(null);
  const [spectrum, setSpectrum] = useState<Spectrum | null>(null);
  const [profile, setProfile] = useState<SpectrumProfile | null>(null);
  const [zoom, setZoom] = useState(1);
  const [box, setBox] = useState(0); // side of the square on screen, in CSS pixels
  const [hover, setHover] = useState<number | null>(null); // radius under the cursor, spectrum pixels
  const report = useRef(onError);
  report.current = onError;
  const optionsKey = JSON.stringify(options);
  const { t_end: tEnd, gain, gain_orient: gainOrient, gain_inv: gainInvert } = proc;

  // ── data: latest wins ──────────────────────────────────────────────────────
  useEffect(() => {
    const controller = new AbortController();
    const request = { t: index.t, c: index.c, z: index.z, t_end: tEnd, gain, gain_orient: gainOrient, gain_inv: gainInvert };
    Promise.all([
      api.spectrum(path, request, options, controller.signal),
      api.spectrumProfile(path, request, options, controller.signal),
    ])
      .then(([s, p]) => {
        setSpectrum(s);
        setProfile(p);
      })
      .catch((e) => {
        if (isAbort(e)) return;
        setSpectrum(null);
        setProfile(null);
        report.current(e instanceof Error ? e.message : String(e));
      });
    return () => controller.abort();
    // options are compared by value
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, etag, optionsKey, index.t, index.c, index.z, tEnd, gain, gainOrient, gainInvert]);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setBox(el.getBoundingClientRect().width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const side = spectrum?.side ?? 0;
  const rings = useMemo(() => (side && apix ? spectrumRings(side, apix, side / (14 * zoom)) : []), [side, apix, zoom]);

  // ── the spectrum itself: grey levels on a log scale, central part when zoomed ─
  useEffect(() => {
    const canvas = imageRef.current;
    if (!canvas || !spectrum) return;
    const n = spectrum.side;
    const full = document.createElement("canvas");
    full.width = full.height = n;
    full.getContext("2d")?.putImageData(new ImageData(spectrumToRgba(spectrum.data, spectrumWindow(spectrum.data)), n, n), 0, 0);
    canvas.width = canvas.height = n;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const visible = n / zoom;
    // the zero frequency (pixel n/2) stays at the centre of the canvas
    ctx.imageSmoothingEnabled = zoom === 1;
    ctx.drawImage(full, n / 2 - visible / 2, n / 2 - visible / 2, visible, visible, 0, 0, n, n);
  }, [spectrum, zoom]);

  // ── rings, in screen space: constant line width and text size ───────────────
  useEffect(() => {
    const canvas = overlayRef.current;
    if (!canvas || !box) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = canvas.height = Math.round(box * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, box, box);
    if (!side) return;
    const scale = (box / side) * zoom; // screen pixels per spectrum pixel
    const centre = box / 2;
    ctx.font = FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (const ring of rings) {
      const r = ring.radius * scale;
      if (r > box * 0.72) continue; // entirely outside the square
      const colour = ring.ice ? ICE_COLOUR : RING_COLOUR;
      ctx.setLineDash(ring.ice ? [] : [4, 4]);
      for (const [stroke, width] of [["rgba(5, 8, 12, 0.6)", 3], [colour, 1.25]] as const) {
        ctx.strokeStyle = stroke; // a dark edge keeps the ring visible on a bright spectrum
        ctx.lineWidth = width;
        ctx.beginPath();
        ctx.arc(centre, centre, r, 0, 2 * Math.PI);
        ctx.stroke();
      }
      const along = r * Math.SQRT1_2; // label on the upper-right diagonal
      const label = ringLabel(ring);
      const width = ctx.measureText(label).width;
      const x = centre + along + 4;
      const y = centre - along - 9;
      if (x + width + 4 < box && y - 8 > 0) {
        ctx.fillStyle = "rgba(5, 8, 12, 0.8)";
        ctx.fillRect(x - 3, y - 8, width + 6, 16);
        ctx.fillStyle = colour;
        ctx.fillText(label, x, y);
      }
    }
    ctx.setLineDash([]);
  }, [box, side, rings, zoom]);

  // ── rotational average ─────────────────────────────────────────────────────
  useEffect(() => {
    const canvas = profileRef.current;
    if (!canvas || !box) return;
    const dpr = window.devicePixelRatio || 1;
    const w = box;
    const h = 130;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#05080c";
    ctx.fillRect(0, 0, w, h);
    if (!profile) return;
    const last = Math.max(2, Math.floor((profile.power.length - 1) / zoom)); // same extent as the image above
    const logs = profile.power.map((p) => (p !== null && p > 0 ? Math.log10(p) : NaN));
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = 2; i <= last; i++) {
      const v = logs[i]!;
      if (Number.isFinite(v)) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
    if (!(hi > lo)) return;
    const x = (radius: number) => (radius / last) * w;
    const y = (v: number) => h - 14 - ((v - lo) / (hi - lo)) * (h - 32);
    ctx.font = FONT;
    ctx.lineWidth = 1;
    for (const ring of rings) {
      if (ring.radius > last) continue;
      ctx.strokeStyle = ctx.fillStyle = ring.ice ? ICE_COLOUR : "rgba(255, 179, 64, 0.45)";
      ctx.beginPath();
      ctx.moveTo(x(ring.radius), 14);
      ctx.lineTo(x(ring.radius), h);
      ctx.stroke();
      ctx.textAlign = x(ring.radius) > w - 40 ? "right" : "center";
      ctx.textBaseline = "top";
      ctx.fillText(ring.ice ? "glace" : String(ring.d), x(ring.radius), 1);
    }
    ctx.strokeStyle = "#d7e1ea";
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    let started = false;
    for (let i = 1; i <= last; i++) {
      const v = logs[i]!;
      if (!Number.isFinite(v)) continue;
      const py = Math.min(h, Math.max(0, y(v)));
      if (started) ctx.lineTo(x(i), py);
      else ctx.moveTo(x(i), py);
      started = true;
    }
    ctx.stroke();
    if (hover !== null && hover <= last) {
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x(hover), 14);
      ctx.lineTo(x(hover), h);
      ctx.stroke();
    }
  }, [box, profile, rings, zoom, hover]);

  // ── readout ────────────────────────────────────────────────────────────────
  const onImageMove = (e: React.PointerEvent) => {
    const rect = boxRef.current?.getBoundingClientRect();
    if (!rect || !side || !rect.width) return;
    const scale = (rect.width / side) * zoom;
    setHover(Math.hypot(e.clientX - rect.left - rect.width / 2, e.clientY - rect.top - rect.height / 2) / scale);
  };
  const onProfileMove = (e: React.PointerEvent) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (!profile || !rect.width) return;
    const last = Math.max(2, Math.floor((profile.power.length - 1) / zoom));
    setHover(Math.max(0, ((e.clientX - rect.left) / rect.width) * last));
  };

  let readout = "—";
  if (hover !== null && side) {
    readout = apix
      ? `${formatResolution(resolutionAt(side, apix, hover))}  ·  ${(hover / (side * apix)).toFixed(4)} Å⁻¹`
      : `période ${hover > 0 ? (side / hover).toFixed(1) : "∞"} px`;
  }

  return (
    <section className="spectrum" data-testid="spectrum">
      <div className="spectrum-head">
        <span className="panel-title">Spectre de puissance</span>
        <div className="group">
          {ZOOMS.map((z) => (
            <button key={z} className={zoom === z ? "on" : ""} data-testid={`spectrum-zoom-${z}`}
              title={`Agrandir le centre ${z} fois`} onClick={() => setZoom(z)}>×{z}</button>
          ))}
        </div>
      </div>
      <div ref={boxRef} className="spectrum-box" onPointerMove={onImageMove} onPointerLeave={() => setHover(null)}>
        <canvas ref={imageRef} className="spectrum-image" data-testid="spectrum-image" data-side={side || undefined} />
        <canvas ref={overlayRef} className="spectrum-overlay" />
        {!spectrum && <div className="spectrum-wait">Calcul…</div>}
      </div>
      <div className="spectrum-readout" data-testid="spectrum-readout">{readout}</div>
      <canvas ref={profileRef} className="spectrum-profile" data-testid="spectrum-profile"
        onPointerMove={onProfileMove} onPointerLeave={() => setHover(null)} />
      <div className="spectrum-note" data-testid="spectrum-rings">
        {apix
          ? rings.length
            ? `Anneaux : ${rings.map(ringLabel).join(", ")} · Nyquist ${formatResolution(2 * apix)}`
            : `Nyquist ${formatResolution(2 * apix)}`
          : "Taille de pixel inconnue : pas d'anneaux de résolution."}
        {tEnd !== undefined && ` · moyenne des frames ${index.t + 1} à ${tEnd + 1}`}
        {gain && " · gain appliqué"}
      </div>
    </section>
  );
}
