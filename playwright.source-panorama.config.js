const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./e2e",
  testMatch: "source-panorama.spec.js",
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 12_000 },
  outputDir: ".toposync-data/source-panorama-validation/test-results",
  reporter: [["list"], ["json", { outputFile: ".toposync-data/source-panorama-validation/results.json" }]],
  use: {
    baseURL: "http://127.0.0.1:5178", headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure", screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: ".venv/bin/python tests/source_panorama_browser_fixture.py --port 8108 --data-dir .toposync-data/source-panorama-browser-fixture",
      url: "http://127.0.0.1:8108/api/__source_panorama_fixture",
      timeout: 60_000,
      reuseExistingServer: process.env.TOPOSYNC_REUSE_SOURCE_PANORAMA_TEST_SERVER === "1",
    },
    {
      command: "TOPOSYNC_FRONTEND_PORT=5178 TOPOSYNC_BACKEND_PORT=8108 npm --workspace @toposync/frontend run dev -- --host 127.0.0.1",
      url: "http://127.0.0.1:5178", timeout: 60_000,
      reuseExistingServer: process.env.TOPOSYNC_REUSE_SOURCE_PANORAMA_TEST_SERVER === "1",
    },
  ],
});
