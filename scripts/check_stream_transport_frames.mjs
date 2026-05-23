#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { chromium } from "playwright";

const require = createRequire(import.meta.url);

const DEFAULT_BASE_URL = "http://127.0.0.1:8100";
const DEFAULT_LIVE_VIEW_ID = "local-frente-live";
const DEFAULT_CONTEXT = "thumbnail";
const DEFAULT_OUT_DIR = ".toposync-data/runtime/stream-transport-frame-checks";
const FFMPEG_TIMEOUT_MS = 20_000;
const BROWSER_CAPTURE_TIMEOUT_MS = 20_000;
const HLS_FRAME_ATTEMPTS = 8;
const HLS_FRAME_RETRY_MS = 1500;
const DEFAULT_TRANSPORTS = ["hls", "webrtc", "mse", "jsmpeg"];

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
    matrix: process.env.TOPOSYNC_FRAME_CHECK_MATRIX === "1",
    transports: String(process.env.TOPOSYNC_FRAME_CHECK_TRANSPORTS || DEFAULT_TRANSPORTS.join(","))
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
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
    } else if (item === "--matrix") {
      out.matrix = true;
    } else if (item === "--transports" && next) {
      out.transports = String(next)
        .split(",")
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean);
      index += 1;
    }
  }
  out.baseUrl = String(out.baseUrl).replace(/\/+$/, "");
  out.transports = out.transports.filter((transport) => ["rtsp", "hls", "webrtc", "mse", "jsmpeg"].includes(transport));
  if (!out.transports.length) out.transports = [...DEFAULT_TRANSPORTS];
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
  let brightnessSum = 0;
  let brightnessSqSum = 0;
  let colorDeltaSum = 0;
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
    brightnessSum += brightness;
    brightnessSqSum += brightness * brightness;
    colorDeltaSum += (Math.abs(pr - pg) + Math.abs(pg - pb) + Math.abs(pr - pb)) / 3;
    if (brightness > 32) nonDarkPixels += 1;
    if (pg > pr + 8 && pg > pb + 4 && brightness > 35) greenPixels += 1;
  }
  const safePixels = Math.max(1, pixels);
  const meanBrightness = brightnessSum / safePixels;
  const variance = Math.max(0, brightnessSqSum / safePixels - meanBrightness * meanBrightness);
  return {
    file_bytes: info.size,
    sample_width: width,
    sample_height: height,
    avg_rgb: [
      Number((r / safePixels).toFixed(1)),
      Number((g / safePixels).toFixed(1)),
      Number((b / safePixels).toFixed(1)),
    ],
    brightness: Number(meanBrightness.toFixed(1)),
    luma_stddev: Number(Math.sqrt(variance).toFixed(1)),
    color_delta: Number((colorDeltaSum / safePixels).toFixed(1)),
    green_pixel_ratio: Number((greenPixels / safePixels).toFixed(3)),
    non_dark_pixel_ratio: Number((nonDarkPixels / safePixels).toFixed(3)),
  };
}

function frameLooksVisual(stats) {
  if (!stats) return false;
  if (Number(stats.file_bytes || 0) <= 1024) return false;
  if (Number(stats.non_dark_pixel_ratio || 0) < 0.15) return false;
  if (Number(stats.brightness || 0) < 8) return false;
  // Placeholders/warmup frames are often flat mid-gray and must not count as visual proof.
  if (Number(stats.luma_stddev || 0) < 3.5 && Number(stats.color_delta || 0) < 3.5) return false;
  return true;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function slug(value, fallback = "item") {
  const normalized = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "");
  return normalized || fallback;
}

function contextForVariant(variant) {
  const role = String(variant?.role || "").trim().toLowerCase();
  if (role === "main") return "fullscreen";
  if (role === "zoom") return "ptz";
  return "thumbnail";
}

function plannedBlocker(playback, transport) {
  const planned = (playback.playback_plan?.transports || []).find((item) => item.transport === transport);
  const blockers = Array.isArray(planned?.blocking_errors) ? planned.blocking_errors : [];
  return blockers.find((item) => String(item || "").trim()) || null;
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
  let lastError = null;
  for (let attempt = 1; attempt <= HLS_FRAME_ATTEMPTS; attempt += 1) {
    await primeDemand(baseUrl, playback.transmission.id, output.output_id);
    await heartbeat(baseUrl, playback.transmission.id, output.output_id, "hls");
    try {
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
    } catch (error) {
      lastError = error;
    }
    if (attempt < HLS_FRAME_ATTEMPTS) await sleep(HLS_FRAME_RETRY_MS);
  }
  const errorText = lastError instanceof Error ? lastError.message : String(lastError || "");
  throw new Error(
    `HLS did not produce a visual frame after ${HLS_FRAME_ATTEMPTS} attempts. last_error=${errorText.slice(
      0,
      500,
    )} last_stats=${JSON.stringify(lastStats)}`,
  );
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

async function captureMseFrame({ baseUrl, playback, outputDir }) {
  const output = outputForProtocol(playback, "mse");
  const blocker = plannedBlocker(playback, "mse");
  if (!output?.url) throw new OptionalTransportUnavailable(blocker || "No MSE output URL is available.");
  await primeDemand(baseUrl, playback.transmission.id, output.output_id);
  await heartbeat(baseUrl, playback.transmission.id, output.output_id, "mse");
  const framePath = path.join(outputDir, "mse.png");
  const dataUrl = await withBrowser(baseUrl, async (page) => {
    return await page.evaluate(
      async ({ url, timeoutMs }) => {
        const codecRequest = "avc1.640029,avc1.64002A,avc1.640033,avc1.42E01E,mp4a.40.2,opus";
        const normalizeWsUrl = (rawUrl) => {
          const parsed = new URL(rawUrl, window.location.href);
          if (parsed.protocol === "http:") parsed.protocol = "ws:";
          if (parsed.protocol === "https:") parsed.protocol = "wss:";
          return parsed.toString();
        };
        const mimeFromMessage = (raw) => {
          const text = String(raw || "").trim();
          if (!text) return null;
          try {
            const parsed = JSON.parse(text);
            const explicit = String(parsed.mime || parsed.mimetype || "").trim();
            if (explicit) return explicit;
            const type = String(parsed.type || "").trim().toLowerCase();
            const value = String(parsed.value || "").trim();
            if (type === "mse" && value.includes("video/mp4")) return value;
            if (type === "mse" && /(avc1|hvc1|hev1|mp4a)/i.test(value)) return `video/mp4; codecs="${value.replace(/^codecs=/i, "").replace(/^"|"$/g, "")}"`;
            const codecs = String(parsed.codecs || parsed.codec || "").trim();
            if (codecs) return `video/mp4; codecs="${codecs}"`;
          } catch {
            // Plain MIME/codecs string.
          }
          if (text.includes("video/mp4")) return text;
          if (/(avc1|hvc1|hev1|mp4a)/i.test(text)) return `video/mp4; codecs="${text.replace(/^codecs=/i, "").replace(/^"|"$/g, "")}"`;
          return null;
        };
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
                reject(new Error("Timed out waiting for MSE video frame."));
              }
            }, 100);
          });
        if (typeof MediaSource === "undefined") throw new Error("MediaSource unavailable.");
        const video = document.createElement("video");
        video.muted = true;
        video.autoplay = true;
        video.playsInline = true;
        document.body.appendChild(video);
        const mediaSource = new MediaSource();
        video.src = URL.createObjectURL(mediaSource);
        const wsUrl = normalizeWsUrl(url);
        let socket = null;
        try {
          await new Promise((resolve, reject) => {
            let settled = false;
            let sourceBuffer = null;
            const queue = [];
            const timeout = window.setTimeout(() => {
              if (settled) return;
              settled = true;
              reject(new Error("Timed out waiting for MSE initialization data."));
            }, timeoutMs);
            const resolveOnce = () => {
              if (settled) return;
              settled = true;
              window.clearTimeout(timeout);
              resolve();
            };
            const rejectOnce = (error) => {
              if (settled) return;
              settled = true;
              window.clearTimeout(timeout);
              reject(error);
            };
            const flush = () => {
              if (!sourceBuffer || sourceBuffer.updating || queue.length === 0) return;
              sourceBuffer.appendBuffer(queue.shift());
            };
            mediaSource.addEventListener(
              "sourceopen",
              () => {
                socket = new WebSocket(wsUrl);
                socket.binaryType = "arraybuffer";
                socket.addEventListener("open", () => {
                  socket.send(JSON.stringify({ type: "mse", value: codecRequest }));
                });
                socket.addEventListener("error", () => rejectOnce(new Error("MSE WebSocket failed.")));
                socket.addEventListener("message", (event) => {
                  if (typeof event.data === "string") {
                    if (sourceBuffer) return;
                    const mime = mimeFromMessage(event.data);
                    if (!mime) return;
                    if (!MediaSource.isTypeSupported(mime)) {
                      rejectOnce(new Error(`Browser does not support MSE mime type: ${mime}`));
                      return;
                    }
                    sourceBuffer = mediaSource.addSourceBuffer(mime);
                    sourceBuffer.addEventListener("updateend", flush);
                    sourceBuffer.addEventListener("error", () => rejectOnce(new Error("MSE SourceBuffer error.")));
                    video.play().catch(() => null);
                    return;
                  }
                  if (!(event.data instanceof ArrayBuffer)) return;
                  queue.push(event.data);
                  flush();
                  resolveOnce();
                });
              },
              { once: true },
            );
          });
          await waitForFrame(video);
          await new Promise((resolve) => window.setTimeout(resolve, 750));
          const canvas = document.createElement("canvas");
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          const context = canvas.getContext("2d");
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          return canvas.toDataURL("image/png");
        } finally {
          if (socket) socket.close();
        }
      },
      { url: absoluteMediaUrl(baseUrl, output.url), timeoutMs: BROWSER_CAPTURE_TIMEOUT_MS },
    );
  });
  await saveDataUrl(dataUrl, framePath);
  return { transport: "mse", output_id: output.output_id, path: framePath, stats: await imageStats(framePath) };
}

async function captureJsmpegFrame({ baseUrl, playback, outputDir }) {
  const output = outputForProtocol(playback, "jsmpeg");
  const blocker = plannedBlocker(playback, "jsmpeg");
  if (!output?.url) throw new OptionalTransportUnavailable(blocker || "No JSMpeg output URL is available.");
  await primeDemand(baseUrl, playback.transmission.id, output.output_id);
  await heartbeat(baseUrl, playback.transmission.id, output.output_id, "jsmpeg");
  const framePath = path.join(outputDir, "jsmpeg.png");
  const playerBundlePath = path.join(
    path.dirname(require.resolve("@cycjimmy/jsmpeg-player")),
    "jsmpeg-player.umd.js",
  );
  const dataUrl = await withBrowser(baseUrl, async (page) => {
    await page.addScriptTag({ path: playerBundlePath });
    return await page.evaluate(
      async ({ url, timeoutMs }) => {
        const normalizeWsUrl = (rawUrl) => {
          const parsed = new URL(rawUrl, window.location.href);
          if (parsed.protocol === "http:") parsed.protocol = "ws:";
          if (parsed.protocol === "https:") parsed.protocol = "wss:";
          return parsed.toString();
        };
        const canvas = document.createElement("canvas");
        canvas.width = 854;
        canvas.height = 480;
        canvas.style.width = "854px";
        canvas.style.height = "480px";
        document.body.appendChild(canvas);
        const jsmpeg = window.JSMpeg;
        if (!jsmpeg?.Player) throw new Error("JSMpeg browser bundle did not expose Player.");
        let player = null;
        try {
          await new Promise((resolve, reject) => {
            let settled = false;
            const timeout = window.setTimeout(() => {
              if (settled) return;
              settled = true;
              reject(new Error("Timed out waiting for JSMpeg video frame."));
            }, timeoutMs);
            const resolveOnce = () => {
              if (settled) return;
              settled = true;
              window.clearTimeout(timeout);
              resolve();
            };
            const rejectOnce = (error) => {
              if (settled) return;
              settled = true;
              window.clearTimeout(timeout);
              reject(error);
            };
            const canvasLooksUseful = () => {
              const probe = document.createElement("canvas");
              probe.width = 80;
              probe.height = 45;
              const context = probe.getContext("2d");
              if (!context) return false;
              context.drawImage(canvas, 0, 0, probe.width, probe.height);
              const pixels = context.getImageData(0, 0, probe.width, probe.height).data;
              let lumaSum = 0;
              let lumaSquaredSum = 0;
              let nonDark = 0;
              let colorDeltaSum = 0;
              const count = probe.width * probe.height;
              for (let index = 0; index < pixels.length; index += 4) {
                const red = pixels[index];
                const green = pixels[index + 1];
                const blue = pixels[index + 2];
                const luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue;
                lumaSum += luma;
                lumaSquaredSum += luma * luma;
                colorDeltaSum += Math.abs(red - green) + Math.abs(green - blue) + Math.abs(red - blue);
                if (luma > 20) nonDark += 1;
              }
              const mean = lumaSum / count;
              const variance = Math.max(0, lumaSquaredSum / count - mean * mean);
              const stddev = Math.sqrt(variance);
              const colorDelta = colorDeltaSum / count;
              return nonDark / count > 0.2 && stddev > 8 && colorDelta > 3;
            };
            const resolveWhenUseful = () => {
              window.requestAnimationFrame(() => {
                if (canvasLooksUseful()) {
                  resolveOnce();
                }
              });
            };
            player = new jsmpeg.Player(normalizeWsUrl(url), {
              canvas,
              autoplay: true,
              audio: false,
              loop: false,
              preserveDrawingBuffer: true,
              reconnectInterval: 0,
              onVideoDecode: resolveWhenUseful,
              onError: rejectOnce,
            });
          });
          await new Promise((resolve) => window.setTimeout(resolve, 250));
          return canvas.toDataURL("image/png");
        } finally {
          try {
            player?.destroy?.();
          } catch {
            // ignore
          }
        }
      },
      { url: absoluteMediaUrl(baseUrl, output.url), timeoutMs: BROWSER_CAPTURE_TIMEOUT_MS },
    );
  });
  await saveDataUrl(dataUrl, framePath);
  return { transport: "jsmpeg", output_id: output.output_id, path: framePath, stats: await imageStats(framePath) };
}

async function createContactSheet(captures, outputDir) {
  const ordered = ["rtsp", "hls", "webrtc", "mse", "jsmpeg"]
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

const CAPTURE_BY_TRANSPORT = {
  rtsp: captureRtspFrame,
  hls: captureHlsFrame,
  webrtc: captureWebRtcFrame,
  mse: captureMseFrame,
  jsmpeg: captureJsmpegFrame,
};

async function runTransportCaptures({ baseUrl, playback, outputDir, transports }) {
  const captures = [];
  for (const transport of transports) {
    const captureFn = CAPTURE_BY_TRANSPORT[transport];
    if (!captureFn) continue;
    try {
      const capture = await captureFn({ baseUrl, playback, outputDir });
      const visual = capture.stats ? frameLooksVisual(capture.stats) : true;
      captures.push({
        ok: visual,
        status: visual ? "ok" : "visual-invalid",
        ...capture,
      });
    } catch (error) {
      const blocked = error instanceof OptionalTransportUnavailable;
      captures.push({
        ok: false,
        status: blocked ? "blocked" : "failed",
        blocked,
        transport,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
  return captures;
}

async function run() {
  const options = parseArgs(process.argv.slice(2));
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outputDir = path.resolve(options.outDir, timestamp);
  await mkdir(outputDir, { recursive: true });

  const result = {
    ok: true,
    generated_at: new Date().toISOString(),
    base_url: options.baseUrl,
    matrix: options.matrix,
    transports: options.transports,
    output_dir: outputDir,
    captures: [],
  };

  if (options.matrix) {
    const liveViews = await fetchJson(`${options.baseUrl}/api/streams/camera-live-views`);
    result.live_views = [];
    for (const liveView of liveViews) {
      const viewResult = {
        id: liveView.id,
        name: liveView.name,
        variants: [],
      };
      for (const variant of liveView.variants || []) {
        if (variant.enabled === false) continue;
        const context = contextForVariant(variant);
        const caseDir = path.join(outputDir, slug(liveView.id, "live-view"), slug(variant.id, "variant"));
        await mkdir(caseDir, { recursive: true });
        const playback = await fetchJson(
          `${options.baseUrl}/api/streams/camera-live-views/${encodeURIComponent(liveView.id)}/playback?context=${encodeURIComponent(
            context,
          )}&variant_id=${encodeURIComponent(variant.id)}`,
        );
        const captures = await runTransportCaptures({
          baseUrl: options.baseUrl,
          playback,
          outputDir: caseDir,
          transports: options.transports,
        });
        const caseOk = captures.some((item) => item.ok) && captures.every((item) => item.status === "ok" || item.status === "blocked");
        if (!caseOk) result.ok = false;
        const contactSheetPath = await createContactSheet(captures, caseDir).catch((error) => {
          viewResult.contact_sheet_error = error instanceof Error ? error.message : String(error);
          return null;
        });
        viewResult.variants.push({
          id: variant.id,
          label: variant.label,
          role: variant.role,
          context,
          camera_source_id: playback.camera_source_id,
          transmission_id: playback.transmission?.id,
          output_dir: caseDir,
          contact_sheet_path: contactSheetPath,
          captures,
        });
      }
      result.live_views.push(viewResult);
    }
    result.captures = result.live_views.flatMap((view) =>
      view.variants.flatMap((variant) =>
        variant.captures.map((capture) => ({
          live_view_id: view.id,
          variant_id: variant.id,
          ...capture,
        })),
      ),
    );
  } else {
    const playback = await fetchJson(
      `${options.baseUrl}/api/streams/camera-live-views/${encodeURIComponent(options.liveViewId)}/playback?context=${encodeURIComponent(
        options.context,
      )}`,
    );
    result.live_view_id = options.liveViewId;
    result.context = options.context;
    result.camera_name = playback.camera_name;
    result.camera_source_name = playback.camera_source_name;
    result.transmission_id = playback.transmission?.id;
    result.captures = await runTransportCaptures({
      baseUrl: options.baseUrl,
      playback,
      outputDir,
      transports: options.transports,
    });
    result.ok =
      result.captures.some((item) => item.ok) &&
      result.captures.every((item) => item.status === "ok" || item.status === "blocked");
    result.contact_sheet_path = await createContactSheet(result.captures, outputDir).catch((error) => {
      result.contact_sheet_error = error instanceof Error ? error.message : String(error);
      return null;
    });
  }

  const reportPath = path.join(outputDir, "report.json");
  await writeFile(reportPath, `${JSON.stringify(result, null, 2)}\n`);
  const report = await readFile(reportPath, "utf8");
  process.stdout.write(report);
  process.exit(result.ok ? 0 : 1);
}

run().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exit(1);
});
