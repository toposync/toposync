// @ts-check

const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://127.0.0.1:5173",
    headless: true,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 720 },
  },
  webServer: [
    {
      command: "uv run toposync serve --data-dir ./.toposync-data/e2e --port 8000 --log-level warning",
      url: "http://127.0.0.1:8000/api/health",
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: "npm --workspace @toposync/frontend run dev",
      url: "http://127.0.0.1:5173",
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});

