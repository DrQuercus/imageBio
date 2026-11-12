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

  // only the marked images, then every image sorted on its ice ring: that one first
  await page.getByTestId("grid-filter").selectOption("flagged");
  await expect(page.getByTestId("tile")).toHaveCount(1);
  await expect(page.getByTestId("grid-count")).toHaveText(/^1 sur \d+ images$/);
  await page.getByTestId("grid-filter").selectOption("clean");
  await expect(page.locator(`[data-testid="tile"][data-name="${name}"]`)).toHaveCount(0);
  await page.getByTestId("grid-filter").selectOption("all");
  await page.getByTestId("grid-sort").selectOption("ring");
  await expect(page.getByTestId("order")).toHaveText("Décroissant");
  await expect(page.getByTestId("tile").first()).toHaveAttribute("data-name", name);
  await page.getByTestId("grid-sort").selectOption("time");

  // the counts of an empty hole and a mean free path: thickness in nanometres, empty holes marked
  await expect(page.getByTestId("ice-mode")).toHaveText("Glace relative aux images les plus claires de la session.");
  const reference = Number(await page.getByTestId("ice-i0").getAttribute("placeholder"));
  expect(reference).toBeGreaterThan(0);
  await page.getByTestId("ice-i0").fill(String(reference * Math.E)); // every image then has one free path of ice
  await page.getByTestId("ice-mfp").fill("400");
  await page.getByTestId("ice-save").click();
  await expect(page.getByTestId("ice-saved")).toBeVisible();
  await expect(page.getByTestId("ice-mode")).toHaveText("Épaisseur absolue : comparée au trou vide.");
  await expect(page.getByTestId("stat-ice")).toHaveText(/^(39\d|40\d) nm$/);
  await expect(page.getByTestId("tile").nth(3)).toContainText(/glace (39\d|40\d) nm/);
  await expect(page.getByTestId("tile").nth(3)).toHaveAttribute("title", /Épaisseur de glace : (39\d|40\d) nm/);
  // with a hole as bright as the images, they are empty holes
  await page.getByTestId("ice-i0").fill(String(reference));
  await page.getByTestId("ice-save").click();
  await expect(page.locator('[data-testid="tile"][data-flags~="empty"]').first()).toContainText("trou vide");
  await page.getByTestId("ice-mfp").fill("3"); // not a mean free path
  await expect(page.getByTestId("ice-save")).toBeDisabled();
  await page.getByTestId("ice-i0").fill("");
  await page.getByTestId("ice-mfp").fill("");
  await page.getByTestId("ice-save").click();
  await expect(page.getByTestId("ice-mode")).toHaveText("Glace relative aux images les plus claires de la session.");
  await expect(page.locator('[data-testid="tile"][data-flags~="empty"]')).toHaveCount(0);

  // acceptance thresholds: crystalline ice is rejected, the tile and the count say so
  await expect(page.getByTestId("stat-quality")).toHaveCount(0); // nothing is judged without a threshold
  await page.getByTestId("quality-ring").fill("1.2");
  await page.getByTestId("quality-save").click();
  await expect(page.getByTestId("quality-saved")).toBeVisible();
  await expect(tile).toHaveAttribute("data-verdict", "rejected");
  await expect(tile).toContainText("rejetée");
  await expect(tile).toHaveAttribute("title", /Rejetée : anneau de glace \d+\.\d\d \(seuil 1\.2\)/);
  await expect(page.getByTestId("stat-quality-sub")).toHaveText(/^1 rejetée sur \d+$/);
  await expect(page.getByTestId("tile").nth(3)).toHaveAttribute("data-verdict", "accepted");
  await page.getByTestId("grid-filter").selectOption("rejected");
  await expect(page.getByTestId("tile")).toHaveCount(1);
  await page.getByTestId("grid-filter").selectOption("all");
  // the map: one point per image at its stage position, the rejected one in its own colour
  await page.getByTestId("map-toggle").click();
  const count = Number(await page.getByTestId("stage-map").getAttribute("data-points"));
  expect(count).toBeGreaterThan(100);
  await expect(page.getByTestId("stage-point")).toHaveCount(count);
  await expect(page.locator('[data-testid="stage-point"][data-verdict="rejected"]')).toHaveCount(1);
  const places = await page.getByTestId("stage-point").evaluateAll((points) => points.map((p) => `${p.getAttribute("cx")},${p.getAttribute("cy")}`));
  expect(new Set(places).size).toBeGreaterThan(50); // spread over the grid, not piled up
  await page.getByTestId("map-toggle").click();
  await page.getByTestId("quality-ring").fill("0.5"); // not a ratio one can ask for
  await expect(page.getByTestId("quality-save")).toBeDisabled();
  await page.getByTestId("quality-ring").fill("");
  await page.getByTestId("quality-save").click();
  await expect(tile).toHaveAttribute("data-verdict", "");

  // the overview images EPU takes on the way are another view of the same session
  const squares = page.waitForResponse((r) => r.url().includes("/items?") && r.url().includes("category=gridsquare") && r.ok());
  await page.getByTestId("grid-category").selectOption("gridsquare");
  await squares;
  await expect(page.getByTestId("tile")).toHaveCount(4); // one overview per grid square of the session
  await expect(page.getByTestId("tile").first()).toHaveAttribute("data-name", /^GridSquare_/);
  await expect(page.getByTestId("grid-count")).toHaveText("4 images");
  await page.getByTestId("grid-category").selectOption("data");
  await expect(page.getByTestId("tile").first()).toBeVisible();
  await expect(page.getByTestId("error")).toHaveCount(0);
});

test("the programs of the server run on what arrives, once the session asks for them", async ({ page }) => {
  await page.goto(LINK);
  // the test server declares one program, which stands for CTFFIND (see make_fixtures.py)
  await expect(page.getByTestId("processing")).toContainText("ajustement du CTF");
  const toggle = page.getByTestId("processing-on");
  await expect(toggle).not.toBeChecked(); // nothing runs unless asked for
  await toggle.check();
  await expect(toggle).toBeChecked();

  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "_").slice(0, 15);
  const name = `FoilHole_29999997_Data_39999997_997_${stamp}`;
  writeExposure(name).finish();
  const tile = page.locator(`[data-testid="tile"][data-name="${name}"]`);
  await expect(tile).toHaveAttribute("data-state", "complete", { timeout: 20_000 });
  // the fit of the program: both defocus values, the astigmatism, how far the rings were fitted
  // (the stand-in answers 0.04 µm of astigmatism and four pixels: 3.3 Å at 0.83 Å per pixel)
  await expect(tile).toHaveAttribute("title", /CTF ajusté : défocus \d+\.\d\d et \d+\.\d\d µm, astigmatisme 0\.040 µm, ajusté jusqu'à 3\.3 Å/, {
    timeout: 30_000,
  });
  await expect(page.getByTestId("processing-count")).toHaveText(/^\d+ images? traitées?/);

  // the choice belongs to the session and is kept by the server
  await page.reload();
  await expect(page.getByTestId("processing-on")).toBeChecked();
  await page.getByTestId("processing-on").uncheck();
  await expect(page.getByTestId("processing-on")).not.toBeChecked();
  await page.reload();
  await expect(page.getByTestId("processing-on")).not.toBeChecked();
});

test("the quality of the session in charts, and its report to keep", async ({ page }) => {
  await page.goto(LINK);
  await expect(page.getByTestId("stat-ice")).toHaveText(/^-?\d\.\d\d$/, { timeout: 60_000 }); // measured

  // charts of the images shown: the images of this session are too small for a CTF, not for the ice
  await page.getByTestId("stats-toggle").click();
  await expect(page.getByTestId("stats-view")).toBeVisible();
  await expect(page.getByTestId("chart-verdicts")).toBeVisible();
  await expect(page.getByTestId("chart-ice")).toHaveAttribute("data-points", /^\d{2,}$/);
  await expect(page.getByTestId("hist-ice")).toBeVisible();
  // (the defocus is drawn only for images with a CTF: here those an earlier test had a program fit)
  await expect(page.getByTestId("squares")).toContainText("GridSquare_1000003");
  await page.getByTestId("stats-toggle").click();
  await expect(page.getByTestId("stats-view")).toHaveCount(0);

  // a report, now
  const rows = page.getByTestId("report-row");
  const before = await rows.count();
  await page.getByTestId("report-now").click();
  await expect(rows).toHaveCount(before + 1, { timeout: 30_000 });
  await expect(rows.first()).toContainText("demandé");
  const href = await rows.first().getByTestId("report-view").getAttribute("href");
  expect(href).toMatch(/\/reports\/\d+\/html\?view=true$/);
  const shown = await page.request.get(href!);
  expect(shown.status()).toBe(200);
  expect(shown.headers()["content-security-policy"]).toMatch(/^sandbox; default-src 'none'/);
  const text = await shown.text();
  expect(text).toContain("Rapport de qualité de la collecte");
  expect(text).toContain("demo/epu_session");
  expect(text).not.toMatch(/<script/i);
  const [zip] = await Promise.all([page.waitForEvent("download"), rows.first().getByTestId("report-zip").click()]);
  expect(zip.suggestedFilename()).toMatch(/^rapport_epu_session_\d{4}-\d\d-\d\d_\d{4}\.zip$/);
  // the atlas of the session and the overview of each of its four squares are in the page
  await expect(page.getByTestId("report-atlas")).toContainText("Atlas_1.mrc (trouvé dans la session)");
  expect(text).toContain('alt="Atlas de la grille"');
  expect(text.match(/alt="Vue du GridSquare_\d+"/g)).toHaveLength(4);

  // another atlas, chosen in the file browser (EPU usually keeps it in a session of its own)
  await page.getByTestId("atlas-choose").click();
  const dialog = page.getByTestId("atlas-dialog");
  await dialog.getByRole("button", { name: "gradient.mrc" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("report-atlas")).toContainText("gradient.mrc (choisi)");
  await page.reload();
  await expect(page.getByTestId("report-atlas")).toContainText("gradient.mrc (choisi)"); // kept by the server
  await page.waitForTimeout(10_500); // a report asked for again within ten seconds is the one just made
  await page.getByTestId("report-now").click();
  await expect(rows).toHaveCount(before + 2, { timeout: 30_000 });
  const chosen = await (await page.request.get((await rows.first().getByTestId("report-view").getAttribute("href"))!)).text();
  expect(chosen).toContain("choisi pour ce rapport");
  expect(chosen).toContain("gradient.mrc");
  await page.getByTestId("atlas-clear").click();
  await expect(page.getByTestId("report-atlas")).toContainText("Atlas_1.mrc (trouvé dans la session)");
  await expect(page.getByTestId("atlas-clear")).toHaveCount(0);

  // made by itself when the watch ends, unless told not to: the choice is kept by the server
  const auto = page.getByTestId("report-auto");
  await expect(auto).toBeChecked();
  await auto.uncheck();
  await page.reload();
  await expect(page.getByTestId("report-auto")).not.toBeChecked();
  await page.getByTestId("report-auto").check();
  await expect(page.getByTestId("report-auto")).toBeChecked();

  // the microscope of the session: the voltage of a 200 kV microscope, then back to the server's
  await expect(page.getByTestId("microscope-source")).toContainText("300 kV (serveur)");
  await page.getByTestId("microscope-kv").fill("2000"); // not a voltage one can ask for
  await expect(page.getByTestId("microscope-save")).toBeDisabled();
  await page.getByTestId("microscope-kv").fill("200");
  await page.getByTestId("microscope-save").click();
  await expect(page.getByTestId("microscope-note")).toContainText("enregistré");
  await expect(page.getByTestId("microscope-source")).toContainText("200 kV (réglé pour la session)", { timeout: 10_000 });
  await page.reload();
  await expect(page.getByTestId("microscope-kv")).toHaveValue("200");
  await page.getByTestId("microscope-kv").fill("");
  await page.getByTestId("microscope-save").click();
  await expect(page.getByTestId("microscope-source")).toContainText("300 kV (serveur)", { timeout: 10_000 });
  await expect(page.getByTestId("error")).toHaveCount(0);
});
