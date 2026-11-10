/** The Live mode: follow an acquisition session as its images arrive.
 *
 *  The session is designated by the address ("#live=<directory>"): opening that link watches
 *  the directory (or finds the watch one already has on it). The server scans; this page asks
 *  every few seconds for what changed since the last change it knows. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isAbort } from "../api/client";
import type { LiveHistory, LiveSummary, LiveWatch, Me } from "../api/types";
import { FileBrowser } from "../components/FileBrowser";
import { ImageGrid } from "./ImageGrid";
import { RateGraph } from "./RateGraph";
import { StatsBar } from "./StatsBar";
import { formatLiveLink, type ItemMap, mergeItems, orderItems } from "./store";

const POLL_MS = 3000;
const PAGE = 2000;
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

interface Props {
  /** Directory designated by the address, or null when none is chosen yet. */
  dir: string | null;
  /** Leave for the viewer, on a file or on what it was showing before. */
  onViewer: (path?: string) => void;
}

export function LiveView({ dir, onViewer }: Props) {
  const [me, setMe] = useState<Me | null>(null);
  const [watches, setWatches] = useState<LiveWatch[]>([]);
  const [watch, setWatch] = useState<LiveWatch | null>(null);
  const [summary, setSummary] = useState<LiveSummary | null>(null);
  const receivedAt = useRef(0);
  const [items, setItems] = useState<ItemMap>(new Map());
  const [history, setHistory] = useState<LiveHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [browsed, setBrowsed] = useState<string | null>(null);
  const [newestFirst, setNewestFirst] = useState(true);
  const [tile, setTile] = useState(180);
  const onError = useCallback((m: string) => setError(m), []);

  const refreshWatches = useCallback(() => {
    api.live.watches().then(setWatches).catch((e) => setError(message(e)));
  }, []);

  useEffect(() => {
    api.me().then(setMe).catch((e) => setError(message(e)));
    refreshWatches();
  }, [refreshWatches]);

  // ── the session of the address: watch it (again) ───────────────────────────
  useEffect(() => {
    setWatch(null);
    setSummary(null);
    setItems(new Map());
    setHistory(null);
    if (!dir) return;
    const controller = new AbortController();
    api.live
      .start(dir, controller.signal)
      .then((w) => {
        setWatch(w);
        setError(null);
        refreshWatches();
      })
      .catch((e) => !isAbort(e) && setError(message(e)));
    return () => controller.abort();
  }, [dir, refreshWatches]);

  // ── follow it: one loop, each turn asks for what changed since the last one ──
  const watchId = watch?.id ?? null;
  useEffect(() => {
    if (watchId === null) return;
    const controller = new AbortController();
    let timer = 0;
    let since = 0;
    let known: ItemMap = new Map();
    let historyAt = -1;
    const turn = async () => {
      try {
        const s = await api.live.summary(watchId, controller.signal);
        receivedAt.current = performance.now();
        setSummary(s);
        while (s.seq > since) {
          const page = await api.live.items(watchId, since, PAGE, controller.signal);
          known = mergeItems(known, page.items);
          since = page.seq;
          if (!page.more) break;
        }
        setItems(known);
        if (s.seq !== historyAt) {
          historyAt = s.seq;
          setHistory(await api.live.history(watchId, 60, controller.signal));
        }
      } catch (e) {
        if (isAbort(e)) return;
        setError(message(e));
      }
      if (!controller.signal.aborted) timer = window.setTimeout(turn, POLL_MS);
    };
    void turn();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [watchId]);

  const ordered = useMemo(() => orderItems(items), [items]);
  const open = (path: string | null) => {
    window.location.hash = formatLiveLink(path);
  };
  const stop = (w: LiveWatch) => {
    api.live
      .stop(w.id)
      .then((stopped) => {
        if (watch?.id === stopped.id) setWatch(stopped);
        refreshWatches();
      })
      .catch((e) => setError(message(e)));
  };

  return (
    <div className="app live" data-testid="live">
      <header className="topbar">
        <span className="logo">imageBio</span>
        <div className="group">
          <button onClick={() => onViewer()} data-testid="mode-viewer">Visionneuse</button>
          <button className="on" data-testid="mode-live">Live</button>
        </div>
        <span className="session" data-testid="live-session" title={watch?.path}>{watch ? watch.path : "aucune session"}</span>
        <span className="spacer" />
        {me && me.auth_mode !== "none" && <span className="user">{me.user}</span>}
      </header>

      <aside className="left">
        <section className="panel" data-testid="watches">
          <div className="panel-title">Sessions suivies</div>
          {watches.length === 0 && <div className="hint">Aucune. Choisissez un dossier ci-dessous.</div>}
          {watches.map((w) => (
            <div key={w.id} className={`watch${watch?.id === w.id ? " active" : ""}`}>
              <button className="row" onClick={() => open(w.path)} title={w.path}>
                <span className={`beacon ${w.active ? "fresh" : "none"}`} />
                <span className="name">{w.path}</span>
              </button>
              {w.active && (
                <button className="watch-stop" onClick={() => stop(w)} title="Arrêter la surveillance (l'index est conservé)" aria-label={`Arrêter ${w.path}`}>
                  ■
                </button>
              )}
            </div>
          ))}
        </section>
        <div className="live-browser">
          <FileBrowser current={null} onOpen={onViewer} onError={onError} onDirectory={setBrowsed} />
          <button className="watch-here" disabled={!browsed} onClick={() => open(browsed)} data-testid="watch-here">
            Surveiller ce dossier
          </button>
        </div>
      </aside>

      <main className="center live-main">
        {error && (
          <div className="error" role="alert" data-testid="error">
            <span>{error}</span>
            <button onClick={() => setError(null)} aria-label="Fermer">×</button>
          </div>
        )}
        {watch && summary ? (
          <>
            <StatsBar summary={summary} receivedAt={receivedAt.current} />
            <RateGraph history={history} />
            <div className="grid-bar">
              <span data-testid="grid-count">{ordered.length.toLocaleString("fr-FR")} image{ordered.length > 1 ? "s" : ""}</span>
              <button className={newestFirst ? "on" : ""} onClick={() => setNewestFirst((v) => !v)} data-testid="order">
                {newestFirst ? "Les plus récentes d'abord" : "Dans l'ordre d'acquisition"}
              </button>
              <span className="spacer" />
              <label className="tile-size">
                Taille
                <input type="range" min={100} max={320} step={20} value={tile} onChange={(e) => setTile(Number(e.target.value))} aria-label="Taille des vignettes" />
              </label>
            </div>
            <ImageGrid items={ordered} newestFirst={newestFirst} tile={tile} onOpen={onViewer} />
          </>
        ) : (
          <div className="empty" data-testid="live-empty">
            {dir && !error ? "Ouverture de la session…" : "Choisissez un dossier à surveiller dans la liste de gauche."}
          </div>
        )}
      </main>

      <footer className="status">
        <span>{watch ? (watch.active ? "surveillance active" : "surveillance arrêtée : l'index n'est plus mis à jour") : ""}</span>
        <span className="spacer" />
        <span>{summary?.scanner.cycle_ms != null ? `dernier passage en ${summary.scanner.cycle_ms.toFixed(0)} ms` : ""}</span>
      </footer>
    </div>
  );
}
