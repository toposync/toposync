const { defineConfig } = require("@playwright/test");
const path = require("node:path");
const os = require("node:os");
const artifacts = process.env.TOPOSYNC_GEOGRAPHIC_ARTIFACT_DIR || path.join(os.homedir(), ".codex/task-artifacts/toposync/ajustes-configuracao-cameras-2026-10-10/mapa-itens-16-22/validacao/browser");

module.exports = defineConfig({
  testDir: "./e2e", testMatch: "geographic-map.spec.js", workers: 1,
  timeout: 60000, expect: { timeout: 12000 },
  outputDir: path.join(artifacts, "results"), reporter: [["list"], ["json", { outputFile: path.join(artifacts, "results.json") }]],
  use: { baseURL: "http://127.0.0.1:5197", headless: true, viewport: { width: 1440, height: 1000 }, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    { command: `.venv/bin/python tests/geographic_browser_fixture.py --port 8137 --data-dir '${path.join(artifacts, "data").replace(/'/g, "'\\''")}'`, url: "http://127.0.0.1:8137/api/__geographic_fixture", reuseExistingServer: false, timeout: 60000 },
    { command: "TOPOSYNC_FRONTEND_PORT=5197 TOPOSYNC_BACKEND_PORT=8137 npm --workspace @toposync/frontend run dev -- --host 127.0.0.1", url: "http://127.0.0.1:5197", reuseExistingServer: false, timeout: 60000 },
  ],
});
