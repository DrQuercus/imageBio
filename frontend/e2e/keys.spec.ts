import { type Browser, expect, request, test } from "@playwright/test";

/** Personal keys (ADR 015), against the second test server: proxy mode, its own files
 *  (e2e/proxy_fixtures.py). The tests play the proxy: they send X-Forwarded-User. */

const BASE = "http://127.0.0.1:8767"; // PROXY_PORT of playwright.config.ts

async function as(browser: Browser, user: string) {
  const context = await browser.newContext({ baseURL: BASE, extraHTTPHeaders: { "X-Forwarded-User": user } });
  return { context, page: await context.newPage() };
}

/** A client with nothing but the key: no password, no identity from a proxy. */
async function withKey(key: string) {
  return request.newContext({ baseURL: BASE, extraHTTPHeaders: { Authorization: `Bearer ${key}` } });
}

test("a key made in the page is shown once, reads, only reads, and is revoked", async ({ browser }) => {
  const { context, page } = await as(browser, "alice");
  await page.goto("/#settings=ai");
  await expect(page.getByTestId("settings-ai")).toBeVisible();
  await expect(page.getByTestId("keys-none")).toBeVisible();
  await expect(page.getByTestId("ai-mcp-config")).toContainText(`"IMAGEBIO_URL": "${BASE}"`);
  await expect(page.getByTestId("ai-mcp-config")).toContainText(`"IMAGEBIO_KEY": "ibk_…"`);
  await expect(page.getByTestId("ai-privacy")).toBeVisible();

  await expect(page.getByTestId("key-make")).toBeDisabled();
  await page.getByTestId("key-name").fill("assistant");
  await page.getByTestId("key-days").selectOption("30");
  await page.getByTestId("key-make").click();
  await expect(page.getByTestId("key-made")).toBeVisible();
  const key = await page.getByTestId("key-value").inputValue();
  expect(key).toMatch(/^ibk_[A-Za-z0-9_-]{40,}$/);
  await expect(page.getByTestId("key-row")).toHaveCount(1);
  await expect(page.getByTestId("key-row")).toContainText(`${key.slice(0, 8)}…`);
  await expect(page.getByTestId("key-row")).toContainText("jamais");
  // the snippets never hold the key itself
  await expect(page.getByTestId("ai-mcp-config")).not.toContainText(key);

  const client = await withKey(key);
  const me = await (await client.get("/api/me")).json();
  expect([me.user, me.admin]).toEqual(["alice", false]);
  expect((await client.get("/api/fs/list?path=lab/shared")).status()).toBe(200);
  const write = await client.post("/api/me/keys", { data: { name: "another", days: 1 } });
  expect(write.status()).toBe(403);
  expect((await write.json()).error.code).toBe("read_only_key");

  // shown once: gone when dismissed, and not shown again after a reload
  await page.getByTestId("key-hide").click();
  await expect(page.getByTestId("key-made")).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("key-row")).toHaveCount(1);
  expect(await page.content()).not.toContain(key);

  const download = page.waitForEvent("download");
  await page.getByTestId("ai-download-client").click();
  expect((await download).suggestedFilename()).toBe("imagebio_ai.py");

  await page.getByTestId("key-revoke").click();
  await expect(page.getByTestId("keys-none")).toBeVisible();
  expect((await client.get("/api/me")).status()).toBe(401);
  await client.dispose();
  await context.close();
});

test("an administrator sees the keys of an account and revokes them", async ({ browser }) => {
  const alice = await request.newContext({ baseURL: BASE, extraHTTPHeaders: { "X-Forwarded-User": "alice" } });
  const made = await alice.post("/api/me/keys", { data: { name: "laptop", days: 7 } });
  expect(made.status()).toBe(201);
  const key = ((await made.json()) as { key: string }).key;
  const client = await withKey(key);
  expect((await client.get("/api/me")).status()).toBe(200);

  const { context, page } = await as(browser, "root"); // named in the file
  await page.goto("/#settings=accounts");
  const row = page.locator('[data-testid="account-row"][data-name="alice"]');
  await expect(row.getByTestId("account-keys")).toContainText("1");
  await row.getByTestId("account-revoke-keys").click();
  await expect(page.getByTestId("settings-note")).toContainText("alice : 1 clé(s) révoquée(s)");
  await expect(row.getByTestId("account-revoke-keys")).toHaveCount(0);
  expect((await client.get("/api/me")).status()).toBe(401);
  await Promise.all([client.dispose(), alice.dispose(), context.close()]);
});
