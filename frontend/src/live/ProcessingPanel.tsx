import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ProcessingState } from "../api/types";

interface Props {
  watchId: number;
  state: ProcessingState;
  onError: (message: string) => void;
}

const STEPS = { motion: "alignement des films", ctf: "ajustement du CTF" } as const;

/** The programs the administrator declared on the server (motion correction, CTF fit): run
 *  on each image that arrives once the user of the session asks for them. Shown only when the
 *  server has some. */
export function ProcessingPanel({ watchId, state, onError }: Props) {
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState<boolean | null>(null); // what was just asked, until the server says so
  useEffect(() => setAsked(null), [state.on]); // the server said: it is its word that counts
  const on = asked ?? state.on;
  const toggle = (next: boolean) => {
    setBusy(true);
    setAsked(next);
    api.live
      .setProcessing(watchId, next)
      .catch((e) => {
        setAsked(null);
        onError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setBusy(false));
  };
  if (!state.available) return null;
  return (
    <section className="panel" data-testid="processing">
      <div className="panel-title">Traitement</div>
      <label className="check" title="Les programmes déclarés sur le serveur tournent sur chaque image qui arrive pendant le suivi, la plus récente d'abord. Les utilisateurs passent à tour de rôle.">
        <input type="checkbox" checked={on} disabled={busy} data-testid="processing-on" onChange={(e) => toggle(e.target.checked)} />
        <span>{state.steps.map((s) => STEPS[s]).join(", puis ")}</span>
      </label>
      <div className="note" data-testid="processing-count">
        {state.done} image{state.done > 1 ? "s" : ""} traitée{state.done > 1 ? "s" : ""}
        {on && state.waiting > 0 ? `, ${state.waiting} en attente` : ""}
      </div>
    </section>
  );
}
