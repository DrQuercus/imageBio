import { defineConfig } from "@playwright/test";

const PORT = 8765;

// The tests run against the real backend serving the built interface (npm run build first)
// and a directory of generated files.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1280, height: 800 },
    trace: "off",
  },
  webServer: {
    command: `python3 e2e/make_fixtures.py .e2e-data && python3 -m imagebio.api.cli serve --config .e2e-data/imagebio.toml --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
