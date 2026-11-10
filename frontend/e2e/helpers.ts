/** Shared by the end-to-end tests: opening a file and reading what is on screen. */

import type { Page } from "@playwright/test";

/** Hooks exposed by the viewer (see ViewerCanvas.tsx). */
export interface Debug {
  readPixel(x: number, y: number): [number, number, number, number];
  toScreen(x: number, y: number): [number, number] | null;
  levels(): { base: number | null; detail: number | null };
  path(): string;
}

// Function predicates only: the page's Content-Security-Policy forbids eval, as it should.
/** Wait until the overview of `path` is on screen. */
export async function ready(page: Page, path: string): Promise<void> {
  await page.waitForFunction((expected) => {
    const d = (window as unknown as { __imagebio?: Debug }).__imagebio;
    return !!d && d.path() === expected && d.levels().base !== null && d.toScreen(0, 0) !== null;
  }, path);
  await page.waitForTimeout(200);
}

export async function openFile(page: Page, path: string, extra = ""): Promise<void> {
  await page.goto(`/#path=${encodeURIComponent(path)}${extra}`);
  await ready(page, path);
}

/** Screen position (page coordinates) of the centre of image pixel (x, y). */
export async function screenOf(page: Page, x: number, y: number): Promise<[number, number]> {
  const p = await page.evaluate(([px, py]) => ((window as unknown as { __imagebio: Debug }).__imagebio.toScreen(px!, py!)), [x + 0.5, y + 0.5]);
  const box = await page.getByTestId("viewer").boundingBox();
  if (!p || !box) throw new Error("viewer not ready");
  return [box.x + p[0], box.y + p[1]];
}

export async function hoverPixel(page: Page, x: number, y: number): Promise<void> {
  const [sx, sy] = await screenOf(page, x, y);
  await page.mouse.move(sx, sy);
}

/** Colour drawn at the centre of image pixel (x, y). */
export async function colourAt(page: Page, x: number, y: number): Promise<number[]> {
  return page.evaluate(([px, py]) => {
    const d = (window as unknown as { __imagebio: Debug }).__imagebio;
    const p = d.toScreen(px!, py!);
    return p ? Array.from(d.readPixel(p[0], p[1])) : [];
  }, [x + 0.5, y + 0.5]);
}

/** Count the plane requests and how many are in flight at once. */
export function trackPlanes(page: Page) {
  const t = { total: 0, inFlight: 0, maxInFlight: 0, urls: [] as string[] };
  const isPlane = (url: string) => url.includes("/api/datasets/plane");
  page.on("request", (r) => {
    if (!isPlane(r.url())) return;
    t.total++;
    t.urls.push(r.url());
    t.maxInFlight = Math.max(t.maxInFlight, ++t.inFlight);
  });
  const done = (r: { url(): string }) => {
    if (isPlane(r.url())) t.inFlight--;
  };
  page.on("requestfinished", done);
  page.on("requestfailed", done);
  return t;
}

/** Collect the API requests the server refused (status 400 and above). */
export function trackRefused(page: Page): string[] {
  const refused: string[] = [];
  page.on("response", (r) => {
    if (r.status() >= 400 && r.url().includes("/api/")) refused.push(`${r.status()} ${r.url()}`);
  });
  return refused;
}
