import { useEffect, useRef, useState } from "react";
import type { LiveHistory } from "../api/types";
import { historyBars } from "./store";

const MAX_BARS = 240; // four hours of one-minute buckets
const FONT = "11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

/** Acquisitions per minute over the last hours, by acquisition time: gaps show at a glance. */
export function RateGraph({ history }: { history: LiveHistory | null }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const observer = new ResizeObserver(() => setWidth(canvas.getBoundingClientRect().width));
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !width) return;
    const dpr = window.devicePixelRatio || 1;
    const height = 90;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#05080c";
    ctx.fillRect(0, 0, width, height);
    ctx.font = FONT;
    const bars = history ? historyBars(history.counts, history.bucket, MAX_BARS) : { start: 0, values: [] };
    if (!history || !bars.values.length) {
      ctx.fillStyle = "#7c8fa2";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("Pas encore d'image datée.", width / 2, height / 2);
      return;
    }
    const left = 34;
    const bottom = height - 16;
    const top = 6;
    const peak = Math.max(1, ...bars.values);
    const step = (width - left - 4) / bars.values.length;
    ctx.fillStyle = "#7c8fa2";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    // per-minute counts read as an hourly rate
    const perHour = (n: number) => Math.round((n * 3600) / history.bucket);
    ctx.fillText(String(perHour(peak)), left - 4, top + 4);
    ctx.fillText("0", left - 4, bottom);
    ctx.fillStyle = "#19c8ff";
    bars.values.forEach((n, k) => {
      const h = (n / peak) * (bottom - top);
      ctx.fillRect(left + k * step, bottom - h, Math.max(1, step - 1), h);
    });
    ctx.fillStyle = "#7c8fa2";
    ctx.textBaseline = "top";
    const label = (seconds: number) =>
      new Date(seconds * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    ctx.textAlign = "left";
    ctx.fillText(label(bars.start), left, bottom + 3);
    ctx.textAlign = "right";
    ctx.fillText(`${label(bars.start + bars.values.length * history.bucket)}  ·  images/h`, width - 4, bottom + 3);
  }, [history, width]);

  return <canvas ref={canvasRef} className="rate-graph" data-testid="rate-graph" />;
}
