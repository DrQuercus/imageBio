import { type ReactNode, useEffect, useRef, useState } from "react";
import type { DatasetInfo } from "../api/types";
import { formatResolution } from "../viewer/cryo";
import { formatValue } from "./ContrastPanel";
import { SOURCES } from "./Panels";

/** Wait after the last digit typed before a pixel size is applied, in ms: each value applied
 *  can cost a filter on the server, the digits typed on the way must not. */
const TYPING_MS = 400;

interface Props {
  info: DatasetInfo;
  /** Å per pixel read in the file, or null. */
  fileApix: number | null;
  /** Å per pixel typed by the user (it replaces the file's), or null. */
  userApix: number | null;
  onUserApix: (apix: number | null) => void;
  /** Bin of the finest level on screen (1: full resolution). */
  level: number;
  children?: ReactNode;
}

/** The pixel size in Å, always in view and always editable, as in the first CryoViewer: it is
 *  what every length, filter and CTF of the image rests on. A value typed replaces the file's
 *  (a header can be wrong); "Rétablir" goes back to it. */
export function ScalePanel({ info, fileApix, userApix, onUserApix, level, children }: Props) {
  const apix = userApix ?? fileApix;
  const shown = (value: number | null) => (value === null ? "" : formatValue(value));
  const [draft, setDraft] = useState(shown(apix));
  const timer = useRef<number | undefined>(undefined);
  // The field is rewritten from the value in effect only when nobody is typing in it ("1." is
  // on its way to "1.06", not 1), and a value is applied only when it was typed: passing
  // through the field must not turn the header's value into a typed one.
  const focused = useRef(false);
  const edited = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(shown(apix));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apix]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const commit = (text: string) => {
    window.clearTimeout(timer.current);
    if (!edited.current) return;
    const typed = text.trim().replace(",", ".");
    if (typed === "") {
      edited.current = false;
      onUserApix(null);
      return;
    }
    const value = Number(typed);
    if (!(value > 0) || !Number.isFinite(value) || value > 1e7) return; // not a usable size: wait
    edited.current = false;
    // the header's value as the field shows it (six digits) is the header's value
    onUserApix(fileApix !== null && Math.abs(value - fileApix) <= 5e-6 * fileApix ? null : value);
  };
  const cal = info.calibration.x;
  const source = SOURCES[cal.source] ?? cal.source;

  let origin: ReactNode;
  if (userApix !== null && fileApix !== null) {
    origin = (
      <div className="note warn" data-testid="apix-source">
        Saisie : remplace {formatValue(fileApix)} Å/px ({source}).{" "}
        <button data-testid="apix-reset" onClick={() => onUserApix(null)}>Rétablir</button>
      </div>
    );
  } else if (userApix !== null) {
    origin = <div className="note" data-testid="apix-source">Saisie : le fichier n'en donne pas.</div>;
  } else if (fileApix !== null) {
    origin = <div className="note" data-testid="apix-source">{source[0]?.toUpperCase()}{source.slice(1)}</div>;
  } else {
    origin = (
      <div className="note warn" data-testid="apix-source">
        Inconnue : saisissez-la pour mesurer et filtrer en Å.
      </div>
    );
  }

  return (
    <section className="panel scale" data-testid="scale">
      <div className="apix-row">
        <label className="apix-label" htmlFor="apix-input">Å / pixel</label>
        <input id="apix-input" className={`apix-input ${apix === null ? "unknown" : ""}`} inputMode="decimal" value={draft}
          placeholder="à saisir" aria-label="Taille de pixel en ångströms" data-testid="user-apix"
          onFocus={() => { focused.current = true; }}
          onChange={(e) => {
            edited.current = true;
            setDraft(e.target.value);
            window.clearTimeout(timer.current);
            const text = e.target.value;
            if (!/[.,]\s*$/.test(text)) timer.current = window.setTimeout(() => commit(text), TYPING_MS);
          }}
          onBlur={() => {
            focused.current = false;
            commit(draft);
            edited.current = false;
            setDraft(shown(apix)); // replaced by the new value, if one was applied
          }}
          onKeyDown={(e) => e.key === "Enter" && commit(draft)} />
      </div>
      {origin}
      {apix !== null && (
        <div className="note" data-testid="apix-level">
          {level > 1
            ? `À l'écran : ${formatValue(Number((apix * level).toPrecision(4)))} Å/px (réduit ${level}×)`
            : "À l'écran : pleine résolution"}
          {" · "}Nyquist {formatResolution(2 * apix)}
        </div>
      )}
      {children}
    </section>
  );
}
