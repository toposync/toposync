#!/usr/bin/env node

const { execFile, spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const isWindows = process.platform === "win32";
const npmCmd = isWindows ? "npm.cmd" : "npm";
const runnerCmd = npmCmd;

function loadDotenv() {
  const envFile = String(process.env.TOPOSYNC_ENV_FILE ?? ".env").trim() || ".env";
  const envPath = path.resolve(process.cwd(), envFile);
  if (!fs.existsSync(envPath)) return;

  const content = fs.readFileSync(envPath, "utf8");
  for (const rawLine of content.split(/\r?\n/)) {
    const trimmed = rawLine.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const line = trimmed.startsWith("export ") ? trimmed.slice(7).trim() : trimmed;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;

    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    if (Object.prototype.hasOwnProperty.call(process.env, key)) continue;

    let value = line.slice(eq + 1).trim();
    const quoted =
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"));
    if (quoted && value.length >= 2) value = value.slice(1, -1);
    process.env[key] = value;
  }
}

loadDotenv();

function spawnScript(label, scriptName, extraArgs = []) {
  const child = spawn(runnerCmd, ["run", scriptName, ...extraArgs], {
    stdio: "inherit",
    env: process.env,
    detached: !isWindows,
  });

  child.on("error", (err) => {
    console.error(`[dev] Failed to start ${label}:`, err);
  });

  return child;
}

function waitForExit(child, timeoutMs) {
  if (!child || child.exitCode != null) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timeout = setTimeout(() => resolve(false), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve(true);
    });
  });
}

function onceExit(child) {
  if (!child || child.exitCode != null) return Promise.resolve();
  return new Promise((resolve) => {
    child.once("exit", resolve);
  });
}

function killProcessTree(child, signal) {
  if (!child || child.exitCode != null) return Promise.resolve();
  const pid = child.pid;
  if (!pid) return Promise.resolve();

  if (isWindows) {
    return new Promise((resolve) => {
      execFile("taskkill", ["/PID", String(pid), "/T"], (err) => {
        if (!err) return resolve();
        execFile("taskkill", ["/PID", String(pid), "/T", "/F"], () => resolve());
      });
    });
  }

  try {
    process.kill(-pid, signal);
  } catch {
  }
  return Promise.resolve();
}

const backendDataDir = String(process.env.TOPOSYNC_DATA_DIR ?? ".toposync-data").trim() || ".toposync-data";
const backendArgs = ["--", "--data-dir", backendDataDir];

const backend = spawnScript("backend", "dev:backend", backendArgs);
const frontend = spawnScript("frontend", "dev:frontend");

let shuttingDown = false;
let desiredExitCode = 0;

function describeExit(code, signal) {
  if (signal) return `signal ${signal}`;
  if (typeof code === "number") return `code ${code}`;
  return "unknown status";
}

function handleChildExit(label, code, signal) {
  const unexpected = !shuttingDown;
  if (unexpected) {
    console.error(`[dev] ${label} exited unexpectedly (${describeExit(code, signal)}). Shutting down the rest.`);
  }

  if (typeof code === "number" && code !== 0) {
    desiredExitCode ||= code;
  } else if (signal && unexpected) {
    desiredExitCode ||= 1;
  }

  void shutdown("SIGINT");
}

async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;

  const frontendSignal = signal === "SIGTERM" ? "SIGTERM" : "SIGINT";
  const backendSignal = "SIGTERM";

  await killProcessTree(frontend, frontendSignal);
  await waitForExit(frontend, 4_000);

  await killProcessTree(backend, backendSignal);
}

backend.on("exit", (code, signal) => {
  handleChildExit("backend", code, signal);
});

frontend.on("exit", (code, signal) => {
  handleChildExit("frontend", code, signal);
});

process.on("SIGINT", () => {
  void shutdown("SIGINT");
});

process.on("SIGTERM", () => {
  void shutdown("SIGTERM");
});

Promise.all([onceExit(backend), onceExit(frontend)]).then(() => {
  process.exitCode = desiredExitCode;
});
