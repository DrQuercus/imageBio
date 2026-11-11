import { expect, type Page, test } from "@playwright/test";
import { type Debug, hoverPixel, openFile, ready, trackRefused } from "./helpers";

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
  const shownValue = async () => Number((await page.getByTestId("hover-value").innerText()).split(" ")[0]);
  await hoverPixel(page, 512, 300); // a crest of the lattice: cos(2 pi 512 / 8) = 1
  expect(await shownValue()).toBeGreaterThan(100.4);

  const filtered = planeWith(page, "lp=20");
  await page.getByTestId("lp-20").focus();
  await page.keyboard.press("Enter"); // the mouse stays on the image
  const url = await filtered;
  // the value under the still cursor is that of the filtered image, not the one read before
  await expect.poll(shownValue).toBeLessThan(100.3);
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

test("regrouping a movie from its last frames asks for nothing that does not exist", async ({ page }) => {
  // Regression: while the regrouped movie was being described, planes were requested with the
  // new grouping and the old frame number (frame 21 of a movie that now has 4), and the
  // server's refusal was shown as an error.
  const refused = trackRefused(page);
  await openFile(page, "demo/falcon.eer", "&t=20&avg=3"); // 24 frames of one fraction
  await expect(page.getByTestId("t-label")).toHaveText("Frames 21–23 / 24");
  await page.getByTestId("eer-group").fill("6");
  await page.getByTestId("eer-group").press("Enter");
  await expect(page.getByTestId("t-label")).toHaveText("Frame 4 / 4"); // the last frame, alone
  await ready(page, "demo/falcon.eer");
  await page.getByTestId("eer-group").fill("1");
  await page.getByTestId("eer-group").press("Enter");
  await expect(page.getByTestId("t-label")).toHaveText("Frames 4–6 / 24");
  // from a movie to a single image: the frame range of the movie does not follow
  await page.goto("/#path=demo%2Ffalcon.eer&t=20&avg=4");
  await ready(page, "demo/falcon.eer");
  await page.getByTestId("browser").getByRole("button", { name: "gradient.mrc" }).click();
  await ready(page, "demo/gradient.mrc");
  await page.waitForTimeout(300);
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
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

test("EER fractions are grouped by dose once the pixel size is known", async ({ page }) => {
  // 4000 electrons per fraction on 128 x 128 pixels: 0.244 e per pixel and fraction
  await openFile(page, "demo/falcon/pattern.eer");
  await expect(page.getByTestId("eer-dose")).toBeDisabled(); // no pixel size in an EER file
  await page.getByTestId("user-apix").fill("1");
  await expect(page.getByTestId("eer-dose")).toBeEnabled();
  await page.getByTestId("eer-dose").click(); // 0.244 e/Å² per fraction: 4 fractions for about 1 e/Å²
  await expect(page.getByTestId("eer-group")).toHaveValue("4");
  await expect(page.getByTestId("t-label")).toHaveText("Frame 1 / 6");
  await page.getByTestId("user-apix").fill("0.5"); // 0.977 e/Å² per fraction: one is enough
  await page.getByTestId("eer-dose").click();
  await expect(page.getByTestId("eer-group")).toHaveValue("1");
  await expect(page.getByTestId("t-label")).toHaveText("Frame 1 / 24");
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("colour pictures have no Cryo tools", async ({ page }) => {
  await openFile(page, "demo/colours.png", "&lp=20&ps=1");
  await expect(page.getByTestId("info")).toBeVisible();
  await expect(page.getByTestId("cryo")).toHaveCount(0);
  await expect(page.getByTestId("spectrum")).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("a gain stored upside down is noticed, and corrected in one click", async ({ page }) => {
  const refused = trackRefused(page);
  // 24 fractions from a detector with a fixed pattern; the mean of all of them is on screen
  await openFile(page, "demo/falcon/pattern.eer", "&avg=24");
  await expect(page.getByTestId("t-label")).toHaveText("Frames 1–24 / 24");
  const raw = await shownStd(page);
  await expect(page.getByTestId("gain-select").locator("option")).toHaveText(["Aucune", "flipped.gain", "good.gain"]);
  await expect(page.getByTestId("gain-status")).toHaveCount(0);

  const applied = planeWith(page, "gain=demo%2Ffalcon%2Fflipped.gain");
  await page.getByTestId("gain-select").selectOption({ label: "flipped.gain" });
  expect(await applied).not.toContain("gain_orient");
  await expect(page.getByTestId("gain-status")).toContainText("miroir haut-bas");
  await expect.poll(() => shownStd(page)).toBeGreaterThan(raw); // upside down, it adds its pattern to the detector's

  const fixed = planeWith(page, "gain_orient=6");
  await page.getByTestId("gain-fix").click();
  await fixed;
  await expect(page.getByTestId("gain-orient")).toHaveValue("6");
  await expect(page.getByTestId("gain-status")).toContainText("Orientation vérifiée sur 20 frames · 1 pixel défectueux");
  // The pattern is gone and shot noise remains: 0.244 count per pixel and fraction, at most one
  // per fraction, gives sqrt(0.244 x 0.756 / 24) = 0.088 on the mean of 24, and 0.090 once
  // weighted by the gain. The raw image adds the pattern of the detector (0.05) to it.
  await expect.poll(() => shownStd(page)).toBeLessThan(raw * 0.96);
  expect(await shownStd(page)).toBeGreaterThan(0.085);
  expect(await shownStd(page)).toBeLessThan(0.096);
  await page.waitForTimeout(300);
  await hoverPixel(page, 70, 40); // the hot pixel: 1 count per fraction in the file
  const hot = Number((await page.getByTestId("hover-value").innerText()).split(" ")[0]);
  expect(hot).toBeLessThan(0.6); // filled from its neighbours, which hold about a quarter of a count
  await expect.poll(() => page.url()).toContain("gain=demo%2Ffalcon%2Fflipped.gain");
  expect(page.url()).toContain("go=6");

  // the reference stored the right way is confirmed as it is
  await page.getByTestId("gain-select").selectOption({ label: "good.gain" });
  await expect(page.getByTestId("gain-orient")).toHaveValue("0");
  await expect(page.getByTestId("gain-status")).toContainText("Orientation vérifiée");
  await expect.poll(() => shownStd(page)).toBeLessThan(raw * 0.96);

  // the spectrum is that of the corrected image
  const spectrum = page.waitForResponse((r) => r.url().includes("/api/datasets/spectrum?") && r.url().includes("gain=") && r.ok());
  await page.getByTestId("spectrum-toggle").click();
  await spectrum;
  await expect(page.getByTestId("spectrum-rings")).toContainText("gain appliqué");

  // another kind of file in the same directory does not inherit the gain
  const sent: string[] = [];
  page.on("request", (r) => r.url().includes("/api/datasets/") && sent.push(r.url()));
  await page.getByTestId("browser").getByRole("button", { name: "aligned.mrc" }).click();
  await ready(page, "demo/falcon/aligned.mrc");
  await expect(page.getByTestId("gain-select")).toHaveValue("");
  expect(sent.filter((u) => u.includes("aligned.mrc")).some((u) => u.includes("gain="))).toBe(false);
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]); // nor the frame range of the movie: nothing was asked that does not exist
});

/** Strongest opacity of the overlay (ruler, particles) around image position (x, y). */
async function overlayAlpha(page: Page, x: number, y: number): Promise<number> {
  return page.evaluate(([px, py]) => {
    const p = (window as unknown as { __imagebio: Debug }).__imagebio.toScreen(px!, py!);
    const canvas = document.querySelector('[data-testid="viewer-overlay"]') as HTMLCanvasElement;
    if (!p) return -1;
    const dpr = window.devicePixelRatio || 1;
    const data = canvas.getContext("2d")!.getImageData(Math.round(p[0] * dpr) - 2, Math.round(p[1] * dpr) - 2, 5, 5).data;
    let alpha = 0;
    for (let k = 3; k < data.length; k += 4) alpha = Math.max(alpha, data[k]!);
    return alpha;
  }, [x, y]);
}

test("particle coordinates are drawn over the image, from a file about it or about a whole data set", async ({ page }) => {
  const refused = trackRefused(page);
  await openFile(page, "demo/picks/mic_001.mrc");
  const viewer = page.getByTestId("viewer");
  await expect(viewer).not.toHaveAttribute("data-picks");
  expect(await overlayAlpha(page, 84, 64)).toBe(0);

  // the box file that bears the name of the image is offered
  await expect(page.getByTestId("picks-candidate")).toHaveText("mic_001.box");
  await page.getByTestId("picks-candidate").click();
  await expect(page.getByTestId("picks-status")).toHaveText("3 particules sur cette image");
  await expect(viewer).toHaveAttribute("data-picks", "3");
  await expect(viewer).toHaveAttribute("data-picks-drawn", "3");
  await expect(page.getByTestId("picks-diameter")).toHaveAttribute("placeholder", "40"); // the boxes of the file
  // a circle 40 pixels across around (64, 64): its edge is drawn, its centre and the next particle's place are not
  await expect.poll(() => overlayAlpha(page, 84, 64)).toBeGreaterThan(100);
  expect(await overlayAlpha(page, 64, 44)).toBeGreaterThan(100);
  expect(await overlayAlpha(page, 64, 64)).toBe(0);
  expect(await overlayAlpha(page, 148, 64)).toBe(0);
  // rows counted from the bottom: the particle of (128, 192) is drawn at (128, 64)
  await page.getByTestId("picks-flip").check();
  await expect.poll(() => overlayAlpha(page, 148, 64)).toBeGreaterThan(100);
  expect(await overlayAlpha(page, 84, 64)).toBe(0);
  await page.getByTestId("picks-flip").uncheck();
  // a diameter typed by hand
  await page.getByTestId("picks-diameter").fill("80");
  await page.getByTestId("picks-diameter").press("Enter");
  await expect.poll(() => overlayAlpha(page, 104, 64)).toBeGreaterThan(100);
  expect(await overlayAlpha(page, 84, 64)).toBe(0);
  // the address follows a moment later
  await expect.poll(() => page.evaluate(() => window.location.hash)).toContain("picks=demo%2Fpicks%2Fmic_001.box");
  await expect.poll(() => page.evaluate(() => window.location.hash)).toContain("pd=80");

  // a file about this image only does not follow to the next image
  await page.getByTestId("browser").getByText("mic_002.mrc", { exact: true }).click();
  await ready(page, "demo/picks/mic_002.mrc");
  await expect(viewer).not.toHaveAttribute("data-picks");
  await expect(page.getByTestId("picks-hint")).toBeVisible(); // nothing bears its name

  // a file of a whole data set, chosen in the list: the particles of the image shown, then of the next
  await page.getByTestId("browser").getByText("particles.star", { exact: true }).click();
  await expect(page.getByTestId("picks-file")).toHaveText("particles.star");
  await expect(page.getByTestId("picks-status")).toHaveText("1 particule sur cette image");
  await page.getByTestId("browser").getByText("mic_001.mrc", { exact: true }).click();
  await ready(page, "demo/picks/mic_001.mrc");
  await expect(page.getByTestId("picks-status")).toHaveText("2 particules sur cette image");
  await expect(viewer).toHaveAttribute("data-picks", "2");
  // the less certain one is hidden by the score threshold
  await page.getByTestId("picks-score").fill("0.55");
  await expect(viewer).toHaveAttribute("data-picks", "1");

  // the link reproduces the overlay
  await expect.poll(() => page.evaluate(() => window.location.hash)).toContain("pms=0.55");
  const link = await page.evaluate(() => window.location.href);
  expect(link).toContain("picks=demo%2Fpicks%2Fparticles.star");
  await page.goto("about:blank");
  await page.goto(link);
  await ready(page, "demo/picks/mic_001.mrc");
  await expect(viewer).toHaveAttribute("data-picks", "1");
  await page.getByTestId("picks-clear").click();
  await expect(viewer).not.toHaveAttribute("data-picks");
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
});

test("reduced views can be made in Fourier space", async ({ page }) => {
  const refused = trackRefused(page);
  await openFile(page, "demo/big.mrc"); // larger than the window: shown reduced
  await expect(page.getByTestId("level")).not.toHaveText("pleine résolution");
  const asked = planeWith(page, "fbin=true");
  await page.getByTestId("fourier-bin").check();
  expect(await asked).toMatch(/[?&]bin=\d+/); // the overview is asked again, reduced in Fourier space
  await ready(page, "demo/big.mrc");
  await expect.poll(() => page.evaluate(() => window.location.hash)).toContain("fb=1");
  // zooming in on a part asks for that part the same way, and the server cuts it out of the whole
  const detail = planeWith(page, "&w=");
  await page.getByTestId("viewer").hover();
  await page.mouse.wheel(0, -600);
  expect(await detail).toContain("fbin=true");
  await page.getByTestId("fourier-bin").uncheck();
  await expect.poll(() => page.evaluate(() => window.location.hash)).not.toContain("fb=1");
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
});

