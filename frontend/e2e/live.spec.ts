import { expect, type Page, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { type Debug, trackRefused } from "./helpers";

const SESSION = "demo/epu_session";
const DATA = path.resolve(".e2e-data/demo/epu_session/Images-Disc1/GridSquare_1000003/Data");
const LINK = `/#live=${encodeURIComponent(SESSION)}`;

/** The exposure names of the tiles that exist in the page. */
const tileNames = (page: Page) =>
  page.getByTestId("tile").evaluateAll((tiles) => tiles.map((t) => (t as HTMLElement).dataset.name ?? ""));

/** Write an exposure into the session as a microscope would: the image, then its metadata.
 *  `cut` keeps only that share of the image file (a write that stopped in the middle). */
function writeExposure(name: string, cut = 1): { finish: () => void } {
  const source = fs.readdirSync(DATA).find((f) => /_\d{6}\.mrc$/.test(f))!;
  const image = fs.readFileSync(path.join(DATA, source));
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

  // a directory one may not read, or that is not there
  await page.goto("/#live=demo%2Fnowhere");
  await expect(page.getByTestId("error")).toContainText("not found");
  await expect(page.getByTestId("live-empty")).toBeVisible();
});
