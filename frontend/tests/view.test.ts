import { describe, expect, it } from "vitest";
import {
  BASE_MAX_TEXELS,
  baseBin,
  detailRequest,
  fitView,
  formatLength,
  idealBin,
  imageToScreen,
  niceLength,
  panBy,
  regionContains,
  screenToImage,
  zoomAt,
} from "../src/viewer/view";

const viewport = { width: 1000, height: 800 };

describe("view geometry", () => {
  it("fits the whole image and centres it", () => {
    const view = fitView({ width: 4096, height: 4096 }, viewport, 1);
    expect(view.zoom).toBeCloseTo(800 / 4096);
    expect(imageToScreen(view, viewport, 2048, 2048)).toEqual([500, 400]);
    expect(imageToScreen(view, viewport, 0, 0)[1]).toBeCloseTo(0);
  });

  it("converts between screen and image coordinates both ways", () => {
    const view = { zoom: 2.5, cx: 120, cy: 77 };
    const [sx, sy] = imageToScreen(view, viewport, 33.25, 91.5);
    const [ix, iy] = screenToImage(view, viewport, sx, sy);
    expect(ix).toBeCloseTo(33.25);
    expect(iy).toBeCloseTo(91.5);
  });

  it("keeps the point under the cursor fixed while zooming", () => {
    let view = { zoom: 0.3, cx: 2000, cy: 1500 };
    const cursor: [number, number] = [137, 642];
    const before = screenToImage(view, viewport, ...cursor);
    for (const factor of [1.12, 1.12, 3, 0.2, 7]) view = zoomAt(view, viewport, factor, ...cursor);
    const after = screenToImage(view, viewport, ...cursor);
    expect(after[0]).toBeCloseTo(before[0], 6);
    expect(after[1]).toBeCloseTo(before[1], 6);
  });

  it("clamps the zoom", () => {
    const view = { zoom: 1, cx: 0, cy: 0 };
    expect(zoomAt(view, viewport, 1e9, 0, 0).zoom).toBe(64);
    expect(zoomAt(view, viewport, 1e-9, 0, 0).zoom).toBe(1 / 512);
  });

  it("pans by screen pixels", () => {
    const view = panBy({ zoom: 2, cx: 100, cy: 100 }, 20, -10);
    expect(view).toEqual({ zoom: 2, cx: 90, cy: 105 });
  });
});

describe("levels of detail", () => {
  it("bins the overview just enough to bound its size", () => {
    expect(baseBin({ width: 1024, height: 1024 })).toBe(1);
    expect(baseBin({ width: 2048, height: 2048 })).toBe(2);
    expect(baseBin({ width: 4096, height: 4096 })).toBe(4);
    expect(baseBin({ width: 5760, height: 4092 })).toBe(4);
    expect(baseBin({ width: 11520, height: 8184 })).toBe(8);
    const image = { width: 65536, height: 65536 };
    const bin = baseBin(image);
    expect(bin).toBe(64);
    expect(BASE_MAX_TEXELS).toBeGreaterThanOrEqual((image.width / bin) ** 2);
  });

  it("never requests more detail than the screen can show", () => {
    expect(idealBin(1, 1)).toBe(1); // one image pixel per device pixel
    expect(idealBin(4, 1)).toBe(1); // magnified
    expect(idealBin(0.5, 1)).toBe(2);
    expect(idealBin(0.5, 2)).toBe(1); // a high-density screen shows every pixel
    expect(idealBin(0.3, 1)).toBe(2); // 3.3 image px per device px: bin 2, not 4 (no lost detail)
    expect(idealBin(0.001, 1)).toBe(64);
  });

  it("requests the visible region, aligned on the bin grid, with a margin", () => {
    const image = { width: 4096, height: 4096 };
    const view = { zoom: 0.5, cx: 1000, cy: 1000 }; // 2000 x 1600 image pixels visible
    const req = detailRequest(view, viewport, image, 1);
    expect(req).not.toBeNull();
    const { bin, region } = req!;
    expect(bin).toBe(2);
    const [x, y, w, h] = region;
    expect(x % bin).toBe(0);
    expect(y % bin).toBe(0);
    expect(regionContains(region, [0, 200, 2000, 1600])).toBe(true); // the visible part, clamped at x = 0
    expect(x).toBe(0);
    expect(w).toBeLessThanOrEqual(2000 + 2 * 500 + bin);
    expect(x + w).toBeLessThanOrEqual(image.width);
    expect(y + h).toBeLessThanOrEqual(image.height);
  });

  it("clamps at the image edges, including odd sizes", () => {
    const image = { width: 4095, height: 4093 };
    const req = detailRequest({ zoom: 0.25, cx: 4000, cy: 4000 }, viewport, image, 1)!;
    const [x, y, w, h] = req.region;
    expect(x + w).toBe(4095);
    expect(y + h).toBe(4093);
    expect(x % req.bin).toBe(0);
  });

  it("coarsens the bin when the region would be too large", () => {
    const image = { width: 16384, height: 16384 };
    const view = fitView(image, viewport);
    const req = detailRequest(view, viewport, image, 2, 0.25, 1 << 20)!; // dpr 2 would like bin 8
    const texels = Math.ceil(req.region[2] / req.bin) * Math.ceil(req.region[3] / req.bin);
    expect(texels).toBeLessThanOrEqual(1 << 20);
    expect(req.bin).toBe(16);
  });

  it("returns nothing when the image is out of view", () => {
    const image = { width: 100, height: 100 };
    expect(detailRequest({ zoom: 1, cx: 5000, cy: 5000 }, viewport, image, 1)).toBeNull();
  });
});

describe("lengths", () => {
  it("picks round scale-bar lengths", () => {
    expect(niceLength(137)).toBe(100);
    expect(niceLength(260)).toBe(200);
    expect(niceLength(999)).toBe(500);
    expect(niceLength(0.034)).toBeCloseTo(0.02);
    expect(niceLength(0)).toBe(1);
  });

  it("formats lengths given in Å", () => {
    expect(formatLength(12.34)).toBe("12.3 Å");
    expect(formatLength(250)).toBe("25.0 nm");
    expect(formatLength(5000)).toBe("500 nm");
    expect(formatLength(25000)).toBe("2.50 µm");
    expect(formatLength(3e7)).toBe("3.00 mm");
  });
});
