#!/usr/bin/env node

const { execFile, spawn } = require("node:child_process");

const isWindows = process.platform === "win32";
const npmCmd = isWindows ? "npm.cmd" : "npm";
const runnerCmd = npmCmd;

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

const backendDataDir = String(process.env.TOPOSYNC_DATA_DIR ?? "").trim();
const backendArgs = backendDataDir ? ["--", "--data-dir", backendDataDir] : [];

const backend = spawnScript("backend", "dev:backend", backendArgs);
const frontend = spawnScript("frontend", "dev:frontend");

let shuttingDown = false;
let desiredExitCode = 0;

async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;

  const frontendSignal = signal === "SIGTERM" ? "SIGTERM" : "SIGINT";
  const backendSignal = "SIGTERM";

  await killProcessTree(frontend, frontendSignal);
  await waitForExit(frontend, 4_000);

  await killProcessTree(backend, backendSignal);
}

backend.on("exit", (code) => {
  if (typeof code === "number" && code !== 0) desiredExitCode ||= code;
  void shutdown("SIGINT");
});

frontend.on("exit", (code) => {
  if (typeof code === "number" && code !== 0) desiredExitCode ||= code;
  void shutdown("SIGINT");
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
