/** The forms of the settings page, as pure functions: what is typed, turned into what the
 *  server takes (admin/store.py), with the reason when it cannot be. The server checks again. */

import type { ChannelSetting, LiveSetting, MicroscopeSetting, RootSetting } from "../api/types";

/** A group name, as the server takes it (config.ROOT_NAME_RE). */
export const GROUP_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const ADDRESS_RE = /^[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}$/;

export type Checked<T> = { value: T; error: null } | { value: null; error: string };

const ok = <T>(value: T): Checked<T> => ({ value, error: null });
const fail = <T>(error: string): Checked<T> => ({ value: null, error });

/** Names separated by commas or spaces. */
export function splitNames(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean))];
}

export function parseGroups(text: string): Checked<string[]> {
  const groups = splitNames(text);
  const bad = groups.filter((g) => !GROUP_RE.test(g));
  return bad.length ? fail(`Nom de groupe invalide : ${bad.join(", ")} (lettres, chiffres, _ - .)`) : ok(groups);
}

/** A number typed in a field: empty is null (the value of the file), a decimal comma is a point. */
export function parseNumber(text: string, label: string, low: number, high: number): Checked<number | null> {
  const typed = text.trim().replace(",", ".");
  if (typed === "") return ok(null);
  const value = Number(typed);
  if (!Number.isFinite(value) || value < low || value > high) return fail(`${label} : un nombre de ${low} à ${high}`);
  return ok(value);
}

export interface RootDraft {
  name: string;
  path: string;
  groups: string;
  kv: string;
  cs_mm: string;
  amplitude_contrast: string;
}

export function rootDraft(root: RootSetting): RootDraft {
  const text = (v: number | null | undefined) => (v === null || v === undefined ? "" : String(v));
  return {
    name: root.name,
    path: root.path,
    groups: root.groups.join(", "),
    kv: text(root.kv),
    cs_mm: text(root.cs_mm),
    amplitude_contrast: text(root.amplitude_contrast),
  };
}

/** The roots as the server takes them, or why not. */
export function rootsValue(drafts: RootDraft[]): Checked<RootSetting[]> {
  const out: RootSetting[] = [];
  const names = new Set<string>();
  for (const d of drafts) {
    const name = d.name.trim();
    if (!GROUP_RE.test(name)) return fail(`Nom de dossier invalide : « ${name} » (lettres, chiffres, _ - .)`);
    if (names.has(name)) return fail(`Deux dossiers s'appellent « ${name} »`);
    names.add(name);
    const path = d.path.trim();
    if (!path.startsWith("/")) return fail(`${name} : le chemin doit être absolu (commencer par /)`);
    const groups = parseGroups(d.groups);
    if (groups.error !== null) return fail(`${name} : ${groups.error}`);
    const root: RootSetting = { name, path, groups: groups.value };
    for (const [key, label, low, high] of [
      ["kv", "tension (kV)", 20, 1500],
      ["cs_mm", "Cs (mm)", 0, 20],
      ["amplitude_contrast", "contraste d'amplitude", 0, 0.5],
    ] as const) {
      const n = parseNumber(d[key], `${name}, ${label}`, low, high);
      if (n.error !== null) return fail(n.error);
      if (n.value !== null) root[key] = n.value;
    }
    out.push(root);
  }
  return ok(out);
}

export interface MicroscopeDraft {
  kv: string;
  cs_mm: string;
  amplitude_contrast: string;
}

/** The microscope keys set in the page (an empty field: the file's value). */
export function microscopeValue(d: MicroscopeDraft): Checked<Partial<MicroscopeSetting>> {
  const out: Partial<MicroscopeSetting> = {};
  for (const [key, label, low, high] of [
    ["kv", "Tension (kV)", 20, 1500],
    ["cs_mm", "Cs (mm)", 0, 20],
    ["amplitude_contrast", "Contraste d'amplitude", 0, 0.5],
  ] as const) {
    const n = parseNumber(d[key], label, low, high);
    if (n.error !== null) return fail(n.error);
    if (n.value !== null) out[key] = n.value;
  }
  return ok(out);
}

export interface LiveDraft {
  report_after_idle_min: string;
  mean_free_path_nm: string;
  /** "" : the file's; "on" / "off" : set here. */
  metrics: "" | "on" | "off";
  ctf: "" | "on" | "off";
  drift: "" | "on" | "off";
}

export function liveValue(d: LiveDraft): Checked<Partial<LiveSetting>> {
  const out: Partial<LiveSetting> = {};
  const idle = d.report_after_idle_min.trim();
  if (idle !== "") {
    const n = Number(idle);
    if (!(Number.isInteger(n) && (n === 0 || (n >= 10 && n <= 10080)))) {
      return fail("Rapport après un silence : 0 (jamais) ou 10 à 10 080 minutes");
    }
    out.report_after_idle_min = n;
  }
  const mfp = parseNumber(d.mean_free_path_nm, "Libre parcours moyen (nm)", 50, 5000);
  if (mfp.error !== null) return fail(mfp.error);
  if (mfp.value !== null) out.mean_free_path_nm = mfp.value;
  for (const key of ["metrics", "ctf", "drift"] as const) if (d[key]) out[key] = d[key] === "on";
  return ok(out);
}

export interface ChannelDraft {
  name: string;
  kind: ChannelSetting["kind"];
  /** The address (ntfy, webhook) or the recipients (e-mail), as typed. */
  target: string;
}

export function channelDraft(c: ChannelSetting): ChannelDraft {
  return { name: c.name, kind: c.kind, target: c.kind === "email" ? (c.to ?? []).join(", ") : (c.url ?? "") };
}

export function channelsValue(drafts: ChannelDraft[], email: boolean): Checked<ChannelSetting[]> {
  const out: ChannelSetting[] = [];
  const names = new Set<string>();
  for (const d of drafts) {
    const name = d.name.trim();
    if (!GROUP_RE.test(name)) return fail(`Nom de canal invalide : « ${name} »`);
    if (names.has(name)) return fail(`Deux canaux s'appellent « ${name} »`);
    names.add(name);
    if (d.kind === "email") {
      if (!email) return fail(`${name} : le courriel demande un relais ([alerts.smtp] dans le fichier de configuration)`);
      const to = splitNames(d.target);
      if (!to.length || !to.every((a) => ADDRESS_RE.test(a))) return fail(`${name} : des adresses de courriel`);
      out.push({ name, kind: "email", to });
    } else {
      const url = d.target.trim();
      if (!/^https?:\/\/[^\s/?#]+(\/\S*)?$/.test(url)) return fail(`${name} : une adresse http(s)`);
      if (d.kind === "ntfy" && !new URL(url).pathname.replace(/\//g, "")) return fail(`${name} : l'adresse ntfy finit par un sujet`);
      out.push({ name, kind: d.kind, url });
    }
  }
  return ok(out);
}
