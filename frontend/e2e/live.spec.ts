import { expect, type Page, test } from "@playwright/test";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { type Debug, trackRefused } from "./helpers";

const SESSION = "demo/epu_session";
const DATA = path.resolve(".e2e-data/demo/epu_session/Images-Disc1/GridSquare_1000003/Data");
const LINK = `/#live=${encodeURIComponent(SESSION)}`;

/** The exposure names of the tiles that exist in the page. */
const tileNames = (page: Page) =>
  page.getByTestId("tile").evaluateAll((tiles) => tiles.map((t) => (t as HTMLElement).dataset.name ?? ""));

/** Write an exposure into the session as a microscope would: the image, then its metadata.
 *  `cut` keeps only that share of the image file (a write that stopped in the middle);
 *  `pixels`: the image file to use instead of a copy of one of the session. */
function writeExposure(name: string, cut = 1, pixels?: string): { finish: () => void } {
  const source = fs.readdirSync(DATA).find((f) => /_\d{6}\.mrc$/.test(f))!;
  const image = fs.readFileSync(pixels ?? path.join(DATA, source));
  const xml = fs
    .readFileSync(path.join(DATA, source.replace(".mrc", ".xml")), "utf8")
    .replace(/<acquisitionDateTime>[^<]*</, `<acquisitionDateTime>${new Date().toISOString()}<`);
  fs.writeFileSync(path.join(DATA, `${name}.mrc`), image.subarray(0, Math.floor(image.length * cut)));
  return {
    finish: () => {
      fs.writeFileSync(path.join(DATA, `${name}.mrc`), image);
      fs.writeFileSync(path.join(DATA, `${name}.xml`), xml);
    },
  };
}

test.beforeEach(async ({ page }) => {
  page.on("dialog", () => {
    throw new Error("unexpected dialog");
  });
});

test("a session is shown at once, and only the tiles in view exist", async ({ page }) => {
  const refused = trackRefused(page);
  const violations: string[] = [];
  page.on("console", (m) => m.text().includes("Content Security Policy") && violations.push(m.text()));
  await page.goto(LINK);
  await expect(page.getByTestId("live-session")).toHaveText(SESSION);
  await expect(page.getByTestId("stat-acquired")).toHaveText("120");
  await expect(page.getByTestId("stat-incoming")).toHaveText("toutes complètes");
  await expect(page.getByTestId("stat-rate")).toHaveText("0"); // found, not arrived: the session is two days old
  await expect(page.getByTestId("stat-status")).toHaveText("en cours");
  await expect(page.getByTestId("grid-count")).toHaveText("120 images");
  await expect(page.getByTestId("grid")).toHaveAttribute("data-count", "120");
  await expect(page.getByTestId("stat-last")).toContainText("il y a 47 h"); // it ended just under two days ago

  // 120 exposures, far fewer tiles in the page, laid out in rows and columns
  const names = await tileNames(page);
  expect(names.length).toBeGreaterThan(8);
  expect(names.length).toBeLessThan(60);
  const boxes = await page.getByTestId("tile").evaluateAll((tiles) => tiles.slice(0, 12).map((t) => t.getBoundingClientRect()).map((r) => [r.x, r.y, r.width]));
  expect(new Set(boxes.map((b) => b[0])).size).toBeGreaterThan(1);
  expect(new Set(boxes.map((b) => b[1])).size).toBeGreaterThan(1);
  expect(boxes.every((b) => b[2] === 180)).toBe(true);
  expect(violations).toEqual([]); // the positions are applied, not refused by the page's policy

  // the most recent first; thumbnails are real pictures
  expect(names[0]).toMatch(/_119_\d{8}_\d{6}$/);
  await expect
    .poll(() => page.getByTestId("tile").first().locator("img").evaluate((img) => (img as HTMLImageElement).naturalWidth))
    .toBe(64);

  // scrolling to the end brings the first exposure of the session, and drops the last
  await page.getByTestId("grid").evaluate((el) => el.scrollTo(0, el.scrollHeight));
  await expect.poll(async () => (await tileNames(page)).some((n) => /_Data_30000000_0_/.test(n))).toBe(true);
  expect((await tileNames(page)).some((n) => /_119_\d{8}_\d{6}$/.test(n))).toBe(false);
  await page.getByTestId("order").click();
  await page.getByTestId("grid").evaluate((el) => el.scrollTo(0, 0));
  await expect.poll(async () => (await tileNames(page))[0]).toMatch(/_Data_30000000_0_/);
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
});

test("an image that arrives is shown as incomplete until it is complete", async ({ page }) => {
  const refused = trackRefused(page);
  await page.goto(LINK);
  await expect(page.getByTestId("stat-acquired")).toHaveText("120");
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "_").slice(0, 15);
  const name = `FoilHole_29999999_Data_39999999_999_${stamp}`;
  const tile = page.locator(`[data-testid="tile"][data-name="${name}"]`);

  // half of the file, then nothing for a while: longer than the two seconds the scanner waits
  const exposure = writeExposure(name, 0.5);
  await expect(tile).toHaveAttribute("data-state", "incoming");
  await expect(tile).toContainText("truncated or still being written");
  await expect(page.getByTestId("stat-incoming")).toHaveText("1 en cours d'écriture");
  await page.waitForTimeout(5000);
  await expect(tile).toHaveAttribute("data-state", "incoming"); // still not called complete
  await expect(page.getByTestId("stat-acquired")).toHaveText("120");
  await expect(tile.locator("img")).toHaveCount(0); // and no picture of half a file

  exposure.finish();
  await expect(tile).toHaveAttribute("data-state", "complete", { timeout: 20_000 });
  await expect(page.getByTestId("stat-acquired")).toHaveText("121");
  await expect(page.getByTestId("stat-rate")).toHaveText("12"); // one arrival in five minutes
  await expect(page.getByTestId("stat-last")).toContainText(/il y a \d+ s/);
  await expect.poll(() => tile.locator("img").evaluate((img) => (img as HTMLImageElement).naturalWidth)).toBe(64);
  expect((await tileNames(page))[0]).toBe(name); // the newest: first
  await expect(page.getByTestId("error")).toHaveCount(0);
  expect(refused).toEqual([]);
});

test("a tile opens in the viewer, and Live brings the session back", async ({ page }) => {
  await page.goto(LINK);
  await expect(page.getByTestId("grid-count")).toContainText("images");
  const tile = page.getByTestId("tile").nth(3);
  const name = (await tile.getAttribute("data-name"))!;
  await tile.click();
  await page.waitForFunction((expected) => {
    const d = (window as unknown as { __imagebio?: Debug }).__imagebio;
    return !!d && d.path().endsWith(`${expected}.mrc`) && d.levels().base !== null;
  }, name);
  expect(page.url()).toContain("#path=demo%2Fepu_session%2FImages-Disc1");
  await expect(page.getByTestId("pixel-size")).toContainText("0.83 Å/px");
  await page.getByTestId("mode-live").click();
  await expect(page.getByTestId("live-session")).toHaveText(SESSION);
  await expect(page.getByTestId("grid-count")).toContainText("images");
  expect(page.url()).toContain("#live=demo%2Fepu_session");
  await page.getByTestId("mode-viewer").click(); // and the viewer is where it was left
  await page.waitForFunction((expected) => (window as unknown as { __imagebio?: Debug }).__imagebio?.path().endsWith(`${expected}.mrc`), name);
});

test("a session is chosen in the browser, listed, and stopped", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("mode-live").click();
  await expect(page.getByTestId("live-empty")).toContainText("Choisissez un dossier");
  await expect(page.getByTestId("watch-here")).toBeEnabled(); // the only root is listed already
  await page.getByTestId("browser").getByText("session", { exact: true }).click();
  await page.getByTestId("watch-here").click();
  await expect(page.getByTestId("live-session")).toHaveText("demo/session");
  await expect(page.getByTestId("stat-acquired")).toHaveText("1"); // inside.mrc
  const listed = page.getByTestId("watches").getByRole("button", { name: "demo/session", exact: true });
  await expect(listed).toBeVisible();
  await page.getByRole("button", { name: "Arrêter demo/session" }).click();
  await expect(page.getByTestId("stat-status")).toHaveText("arrêtée");
  await expect(page.getByRole("button", { name: "Arrêter demo/session" })).toHaveCount(0);
  await expect(listed).toBeVisible(); // still there to be read, or watched again

  // forgetting it takes it off the list, and leaves the session
  await page.getByRole("button", { name: "Oublier demo/session" }).click();
  await expect(listed).toHaveCount(0);
  await expect(page.getByTestId("live-session")).toHaveText("aucune session");
  await expect(page.getByTestId("live-empty")).toBeVisible();

  // a directory one may not read, or that is not there
  await page.goto("/#live=demo%2Fnowhere");
  await expect(page.getByTestId("error")).toContainText("not found");
  await expect(page.getByTestId("live-empty")).toBeVisible();
});

test("alerts are set per session, kept, and can be tried", async ({ page }) => {
  // the place the test server sends its alerts to (see make_fixtures.py)
  const received: { url: string; body: Record<string, unknown> }[] = [];
  const receiver = http.createServer((request, response) => {
    let text = "";
    request.on("data", (chunk) => (text += chunk));
    request.on("end", () => {
      received.push({ url: request.url ?? "", body: JSON.parse(text) });
      response.writeHead(200).end();
    });
  });
  await new Promise<void>((resolve) => receiver.listen(8766, "127.0.0.1", resolve));
  try {
    const refused = trackRefused(page);
    await page.goto(LINK);
    const panel = page.getByTestId("alerts");
    await expect(panel).toContainText("essai (webhook)");
    await expect(page.getByTestId("alerts-test")).toBeDisabled(); // nothing saved yet: nowhere to send
    await page.getByTestId("alert-channel-essai").check();
    await page.getByTestId("alert-no-image").fill("0"); // under a minute: not a threshold the server takes
    await expect(page.getByTestId("alerts-save")).toBeDisabled();
    await page.getByTestId("alert-no-image").fill("30");
    await page.getByTestId("alert-disk").fill("2,5");
    await page.getByTestId("alerts-save").click();
    await expect(page.getByTestId("alerts-saved")).toBeVisible();

    await page.getByTestId("alerts-test").click();
    await expect(page.getByTestId("alerts-test-results")).toHaveText("essai : envoyé");
    expect(received).toHaveLength(1);
    expect(received[0]!.url).toBe("/hook");
    expect(received[0]!.body).toMatchObject({ source: "imagebio", condition: "test", title: "imageBio : essai des alertes", session: SESSION });

    await page.reload(); // the rules belong to the watch, on the server
    await expect(page.getByTestId("alert-channel-essai")).toBeChecked();
    await expect(page.getByTestId("alert-no-image")).toHaveValue("30");
    await expect(page.getByTestId("alert-disk")).toHaveValue("2.5");
    await expect(page.getByTestId("alert-rate")).toHaveValue("");
    await expect(page.getByTestId("alerts-firing")).toHaveCount(0); // half an hour is not up

    await page.getByTestId("alert-channel-essai").uncheck();
    await page.getByTestId("alerts-save").click();
    await expect(page.getByTestId("alerts-saved")).toBeVisible();
    await expect(page.getByTestId("alerts-test")).toBeDisabled();
    await expect(page.getByTestId("error")).toHaveCount(0);
    expect(refused).toEqual([]);
  } finally {
    await new Promise((resolve) => receiver.close(resolve));
  }
});

test("images are measured: ice against the session, and a mark on crystalline ice", async ({ page }) => {
  await page.goto(LINK);
  // the images of the session are measured a few at each pass of the scanner
  await expect(page.getByTestId("stat-ice")).toHaveText(/^-?\d\.\d\d$/, { timeout: 60_000 });
  await expect(page.getByTestId("stat-ice-sub")).toContainText(/sur \d+ images · anneau 0 %/);
  const newest = page.getByTestId("tile").first();
  await expect(newest).toHaveAttribute("title", /Coups moyens : \d+/);
  await expect(newest).toHaveAttribute("title", /Glace relative : -?\d\.\d\d/);
  await expect(newest).not.toHaveAttribute("title", /Anneau de glace/); // 64 pixels: too small an image to tell
  await expect(newest).toContainText(/glace -?\d\.\d\d/);
  await expect(page.locator('[data-testid="tile"][data-flags~="ice_ring"]')).toHaveCount(0);

  // an image of crystalline ice arrives: it is marked as soon as it is measured
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "_").slice(0, 15);
  const name = `FoilHole_29999998_Data_39999998_998_${stamp}`;
  writeExposure(name, 1, path.resolve(".e2e-data/spare/icy.mrc")).finish();
  const tile = page.locator(`[data-testid="tile"][data-name="${name}"]`);
  await expect(tile).toHaveAttribute("data-state", "complete", { timeout: 20_000 });
  await expect(tile).toHaveAttribute("data-flags", "ice_ring", { timeout: 20_000 });
  await expect(tile).toContainText("glace cristalline");
  await expect(tile).toHaveAttribute("title", /Anneau de glace : ([2-9]|\d\d)\.\d\d/); // far above 1
  await expect(page.getByTestId("stat-ice-sub")).toContainText(/ · anneau [1-9]\d* %/);
  await expect(page.getByTestId("error")).toHaveCount(0);
});
