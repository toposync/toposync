const { defineConfig } = require("@playwright/test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const root = path.join(os.homedir(), ".codex/task-artifacts/toposync-2/sensores-visuais-ux-ui/browser-tests");
fs.mkdirSync(root, { recursive: true });
const artifacts = process.env.TOPOSYNC_VISUAL_TEST_RUN_DIR ||= fs.mkdtempSync(path.join(root, "run-"));
if (path.dirname(fs.realpathSync(artifacts)) !== fs.realpathSync(root)) throw new Error("Use a dedicated external visual test directory");
const quote = value => `'${value.replace(/'/g, "'\\''")}'`;

module.exports = defineConfig({
  testDir: "./e2e", testMatch: ["visual-state-editor.spec.js", "visual-state-accessibility.spec.js"],
  workers: 1, timeout: 60000, expect: { timeout: 12000 },
  outputDir: path.join(artifacts, "results"),
  reporter: [["list"], ["json", { outputFile: path.join(artifacts, "results.json") }]],
  use: { baseURL: "http://127.0.0.1:8876", headless: true, serviceWorkers: "block", locale: "pt-BR",
    viewport: { width: 1440, height: 1000 }, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: {
    command: `npm run build:frontend && npm run build:extension-ui -- vision && uv run --no-sync python tests/visual_state_browser_fixture.py --port 8876 --data-dir ${quote(path.join(artifacts, "data"))}`,
    url: "http://127.0.0.1:8876/fixture/stats", reuseExistingServer: false, timeout: 120000,
  },
});
