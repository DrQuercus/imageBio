import { expect, test } from "@playwright/test";
import { colourAt, type Debug, hoverPixel, openFile, ready, screenOf, trackPlanes } from "./helpers";

test.beforeEach(async ({ page }) => {
  page.on("dialog", () => {
    throw new Error("unexpected dialog: a file name was executed as HTML");
  });
});

test("the value under the cursor is the value stored in the file", async ({ page }) => {
  await openFile(page, "demo/gradient.mrc"); // value = 100 y + x
  for (const [x, y] of [[0, 0], [63, 47], [17, 5], [40, 31], [1, 46]] as const) {
    await hoverPixel(page, x, y);
    await expect(page.getByTestId("hover-xy")).toHaveText(`x ${x}  y ${y}`);
    await expect(page.getByTestId("hover-value")).toHaveText(String(100 * y + x));
  }
  await expect(page.getByTestId("level")).toHaveText("pleine résolution");
  await expect(page.getByTestId("pixel-size")).toContainText("1.5 Å/px (en-tête du fichier)");
});

test("contrast, gamma, inversion and palette cost no request", async ({ page }) => {
  const planes = trackPlanes(page);
  let stats = 0;
  page.on("request", (r) => r.url().includes("/api/datasets/stats") && stats++);
  await openFile(page, "demo/movie.mrc");
  const before = { planes: planes.total, stats };
  expect(before.planes).toBeGreaterThan(0);
  const colours = [await colourAt(page, 50, 50)];
  await page.getByTestId("min-slider").fill("50"); // a window around the noise: mid tones on screen
  await page.getByTestId("max-slider").fill("400");
  colours.push(await colourAt(page, 50, 50));
  await page.getByTestId("gamma-slider").fill("250");
  await page.getByTestId("invert").click();
  colours.push(await colourAt(page, 50, 50));
  await page.getByTestId("lut").selectOption("fire");
  await page.waitForTimeout(300);
  colours.push(await colourAt(page, 50, 50));
  expect(new Set(colours.map(String)).size).toBeGreaterThan(2); // the image really changed
  const fire = colours[3]!;
  expect(fire[0] !== fire[1] || fire[1] !== fire[2]).toBe(true); // no longer grey
  expect({ planes: planes.total, stats }).toEqual(before); // and nothing was fetched
});

test("the player never has more than one plane request in flight", async ({ page }) => {
  await openFile(page, "demo/movie.mrc");
  const planes = trackPlanes(page);
  await page.getByTestId("play").click();
  await page.waitForTimeout(3000);
  await page.getByTestId("play").click();
  await page.waitForTimeout(300);
  expect(planes.total).toBeGreaterThanOrEqual(8); // it did play
  expect(planes.maxInFlight).toBeLessThanOrEqual(1);
  await expect(page.getByTestId("t-label")).not.toHaveText("Frame 1 / 30");
  // holding the arrow key does not pile requests up either
  planes.maxInFlight = 0;
  for (let i = 0; i < 25; i++) await page.keyboard.press("ArrowRight");
  await page.waitForTimeout(500);
  expect(planes.inFlight).toBe(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("images with NaN, Inf and a hot pixel are displayed", async ({ page }) => {
  await openFile(page, "demo/nan_hot.mrc"); // ramp 50 + 2 x, NaN block at 10..30, hot pixel 1e9
  await expect(page.getByTestId("error")).toHaveCount(0);
  const left = await colourAt(page, 40, 100);
  const right = await colourAt(page, 120, 100);
  expect(left[0]).toBeLessThan(right[0]!); // the ramp is visible: the hot pixel did not flatten it
  expect(left[0]).toBe(left[1]);
  const nan = await colourAt(page, 20, 20);
  expect(nan[0]).not.toBe(nan[1]); // NaN has its own colour, it is not a grey level
  await expect(page.getByTestId("info")).toContainText("400 NaN, 2 Inf");
  expect(Number(await page.getByTestId("max-input").inputValue())).toBeLessThan(1000);
  await hoverPixel(page, 60, 60);
  await expect(page.getByTestId("hover-value")).toHaveText("1.000e+9");
  await hoverPixel(page, 20, 20);
  await expect(page.getByTestId("hover-value")).toHaveText("NaN");
});

test("a copied link reproduces the same view", async ({ page, context }) => {
  await openFile(page, "demo/movie.mrc");
  await page.getByTestId("t-slider").fill("7");
  const [sx, sy] = await screenOf(page, 100, 120);
  await page.mouse.move(sx, sy);
  await page.mouse.wheel(0, -600);
  await page.getByTestId("lut").selectOption("ice");
  await page.getByTestId("min-input").fill("120");
  await page.getByTestId("max-input").fill("210");
  await page.getByTestId("gamma-slider").fill("140");
  await page.waitForTimeout(700);
  const url = page.url();
  for (const part of ["path=demo%2Fmovie.mrc", "t=7", "zoom=", "min=120", "max=210", "gamma=1.4", "lut=ice"]) {
    expect(url).toContain(part);
  }
  const points = [[100, 120], [110, 115], [90, 130], [118, 108]] as const;
  const original = [];
  for (const [x, y] of points) original.push(await colourAt(page, x, y));

  const other = await context.newPage();
  await other.goto(url);
  await ready(other, "demo/movie.mrc");
  await other.waitForTimeout(500);
  await expect(other.getByTestId("t-label")).toHaveText("Frame 8 / 30");
  await expect(other.getByTestId("lut")).toHaveValue("ice");
  await expect(other.getByTestId("min-input")).toHaveValue("120");
  await expect(other.getByTestId("zoom")).toHaveText(await page.getByTestId("zoom").innerText());
  for (const [i, [x, y]] of points.entries()) expect(await colourAt(other, x, y)).toEqual(original[i]);
});

test("a hostile file name is shown as text and opens like any file", async ({ page }) => {
  await page.goto("/");
  const name = "<img src=x onerror=alert(1)>.mrc";
  const row = page.getByTestId("browser").getByRole("button", { name });
  await expect(row).toBeVisible();
  expect(await page.locator("img").count()).toBe(0);
  await row.click();
  await ready(page, `demo/${name}`);
  await expect(page.getByTestId("info")).toContainText(name);
  expect(await page.locator("img").count()).toBe(0);
});

test("a large image is shown binned, then at full resolution where one zooms", async ({ page }) => {
  const planes = trackPlanes(page);
  await openFile(page, "demo/big.mrc"); // 4096 x 4096, value = x + 2 y
  await expect(page.getByTestId("level")).toHaveText("binning 4×");
  await hoverPixel(page, 1000, 2000);
  await expect(page.getByTestId("hover-value")).toContainText("(moyenne 4×4)");
  expect(planes.urls.every((u) => u.includes("bin=4"))).toBe(true); // the whole image never travels at full size

  await page.getByTestId("one-to-one").click();
  await page.waitForFunction(() => (window as unknown as { __imagebio: Debug }).__imagebio.levels().detail === 1);
  await expect(page.getByTestId("level")).toHaveText("pleine résolution");
  const detail = planes.urls.at(-1)!;
  expect(detail).toMatch(/bin=1&x=\d+&y=\d+&w=\d+&h=\d+/);
  const width = Number(new URL(detail).searchParams.get("w"));
  expect(width).toBeLessThan(2200); // only the visible region, with a margin
  for (const [x, y] of [[2048, 2048], [2100, 1990], [1801, 2203]] as const) {
    await hoverPixel(page, x, y);
    await expect(page.getByTestId("hover-value")).toHaveText(String(x + 2 * y));
  }
});

test("ruler and pixel size, known or entered by hand", async ({ page }) => {
  await openFile(page, "demo/gradient.mrc"); // 1.5 Å per pixel
  await page.getByTestId("tool-ruler").click();
  const [x0, y0] = await screenOf(page, 10, 10);
  const [x1, y1] = await screenOf(page, 40, 10);
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  await page.mouse.move(x1, y1, { steps: 4 });
  await page.mouse.up();
  await expect(page.getByTestId("ruler-length")).toHaveText("45.0 Å  (30.0 px)");

  await openFile(page, "demo/noapix.tif");
  await expect(page.getByTestId("pixel-size")).toHaveText("taille inconnue");
  await expect(page.getByTestId("warnings")).toBeVisible();
  await expect(page.getByTestId("ruler-length")).toHaveCount(0); // a new file starts without the old ruler
  await expect(page.getByTestId("tool-ruler")).toHaveClass(/on/); // the tool stays selected
  const [a0, b0] = await screenOf(page, 4, 4);
  const [a1, b1] = await screenOf(page, 4, 24);
  await page.mouse.move(a0, b0);
  await page.mouse.down();
  await page.mouse.move(a1, b1, { steps: 4 });
  await page.mouse.up();
  await expect(page.getByTestId("ruler-length")).toHaveText("20.0 px"); // no invented unit
  await page.getByTestId("user-apix").fill("2");
  await expect(page.getByTestId("ruler-length")).toHaveText("40.0 Å  (20.0 px)");
});

test("colour images and hyperstacks", async ({ page }) => {
  await openFile(page, "demo/colours.png"); // red left half, green right half
  const red = await colourAt(page, 10, 30);
  const green = await colourAt(page, 50, 30);
  expect(red[0]).toBeGreaterThan(200);
  expect(red[1]).toBeLessThan(40);
  expect(green[1]).toBeGreaterThan(150);
  expect(green[0]).toBeLessThan(40);
  await hoverPixel(page, 10, 30);
  await expect(page.getByTestId("hover-value")).toHaveText("255, 0, 0");

  await openFile(page, "demo/hyper.tif"); // value = 1000 t + 100 z + 10 c
  await page.getByTestId("t-slider").fill("1");
  await page.getByTestId("z-slider").fill("2");
  await page.getByTestId("c-slider").fill("1");
  await page.waitForTimeout(400);
  await hoverPixel(page, 8, 8);
  await expect(page.getByTestId("hover-value")).toHaveText("1210");
});

test("browsing directories and keyboard shortcuts", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("browser").getByRole("button", { name: "session" }).click();
  await page.getByTestId("browser").getByRole("button", { name: "inside.mrc" }).click();
  await ready(page, "demo/session/inside.mrc");
  await expect(page.getByTestId("zoom")).toHaveText("6400 %"); // an 8 x 8 image: the maximum zoom
  await hoverPixel(page, 3, 3);
  await expect(page.getByTestId("hover-value")).toHaveText("7");
  await page.getByTestId("browser").getByRole("button", { name: ".." }).click();
  await expect(page.getByTestId("browser").getByRole("button", { name: "notes.txt" })).toBeDisabled();

  await page.getByTestId("browser").getByRole("button", { name: "movie.mrc" }).click();
  await ready(page, "demo/movie.mrc");
  await expect(page.getByTestId("zoom")).not.toHaveText("6400 %"); // refitted, not the zoom of the previous file
  await page.getByTestId("viewer").hover();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId("t-label")).toHaveText("Frame 3 / 30");
  const zoom = await page.getByTestId("zoom").innerText();
  await page.keyboard.press("+");
  await expect(page.getByTestId("zoom")).not.toHaveText(zoom);
  await page.keyboard.press("f");
  await expect(page.getByTestId("zoom")).toHaveText(zoom);
  await page.keyboard.press("i");
  await expect(page.getByTestId("invert")).toHaveClass(/on/);
});
