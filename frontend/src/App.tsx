import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isAbort } from "./api/client";
import type { ClassInfo, DatasetInfoResponse, Me, PicksResponse, PlaneIndex, PlaneStats, ReaderOptions } from "./api/types";
import { ContrastPanel } from "./components/ContrastPanel";
import { CryoPanel } from "./components/CryoPanel";
import { FileBrowser } from "./components/FileBrowser";
import { InfoPanel, StackControls, StatusBar } from "./components/Panels";
import { MeasurePanel } from "./components/MeasurePanel";
import { ScalePanel } from "./components/ScalePanel";
import { type DisplaySettings, formatLink, type LinkState, parseLink } from "./state/url";
import { averageEnd, type CryoSettings, dirname, filterProblem, keepsGain, NO_CRYO, pickOverlay, processingKey, processingOf, statsLevel } from "./viewer/cryo";
import { Gallery } from "./viewer/Gallery";
import { galleryAxis, type GallerySort, isClassAverages, MAX_EXPORT_BYTES, stackBytes } from "./viewer/gallery";
import { type Lut, LUTS, parseLutFile } from "./viewer/luts";
import type { Profile } from "./viewer/profile";
import { neighbour, siblingImages } from "./viewer/siblings";
import { SpectrumView } from "./viewer/SpectrumView";
import { type HoverInfo, type Ruler, rulerLabel, type Tool, ViewerCanvas } from "./viewer/ViewerCanvas";
import { baseBin, clamp, clampIndex, MAX_ZOOM, MIN_ZOOM, type View } from "./viewer/view";

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
  // the address as the page last read or wrote it, and the write that is waiting to be made
  const addressSeen = useRef(window.location.hash);
  const addressTimer = useRef<number | undefined>(undefined);
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
  const [picks, setPicks] = useState<PicksResponse | null>(null);
  const [gallery, setGallery] = useState(initial.gallery ?? false);
  const [gallerySort, setGallerySort] = useState<GallerySort>("index");
  const [galleryTile, setGalleryTile] = useState(128);
  const [classes, setClasses] = useState<ClassInfo[] | null>(null);
  const [picksError, setPicksError] = useState<string | null>(null);
  /** A filter slider is held: the view shows its overview and waits for the release to
   *  load the detail (a filter at full resolution takes more than a second on a 4k image). */
  const [adjusting, setAdjusting] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  /** Diameter in Å of the circle that follows the cursor (key C), and whether it is shown. */
  const [circle, setCircle] = useState(150);
  const [circleOn, setCircleOn] = useState(false);
  /** The images of the folder of the file on screen, for the keys N and B. */
  const [siblings, setSiblings] = useState<{ dir: string; files: string[] } | null>(null);
  const [siblingsAsked, setSiblingsAsked] = useState(0);

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

  // Pixel size in effect: the one typed by the user, which replaces the file's (a header can be
  // wrong), else the file's. Filters and the frame average are kept from one file to the next;
  // a typed pixel size only when moving to the next file of the folder (see openFile).
  const fileApix = info?.calibration.x.angstrom ?? null;
  const apix = info ? (userApix ?? fileApix) : null;
  const apixTyped = userApix !== null;
  const frames = info?.shape.t ?? 1;
  // the Cryo tools work on one channel of data, not on a colour picture
  const cryoOn = info !== null && !info.rgb;
  const current = processingOf(cryoOn ? cryo : NO_CRYO, index.t, frames, apix, !apixTyped && fileApix !== null);
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
  // "same-size": keep the view if the next file has the size of the one on screen (the next
  // image of a session: the same region of the next micrograph).
  const fitOnLoad = useRef<boolean | "same-size">(false);
  // (the file last loaded: while the next one loads, `info` is already null)
  const shownShape = useRef(loaded?.response.info.shape);
  shownShape.current = loaded?.response.info.shape;
  const shownApix = useRef<number | null>(null);
  shownApix.current = loaded?.response.info.calibration.x.angstrom ?? null;
  /** Whether the coordinate file in use names micrographs (it then applies to other images). */
  const picksMany = useRef(false);
  const pathRef = useRef(path);
  pathRef.current = path;
  /** Open `next`. `keep`: the next or previous image of the folder, with everything kept that
   *  can be (view, contrast, typed pixel size); otherwise those start afresh. */
  const openFile = useCallback((next: string, keep = false) => {
    fitOnLoad.current = keep ? "same-size" : true;
    // filters and frame average follow the user from file to file; a gain only to a file of
    // the same kind in the same directory (an already corrected image must not get it again)
    if (!keepsGain(pathRef.current, next)) setCryo((c) => ({ ...c, gain: null, gainOrient: 0, gainInvert: false }));
    // a coordinate file about one image does not follow to the next; one of a whole data set does
    if (!picksMany.current) setCryo((c) => ({ ...c, picks: null, picksMinScore: null }));
    setPath(next);
    setOptions({});
    setIndex({ t: 0, c: 0, z: 0 });
    if (!keep) setDisplay((d) => ({ ...d, auto: true }));
    setRuler(null);
    setPlaying(false);
    if (!keep) setUserApix(null);
    setError(null);
  }, []);

  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api
      .info(path, options, controller.signal)
      .then((r) => {
        // one update: the file, its options and a plane that exists in it change together
        const before = shownShape.current;
        const sameSize = before !== undefined && before.x === r.info.shape.x && before.y === r.info.shape.y;
        const sameApix = shownApix.current === (r.info.calibration.x.angstrom ?? null);
        setLoaded({ requested: path, options, response: r });
        setStats(null);
        if (fitOnLoad.current === true || (fitOnLoad.current === "same-size" && !sameSize)) setView(null);
        // a typed pixel size follows to the next image of the folder only if it is of the same
        // kind (same size, same pixel size in its file): not to a thumbnail or a raw movie
        if (fitOnLoad.current === "same-size" && !(sameSize && sameApix)) setUserApix(null);
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
      // the address is now one the page has read; what it meant to write there is out of date
      window.clearTimeout(addressTimer.current);
      addressSeen.current = window.location.hash;
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
      setGallery(link.gallery ?? false);
      setRuler(null);
      setPlaying(false);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // ── the images of the folder: next and previous (N and B) ─────────────────
  const folder = path ? dirname(path) : null;
  useEffect(() => {
    if (folder === null) return;
    const controller = new AbortController();
    (async () => {
      const files: string[] = [];
      // a session folder can hold tens of thousands of files (images, metadata, thumbnails)
      for (let offset = 0, total = 1; offset < total && offset < 50_000; offset += 10_000) {
        const page = await api.list(folder, offset, 10_000, controller.signal);
        files.push(...siblingImages(page.entries));
        total = page.total;
      }
      if (!controller.signal.aborted) setSiblings({ dir: folder, files });
    })().catch(() => !controller.signal.aborted && setSiblings({ dir: folder, files: [] }));
    return () => controller.abort();
  }, [folder, siblingsAsked]);
  const folderFiles = siblings && siblings.dir === folder ? siblings.files : null;
  const position = path && folderFiles ? folderFiles.indexOf(path) : -1;
  const go = useCallback(
    (delta: number) => {
      if (!path || !folderFiles) return;
      const next = neighbour(folderFiles, path, delta);
      if (next) openFile(next, true);
      else setSiblingsAsked((n) => n + 1); // at an end: the session may have grown since
    },
    [path, folderFiles, openFile],
  );

  // ── statistics of the plane on screen: histogram and automatic window ──────
  // While a filter slider is held, those of the overview: they follow it at once, where those
  // of the full plane would wait for a filter at full resolution (ADR 013).
  const statsChannel = info?.rgb ? 0 : index.c;
  const shownPath = response?.path ?? null;
  const statsBin = info ? statsLevel(proc, adjusting, baseBin({ width: info.shape.x, height: info.shape.y })) : 1;
  useEffect(() => {
    if (!shownPath) return;
    const controller = new AbortController();
    api
      .stats(shownPath, { t: index.t, c: statsChannel, z: index.z }, shownOptions, controller.signal, proc, statsBin)
      .then((s) => {
        setStats(s);
        setDisplay((d) => (d.auto ? { ...d, min: s.auto[0], max: s.auto[1] } : d));
      })
      .catch((e) => !isAbort(e) && setError(message(e)));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownPath, etag, shownOptionsKey, index.t, index.z, statsChannel, procKey, statsBin]);

  // ── particle coordinates of the image on screen ────────────────────────────
  const picksFile = cryoOn ? cryo.picks : null;
  useEffect(() => {
    setPicks(null);
    setPicksError(null);
    if (!shownPath || !picksFile) return;
    const controller = new AbortController();
    api
      .picks(shownPath, picksFile, shownOptions, controller.signal)
      .then((found) => {
        picksMany.current = found.many;
        setPicks(found);
      })
      .catch((e) => !isAbort(e) && setPicksError(message(e)));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownPath, etag, shownOptionsKey, picksFile]);
  const overlay = useMemo(
    () => (picks && info ? pickOverlay(picks, cryo, info.shape.x, info.shape.y) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [picks, cryo.picksFlipY, cryo.picksDiameter, cryo.picksMinScore, info?.shape.x, info?.shape.y],
  );
  const usePicks = useCallback((file: string) => setCryo((c) => ({ ...c, picks: file, picksMinScore: null })), []);

  // ── gallery of a stack ─────────────────────────────────────────────────────
  const axis = info ? galleryAxis(info) : null;
  const galleryOn = gallery && axis !== null;
  useEffect(() => {
    setClasses(null);
    setGallerySort("index");
    if (!shownPath || !galleryOn || !isClassAverages(shownPath)) return;
    const controller = new AbortController();
    api
      .classes(shownPath, controller.signal)
      .then((r) => {
        setClasses(r.classes);
        if (r.classes) setGallerySort("population"); // as RELION shows them
      })
      .catch((e) => !isAbort(e) && setError(message(e)));
    return () => controller.abort();
  }, [shownPath, etag, galleryOn]);
  const openPlane = useCallback(
    (plane: number) => {
      if (axis) setIndex((i) => ({ ...i, [axis]: plane }));
      setGallery(false);
    },
    [axis],
  );

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
      // An address the page neither wrote nor read yet is somebody else's: a link pasted in
      // the address bar, Back, the Live button. The browser changes it at once and tells the
      // page a moment later; writing the view there in between would lose that link for good.
      if (window.location.hash !== addressSeen.current) return;
      const link = formatLink({ path: path ?? undefined, index, options, view: view ?? undefined, display, apix: userApix ?? undefined, cryo, gallery });
      window.history.replaceState(null, "", link || window.location.pathname);
      addressSeen.current = window.location.hash;
    }, 200);
    addressTimer.current = timer;
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, indexKey, optionsKey, view, display, userApix, cryo, gallery]);

  const copyLink = () => {
    const link = window.location.origin + window.location.pathname + formatLink({ path: path ?? undefined, index, options, view: view ?? undefined, display, apix: userApix ?? undefined, cryo, gallery });
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
        case "l": case "L": setCryo((c) => (c.lowpass !== null || c.highpass !== null ? { ...c, filtersOff: !c.filtersOff } : c)); break;
        case "c": case "C": if (apix) setCircleOn((on) => !on); break;
        case "n": case "N": go(1); break;
        case "b": case "B": go(-1); break;
        // on a slider, these move it
        case "PageDown": if (range) return; e.preventDefault(); go(1); break;
        case "PageUp": if (range) return; e.preventDefault(); go(-1); break;
        case "g": case "G": setGallery((g) => !g); break;
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
  }, [step, changeDisplay, display.invert, zoomBy, go, apix]);

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
        {path && folderFiles && position >= 0 && (
          <div className="group">
            <button onClick={() => go(-1)} disabled={position === 0} title="Image précédente du dossier, réglages conservés (B, Page ↑)"
              aria-label="Image précédente" data-testid="file-prev">◀</button>
            <span className="position" data-testid="file-position">{position + 1} / {folderFiles.length}</span>
            <button onClick={() => go(1)} title="Image suivante du dossier, réglages conservés (N, Page ↓)"
              aria-label="Image suivante" data-testid="file-next">▶</button>
          </div>
        )}
        <div className="group">
          <button onClick={() => setView(null)} title="Ajuster à la fenêtre (F)" data-testid="fit">Ajuster</button>
          <button onClick={() => setView((v) => v && { ...v, zoom: 1 })} title="1 pixel image = 1 pixel écran (1)" data-testid="one-to-one">1:1</button>
          <button onClick={() => zoomBy(1.25)} title="Zoom avant (+)" aria-label="Zoom avant">+</button>
          <button onClick={() => zoomBy(1 / 1.25)} title="Zoom arrière (-)" aria-label="Zoom arrière">−</button>
        </div>
        {axis && info && (
          <div className="group">
            <button className={galleryOn ? "on" : ""} onClick={() => setGallery((g) => !g)} title="Tous les plans de la pile côte à côte (G)" data-testid="gallery-toggle">
              Galerie
            </button>
            {galleryOn && classes && (
              <button onClick={() => setGallerySort((s) => (s === "index" ? "population" : "index"))} data-testid="gallery-sort"
                title="Ordre des classes : par numéro, ou les plus peuplées d'abord">
                {gallerySort === "population" ? "Par population" : "Par numéro"}
              </button>
            )}
            {galleryOn && (
              <input type="range" min={64} max={256} step={16} value={galleryTile} aria-label="Taille des vignettes" data-testid="gallery-size"
                onChange={(e) => setGalleryTile(Number(e.target.value))} />
            )}
          </div>
        )}
        <span className="spacer" />
        {response && info && (
          <div className="group">
            <a className="button" data-testid="fiji-plane" download title="Télécharger le plan affiché, tel qu'il est traité, en TIFF ImageJ avec sa taille de pixel : il s'ouvre dans Fiji"
              href={api.exportUrl(response.path, index, shownOptions, apixTyped ? { ...proc, apix: userApix } : proc, "plane")}>
              Ouvrir dans Fiji
            </a>
            {axis && stackBytes(info) <= MAX_EXPORT_BYTES && (
              <a className="button" data-testid="fiji-stack" download title="Télécharger tous les plans du fichier, tels qu'ils sont enregistrés, en une pile ImageJ"
                href={api.exportUrl(response.path, index, shownOptions, {}, "stack")}>
                Pile
              </a>
            )}
          </div>
        )}
        {path && <button onClick={copyLink} data-testid="copy-link">{copied ? "Lien copié" : "Copier le lien"}</button>}
        {me && me.auth_mode !== "none" && <span className="user">{me.user}</span>}
      </header>

      <aside className="left">
        <FileBrowser current={path} onOpen={openFile} onError={onError} onPicks={response && cryoOn ? usePicks : undefined} picks={cryo.picks} />
      </aside>

      <main className="center">
        <div className="stage">
          {error && (
            <div className="error" role="alert" data-testid="error">
              <span>{error}</span>
              <button onClick={() => setError(null)} aria-label="Fermer">×</button>
            </div>
          )}
          {response && info && galleryOn && axis ? (
            <Gallery path={response.path} options={shownOptions} axis={axis} count={info.shape[axis]} current={index[axis]}
              classes={classes} sort={gallerySort} tile={galleryTile} onOpen={openPlane} />
          ) : response && info ? (
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
              picks={overlay}
              onHover={setHover}
              onShown={onShown}
              onError={onError}
              onLevel={setLevel}
              holdDetail={adjusting}
              circle={circleOn && apix ? circle / apix : null}
              onProfile={setProfile}
            />
          ) : (
            <div className="empty" data-testid="empty">
              {path && !error ? "Ouverture…" : "Choisissez un fichier dans la liste de gauche."}
            </div>
          )}
        </div>
        {response && cryoOn && cryo.spectrum && (
          <SpectrumView path={response.path} etag={etag} index={index} proc={proc} options={shownOptions} apix={apix} apixTyped={apixTyped} onError={onError} />
        )}
      </main>

      <aside className="right">
        {response && info && (
          <>
            <ScalePanel info={info} fileApix={fileApix} userApix={userApix} onUserApix={setUserApix} level={level}>
              <MeasurePanel ruler={ruler} profile={profile} apix={apix} circle={circle} circleOn={circleOn && apix !== null}
                onCircle={setCircle} onCircleOn={setCircleOn} />
            </ScalePanel>
            <ContrastPanel reducedBy={statsBin} stats={stats} display={display} rgb={info.rgb} customLut={customLut !== null} onChange={changeDisplay} onLutFile={loadLutFile} />
            <StackControls info={info} index={index} onChange={setIndex} playing={playing} onPlay={setPlaying} fps={fps} onFps={setFps}
              averageEnd={tEnd} />
            {cryoOn && (
              <CryoPanel path={response.path} etag={etag} info={info} cryo={cryo} onChange={changeCryo} t={index.t} apix={apix}
                problem={filterProblem(cryo, apix)} options={shownOptions} onOptions={setOptions} picks={picks} picksError={picksError}
                onAdjust={setAdjusting} />
            )}
            <InfoPanel response={response} stats={stats} />
          </>
        )}
      </aside>

      <StatusBar hover={hover} zoom={view?.zoom ?? null} level={level} ruler={rulerText} />
    </div>
  );
}
