import { describe, expect, it } from "vitest";
import type { LiveItem } from "../src/api/types";
import {
  formatAgo,
  formatBytes,
  formatLiveLink,
  freshness,
  gridLayout,
  historyBars,
  itemDetails,
  mergeItems,
  orderItems,
  parseLiveLink,
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
