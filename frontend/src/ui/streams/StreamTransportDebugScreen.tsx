import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type Hls from "hls.js";

import {
  getStreamingCameraLiveViewPlayback,
  getStreamingTransmissionPlaybackPlan,
  getStreamingTransmissionUrls,
  heartbeatStreamingTransmissionDemand,
  listStreamingTransmissions,
  primeStreamingTransmissionDemand,
  type StreamingCameraLiveContext,
  type StreamingPlaybackPlanResponse,
  type StreamingPlaybackTransport,
  type StreamingQualityProfileId,
  type StreamingTransmission,
  type StreamingTransmissionUrlOutput,
  type StreamingTransmissionUrlsResponse,
} from "../../util/api";

type DebugTransport = StreamingPlaybackTransport;
type DebugSeverity = "debug" | "info" | "warn" | "error";
type DebugStatus = "idle" | "loading" | "playing" | "failed";

type DebugEvent = {
  id: number;
  at: number;
  severity: DebugSeverity;
  type: string;
  message: string;
  data?: Record<string, unknown>;
};

type BasicAuthCredentials = {
  username: string;
  password: string;
};

type SelectedDebugOutput = {
  transport: DebugTransport;
  outputId: string | null;
  qualityProfileId: StreamingQualityProfileId | null;
  url: string;
  auth: BasicAuthCredentials | null;
  mediaAuthType: "none" | "signed_url" | "basic";
  source: "urls" | "playback_plan";
};

type LoadedDebugData = {
  liveViewName: string | null;
  transmission: StreamingTransmission;
  urls: StreamingTransmissionUrlsResponse;
  playbackPlan: StreamingPlaybackPlanResponse | null;
  selected: SelectedDebugOutput | null;
};

const DEMAND_HEARTBEAT_MS = 10000;
const DEBUG_HLS_PROBE_TIMEOUT_MS = 4000;
const DEBUG_HLS_WARMUP_MS = 18000;
const DEBUG_HLS_WARMUP_RETRY_MS = 1200;
const WEBRTC_SIGNAL_TIMEOUT_MS = 6000;
const WEBRTC_CONNECT_TIMEOUT_MS = 7000;
const EVENT_LIMIT = 500;

const TRANSPORTS: DebugTransport[] = ["hls", "webrtc", "mse", "jsmpeg"];

function isDebugTransport(value: string): value is DebugTransport {
  return value === "hls" || value === "webrtc" || value === "mse" || value === "jsmpeg";
}

function normalizeTransport(value: string | null): DebugTransport {
  const normalized = String(value || "").trim().toLowerCase();
  return isDebugTransport(normalized) ? normalized : "hls";
}

function normalizeContext(value: string | null): StreamingCameraLiveContext {
  if (value === "pip" || value === "large" || value === "fullscreen" || value === "ptz") return value;
  return "thumbnail";
}

function normalizeOptionalText(value: string | null): string | null {
  const normalized = String(value || "").trim();
  return normalized ? normalized : null;
}

function asErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "unknown error");
}

function buildBasicAuthHeader(auth: BasicAuthCredentials | null): string | null {
  if (!auth) return null;
  try {
    return `Basic ${btoa(`${auth.username}:${auth.password}`)}`;
  } catch {
    return null;
  }
}

function withBasicAuthInUrl(url: string, auth: BasicAuthCredentials | null): string {
  if (!auth) return url;
  try {
    const parsed = new URL(url, window.location.href);
    parsed.username = auth.username;
    parsed.password = auth.password;
    return parsed.toString();
  } catch {
    return url;
  }
}

function resolveOutputBasicAuth(transmission: StreamingTransmission, outputId: string | null): BasicAuthCredentials | null {
  if (!outputId) return null;
  const outputs = Array.isArray(transmission.outputs) ? transmission.outputs : [];
  const output = outputs.find((item) => String(item?.id || "").trim() === outputId) ?? null;
  const auth = output?.authentication;
  if (!auth || auth.enabled !== true) return null;
  const username = String(auth.username || "").trim();
  const password = String(auth.password || "").trim();
  if (!username || !password) return null;
  return { username, password };
}

function canPlayNativeHls(video: HTMLVideoElement): boolean {
  const check = video.canPlayType("application/vnd.apple.mpegurl");
  return check === "probably" || check === "maybe";
}

function shouldUseNativeHls(video: HTMLVideoElement): boolean {
  if (!canPlayNativeHls(video)) return false;
  const userAgent = String(window.navigator?.userAgent || "");
  const isSafari =
    /\bSafari\//.test(userAgent) && !/\b(Chrome|Chromium|CriOS|FxiOS|Edg|OPR|Android)\b/.test(userAgent);
  const isIos = /\b(iPhone|iPad|iPod)\b/.test(userAgent);
  return isSafari || isIos;
}

function resolveRelativeUrl(baseUrl: string, rawUrl: string): string {
  return new URL(String(rawUrl || "").trim(), new URL(String(baseUrl || "").trim(), window.location.href).toString()).toString();
}

function playlistUris(playlistText: string): string[] {
  return String(playlistText || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
}

function playlistAttributeUris(playlistText: string): string[] {
  const out: string[] = [];
  const uriPattern = /URI="([^"]+)"/gi;
  for (const line of String(playlistText || "").split(/\r?\n/)) {
    uriPattern.lastIndex = 0;
    let match = uriPattern.exec(line);
    while (match) {
      out.push(match[1]);
      match = uriPattern.exec(line);
    }
  }
  return out;
}

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = DEBUG_HLS_PROBE_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), Math.max(500, timeoutMs));
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function probeHlsUrl(
  url: string,
  authHeader: string | null,
): Promise<{ mediaPlaylistUrl: string; tailSegmentUrl: string; mediaSequence: string | null; targetDuration: string | null }> {
  const headers: Record<string, string> = {
    accept: "application/vnd.apple.mpegurl, application/x-mpegurl, text/plain, */*",
  };
  if (authHeader) headers.authorization = authHeader;
  const masterResponse = await fetchWithTimeout(url, { cache: "no-store", headers });
  if (!masterResponse.ok) throw new Error(`HLS master playlist failed (${masterResponse.status}).`);
  const masterText = await masterResponse.text();
  if (!masterText.includes("#EXTM3U")) throw new Error("HLS master response is not a playlist.");
  const masterUris = playlistUris(masterText);
  const mediaPlaylistUrl =
    masterText.includes("#EXT-X-STREAM-INF") && masterUris.length ? resolveRelativeUrl(url, masterUris[0]) : url;
  const mediaText =
    mediaPlaylistUrl === url
      ? masterText
      : await (async () => {
          const mediaResponse = await fetchWithTimeout(mediaPlaylistUrl, { cache: "no-store", headers });
          if (!mediaResponse.ok) throw new Error(`HLS media playlist failed (${mediaResponse.status}).`);
          const text = await mediaResponse.text();
          if (!text.includes("#EXTM3U")) throw new Error("HLS media response is not a playlist.");
          return text;
        })();
  const tailCandidates = [...playlistUris(mediaText), ...playlistAttributeUris(mediaText)].filter((item) => !item.startsWith("data:"));
  const tailUri = tailCandidates[tailCandidates.length - 1];
  if (!tailUri) throw new Error("HLS media playlist has no segment/map/key URI.");
  const tailSegmentUrl = resolveRelativeUrl(mediaPlaylistUrl, tailUri);
  const tailHeaders: Record<string, string> = { accept: "*/*", range: "bytes=0-1" };
  if (authHeader) tailHeaders.authorization = authHeader;
  const tailResponse = await fetchWithTimeout(tailSegmentUrl, { cache: "no-store", headers: tailHeaders });
  if (!tailResponse.ok && tailResponse.status !== 206) throw new Error(`HLS tail probe failed (${tailResponse.status}).`);
  return {
    mediaPlaylistUrl,
    tailSegmentUrl,
    mediaSequence: mediaText.match(/#EXT-X-MEDIA-SEQUENCE:(\d+)/)?.[1] ?? null,
    targetDuration: mediaText.match(/#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)/)?.[1] ?? null,
  };
}

function waitForIceGatheringComplete(peerConnection: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (peerConnection.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      peerConnection.removeEventListener("icegatheringstatechange", onStateChange);
      reject(new Error("Timed out waiting for ICE gathering."));
    }, Math.max(500, timeoutMs));
    const onStateChange = () => {
      if (peerConnection.iceGatheringState !== "complete") return;
      window.clearTimeout(timeoutId);
      peerConnection.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    };
    peerConnection.addEventListener("icegatheringstatechange", onStateChange);
  });
}

function waitForPeerConnectionReady(peerConnection: RTCPeerConnection, timeoutMs: number): Promise<void> {
  const isConnected = () =>
    peerConnection.connectionState === "connected" ||
    peerConnection.iceConnectionState === "connected" ||
    peerConnection.iceConnectionState === "completed";
  const isFailed = () =>
    peerConnection.connectionState === "failed" ||
    peerConnection.connectionState === "disconnected" ||
    peerConnection.connectionState === "closed" ||
    peerConnection.iceConnectionState === "failed" ||
    peerConnection.iceConnectionState === "disconnected" ||
    peerConnection.iceConnectionState === "closed";
  if (isConnected()) return Promise.resolve();
  if (isFailed()) return Promise.reject(new Error("WebRTC connection failed."));
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      cleanup();
      reject(new Error("WebRTC connection timed out."));
    }, Math.max(1000, timeoutMs));
    const onStateChange = () => {
      if (isConnected()) {
        cleanup();
        resolve();
        return;
      }
      if (isFailed()) {
        cleanup();
        reject(new Error("WebRTC connection failed."));
      }
    };
    const cleanup = () => {
      window.clearTimeout(timeoutId);
      peerConnection.removeEventListener("connectionstatechange", onStateChange);
      peerConnection.removeEventListener("iceconnectionstatechange", onStateChange);
    };
    peerConnection.addEventListener("connectionstatechange", onStateChange);
    peerConnection.addEventListener("iceconnectionstatechange", onStateChange);
  });
}

function normalizeWebSocketUrl(rawUrl: string): string {
  const parsed = new URL(rawUrl, window.location.href);
  if (parsed.protocol === "http:") parsed.protocol = "ws:";
  else if (parsed.protocol === "https:") parsed.protocol = "wss:";
  return parsed.toString();
}

function mimeFromMseControlMessage(raw: string): string | null {
  const text = String(raw || "").trim();
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>;
    const explicitMime = String(parsed.mime || parsed.mimetype || "").trim();
    if (explicitMime) return explicitMime;
    const explicitCodecs = String(parsed.codecs || parsed.codec || "").trim();
    if (explicitCodecs) return `video/mp4; codecs="${explicitCodecs}"`;
    const tracks = Array.isArray(parsed.tracks) ? parsed.tracks : [];
    const codecs = tracks
      .map((track) => {
        if (!track || typeof track !== "object") return "";
        const record = track as Record<string, unknown>;
        return String(record.codec || record.codecs || "").trim();
      })
      .filter(Boolean);
    if (codecs.length) return `video/mp4; codecs="${codecs.join(",")}"`;
  } catch {
    // Some MSE sidecars send a plain codecs string as the first message.
  }
  if (text.includes("video/mp4")) return text;
  if (/(avc1|hvc1|hev1|mp4a)/i.test(text)) return `video/mp4; codecs="${text.replace(/^codecs=/i, "").replace(/^"|"$/g, "")}"`;
  return null;
}

function selectDebugOutput(
  transport: DebugTransport,
  transmission: StreamingTransmission,
  urls: StreamingTransmissionUrlsResponse,
  playbackPlan: StreamingPlaybackPlanResponse | null,
  requestedOutputId: string | null,
  requestedQualityProfileId: StreamingQualityProfileId | null,
): SelectedDebugOutput | null {
  const urlOutputs = Array.isArray(urls.outputs) ? urls.outputs : [];
  const matchingUrlOutputs = urlOutputs.filter((output) => output.protocol === transport);
  const orderedUrlOutputs = [
    ...matchingUrlOutputs.filter((output) => requestedOutputId && output.output_id === requestedOutputId),
    ...matchingUrlOutputs.filter(
      (output) =>
        !requestedOutputId &&
        requestedQualityProfileId &&
        output.quality_profile_id === requestedQualityProfileId,
    ),
    ...matchingUrlOutputs.filter((output) => !requestedOutputId && !requestedQualityProfileId),
    ...matchingUrlOutputs.filter(
      (output) =>
        output.output_id !== requestedOutputId &&
        (!requestedQualityProfileId || output.quality_profile_id !== requestedQualityProfileId),
    ),
  ];
  const seen = new Set<string>();
  for (const output of orderedUrlOutputs) {
    const url = String(output.url || "").trim();
    const outputId = String(output.output_id || "").trim();
    const key = `${outputId}:${url}`;
    if (!url || seen.has(key)) continue;
    seen.add(key);
    const mediaAuthType = output.media_auth_type ?? "none";
    return {
      transport,
      outputId: outputId || null,
      qualityProfileId: output.quality_profile_id ?? null,
      url,
      auth: output.requires_auth === true && mediaAuthType !== "signed_url" ? resolveOutputBasicAuth(transmission, outputId) : null,
      mediaAuthType,
      source: "urls",
    };
  }

  const planned = playbackPlan?.transports?.find((item) => item.transport === transport) ?? null;
  const plannedUrl = String(planned?.url || "").trim();
  if (plannedUrl) {
    const outputId = String(planned?.output_id || "").trim();
    const mediaAuthType = planned?.media_auth_type ?? "none";
    return {
      transport,
      outputId: outputId || null,
      qualityProfileId: planned?.quality_profile_id ?? null,
      url: plannedUrl,
      auth: planned?.requires_auth === true && mediaAuthType !== "signed_url" ? resolveOutputBasicAuth(transmission, outputId) : null,
      mediaAuthType,
      source: "playback_plan",
    };
  }

  return null;
}

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function shortUrl(value: string): string {
  try {
    const parsed = new URL(value, window.location.href);
    parsed.searchParams.sort();
    return parsed.toString();
  } catch {
    return value;
  }
}

export function StreamTransportDebugScreen(): JSX.Element {
  const query = useMemo(() => new URLSearchParams(window.location.search), []);
  const transport = useMemo(() => normalizeTransport(query.get("transport")), [query]);
  const transmissionId = useMemo(
    () => normalizeOptionalText(query.get("transmission_id") ?? query.get("transmission") ?? query.get("stream")),
    [query],
  );
  const liveViewId = useMemo(() => normalizeOptionalText(query.get("live_view_id") ?? query.get("live_view")), [query]);
  const outputId = useMemo(() => normalizeOptionalText(query.get("output_id")), [query]);
  const qualityProfileId = useMemo(
    () => normalizeOptionalText(query.get("quality_profile_id")) as StreamingQualityProfileId | null,
    [query],
  );
  const context = useMemo(() => normalizeContext(query.get("context")), [query]);
  const variantId = useMemo(() => normalizeOptionalText(query.get("variant_id") ?? query.get("variant")), [query]);

  const [status, setStatus] = useState<DebugStatus>("idle");
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [loaded, setLoaded] = useState<LoadedDebugData | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const eventIdRef = useRef(1);

  const appendEvent = useCallback((severity: DebugSeverity, type: string, message: string, data?: Record<string, unknown>) => {
    const event: DebugEvent = {
      id: eventIdRef.current,
      at: Date.now(),
      severity,
      type,
      message,
      data,
    };
    eventIdRef.current += 1;
    setEvents((previous) => [...previous, event].slice(-EVENT_LIMIT));
    const consoleMethod = severity === "error" ? "error" : severity === "warn" ? "warn" : severity === "debug" ? "debug" : "info";
    console[consoleMethod](`[stream-debug:${type}] ${message}`, data ?? {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load(): Promise<void> {
      setStatus("loading");
      setErrorText(null);
      setLoaded(null);
      appendEvent("info", "boot", "Transport debug screen started.", {
        transport,
        transmission_id: transmissionId,
        live_view_id: liveViewId,
        context,
        variant_id: variantId,
        output_id: outputId,
        quality_profile_id: qualityProfileId,
      });
      try {
        let transmission: StreamingTransmission | null = null;
        let urls: StreamingTransmissionUrlsResponse | null = null;
        let playbackPlan: StreamingPlaybackPlanResponse | null = null;
        let liveViewName: string | null = null;

        if (liveViewId) {
          appendEvent("info", "api.request", "Loading live-view playback contract.", { live_view_id: liveViewId, context, variant_id: variantId });
          const playback = await getStreamingCameraLiveViewPlayback(liveViewId, { context, variantId });
          if (cancelled) return;
          transmission = playback.transmission;
          urls = playback.urls;
          playbackPlan = playback.playback_plan ?? null;
          liveViewName = playback.live_view?.name || playback.camera_name || null;
          appendEvent("info", "api.response", "Live-view playback contract loaded.", {
            transmission_id: transmission.id,
            outputs: urls.outputs.length,
            selected_output_id: playback.selected_output?.output_id ?? null,
          });
        } else if (transmissionId) {
          appendEvent("info", "api.request", "Loading transmission list.", { transmission_id: transmissionId });
          const transmissions = await listStreamingTransmissions();
          if (cancelled) return;
          transmission = transmissions.find((item) => item.id === transmissionId) ?? null;
          if (!transmission) throw new Error(`Transmission not found: ${transmissionId}`);
          appendEvent("info", "api.response", "Transmission loaded.", { transmission_id: transmission.id, name: transmission.name });

          const urlOptions = { outputId, qualityProfileId };
          appendEvent("info", "api.request", "Loading transmission URLs.", urlOptions);
          urls = await getStreamingTransmissionUrls(transmission.id, urlOptions);
          if (cancelled) return;
          appendEvent("info", "api.response", "Transmission URLs loaded.", {
            outputs: urls.outputs.length,
            engine_running: urls.engine_running,
          });

          appendEvent("info", "api.request", "Loading playback plan for diagnostics.", {
            transport,
            output_id: outputId,
            quality_profile_id: qualityProfileId,
          });
          playbackPlan = await getStreamingTransmissionPlaybackPlan(transmission.id, {
            outputId,
            qualityProfileId,
            client: "web",
            context,
            lowLatency: transport === "webrtc",
          });
          if (cancelled) return;
          appendEvent("info", "api.response", "Playback plan loaded.", {
            selected_transport: playbackPlan.selected_transport ?? null,
            transports: playbackPlan.transports.map((item) => ({
              transport: item.transport,
              available: item.available,
              output_id: item.output_id ?? null,
            })),
          });
        } else {
          throw new Error("Missing transmission_id or live_view_id query parameter.");
        }

        if (!transmission || !urls) throw new Error("Playback contract did not return a transmission.");
        const selected = selectDebugOutput(transport, transmission, urls, playbackPlan, outputId, qualityProfileId);
        const plannedTransport = playbackPlan?.transports?.find((item) => item.transport === transport) ?? null;
        for (const message of plannedTransport?.warnings ?? []) {
          appendEvent("warn", "playback_plan.warning", message, { transport });
        }
        for (const message of plannedTransport?.blocking_errors ?? []) {
          appendEvent("error", "playback_plan.blocked", message, { transport });
        }
        if (!selected) {
          setLoaded({ liveViewName, transmission, urls, playbackPlan, selected: null });
          const message = plannedTransport?.blocking_errors?.[0] || `No ${transport.toUpperCase()} browser URL is available for this transmission.`;
          throw new Error(message);
        }
        appendEvent("info", "transport.selected", "Fixed transport selected.", {
          transport,
          output_id: selected.outputId,
          quality_profile_id: selected.qualityProfileId,
          source: selected.source,
          media_auth_type: selected.mediaAuthType,
          url: shortUrl(selected.url),
        });
        setLoaded({ liveViewName, transmission, urls, playbackPlan, selected });
      } catch (error) {
        if (cancelled) return;
        const message = asErrorMessage(error);
        setStatus("failed");
        setErrorText(message);
        appendEvent("error", "load.failed", message);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [appendEvent, context, liveViewId, outputId, qualityProfileId, transmissionId, transport, variantId]);

  useEffect(() => {
    const selected = loaded?.selected ?? null;
    const transmission = loaded?.transmission ?? null;
    if (!selected || !transmission) return;
    if (transport !== "hls" && transport !== "webrtc") {
      appendEvent("info", "demand.skipped", "Demand heartbeat is currently only sent for HLS/WebRTC transports.", { transport });
      return;
    }
    if (!selected.outputId) {
      appendEvent("warn", "demand.skipped", "Cannot send demand heartbeat without an output id.");
      return;
    }
    const currentSelected = selected;
    const currentTransmission = transmission;
    let cancelled = false;
    const playbackSessionId =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `stream-debug-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const request = {
      playbackSessionId,
      outputId: currentSelected.outputId,
      qualityProfileId: currentSelected.qualityProfileId,
      transport,
      source: "player" as const,
      ttlSeconds: 45,
    };
    async function renew(reason: string): Promise<void> {
      try {
        appendEvent("debug", "demand.heartbeat.request", "Renewing demand heartbeat.", { reason, playback_session_id: playbackSessionId });
        const response = await heartbeatStreamingTransmissionDemand(currentTransmission.id, request);
        if (cancelled) return;
        appendEvent("debug", "demand.heartbeat.response", "Demand heartbeat renewed.", {
          renewed: response.renewed,
          renewed_outputs: response.renewed_outputs,
          lease_seconds: response.lease_seconds,
        });
      } catch (error) {
        if (cancelled) return;
        appendEvent("warn", "demand.heartbeat.failed", asErrorMessage(error));
      }
    }
    async function prime(): Promise<void> {
      try {
        appendEvent("info", "demand.prime.request", "Priming transmission demand.", {
          output_id: currentSelected.outputId,
          quality_profile_id: currentSelected.qualityProfileId,
        });
        const response = await primeStreamingTransmissionDemand(currentTransmission.id, {
          outputId: currentSelected.outputId,
          qualityProfileId: currentSelected.qualityProfileId,
        });
        if (cancelled) return;
        appendEvent("info", "demand.prime.response", "Transmission demand primed.", {
          primed: response.primed,
          primed_outputs: response.primed_outputs,
        });
      } catch (error) {
        if (cancelled) return;
        appendEvent("warn", "demand.prime.failed", asErrorMessage(error));
      }
    }
    void prime().then(() => renew("initial"));
    const intervalId = window.setInterval(() => void renew("interval"), DEMAND_HEARTBEAT_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [appendEvent, loaded, transport]);

  useEffect(() => {
    const selected = loaded?.selected ?? null;
    const video = videoRef.current;
    const canvas = canvasRef.current;
    const transmission = loaded?.transmission ?? null;
    if (!selected || !video || !transmission) return;
    const selectedOutput = selected;
    const videoElement = video;
    const canvasElement = canvas;
    const currentTransmission = transmission;
    let cancelled = false;
    let hls: Hls | null = null;
    let peerConnection: RTCPeerConnection | null = null;
    let whepSessionUrl: string | null = null;
    let mediaSource: MediaSource | null = null;
    let mseSocket: WebSocket | null = null;
    let mseObjectUrl: string | null = null;
    let jsmpegPlayer: { destroy?: () => void } | null = null;
    let hlsMediaRecoveries = 0;

    const logVideoEvent = (event: Event) => {
      const target = event.currentTarget as HTMLVideoElement;
      appendEvent(event.type === "error" ? "error" : "debug", `video.${event.type}`, `Video event: ${event.type}`, {
        ready_state: target.readyState,
        network_state: target.networkState,
        current_time: Number(target.currentTime.toFixed(3)),
        paused: target.paused,
        ended: target.ended,
        error: target.error ? { code: target.error.code, message: target.error.message } : null,
      });
      if (event.type === "error") {
        const message = target.error?.message || `Video element failed with code ${target.error?.code ?? "unknown"}.`;
        if (transport === "hls" && hls && hlsMediaRecoveries < 2) {
          hlsMediaRecoveries += 1;
          appendEvent("warn", "hls.media_recover", "Video element failed; recovering HLS media on the same transport.", {
            recovery: hlsMediaRecoveries,
            error: message,
          });
          setStatus("loading");
          setErrorText(null);
          try {
            hls.recoverMediaError();
          } catch (error) {
            appendEvent("error", "hls.media_recover.failed", asErrorMessage(error));
            setStatus("failed");
            setErrorText(message);
          }
          return;
        }
        setStatus("failed");
        setErrorText(message);
      }
    };
    const videoEvents = ["loadstart", "loadedmetadata", "loadeddata", "canplay", "playing", "waiting", "stalled", "pause", "ended", "error"];
    videoEvents.forEach((eventName) => videoElement.addEventListener(eventName, logVideoEvent));
    let lastTimeupdate = 0;
    const onTimeupdate = () => {
      const now = Date.now();
      if (now - lastTimeupdate < 3000) return;
      lastTimeupdate = now;
      appendEvent("debug", "video.timeupdate", "Video playback advanced.", {
        current_time: Number(videoElement.currentTime.toFixed(3)),
        ready_state: videoElement.readyState,
      });
    };
    videoElement.addEventListener("timeupdate", onTimeupdate);

    const cleanupPlayback = () => {
      hls?.destroy();
      hls = null;
      if (whepSessionUrl) {
        fetch(whepSessionUrl, { method: "DELETE" }).catch(() => undefined);
      }
      whepSessionUrl = null;
      peerConnection?.close();
      peerConnection = null;
      mseSocket?.close();
      mseSocket = null;
      if (mseObjectUrl) URL.revokeObjectURL(mseObjectUrl);
      mseObjectUrl = null;
      mediaSource = null;
      jsmpegPlayer?.destroy?.();
      jsmpegPlayer = null;
      videoElement.srcObject = null;
      videoElement.removeAttribute("src");
      videoElement.load();
    };

    async function primeDemandForPlayback(): Promise<void> {
      if (transport !== "hls" && transport !== "webrtc") return;
      if (!selectedOutput.outputId) return;
      try {
        appendEvent("info", "demand.preflight_prime.request", "Priming demand before starting playback.", {
          output_id: selectedOutput.outputId,
          quality_profile_id: selectedOutput.qualityProfileId,
        });
        const response = await primeStreamingTransmissionDemand(currentTransmission.id, {
          outputId: selectedOutput.outputId,
          qualityProfileId: selectedOutput.qualityProfileId,
        });
        appendEvent("info", "demand.preflight_prime.response", "Preflight demand prime completed.", {
          primed: response.primed,
          primed_outputs: response.primed_outputs,
        });
      } catch (error) {
        appendEvent("warn", "demand.preflight_prime.failed", asErrorMessage(error));
      }
    }

    async function probeHlsWithWarmup(url: string, authHeader: string | null): Promise<Awaited<ReturnType<typeof probeHlsUrl>>> {
      const deadline = Date.now() + DEBUG_HLS_WARMUP_MS;
      let attempt = 1;
      let lastError: unknown = null;
      while (!cancelled) {
        try {
          appendEvent("info", "hls.probe.start", "Probing HLS playlist and tail segment.", {
            url: shortUrl(url),
            attempt,
          });
          return await probeHlsUrl(url, authHeader);
        } catch (error) {
          lastError = error;
          const message = asErrorMessage(error);
          const retryable = !message.includes("(401)") && !message.includes("(403)") && Date.now() < deadline;
          if (!retryable) throw error;
          appendEvent("warn", "hls.probe.retry", "HLS probe failed during warmup; retrying same transport.", {
            attempt,
            error: message,
            retry_ms: DEBUG_HLS_WARMUP_RETRY_MS,
          });
          await new Promise((resolve) => window.setTimeout(resolve, DEBUG_HLS_WARMUP_RETRY_MS));
          attempt += 1;
        }
      }
      throw lastError instanceof Error ? lastError : new Error("HLS probe cancelled.");
    }

    async function startHls(): Promise<void> {
      const authHeader = buildBasicAuthHeader(selectedOutput.auth);
      const url = selectedOutput.mediaAuthType === "basic" ? withBasicAuthInUrl(selectedOutput.url, selectedOutput.auth) : selectedOutput.url;
      const probe = await probeHlsWithWarmup(url, authHeader);
      appendEvent("info", "hls.probe.ok", "HLS probe succeeded.", {
        media_playlist_url: shortUrl(probe.mediaPlaylistUrl),
        tail_segment_url: shortUrl(probe.tailSegmentUrl),
        media_sequence: probe.mediaSequence,
        target_duration: probe.targetDuration,
      });
      if (shouldUseNativeHls(videoElement)) {
        appendEvent("info", "hls.native.start", "Starting native HLS playback.");
        videoElement.src = url;
        await videoElement.play();
        return;
      }
      if (canPlayNativeHls(videoElement)) {
        appendEvent("info", "hls.native.skipped", "Native HLS is available, but diagnostics prefer hls.js outside Safari/iOS.");
      }
      appendEvent("info", "hls.js.load", "Loading hls.js for fixed HLS playback.");
      const hlsModule = await import("hls.js");
      const HlsConstructor = hlsModule.default;
      if (!HlsConstructor.isSupported()) throw new Error("This browser does not support native HLS or hls.js MediaSource playback.");
      hls = new HlsConstructor({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        xhrSetup: (xhr) => {
          if (authHeader) xhr.setRequestHeader("Authorization", authHeader);
        },
        fetchSetup: (context: { url: string }, initParams: RequestInit) => {
          const requestUrl = String(context?.url || "");
          if (!authHeader) return new Request(requestUrl, initParams);
          const headers = new Headers(initParams?.headers || {});
          headers.set("Authorization", authHeader);
          return new Request(requestUrl, { ...initParams, headers });
        },
      });
      hls.on(HlsConstructor.Events.MEDIA_ATTACHED, () => appendEvent("debug", "hls.media_attached", "hls.js media attached."));
      hls.on(HlsConstructor.Events.MANIFEST_PARSED, (_event, data) => {
        appendEvent("info", "hls.manifest_parsed", "hls.js manifest parsed.", {
          levels: Array.isArray(data.levels) ? data.levels.length : null,
        });
      });
      hls.on(HlsConstructor.Events.LEVEL_LOADED, (_event, data) => {
        appendEvent("debug", "hls.level_loaded", "hls.js level loaded.", {
          live: data.details?.live ?? null,
          media_sequence: data.details?.startSN ?? null,
          target_duration: data.details?.targetduration ?? null,
        });
      });
      hls.on(HlsConstructor.Events.FRAG_LOADED, (_event, data) => {
        appendEvent("debug", "hls.frag_loaded", "hls.js fragment loaded.", {
          sn: data.frag?.sn ?? null,
          duration: data.frag?.duration ?? null,
        });
      });
      hls.on(HlsConstructor.Events.ERROR, (_event, data) => {
        appendEvent(data.fatal ? "error" : "warn", "hls.error", data.details || "hls.js error", {
          type: data.type,
          details: data.details,
          fatal: data.fatal,
          response: data.response ? { code: data.response.code, text: data.response.text } : null,
        });
        if (data.fatal) {
          if (data.type === "mediaError" && hls && hlsMediaRecoveries < 2) {
            hlsMediaRecoveries += 1;
            appendEvent("warn", "hls.media_recover", "hls.js fatal media error; recovering on the same transport.", {
              recovery: hlsMediaRecoveries,
              details: data.details,
            });
            setStatus("loading");
            setErrorText(null);
            hls.recoverMediaError();
            return;
          }
          setStatus("failed");
          setErrorText(data.details || "Fatal hls.js error.");
        }
      });
      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const settleOk = () => {
          if (settled) return;
          settled = true;
          cleanupListeners();
          resolve();
        };
        const settleError = () => {
          if (settled) return;
          settled = true;
          cleanupListeners();
          reject(new Error(videoElement.error?.message || `Video element failed with code ${videoElement.error?.code ?? "unknown"}.`));
        };
        const cleanupListeners = () => {
          videoElement.removeEventListener("playing", settleOk);
          videoElement.removeEventListener("loadeddata", settleOk);
          videoElement.removeEventListener("error", settleError);
        };
        videoElement.addEventListener("playing", settleOk);
        videoElement.addEventListener("loadeddata", settleOk);
        videoElement.addEventListener("error", settleError);
        hls?.once(HlsConstructor.Events.MANIFEST_PARSED, () => {
          appendEvent("debug", "hls.play.request", "Starting video playback after manifest parse.");
          void videoElement.play().catch((error) => {
            appendEvent("warn", "hls.play.failed", asErrorMessage(error));
          });
        });
        hls?.once(HlsConstructor.Events.MEDIA_ATTACHED, () => {
          hls?.loadSource(url);
        });
        hls?.attachMedia(videoElement);
      });
    }

    async function startWebRtc(): Promise<void> {
      if (typeof RTCPeerConnection === "undefined") throw new Error("RTCPeerConnection is unavailable in this browser.");
      appendEvent("info", "webrtc.start", "Starting fixed WebRTC/WHEP playback.", { url: shortUrl(selectedOutput.url) });
      const authHeader = buildBasicAuthHeader(selectedOutput.auth);
      const pc = new RTCPeerConnection();
      peerConnection = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.addEventListener("icegatheringstatechange", () =>
        appendEvent("debug", "webrtc.ice_gathering_state", "ICE gathering state changed.", { state: pc.iceGatheringState }),
      );
      pc.addEventListener("iceconnectionstatechange", () =>
        appendEvent("info", "webrtc.ice_connection_state", "ICE connection state changed.", { state: pc.iceConnectionState }),
      );
      pc.addEventListener("connectionstatechange", () =>
        appendEvent("info", "webrtc.connection_state", "Peer connection state changed.", { state: pc.connectionState }),
      );
      pc.addEventListener("signalingstatechange", () =>
        appendEvent("debug", "webrtc.signaling_state", "Signaling state changed.", { state: pc.signalingState }),
      );
      pc.addEventListener("track", (event) => {
        appendEvent("info", "webrtc.track", "Remote track received.", {
          kind: event.track.kind,
          id: event.track.id,
          streams: event.streams.map((stream) => stream.id),
        });
        const stream = event.streams[0] ?? new MediaStream([event.track]);
        if (videoElement.srcObject !== stream) videoElement.srcObject = stream;
      });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIceGatheringComplete(pc, WEBRTC_SIGNAL_TIMEOUT_MS);
      const localDescription = pc.localDescription?.sdp;
      if (!localDescription) throw new Error("WebRTC local SDP offer is empty.");
      appendEvent("info", "webrtc.offer", "Posting WHEP offer.", { sdp_length: localDescription.length });
      const headers: Record<string, string> = { "content-type": "application/sdp", accept: "application/sdp" };
      if (authHeader) headers.authorization = authHeader;
      const response = await fetch(selectedOutput.mediaAuthType === "basic" ? withBasicAuthInUrl(selectedOutput.url, selectedOutput.auth) : selectedOutput.url, {
        method: "POST",
        headers,
        body: localDescription,
      });
      appendEvent(response.ok ? "info" : "error", "webrtc.answer.response", "WHEP answer response received.", {
        status: response.status,
        location: response.headers.get("location"),
      });
      if (!response.ok) throw new Error(`WHEP negotiation failed (${response.status}).`);
      const sessionLocation = response.headers.get("location");
      whepSessionUrl = sessionLocation ? new URL(sessionLocation, selectedOutput.url).toString() : null;
      const answer = await response.text();
      if (!answer.trim()) throw new Error("WHEP answer is empty.");
      await pc.setRemoteDescription({ type: "answer", sdp: answer });
      await waitForPeerConnectionReady(pc, WEBRTC_CONNECT_TIMEOUT_MS);
      await videoElement.play();
    }

    async function startMse(): Promise<void> {
      if (typeof MediaSource === "undefined") throw new Error("MediaSource is unavailable in this browser.");
      const wsUrl = normalizeWebSocketUrl(selectedOutput.url);
      appendEvent("info", "mse.start", "Starting generic WebSocket MSE playback.", { url: shortUrl(wsUrl) });
      mediaSource = new MediaSource();
      mseObjectUrl = URL.createObjectURL(mediaSource);
      videoElement.src = mseObjectUrl;
      await new Promise<void>((resolve, reject) => {
        if (!mediaSource) {
          reject(new Error("MediaSource was not created."));
          return;
        }
        let sourceBuffer: SourceBuffer | null = null;
        const queue: ArrayBuffer[] = [];
        const flush = () => {
          if (!sourceBuffer || sourceBuffer.updating || queue.length === 0) return;
          const next = queue.shift();
          if (!next) return;
          try {
            sourceBuffer.appendBuffer(next);
          } catch (error) {
            reject(error);
          }
        };
        const onSourceOpen = () => {
          if (!mediaSource) return;
          appendEvent("info", "mse.source_open", "MediaSource opened.");
          mseSocket = new WebSocket(wsUrl);
          mseSocket.binaryType = "arraybuffer";
          mseSocket.addEventListener("open", () => appendEvent("info", "mse.websocket.open", "MSE WebSocket opened."));
          mseSocket.addEventListener("close", (event) =>
            appendEvent(event.wasClean ? "info" : "warn", "mse.websocket.close", "MSE WebSocket closed.", {
              code: event.code,
              reason: event.reason,
              was_clean: event.wasClean,
            }),
          );
          mseSocket.addEventListener("error", () => {
            appendEvent("error", "mse.websocket.error", "MSE WebSocket error.");
            reject(new Error("MSE WebSocket failed."));
          });
          mseSocket.addEventListener("message", (event) => {
            if (typeof event.data === "string") {
              appendEvent("debug", "mse.websocket.text", "MSE text control message received.", { message: event.data.slice(0, 500) });
              if (!sourceBuffer) {
                const mime = mimeFromMseControlMessage(event.data);
                if (!mime) return;
                if (!MediaSource.isTypeSupported(mime)) {
                  reject(new Error(`Browser does not support MSE mime type: ${mime}`));
                  return;
                }
                sourceBuffer = mediaSource?.addSourceBuffer(mime) ?? null;
                sourceBuffer?.addEventListener("updateend", flush);
                sourceBuffer?.addEventListener("error", () => reject(new Error("MSE SourceBuffer error.")));
                appendEvent("info", "mse.source_buffer", "MSE SourceBuffer created.", { mime });
                void videoElement.play().catch((error) => appendEvent("warn", "mse.video.play_failed", asErrorMessage(error)));
                resolve();
              }
              return;
            }
            if (!(event.data instanceof ArrayBuffer)) {
              appendEvent("warn", "mse.websocket.unknown", "MSE message is not text or ArrayBuffer.");
              return;
            }
            appendEvent("debug", "mse.websocket.binary", "MSE binary media fragment received.", { bytes: event.data.byteLength });
            queue.push(event.data);
            flush();
          });
        };
        mediaSource.addEventListener("sourceopen", onSourceOpen, { once: true });
      });
    }

    async function startJsmpeg(): Promise<void> {
      if (!canvasElement) throw new Error("JSMpeg canvas is unavailable.");
      const globalWindow = window as unknown as { JSMpeg?: { Player?: new (url: string, options: Record<string, unknown>) => { destroy?: () => void } } };
      const Player = globalWindow.JSMpeg?.Player;
      if (!Player) {
        const wsUrl = normalizeWebSocketUrl(selectedOutput.url);
        appendEvent("warn", "jsmpeg.decoder_missing", "JSMpeg decoder is not bundled; opening WebSocket for transport diagnostics only.", {
          url: shortUrl(wsUrl),
        });
        const socket = new WebSocket(wsUrl);
        socket.binaryType = "arraybuffer";
        mseSocket = socket;
        socket.addEventListener("open", () => appendEvent("info", "jsmpeg.websocket.open", "JSMpeg WebSocket opened."));
        socket.addEventListener("message", (event) => {
          appendEvent("debug", "jsmpeg.websocket.message", "JSMpeg WebSocket message received.", {
            bytes: event.data instanceof ArrayBuffer ? event.data.byteLength : String(event.data || "").length,
          });
        });
        socket.addEventListener("close", (event) =>
          appendEvent(event.wasClean ? "info" : "warn", "jsmpeg.websocket.close", "JSMpeg WebSocket closed.", {
            code: event.code,
            reason: event.reason,
            was_clean: event.wasClean,
          }),
        );
        socket.addEventListener("error", () => appendEvent("error", "jsmpeg.websocket.error", "JSMpeg WebSocket error."));
        throw new Error("JSMpeg decoder is not bundled in the frontend.");
      }
      appendEvent("info", "jsmpeg.start", "Starting JSMpeg canvas playback.", { url: shortUrl(selectedOutput.url) });
      jsmpegPlayer = new Player(selectedOutput.url, {
        canvas: canvasElement,
        autoplay: true,
        audio: false,
        loop: false,
        onVideoDecode: () => appendEvent("debug", "jsmpeg.video_decode", "JSMpeg decoded a video frame."),
      });
    }

    async function start(): Promise<void> {
      try {
        setStatus("loading");
        setErrorText(null);
        await primeDemandForPlayback();
        if (cancelled) return;
        if (transport === "hls") await startHls();
        else if (transport === "webrtc") await startWebRtc();
        else if (transport === "mse") await startMse();
        else await startJsmpeg();
        if (cancelled) return;
        setStatus("playing");
        appendEvent("info", "playback.started", "Fixed transport playback started.", { transport });
      } catch (error) {
        if (cancelled) return;
        const message = asErrorMessage(error);
        setStatus("failed");
        setErrorText(message);
        appendEvent("error", "playback.failed", message, { transport });
      }
    }

    void start();
    return () => {
      cancelled = true;
      videoEvents.forEach((eventName) => videoElement.removeEventListener(eventName, logVideoEvent));
      videoElement.removeEventListener("timeupdate", onTimeupdate);
      cleanupPlayback();
    };
  }, [appendEvent, loaded, transport]);

  const selected = loaded?.selected ?? null;
  const transmission = loaded?.transmission ?? null;
  const planTransport = loaded?.playbackPlan?.transports?.find((item) => item.transport === transport) ?? null;
  const availableTransports = loaded?.playbackPlan?.transports ?? [];

  return (
    <div className="streamTransportDebugRoot">
      <header className="streamTransportDebugHeader">
        <div className="streamTransportDebugTitleGroup">
          <div className="streamTransportDebugEyebrow">Diagnóstico de transporte fixo</div>
          <h1 className="streamTransportDebugTitle">{transport.toUpperCase()}</h1>
          <div className="streamTransportDebugMeta">
            <span>Status: {status}</span>
            {transmission ? <span>Transmissão: {transmission.name || transmission.id}</span> : null}
            {loaded?.liveViewName ? <span>Live view: {loaded.liveViewName}</span> : null}
            {selected?.outputId ? <span>Output: {selected.outputId}</span> : null}
          </div>
        </div>
        <div className="streamTransportDebugSwitch">
          {TRANSPORTS.map((item) => {
            const params = new URLSearchParams(window.location.search);
            params.set("transport", item);
            return (
              <a
                key={item}
                className={`streamTransportDebugTransportLink ${item === transport ? "isActive" : ""}`}
                href={`/streams/debug?${params.toString()}`}
              >
                {item.toUpperCase()}
              </a>
            );
          })}
        </div>
      </header>

      <main className="streamTransportDebugBody">
        <section className="streamTransportDebugStage">
          {transport === "jsmpeg" ? <canvas ref={canvasRef} className="streamTransportDebugCanvas" /> : null}
          <video
            ref={videoRef}
            className={`streamTransportDebugVideo ${transport === "jsmpeg" ? "isHidden" : ""}`}
            controls
            playsInline
            muted
            autoPlay
          />
          {errorText ? (
            <div className="streamTransportDebugOverlay isError">
              <div className="streamTransportDebugOverlayTitle">Falhou neste transporte</div>
              <div className="streamTransportDebugOverlayText">{errorText}</div>
            </div>
          ) : status === "loading" ? (
            <div className="streamTransportDebugOverlay">
              <div className="streamTransportDebugOverlayTitle">Carregando {transport.toUpperCase()}</div>
              <div className="streamTransportDebugOverlayText">Sem fallback automático.</div>
            </div>
          ) : null}
        </section>

        <aside className="streamTransportDebugSidePanel">
          <section className="streamTransportDebugInfoBlock">
            <h2>Contrato</h2>
            <dl>
              <div>
                <dt>Transporte fixo</dt>
                <dd>{transport.toUpperCase()}</dd>
              </div>
              <div>
                <dt>Disponível no plano</dt>
                <dd>{planTransport ? (planTransport.available ? "sim" : "não") : "-"}</dd>
              </div>
              <div>
                <dt>Origem da URL</dt>
                <dd>{selected?.source ?? "-"}</dd>
              </div>
              <div>
                <dt>URL</dt>
                <dd title={selected?.url ?? ""}>{selected ? shortUrl(selected.url) : "-"}</dd>
              </div>
              <div>
                <dt>Media auth</dt>
                <dd>{selected?.mediaAuthType ?? "-"}</dd>
              </div>
            </dl>
          </section>

          <section className="streamTransportDebugInfoBlock">
            <h2>Plano recebido</h2>
            {availableTransports.length ? (
              <div className="streamTransportDebugPlanList">
                {availableTransports.map((item) => (
                  <div key={item.transport} className="streamTransportDebugPlanItem">
                    <span>{item.transport.toUpperCase()}</span>
                    <span>{item.available ? "available" : "blocked"}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="streamTransportDebugEmpty">Ainda não carregado.</div>
            )}
          </section>
        </aside>
      </main>

      <section className="streamTransportDebugConsole" aria-label="Console de eventos do transporte">
        <div className="streamTransportDebugConsoleHeader">
          <h2>Console</h2>
          <span>{events.length} eventos</span>
        </div>
        <div className="streamTransportDebugConsoleBody">
          {events.map((event) => (
            <div key={event.id} className={`streamTransportDebugLogLine is-${event.severity}`}>
              <span className="streamTransportDebugLogTime">{formatTime(event.at)}</span>
              <span className="streamTransportDebugLogSeverity">{event.severity}</span>
              <span className="streamTransportDebugLogType">{event.type}</span>
              <span className="streamTransportDebugLogMessage">{event.message}</span>
              {event.data ? <code>{JSON.stringify(event.data)}</code> : null}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
