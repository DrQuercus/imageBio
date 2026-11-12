import { expect, type Page, test } from "@playwright/test";

/** The test server runs in standalone mode: its one user, "local", administers it. Every
 *  test puts back what it changed: the server is shared by the whole run. */

async function openSettings(page: Page, tab = ""): Promise<void> {
  await page.goto(tab ? `/#settings=${tab}` : "/#settings");
  await expect(page.getByTestId("mode-settings")).toHaveClass(/on/);
}

const roots = async (page: Page) =>
  ((await (await page.request.get("/api/fs/roots")).json()) as { roots: { name: string }[] }).roots.map((r) => r.name);

test("one's account, reached from the viewer and from Live", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("mode-settings").click();
  await expect(page).toHaveURL(/#settings$/);
  await expect(page.getByTestId("me-name")).toHaveText("local");
  await expect(page.getByTestId("me-role")).toHaveText("administrateur");
  await expect(page.getByTestId("me-roots")).toContainText("demo");
  await page.getByTestId("mode-live").click();
  await expect(page.getByTestId("mode-live")).toHaveClass(/on/);
  await page.getByTestId("mode-settings").click();
  await page.getByTestId("mode-viewer").click();
  await expect(page.getByTestId("mode-viewer")).toHaveClass(/on/);
});

test("a data root added here is served at once, and the file comes back", async ({ page }) => {
  await openSettings(page, "roots");
  await expect(page.getByTestId("source-roots")).toHaveText("fichier de configuration");
  await expect(page.getByTestId("root-row")).toHaveCount(1);
  await expect(page.getByTestId("root-name").first()).toHaveValue("demo");

  // outside the parents the file allows: refused, and said without a path
  await page.getByTestId("root-add").click();
  await page.getByTestId("root-name").nth(1).fill("etc");
  await page.getByTestId("root-path").nth(1).fill("/etc");
  await page.getByTestId("save-roots").click();
  await expect(page.getByTestId("error")).toContainText("inside a data root of the configuration file");
  await expect(page.getByTestId("error")).not.toContainText("/etc");
  await page.getByTestId("error").getByRole("button").click();

  // a folder inside the root of the file, for a group (the page offers the root to start from)
  const path = page.getByTestId("root-path").nth(1);
  await page.getByTestId("root-remove").nth(1).click();
  await page.getByTestId("root-add").click();
  const parent = await path.inputValue();
  expect(parent).toMatch(/\/\.e2e-data\/demo\/$/);
  await page.getByTestId("root-name").nth(1).fill("session");
  await path.fill(`${parent}session`);
  await page.getByTestId("root-groups").nth(1).fill("cryoem");
  await page.getByTestId("save-roots").click();
  await expect(page.getByTestId("settings-note")).toContainText("Dossiers de données : enregistré");
  await expect(page.getByTestId("source-roots")).toContainText("changé ici");
  expect(await roots(page)).toEqual(["demo", "session"]);
  const listed = await (await page.request.get("/api/fs/list?path=session")).json();
  expect(listed.entries.map((e: { name: string }) => e.name)).toContain("inside.mrc");

  // kept by the server, then back to the file
  await page.reload();
  await expect(page.getByTestId("root-row")).toHaveCount(2);
  await page.getByTestId("reset-roots").click();
  await expect(page.getByTestId("source-roots")).toHaveText("fichier de configuration");
  expect(await roots(page)).toEqual(["demo"]);
});

test("microscope, Live, alert channels and accounts; a change made meanwhile is not overwritten", async ({ page }) => {
  await openSettings(page, "microscope");
  await expect(page.getByTestId("microscope-kv")).toHaveAttribute("placeholder", "300");
  await page.getByTestId("microscope-kv").fill("200");
  await page.getByTestId("save-microscope").click();
  await expect(page.getByTestId("source-microscope")).toContainText("changé ici");
  await page.getByTestId("live-idle").fill("5"); // not a silence the server accepts
  await page.getByTestId("save-live").click();
  await expect(page.getByTestId("microscope-error")).toContainText("10");

  // another administrator saves meanwhile: this page's change is refused, and it shows theirs
  const state = await (await page.request.get("/api/admin/settings")).json();
  const theirs = await page.request.put("/api/admin/settings/microscope", {
    data: { version: state.version, value: { kv: 120 } },
  });
  expect(theirs.status()).toBe(200);
  await page.getByTestId("live-idle").fill("60");
  await page.getByTestId("save-live").click();
  await expect(page.getByTestId("error")).toContainText("ont changé entre-temps");
  await expect(page.getByTestId("microscope-kv")).toHaveValue("120");
  await page.getByTestId("reset-microscope").click();
  await expect(page.getByTestId("source-microscope")).toHaveText("fichier de configuration");

  // an alert channel: users get its name, never its address
  await page.getByTestId("settings-tab-alerts").click();
  await page.getByTestId("channel-add").click();
  await page.getByTestId("channel-name").last().fill("telephone");
  await page.getByTestId("channel-target").last().fill("https://ntfy.sh/lab-topic-for-tests");
  await page.getByTestId("save-alerts").click();
  await expect(page.getByTestId("source-alerts")).toContainText("changé ici");
  const channels = await (await page.request.get("/api/live/alerts/channels")).text();
  expect(channels).toContain("telephone");
  expect(channels).not.toContain("lab-topic-for-tests");
  await page.getByTestId("reset-alerts").click();
  await expect(page.getByTestId("source-alerts")).toHaveText("fichier de configuration");

  // an account given groups before it ever came
  await page.getByTestId("settings-tab-accounts").click();
  await page.getByTestId("account-new-name").fill("bob@lab");
  await page.getByTestId("account-new-groups").fill("lm, cryoem");
  await page.getByTestId("account-add").click();
  const bob = page.locator('[data-testid="account-row"][data-name="bob@lab"]');
  await expect(bob.getByTestId("account-groups")).toHaveValue("cryoem, lm");
  await bob.getByTestId("account-remove").click();
  await expect(bob).toHaveCount(0);

  // the log says who did what, without the address of the channel
  await page.getByTestId("settings-tab-log").click();
  await expect(page.getByTestId("log-row").first()).toContainText("compte bob@lab");
  await expect(page.getByTestId("settings-log")).toContainText("canaux d'alerte : +telephone");
  await expect(page.getByTestId("settings-log")).not.toContainText("lab-topic-for-tests");

  // what only the file sets, to read; nothing secret
  await page.getByTestId("settings-tab-config").click();
  await expect(page.getByTestId("settings-config")).toContainText("Authentification");
  await expect(page.getByTestId("settings-config")).toContainText("mode");
});

test("scripts and assistants in standalone mode: no key, how to plug one in", async ({ page }) => {
  await openSettings(page, "ai");
  await expect(page.getByTestId("keys-standalone")).toBeVisible();
  await expect(page.getByTestId("key-make")).toHaveCount(0);
  const config = JSON.parse((await page.getByTestId("ai-mcp-config").textContent()) ?? "{}");
  expect(config.mcpServers.imagebio.env).toEqual({ IMAGEBIO_URL: "http://127.0.0.1:8765" });
  expect(config.mcpServers.imagebio.args).toEqual(["/chemin/vers/imagebio_ai.py", "mcp"]);
  for (const [name, start] of [
    ["imagebio_ai.py", "#!/usr/bin/env python3"],
    ["SKILL.md", "---\nname: imagebio"],
  ] as const) {
    const r = await page.request.get(`/api/ai/${name}`);
    expect(r.status()).toBe(200);
    expect((await r.text()).startsWith(start)).toBe(true);
  }
  const spec = await (await page.request.get("/api/openapi.json")).json();
  expect(Object.keys(spec.paths)).toContain("/api/live/watches/{watch_id}/items");
});
