import { describe, expect, it } from "vitest";
import type { LiveItem } from "../src/api/types";
import {
  ALERT_LABELS,
  formatAgo,
  formatBytes,
  formatLiveLink,
  freshness,
  arrangeItems,
  DARK_ABOVE,
  filterItems,
  gridLayout,
  historyBars,
  iceLabel,
  iceSummary,
  iceThickness,
  imageFlags,
  itemDetails,
  mergeItems,
  metricsLines,
  orderItems,
  parseLiveLink,
  parseThreshold,
  relativeIce,
  sortValue,
  tilePosition,
} from "../src/live/store";

function item(id: number, patch: Partial<LiveItem> = {}): LiveItem {
  return {
    id,
    seq: id,
    name: `exposure_${id}`,
    dir: "Data",
    path: `cryo/session/Data/exposure_${id}.mrc`,
    category: "data",
    state: "complete",
    role: "image",
    size: 100,
    time: 1000 + id,
    time_source: "xml",
    first_seen: 2000 + id,
    completed_at: 2010 + id,
    verified: true,
    note: null,
    members: { image: "complete" },
    meta: {},
    metrics: null,
    ...patch,
  };
}

describe("following a session by its changes", () => {
  it("adds, replaces and removes exposures", () => {
    let known = mergeItems(new Map(), [item(1), item(2), item(3, { state: "incoming" })]);
    expect([...known.keys()]).toEqual([1, 2, 3]);
    known = mergeItems(known, [item(3, { seq: 9 }), item(1, { state: "deleted", seq: 10 }), item(4)]);
    expect([...known.keys()].sort()).toEqual([2, 3, 4]);
    expect(known.get(3)!.state).toBe("complete");
    expect(known.get(3)!.seq).toBe(9);
    expect(mergeItems(known, [item(99, { state: "deleted" })]).size).toBe(3); // unknown and gone: nothing to do
  });

  it("returns the same map when nothing changed", () => {
    const known = mergeItems(new Map(), [item(1)]);
    expect(mergeItems(known, [])).toBe(known);
  });

  it("orders by acquisition time, then by what is left", () => {
    const known = mergeItems(new Map(), [
      item(1, { time: 300 }),
      item(2, { time: 100 }),
      item(3, { time: null, first_seen: 200 }), // no acquisition time: when it was first seen
      item(4, { time: 100, name: "a" }),
      item(5, { time: 100, name: "a" }),
    ]);
    expect(orderItems(known).map((i) => i.id)).toEqual([4, 5, 2, 3, 1]);
  });
});

describe("the grid draws only what is in view", () => {
  it("fits whole columns and counts rows", () => {
    // 1000 px wide, tiles of 180 with gaps of 8: 5 columns (5 x 180 + 4 x 8 = 932)
    const layout = gridLayout(50_000, 1000, 600, 0, 180);
    expect(layout.columns).toBe(5);
    expect(layout.rows).toBe(10_000);
    expect(layout.height).toBe(10_000 * 188 - 8);
    expect(layout.first).toBe(0);
    expect(layout.last).toBe(5 * (Math.ceil(600 / 188) + 2 + 1) - 1); // rows in view, two more, inclusive
    expect(layout.last - layout.first + 1).toBeLessThan(40); // of fifty thousand
  });

  it("follows the scroll position", () => {
    const layout = gridLayout(50_000, 1000, 600, 188 * 1000, 180);
    expect(layout.first).toBe(5 * 998); // two rows above the first visible one
    expect(layout.last).toBe(5 * (1000 + Math.ceil(600 / 188) + 2 + 1) - 1);
    const end = gridLayout(47, 1000, 600, 99_999, 180);
    expect(end.last).toBe(46); // never beyond the last tile
    expect(end.rows).toBe(10);
  });

  it("copes with nothing to show and with a narrow container", () => {
    expect(gridLayout(0, 1000, 600, 0, 180)).toMatchObject({ rows: 0, height: 0, first: 0, last: -1 });
    expect(gridLayout(3, 50, 600, 0, 180)).toMatchObject({ columns: 1, rows: 3 });
  });

  it("places tiles row by row", () => {
    expect(tilePosition(0, 5, 180)).toEqual({ top: 0, left: 0 });
    expect(tilePosition(4, 5, 180)).toEqual({ top: 0, left: 4 * 188 });
    expect(tilePosition(5, 5, 180)).toEqual({ top: 188, left: 0 });
    expect(tilePosition(12, 5, 180)).toEqual({ top: 2 * 188, left: 2 * 188 });
  });
});

describe("wording", () => {
  it("says how long ago", () => {
    expect(formatAgo(0)).toBe("il y a 0 s");
    expect(formatAgo(59.9)).toBe("il y a 59 s");
    expect(formatAgo(185)).toBe("il y a 3 min 05 s");
    expect(formatAgo(2 * 3600 + 10 * 60)).toBe("il y a 2 h 10 min");
    expect(formatAgo(3 * 86400)).toBe("il y a 3 j");
    expect(formatAgo(-5)).toBe("il y a 0 s"); // clocks that disagree by a few seconds
  });

  it("grades the time since the last image", () => {
    expect([null, 0, 119, 120, 599, 600, 86400].map(freshness)).toEqual(["none", "fresh", "fresh", "late", "late", "stalled", "stalled"]);
  });

  it("formats sizes in decimal units, as disks are sold", () => {
    expect(formatBytes(999)).toBe("999 o");
    expect(formatBytes(1500)).toBe("1.5 Ko");
    expect(formatBytes(7.2e12)).toBe("7.2 To");
    expect(formatBytes(250e9)).toBe("250 Go");
  });

  it("puts the dose and the defocus under a tile when they are known", () => {
    expect(itemDetails(item(1))).toBe("");
    expect(itemDetails(item(1, { meta: { epu: { applied_defocus: -1.5, dose: 42.53 } } }))).toBe("-1.5 µm · 42.5 e⁻/Å²");
    expect(itemDetails(item(1, { meta: { mdoc: { tilt_angle: -3, dose: 3.05, defocus: -4 } } }))).toBe("-4.0 µm · 3.0 e⁻/Å² · -3.0°");
  });
});

describe("what was measured on an image", () => {
  const measured = (id: number, mean: number, ring?: number) => item(id, { metrics: { mean, std: 1, ice_ring: ring } });

  it("compares the counts of an image with the brightest of its session", () => {
    expect(relativeIce(measured(1, 50), 50)).toBe(0);
    expect(relativeIce(measured(1, 50 / Math.E), 50)).toBeCloseTo(1, 12);
    expect(relativeIce(measured(1, 60), 50)).toBeCloseTo(Math.log(50 / 60), 12); // brighter than the reference: negative
    // nothing to say without a measurement, without a reference, or with counts that are not counts
    expect(relativeIce(item(1), 50)).toBeNull();
    expect(relativeIce(item(1, { metrics: {} }), 50)).toBeNull();
    expect(relativeIce(measured(1, 50), null)).toBeNull();
    expect(relativeIce(measured(1, 0), 50)).toBeNull();
    expect(relativeIce(measured(1, -3), 50)).toBeNull();
    expect(relativeIce(measured(1, 50), 0)).toBeNull();
  });

  it("marks crystalline ice and images ten times darker than the others", () => {
    expect(imageFlags(item(1), 50)).toEqual([]);
    expect(imageFlags(measured(1, 40, 1.2), 50)).toEqual([]); // at the limit: not above
    expect(imageFlags(measured(1, 40, 1.21), 50)).toEqual(["ice_ring"]);
    expect(imageFlags(measured(1, 40, 1.9), null)).toEqual(["ice_ring"]); // needs no reference
    expect(DARK_ABOVE).toBeCloseTo(Math.log(10), 12);
    expect(imageFlags(measured(1, 5.1), 50)).toEqual([]);
    expect(imageFlags(measured(1, 4.9), 50)).toEqual(["dark"]);
    expect(imageFlags(measured(1, 4.9), null)).toEqual([]);
    expect(imageFlags(measured(1, 2, 3), 50)).toEqual(["ice_ring", "dark"]);
  });

  it("writes the relative ice on the tile and the numbers in its tooltip", () => {
    const one = item(1, { meta: { epu: { applied_defocus: -1.5, dose: 42.53 } }, metrics: { mean: 30.327, std: 2, ice_ring: 1.347 } });
    expect(iceLabel(one, 50)).toBe("glace 0.50");
    expect(iceLabel(one, null)).toBeNull(); // no reference yet
    expect(iceLabel(item(1), 50)).toBeNull(); // not measured
    expect(itemDetails(one)).toBe("-1.5 µm · 42.5 e⁻/Å²");
    expect(metricsLines(one, 50)).toEqual(["Coups moyens : 30.33", "Glace relative : 0.50 (ln I₀/I)", "Anneau de glace : 1.35"]);
    expect(metricsLines(one, null)).toEqual(["Coups moyens : 30.33", "Anneau de glace : 1.35"]);
    expect(metricsLines(item(1), 50)).toEqual([]);
    expect(metricsLines(item(1, { metrics: {} }), 50)).toEqual(["Image illisible : pas de mesure"]);
    const movie = item(1, { metrics: { mean: 1234.56, estimated: true, frames: 40 } });
    expect(metricsLines(movie, null)).toEqual(["Coups moyens : 1235 (estimés sur les premières frames)"]);
  });

  it("gives a thickness in nanometres once the mean free path is known", () => {
    const one = measured(1, 50 / Math.E); // relative ice 1
    expect(iceThickness(one, 50, 395)).toBeCloseTo(395, 9);
    expect(iceThickness(one, 50, null)).toBeNull();
    expect(iceThickness(item(1), 50, 395)).toBeNull();
    expect(iceLabel(one, 50, 395)).toBe("glace 395 nm");
    expect(iceLabel(one, 50)).toBe("glace 1.00");
    expect(metricsLines(one, 50, 395)).toEqual(["Coups moyens : 18.39", "Glace relative : 1.00 (ln I₀/I)", "Épaisseur de glace : 395 nm"]);
    const drifting = item(1, { metrics: { mean: 20, drift: 4.26, drift_unit: "A" } });
    expect(metricsLines(drifting, null)).toEqual(["Coups moyens : 20.00", "Dérive entre le début et la fin : 4.3 Å"]);
    expect(metricsLines(item(1, { metrics: { mean: 20, drift: 3, drift_unit: "px" } }), null)[1]).toBe("Dérive entre le début et la fin : 3.0 px");
  });

  it("tells the defocus measured next to the one asked for, and sorts on the fit", () => {
    const good = item(1, { meta: { epu: { applied_defocus: -1.5 } }, metrics: { mean: 20, ctf: { defocus: 1.62, fit: 3.84, score: 12 } } });
    expect(metricsLines(good, null)).toEqual([
      "Coups moyens : 20.00",
      "Défocus mesuré : 1.62 µm (visé 1.5)",
      "Anneaux de Thon retrouvés jusqu'à 3.8 Å",
    ]);
    const blind = item(2, { metrics: { mean: 20, ctf: { defocus: 2.4, fit: 7.5, score: 9 } } });
    expect(metricsLines(blind, null)[1]).toBe("Défocus mesuré : 2.40 µm");
    const none = item(3, { metrics: { mean: 20 } });
    expect(sortValue(good, "fit", null)).toBe(3.84);
    expect(sortValue(good, "focus", null)).toBe(1.62);
    expect(sortValue(none, "fit", null)).toBeNull();
    // the best fits first; the image without rings last
    expect(arrangeItems([none, blind, good], "fit", false, null).map((i) => i.id)).toEqual([1, 2, 3]);
    expect(arrangeItems([none, blind, good], "focus", true, null).map((i) => i.id)).toEqual([2, 1, 3]);
  });

  it("calls an image an empty hole only against the counts of an empty hole", () => {
    const bright = measured(1, 49); // within 5 % of the reference
    expect(imageFlags(bright, 50, false)).toEqual([]); // the reference is only the brightest of the session
    expect(imageFlags(bright, 50, true)).toEqual(["empty"]);
    expect(imageFlags(measured(1, 47), 50, true)).toEqual([]);
    expect(imageFlags(measured(1, 55), 50, true)).toEqual(["empty"]); // brighter still
  });

  it("sorts the tiles on a value, those without it last", () => {
    const a = item(1, { meta: { epu: { applied_defocus: -2.5, dose: 40 } }, metrics: { mean: 50, ice_ring: 1.1 } });
    const b = item(2, { meta: { epu: { applied_defocus: -1, dose: 42 } }, metrics: { mean: 25, ice_ring: 1.6 } });
    const c = item(3); // not measured, no metadata
    const d = item(4, { meta: { mdoc: { defocus: -4, dose: 3 } }, metrics: { mean: 40, drift: 2 } });
    const session = [a, b, c, d];
    expect(sortValue(a, "defocus", null)).toBe(2.5); // how far from focus, whatever the sign
    expect(sortValue(d, "dose", null)).toBe(3);
    expect(sortValue(c, "ice", 50)).toBeNull();
    expect(sortValue(b, "ice", 50)).toBeCloseTo(Math.log(2), 12);
    const names = (key: Parameters<typeof arrangeItems>[1], descending: boolean) => arrangeItems(session, key, descending, 50).map((i) => i.id);
    expect(names("time", false)).toEqual([1, 2, 3, 4]);
    expect(names("time", true)).toEqual([4, 3, 2, 1]);
    expect(names("ice", true)).toEqual([2, 4, 1, 3]); // thickest first; the unmeasured one last
    expect(names("ice", false)).toEqual([1, 4, 2, 3]); // and last again the other way round
    expect(names("ring", true)).toEqual([2, 1, 3, 4]);
    expect(names("defocus", false)).toEqual([2, 1, 4, 3]);
    expect(names("dose", true)).toEqual([2, 1, 4, 3]);
    expect(names("drift", true)).toEqual([4, 1, 2, 3]);
    expect(arrangeItems([], "ice", true, 50)).toEqual([]);
  });

  it("keeps the marked images, or those measured and without a mark", () => {
    const fine = measured(1, 45, 1.0);
    const icy = measured(2, 45, 1.9);
    const dark = measured(3, 2);
    const unmeasured = item(4);
    const incoming = item(5, { state: "incoming", metrics: { mean: 45, ice_ring: 3 } });
    const all = [fine, icy, dark, unmeasured, incoming];
    expect(filterItems(all, "all", 50, false)).toBe(all);
    expect(filterItems(all, "flagged", 50, false).map((i) => i.id)).toEqual([2, 3]);
    expect(filterItems(all, "clean", 50, false).map((i) => i.id)).toEqual([1]);
    expect(filterItems([measured(6, 49.5)], "flagged", 50, true).map((i) => i.id)).toEqual([6]); // an empty hole, once calibrated
  });

  it("summarises the last measured images of the session", () => {
    expect(iceSummary([], 50)).toBeNull();
    expect(iceSummary([item(1), item(2)], 50)).toBeNull();
    expect(iceSummary([measured(1, 40)], null)).toBeNull();
    const session = [measured(1, 50), measured(2, 50 / Math.E, 1.5), item(3), measured(4, 50 / Math.E ** 2), measured(5, 50 / Math.E ** 4, 2)];
    const all = iceSummary(session, 50);
    expect(all?.count).toBe(4);
    expect(all?.median).toBeCloseTo(1.5, 12); // of 0, 1, 2 and 4
    expect(all?.ring).toBe(0.5);
    const lastThree = iceSummary(session, 50, 3); // the most recent measured ones: 1, 2 and 4
    expect(lastThree?.count).toBe(3);
    expect(lastThree?.median).toBeCloseTo(2, 12);
    expect(lastThree?.ring).toBeCloseTo(2 / 3, 12);
  });
});

describe("the address of a session", () => {
  it("round-trips a directory, and tells a Live address from a viewer one", () => {
    expect(parseLiveLink("#path=cryo%2Fa.mrc&t=3")).toBeNull();
    expect(parseLiveLink("")).toBeNull();
    expect(parseLiveLink("#live")).toEqual({ dir: null });
    expect(parseLiveLink("#live=")).toEqual({ dir: null });
    const link = formatLiveLink("glacios/session 12/Images-Disc1");
    expect(link).toBe("#live=glacios%2Fsession+12%2FImages-Disc1");
    expect(parseLiveLink(link)).toEqual({ dir: "glacios/session 12/Images-Disc1" });
    expect(parseLiveLink(formatLiveLink(null))).toEqual({ dir: null });
  });
});

describe("acquisition histogram", () => {
  it("fills the empty buckets between the first and the last", () => {
    expect(historyBars([], 60, 100)).toEqual({ start: 0, values: [] });
    expect(historyBars([[600, 4], [660, 2], [840, 7]], 60, 100)).toEqual({ start: 600, values: [4, 2, 0, 0, 7] });
  });

  it("keeps the most recent bars when there are too many", () => {
    const counts: [number, number][] = Array.from({ length: 500 }, (_, k) => [k * 60, k]);
    const bars = historyBars(counts, 60, 120);
    expect(bars.values).toHaveLength(120);
    expect(bars.start).toBe(380 * 60);
    expect(bars.values[0]).toBe(380);
    expect(bars.values[119]).toBe(499);
  });
});

describe("alert thresholds as typed", () => {
  it("reads numbers, with a comma or a point", () => {
    expect(parseThreshold("10", 1, 1440)).toBe(10);
    expect(parseThreshold(" 2,5 ", 0.1, 50)).toBe(2.5);
    expect(parseThreshold("0.1", 0.1, 50)).toBe(0.1);
  });

  it("takes an empty field as 'not watched'", () => {
    expect(parseThreshold("", 1, 1440)).toBeNull();
    expect(parseThreshold("   ", 1, 1440)).toBeNull();
  });

  it("refuses what the server would refuse", () => {
    for (const text of ["0", "1441", "soon", "-5", "1e999", "NaN"]) expect(parseThreshold(text, 1, 1440)).toBeUndefined();
  });

  it("names every condition", () => {
    expect(Object.keys(ALERT_LABELS).sort()).toEqual(["disk", "no_image", "rate_drop", "scanner"]);
  });
});
