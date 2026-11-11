/** Alerts of the session on screen: what to be told about, and where.
 *  The places are named by the server's administrator; nobody types an address here. */

import { useEffect, useState } from "react";
import { api, isAbort } from "../api/client";
import type { AlertCondition, AlertTestResult, LiveAlerts } from "../api/types";
import { ALERT_LABELS, parseThreshold } from "./store";

interface Props {
  watchId: number;
  /** Conditions firing at the moment, from the summary the page already polls. */
  firing: AlertCondition[];
  onError: (message: string) => void;
}

const FIELDS = [
  { key: "no_image_min", label: "Aucune image depuis", unit: "min", min: 1, max: 1440, testid: "alert-no-image" },
  { key: "disk_free_pct", label: "Espace libre sous", unit: "%", min: 0.1, max: 50, testid: "alert-disk" },
  { key: "rate_drop_pct", label: "Débit sous", unit: "% du débit horaire", min: 1, max: 99, testid: "alert-rate" },
] as const;

type Drafts = Record<(typeof FIELDS)[number]["key"], string>;

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function AlertsPanel({ watchId, firing, onError }: Props) {
  const [alerts, setAlerts] = useState<LiveAlerts | null>(null);
  const [channels, setChannels] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Drafts>({ no_image_min: "", disk_free_pct: "", rate_drop_pct: "" });
  const [saved, setSaved] = useState(false);
  const [tests, setTests] = useState<AlertTestResult[] | null>(null);

  const show = (a: LiveAlerts) => {
    setAlerts(a);
    setChannels(a.rules.channels);
    setDrafts({
      no_image_min: a.rules.no_image_min?.toString() ?? "",
      disk_free_pct: a.rules.disk_free_pct?.toString() ?? "",
      rate_drop_pct: a.rules.rate_drop_pct?.toString() ?? "",
    });
  };

  useEffect(() => {
    setAlerts(null);
    setTests(null);
    setSaved(false);
    const controller = new AbortController();
    api.live
      .alerts(watchId, controller.signal)
      .then(show)
      .catch((e) => !isAbort(e) && onError(message(e)));
    return () => controller.abort();
  }, [watchId, onError]);

  if (!alerts) return null;
  if (alerts.channels.length === 0) {
    return (
      <section className="panel" data-testid="alerts">
        <div className="panel-title">Alertes</div>
        <div className="hint">Aucun canal d'alerte n'est configuré sur ce serveur (section [alerts] de sa configuration).</div>
      </section>
    );
  }

  const parsed = FIELDS.map((f) => parseThreshold(drafts[f.key], f.min, f.max));
  const invalid = parsed.some((v) => v === undefined);
  const save = () => {
    api.live
      .setAlerts(watchId, {
        channels,
        no_image_min: parsed[0] ?? null,
        disk_free_pct: parsed[1] ?? null,
        rate_drop_pct: parsed[2] ?? null,
      })
      .then((a) => {
        show(a);
        setSaved(true);
        setTests(null);
      })
      .catch((e) => onError(message(e)));
  };
  const test = () => {
    api.live
      .testAlerts(watchId)
      .then(setTests)
      .catch((e) => onError(message(e)));
  };
  const toggle = (name: string) => {
    setSaved(false);
    setChannels((c) => (c.includes(name) ? c.filter((n) => n !== name) : [...c, name]));
  };

  return (
    <section className="panel" data-testid="alerts">
      <div className="panel-title">Alertes</div>
      {firing.length > 0 && (
        <div className="note warn" data-testid="alerts-firing">
          En cours : {firing.map((c) => ALERT_LABELS[c]).join(", ")}
        </div>
      )}
      <div className="field-label">Prévenir par</div>
      {alerts.channels.map((c) => (
        <label key={c.name} className="check">
          <input type="checkbox" checked={channels.includes(c.name)} onChange={() => toggle(c.name)} data-testid={`alert-channel-${c.name}`} />
          {c.name} <span className="kind">({c.kind})</span>
        </label>
      ))}
      {FIELDS.map((f, k) => (
        <label key={f.key} className={`threshold${parsed[k] === undefined ? " invalid" : ""}`}>
          <span>{f.label}</span>
          <input
            inputMode="decimal"
            value={drafts[f.key]}
            placeholder="—"
            aria-label={`${f.label} (${f.unit})`}
            data-testid={f.testid}
            onChange={(e) => {
              setSaved(false);
              setDrafts((d) => ({ ...d, [f.key]: e.target.value }));
            }}
          />
          <span>{f.unit}</span>
        </label>
      ))}
      <div className="buttons">
        <button onClick={save} disabled={invalid} data-testid="alerts-save">Enregistrer</button>
        <button onClick={test} disabled={alerts.rules.channels.length === 0} title="Envoie un message d'essai aux canaux enregistrés" data-testid="alerts-test">
          Tester
        </button>
        {saved && <span className="note" data-testid="alerts-saved">enregistré</span>}
      </div>
      {invalid && <div className="note warn">Une valeur est hors des bornes : laissez vide pour ne pas surveiller.</div>}
      {tests && (
        <ul className="test-results" data-testid="alerts-test-results">
          {tests.map((t) => (
            <li key={t.channel} className={t.delivered ? "" : "warn"}>
              {t.channel} : {t.delivered ? "envoyé" : `échec (${t.error ?? "?"})`}
            </li>
          ))}
        </ul>
      )}
      {alerts.firing.some((f) => f.error) && (
        <div className="note warn">Un message n'a pas pu partir : {alerts.firing.find((f) => f.error)?.error}</div>
      )}
    </section>
  );
}
