/** The settings page: one's own account for everybody; for administrators, the accounts, the
 *  data roots, the microscope and the Live settings, the alert channels, the configuration
 *  file (read only) and the log of changes (ADR 014). What is saved applies at once. */

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, isAbort } from "../api/client";
import type { AccountRow, AdminState, LogEntry, Me, SettingSection } from "../api/types";
import {
  type ChannelDraft,
  channelDraft,
  channelsValue,
  type LiveDraft,
  liveValue,
  type MicroscopeDraft,
  microscopeValue,
  parseGroups,
  type RootDraft,
  rootDraft,
  rootsValue,
} from "./forms";

const TABS = [
  { key: "me", label: "Mon compte", admin: false },
  { key: "accounts", label: "Comptes", admin: true },
  { key: "roots", label: "Dossiers de données", admin: true },
  { key: "microscope", label: "Microscope et Live", admin: true },
  { key: "alerts", label: "Alertes", admin: true },
  { key: "config", label: "Configuration", admin: true },
  { key: "log", label: "Journal", admin: true },
] as const;

export type SettingsTab = (typeof TABS)[number]["key"];

export function isSettingsTab(text: string): text is SettingsTab {
  return TABS.some((t) => t.key === text);
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
const when = (t: number | null | undefined) =>
  t ? new Date(t * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "—";

interface Props {
  tab: SettingsTab;
  onTab: (tab: SettingsTab) => void;
  onViewer: () => void;
  onLive: () => void;
}

export function SettingsView({ tab, onTab, onViewer, onLive }: Props) {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<AdminState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api.me(controller.signal).then(setMe).catch((e) => !isAbort(e) && setError(message(e)));
    return () => controller.abort();
  }, []);

  const reload = useCallback(() => {
    api.admin.settings().then(setState).catch((e) => setError(message(e)));
  }, []);
  useEffect(() => {
    if (me?.admin) reload();
  }, [me?.admin, reload]);

  /** Send a change; a refusal because somebody saved meanwhile reloads what is now in force. */
  const save = useCallback(
    (what: string, send: (version: number) => Promise<AdminState>) => {
      if (!state) return;
      setError(null);
      setNote(null);
      send(state.version)
        .then((next) => {
          setState(next);
          setNote(`${what} : enregistré, en vigueur.`);
          if (what === "Compte") api.me().then(setMe).catch(() => undefined);
        })
        .catch((e) => {
          if (e instanceof ApiError && e.status === 409) {
            setError("Les réglages ont changé entre-temps (un autre administrateur) : la page montre maintenant les leurs.");
            reload();
          } else setError(message(e));
        });
    },
    [state, reload],
  );

  const shown = TABS.filter((t) => !t.admin || me?.admin);
  const current = shown.some((t) => t.key === tab) ? tab : "me";

  return (
    <div className="app settings-app">
      <header className="topbar">
        <span className="logo">imageBio</span>
        <div className="group">
          <button onClick={onViewer} data-testid="mode-viewer">Visionneuse</button>
          <button onClick={onLive} data-testid="mode-live">Live</button>
          <button className="on" data-testid="mode-settings">Paramètres</button>
        </div>
        <span className="spacer" />
        {me && me.auth_mode !== "none" && <span className="user">{me.user}</span>}
      </header>
      <nav className="settings-tabs" aria-label="Paramètres">
        {shown.map((t) => (
          <button key={t.key} className={t.key === current ? "on" : ""} onClick={() => onTab(t.key)} data-testid={`settings-tab-${t.key}`}>
            {t.label}
          </button>
        ))}
      </nav>
      <main className="settings-main">
        {error && (
          <div className="error" role="alert" data-testid="error">
            <span>{error}</span>
            <button onClick={() => setError(null)} aria-label="Fermer">×</button>
          </div>
        )}
        {note && <div className="note saved" data-testid="settings-note">{note}</div>}
        {current === "me" && me && <MyAccount me={me} />}
        {me?.admin && !state && current !== "me" && <div className="note">Lecture des réglages…</div>}
        {state && current === "accounts" && <Accounts state={state} me={me} save={save} />}
        {state && current === "roots" && <Roots state={state} save={save} />}
        {state && current === "microscope" && <MicroscopeAndLive state={state} save={save} />}
        {state && current === "alerts" && <Alerts state={state} save={save} />}
        {state && current === "config" && <Configuration state={state} />}
        {state && current === "log" && <Log version={state.version} />}
      </main>
    </div>
  );
}

type Save = (what: string, send: (version: number) => Promise<AdminState>) => void;

function Source({ state, section }: { state: AdminState; section: SettingSection }) {
  const own = state.sections[section].source === "interface";
  return (
    <span className={`source ${own ? "own" : ""}`} data-testid={`source-${section}`}>
      {own ? "changé ici (remplace le fichier de configuration)" : "fichier de configuration"}
    </span>
  );
}

function MyAccount({ me }: { me: Me }) {
  const proxyGroups = me.groups.filter((g) => !me.groups_given.includes(g));
  return (
    <section className="settings-section" data-testid="settings-me">
      <h2>Mon compte</h2>
      <dl>
        <dt>Nom</dt>
        <dd data-testid="me-name">{me.user}</dd>
        <dt>Connexion</dt>
        <dd>{me.auth_mode === "none" ? "poste seul (aucun mot de passe)" : "par le serveur d'accès (proxy)"}</dd>
        <dt>Rôle</dt>
        <dd data-testid="me-role">{me.admin ? "administrateur" : "utilisateur"}</dd>
        {me.auth_mode !== "none" && (
          <>
            <dt>Groupes</dt>
            <dd data-testid="me-groups">
              {proxyGroups.length ? proxyGroups.join(", ") : "—"} <span className="note">(du serveur d'accès)</span>
              {me.groups_given.length > 0 && (
                <>
                  <br />
                  {me.groups_given.join(", ")} <span className="note">(donnés par un administrateur)</span>
                </>
              )}
            </dd>
          </>
        )}
        <dt>Dossiers lisibles</dt>
        <dd data-testid="me-roots">
          {me.roots.length
            ? me.roots.map((r) => (
                <span key={r.name} className={`chip ${r.available ? "" : "warn"}`} title={r.available ? "" : "indisponible sur le serveur"}>
                  {r.name}
                </span>
              ))
            : "aucun"}
        </dd>
      </dl>
      <p className="note">Le mot de passe se change auprès du serveur d'accès (le compte du laboratoire ou l'authentification unique), pas ici.</p>
    </section>
  );
}

function Accounts({ state, me, save }: { state: AdminState; me: Me | null; save: Save }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState({ name: "", groups: "", admin: false });
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setDrafts({}), [state.version]);

  const send = (row: Pick<AccountRow, "name">, groupsText: string, admin: boolean) => {
    const groups = parseGroups(groupsText);
    if (groups.error !== null) {
      setError(groups.error);
      return;
    }
    setError(null);
    save("Compte", (version) => api.admin.setAccount(version, row.name, groups.value, admin));
  };
  return (
    <section className="settings-section" data-testid="settings-accounts">
      <h2>Comptes</h2>
      <p className="note">
        Les mots de passe restent au serveur d'accès. Ici, un administrateur donne à un nom de compte des groupes, qui
        s'ajoutent à ceux du serveur d'accès et ouvrent les dossiers de données de ces groupes, et le rôle
        d'administrateur. Les administrateurs nommés dans le fichier de configuration ne peuvent pas être retirés ici.
      </p>
      {error && <div className="note warn" data-testid="accounts-error">{error}</div>}
      <table className="settings-table" data-testid="accounts-table">
        <thead>
          <tr><th>Compte</th><th>Groupes donnés ici</th><th>Groupes du proxy</th><th>Administrateur</th><th>Vu</th><th /></tr>
        </thead>
        <tbody>
          {state.accounts.map((a) => {
            const fixed = a.admin === "file" || a.admin === "group";
            const text = drafts[a.name] ?? a.groups.join(", ");
            return (
              <tr key={a.name} data-testid="account-row" data-name={a.name}>
                <td className="mono">{a.name}</td>
                <td>
                  <input value={text} aria-label={`Groupes de ${a.name}`} data-testid="account-groups" list="known-groups"
                    onChange={(e) => setDrafts((d) => ({ ...d, [a.name]: e.target.value }))} />
                </td>
                <td className="note">{a.proxy_groups.join(", ") || "—"}</td>
                <td>
                  {fixed ? (
                    <span className="note">{a.admin === "file" ? "oui (fichier)" : "oui (groupe)"}</span>
                  ) : (
                    <input type="checkbox" checked={a.admin === "interface"} aria-label={`${a.name} administrateur`} data-testid="account-admin"
                      disabled={a.name === me?.user && a.admin === "interface"}
                      title={a.name === me?.user ? "On ne se retire pas soi-même le rôle" : ""}
                      onChange={(e) => send(a, text, e.target.checked)} />
                  )}
                </td>
                <td className="note">{a.seen ? when(a.seen) : "—"}</td>
                <td className="actions">
                  <button onClick={() => send(a, text, a.admin === "interface")} data-testid="account-save" disabled={drafts[a.name] === undefined}>Enregistrer</button>
                  {(a.groups.length > 0 || a.admin === "interface") && a.name !== me?.user && (
                    <button onClick={() => save("Compte", (v) => api.admin.removeAccount(v, a.name))} data-testid="account-remove" title="Retirer les groupes et le rôle donnés ici">Retirer</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <datalist id="known-groups">
        {state.groups.map((g) => <option key={g} value={g} />)}
      </datalist>
      <h3>Ajouter un compte</h3>
      <p className="note">Le nom tel que le serveur d'accès le transmet (l'identifiant de connexion). La personne n'a pas besoin d'être déjà venue.</p>
      <div className="settings-form">
        <input value={adding.name} placeholder="identifiant" aria-label="Identifiant du compte" data-testid="account-new-name"
          onChange={(e) => setAdding((a) => ({ ...a, name: e.target.value }))} />
        <input value={adding.groups} placeholder="groupes, séparés par des virgules" aria-label="Groupes du compte" data-testid="account-new-groups" list="known-groups"
          onChange={(e) => setAdding((a) => ({ ...a, groups: e.target.value }))} />
        <label className="check">
          <input type="checkbox" checked={adding.admin} data-testid="account-new-admin" onChange={(e) => setAdding((a) => ({ ...a, admin: e.target.checked }))} />
          administrateur
        </label>
        <button disabled={!adding.name.trim()} data-testid="account-add"
          onClick={() => {
            send({ name: adding.name.trim() }, adding.groups, adding.admin);
            setAdding({ name: "", groups: "", admin: false });
          }}>Ajouter</button>
      </div>
    </section>
  );
}

function SectionActions({ state, section, onSave, disabled, save }: { state: AdminState; section: SettingSection; onSave: () => void; disabled?: boolean; save: Save }) {
  return (
    <div className="buttons">
      <button onClick={onSave} disabled={disabled} data-testid={`save-${section}`}>Enregistrer</button>
      {state.sections[section].source === "interface" && (
        <button onClick={() => save("Retour au fichier", (v) => api.admin.resetSection(section, v))} data-testid={`reset-${section}`}
          title="Oublier ce qui a été changé ici et reprendre le fichier de configuration">Revenir au fichier</button>
      )}
    </div>
  );
}

function Roots({ state, save }: { state: AdminState; save: Save }) {
  const [drafts, setDrafts] = useState<RootDraft[]>(() => state.sections.roots.value.map(rootDraft));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setDrafts(state.sections.roots.value.map(rootDraft)), [state.version, state.sections.roots.value]);
  const set = (k: number, patch: Partial<RootDraft>) => setDrafts((d) => d.map((r, i) => (i === k ? { ...r, ...patch } : r)));
  const available = new Map(state.sections.roots.value.map((r) => [r.name, r.available]));
  const submit = () => {
    const checked = rootsValue(drafts);
    if (checked.error !== null) {
      setError(checked.error);
      return;
    }
    setError(null);
    save("Dossiers de données", (version) => api.admin.setSection("roots", version, checked.value));
  };
  return (
    <section className="settings-section" data-testid="settings-roots">
      <h2>Dossiers de données <Source state={state} section="roots" /></h2>
      <p className="note">
        Chaque dossier est lisible par les comptes de ses groupes (aucun groupe : tous les comptes). Un dossier ajouté ici doit
        exister, ne passer par aucun lien, et se trouver dans un dossier de données du fichier de configuration ou sous un
        dossier qu'il permet (server.root_parents) : <span className="mono">{state.root_parents.join(", ") || "aucun"}</span>.
        Un dossier dont le chemin a changé depuis (un lien posé à sa place) n'est plus servi. Tension, Cs et contraste
        d'amplitude : ceux du microscope de ce dossier, s'il n'est pas celui du serveur.
      </p>
      {error && <div className="note warn" data-testid="roots-error">{error}</div>}
      <table className="settings-table" data-testid="roots-table">
        <thead>
          <tr><th>Nom</th><th>Chemin sur le serveur</th><th>Groupes</th><th>kV</th><th>Cs</th><th>Ampl.</th><th /></tr>
        </thead>
        <tbody>
          {drafts.map((r, k) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={k} data-testid="root-row">
              <td><input value={r.name} aria-label="Nom du dossier" data-testid="root-name" onChange={(e) => set(k, { name: e.target.value })} /></td>
              <td>
                <input className={`mono wide-input ${available.get(r.name) === false ? "warn" : ""}`} value={r.path} aria-label="Chemin du dossier" data-testid="root-path"
                  onChange={(e) => set(k, { path: e.target.value })} title={available.get(r.name) === false ? "indisponible : le serveur ne le lit pas" : ""} />
              </td>
              <td><input value={r.groups} aria-label="Groupes du dossier" data-testid="root-groups" list="known-groups" onChange={(e) => set(k, { groups: e.target.value })} /></td>
              <td><input className="num" value={r.kv} aria-label="Tension" onChange={(e) => set(k, { kv: e.target.value })} /></td>
              <td><input className="num" value={r.cs_mm} aria-label="Cs" onChange={(e) => set(k, { cs_mm: e.target.value })} /></td>
              <td><input className="num" value={r.amplitude_contrast} aria-label="Contraste d'amplitude" onChange={(e) => set(k, { amplitude_contrast: e.target.value })} /></td>
              <td><button onClick={() => setDrafts((d) => d.filter((_, i) => i !== k))} aria-label={`Retirer ${r.name}`} data-testid="root-remove">×</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <datalist id="known-groups">
        {state.groups.map((g) => <option key={g} value={g} />)}
      </datalist>
      <div className="buttons">
        <button onClick={() => setDrafts((d) => [...d, { name: "", path: state.root_parents[0] ? `${state.root_parents[0]}/` : "/", groups: "", kv: "", cs_mm: "", amplitude_contrast: "" }])}
          data-testid="root-add">Ajouter un dossier</button>
      </div>
      <SectionActions state={state} section="roots" onSave={submit} save={save} />
    </section>
  );
}

function MicroscopeAndLive({ state, save }: { state: AdminState; save: Save }) {
  const micro = state.sections.microscope;
  const live = state.sections.live;
  const text = (v: number | null | undefined) => (v === null || v === undefined ? "" : String(v));
  const flag = (v: boolean | undefined): "" | "on" | "off" => (v === undefined ? "" : v ? "on" : "off");
  const initialMicro = (): MicroscopeDraft => ({ kv: text(micro.own.kv), cs_mm: text(micro.own.cs_mm), amplitude_contrast: text(micro.own.amplitude_contrast) });
  const initialLive = (): LiveDraft => ({
    report_after_idle_min: text(live.own.report_after_idle_min),
    mean_free_path_nm: text(live.own.mean_free_path_nm),
    metrics: flag(live.own.metrics),
    ctf: flag(live.own.ctf),
    drift: flag(live.own.drift),
  });
  const [m, setM] = useState<MicroscopeDraft>(initialMicro);
  const [l, setL] = useState<LiveDraft>(initialLive);
  const [error, setError] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setM(initialMicro()); setL(initialLive()); }, [state.version]);
  const yes = (v: boolean) => (v ? "oui" : "non");
  const switches: [keyof LiveDraft & ("metrics" | "ctf" | "drift"), string][] = [
    ["metrics", "Mesurer chaque image (coups, anneau de glace)"],
    ["ctf", "Estimer le CTF de chaque image"],
    ["drift", "Mesurer la dérive des films (lit huit frames de chacun)"],
  ];
  return (
    <section className="settings-section" data-testid="settings-microscope">
      <h2>Microscope <Source state={state} section="microscope" /></h2>
      <p className="note">Pour le CTF, quand ni le fichier d'acquisition, ni la session, ni le dossier de données ne les donnent. Champ vide : la valeur du fichier de configuration.</p>
      <div className="settings-grid">
        {([["kv", "Tension (kV)"], ["cs_mm", "Cs (mm)"], ["amplitude_contrast", "Contraste d'amplitude"]] as const).map(([key, label]) => (
          <label key={key}>
            <span>{label}</span>
            <input className="num" value={m[key]} placeholder={String(micro.file[key])} data-testid={`microscope-${key}`} onChange={(e) => setM((d) => ({ ...d, [key]: e.target.value }))} />
          </label>
        ))}
      </div>
      <SectionActions state={state} section="microscope" save={save} onSave={() => {
        const checked = microscopeValue(m);
        if (checked.error !== null) return setError(checked.error);
        setError(null);
        save("Microscope", (version) => api.admin.setSection("microscope", version, checked.value));
      }} />

      <h2>Live <Source state={state} section="live" /></h2>
      <div className="settings-grid">
        <label>
          <span>Rapport après un silence de (min ; 0 : jamais)</span>
          <input className="num" value={l.report_after_idle_min} placeholder={String(live.file.report_after_idle_min)} data-testid="live-idle"
            onChange={(e) => setL((d) => ({ ...d, report_after_idle_min: e.target.value }))} />
        </label>
        <label>
          <span>Libre parcours moyen dans la glace (nm)</span>
          <input className="num" value={l.mean_free_path_nm} placeholder={live.file.mean_free_path_nm === null ? "aucun" : String(live.file.mean_free_path_nm)}
            onChange={(e) => setL((d) => ({ ...d, mean_free_path_nm: e.target.value }))} />
        </label>
        {switches.map(([key, label]) => (
          <label key={key}>
            <span>{label}</span>
            <select value={l[key]} data-testid={`live-${key}`} onChange={(e) => setL((d) => ({ ...d, [key]: e.target.value as LiveDraft[typeof key] }))}>
              <option value="">comme le fichier ({yes(live.file[key])})</option>
              <option value="on">oui</option>
              <option value="off">non</option>
            </select>
          </label>
        ))}
      </div>
      {error && <div className="note warn" data-testid="microscope-error">{error}</div>}
      <SectionActions state={state} section="live" save={save} onSave={() => {
        const checked = liveValue(l);
        if (checked.error !== null) return setError(checked.error);
        setError(null);
        save("Live", (version) => api.admin.setSection("live", version, checked.value));
      }} />
    </section>
  );
}

function Alerts({ state, save }: { state: AdminState; save: Save }) {
  const section = state.sections.alerts;
  const [drafts, setDrafts] = useState<ChannelDraft[]>(() => section.value.map(channelDraft));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setDrafts(section.value.map(channelDraft)), [state.version, section.value]);
  const set = (k: number, patch: Partial<ChannelDraft>) => setDrafts((d) => d.map((c, i) => (i === k ? { ...c, ...patch } : c)));
  return (
    <section className="settings-section" data-testid="settings-alerts">
      <h2>Canaux d'alerte <Source state={state} section="alerts" /></h2>
      <p className="note">
        Les utilisateurs choisissent un canal par son nom ; ils ne voient jamais l'adresse. Une adresse ntfy vaut un mot de
        passe : qui la connaît lit les alertes. Le mot de passe du relais de courriel reste dans l'environnement du serveur.
      </p>
      {error && <div className="note warn" data-testid="alerts-error">{error}</div>}
      <table className="settings-table" data-testid="alerts-table">
        <thead><tr><th>Nom</th><th>Type</th><th>Adresse ou destinataires</th><th /></tr></thead>
        <tbody>
          {drafts.map((c, k) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={k} data-testid="channel-row">
              <td><input value={c.name} aria-label="Nom du canal" data-testid="channel-name" onChange={(e) => set(k, { name: e.target.value })} /></td>
              <td>
                <select value={c.kind} aria-label="Type du canal" data-testid="channel-kind" onChange={(e) => set(k, { kind: e.target.value as ChannelDraft["kind"] })}>
                  <option value="ntfy">ntfy (téléphone)</option>
                  <option value="webhook">webhook</option>
                  <option value="email" disabled={!section.email}>courriel{section.email ? "" : " (pas de relais)"}</option>
                </select>
              </td>
              <td><input className="mono wide-input" value={c.target} aria-label="Adresse du canal" data-testid="channel-target" onChange={(e) => set(k, { target: e.target.value })} /></td>
              <td><button onClick={() => setDrafts((d) => d.filter((_, i) => i !== k))} aria-label={`Retirer ${c.name}`}>×</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="buttons">
        <button onClick={() => setDrafts((d) => [...d, { name: "", kind: "ntfy", target: "https://ntfy.sh/" }])} data-testid="channel-add">Ajouter un canal</button>
      </div>
      <SectionActions state={state} section="alerts" save={save} onSave={() => {
        const checked = channelsValue(drafts, section.email);
        if (checked.error !== null) return setError(checked.error);
        setError(null);
        save("Canaux d'alerte", (version) => api.admin.setSection("alerts", version, checked.value));
      }} />
    </section>
  );
}

const LABELS: Record<string, string> = {
  server: "Serveur",
  auth: "Authentification",
  cache: "Caches",
  live: "Live",
  smtp: "Relais de courriel",
  processing: "Programmes externes",
};

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "oui" : "non";
  if (Array.isArray(value)) return value.length ? value.map(show).join(", ") : "—";
  if (typeof value === "object") return Object.entries(value as Record<string, unknown>).map(([k, v]) => `${k} : ${show(v)}`).join(" · ");
  return String(value);
}

function Configuration({ state }: { state: AdminState }) {
  const { version, ...sections } = state.configuration;
  return (
    <section className="settings-section" data-testid="settings-config">
      <h2>Configuration</h2>
      <p className="note">
        Ce qui ne se change que dans le fichier de configuration (imagebio.toml), puis en redémarrant le service : adresse et
        nom du serveur, authentification, caches, programmes externes. Les secrets ne sont jamais montrés : seulement s'ils
        sont réglés. Version d'imageBio : <span className="mono">{show(version)}</span>.
      </p>
      {Object.entries(sections).map(([name, values]) => (
        <div key={name} className="config-block">
          <h3>{LABELS[name] ?? name}</h3>
          {values === null || typeof values !== "object" ? (
            <p className="note">aucun</p>
          ) : (
            <dl>
              {Object.entries(values).map(([key, value]) => (
                <div key={key} className="config-row">
                  <dt className="mono">{key}</dt>
                  <dd className="mono">{show(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      ))}
    </section>
  );
}

function Log({ version }: { version: number }) {
  const [entries, setEntries] = useState<LogEntry[] | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.admin.log(controller.signal).then((r) => setEntries(r.entries)).catch(() => setEntries([]));
    return () => controller.abort();
  }, [version]);
  return (
    <section className="settings-section" data-testid="settings-log">
      <h2>Journal des changements</h2>
      <p className="note">Qui a changé quoi, et quand ; sans les adresses des canaux. Le fichier : settings-log.jsonl dans le dossier d'état du serveur.</p>
      <table className="settings-table" data-testid="log-table">
        <thead><tr><th>Quand</th><th>Qui</th><th>Quoi</th></tr></thead>
        <tbody>
          {entries?.map((e, k) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={k} data-testid="log-row"><td>{when(e.at)}</td><td className="mono">{e.by}</td><td>{e.what}</td></tr>
          ))}
        </tbody>
      </table>
      {entries && !entries.length && <p className="note">Rien n'a été changé dans cette page.</p>}
    </section>
  );
}
