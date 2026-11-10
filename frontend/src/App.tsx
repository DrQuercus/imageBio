import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isAbort } from "./api/client";
import type { DatasetInfoResponse, Me, PlaneIndex, PlaneStats, ReaderOptions } from "./api/types";
import { ContrastPanel } from "./components/ContrastPanel";
import { CryoPanel } from "./components/CryoPanel";
import { FileBrowser } from "./components/FileBrowser";
import { InfoPanel, StackControls, StatusBar } from "./components/Panels";
import { type DisplaySettings, formatLink, type LinkState, parseLink } from "./state/url";
import { averageEnd, type CryoSettings, filterProblem, keepsGain, NO_CRYO, processingKey, processingOf } from "./viewer/cryo";
import { type Lut, LUTS, parseLutFile } from "./viewer/luts";
import { SpectrumView } from "./viewer/SpectrumView";
import { type HoverInfo, type Ruler, rulerLabel, type Tool, ViewerCanvas } from "./viewer/ViewerCanvas";
import { clamp, clampIndex, MAX_ZOOM, MIN_ZOOM, type View } from "./viewer/view";

const DEFAULT_DISPLAY: DisplaySettings = { min: 0, max: 1, gamma: 1, invert: false, lut: "grays", auto: true };

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The description of a file, with the path and the reader options it was asked for. */
interface Loaded {
  requested: string;
  options: ReaderOptions;
  response: DatasetInfoResponse;
}

export function App({ onLive }: { onLive: () => void }) {
  const initial = useMemo<LinkState>(() => parseLink(window.location.hash), []);
  const [me, setMe] = useState<Me | null>(null);
  const [path, setPath] = useState<string | null>(initial.path ?? null);
  const [options, setOptions] = useState<ReaderOptions>(initial.options);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [requestedIndex, setIndex] = useState<PlaneIndex>(initial.index);
  const [view, setView] = useState<View | null>(initial.view ?? null);
  const [display, setDisplay] = useState<DisplaySettings>({ ...DEFAULT_DISPLAY, ...initial.display });
  const [userApix, setUserApix] = useState<number | null>(initial.apix ?? null);
  const [cryo, setCryo] = useState<CryoSettings>({ ...NO_CRYO, ...initial.cryo });
  const [customLut, setCustomLut] = useState<Lut | null>(null);
  const [stats, setStats] = useState<PlaneStats | null>(null);
  const [tool, setTool] = useState<Tool>("pan");
  const [ruler, setRuler] = useState<Ruler | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [fps, setFps] = useState(8);
  const [level, setLevel] = useState(1);
  const [shown, setShown] = useState("");
  const [copied, setCopied] = useState(false);

  // What is on screen. Every request for pixels is built from these values only: the file as
  // the server described it, the options it was opened with and a plane that exists in it.
  // While another file, other options or another plane are being loaded, `path`, `options` and
  // `requestedIndex` already hold the new values; mixing the two gives requests that make no
  // sense (frame 20 of a movie regrouped into 4 frames, the frame range of the previous file).
  const onScreen = loaded && loaded.requested === path ? loaded : null;
  const response = onScreen?.response ?? null;
  const shownOptions = onScreen?.options ?? options;
  const optionsKey = JSON.stringify(options);
  const shownOptionsKey = JSON.stringify(shownOptions);
  const info = response?.info ?? null;
  const etag = response?.file.etag ?? "";
  const shape = info?.shape;
  const index = useMemo<PlaneIndex>(
    () => (shape ? clampIndex(requestedIndex, shape) : requestedIndex),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [requestedIndex.t, requestedIndex.c, requestedIndex.z, shape?.t, shape?.c, shape?.z],
  );
  const onError = useCallback((m: string) => setError(m), []);

  // Pixel size in effect: the file's, else the one typed by the user. Filters and the frame
  // average are kept from one file to the next; a typed pixel size is not (see openFile).
  const fileApix = info?.calibration.x.angstrom ?? null;
  const apix = info ? (fileApix ?? userApix) : null;
  const frames = info?.shape.t ?? 1;
  // the Cryo tools work on one channel of data, not on a colour picture
  const cryoOn = info !== null && !info.rgb;
  const current = processingOf(cryoOn ? cryo : NO_CRYO, index.t, frames, apix, fileApix !== null);
  const procKey = processingKey(current);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const proc = useMemo(() => current, [procKey]);
  const tEnd = cryoOn ? averageEnd(index.t, cryo.average, frames) : null;
  const changeCryo = useCallback((patch: Partial<CryoSettings>) => setCryo((c) => ({ ...c, ...patch })), []);

  useEffect(() => {
    api.me().then(setMe).catch((e) => setError(message(e)));
  }, []);

  // ── opening a file ─────────────────────────────────────────────────────────
  // Set when the next file to arrive must be fitted to the window. The view cannot simply be
  // cleared here: the previous file is still on screen and would be refitted in its place.
  const fitOnLoad = useRef(false);
  const pathRef = useRef(path);
  pathRef.current = path;
  const openFile = useCallback((next: string) => {
    fitOnLoad.current = true;
    // filters and frame average follow the user from file to file; a gain only to a file of
    // the same kind in the same directory (an already corrected image must not get it again)
    if (!keepsGain(pathRef.current, next)) setCryo((c) => ({ ...c, gain: null, gainOrient: 0, gainInvert: false }));
    setPath(next);
    setOptions({});
    setIndex({ t: 0, c: 0, z: 0 });
    setDisplay((d) => ({ ...d, auto: true }));
    setRuler(null);
    setPlaying(false);
    setUserApix(null);
    setError(null);
  }, []);

  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api
      .info(path, options, controller.signal)
      .then((r) => {
        // one update: the file, its options and a plane that exists in it change together
        setLoaded({ requested: path, options, response: r });
        setStats(null);
        if (fitOnLoad.current) setView(null);
        fitOnLoad.current = false;
        setIndex((i) => clampIndex(i, r.info.shape));
      })
      .catch((e) => !isAbort(e) && setError(message(e)));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, optionsKey]);

  // a link pasted in the address bar of an open tab
  useEffect(() => {
    const onHash = () => {
      const link = parseLink(window.location.hash);
      if (!link.path) return;
      fitOnLoad.current = !link.view;
      setPath(link.path);
      setOptions(link.options);
      setIndex(link.index);
      if (link.view) setView(link.view);
      setDisplay((d) => ({ ...d, auto: true, ...link.display }));
      setUserApix(link.apix ?? null);
      setCryo({ ...NO_CRYO, ...link.cryo });
      setRuler(null);
      setPlaying(false);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // ── statistics of the plane on screen: histogram and automatic window ──────
  const statsChannel = info?.rgb ? 0 : index.c;
  const shownPath = response?.path ?? null;
  useEffect(() => {
    if (!shownPath) return;
    const controller = new AbortController();
    api
      .stats(shownPath, { t: index.t, c: statsChannel, z: index.z }, shownOptions, controller.signal, proc)
      .then((s) => {
        setStats(s);
        setDisplay((d) => (d.auto ? { ...d, min: s.auto[0], max: s.auto[1] } : d));
      })
      .catch((e) => !isAbort(e) && setError(message(e)));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownPath, etag, shownOptionsKey, index.t, index.z, statsChannel, procKey]);

  const changeDisplay = useCallback(
    (patch: Partial<DisplaySettings>) => {
      if (patch.lut) setCustomLut(null);
      setDisplay((d) => {
        const next = { ...d, ...patch };
        return patch.auto && stats ? { ...next, min: stats.auto[0], max: stats.auto[1] } : next;
      });
    },
    [stats],
  );

  const loadLutFile = useCallback((file: File) => {
    file
      .arrayBuffer()
      .then((buffer) => setCustomLut(parseLutFile(buffer)))
      .catch((e) => setError(message(e)));
  }, []);

  const lut = useMemo<Lut>(() => customLut ?? (LUTS[display.lut] ?? LUTS.grays!)(), [customLut, display.lut]);

  // ── player: the next plane is requested once the current one is on screen ──
  const indexKey = `${index.t},${index.c},${index.z}`;
  const advancedAt = useRef(0);
  useEffect(() => {
    if (!playing || !info || shown !== `${shownPath}|${indexKey}`) return;
    const axis = info.shape.t > 1 ? "t" : "z";
    const count = info.shape[axis];
    if (count < 2) {
      setPlaying(false);
      return;
    }
    const wait = Math.max(0, 1000 / fps - (performance.now() - advancedAt.current));
    const timer = window.setTimeout(() => {
      advancedAt.current = performance.now();
      setIndex((i) => ({ ...i, [axis]: (i[axis] + 1) % count }));
    }, wait);
    return () => window.clearTimeout(timer);
  }, [playing, shown, indexKey, shownPath, info, fps]);

  const step = useCallback(
    (delta: number) => {
      if (!info) return;
      const axis = info.shape.t > 1 ? "t" : "z";
      setIndex((i) => ({ ...i, [axis]: clamp(i[axis] + delta, 0, info.shape[axis] - 1) }));
    },
    [info],
  );

  // ── the view in the address bar ────────────────────────────────────────────
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const link = formatLink({ path: path ?? undefined, index, options, view: view ?? undefined, display, apix: userApix ?? undefined, cryo });
      window.history.replaceState(null, "", link || window.location.pathname);
    }, 200);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, indexKey, optionsKey, view, display, userApix, cryo]);

  const copyLink = () => {
    const link = window.location.origin + window.location.pathname + formatLink({ path: path ?? undefined, index, options, view: view ?? undefined, display, apix: userApix ?? undefined, cryo });
    void navigator.clipboard?.writeText(link).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  // ── keyboard ───────────────────────────────────────────────────────────────
  const zoomBy = useCallback((f: number) => setView((v) => v && { ...v, zoom: clamp(v.zoom * f, MIN_ZOOM, MAX_ZOOM) }), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const el = document.activeElement as HTMLElement | null;
      const typing = el && (el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable || (el.tagName === "INPUT" && (el as HTMLInputElement).type !== "range"));
      if (typing) return;
      const range = el?.tagName === "INPUT";
      switch (e.key) {
        case "ArrowLeft": if (range) return; step(-1); break;
        case "ArrowRight": if (range) return; step(1); break;
        case " ": e.preventDefault(); setPlaying((p) => !p); break;
        case "a": case "A": changeDisplay({ auto: true }); break;
        case "i": case "I": changeDisplay({ invert: !display.invert }); break;
        case "r": case "R": setTool((t) => (t === "ruler" ? "pan" : "ruler")); break;
        case "p": case "P": setCryo((c) => ({ ...c, spectrum: !c.spectrum })); break;
        case "Escape": setTool("pan"); setRuler(null); break;
        case "f": case "F": case "0": setView(null); break;
        case "1": setView((v) => v && { ...v, zoom: 1 }); break;
        case "+": case "=": zoomBy(1.25); break;
        case "-": zoomBy(1 / 1.25); break;
        default: return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, changeDisplay, display.invert, zoomBy]);

  const rulerText = ruler ? rulerLabel(Math.hypot(ruler.x1 - ruler.x0, ruler.y1 - ruler.y0), apix) : null;
  const onShown = useCallback((i: PlaneIndex) => setShown(`${shownPath}|${i.t},${i.c},${i.z}`), [shownPath]);

  return (
    <div className="app">
      <header className="topbar">
        <span className="logo">imageBio</span>
        <div className="group">
          <button className="on" data-testid="mode-viewer">Visionneuse</button>
          <button onClick={onLive} title="Suivre une session d'acquisition" data-testid="mode-live">Live</button>
        </div>
        <div className="group">
          <button className={tool === "pan" ? "on" : ""} onClick={() => setTool("pan")} title="Déplacer (Échap)" data-testid="tool-pan">Déplacer</button>
          <button className={tool === "ruler" ? "on" : ""} onClick={() => setTool("ruler")} title="Règle (R)" data-testid="tool-ruler">Règle</button>
        </div>
        <div className="group">
          <button onClick={() => setView(null)} title="Ajuster à la fenêtre (F)" data-testid="fit">Ajuster</button>
          <button onClick={() => setView((v) => v && { ...v, zoom: 1 })} title="1 pixel image = 1 pixel écran (1)" data-testid="one-to-one">1:1</button>
          <button onClick={() => zoomBy(1.25)} title="Zoom avant (+)" aria-label="Zoom avant">+</button>
          <button onClick={() => zoomBy(1 / 1.25)} title="Zoom arrière (-)" aria-label="Zoom arrière">−</button>
        </div>
        <span className="spacer" />
        {path && <button onClick={copyLink} data-testid="copy-link">{copied ? "Lien copié" : "Copier le lien"}</button>}
        {me && me.auth_mode !== "none" && <span className="user">{me.user}</span>}
      </header>

      <aside className="left">
        <FileBrowser current={path} onOpen={openFile} onError={onError} />
      </aside>

      <main className="center">
        <div className="stage">
          {error && (
            <div className="error" role="alert" data-testid="error">
              <span>{error}</span>
              <button onClick={() => setError(null)} aria-label="Fermer">×</button>
            </div>
          )}
          {response && info ? (
            <ViewerCanvas
              key={`${response.path}|${etag}|${shownOptionsKey}`}
              path={response.path}
              etag={etag}
              info={info}
              index={index}
              options={shownOptions}
              proc={proc}
              display={display}
              lut={lut}
              view={view}
              onViewChange={setView}
              tool={tool}
              ruler={ruler}
              onRulerChange={setRuler}
              apix={apix}
              onHover={setHover}
              onShown={onShown}
              onError={onError}
              onLevel={setLevel}
            />
          ) : (
            <div className="empty" data-testid="empty">
              {path && !error ? "Ouverture…" : "Choisissez un fichier dans la liste de gauche."}
            </div>
          )}
        </div>
        {response && cryoOn && cryo.spectrum && (
          <SpectrumView path={response.path} etag={etag} index={index} proc={proc} options={shownOptions} apix={apix} onError={onError} />
        )}
      </main>

      <aside className="right">
        {response && info && (
          <>
            <ContrastPanel stats={stats} display={display} rgb={info.rgb} customLut={customLut !== null} onChange={changeDisplay} onLutFile={loadLutFile} />
            <StackControls info={info} index={index} onChange={setIndex} playing={playing} onPlay={setPlaying} fps={fps} onFps={setFps}
              averageEnd={tEnd} />
            {cryoOn && (
              <CryoPanel path={response.path} etag={etag} info={info} cryo={cryo} onChange={changeCryo} t={index.t} apix={apix}
                problem={filterProblem(cryo, apix)} options={shownOptions} onOptions={setOptions} />
            )}
            <InfoPanel response={response} stats={stats} userApix={userApix} onUserApix={setUserApix} />
          </>
        )}
      </aside>

      <StatusBar hover={hover} zoom={view?.zoom ?? null} level={level} ruler={rulerText} />
    </div>
  );
}
