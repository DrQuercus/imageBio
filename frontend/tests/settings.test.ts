import { describe, expect, it } from "vitest";
import { channelsValue, liveValue, microscopeValue, parseGroups, rootDraft, rootsValue, splitNames } from "../src/settings/forms";

describe("settings forms", () => {
  it("reads groups and names as typed", () => {
    expect(splitNames(" cryoem, staff  lm;cryoem ")).toEqual(["cryoem", "staff", "lm"]);
    expect(parseGroups("cryoem, krios-2")).toEqual({ value: ["cryoem", "krios-2"], error: null });
    expect(parseGroups("cryo em, é")).toEqual({ value: null, error: expect.stringContaining("é") });
  });

  it("turns rows of roots into what the server takes, or says why not", () => {
    const row = rootDraft({ name: "krios", path: "/data/krios", groups: ["cryoem"], kv: 300, cs_mm: null });
    expect(row).toEqual({ name: "krios", path: "/data/krios", groups: "cryoem", kv: "300", cs_mm: "", amplitude_contrast: "" });
    expect(rootsValue([{ ...row, cs_mm: "2,7" }])).toEqual({
      value: [{ name: "krios", path: "/data/krios", groups: ["cryoem"], kv: 300, cs_mm: 2.7 }],
      error: null,
    });
    expect(rootsValue([row, row]).error).toContain("Deux dossiers");
    expect(rootsValue([{ ...row, path: "data/krios" }]).error).toContain("absolu");
    expect(rootsValue([{ ...row, name: "../x" }]).error).toContain("invalide");
    expect(rootsValue([{ ...row, kv: "3000" }]).error).toContain("tension");
    expect(rootsValue([{ ...row, groups: "a b!" }]).error).toContain("b!");
    expect(rootsValue([])).toEqual({ value: [], error: null });
  });

  it("leaves empty fields to the file", () => {
    expect(microscopeValue({ kv: "200", cs_mm: "", amplitude_contrast: "0,1" })).toEqual({
      value: { kv: 200, amplitude_contrast: 0.1 },
      error: null,
    });
    expect(microscopeValue({ kv: "abc", cs_mm: "", amplitude_contrast: "" }).error).toContain("Tension");
    expect(liveValue({ report_after_idle_min: "", mean_free_path_nm: "", metrics: "", ctf: "off", drift: "on" })).toEqual({
      value: { ctf: false, drift: true },
      error: null,
    });
    expect(liveValue({ report_after_idle_min: "5", mean_free_path_nm: "", metrics: "", ctf: "", drift: "" }).error).toContain("10");
    expect(liveValue({ report_after_idle_min: "0", mean_free_path_nm: "320", metrics: "", ctf: "", drift: "" }).value).toEqual({
      report_after_idle_min: 0,
      mean_free_path_nm: 320,
    });
  });

  it("checks alert channels as the server does", () => {
    expect(channelsValue([{ name: "phone", kind: "ntfy", target: "https://ntfy.sh/lab-topic" }], false)).toEqual({
      value: [{ name: "phone", kind: "ntfy", url: "https://ntfy.sh/lab-topic" }],
      error: null,
    });
    expect(channelsValue([{ name: "phone", kind: "ntfy", target: "https://ntfy.sh/" }], false).error).toContain("sujet");
    expect(channelsValue([{ name: "hook", kind: "webhook", target: "ftp://x" }], false).error).toContain("http");
    expect(channelsValue([{ name: "mail", kind: "email", target: "a@b.org" }], false).error).toContain("relais");
    expect(channelsValue([{ name: "mail", kind: "email", target: "a@b.org, c@d.org" }], true).value).toEqual([
      { name: "mail", kind: "email", to: ["a@b.org", "c@d.org"] },
    ]);
    expect(channelsValue([{ name: "mail", kind: "email", target: "not an address" }], true).error).toContain("adresses");
  });
});
