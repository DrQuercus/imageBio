import { useEffect, useState } from "react";
import { api, isAbort } from "../api/client";
import type { DirListing } from "../api/types";
import { isPicksFile } from "../viewer/cryo";

interface Props {
  current: string | null;
  onOpen: (path: string) => void;
  onError: (message: string) => void;
  /** Told which directory is listed (null: the list of roots). */
  onDirectory?: (path: string | null) => void;
  /** Lay a coordinate file (.star, .box, .cbox, .cs) over the image shown. Absent: such files
   *  cannot be used at the moment (no image, or a colour picture). */
  onPicks?: (path: string) => void;
  /** The coordinate file in use, if any. */
  picks?: string | null;
}

const PAGE = 500;

function formatSize(bytes: number): string {
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = bytes;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u++;
  }
  return `${v >= 100 || u === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[u]}`;
}

/** Server-side file browser: roots, then directories. File names are rendered as text. */
export function FileBrowser({ current, onOpen, onError, onDirectory, onPicks, picks }: Props) {
  const [roots, setRoots] = useState<{ name: string; available: boolean }[] | null>(null);
  const [dir, setDir] = useState<string | null>(() => (current?.includes("/") ? current.replace(/\/[^/]*$/, "") : null));
  const [listing, setListing] = useState<DirListing | null>(null);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api
      .roots(controller.signal)
      .then((r) => {
        setRoots(r);
        const only = r.length === 1 ? r[0] : undefined;
        if (only?.available) setDir((d) => d ?? only.name);
      })
      .catch((e) => !isAbort(e) && onError(e instanceof Error ? e.message : String(e)));
    return () => controller.abort();
  }, [onError]);

  useEffect(() => {
    if (dir === null) {
      setListing(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api
      .list(dir, 0, PAGE, controller.signal)
      .then((l) => {
        setListing(l);
        setFilter("");
      })
      .catch((e) => {
        if (isAbort(e)) return;
        onError(e instanceof Error ? e.message : String(e));
        setDir(null);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [dir, onError]);

  const listed = listing?.path ?? null;
  useEffect(() => onDirectory?.(dir === null ? null : listed), [onDirectory, dir, listed]);

  const more = () => {
    if (!listing || dir === null) return;
    api
      .list(dir, listing.entries.length, PAGE)
      .then((next) => setListing({ ...next, entries: [...listing.entries, ...next.entries], offset: 0 }))
      .catch((e) => onError(e instanceof Error ? e.message : String(e)));
  };

  if (dir === null || !listing) {
    return (
      <div className="browser" data-testid="browser">
        <div className="panel-title">Données</div>
        {roots === null && <div className="hint">Chargement…</div>}
        {roots?.length === 0 && <div className="hint">Aucun dossier de données n'est accessible.</div>}
        {roots?.map((r) => (
          <button key={r.name} className="row dir" disabled={!r.available} onClick={() => setDir(r.name)}>
            <span className="icon">▸</span>
            <span className="name">{r.name}</span>
            {!r.available && <span className="meta">indisponible</span>}
          </button>
        ))}
      </div>
    );
  }

  const needle = filter.trim().toLowerCase();
  const shown = needle ? listing.entries.filter((e) => e.name.toLowerCase().includes(needle)) : listing.entries;
  const up = listing.parent;
  return (
    <div className="browser" data-testid="browser">
      <div className="panel-title" title={listing.path}>
        {listing.path}
      </div>
      <input
        className="filter"
        placeholder="Filtrer…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        aria-label="Filtrer les fichiers"
      />
      <div className="rows">
        <button className="row dir" onClick={() => setDir(up)}>
          <span className="icon">↑</span>
          <span className="name">{up === null ? "Racines" : ".."}</span>
        </button>
        {shown.map((e) =>
          e.kind === "dir" ? (
            <button key={e.path} className="row dir" onClick={() => setDir(e.path)}>
              <span className="icon">▸</span>
              <span className="name">{e.name}</span>
            </button>
          ) : onPicks && e.kind === "file" && !e.supported && isPicksFile(e.name) ? (
            <button
              key={e.path}
              className={`row file picks${e.path === picks ? " active" : ""}`}
              title="Superposer ces pointés à l'image affichée"
              onClick={() => onPicks(e.path)}
            >
              <span className="icon">◦</span>
              <span className="name">{e.name}</span>
              <span className="meta">{formatSize(e.size)}</span>
            </button>
          ) : (
            <button
              key={e.path}
              className={`row file${e.path === current ? " active" : ""}${e.supported ? "" : " unsupported"}`}
              disabled={!e.supported}
              title={e.kind === "broken" ? "lien cassé" : e.name}
              onClick={() => onOpen(e.path)}
            >
              <span className="icon">{e.kind === "broken" ? "✕" : "·"}</span>
              <span className="name">{e.name}</span>
              <span className="meta">{e.kind === "file" ? formatSize(e.size) : ""}</span>
            </button>
          ),
        )}
        {loading && <div className="hint">Chargement…</div>}
        {!loading && shown.length === 0 && <div className="hint">Aucun fichier.</div>}
        {listing.entries.length < listing.total && (
          <button className="row more" onClick={more}>
            Afficher la suite ({listing.total - listing.entries.length} restants)
          </button>
        )}
      </div>
    </div>
  );
}
