import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The production build goes straight into the Python package, which serves it:
// `imagebio serve` then delivers the whole application.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/src/imagebio/static",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
});
