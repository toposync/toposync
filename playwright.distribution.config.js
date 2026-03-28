// @ts-check

const { defineConfig } = require("@playwright/test");

const baseURL = process.env.TOPOSYNC_DISTRIBUTION_BASE_URL || "http://127.0.0.1:8000";

module.exports = defineConfig({
  testDir: "./e2e",
  testMatch: /distribution\.spec\.js/,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL,
    headless: true,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 720 },
  },
});
