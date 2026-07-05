import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const requested = process.argv.slice(2).filter((arg) => arg !== "--all");
const webpackBin = path.join(
  root,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "webpack.cmd" : "webpack",
);
const configPath = path.join(root, "frontend", "extensionWebpackConfig.js");

function extensionUiDirs() {
  return fs
    .readdirSync(path.join(root, "extensions"), { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(root, "extensions", entry.name, "ui"))
    .filter((uiDir) => fs.existsSync(path.join(uiDir, "package.json")))
    .sort();
}

function matchesRequest(uiDir) {
  if (requested.length === 0) return true;
  const extensionName = path.basename(path.dirname(uiDir));
  const packageJson = JSON.parse(fs.readFileSync(path.join(uiDir, "package.json"), "utf8"));
  return requested.some((value) => value === extensionName || value === packageJson.name);
}

for (const uiDir of extensionUiDirs().filter(matchesRequest)) {
  const extensionName = path.basename(path.dirname(uiDir));
  console.log(`\n> Building extension UI: ${extensionName}`);
  const result = spawnSync(webpackBin, ["--config", configPath, "--mode", "production"], {
    cwd: uiDir,
    stdio: "inherit",
  });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
