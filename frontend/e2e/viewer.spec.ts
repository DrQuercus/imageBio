import { expect, test } from "@playwright/test";
import { colourAt, type Debug, hoverPixel, openFile, ready, screenOf, trackPlanes, trackRefused } from "./helpers";

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

test("a link that arrives while the page is about to rewrite its address is not lost", async ({ page }) => {
  // Regression. The page writes its view into the address 200 ms after any change. A link that
  // the browser delivered in between (pasted in the address bar, Back, or opened by a test)
  // while the page was busy was overwritten by that pending write before the page had read
  // it: the image on screen stayed, and the link was gone.
  await openFile(page, "demo/gradient.mrc");
  await page.getByTestId("invert").click(); // a change of view: the address will be rewritten
  await expect(page.getByTestId("invert")).toHaveClass(/on/);
  await page.waitForTimeout(50);
  // the page is busy (as it is while it decodes a large plane) until that write is overdue ...
  const busy = page.evaluate(() => {
    const until = performance.now() + 400;
    while (performance.now() < until);
  });
  await page.waitForTimeout(20);
  // ... and the new address arrives meanwhile
  await page.goto(`/#path=${encodeURIComponent("demo/hyper.tif")}&t=1&z=2`);
  await busy;
  await ready(page, "demo/hyper.tif");
  await expect(page.getByTestId("fiji-plane")).toHaveAttribute("href", /[?&]t=1&c=0&z=2/);
  await page.waitForTimeout(400); // the address the page writes next is the one of the new image
  expect(page.url()).toContain("path=demo%2Fhyper.tif");
  expect(page.url()).toContain("t=1");
  // and the address is still kept up to date with what the user does afterwards
  await page.getByTestId("invert").click();
  await expect.poll(() => page.url()).toContain("inv=1");
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
  await page.getByTestId("browser").getByText("session", { exact: true }).click(); // not "epu_session"
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

test("the planes of a stack side by side, and the classes of RELION with their shares", async ({ page }) => {
  const refused = trackRefused(page);
  const path = "demo/Class2D/run_it010_classes.mrcs";
  await openFile(page, path);
  await expect(page.getByTestId("gallery-toggle")).toBeVisible();
  await page.getByTestId("gallery-toggle").click();
  const tiles = page.getByTestId("gallery-tile");
  await expect(tiles).toHaveCount(8);
  // the model file next to the averages is found: the most populated class comes first
  await expect(tiles.first()).toHaveAttribute("data-plane", "5");
  await expect(tiles.first()).toContainText("6 · 40 % · 6.4 Å");
  await expect(tiles.nth(1)).toContainText("7 · 20 % · 6.9 Å");
  await expect(tiles.last()).toContainText("3 · 0.0 %"); // an empty class: no resolution
  await expect.poll(() => tiles.first().locator("img").evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBe(64);
  await expect.poll(() => page.evaluate(() => window.location.hash)).toContain("gal=1");

  await page.getByTestId("gallery-sort").click(); // by number
  await expect(tiles.first()).toHaveAttribute("data-plane", "0");
  await expect(page.getByTestId("gallery-sort")).toHaveText("Par numéro");
  // larger tiles: fewer of them in a row, and larger pictures are not needed for 64-pixel averages
  const before = await tiles.nth(1).boundingBox();
  await page.getByTestId("gallery-size").fill("256");
  const after = await tiles.nth(1).boundingBox();
  expect(after!.width).toBe(256);
  expect(after!.x).toBeGreaterThan(before!.x);

  // a click shows that plane in the viewer: plane 4 holds the value 30 away from its disc
  await tiles.nth(3).click();
  await ready(page, path);
  await expect(page.getByTestId("t-label")).toHaveText("Frame 4 / 8");
  await hoverPixel(page, 2, 2);
  await expect(page.getByTestId("hover-value")).toHaveText("30");
  // back in the gallery (G), the plane on screen is outlined
  await page.keyboard.press("g");
  await expect(page.locator('[data-testid="gallery-tile"].current')).toHaveAttribute("data-plane", "3");

  // a file with one plane has no gallery; a movie has one, without classes
  await openFile(page, "demo/gradient.mrc");
  await expect(page.getByTestId("gallery-toggle")).toHaveCount(0);
  await page.goto(`/#path=${encodeURIComponent("demo/movie.mrc")}&gal=1`); // a link to a gallery
  await expect(page.getByTestId("gallery")).toBeVisible();
  await expect(page.getByTestId("gallery")).toHaveAttribute("data-count", "30");
  await expect(page.getByTestId("gallery-sort")).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
});

test("the image on screen is handed to Fiji as a calibrated TIFF", async ({ page }) => {
  await openFile(page, "demo/gradient.mrc");
  await expect(page.getByTestId("fiji-stack")).toHaveCount(0); // one plane: nothing more to offer
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByTestId("fiji-plane").click()]);
  expect(download.suggestedFilename()).toBe("gradient.tif");
  const href = await page.getByTestId("fiji-plane").getAttribute("href");
  const response = await page.request.get(href!);
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toBe("image/tiff");
  const body = await response.body();
  expect(body.subarray(0, 4).toString("latin1")).toBe("II*\u0000"); // a little-endian TIFF
  expect(body.toString("latin1")).toContain("unit=nm"); // 1.5 Å per pixel, as ImageJ wants it

  // a stack: the plane on screen, named after it, or the whole file
  await openFile(page, "demo/hyper.tif", "&t=1&z=2");
  await expect(page.getByTestId("fiji-plane")).toHaveAttribute("href", /[?&]t=1&c=0&z=2/);
  const [plane] = await Promise.all([page.waitForEvent("download"), page.getByTestId("fiji-plane").click()]);
  expect(plane.suggestedFilename()).toBe("hyper_t2_z3_c1.tif");
  const [stack] = await Promise.all([page.waitForEvent("download"), page.getByTestId("fiji-stack").click()]);
  expect(stack.suggestedFilename()).toBe("hyper.tif");
  await expect(page.getByTestId("error")).toHaveCount(0);
});


test("the next and previous images of the folder keep the settings", async ({ page }) => {
  const refused = trackRefused(page);
  await openFile(page, "demo/picks/mic_001.mrc"); // 256 x 256 at 1.2 Å per pixel, two in the folder
  await expect(page.getByTestId("file-position")).toHaveText("1 / 2");
  await expect(page.getByTestId("file-prev")).toBeDisabled();
  await page.getByTestId("lp-20").click();
  await page.getByTestId("user-apix").fill("2");
  await page.getByTestId("user-apix").press("Enter");
  await page.getByTestId("one-to-one").click();
  await expect(page.getByTestId("zoom")).toHaveText("100 %");

  await page.getByTestId("viewer").hover();
  await page.keyboard.press("n");
  await ready(page, "demo/picks/mic_002.mrc");
  await expect(page.getByTestId("file-position")).toHaveText("2 / 2");
  // filter, typed pixel size and view: as they were (the two images have the same size)
  await expect(page.getByTestId("lp-20")).toHaveClass(/on/);
  await expect(page.getByTestId("user-apix")).toHaveValue("2");
  await expect(page.getByTestId("zoom")).toHaveText("100 %");
  await expect.poll(() => page.url()).toContain("apix=2");
  // the last image: N goes nowhere
  await page.keyboard.press("n");
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => (window as unknown as { __imagebio: { path(): string } }).__imagebio.path())).toBe("demo/picks/mic_002.mrc");
  await page.keyboard.press("PageUp");
  await ready(page, "demo/picks/mic_001.mrc");
  await expect(page.getByTestId("file-position")).toHaveText("1 / 2");
  await page.getByTestId("file-next").click();
  await ready(page, "demo/picks/mic_002.mrc");
  // a file opened from the list starts afresh: its own pixel size, fitted to the window
  await page.getByTestId("browser").getByRole("button", { name: "mic_001.mrc" }).click();
  await ready(page, "demo/picks/mic_001.mrc");
  await expect(page.getByTestId("user-apix")).toHaveValue("1.2");
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
});

test("a circle of a diameter in Å follows the cursor", async ({ page }) => {
  await openFile(page, "demo/lattice.mrc"); // 1.25 Å per pixel
  await page.getByTestId("one-to-one").click();
  await hoverPixel(page, 512, 512);
  await page.keyboard.press("c");
  await expect(page.getByTestId("circle-toggle")).toHaveClass(/on/);
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-circle", "120"); // 150 Å
  const [sx, sy] = await screenOf(page, 512, 512);
  const box = (await page.getByTestId("viewer").boundingBox())!;
  /** Opacity of the overlay at a point of the page. */
  const overlayAlpha = (x: number, y: number) =>
    page.getByTestId("viewer-overlay").evaluate((canvas, [px, py]) => {
      const c = canvas as HTMLCanvasElement;
      const dpr = c.width / c.getBoundingClientRect().width;
      return c.getContext("2d")!.getImageData(Math.round(px! * dpr), Math.round(py! * dpr), 1, 1).data[3]!;
    }, [x - box.x, y - box.y]);
  // at 1:1, the circle passes 60 screen pixels from the centre of the cursor
  await expect.poll(() => overlayAlpha(sx - 0.5 + 60, sy - 0.5)).toBeGreaterThan(100);
  expect(await overlayAlpha(sx - 0.5 + 30, sy - 0.5)).toBe(0);
  await page.getByTestId("circle-diameter").fill("250");
  await page.getByTestId("circle-diameter").press("Enter");
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-circle", "200");
  await page.getByTestId("circle-toggle").click();
  await expect(page.getByTestId("viewer")).not.toHaveAttribute("data-circle", /.*/);
});

test("the intensity along the ruler, and its values for a spreadsheet", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openFile(page, "demo/gradient.mrc"); // value = 100 y + x, 1.5 Å per pixel
  await page.getByTestId("tool-ruler").click();
  const [x0, y0] = await screenOf(page, 4, 10);
  const [x1, y1] = await screenOf(page, 34, 10);
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  await page.mouse.move(x1, y1, { steps: 4 });
  await page.mouse.up();
  await expect(page.getByTestId("measure-value")).toHaveText("45.0 Å");
  const chart = page.getByTestId("profile-chart");
  await expect(chart).toHaveAttribute("data-samples", "31");
  await expect(chart).toHaveAttribute("data-min", "1004");
  await expect(chart).toHaveAttribute("data-max", "1034");
  await page.getByTestId("profile-copy").click();
  await expect(page.getByTestId("profile-copy")).toHaveText("Copié");
  const text = await page.evaluate(() => navigator.clipboard.readText());
  const lines = text.trim().split("\n");
  expect(lines[0]).toBe("distance_A\tvalue");
  expect(lines[1]).toBe("0\t1004");
  expect(lines.at(-1)).toBe("45\t1034");
  expect(lines).toHaveLength(32);
});
