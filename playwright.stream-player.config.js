const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'stream-player.spec.js',
  workers: 1,
  timeout: 30000,
  expect: { timeout: 12000 },
  outputDir: '.toposync-data/stream-player-validation/results',
  use: {
    baseURL: 'http://127.0.0.1:5190',
    headless: true,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npx webpack serve --config e2e/fixtures/stream-player/webpack.config.js',
    url: 'http://127.0.0.1:5190',
    timeout: 60000,
  },
});
