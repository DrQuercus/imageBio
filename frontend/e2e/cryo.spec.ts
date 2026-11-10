import { expect, type Page, test } from "@playwright/test";
import { hoverPixel, openFile, ready } from "./helpers";

/** Standard deviation of the plane on screen, as the Fichier panel shows it. */
async function shownStd(page: Page): Promise<number> {
  const text = await page.getByTestId("mean-std").innerText(); // "100 ± 0.768"
  return Number(text.split("±")[1]);
}

/** Resolves with the URL of the next plane request that contains `part`, once answered. */
function planeWith(page: Page, part: string): Promise<string> {
  return page
    .waitForResponse((r) => r.url().includes("/api/datasets/plane") && r.url().includes(part) && r.ok())
    .then((r) => r.url());
}

/** Grey level of the spectrum image at an offset from its centre, in spectrum pixels. */
async function spectrumGrey(page: Page, dx: number, dy: number): Promise<number> {
  return page.getByTestId("spectrum-image").evaluate(
    (canvas, [ox, oy]) => {
      const c = canvas as HTMLCanvasElement;
      const pixel = c.getContext("2d")!.getImageData(c.width / 2 + ox!, c.height / 2 + oy!, 1, 1).data;
      return pixel[0]!;
    },
    [dx, dy],
  );
}

/** Move the mouse over the spectrum, `dx` spectrum pixels to the right of its centre. */
async function hoverSpectrum(page: Page, dx: number, zoom = 1): Promise<void> {
  const box = await page.locator(".spectrum-box").boundingBox();
  if (!box) throw new Error("no spectrum on screen");
  const scale = (box.width / 512) * zoom;
  await page.mouse.move(box.x + box.width / 2 + dx * scale, box.y + box.height / 2);
}

test("a low-pass in Å removes what is finer than the cut-off", async ({ page }) => {
  // lattice of period 10 Å (amplitude 1) in noise of standard deviation 0.3, around 100
  await openFile(page, "demo/lattice.mrc");
  await expect.poll(() => shownStd(page)).toBeCloseTo(Math.sqrt(0.5 + 0.09), 2);

  const filtered = planeWith(page, "lp=20");
  await page.getByTestId("lp-20").click();
  const url = await filtered;
  expect(url).not.toContain("apix="); // the file knows its pixel size: the server uses it
  // A Gaussian with gain 1/2 at 20 Å has gain 1/16 at 10 Å: the lattice falls to 0.044 and the
  // noise to 0.028 (the filter keeps 0.9 % of the power of white noise at 16 px per cut-off).
  await expect.poll(() => shownStd(page)).toBeGreaterThan(0.04);
  await expect.poll(() => shownStd(page)).toBeLessThan(0.065);
  await expect(page.getByTestId("lp-20")).toHaveClass(/on/);
  await expect(page.getByTestId("cryo-filter")).toContainText("Nyquist 2.50 Å");
  await page.waitForTimeout(300);
  await hoverPixel(page, 500, 500);
  const value = Number((await page.getByTestId("hover-value").innerText()).split(" ")[0]);
  expect(Math.abs(value - 100)).toBeLessThan(0.3); // the grey level is kept, the lattice is gone

  // the link carries the filter
  await expect.poll(() => page.url()).toContain("lp=20");
  await page.goto("about:blank");
  await page.goto(url.replace(/\/api\/.*/, "/#path=demo%2Flattice.mrc&lp=20"));
  await ready(page, "demo/lattice.mrc");
  await expect(page.getByTestId("lp-20")).toHaveClass(/on/);
  await expect.poll(() => shownStd(page)).toBeLessThan(0.065);

  // a cut-off of one's own, then none
  await page.getByTestId("lp-input").fill("7,5"); // decimal comma, as typed on a French keyboard
  const custom = planeWith(page, "lp=7.5");
  await page.getByTestId("lp-input").press("Enter");
  await custom;
  await expect(page.getByTestId("lp-20")).not.toHaveClass(/on/);
  await page.getByTestId("lp-off").click();
  await expect.poll(() => shownStd(page)).toBeCloseTo(Math.sqrt(0.5 + 0.09), 2);
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("a band-pass that makes no sense is refused in the panel, not sent", async ({ page }) => {
  await openFile(page, "demo/lattice.mrc");
  const sent: string[] = [];
  page.on("request", (r) => r.url().includes("/api/datasets/") && sent.push(r.url()));
  await page.getByTestId("lp-20").click();
  await expect.poll(() => sent.some((u) => u.includes("lp=20"))).toBe(true);
  sent.length = 0;
  await page.getByTestId("hp-input").fill("10");
  await page.getByTestId("hp-input").press("Enter");
  await expect(page.getByTestId("cryo-problem")).toContainText("plus fin");
  await page.waitForTimeout(400);
  expect(sent.some((u) => u.includes("hp="))).toBe(false);
  await expect(page.getByTestId("error")).toHaveCount(0);
  await page.getByTestId("hp-input").fill("200");
  await page.getByTestId("hp-input").press("Enter");
  await expect(page.getByTestId("cryo-problem")).toHaveCount(0);
  await expect.poll(() => sent.some((u) => u.includes("lp=20") && u.includes("hp=200"))).toBe(true);
});

test("the mean of frames 3 to 7 is the mean of those five frames", async ({ page }) => {
  await openFile(page, "demo/steps.mrc"); // frame k (from 1) is 10 k + x
  await page.getByTestId("t-slider").fill("2");
  await expect(page.getByTestId("t-label")).toHaveText("Frame 3 / 10");
  await expect(page.getByTestId("avg-range")).toHaveText("Frame 3 seule");

  const averaged = planeWith(page, "t_end=6");
  await page.getByTestId("avg-input").fill("5");
  await page.getByTestId("avg-input").press("Enter");
  expect(await averaged).toContain("t=2&");
  await expect(page.getByTestId("t-label")).toHaveText("Frames 3–7 / 10");
  await expect(page.getByTestId("avg-range")).toHaveText("Moyenne des frames 3 à 7 (sur 10)");
  await page.waitForTimeout(300);
  await hoverPixel(page, 5, 9);
  await expect(page.getByTestId("hover-value")).toHaveText("55"); // (30 + 40 + 50 + 60 + 70) / 5 + 5
  await expect.poll(() => page.url()).toContain("avg=5");

  // the window follows the frame slider and stops at the end of the movie
  const end = planeWith(page, "t=8&");
  await page.getByTestId("t-slider").fill("8");
  expect(await end).toContain("t_end=9");
  await expect(page.getByTestId("t-label")).toHaveText("Frames 9–10 / 10");
  await page.waitForTimeout(300);
  await hoverPixel(page, 5, 9);
  await expect(page.getByTestId("hover-value")).toHaveText("100"); // (90 + 100) / 2 + 5

  const all = planeWith(page, "t_end=9");
  await page.getByTestId("t-slider").fill("0");
  await page.getByTestId("avg-all").click();
  await all;
  await expect(page.getByTestId("t-label")).toHaveText("Frames 1–10 / 10");
  await page.waitForTimeout(300);
  await hoverPixel(page, 0, 0);
  await expect(page.getByTestId("hover-value")).toHaveText("55"); // mean of 10, 20, … 100

  await page.getByTestId("avg-off").click();
  await expect(page.getByTestId("t-label")).toHaveText("Frame 1 / 10");
  await page.waitForTimeout(400);
  await hoverPixel(page, 1, 1);
  await hoverPixel(page, 0, 0);
  await expect(page.getByTestId("hover-value")).toHaveText("10");
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("the power spectrum puts a 10 Å lattice on the 10 Å ring", async ({ page }) => {
  await openFile(page, "demo/lattice.mrc"); // period 8 px at 1.25 Å per pixel, along x
  await page.getByTestId("viewer").hover();
  await page.keyboard.press("p");
  await expect(page.getByTestId("spectrum-image")).toHaveAttribute("data-side", "512");
  await expect(page.getByTestId("spectrum-toggle")).toHaveClass(/on/);
  await expect(page.getByTestId("spectrum-rings")).toContainText("10 Å");
  await expect(page.getByTestId("spectrum-rings")).toContainText("glace 3.7 Å");
  await expect(page.getByTestId("spectrum-rings")).toContainText("Nyquist 2.50 Å");

  // 512 x 1.25 / 10 = 64 px from the centre, on the horizontal axis and nowhere else
  expect(await spectrumGrey(page, 64, 0)).toBe(255);
  expect(await spectrumGrey(page, -64, 0)).toBe(255);
  const elsewhere = [];
  for (const [dx, dy] of [[0, 64], [0, -64], [45, 45], [-45, 45], [40, 0], [90, 0]] as const) {
    elsewhere.push(await spectrumGrey(page, dx, dy));
  }
  expect(Math.max(...elsewhere)).toBeLessThan(255);

  await hoverSpectrum(page, 64);
  await expect(page.getByTestId("spectrum-readout")).toContainText("10.0 Å");
  await expect(page.getByTestId("spectrum-readout")).toContainText("0.1000 Å⁻¹");

  // enlarged twice: the peak is twice as far on screen, at the same resolution
  await page.getByTestId("spectrum-zoom-2").click();
  await expect.poll(() => spectrumGrey(page, 128, 0)).toBe(255);
  await hoverSpectrum(page, 64, 2);
  await expect(page.getByTestId("spectrum-readout")).toContainText("10.0 Å");

  await expect.poll(() => page.url()).toContain("ps=1");
  await page.getByTestId("viewer").hover();
  await page.keyboard.press("p");
  await expect(page.getByTestId("spectrum")).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("EER fractions are grouped from the panel, and filters wait for a pixel size", async ({ page }) => {
  await openFile(page, "demo/falcon.eer"); // 24 fractions, one electron on (20, 10) in each
  await expect(page.getByTestId("t-label")).toHaveText("Frame 1 / 24");
  await expect(page.getByTestId("eer-group")).toHaveValue("1");
  await hoverPixel(page, 20, 10);
  await expect(page.getByTestId("hover-value")).toHaveText("1");

  await page.getByTestId("eer-group").fill("6");
  await page.getByTestId("eer-group").press("Enter");
  await expect(page.getByTestId("t-label")).toHaveText("Frame 1 / 4");
  await ready(page, "demo/falcon.eer");
  await hoverPixel(page, 21, 11);
  await hoverPixel(page, 20, 10);
  await expect(page.getByTestId("hover-value")).toHaveText("6");
  await expect.poll(() => page.url()).toContain("opt.group=6");

  // an EER file does not store its pixel size: no filter in Å until the user gives it
  const sent: string[] = [];
  page.on("request", (r) => r.url().includes("/api/datasets/") && sent.push(r.url()));
  await page.getByTestId("lp-10").click();
  await expect(page.getByTestId("cryo-problem")).toContainText("Taille de pixel inconnue");
  await page.waitForTimeout(400);
  expect(sent.some((u) => u.includes("lp="))).toBe(false);
  await page.getByTestId("user-apix").fill("1.2");
  await expect(page.getByTestId("cryo-problem")).toHaveCount(0);
  await expect.poll(() => sent.some((u) => u.includes("lp=10") && u.includes("apix=1.2") && u.includes("opt.group=6"))).toBe(true);
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("colour pictures have no Cryo tools", async ({ page }) => {
  await openFile(page, "demo/colours.png", "&lp=20&ps=1");
  await expect(page.getByTestId("info")).toBeVisible();
  await expect(page.getByTestId("cryo")).toHaveCount(0);
  await expect(page.getByTestId("spectrum")).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
});
