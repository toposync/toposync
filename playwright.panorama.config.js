// @ts-check
const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./e2e",
  testMatch: "panorama-mapping.spec.js",
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 12_000 },
  use: {
    baseURL: "http://127.0.0.1:5187",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: ".venv/bin/python tests/panorama_browser_fixture.py --port 8127 --data-dir .toposync-data/panorama-browser-fixture",
      url: "http://127.0.0.1:8127/api/__panorama_fixture",
      timeout: 60_000,
      reuseExistingServer: process.env.TOPOSYNC_REUSE_PANORAMA_TEST_SERVER === "1",
    },
    {
      command: "TOPOSYNC_FRONTEND_PORT=5187 TOPOSYNC_BACKEND_PORT=8127 npm --workspace @toposync/frontend run dev -- --host 127.0.0.1",
      url: "http://127.0.0.1:5187",
      timeout: 60_000,
      reuseExistingServer: process.env.TOPOSYNC_REUSE_PANORAMA_TEST_SERVER === "1",
    },
  ],
});
