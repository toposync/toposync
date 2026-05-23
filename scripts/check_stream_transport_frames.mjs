#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const DEFAULT_BASE_URL = "http://127.0.0.1:8100";
const DEFAULT_LIVE_VIEW_ID = "local-frente-live";
const DEFAULT_CONTEXT = "thumbnail";
const DEFAULT_OUT_DIR = ".toposync-data/runtime/stream-transport-frame-checks";
const FFMPEG_TIMEOUT_MS = 20_000;
const BROWSER_CAPTURE_TIMEOUT_MS = 20_000;
const HLS_FRAME_ATTEMPTS = 8;
const HLS_FRAME_RETRY_MS = 1500;

class OptionalTransportUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = "OptionalTransportUnavailable";
  }
}

function parseArgs(argv) {
  const out = {
    baseUrl: process.env.TOPOSYNC_FRAME_CHECK_BASE_URL || DEFAULT_BASE_URL,
    liveViewId: process.env.TOPOSYNC_FRAME_CHECK_LIVE_VIEW_ID || DEFAULT_LIVE_VIEW_ID,
    context: process.env.TOPOSYNC_FRAME_CHECK_CONTEXT || DEFAULT_CONTEXT,
    outDir: process.env.TOPOSYNC_FRAME_CHECK_OUT_DIR || DEFAULT_OUT_DIR,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    const next = argv[index + 1];
    if (item === "--base-url" && next) {
      out.baseUrl = next;
      index += 1;
    } else if (item === "--live-view-id" && next) {
      out.liveViewId = next;
      index += 1;
    } else if (item === "--context" && next) {
      out.context = next;
      index += 1;
    } else if (item === "--out-dir" && next) {
      out.outDir = next;
      index += 1;
    }
  }
  out.baseUrl = String(out.baseUrl).replace(/\/+$/, "");
  return out;
}

async function fetchJson(url, init = {}) {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`${init.method || "GET"} ${url} failed (${response.status}) ${body.slice(0, 300)}`);
  }
  return response.json();
}

async function primeDemand(baseUrl, transmissionId, outputId) {
  const query = outputId ? `?output_id=${encodeURIComponent(outputId)}` : "";
  await fetchJson(`${baseUrl}/api/streams/transmissions/${encodeURIComponent(transmissionId)}/demand/prime${query}`, {
    method: "POST",
  }).catch(() => null);
}

async function heartbeat(baseUrl, transmissionId, outputId, transport) {
  await fetchJson(`${baseUrl}/api/streams/transmissions/${encodeURIComponent(transmissionId)}/demand/heartbeat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      playback_session_id: `frame-check-${transport}-${Date.now()}`,
      transport,
      output_id: outputId || null,
      ttl_seconds: 45,
    }),
  });
}

function runProcess(command, args, { timeoutMs = FFMPEG_TIMEOUT_MS, input = null } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    const timeout = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`${command} timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      const stdoutBuffer = Buffer.concat(stdout);
      const stderrText = Buffer.concat(stderr).toString("utf8");
      if (code !== 0) {
        reject(new Error(`${command} exited with ${code}: ${stderrText.slice(-1200)}`));
        return;
      }
      resolve({ stdout: stdoutBuffer, stderr: stderrText });
    });
    if (input) {
      child.stdin.end(input);
    } else {
      child.stdin.end();
    }
  });
}

function outputForProtocol(playback, protocol) {
  return (playback.urls?.outputs || []).find((item) => item.protocol === protocol) || null;
}

function absoluteMediaUrl(baseUrl, url) {
  const value = String(url || "").trim();
  if (!value) return "";
  return value.startsWith("http://") || value.startsWith("https://") ? value : `${baseUrl}${value}`;
}

async function saveDataUrl(dataUrl, outputPath) {
  const match = /^data:image\/png;base64,(.+)$/i.exec(String(dataUrl || ""));
  if (!match) {
    throw new Error("Browser capture did not return a PNG data URL.");
  }
  await writeFile(outputPath, Buffer.from(match[1], "base64"));
}

async function imageStats(imagePath) {
  const info = await stat(imagePath);
  const width = 160;
  const height = 90;
  const { stdout } = await runProcess(
    "ffmpeg",
    [
      "-v",
      "error",
      "-i",
      imagePath,
      "-vf",
      `scale=${width}:${height}`,
      "-frames:v",
      "1",
      "-f",
      "rawvideo",
      "-pix_fmt",
      "rgb24",
      "pipe:1",
    ],
    { timeoutMs: 10_000 },
  );
  let r = 0;
  let g = 0;
  let b = 0;
  let greenPixels = 0;
  let nonDarkPixels = 0;
  const pixels = Math.floor(stdout.length / 3);
  for (let index = 0; index < pixels; index += 1) {
    const offset = index * 3;
    const pr = stdout[offset];
    const pg = stdout[offset + 1];
    const pb = stdout[offset + 2];
    r += pr;
    g += pg;
    b += pb;
    const brightness = (pr + pg + pb) / 3;
    if (brightness > 32) nonDarkPixels += 1;
    if (pg > pr + 8 && pg > pb + 4 && brightness > 35) greenPixels += 1;
  }
  const safePixels = Math.max(1, pixels);
  return {
    file_bytes: info.size,
    sample_width: width,
    sample_height: height,
    avg_rgb: [
      Number((r / safePixels).toFixed(1)),
      Number((g / safePixels).toFixed(1)),
      Number((b / safePixels).toFixed(1)),
    ],
    brightness: Number(((r + g + b) / (safePixels * 3)).toFixed(1)),
    green_pixel_ratio: Number((greenPixels / safePixels).toFixed(3)),
    non_dark_pixel_ratio: Number((nonDarkPixels / safePixels).toFixed(3)),
  };
}

function frameLooksVisual(stats) {
  if (!stats) return false;
  if (Number(stats.file_bytes || 0) <= 1024) return false;
  if (Number(stats.non_dark_pixel_ratio || 0) < 0.15) return false;
  return Number(stats.brightness || 0) >= 8;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function captureRtspFrame({ baseUrl, playback, outputDir }) {
  const output = outputForProtocol(playback, "rtsp");
  if (!output?.url) throw new OptionalTransportUnavailable("No RTSP output URL is available.");
  await primeDemand(baseUrl, playback.transmission.id, output.output_id);
  await heartbeat(baseUrl, playback.transmission.id, output.output_id, "rtsp");
  const framePath = path.join(outputDir, "rtsp.jpg");
  await runProcess(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "warning",
      "-y",
      "-rtsp_transport",
      "tcp",
      "-i",
      output.url,
      "-frames:v",
      "1",
      "-q:v",
      "2",
      framePath,
    ],
    { timeoutMs: FFMPEG_TIMEOUT_MS },
  );
  return { transport: "rtsp", output_id: output.output_id, path: framePath, stats: await imageStats(framePath) };
}

async function withBrowser(baseUrl, callback) {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    await page.goto(`${baseUrl}/api/streams/health`, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      document.body.innerHTML = "";
      document.body.style.margin = "0";
      document.body.style.background = "#000";
    });
    return await callback(page);
  } finally {
    await browser.close();
  }
}

async function captureHlsFrame({ baseUrl, playback, outputDir }) {
  const output = outputForProtocol(playback, "hls");
  if (!output?.url) throw new OptionalTransportUnavailable("No HLS output URL is available.");
  const framePath = path.join(outputDir, "hls.jpg");
  let lastStats = null;
  for (let attempt = 1; attempt <= HLS_FRAME_ATTEMPTS; attempt += 1) {
    await primeDemand(baseUrl, playback.transmission.id, output.output_id);
    await heartbeat(baseUrl, playback.transmission.id, output.output_id, "hls");
    await runProcess(
      "ffmpeg",
      [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        absoluteMediaUrl(baseUrl, output.url),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-update",
        "1",
        framePath,
      ],
      { timeoutMs: FFMPEG_TIMEOUT_MS },
    );
    lastStats = await imageStats(framePath);
    if (frameLooksVisual(lastStats)) {
      return { transport: "hls", output_id: output.output_id, path: framePath, stats: lastStats, attempts: attempt };
    }
    if (attempt < HLS_FRAME_ATTEMPTS) await sleep(HLS_FRAME_RETRY_MS);
  }
  throw new Error(`HLS stayed black/dark after ${HLS_FRAME_ATTEMPTS} attempts: ${JSON.stringify(lastStats)}`);
}

async function captureWebRtcFrame({ baseUrl, playback, outputDir }) {
  const output = outputForProtocol(playback, "webrtc");
  if (!output?.url) throw new OptionalTransportUnavailable("No WebRTC output URL is available.");
  await primeDemand(baseUrl, playback.transmission.id, output.output_id);
  await heartbeat(baseUrl, playback.transmission.id, output.output_id, "webrtc");
  const framePath = path.join(outputDir, "webrtc.png");
  const dataUrl = await withBrowser(baseUrl, async (page) => {
    return await page.evaluate(
      async ({ url, timeoutMs }) => {
        const normalizeSdp = (sdp) => {
          const normalized = String(sdp || "").replace(/\r?\n/g, "\r\n");
          return normalized.endsWith("\r\n") ? normalized : `${normalized}\r\n`;
        };
        const waitForIceGatheringComplete = (peerConnection) =>
          new Promise((resolve, reject) => {
            if (peerConnection.iceGatheringState === "complete") {
              resolve();
              return;
            }
            const timeout = window.setTimeout(() => {
              peerConnection.removeEventListener("icegatheringstatechange", onChange);
              reject(new Error("Timed out waiting for ICE gathering."));
            }, 5000);
            function onChange() {
              if (peerConnection.iceGatheringState !== "complete") return;
              window.clearTimeout(timeout);
              peerConnection.removeEventListener("icegatheringstatechange", onChange);
              resolve();
            }
            peerConnection.addEventListener("icegatheringstatechange", onChange);
          });
        const waitForFrame = (video) =>
          new Promise((resolve, reject) => {
            const started = Date.now();
            const timer = window.setInterval(() => {
              if (video.videoWidth > 0 && video.videoHeight > 0 && video.readyState >= 2) {
                window.clearInterval(timer);
                resolve();
                return;
              }
              if (Date.now() - started > timeoutMs) {
                window.clearInterval(timer);
                reject(new Error("Timed out waiting for WebRTC video frame."));
              }
            }, 100);
          });

        const video = document.createElement("video");
        video.muted = true;
        video.autoplay = true;
        video.playsInline = true;
        document.body.appendChild(video);

        const peerConnection = new RTCPeerConnection();
        const remoteStream = new MediaStream();
        video.srcObject = remoteStream;
        peerConnection.ontrack = (event) => {
          const stream = event.streams[0];
          if (stream) {
            for (const track of stream.getTracks()) {
              if (!remoteStream.getTracks().some((item) => item.id === track.id)) {
                remoteStream.addTrack(track);
              }
            }
            return;
          }
          remoteStream.addTrack(event.track);
        };
        peerConnection.addTransceiver("video", { direction: "recvonly" });
        peerConnection.addTransceiver("audio", { direction: "recvonly" });

        let sessionUrl = null;
        try {
          const offer = await peerConnection.createOffer();
          await peerConnection.setLocalDescription(offer);
          await waitForIceGatheringComplete(peerConnection);
          const response = await fetch(url, {
            method: "POST",
            headers: { accept: "application/sdp", "content-type": "application/sdp" },
            body: normalizeSdp(peerConnection.localDescription.sdp),
          });
          if (!response.ok) {
            throw new Error(`WHEP negotiation failed (${response.status}): ${(await response.text()).slice(0, 300)}`);
          }
          const location = response.headers.get("location");
          sessionUrl = location ? new URL(location, url).toString() : null;
          await peerConnection.setRemoteDescription({
            type: "answer",
            sdp: normalizeSdp(await response.text()),
          });
          await video.play().catch(() => null);
          await waitForFrame(video);
          await new Promise((resolve) => window.setTimeout(resolve, 750));
          const canvas = document.createElement("canvas");
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          const context = canvas.getContext("2d");
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          return canvas.toDataURL("image/png");
        } finally {
          peerConnection.close();
          if (sessionUrl) {
            fetch(sessionUrl, { method: "DELETE" }).catch(() => null);
          }
        }
      },
      { url: output.url, timeoutMs: BROWSER_CAPTURE_TIMEOUT_MS },
    );
  });
  await saveDataUrl(dataUrl, framePath);
  return { transport: "webrtc", output_id: output.output_id, path: framePath, stats: await imageStats(framePath) };
}

async function createContactSheet(captures, outputDir) {
  const ordered = ["rtsp", "hls", "webrtc"]
    .map((transport) => captures.find((item) => item.ok && item.transport === transport))
    .filter(Boolean);
  if (ordered.length < 2) return null;

  const outputPath = path.join(outputDir, "contact-sheet.png");
  const args = ["-hide_banner", "-loglevel", "error", "-y"];
  for (const capture of ordered) {
    args.push("-i", capture.path);
  }
  const scaled = ordered.map((_capture, index) => `[${index}:v]scale=426:240[v${index}]`).join(";");
  const inputs = ordered.map((_capture, index) => `[v${index}]`).join("");
  args.push("-filter_complex", `${scaled};${inputs}hstack=inputs=${ordered.length}`, "-frames:v", "1", outputPath);
  await runProcess("ffmpeg", args, { timeoutMs: 10_000 });
  return outputPath;
}

async function run() {
  const options = parseArgs(process.argv.slice(2));
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outputDir = path.resolve(options.outDir, timestamp);
  await mkdir(outputDir, { recursive: true });

  const playback = await fetchJson(
    `${options.baseUrl}/api/streams/camera-live-views/${encodeURIComponent(options.liveViewId)}/playback?context=${encodeURIComponent(options.context)}`,
  );
  const result = {
    ok: true,
    generated_at: new Date().toISOString(),
    base_url: options.baseUrl,
    live_view_id: options.liveViewId,
    context: options.context,
    camera_name: playback.camera_name,
    camera_source_name: playback.camera_source_name,
    transmission_id: playback.transmission?.id,
    output_dir: outputDir,
    captures: [],
  };

  const captureFns = [captureRtspFrame, captureHlsFrame, captureWebRtcFrame];
  for (const captureFn of captureFns) {
    try {
      const capture = await captureFn({ baseUrl: options.baseUrl, playback, outputDir });
      result.captures.push({ ok: true, ...capture });
    } catch (error) {
      const skipped = error instanceof OptionalTransportUnavailable;
      if (!skipped) result.ok = false;
      result.captures.push({
        ok: false,
        skipped,
        transport: captureFn.name.replace(/^capture|Frame$/g, "").toLowerCase(),
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
  if (!result.captures.some((item) => item.ok)) {
    result.ok = false;
  }
  result.contact_sheet_path = await createContactSheet(result.captures, outputDir).catch((error) => {
    result.contact_sheet_error = error instanceof Error ? error.message : String(error);
    return null;
  });

  const reportPath = path.join(outputDir, "report.json");
  await writeFile(reportPath, `${JSON.stringify(result, null, 2)}\n`);
  const report = await readFile(reportPath, "utf8");
  process.stdout.write(report);
  if (!result.ok) process.exitCode = 1;
}

run().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});
