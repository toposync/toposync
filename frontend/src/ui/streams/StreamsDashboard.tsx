import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type Hls from "hls.js";

import {
  getStreamingTransmissionUrls,
  getStreamingRuntimeHealth,
  heartbeatStreamingTransmissionDemand,
  listStreamingTransmissions,
  postStreamingPlaybackEvents,
  primeStreamingTransmissionDemand,
  type StreamingQualityProfileId,
  type StreamingRuntimeTransmissionHealth,
  type StreamingTransmission,
  type StreamingTransmissionUrlOutput,
  type StreamingTransmissionUrlsResponse,
} from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
import { Modal } from "../Modal";
import { StreamsPtzOverlay } from "./StreamsPtzOverlay";

type GridMode = "1x1" | "2x2";

type Props = {
  uiVisible: boolean;
  isActive: boolean;
};

type TilePlaybackStatus = "idle" | "loading" | "playing" | "error" | "unsupported";
type TileHealthTone = "muted" | "warn" | "error";
type TilePlaybackTransport = "none" | "webrtc" | "hls";
type StreamProtocol = "hls" | "rtsp" | "webrtc";
type StreamQualityPreference = "auto" | "low" | "stable" | "high" | "diagnostic";
type StreamTransportPreference = "auto" | "webrtc" | "hls";
type EffectiveTransportMode = "auto_hls" | "auto_webrtc" | "ptz_webrtc" | "hls" | "webrtc" | "hls_fallback" | "auto";
type TranslateFn = ReturnType<typeof i18n.useI18n>["t"];

type StreamPlaybackPlan = {
  allowHls: boolean;
  allowWebRtc: boolean;
  preferWebRtcFirst: boolean;
  effectiveMode: EffectiveTransportMode;
  webRtcBlocked: boolean;
  webRtcIssueMessages: string[];
  homeAssistantProxyHls: boolean;
  mobileTouchBrowser: boolean;
};

type BasicAuthCredentials = {
  username: string;
  password: string;
};

type PlaybackOutputSelection = {
  outputId: string;
  url: string;
  auth: BasicAuthCredentials | null;
  mediaAuthType: "none" | "signed_url" | "basic";
  urlExpiresAtUnix: number | null;
  renewAfterUnix: number | null;
  qualityProfileId: StreamingQualityProfileId | null;
};

type WebRtcStatsSummary = {
  iceConnectionState: string;
  connectionState: string;
  rttMs: number | null;
  packetLossPct: number | null;
  packetsLost: number | null;
  jitterMs: number | null;
  framesDecoded: number | null;
  framesPerSecond: number | null;
};

const GRID_MODE_STORAGE_KEY = "toposync.streams.grid_mode.v1";
const QUALITY_PREFERENCE_STORAGE_KEY = "toposync.streams.quality_preference.v1";
const TRANSPORT_PREFERENCE_STORAGE_KEY = "toposync.streams.transport_preference.v1";
const TRANSMISSIONS_REFRESH_MS = 15000;
const RETRY_BASE_MS = 900;
const RETRY_MAX_MS = 8000;
const WEBRTC_SIGNAL_TIMEOUT_MS = 5000;
const WEBRTC_CONNECT_TIMEOUT_MS = 5000;
const WEBRTC_WHEP_READY_ATTEMPTS = 8;
const WEBRTC_WHEP_READY_RETRY_MS = 500;
const RUNTIME_HEALTH_REFRESH_MS = 2000;
const HLS_BROWSER_PROBE_TIMEOUT_MS = 2500;
const DEMAND_HEARTBEAT_INTERVAL_MS = 10000;

function readGridMode(): GridMode {
  if (typeof window === "undefined") return "2x2";
  try {
    const saved = String(localStorage.getItem(GRID_MODE_STORAGE_KEY) || "").trim();
    return saved === "1x1" ? "1x1" : "2x2";
  } catch {
    return "2x2";
  }
}

function readQualityPreferenceByTransmissionId(): Record<string, StreamQualityPreference> {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(String(localStorage.getItem(QUALITY_PREFERENCE_STORAGE_KEY) || "{}"));
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, StreamQualityPreference> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (
        value === "auto" ||
        value === "low" ||
        value === "stable" ||
        value === "high" ||
        value === "diagnostic"
      ) {
        out[String(key)] = value;
      }
    }
    return out;
  } catch {
    return {};
  }
}

function readTransportPreferenceByTransmissionId(): Record<string, StreamTransportPreference> {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(String(localStorage.getItem(TRANSPORT_PREFERENCE_STORAGE_KEY) || "{}"));
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, StreamTransportPreference> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (value === "auto" || value === "webrtc" || value === "hls") out[String(key)] = value;
    }
    return out;
  } catch {
    return {};
  }
}

function qualityProfileIdForPreference(
  preference: StreamQualityPreference,
  gridMode: GridMode,
): StreamingQualityProfileId {
  if (preference === "low") return "quad_grid";
  if (preference === "stable") return "stable_apple_tv";
  if (preference === "high") return "fullscreen_quality";
  if (preference === "diagnostic") return "diagnostic_low";
  return gridMode === "2x2" ? "quad_grid" : "stable_apple_tv";
}

function qualityPreferenceLabel(preference: StreamQualityPreference, t: TranslateFn): string {
  if (preference === "low") return t("core.ui.streams.quality.low", {}, "Low");
  if (preference === "stable") return t("core.ui.streams.quality.stable", {}, "Stable");
  if (preference === "high") return t("core.ui.streams.quality.high", {}, "High");
  if (preference === "diagnostic") return t("core.ui.streams.quality.diagnostic", {}, "Diagnostic");
  return t("core.ui.streams.quality.auto", {}, "Auto");
}

function transportPreferenceLabel(preference: StreamTransportPreference, t: TranslateFn): string {
  if (preference === "webrtc") return t("core.ui.streams.transport.low_latency", {}, "Low latency");
  if (preference === "hls") return t("core.ui.streams.transport.hls", {}, "HLS");
  return t("core.ui.streams.transport.auto", {}, "Auto");
}

function effectiveTransportModeLabel(mode: EffectiveTransportMode, t: TranslateFn): string {
  if (mode === "auto_hls") return t("core.ui.streams.transport.effective_auto_hls", {}, "Auto -> HLS");
  if (mode === "auto_webrtc") return t("core.ui.streams.transport.effective_auto_webrtc", {}, "Auto -> WebRTC");
  if (mode === "ptz_webrtc") return t("core.ui.streams.transport.effective_ptz_webrtc", {}, "PTZ -> WebRTC");
  if (mode === "hls") return t("core.ui.streams.transport.hls", {}, "HLS");
  if (mode === "webrtc") return t("core.ui.streams.transport.low_latency", {}, "Low latency");
  if (mode === "hls_fallback") return t("core.ui.streams.transport.effective_hls_fallback", {}, "HLS fallback");
  return t("core.ui.streams.transport.auto", {}, "Auto");
}

function sourceHealthStatusLabel(status: string | null | undefined, t: TranslateFn): string {
  const normalized = String(status || "unknown").trim().toLowerCase() || "unknown";
  return t(`core.ui.streams.source.status.${normalized}`, {}, normalized);
}

function sourceHealthRecommendedActionLabel(value: string | null | undefined, t: TranslateFn): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const keysByMessage: Record<string, string> = {
    "Camera source is healthy.": "healthy",
    "Waiting for the camera source to produce frames.": "starting",
    "Camera source stopped producing fresh frames. Test RTSP and check camera load/network.": "stale",
    "Check camera power, network reachability and RTSP URL.": "unreachable",
    "Check camera username/password or ONVIF-generated RTSP credentials.": "unauthorized",
    "Camera source is idle because the source is not currently active.": "idle",
    "Review the camera backend error and test RTSP.": "error",
    "Insufficient source health data.": "unknown",
  };
  const key = keysByMessage[raw];
  if (!key) return raw;
  return t(`core.ui.streams.source.action.${key}`, {}, raw);
}

function isProbablyMobileTouchBrowser(): boolean {
  if (typeof window === "undefined" || typeof navigator === "undefined") return false;
  const userAgent = String(navigator.userAgent || "").toLowerCase();
  const platform = String((navigator as Navigator & { platform?: string }).platform || "").toLowerCase();
  const touchPoints = Number(navigator.maxTouchPoints || 0);
  const coarsePointer = typeof window.matchMedia === "function" && window.matchMedia("(pointer: coarse)").matches;
  const mobileUserAgent = /android|iphone|ipad|ipod|mobile|silk|kindle/.test(userAgent);
  const ipadDesktopMode = platform === "macintel" && touchPoints > 1;
  const compactTouchViewport = coarsePointer && Math.min(window.innerWidth || 0, window.innerHeight || 0) <= 900;
  return mobileUserAgent || ipadDesktopMode || (touchPoints > 0 && compactTouchViewport);
}

function getWebRtcIssueMessages(urls: StreamingTransmissionUrlsResponse | undefined): string[] {
  const contract = urls?.network_contract ?? null;
  const rawMessages = [
    ...(urls?.blocking_errors ?? []),
    ...(urls?.warnings ?? []),
    ...(contract?.blocking_errors ?? []),
    ...(contract?.warnings ?? []),
  ];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of rawMessages) {
    const message = String(raw || "").trim();
    if (!message) continue;
    const lowered = message.toLowerCase();
    if (!lowered.includes("webrtc") && !lowered.includes("whep") && !lowered.includes("ice")) continue;
    if (seen.has(message)) continue;
    seen.add(message);
    out.push(message);
  }
  return out;
}

function hasHomeAssistantProxyHlsContract(urls: StreamingTransmissionUrlsResponse | undefined): boolean {
  const contract = urls?.network_contract ?? null;
  return contract?.environment === "home_assistant_addon" && contract?.public_hls_mode === "proxy";
}

function buildPlaybackPlan(options: {
  transportPreference: StreamTransportPreference;
  urls: StreamingTransmissionUrlsResponse | undefined;
  hlsUrl: string | null;
  webrtcUrl: string | null;
  lowLatencyRequested: boolean;
}): StreamPlaybackPlan {
  const hasHls = Boolean(options.hlsUrl);
  const hasWebRtc = Boolean(options.webrtcUrl);
  const webRtcIssueMessages = getWebRtcIssueMessages(options.urls);
  const webRtcBlocked = webRtcIssueMessages.length > 0;
  const homeAssistantProxyHls = hasHomeAssistantProxyHlsContract(options.urls);
  const mobileTouchBrowser = isProbablyMobileTouchBrowser();

  if (options.transportPreference === "hls") {
    return {
      allowHls: hasHls,
      allowWebRtc: false,
      preferWebRtcFirst: false,
      effectiveMode: "hls",
      webRtcBlocked,
      webRtcIssueMessages,
      homeAssistantProxyHls,
      mobileTouchBrowser,
    };
  }

  if (options.transportPreference === "webrtc") {
    return {
      allowHls: false,
      allowWebRtc: hasWebRtc,
      preferWebRtcFirst: true,
      effectiveMode: "webrtc",
      webRtcBlocked,
      webRtcIssueMessages,
      homeAssistantProxyHls,
      mobileTouchBrowser,
    };
  }

  const hlsFirstForHomeAssistant = homeAssistantProxyHls && hasHls && !options.lowLatencyRequested;
  const hlsFirstForContract = webRtcBlocked && hasHls;
  const preferHlsFirst = hlsFirstForHomeAssistant || hlsFirstForContract;
  const allowWebRtc = hasWebRtc && !webRtcBlocked && (options.lowLatencyRequested || !preferHlsFirst || !hasHls);
  const preferWebRtcFirst = allowWebRtc && (options.lowLatencyRequested || !preferHlsFirst);
  const effectiveMode: EffectiveTransportMode = preferWebRtcFirst
    ? options.lowLatencyRequested
      ? "ptz_webrtc"
      : "auto_webrtc"
    : hasHls
      ? "auto_hls"
      : allowWebRtc
        ? "auto_webrtc"
        : "auto";

  return {
    allowHls: hasHls,
    allowWebRtc,
    preferWebRtcFirst,
    effectiveMode,
    webRtcBlocked,
    webRtcIssueMessages,
    homeAssistantProxyHls,
    mobileTouchBrowser,
  };
}

function hlsOutputsHaveProfiles(urls: StreamingTransmissionUrlsResponse | undefined): boolean {
  return Boolean(urls?.outputs?.some((output) => output?.protocol === "hls" && output.quality_profile_id));
}

function transmissionHasProfiledHls(transmission: StreamingTransmission): boolean {
  return Boolean(transmission.outputs?.some((output) => output?.protocol === "hls" && output.quality_profile_id));
}

function normalizeText(value: unknown, fallback: string): string {
  const normalized = typeof value === "string" ? value.trim() : "";
  return normalized || fallback;
}

function selectOutputByProtocol(
  transmission: StreamingTransmission,
  urls: StreamingTransmissionUrlsResponse | undefined,
  protocol: StreamProtocol,
  options: {
    qualityProfileId?: StreamingQualityProfileId | null;
  } = {},
): PlaybackOutputSelection | null {
  if (!urls || !Array.isArray(urls.outputs)) return null;
  const requestedProfileId = options.qualityProfileId ?? null;
  const candidates = urls.outputs.filter((output) => output && output.protocol === protocol);
  const orderedCandidates =
    protocol === "hls" && requestedProfileId
      ? [
          ...candidates.filter((output) => output.quality_profile_id === requestedProfileId),
          ...candidates.filter((output) => output.quality_profile_id === "stable_apple_tv"),
          ...candidates.filter((output) => output.quality_profile_id === "quad_grid"),
          ...candidates.filter((output) => !output.quality_profile_id),
          ...candidates.filter(
            (output) =>
              output.quality_profile_id &&
              output.quality_profile_id !== requestedProfileId &&
              output.quality_profile_id !== "stable_apple_tv" &&
              output.quality_profile_id !== "quad_grid",
          ),
        ]
      : candidates;
  const seen = new Set<string>();
  for (const output of orderedCandidates) {
    const outputKey = `${output.output_id}:${output.url}`;
    if (seen.has(outputKey)) continue;
    seen.add(outputKey);
    const url = String(output.url || "").trim();
    if (!url) continue;
    const outputId = String(output.output_id || "").trim();
    const mediaAuthType = output.media_auth_type ?? "none";
    return {
      outputId,
      url,
      auth:
        output.requires_auth === true && mediaAuthType !== "signed_url"
          ? resolveOutputBasicAuth(transmission, outputId)
          : null,
      mediaAuthType,
      urlExpiresAtUnix:
        typeof output.url_expires_at_unix === "number" ? output.url_expires_at_unix : null,
      renewAfterUnix: typeof output.renew_after_unix === "number" ? output.renew_after_unix : null,
      qualityProfileId: output.quality_profile_id ?? null,
    };
  }
  return null;
}

function resolveOutputBasicAuth(transmission: StreamingTransmission, outputId: string): BasicAuthCredentials | null {
  const outputs = Array.isArray(transmission.outputs) ? transmission.outputs : [];
  const output = outputs.find((item) => String(item?.id || "").trim() === String(outputId || "").trim()) ?? null;
  if (!output) return null;
  const auth = output.authentication;
  if (!auth || auth.enabled !== true) return null;
  const username = String(auth.username || "").trim();
  const password = String(auth.password || "").trim();
  if (!username || !password) return null;
  return { username, password };
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

function asErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || i18n.t("core.ui.streams.error_unknown", {}, "unknown error"));
}

function isHlsAuthProbeErrorMessage(message: string): boolean {
  const lowered = String(message || "").toLowerCase();
  return lowered.includes("(401)") || lowered.includes("(403)") || lowered.includes("media_token_expired");
}

function canPlayNativeHls(video: HTMLVideoElement): boolean {
  const check = video.canPlayType("application/vnd.apple.mpegurl");
  return check === "probably" || check === "maybe";
}

function canUseWebRtc(): boolean {
  return typeof RTCPeerConnection !== "undefined";
}

function resolveHlsRelativeUrl(baseUrl: string, rawUrl: string): string {
  return new URL(String(rawUrl || "").trim(), baseUrl).toString();
}

function hlsPlaylistUris(playlistText: string): string[] {
  return String(playlistText || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
}

function hlsAttributeUris(playlistText: string): string[] {
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

async function fetchWithTimeout(url: string, init: RequestInit = {}): Promise<Response> {
  const abortController = new AbortController();
  const timeoutId = window.setTimeout(() => abortController.abort(), HLS_BROWSER_PROBE_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: abortController.signal });
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function fetchHlsPlaylistText(url: string, authHeader: string | null): Promise<string> {
  const headers: Record<string, string> = {
    accept: "application/vnd.apple.mpegurl, application/x-mpegurl, text/plain, */*",
  };
  if (authHeader) headers.authorization = authHeader;
  const response = await fetchWithTimeout(url, { cache: "no-store", headers });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error(`Secure HLS URL expired or was rejected (${response.status}).`);
    }
    throw new Error(`HLS playlist probe failed (${response.status}).`);
  }
  const text = await response.text();
  if (!text.includes("#EXTM3U")) {
    throw new Error("HLS playlist probe failed: response is not an HLS playlist.");
  }
  return text;
}

async function probeBrowserHlsPlayback(
  masterPlaylistUrl: string,
  authHeader: string | null,
): Promise<{ mediaPlaylistUrl: string; tailSegmentUrl: string; mediaSequence: string | null; targetDuration: string | null }> {
  const masterText = await fetchHlsPlaylistText(masterPlaylistUrl, authHeader);
  const masterUris = hlsPlaylistUris(masterText);
  const mediaPlaylistUrl =
    masterText.includes("#EXT-X-STREAM-INF") && masterUris.length
      ? resolveHlsRelativeUrl(masterPlaylistUrl, masterUris[0])
      : masterPlaylistUrl;
  const mediaText = mediaPlaylistUrl === masterPlaylistUrl ? masterText : await fetchHlsPlaylistText(mediaPlaylistUrl, authHeader);
  const mediaUris = hlsPlaylistUris(mediaText);
  const attributeUris = hlsAttributeUris(mediaText);
  const tailCandidates = [...mediaUris, ...attributeUris].filter((uri) => !uri.startsWith("data:"));
  const tailUri = tailCandidates.length ? tailCandidates[tailCandidates.length - 1] : undefined;
  if (!tailUri) {
    throw new Error("HLS playlist probe failed: no media segment was found.");
  }
  const tailSegmentUrl = resolveHlsRelativeUrl(mediaPlaylistUrl, tailUri);
  const headers: Record<string, string> = { range: "bytes=0-1", accept: "*/*" };
  if (authHeader) headers.authorization = authHeader;
  const response = await fetchWithTimeout(tailSegmentUrl, { cache: "no-store", headers });
  if (!response.ok && response.status !== 206) {
    if (response.status === 401 || response.status === 403) {
      throw new Error(`Secure HLS URL expired or was rejected (${response.status}).`);
    }
    throw new Error(`HLS tail segment probe failed (${response.status}).`);
  }
  const sequenceMatch = mediaText.match(/#EXT-X-MEDIA-SEQUENCE:(\d+)/);
  const targetDurationMatch = mediaText.match(/#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)/);
  return {
    mediaPlaylistUrl,
    tailSegmentUrl,
    mediaSequence: sequenceMatch?.[1] ?? null,
    targetDuration: targetDurationMatch?.[1] ?? null,
  };
}

async function collectWebRtcStats(peerConnection: RTCPeerConnection): Promise<WebRtcStatsSummary> {
  const stats = await peerConnection.getStats();
  let rttSeconds: number | null = null;
  let packetsLost: number | null = null;
  let packetsReceived: number | null = null;
  let jitterSeconds: number | null = null;
  let framesDecoded: number | null = null;
  let framesPerSecond: number | null = null;

  stats.forEach((raw) => {
    const item = raw as RTCStats & Record<string, unknown>;
    if (item.type === "candidate-pair") {
      const selected =
        item.selected === true ||
        (item.nominated === true && String(item.state || "").toLowerCase() === "succeeded");
      if (selected && typeof item.currentRoundTripTime === "number") {
        rttSeconds = Number(item.currentRoundTripTime);
      }
    }
    if (item.type === "inbound-rtp") {
      const kind = String(item.kind || item.mediaType || "").toLowerCase();
      if (kind && kind !== "video") return;
      if (typeof item.packetsLost === "number") packetsLost = Number(item.packetsLost);
      if (typeof item.packetsReceived === "number") packetsReceived = Number(item.packetsReceived);
      if (typeof item.jitter === "number") jitterSeconds = Number(item.jitter);
      if (typeof item.framesDecoded === "number") framesDecoded = Number(item.framesDecoded);
      if (typeof item.framesPerSecond === "number") framesPerSecond = Number(item.framesPerSecond);
    }
  });

  const totalPackets =
    packetsLost !== null && packetsReceived !== null ? Math.max(0, packetsLost + packetsReceived) : null;
  return {
    iceConnectionState: peerConnection.iceConnectionState,
    connectionState: peerConnection.connectionState,
    rttMs: rttSeconds !== null ? Math.round(rttSeconds * 1000) : null,
    packetLossPct: totalPackets && packetsLost !== null ? Math.max(0, (packetsLost / totalPackets) * 100) : null,
    packetsLost,
    jitterMs: jitterSeconds !== null ? Math.round(jitterSeconds * 1000) : null,
    framesDecoded,
    framesPerSecond,
  };
}

function formatRuntimeAge(seconds: number | null | undefined): string {
  if (!Number.isFinite(seconds ?? NaN)) return "-";
  const value = Math.max(0, Number(seconds));
  if (value < 10) return `${value.toFixed(1)}s`;
  return `${Math.round(value)}s`;
}

function formatLastLiveTime(unixSeconds: number | null | undefined): string {
  if (!Number.isFinite(unixSeconds ?? NaN) || !unixSeconds) return "";
  try {
    return new Date(Number(unixSeconds) * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "";
  }
}

function formatUnixDateTime(unixSeconds: number | null | undefined): string {
  if (!Number.isFinite(unixSeconds ?? NaN) || !unixSeconds) return "-";
  try {
    return new Date(Number(unixSeconds) * 1000).toLocaleString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  } catch {
    return "-";
  }
}

function formatTechnicalBoolean(value: boolean | null | undefined): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "-";
}

function formatTechnicalNumber(value: number | null | undefined, suffix = "", maximumFractionDigits = 1): string {
  if (!Number.isFinite(value ?? NaN)) return "-";
  return `${Number(value).toLocaleString([], { maximumFractionDigits })}${suffix}`;
}

function formatResolution(resolution: { width?: number; height?: number } | null | undefined): string {
  const width = Number(resolution?.width);
  const height = Number(resolution?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return "-";
  return `${Math.round(width)}x${Math.round(height)}`;
}

function formatLatencyProfile(value: string | null | undefined): string {
  if (value === "ultra_low") return "Ultra low";
  if (value === "low") return "Low";
  if (value === "normal") return "Normal";
  return "-";
}

function formatQualityProfileId(value: StreamingQualityProfileId | null | undefined): string {
  if (value === "quad_grid") return "Low grid";
  if (value === "stable_apple_tv") return "Stable";
  if (value === "fullscreen_quality") return "High";
  if (value === "diagnostic_low") return "Diagnostic";
  return "-";
}

function findUrlOutput(
  urls: StreamingTransmissionUrlsResponse | undefined,
  outputId: string | null | undefined,
): StreamingTransmissionUrlOutput | null {
  if (!urls || !outputId) return null;
  return urls.outputs.find((output) => output.output_id === outputId) ?? null;
}

function findRuntimeOutput(
  health: StreamingRuntimeTransmissionHealth | undefined,
  outputId: string | null | undefined,
): StreamingRuntimeTransmissionHealth["outputs"][number] | null {
  if (!health || !outputId) return null;
  return health.outputs.find((output) => output.output_id === outputId) ?? null;
}

function hasHealthyHlsRuntime(health: StreamingRuntimeTransmissionHealth | undefined): boolean {
  return Boolean(
    health?.outputs?.some(
      (output) =>
        output.protocol === "hls" &&
        (output.status === "live" || output.status === "degraded") &&
        output.publisher_running !== false,
    ),
  );
}

function buildRuntimeHealthHint(
  health: StreamingRuntimeTransmissionHealth | undefined,
  t: ReturnType<typeof i18n.useI18n>["t"],
  options: { suppressRecoveredClientTransportErrors?: boolean } = {},
): { message: string; tone: TileHealthTone } | null {
  if (!health) return null;
  if (health.event_gated_idle) {
    return {
      message: t(
        "core.ui.streams.health.event_gated_idle",
        {},
        "No event currently. Waiting for motion/detection...",
      ),
      tone: "warn",
    };
  }
  const sourceHealth = health.source_health;
  if (sourceHealth?.status === "stale" || health.classification === "source_stale") {
    const lastCameraFrame = formatLastLiveTime(sourceHealth?.last_frame_at_unix);
    const suffix = lastCameraFrame
      ? t("core.ui.streams.health.camera_last_frame_suffix", { time: lastCameraFrame }, ` Last camera frame: ${lastCameraFrame}.`)
      : "";
    return {
      message: t(
        "core.ui.streams.health.camera_source_stale",
        { suffix },
        `Camera source stale.${suffix} Recovering...`,
      ),
      tone: "error",
    };
  }
  if (sourceHealth?.status === "unreachable") {
    return {
      message: t(
        "core.ui.streams.health.camera_source_unreachable",
        {},
        "Camera source unreachable. Check camera power/network/RTSP URL.",
      ),
      tone: "error",
    };
  }
  if (sourceHealth?.status === "unauthorized") {
    return {
      message: t(
        "core.ui.streams.health.camera_source_unauthorized",
        {},
        "Camera source unauthorized. Check camera credentials.",
      ),
      tone: "error",
    };
  }
  if (sourceHealth?.status === "error") {
    return {
      message:
        sourceHealthRecommendedActionLabel(sourceHealth.recommended_action, t) ||
        sourceHealth.last_error ||
        t("core.ui.streams.health.camera_source_error", {}, "Camera source error."),
      tone: "error",
    };
  }
  if (health.classification && health.classification !== "healthy" && health.classification !== "unknown") {
    if (
      options.suppressRecoveredClientTransportErrors &&
      (health.classification === "webrtc_transport_error" || health.classification === "app_player_lifecycle") &&
      (health.status === "live" || health.status === "degraded") &&
      hasHealthyHlsRuntime(health)
    ) {
      return null;
    }
    const evidence = (health.evidence ?? []).slice(0, 2).join(" ");
    const message = evidence
      ? `${health.classification}: ${evidence}`
      : health.classification;
    return {
      message,
      tone: health.classification === "app_player_lifecycle" ? "warn" : "error",
    };
  }
  const lastLive = formatLastLiveTime(health.last_live_frame_at_unix);
  if (health.status === "stale" || health.stale) {
    const suffix = lastLive
      ? t("core.ui.streams.health.last_live_suffix", { time: lastLive }, ` Last live frame: ${lastLive}.`)
      : "";
    return {
      message: t(
        "core.ui.streams.health.stale",
        { suffix },
        `Stream stale.${suffix} Recovering...`,
      ),
      tone: "error",
    };
  }
  if (health.status === "offline") {
    return {
      message: t(
        "core.ui.streams.health.offline",
        {},
        "Stream offline. Waiting for publisher and fresh frames...",
      ),
      tone: "error",
    };
  }
  if (health.status === "degraded" || health.fallback_active) {
    return {
      message: t(
        "core.ui.streams.health.degraded",
        { age: formatRuntimeAge(health.selected_frame_age_seconds) },
        `Stream degraded. Selected frame age: ${formatRuntimeAge(health.selected_frame_age_seconds)}.`,
      ),
      tone: "warn",
    };
  }
  return null;
}

function runtimeStatusLabel(
  status: StreamingRuntimeTransmissionHealth["status"] | undefined,
  t: ReturnType<typeof i18n.useI18n>["t"],
  eventGatedIdle = false,
): string | null {
  if (eventGatedIdle) return t("core.ui.streams.health.event_gated_idle_label", {}, "Waiting event");
  if (status === "live") return t("core.ui.streams.health.live_label", {}, "Live");
  if (status === "degraded") return t("core.ui.streams.health.degraded_label", {}, "Degraded");
  if (status === "stale") return t("core.ui.streams.health.stale_label", {}, "Stale");
  if (status === "offline") return t("core.ui.streams.health.offline_label", {}, "Offline");
  return null;
}

function waitForIceGatheringComplete(peerConnection: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (peerConnection.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      peerConnection.removeEventListener("icegatheringstatechange", onStateChange);
      reject(new Error(i18n.t("core.ui.streams.errors.webrtc_ice_timeout", {}, "Timed out waiting for ICE gathering.")));
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
  if (isFailed()) return Promise.reject(new Error(i18n.t("core.ui.streams.errors.webrtc_connection_failed", {}, "WebRTC connection failed.")));

  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      cleanup();
      reject(new Error(i18n.t("core.ui.streams.errors.webrtc_connection_timeout", {}, "WebRTC connection timed out.")));
    }, Math.max(1000, timeoutMs));

    const onStateChange = () => {
      if (isConnected()) {
        cleanup();
        resolve();
        return;
      }
      if (isFailed()) {
        cleanup();
        reject(new Error(i18n.t("core.ui.streams.errors.webrtc_connection_failed", {}, "WebRTC connection failed.")));
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

function TechnicalDetailRow({ label, value }: { label: string; value: React.ReactNode }): React.ReactElement {
  return (
    <div className="streamsAdvancedDetailRow">
      <div className="streamsAdvancedDetailLabel">{label}</div>
      <div className="streamsAdvancedDetailValue">{value || "-"}</div>
    </div>
  );
}

function StreamAdvancedSettingsModal({
  open,
  label,
  onClose,
  urls,
  runtimeHealth,
  hlsOutputId,
  webrtcOutputId,
  selectedOutputId,
  hlsQualityProfileId,
  playbackStatus,
  playbackTransport,
  webRtcStats,
  webRtcFallbackActive,
  hlsProbeSummary,
  hlsUrl,
  playbackPlan,
  lowLatencyRequested,
  errorText,
  sourceHint,
  qualityPreference,
  transportPreference,
  onQualityPreferenceChange,
  onTransportPreferenceChange,
}: {
  open: boolean;
  label: string;
  onClose: () => void;
  urls?: StreamingTransmissionUrlsResponse;
  runtimeHealth?: StreamingRuntimeTransmissionHealth;
  hlsOutputId: string | null;
  webrtcOutputId: string | null;
  selectedOutputId: string | null;
  hlsQualityProfileId: StreamingQualityProfileId | null;
  playbackStatus: TilePlaybackStatus;
  playbackTransport: TilePlaybackTransport;
  webRtcStats: WebRtcStatsSummary | null;
  webRtcFallbackActive: boolean;
  hlsProbeSummary: string | null;
  hlsUrl: string | null;
  playbackPlan: StreamPlaybackPlan;
  lowLatencyRequested: boolean;
  errorText: string | null;
  sourceHint: string | null;
  qualityPreference: StreamQualityPreference;
  transportPreference: StreamTransportPreference;
  onQualityPreferenceChange: (preference: StreamQualityPreference) => void;
  onTransportPreferenceChange: (preference: StreamTransportPreference) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const activeOutputId =
    playbackTransport === "webrtc"
      ? webrtcOutputId
      : playbackTransport === "hls"
        ? hlsOutputId
        : selectedOutputId;
  const activeUrlOutput = findUrlOutput(urls, activeOutputId);
  const activeRuntimeOutput = findRuntimeOutput(runtimeHealth, activeOutputId);
  const sourceHealth = runtimeHealth?.source_health ?? null;
  const evidence = runtimeHealth?.evidence ?? [];
  const warnings = urls?.warnings ?? [];
  const publicBasePath = urls?.public_base_path || urls?.network_contract?.public_base_path || "/";
  const mediaOrigin = urls?.media_url_origin || urls?.network_contract?.media_url_origin || "-";
  const hlsUsesPublicBasePath =
    Boolean(hlsUrl) && publicBasePath !== "/" && String(hlsUrl || "").includes(publicBasePath);
  const webRtcGuidance =
    playbackPlan.homeAssistantProxyHls && (playbackPlan.webRtcBlocked || lowLatencyRequested || transportPreference === "webrtc")
      ? t(
          "core.ui.streams.transport.webrtc_addon_hint",
          {},
          "WebRTC low latency from Home Assistant direct access requires 18760/tcp, 18762/udp, and the browser host/IP in WebRTC additional hosts.",
        )
      : "";

  return (
    <Modal
      open={open}
      title={t("core.ui.streams.advanced.title", { label }, `Stream details: ${label}`)}
      onClose={onClose}
      panelClassName="streamsAdvancedModalPanel"
      bodyClassName="streamsAdvancedModalBody"
    >
      <div className="streamsAdvancedControls">
        <label className="streamsAdvancedControl">
          <span>{t("core.ui.streams.transport.label", {}, "Stream transport")}</span>
          <select
            className="streamsAdvancedSelect"
            value={transportPreference}
            onChange={(event) => onTransportPreferenceChange(event.target.value as StreamTransportPreference)}
          >
            {(["auto", "webrtc", "hls"] as const).map((preference) => (
              <option key={preference} value={preference}>
                {transportPreferenceLabel(preference, t)}
              </option>
            ))}
          </select>
        </label>
        <label className="streamsAdvancedControl">
          <span>{t("core.ui.streams.quality.label", {}, "Stream quality")}</span>
          <select
            className="streamsAdvancedSelect"
            value={qualityPreference}
            onChange={(event) => onQualityPreferenceChange(event.target.value as StreamQualityPreference)}
          >
            {(["auto", "low", "stable", "high", "diagnostic"] as const).map((preference) => (
              <option key={preference} value={preference}>
                {qualityPreferenceLabel(preference, t)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <section className="streamsAdvancedSection">
        <div className="streamsAdvancedSectionTitle">{t("core.ui.streams.advanced.playback", {}, "Playback")}</div>
        <div className="streamsAdvancedDetailGrid">
          <TechnicalDetailRow label="Player status" value={playbackStatus} />
          <TechnicalDetailRow label="Active transport" value={playbackTransport === "none" ? "-" : playbackTransport.toUpperCase()} />
          <TechnicalDetailRow label="Effective mode" value={webRtcFallbackActive ? effectiveTransportModeLabel("hls_fallback", t) : effectiveTransportModeLabel(playbackPlan.effectiveMode, t)} />
          <TechnicalDetailRow label="Transport preference" value={transportPreferenceLabel(transportPreference, t)} />
          <TechnicalDetailRow label="Quality preference" value={qualityPreferenceLabel(qualityPreference, t)} />
          <TechnicalDetailRow label="Low latency requested" value={formatTechnicalBoolean(lowLatencyRequested)} />
          <TechnicalDetailRow label="HLS fallback" value={formatTechnicalBoolean(webRtcFallbackActive)} />
          <TechnicalDetailRow label="HLS public base path" value={publicBasePath || "/"} />
          <TechnicalDetailRow label="HLS ingress prefix in URL" value={formatTechnicalBoolean(hlsUsesPublicBasePath)} />
          <TechnicalDetailRow label="HLS media origin" value={mediaOrigin} />
          <TechnicalDetailRow label="Last HLS probe" value={hlsProbeSummary || "-"} />
          <TechnicalDetailRow label="WebRTC contract" value={playbackPlan.webRtcBlocked ? "warning" : "ok"} />
          <TechnicalDetailRow label="Player error" value={errorText || "-"} />
          <TechnicalDetailRow label="Current hint" value={sourceHint || "-"} />
        </div>
        {playbackPlan.webRtcIssueMessages.length ? <div className="streamsAdvancedNote isWarn">{playbackPlan.webRtcIssueMessages.join(" ")}</div> : null}
        {webRtcGuidance ? <div className="streamsAdvancedNote">{webRtcGuidance}</div> : null}
      </section>

      <section className="streamsAdvancedSection">
        <div className="streamsAdvancedSectionTitle">{t("core.ui.streams.advanced.selected_output", {}, "Selected output")}</div>
        <div className="streamsAdvancedDetailGrid">
          <TechnicalDetailRow label="Output ID" value={activeOutputId || "-"} />
          <TechnicalDetailRow label="Protocol" value={activeUrlOutput?.protocol?.toUpperCase() || activeRuntimeOutput?.protocol?.toUpperCase() || "-"} />
          <TechnicalDetailRow label="Engine path" value={activeUrlOutput?.resolved_engine_path || activeRuntimeOutput?.resolved_engine_path || "-"} />
          <TechnicalDetailRow label="Quality profile" value={formatQualityProfileId(activeUrlOutput?.quality_profile_id ?? activeRuntimeOutput?.quality_profile_id ?? hlsQualityProfileId)} />
          <TechnicalDetailRow label="Resolution" value={formatResolution(activeUrlOutput?.resolution ?? activeRuntimeOutput?.resolution)} />
          <TechnicalDetailRow label="FPS limit" value={formatTechnicalNumber(activeUrlOutput?.fps_limit ?? activeRuntimeOutput?.fps_limit, " fps", 1)} />
          <TechnicalDetailRow label="Bitrate" value={formatTechnicalNumber(activeUrlOutput?.bitrate_kbps ?? activeRuntimeOutput?.bitrate_kbps, " kbps", 0)} />
          <TechnicalDetailRow label="Latency profile" value={formatLatencyProfile(activeUrlOutput?.latency_profile ?? activeRuntimeOutput?.latency_profile)} />
          <TechnicalDetailRow label="Media auth" value={activeUrlOutput?.media_auth_type || "-"} />
          <TechnicalDetailRow label="URL expires" value={formatUnixDateTime(activeUrlOutput?.url_expires_at_unix)} />
          <TechnicalDetailRow label="Renew after" value={formatUnixDateTime(activeUrlOutput?.renew_after_unix)} />
          <TechnicalDetailRow label="Publisher codec" value={activeRuntimeOutput?.publisher_active_codec || "-"} />
          <TechnicalDetailRow label="Hardware encoder" value={formatTechnicalBoolean(activeRuntimeOutput?.publisher_hardware_accelerated)} />
          <TechnicalDetailRow label="Frames sent" value={formatTechnicalNumber(activeRuntimeOutput?.publisher_frames_sent, "", 0)} />
          <TechnicalDetailRow label="Frame rate" value={formatTechnicalNumber(activeRuntimeOutput?.publisher_frames_sent_rate, " fps", 1)} />
          <TechnicalDetailRow label="Viewers" value={formatTechnicalNumber(activeRuntimeOutput?.viewer_count, "", 0)} />
        </div>
      </section>

      <section className="streamsAdvancedSection">
        <div className="streamsAdvancedSectionTitle">{t("core.ui.streams.advanced.runtime", {}, "Runtime health")}</div>
        <div className="streamsAdvancedDetailGrid">
          <TechnicalDetailRow label="Runtime status" value={runtimeStatusLabel(runtimeHealth?.status, t, Boolean(runtimeHealth?.event_gated_idle)) || "-"} />
          <TechnicalDetailRow label="Classification" value={runtimeHealth?.classification || "-"} />
          <TechnicalDetailRow label="Selected frame age" value={formatRuntimeAge(runtimeHealth?.selected_frame_age_seconds)} />
          <TechnicalDetailRow label="Last incoming age" value={formatRuntimeAge(runtimeHealth?.last_incoming_frame_age_seconds)} />
          <TechnicalDetailRow label="Last live frame" value={formatUnixDateTime(runtimeHealth?.last_live_frame_at_unix)} />
          <TechnicalDetailRow label="Fallback active" value={formatTechnicalBoolean(runtimeHealth?.fallback_active)} />
          <TechnicalDetailRow label="Fallback reason" value={runtimeHealth?.fallback_reason || "-"} />
          <TechnicalDetailRow label="Placeholder active" value={formatTechnicalBoolean(runtimeHealth?.placeholder_active)} />
          <TechnicalDetailRow label="Active writer" value={runtimeHealth?.active_writer_id || "-"} />
          <TechnicalDetailRow label="Selected writer" value={runtimeHealth?.selected_writer_id || "-"} />
          <TechnicalDetailRow label="Stream behavior" value={runtimeHealth?.stream_behavior || "-"} />
          <TechnicalDetailRow label="Active sessions" value={formatTechnicalNumber(runtimeHealth?.active_playback_session_count, "", 0)} />
        </div>
        {evidence.length ? <div className="streamsAdvancedNote">{evidence.join(" ")}</div> : null}
      </section>

      <section className="streamsAdvancedSection">
        <div className="streamsAdvancedSectionTitle">{t("core.ui.streams.advanced.source", {}, "Camera source")}</div>
        <div className="streamsAdvancedDetailGrid">
          <TechnicalDetailRow label={t("core.ui.streams.source.status", {}, "Source status")} value={sourceHealthStatusLabel(sourceHealth?.status, t)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.camera", {}, "Camera")} value={sourceHealth?.camera_name || sourceHealth?.camera_id || "-"} />
          <TechnicalDetailRow label={t("core.ui.streams.source.backend", {}, "Backend")} value={sourceHealth?.backend || sourceHealth?.configured_backend || "-"} />
          <TechnicalDetailRow label={t("core.ui.streams.source.transport", {}, "Transport")} value={sourceHealth?.rtsp_transport || "-"} />
          <TechnicalDetailRow label={t("core.ui.streams.source.age", {}, "Source age")} value={formatRuntimeAge(sourceHealth?.source_frame_age_seconds)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.capture_fps", {}, "Capture FPS")} value={formatTechnicalNumber(sourceHealth?.capture_fps, " fps", 1)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.target_fps", {}, "Target FPS")} value={formatTechnicalNumber(sourceHealth?.target_fps, " fps", 1)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.frames_captured", {}, "Frames captured")} value={formatTechnicalNumber(sourceHealth?.frames_captured, "", 0)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.restarts", {}, "Restarts")} value={formatTechnicalNumber(sourceHealth?.restarts_total, "", 0)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.decode_failures", {}, "Decode failures")} value={formatTechnicalNumber(sourceHealth?.decode_failures, "", 0)} />
          <TechnicalDetailRow label={t("core.ui.streams.source.last_frame", {}, "Last camera frame")} value={formatUnixDateTime(sourceHealth?.last_frame_at_unix)} />
          <TechnicalDetailRow
            label={t("core.ui.streams.source.recommended_action", {}, "Recommended action")}
            value={sourceHealthRecommendedActionLabel(sourceHealth?.recommended_action, t) || "-"}
          />
        </div>
        {sourceHealth?.last_error ? <div className="streamsAdvancedNote isError">{sourceHealth.last_error}</div> : null}
      </section>

      <section className="streamsAdvancedSection">
        <div className="streamsAdvancedSectionTitle">{t("core.ui.streams.advanced.webrtc", {}, "WebRTC stats")}</div>
        <div className="streamsAdvancedDetailGrid">
          <TechnicalDetailRow label="ICE state" value={webRtcStats?.iceConnectionState || "-"} />
          <TechnicalDetailRow label="Connection state" value={webRtcStats?.connectionState || "-"} />
          <TechnicalDetailRow label="RTT" value={formatTechnicalNumber(webRtcStats?.rttMs, " ms", 0)} />
          <TechnicalDetailRow label="Packet loss" value={formatTechnicalNumber(webRtcStats?.packetLossPct, "%", 1)} />
          <TechnicalDetailRow label="Packets lost" value={formatTechnicalNumber(webRtcStats?.packetsLost, "", 0)} />
          <TechnicalDetailRow label="Jitter" value={formatTechnicalNumber(webRtcStats?.jitterMs, " ms", 0)} />
          <TechnicalDetailRow label="Decoded FPS" value={formatTechnicalNumber(webRtcStats?.framesPerSecond, " fps", 1)} />
          <TechnicalDetailRow label="Frames decoded" value={formatTechnicalNumber(webRtcStats?.framesDecoded, "", 0)} />
        </div>
      </section>

      <section className="streamsAdvancedSection">
        <div className="streamsAdvancedSectionTitle">{t("core.ui.streams.advanced.outputs", {}, "Outputs")}</div>
        <div className="streamsAdvancedOutputsTable">
          <div className="streamsAdvancedOutputsHeader">
            <span>Output</span>
            <span>Profile</span>
            <span>Video</span>
            <span>Runtime</span>
          </div>
          {(runtimeHealth?.outputs ?? []).map((output) => (
            <div key={output.output_key || output.output_id} className="streamsAdvancedOutputsRow">
              <span title={output.output_id}>{output.protocol.toUpperCase()} · {output.output_id}</span>
              <span>{formatQualityProfileId(output.quality_profile_id)} · {formatLatencyProfile(output.latency_profile)}</span>
              <span>{formatResolution(output.resolution)} · {formatTechnicalNumber(output.fps_limit, " fps", 1)} · {formatTechnicalNumber(output.bitrate_kbps, " kbps", 0)}</span>
              <span>{output.status} · {formatTechnicalNumber(output.publisher_frames_sent_rate, " fps", 1)} · {formatTechnicalNumber(output.viewer_count, " viewers", 0)}</span>
            </div>
          ))}
          {!(runtimeHealth?.outputs ?? []).length ? (
            <div className="streamsAdvancedOutputsEmpty">{t("core.ui.streams.advanced.no_runtime_outputs", {}, "No runtime outputs reported yet.")}</div>
          ) : null}
        </div>
        {warnings.length ? <div className="streamsAdvancedNote isWarn">{warnings.join(" ")}</div> : null}
      </section>
    </Modal>
  );
}

function StreamTilePlayer({
  transmissionId,
  outputId,
  webrtcOutputId,
  hlsOutputId,
  hlsQualityProfileId,
  label,
  urls,
  overlayVisible,
  sourceHint,
  sourceHintTone,
  webrtcUrl,
  webrtcAuthHeader,
  hlsUrl,
  hlsAuthHeader,
  hlsNativeUrl,
  runtimeHealth,
  active,
  ptzEnabled,
  lowLatencyRequested,
  qualityPreference,
  transportPreference,
  onQualityPreferenceChange,
  onTransportPreferenceChange,
  onRefreshUrls,
  onOpenPtz,
}: {
  transmissionId: string;
  outputId: string | null;
  webrtcOutputId: string | null;
  hlsOutputId: string | null;
  hlsQualityProfileId: StreamingQualityProfileId | null;
  label: string;
  urls?: StreamingTransmissionUrlsResponse;
  overlayVisible: boolean;
  sourceHint: string | null;
  sourceHintTone: "muted" | "warn" | "error";
  webrtcUrl: string | null;
  webrtcAuthHeader: string | null;
  hlsUrl: string | null;
  hlsAuthHeader: string | null;
  hlsNativeUrl: string | null;
  runtimeHealth?: StreamingRuntimeTransmissionHealth;
  active: boolean;
  ptzEnabled: boolean;
  lowLatencyRequested: boolean;
  qualityPreference: StreamQualityPreference;
  transportPreference: StreamTransportPreference;
  onQualityPreferenceChange: (preference: StreamQualityPreference) => void;
  onTransportPreferenceChange: (preference: StreamTransportPreference) => void;
  onRefreshUrls: () => Promise<void>;
  onOpenPtz: () => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);
  const playbackSessionIdRef = useRef<string | null>(null);
  const onRefreshUrlsRef = useRef(onRefreshUrls);

  const [status, setStatus] = useState<TilePlaybackStatus>("idle");
  const [transport, setTransport] = useState<TilePlaybackTransport>("none");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [webRtcStats, setWebRtcStats] = useState<WebRtcStatsSummary | null>(null);
  const [webRtcFallbackActive, setWebRtcFallbackActive] = useState(false);
  const [hlsProbeSummary, setHlsProbeSummary] = useState<string | null>(null);
  const [pictureInPictureActive, setPictureInPictureActive] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const playbackActive = active || pictureInPictureActive;
  useEffect(() => {
    onRefreshUrlsRef.current = onRefreshUrls;
  }, [onRefreshUrls]);
  const playbackPlan = useMemo(
    () =>
      buildPlaybackPlan({
        transportPreference,
        urls,
        hlsUrl,
        webrtcUrl,
        lowLatencyRequested,
      }),
    [hlsUrl, lowLatencyRequested, transportPreference, urls, webrtcUrl],
  );
  const { allowHls, allowWebRtc, preferWebRtcFirst } = playbackPlan;
  const transportTelemetryBase = useMemo(
    () => ({
      transport_preference: transportPreference,
      effective_transport: playbackPlan.effectiveMode,
      low_latency_requested: lowLatencyRequested,
      webrtc_blocked: playbackPlan.webRtcBlocked,
      webrtc_issue_count: playbackPlan.webRtcIssueMessages.length,
      home_assistant_proxy_hls: playbackPlan.homeAssistantProxyHls,
      mobile_touch_browser: playbackPlan.mobileTouchBrowser,
    }),
    [
      lowLatencyRequested,
      playbackPlan.effectiveMode,
      playbackPlan.homeAssistantProxyHls,
      playbackPlan.mobileTouchBrowser,
      playbackPlan.webRtcBlocked,
      playbackPlan.webRtcIssueMessages.length,
      transportPreference,
    ],
  );
  const withTransportTelemetry = useCallback(
    (data: Record<string, unknown> = {}) => ({ ...transportTelemetryBase, ...data }),
    [transportTelemetryBase],
  );

  const recordWebPlaybackEvent = useCallback(
    (
      type: string,
      options: {
        severity?: "debug" | "info" | "warn" | "error";
        message?: string;
        data?: Record<string, unknown>;
      } = {},
    ) => {
      const playbackSessionId = playbackSessionIdRef.current;
      if (!playbackSessionId) return;
      void postStreamingPlaybackEvents({
        playback_session_id: playbackSessionId,
        transmission_id: transmissionId,
        output_id: outputId,
        client_kind: "web",
        platform: "web",
        app_state: typeof document === "undefined" ? "unknown" : document.visibilityState,
        pip_active: pictureInPictureActive,
        events: [
          {
            type,
            severity: options.severity ?? "info",
            at_unix: Date.now() / 1000,
            message: options.message,
            data: options.data,
          },
        ],
      }).catch(() => {
        // Playback telemetry is best-effort and must not affect the player.
      });
    },
    [outputId, pictureInPictureActive, transmissionId],
  );

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const updatePictureInPictureState = () => {
      const anyDocument = document as unknown as {
        pictureInPictureElement?: Element | null;
      };
      const anyVideo = video as unknown as {
        webkitPresentationMode?: string;
      };
      const standardPictureInPicture = anyDocument.pictureInPictureElement === video;
      const webkitPictureInPicture = anyVideo.webkitPresentationMode === "picture-in-picture";
      setPictureInPictureActive(Boolean(standardPictureInPicture || webkitPictureInPicture));
    };

    const onEnterPictureInPicture = () => updatePictureInPictureState();
    const onLeavePictureInPicture = () => updatePictureInPictureState();
    const onWebkitPresentationModeChanged = () => updatePictureInPictureState();

    video.addEventListener("enterpictureinpicture", onEnterPictureInPicture);
    video.addEventListener("leavepictureinpicture", onLeavePictureInPicture);
    video.addEventListener("webkitpresentationmodechanged", onWebkitPresentationModeChanged as EventListener);
    updatePictureInPictureState();

    return () => {
      video.removeEventListener("enterpictureinpicture", onEnterPictureInPicture);
      video.removeEventListener("leavepictureinpicture", onLeavePictureInPicture);
      video.removeEventListener("webkitpresentationmodechanged", onWebkitPresentationModeChanged as EventListener);
    };
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !playbackActive) return;
    const onPlay = () =>
      recordWebPlaybackEvent("play", {
        severity: "debug",
        data: withTransportTelemetry({ playback_transport: transport }),
      });
    const onPlaying = () =>
      recordWebPlaybackEvent("playing", {
        severity: "info",
        data: withTransportTelemetry({ playback_transport: transport }),
      });
    const onWaiting = () =>
      recordWebPlaybackEvent("waiting", {
        severity: "warn",
        data: withTransportTelemetry({ playback_transport: transport }),
      });
    const onStalled = () =>
      recordWebPlaybackEvent("stalled", {
        severity: "warn",
        data: withTransportTelemetry({ playback_transport: transport }),
      });
    const onError = () =>
      recordWebPlaybackEvent("error", {
        severity: "error",
        message: video.error?.message || "HTML video playback error.",
        data: withTransportTelemetry({ code: video.error?.code, playback_transport: transport }),
      });
    video.addEventListener("play", onPlay);
    video.addEventListener("playing", onPlaying);
    video.addEventListener("waiting", onWaiting);
    video.addEventListener("stalled", onStalled);
    video.addEventListener("error", onError);
    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("waiting", onWaiting);
      video.removeEventListener("stalled", onStalled);
      video.removeEventListener("error", onError);
    };
  }, [playbackActive, recordWebPlaybackEvent, transport, withTransportTelemetry]);

  useEffect(() => {
    if (!playbackActive || !transmissionId) return;

    let cancelled = false;
    const renewDemandLease = () => {
      const playbackSessionId = playbackSessionIdRef.current;
      if (!playbackSessionId || cancelled) return;
      const heartbeatTransport = transport === "webrtc" && !webRtcFallbackActive ? "webrtc" : "hls";
      const selectedOutputId =
        heartbeatTransport === "webrtc" ? webrtcOutputId ?? outputId : hlsOutputId ?? outputId;
      if (!selectedOutputId) return;
      void heartbeatStreamingTransmissionDemand(transmissionId, {
        playbackSessionId,
        outputId: selectedOutputId,
        qualityProfileId: heartbeatTransport === "hls" ? hlsQualityProfileId : null,
        transport: heartbeatTransport,
      })
        .then((response) => {
          recordWebPlaybackEvent("demand_heartbeat", {
            severity: "debug",
            data: withTransportTelemetry({
              output_id: selectedOutputId,
              transport: heartbeatTransport,
              renewed_outputs: response.renewed_outputs,
              lease_seconds: response.lease_seconds,
            }),
          });
        })
        .catch((error) => {
          recordWebPlaybackEvent("demand_heartbeat_error", {
            severity: "debug",
            message: asErrorMessage(error),
            data: withTransportTelemetry({ output_id: selectedOutputId, transport: heartbeatTransport }),
          });
        });
    };

    renewDemandLease();
    const intervalId = window.setInterval(renewDemandLease, DEMAND_HEARTBEAT_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [
    hlsOutputId,
    hlsQualityProfileId,
    outputId,
    playbackActive,
    recordWebPlaybackEvent,
    transmissionId,
    transport,
    webRtcFallbackActive,
    webrtcOutputId,
    withTransportTelemetry,
  ]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    let cancelled = false;
    let nativeCleanup: (() => void) | null = null;
    let retryTimerId: number | null = null;
    let attempt = 0;
    let hlsPlayer: Hls | null = null;
    let peerConnection: RTCPeerConnection | null = null;
    let whepSessionUrl: string | null = null;
    let webrtcAbortController: AbortController | null = null;
    let webrtcStatsTimerId: number | null = null;
    const clearRetryTimer = () => {
      if (retryTimerId == null) return;
      window.clearTimeout(retryTimerId);
      retryTimerId = null;
    };

    const clearNative = () => {
      if (!nativeCleanup) return;
      nativeCleanup();
      nativeCleanup = null;
    };

    const destroyHls = () => {
      try {
        hlsPlayer?.destroy();
      } catch {
        // ignore
      }
      hlsPlayer = null;
    };

    const clearWebRtcStatsTimer = () => {
      if (webrtcStatsTimerId == null) return;
      window.clearInterval(webrtcStatsTimerId);
      webrtcStatsTimerId = null;
    };

    const destroyWebRtc = () => {
      clearWebRtcStatsTimer();
      setWebRtcStats(null);
      const abortController = webrtcAbortController;
      webrtcAbortController = null;
      if (abortController) abortController.abort();

      const sessionUrl = whepSessionUrl;
      whepSessionUrl = null;
      if (sessionUrl) {
        void fetch(sessionUrl, {
          method: "DELETE",
          mode: "cors",
          headers: webrtcAuthHeader ? { authorization: webrtcAuthHeader } : undefined,
        }).catch(() => {});
      }

      const currentPeerConnection = peerConnection;
      peerConnection = null;
      if (currentPeerConnection) {
        try {
          currentPeerConnection.ontrack = null;
          currentPeerConnection.close();
        } catch {
          // ignore
        }
      }
    };

    const clearVideoSource = () => {
      const video = videoRef.current;
      if (!video) return;
      try {
        video.pause();
      } catch {
        // ignore
      }
      try {
        video.srcObject = null;
      } catch {
        // ignore
      }
      video.removeAttribute("src");
      try {
        video.load();
      } catch {
        // ignore
      }
    };

    const destroyPlayback = () => {
      clearRetryTimer();
      clearNative();
      destroyHls();
      destroyWebRtc();
      clearVideoSource();
    };

    const scheduleRetry = (reason: string) => {
      if (cancelled || !playbackActive || ((!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl)) || retryTimerId != null) return;
      const delayMs = Math.min(RETRY_BASE_MS * Math.max(1, 2 ** attempt), RETRY_MAX_MS);
      recordWebPlaybackEvent("retry_scheduled", {
        severity: "warn",
        message: reason,
        data: withTransportTelemetry({ attempt, delay_ms: delayMs }),
      });
      retryTimerId = window.setTimeout(() => {
        retryTimerId = null;
        attempt += 1;
        setStatus("loading");
        setErrorText(reason);
        void startPlayback();
      }, delayMs);
    };

    const configureVideo = (video: HTMLVideoElement) => {
      video.muted = true;
      video.autoplay = true;
      video.playsInline = true;
      video.controls = false;
    };

    const probeHlsUrlForBrowser = async (sourceUrl: string): Promise<void> => {
      const probe = await probeBrowserHlsPlayback(sourceUrl, hlsAuthHeader);
      const summary = [
        "ok",
        probe.mediaSequence ? `seq ${probe.mediaSequence}` : null,
        probe.targetDuration ? `target ${probe.targetDuration}s` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      setHlsProbeSummary(summary);
      recordWebPlaybackEvent("hls_browser_probe", {
        severity: "debug",
        data: withTransportTelemetry({
          media_playlist_url: probe.mediaPlaylistUrl,
          tail_segment_url: probe.tailSegmentUrl,
          media_sequence: probe.mediaSequence,
          target_duration_seconds: probe.targetDuration,
        }),
      });
    };

    const startNativeHlsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      setTransport("hls");
      const sourceUrl = String(hlsNativeUrl || hlsUrl || "").trim();
      await probeHlsUrlForBrowser(sourceUrl);
      if (cancelled) return;
      let nativeRecovering = false;
      let healthyProbeRecoveries = 0;
      const onPlaying = () => {
        setStatus("playing");
        setErrorText(null);
      };
      const onError = () => {
        if (nativeRecovering) return;
        nativeRecovering = true;
        const message = i18n.t("core.ui.streams.errors.hls_native_error", {}, "Native HLS playback error.");
        void probeHlsUrlForBrowser(sourceUrl)
          .then(() => {
            if (cancelled) return;
            healthyProbeRecoveries += 1;
            if (healthyProbeRecoveries > 2) {
              setStatus("error");
              setErrorText(message);
              destroyPlayback();
              scheduleRetry(message);
              return;
            }
            setStatus("loading");
            setErrorText(null);
            video.src = sourceUrl;
            try {
              video.load();
            } catch {
              // ignore
            }
            void video.play().catch(() => {});
          })
          .catch((probeError) => {
            if (cancelled) return;
            const probeMessage = asErrorMessage(probeError) || message;
            setHlsProbeSummary(`failed: ${probeMessage}`);
            setStatus("error");
            setErrorText(probeMessage);
            destroyPlayback();
            scheduleRetry(probeMessage);
          })
          .finally(() => {
            nativeRecovering = false;
          });
      };

      video.addEventListener("playing", onPlaying);
      video.addEventListener("loadeddata", onPlaying);
      video.addEventListener("error", onError);
      nativeCleanup = () => {
        video.removeEventListener("playing", onPlaying);
        video.removeEventListener("loadeddata", onPlaying);
        video.removeEventListener("error", onError);
      };

      video.src = sourceUrl;
      try {
        await video.play();
      } catch {
        // autoplay can be blocked; user interaction will retry.
      }
    };

    const startHlsJsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      setTransport("hls");
      await probeHlsUrlForBrowser(hlsUrl ?? "");
      if (cancelled) return;
      const hlsModule = await import("hls.js");
      if (cancelled) return;
      const HlsConstructor = hlsModule.default;
      if (!HlsConstructor.isSupported()) {
        setStatus("unsupported");
        setErrorText(i18n.t("core.ui.streams.errors.hls_unsupported_browser", {}, "HLS playback is not supported in this browser."));
        return;
      }

      const hls = new HlsConstructor({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        xhrSetup: (xhr: XMLHttpRequest) => {
          if (hlsAuthHeader) xhr.setRequestHeader("Authorization", hlsAuthHeader);
        },
        fetchSetup: (context: { url: string }, initParams: RequestInit) => {
          const requestUrl = String(context?.url || "");
          if (!hlsAuthHeader) return new Request(requestUrl, initParams);
          const headers = new Headers(initParams?.headers || {});
          headers.set("Authorization", hlsAuthHeader);
          return new Request(requestUrl, { ...initParams, headers });
        },
      });
      hlsPlayer = hls;

      hls.on(HlsConstructor.Events.MANIFEST_PARSED, () => {
        setStatus("playing");
        setErrorText(null);
        void video.play().catch(() => {});
      });

      hls.on(HlsConstructor.Events.ERROR, (_event, data) => {
        if (!data?.fatal) return;
        setStatus("error");
        const details = String(
          data.details || data.type || i18n.t("core.ui.streams.errors.hlsjs_fatal", {}, "hls.js fatal error"),
        );
        void probeHlsUrlForBrowser(hlsUrl ?? "")
          .then(() => {
            if (cancelled) return;
            setStatus("loading");
            setErrorText(null);
            try {
              hls.recoverMediaError();
            } catch {
              destroyPlayback();
              scheduleRetry(details);
            }
          })
          .catch((probeError) => {
            if (cancelled) return;
            const probeMessage = asErrorMessage(probeError) || details;
            setHlsProbeSummary(`failed: ${probeMessage}`);
            setErrorText(probeMessage);
            destroyPlayback();
            scheduleRetry(probeMessage);
          });
      });

      hls.attachMedia(video);
      hls.on(HlsConstructor.Events.MEDIA_ATTACHED, () => {
        hls.loadSource(hlsUrl ?? "");
      });
    };

    const normalizeWhepSdp = (sdp: string): string => {
      // MediaMTX expects SDP with CRLF terminators; using `trim()` can remove the trailing CRLF and break parsing (EOF).
      const normalized = sdp.replace(/\r?\n/g, "\r\n");
      return normalized.endsWith("\r\n") ? normalized : `${normalized}\r\n`;
    };

    const startWebRtcPlayback = async (video: HTMLVideoElement): Promise<void> => {
      if (!webrtcUrl) throw new Error(i18n.t("core.ui.streams.errors.webrtc_url_missing", {}, "WebRTC URL is not available."));
      if (!canUseWebRtc()) throw new Error(i18n.t("core.ui.streams.errors.webrtc_unsupported_browser", {}, "WebRTC is not supported in this browser."));

      setTransport("webrtc");
      const nextPeerConnection = new RTCPeerConnection();
      peerConnection = nextPeerConnection;
      const recordIceState = () => {
        const severity =
          nextPeerConnection.iceConnectionState === "failed" ||
          nextPeerConnection.connectionState === "failed" ||
          nextPeerConnection.connectionState === "disconnected"
            ? "warn"
            : "debug";
        recordWebPlaybackEvent("webrtc_ice_state", {
          severity,
          data: withTransportTelemetry({
            ice_connection_state: nextPeerConnection.iceConnectionState,
            connection_state: nextPeerConnection.connectionState,
          }),
        });
      };
      nextPeerConnection.addEventListener("iceconnectionstatechange", recordIceState);
      nextPeerConnection.addEventListener("connectionstatechange", recordIceState);

      const remoteStream = new MediaStream();
      video.srcObject = remoteStream;

      nextPeerConnection.ontrack = (event) => {
        const sourceStream = event.streams[0] ?? null;
        if (sourceStream) {
          for (const track of sourceStream.getTracks()) {
            const exists = remoteStream.getTracks().some((item) => item.id === track.id);
            if (!exists) remoteStream.addTrack(track);
          }
          return;
        }
        remoteStream.addTrack(event.track);
      };

      nextPeerConnection.addTransceiver("video", { direction: "recvonly" });
      nextPeerConnection.addTransceiver("audio", { direction: "recvonly" });

      try {
        const offer = await nextPeerConnection.createOffer();
        await nextPeerConnection.setLocalDescription(offer);
        await waitForIceGatheringComplete(nextPeerConnection, WEBRTC_SIGNAL_TIMEOUT_MS);

        const localDescription = nextPeerConnection.localDescription;
        const offerSdpRaw = String(localDescription?.sdp || "");
        if (!offerSdpRaw.trim()) {
          throw new Error(i18n.t("core.ui.streams.errors.webrtc_sdp_offer_failed", {}, "Failed to create WebRTC SDP offer."));
        }
        const offerSdp = normalizeWhepSdp(offerSdpRaw);

        const abortController = new AbortController();
        webrtcAbortController = abortController;
        const response = await fetch(webrtcUrl, {
          method: "POST",
          mode: "cors",
          headers: {
            accept: "application/sdp",
            "content-type": "application/sdp",
            ...(webrtcAuthHeader ? { authorization: webrtcAuthHeader } : {}),
          },
          body: offerSdp,
          signal: abortController.signal,
        });
        if (!response.ok) {
          let detail = "";
          try {
            detail = String(await response.text()).trim();
          } catch {
            detail = "";
          }
          const base = i18n.t(
            "core.ui.streams.errors.whep_negotiation_failed",
            { status: response.status },
            "WHEP negotiation failed ({{status}}).",
          );
          const snippet = detail ? detail.slice(0, 280) : "";
          const responseHint = snippet
            ? i18n.t("core.ui.streams.errors.http_response_snippet", { detail: snippet }, "Response: {{detail}}")
            : "";
          throw new Error(responseHint ? `${base} ${responseHint}` : base);
        }

        const answerSdpRaw = String(await response.text());
        if (!answerSdpRaw.trim()) {
          throw new Error(i18n.t("core.ui.streams.errors.whep_answer_empty", {}, "WHEP answer is empty."));
        }
        const answerSdp = normalizeWhepSdp(answerSdpRaw);

        const locationHeader = response.headers.get("location");
        if (locationHeader) {
          try {
            whepSessionUrl = new URL(locationHeader, webrtcUrl).toString();
          } catch {
            whepSessionUrl = locationHeader;
          }
        }

        await nextPeerConnection.setRemoteDescription({ type: "answer", sdp: answerSdp });
        await waitForPeerConnectionReady(nextPeerConnection, WEBRTC_CONNECT_TIMEOUT_MS);
      } catch (error) {
        destroyWebRtc();
        throw error;
      }

      setStatus("playing");
      setErrorText(null);
      const sampleStats = () => {
        const currentPeerConnection = peerConnection;
        if (!currentPeerConnection) return;
        void collectWebRtcStats(currentPeerConnection)
          .then((stats) => {
            if (cancelled) return;
            setWebRtcStats(stats);
            recordWebPlaybackEvent("webrtc_stats", {
              severity: "debug",
              data: withTransportTelemetry(stats as unknown as Record<string, unknown>),
            });
          })
          .catch(() => {});
      };
      sampleStats();
      clearWebRtcStatsTimer();
      webrtcStatsTimerId = window.setInterval(sampleStats, 2000);
      try {
        await video.play();
      } catch {
        // autoplay can be blocked; user interaction will retry.
      }
    };

    const startHlsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      if (!hlsUrl) throw new Error(i18n.t("core.ui.streams.errors.hls_url_missing", {}, "HLS URL is not available."));
      if (canPlayNativeHls(video)) {
        await startNativeHlsPlayback(video);
        return;
      }
      await startHlsJsPlayback(video);
    };

    const primePlaybackOutput = async (
      selectedOutputId: string | null,
      selectedQualityProfileId: StreamingQualityProfileId | null,
    ) => {
      if (!transmissionId) return;
      try {
        await primeStreamingTransmissionDemand(transmissionId, {
          outputId: selectedOutputId,
          qualityProfileId: selectedQualityProfileId,
        });
        recordWebPlaybackEvent("demand_prime", {
          severity: "debug",
          data: withTransportTelemetry({ output_id: selectedOutputId, quality_profile_id: selectedQualityProfileId }),
        });
      } catch (primeError) {
        recordWebPlaybackEvent("demand_prime_error", {
          severity: "warn",
          message: asErrorMessage(primeError),
        });
        setErrorText(
          i18n.t(
            "core.ui.streams.errors.prime_failed",
            { error: asErrorMessage(primeError) },
            "Failed to prime stream: {{error}}",
          ),
        );
      }
    };

    const refreshSignedHlsAfterAuthError = async (message: string): Promise<boolean> => {
      if (!isHlsAuthProbeErrorMessage(message)) return false;
      const renewalMessage = i18n.t(
        "core.ui.streams.errors.hls_signed_url_renewing",
        {},
        "Secure HLS URL expired. Renewing...",
      );
      setStatus("loading");
      setErrorText(renewalMessage);
      recordWebPlaybackEvent("hls_auth_expired", {
        severity: "warn",
        message,
        data: withTransportTelemetry({ output_id: hlsOutputId ?? outputId }),
      });
      await onRefreshUrlsRef.current();
      return true;
    };

    const startPlayback = async () => {
      if (cancelled || !playbackActive || ((!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl))) return;
      const video = videoRef.current;
      if (!video) return;
      if (!playbackSessionIdRef.current) {
        playbackSessionIdRef.current = `${transmissionId || "stream"}:web:${Date.now()}:${Math.floor(Math.random() * 1_000_000)}`;
      }
      recordWebPlaybackEvent("load", {
        severity: "info",
        data: withTransportTelemetry({
          has_webrtc: Boolean(webrtcUrl),
          has_hls: Boolean(hlsUrl),
          selected_hls_output_id: hlsOutputId,
          selected_webrtc_output_id: webrtcOutputId,
          fallback_transport: preferWebRtcFirst && allowHls ? "hls" : null,
        }),
      });

      destroyPlayback();
      configureVideo(video);
      setStatus("loading");
      setErrorText(null);
      setTransport("none");
      setWebRtcFallbackActive(false);
      if (cancelled) return;

      let webRtcError: string | null = null;
      const startHlsWithTelemetry = async (fallbackFromWebRtcError: string | null): Promise<void> => {
        await primePlaybackOutput(hlsOutputId ?? outputId, hlsQualityProfileId);
        recordWebPlaybackEvent("hls_start", {
          severity: "info",
          data: withTransportTelemetry({
            output_id: hlsOutputId ?? outputId,
            quality_profile_id: hlsQualityProfileId,
            fallback_transport: fallbackFromWebRtcError ? "hls" : null,
            fallback_successful: fallbackFromWebRtcError ? true : null,
          }),
        });
        if (fallbackFromWebRtcError) {
          setWebRtcFallbackActive(true);
        }
        await startHlsPlayback(video);
        if (fallbackFromWebRtcError) {
          recordWebPlaybackEvent("webrtc_fallback_hls", {
            severity: "warn",
            message: fallbackFromWebRtcError,
            data: withTransportTelemetry({
              output_id: hlsOutputId ?? outputId,
              fallback_transport: "hls",
              fallback_successful: true,
              effective_transport: "hls_fallback",
            }),
          });
        }
      };

      if (!preferWebRtcFirst && allowHls && hlsUrl) {
        try {
          await startHlsWithTelemetry(null);
          return;
        } catch (error) {
          const hlsError = asErrorMessage(error);
          if (await refreshSignedHlsAfterAuthError(hlsError)) return;
          setStatus("error");
          setErrorText(hlsError);
          recordWebPlaybackEvent("playback_error", {
            severity: "error",
            message: hlsError,
            data: withTransportTelemetry({ output_id: hlsOutputId ?? outputId, playback_transport: "hls" }),
          });
          destroyPlayback();
          scheduleRetry(hlsError);
          return;
        }
      }

      if (preferWebRtcFirst && allowWebRtc && webrtcUrl) {
        await primePlaybackOutput(webrtcOutputId ?? outputId, null);
        for (let attemptIndex = 0; attemptIndex < WEBRTC_WHEP_READY_ATTEMPTS; attemptIndex += 1) {
          try {
            recordWebPlaybackEvent("webrtc_start", {
              severity: "info",
              data: withTransportTelemetry({
                attempt_index: attemptIndex,
                output_id: webrtcOutputId ?? outputId,
                fallback_transport: allowHls ? "hls" : null,
              }),
            });
            await startWebRtcPlayback(video);
            return;
          } catch (error) {
            const message = asErrorMessage(error);
            webRtcError = message;
            const normalizedMessage = message.toLowerCase();

            const shouldRetry =
              attemptIndex < WEBRTC_WHEP_READY_ATTEMPTS - 1 &&
              (normalizedMessage.includes("(404)") ||
                normalizedMessage.includes("no stream is available") ||
                normalizedMessage.includes("path has no one publishing"));
            recordWebPlaybackEvent("webrtc_signaling_error", {
              severity: shouldRetry ? "debug" : "warn",
              message,
              data: withTransportTelemetry({
                attempt_index: attemptIndex,
                output_id: webrtcOutputId ?? outputId,
                fallback_transport: allowHls ? "hls" : null,
                fallback_successful: false,
                retryable: shouldRetry,
              }),
            });
            if (!shouldRetry) break;

            await new Promise((resolve) => window.setTimeout(resolve, WEBRTC_WHEP_READY_RETRY_MS));
            if (cancelled) return;
          }
        }
      }

      if (allowHls && hlsUrl) {
        try {
          await startHlsWithTelemetry(webRtcError);
          return;
        } catch (error) {
          const hlsError = asErrorMessage(error);
          if (await refreshSignedHlsAfterAuthError(hlsError)) return;
          const combinedError = webRtcError
            ? i18n.t(
                "core.ui.streams.errors.webrtc_hls_fallback_failed",
                { webrtcError: webRtcError, hlsError },
                "WebRTC failed: {{webrtcError}}. HLS fallback failed: {{hlsError}}",
              )
            : hlsError;
          setStatus("error");
          setErrorText(combinedError);
          recordWebPlaybackEvent("playback_error", {
            severity: "error",
            message: combinedError,
            data: withTransportTelemetry({
              output_id: hlsOutputId ?? outputId,
              fallback_transport: webRtcError ? "hls" : null,
              fallback_successful: webRtcError ? false : null,
            }),
          });
          destroyPlayback();
          scheduleRetry(combinedError);
          return;
        }
      }

      const message =
        webRtcError || i18n.t("core.ui.streams.errors.no_supported_playback", {}, "No supported playback output available.");
      setStatus("error");
      setErrorText(message);
      recordWebPlaybackEvent("playback_error", {
        severity: "error",
        message,
        data: withTransportTelemetry({
          fallback_transport: webRtcError && allowHls ? "hls" : null,
          fallback_successful: webRtcError && allowHls ? false : null,
        }),
      });
      destroyPlayback();
      scheduleRetry(message);
    };

    if (!playbackActive || ((!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl))) {
      attempt = 0;
      recordWebPlaybackEvent("stop", { severity: "info" });
      playbackSessionIdRef.current = null;
      destroyPlayback();
      setStatus("idle");
      setTransport("none");
      setErrorText(null);
      setWebRtcFallbackActive(false);
      setHlsProbeSummary(null);
      return () => {
        cancelled = true;
        destroyPlayback();
      };
    }

    attempt = 0;
    void startPlayback();

    return () => {
      cancelled = true;
      recordWebPlaybackEvent("stop", { severity: "debug" });
      playbackSessionIdRef.current = null;
      destroyPlayback();
    };
  }, [
    hlsAuthHeader,
    hlsNativeUrl,
    hlsQualityProfileId,
    hlsUrl,
    hlsOutputId,
    outputId,
    allowHls,
    allowWebRtc,
    preferWebRtcFirst,
    playbackActive,
    playbackPlan.effectiveMode,
    playbackPlan.webRtcBlocked,
    playbackPlan.webRtcIssueMessages.length,
    recordWebPlaybackEvent,
    transmissionId,
    withTransportTelemetry,
    webrtcAuthHeader,
    webrtcOutputId,
    webrtcUrl,
  ]);

  const playbackStatusLabel = useMemo(() => {
    const onlineLabel = t("core.ui.streams.status.online", {}, "online");
    const waitingLabel = t("core.ui.streams.status.waiting", {}, "waiting");
    const statusLabel =
      status === "idle"
        ? waitingLabel
        : status === "loading"
          ? t("core.ui.streams.status.loading", {}, "loading")
          : status === "error"
            ? t("core.ui.streams.status.error", {}, "error")
            : status === "unsupported"
              ? t("core.ui.streams.status.unsupported", {}, "unsupported")
              : waitingLabel;

    const transportLabel =
      transport === "webrtc"
        ? t("core.ui.streams.transport.webrtc", {}, "WebRTC")
        : transport === "hls"
          ? t("core.ui.streams.transport.hls", {}, "HLS")
          : onlineLabel;

    const healthLabel = runtimeStatusLabel(runtimeHealth?.status, t, Boolean(runtimeHealth?.event_gated_idle));
    if (status === "playing") {
      return healthLabel ?? (transport === "none" ? onlineLabel : transportLabel);
    }
    if (transport === "none") return statusLabel;
    return t(
      "core.ui.streams.status.with_transport",
      { status: statusLabel, transport: transportLabel },
      `${statusLabel} (${transportLabel})`,
    );
  }, [runtimeHealth?.event_gated_idle, runtimeHealth?.status, status, t, transport]);

  const playbackDotStatus = useMemo<TilePlaybackStatus>(() => {
    if (status === "error" || status === "unsupported") return "error";
    if (runtimeHealth?.event_gated_idle) return "loading";
    if (runtimeHealth?.status === "stale" || runtimeHealth?.status === "offline") return "error";
    if (runtimeHealth?.status === "degraded") return "loading";
    return status;
  }, [runtimeHealth?.event_gated_idle, runtimeHealth?.status, status]);

  const toggleFullscreen = async () => {
    const el = frameRef.current;
    if (!el) return;
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        return;
      }
      await el.requestFullscreen();
      const canUpgrade =
        qualityPreference === "auto" &&
        (runtimeHealth?.status === undefined || runtimeHealth.status === "live" || runtimeHealth.status === "degraded") &&
        runtimeHealth?.stale !== true &&
        runtimeHealth?.classification !== "source_stale" &&
        runtimeHealth?.classification !== "publisher_down";
      if (canUpgrade) {
        onQualityPreferenceChange("high");
      }
    } catch {
      // ignore
    }
  };

  const togglePip = async () => {
    const video = videoRef.current;
    if (!video) return;

    try {
      const anyDoc = document as unknown as { pictureInPictureElement?: Element | null; pictureInPictureEnabled?: boolean; exitPictureInPicture?: () => Promise<void> };
      if (anyDoc.pictureInPictureElement && typeof anyDoc.exitPictureInPicture === "function") {
        await anyDoc.exitPictureInPicture();
        return;
      }

      const anyVideo = video as unknown as { requestPictureInPicture?: () => Promise<void>; webkitSetPresentationMode?: (mode: string) => void; webkitPresentationMode?: string };
      if (anyDoc.pictureInPictureEnabled && typeof anyVideo.requestPictureInPicture === "function") {
        await anyVideo.requestPictureInPicture();
        return;
      }
      if (typeof anyVideo.webkitSetPresentationMode === "function") {
        const current = String(anyVideo.webkitPresentationMode || "inline");
        anyVideo.webkitSetPresentationMode(current === "picture-in-picture" ? "inline" : "picture-in-picture");
      }
    } catch {
      // ignore
    }
  };

  const pipSupported =
    typeof document !== "undefined" &&
    (Boolean((document as any).pictureInPictureEnabled && (HTMLVideoElement.prototype as any).requestPictureInPicture) ||
      Boolean((HTMLVideoElement.prototype as any).webkitSetPresentationMode));

  const fullscreenSupported = typeof document !== "undefined" && Boolean((document as any).fullscreenEnabled && (HTMLElement.prototype as any).requestFullscreen);
  const openAdvancedSettings = () => {
    if (typeof document !== "undefined" && document.fullscreenElement) {
      void document.exitFullscreen().catch(() => undefined);
    }
    setAdvancedOpen(true);
  };

  return (
    <div className="streamsPlayerFrame" ref={frameRef}>
      <video ref={videoRef} className="streamsVideo" muted playsInline autoPlay />

	      <div className={["streamsTileOverlay", overlayVisible ? "isVisible" : "isHidden"].join(" ")}>
        <div className="streamsTileOverlayLeft" title={label}>
          <span className={["streamsPlaybackDot", `is-${playbackDotStatus}`].join(" ")} />
          <span className="streamsTileOverlayTitle">{label}</span>
          <span className="streamsTileOverlayMeta">{playbackStatusLabel}</span>
          {webRtcFallbackActive ? (
            <span className="streamsTileOverlayMeta" title={t("core.ui.streams.transport.hls_fallback_hint", {}, "Low latency unavailable; using HLS fallback.")}>
              {t("core.ui.streams.transport.hls_fallback", {}, "HLS fallback")}
            </span>
          ) : null}
        </div>

	        <div className="streamsTileOverlayActions">
          <button
            type="button"
            className="iconButton streamsTileOverlayButton"
            aria-label={t("core.ui.streams.actions.advanced", {}, "Advanced stream settings")}
            title={t("core.ui.streams.actions.advanced", {}, "Advanced stream settings")}
            onClick={openAdvancedSettings}
          >
            <Icon name="sliders" />
          </button>
	          {ptzEnabled ? (
	            <button
	              type="button"
	              className="iconButton streamsTileOverlayButton"
	              aria-label={t("core.ui.streams.actions.ptz", {}, "Camera controls")}
	              title={t("core.ui.streams.actions.ptz", {}, "Camera controls")}
	              onClick={onOpenPtz}
	            >
	              <Icon name="arrows-up-down-left-right" />
	            </button>
	          ) : null}
	          <button
	            type="button"
	            className="iconButton streamsTileOverlayButton"
	            aria-label={t("core.ui.streams.actions.pip", {}, "Picture-in-picture")}
	            title={t("core.ui.streams.actions.pip", {}, "Picture-in-picture")}
	            onClick={togglePip}
	            disabled={!pipSupported}
	          >
	            <Icon name="window-restore" />
	          </button>
	          <button
	            type="button"
	            className="iconButton streamsTileOverlayButton"
	            aria-label={t("core.ui.streams.actions.fullscreen", {}, "Fullscreen")}
	            title={t("core.ui.streams.actions.fullscreen", {}, "Fullscreen")}
	            onClick={toggleFullscreen}
	            disabled={!fullscreenSupported}
	          >
	            <Icon name="up-right-and-down-left-from-center" />
	          </button>
        </div>
      </div>

      {overlayVisible && (sourceHint || errorText) ? (
        <div className={["streamsTileOverlayHint", `is-${errorText ? "error" : sourceHintTone}`].join(" ")}>
          {errorText || sourceHint}
        </div>
      ) : null}

      <StreamAdvancedSettingsModal
        open={advancedOpen}
        label={label}
        onClose={() => setAdvancedOpen(false)}
        urls={urls}
        runtimeHealth={runtimeHealth}
        hlsOutputId={hlsOutputId}
        webrtcOutputId={webrtcOutputId}
        selectedOutputId={outputId}
        hlsQualityProfileId={hlsQualityProfileId}
        playbackStatus={status}
        playbackTransport={transport}
        webRtcStats={webRtcStats}
        webRtcFallbackActive={webRtcFallbackActive}
        hlsProbeSummary={hlsProbeSummary}
        hlsUrl={hlsUrl}
        playbackPlan={playbackPlan}
        lowLatencyRequested={lowLatencyRequested}
        errorText={errorText}
        sourceHint={sourceHint}
        qualityPreference={qualityPreference}
        transportPreference={transportPreference}
        onQualityPreferenceChange={onQualityPreferenceChange}
        onTransportPreferenceChange={onTransportPreferenceChange}
      />
    </div>
  );
}

export function StreamsDashboard({ uiVisible, isActive }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [gridMode, setGridMode] = useState<GridMode>(() => readGridMode());
  const [pageIndex, setPageIndex] = useState(0);

  const [transmissions, setTransmissions] = useState<StreamingTransmission[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [urlsByTransmissionId, setUrlsByTransmissionId] = useState<Record<string, StreamingTransmissionUrlsResponse>>({});
  const [urlsLoadingByTransmissionId, setUrlsLoadingByTransmissionId] = useState<Record<string, boolean>>({});
  const [urlErrorByTransmissionId, setUrlErrorByTransmissionId] = useState<Record<string, string>>({});
  const [runtimeHealthByTransmissionId, setRuntimeHealthByTransmissionId] = useState<Record<string, StreamingRuntimeTransmissionHealth>>({});
  const [runtimeHealthError, setRuntimeHealthError] = useState<string | null>(null);
  const [qualityPreferenceByTransmissionId, setQualityPreferenceByTransmissionId] = useState<
    Record<string, StreamQualityPreference>
  >(() => readQualityPreferenceByTransmissionId());
  const [transportPreferenceByTransmissionId, setTransportPreferenceByTransmissionId] = useState<
    Record<string, StreamTransportPreference>
  >(() => readTransportPreferenceByTransmissionId());

  const [tabVisible, setTabVisible] = useState<boolean>(() => {
    if (typeof document === "undefined") return true;
    return document.visibilityState === "visible";
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      localStorage.setItem(GRID_MODE_STORAGE_KEY, gridMode);
    } catch {
      // ignore
    }
  }, [gridMode]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      localStorage.setItem(QUALITY_PREFERENCE_STORAGE_KEY, JSON.stringify(qualityPreferenceByTransmissionId));
    } catch {
      // ignore
    }
  }, [qualityPreferenceByTransmissionId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      localStorage.setItem(TRANSPORT_PREFERENCE_STORAGE_KEY, JSON.stringify(transportPreferenceByTransmissionId));
    } catch {
      // ignore
    }
  }, [transportPreferenceByTransmissionId]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisibilityChange = () => setTabVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadTransmissions = async (isFirstLoad: boolean) => {
      if (isFirstLoad) setLoading(true);
      try {
        const payload = await listStreamingTransmissions();
        if (cancelled) return;
        setTransmissions(Array.isArray(payload) ? payload : []);
        setError(null);
      } catch (loadError) {
        if (cancelled) return;
        setError(asErrorMessage(loadError));
      } finally {
        if (!cancelled && isFirstLoad) setLoading(false);
      }
    };

    void loadTransmissions(true);
    const intervalId = window.setInterval(() => {
      void loadTransmissions(false);
    }, TRANSMISSIONS_REFRESH_MS);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  const enabledTransmissions = useMemo(
    () => transmissions.filter((item) => item && item.enabled !== false),
    [transmissions],
  );

  const pageSize = gridMode === "1x1" ? 1 : 4;
  const pageCount = Math.max(1, Math.ceil(enabledTransmissions.length / pageSize));

  useEffect(() => {
    setPageIndex((previous) => Math.min(previous, Math.max(0, pageCount - 1)));
  }, [pageCount]);

  const currentPageItems = useMemo(() => {
    const start = pageIndex * pageSize;
    return enabledTransmissions.slice(start, start + pageSize);
  }, [enabledTransmissions, pageIndex, pageSize]);

  const currentPageTransmissionIds = useMemo(
    () => currentPageItems.map((item) => String(item.id || "").trim()).filter(Boolean),
    [currentPageItems],
  );

  useEffect(() => {
    for (const transmission of currentPageItems) {
      const transmissionId = String(transmission.id || "").trim();
      if (!transmissionId) continue;
      if (urlsByTransmissionId[transmissionId]) continue;
      if (urlsLoadingByTransmissionId[transmissionId]) continue;
      const qualityPreference = qualityPreferenceByTransmissionId[transmissionId] ?? "auto";
      const qualityProfileId = transmissionHasProfiledHls(transmission)
        ? qualityProfileIdForPreference(qualityPreference, gridMode)
        : null;

      setUrlsLoadingByTransmissionId((previous) => ({ ...previous, [transmissionId]: true }));
      void getStreamingTransmissionUrls(transmissionId, { qualityProfileId })
        .then((payload) => {
          setUrlsByTransmissionId((previous) => ({ ...previous, [transmissionId]: payload }));
          setUrlErrorByTransmissionId((previous) => {
            if (!previous[transmissionId]) return previous;
            const next = { ...previous };
            delete next[transmissionId];
            return next;
          });
        })
        .catch((loadError) => {
          setUrlErrorByTransmissionId((previous) => ({
            ...previous,
            [transmissionId]: asErrorMessage(loadError),
          }));
        })
        .finally(() => {
          setUrlsLoadingByTransmissionId((previous) => ({ ...previous, [transmissionId]: false }));
      });
    }
  }, [currentPageItems, gridMode, qualityPreferenceByTransmissionId, urlsByTransmissionId, urlsLoadingByTransmissionId]);

  useEffect(() => {
    if (!tabVisible || currentPageTransmissionIds.length === 0) return;

    const inFlight = new Set<string>();
    const renewSignedUrls = () => {
      const nowUnix = Date.now() / 1000;
      for (const transmissionId of currentPageTransmissionIds) {
        const urls = urlsByTransmissionId[transmissionId];
        const qualityPreference = qualityPreferenceByTransmissionId[transmissionId] ?? "auto";
        const qualityProfileId = qualityProfileIdForPreference(qualityPreference, gridMode);
        const signedHlsOutput = urls?.outputs?.find(
          (output) =>
            output.protocol === "hls" &&
            (!hlsOutputsHaveProfiles(urls) || output.quality_profile_id === qualityProfileId) &&
            output.media_auth_type === "signed_url" &&
            typeof output.renew_after_unix === "number" &&
            nowUnix >= Number(output.renew_after_unix),
        );
        if (!signedHlsOutput || inFlight.has(transmissionId)) continue;

        inFlight.add(transmissionId);
        const hasProfiledHls = hlsOutputsHaveProfiles(urls);
        void getStreamingTransmissionUrls(transmissionId, {
          outputId: hasProfiledHls ? null : signedHlsOutput.output_id,
          qualityProfileId: hasProfiledHls ? signedHlsOutput.quality_profile_id ?? qualityProfileId : null,
        })
          .then((payload) => {
            setUrlsByTransmissionId((previous) => ({ ...previous, [transmissionId]: payload }));
            setUrlErrorByTransmissionId((previous) => {
              if (!previous[transmissionId]) return previous;
              const next = { ...previous };
              delete next[transmissionId];
              return next;
            });
          })
          .catch((loadError) => {
            setUrlErrorByTransmissionId((previous) => ({
              ...previous,
              [transmissionId]: asErrorMessage(loadError),
            }));
          })
          .finally(() => {
            inFlight.delete(transmissionId);
          });
      }
    };

    renewSignedUrls();
    const interval = window.setInterval(renewSignedUrls, 10_000);
    return () => window.clearInterval(interval);
  }, [currentPageTransmissionIds, gridMode, qualityPreferenceByTransmissionId, tabVisible, urlsByTransmissionId]);

  useEffect(() => {
    if (!tabVisible || currentPageTransmissionIds.length === 0) {
      return;
    }

    let cancelled = false;
    const visibleIds = new Set(currentPageTransmissionIds);
    const loadRuntimeHealth = async () => {
      try {
        const payload = await getStreamingRuntimeHealth();
        if (cancelled) return;
        const next: Record<string, StreamingRuntimeTransmissionHealth> = {};
        for (const item of payload.transmissions ?? []) {
          const transmissionId = String(item.transmission_id || "").trim();
          if (!visibleIds.has(transmissionId)) continue;
          next[transmissionId] = item;
        }
        setRuntimeHealthByTransmissionId((previous) => ({ ...previous, ...next }));
        setRuntimeHealthError(null);
      } catch (loadError) {
        if (cancelled) return;
        setRuntimeHealthError(asErrorMessage(loadError));
      }
    };

    void loadRuntimeHealth();
    const intervalId = window.setInterval(() => {
      void loadRuntimeHealth();
    }, RUNTIME_HEALTH_REFRESH_MS);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [currentPageTransmissionIds, tabVisible]);

  const pageTiles = useMemo(() => {
    const out: Array<StreamingTransmission | null> = [...currentPageItems];
    while (out.length < pageSize) out.push(null);
    return out;
  }, [currentPageItems, pageSize]);

  const canGoPrev = pageIndex > 0;
  const canGoNext = pageIndex < pageCount - 1;
  const playersActive = isActive && tabVisible;
  const [ptzOverlay, setPtzOverlay] = useState<{ transmissionId: string; label: string } | null>(null);
  const closePtzOverlay = useCallback(() => setPtzOverlay(null), []);

  if (loading) {
    return (
      <div className="viewportRoot streamsRoot">
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">{t("core.ui.loading", {}, "Loading...")}</div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="viewportRoot streamsRoot">
      {error ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">
              {t("core.ui.streams.unavailable", {}, "Streaming extension unavailable.")} {error}
            </div>
          </div>
        </div>
      ) : null}

      {!error && enabledTransmissions.length === 0 ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">
              {t("core.ui.streams.empty", {}, "No enabled transmissions. Create one in Settings > Transmissions.")}
            </div>
          </div>
        </div>
      ) : null}

      {!error && enabledTransmissions.length > 0 ? (
        <div className={["streamsGrid", gridMode === "1x1" ? "is1x1" : "is2x2"].join(" ")}>
          {pageTiles.map((transmission, slotIndex) => {
            if (!transmission) {
              return <div key={`slot-empty-${slotIndex}`} className="streamsTile streamsTileEmpty" />;
            }

            const transmissionId = String(transmission.id || "").trim();
            const qualityPreference = qualityPreferenceByTransmissionId[transmissionId] ?? "auto";
            const transportPreference = transportPreferenceByTransmissionId[transmissionId] ?? "auto";
            const desiredQualityProfileId = qualityProfileIdForPreference(qualityPreference, gridMode);
            const transmissionName = normalizeText(
              transmission.name,
              normalizeText(transmission.path, transmissionId || `stream-${slotIndex + 1}`),
            );
            const urls = urlsByTransmissionId[transmissionId];
            const hlsOutput = selectOutputByProtocol(transmission, urls, "hls", {
              qualityProfileId: desiredQualityProfileId,
            });
            const webrtcOutput = selectOutputByProtocol(transmission, urls, "webrtc");
            const urlError = urlErrorByTransmissionId[transmissionId];
            const urlLoading = Boolean(urlsLoadingByTransmissionId[transmissionId]);
            const runtimeHealth = runtimeHealthByTransmissionId[transmissionId];
            const webrtcUrl = webrtcOutput?.url ?? null;
            const hlsUrl = hlsOutput?.url ?? null;
            const webrtcAuthHeader = buildBasicAuthHeader(webrtcOutput?.auth ?? null);
            const hlsAuthHeader = buildBasicAuthHeader(hlsOutput?.auth ?? null);
            const hlsNativeUrl = hlsUrl ? withBasicAuthInUrl(hlsUrl, hlsOutput?.auth ?? null) : null;
            const tileActive =
              playersActive &&
              Boolean(
                (transportPreference !== "hls" && webrtcUrl) ||
                  (transportPreference !== "webrtc" && hlsUrl),
              );
            const ptzEnabled = Boolean(transmission.camera_controls?.enabled);
            const lowLatencyRequested = ptzOverlay?.transmissionId === transmissionId && transportPreference === "auto";
            const runtimeHint = buildRuntimeHealthHint(runtimeHealth, t, {
              suppressRecoveredClientTransportErrors: transportPreference !== "webrtc",
            });

            let sourceHint: string | null = null;
            let sourceHintTone: "muted" | "warn" | "error" = "muted";
            if (urlLoading) {
              sourceHint = t("core.ui.streams.hint.loading_url", {}, "Loading stream URL…");
              sourceHintTone = "muted";
            } else if (urlError) {
              sourceHint = urlError;
              sourceHintTone = "error";
            } else if (
              (transportPreference === "webrtc" && !webrtcUrl) ||
              (transportPreference === "hls" && !hlsUrl) ||
              (transportPreference === "auto" && !webrtcUrl && !hlsUrl)
            ) {
              sourceHint = t("core.ui.streams.hint.no_outputs", {}, "No WebRTC/HLS output configured for this transmission.");
              sourceHintTone = "warn";
            } else if (runtimeHint) {
              sourceHint = runtimeHint.message;
              sourceHintTone = runtimeHint.tone;
            } else if (runtimeHealthError) {
              sourceHint = runtimeHealthError;
              sourceHintTone = "warn";
            }

            return (
              <div key={transmissionId} className="streamsTile">
                <StreamTilePlayer
                  transmissionId={transmissionId}
                  outputId={webrtcOutput?.outputId ?? hlsOutput?.outputId ?? null}
                  webrtcOutputId={webrtcOutput?.outputId ?? null}
                  hlsOutputId={hlsOutput?.outputId ?? null}
                  hlsQualityProfileId={hlsOutput?.qualityProfileId ?? null}
                  label={transmissionName}
                  urls={urls}
                  overlayVisible={uiVisible}
                  sourceHint={sourceHint}
                  sourceHintTone={sourceHintTone}
                  webrtcUrl={webrtcUrl}
                  webrtcAuthHeader={webrtcAuthHeader}
                  hlsUrl={hlsUrl}
                  hlsAuthHeader={hlsAuthHeader}
                  hlsNativeUrl={hlsNativeUrl}
                  runtimeHealth={runtimeHealth}
                  active={tileActive}
                  ptzEnabled={ptzEnabled}
                  lowLatencyRequested={lowLatencyRequested}
                  qualityPreference={qualityPreference}
                  transportPreference={transportPreference}
                  onQualityPreferenceChange={(preference) => {
                    setQualityPreferenceByTransmissionId((previous) => ({
                      ...previous,
                      [transmissionId]: preference,
                    }));
                    setUrlsByTransmissionId((previous) => {
                      if (!previous[transmissionId]) return previous;
                      const next = { ...previous };
                      delete next[transmissionId];
                      return next;
                    });
                  }}
                  onTransportPreferenceChange={(preference) => {
                    setTransportPreferenceByTransmissionId((previous) => ({
                      ...previous,
                      [transmissionId]: preference,
                    }));
                  }}
                  onRefreshUrls={async () => {
                    setUrlsLoadingByTransmissionId((previous) => ({ ...previous, [transmissionId]: true }));
                    try {
                      const hasProfiledHls = hlsOutputsHaveProfiles(urls);
                      const payload = await getStreamingTransmissionUrls(transmissionId, {
                        outputId: hasProfiledHls ? null : hlsOutput?.outputId ?? null,
                        qualityProfileId: hasProfiledHls ? desiredQualityProfileId : null,
                      });
                      setUrlsByTransmissionId((previous) => ({ ...previous, [transmissionId]: payload }));
                      setUrlErrorByTransmissionId((previous) => {
                        if (!previous[transmissionId]) return previous;
                        const next = { ...previous };
                        delete next[transmissionId];
                        return next;
                      });
                    } catch (refreshError) {
                      setUrlErrorByTransmissionId((previous) => ({
                        ...previous,
                        [transmissionId]: asErrorMessage(refreshError),
                      }));
                      throw refreshError;
                    } finally {
                      setUrlsLoadingByTransmissionId((previous) => ({ ...previous, [transmissionId]: false }));
                    }
                  }}
                  onOpenPtz={() => {
                    setPtzOverlay({ transmissionId, label: transmissionName });
                  }}
                />
              </div>
            );
          })}
        </div>
      ) : null}

      <StreamsPtzOverlay
        open={ptzOverlay !== null}
        transmissionId={ptzOverlay?.transmissionId ?? ""}
        label={ptzOverlay?.label ?? ""}
        onClose={closePtzOverlay}
      />

      <div className={["streamsHud", uiVisible ? "isVisible" : "isHidden"].join(" ")}>
        <div className="streamsHudGroup">
          <button
            type="button"
            className={["chipButton", "streamsHudModeButton", gridMode === "1x1" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-pressed={gridMode === "1x1"}
            onClick={() => {
              setGridMode("1x1");
              setPageIndex(0);
            }}
          >
            1x1
          </button>
          <button
            type="button"
            className={["chipButton", "streamsHudModeButton", gridMode === "2x2" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-pressed={gridMode === "2x2"}
            onClick={() => {
              setGridMode("2x2");
              setPageIndex(0);
            }}
          >
            2x2
          </button>
        </div>

        <div className="streamsHudGroup streamsHudPager">
          <button
            type="button"
            className="iconButton streamsHudNavButton"
            onClick={() => setPageIndex((previous) => Math.max(0, previous - 1))}
            disabled={!canGoPrev}
            aria-label={t("core.ui.streams.prev_page", {}, "Previous page")}
          >
            <Icon name="chevron-left" />
          </button>
          <div className="streamsHudPageLabel">
            {t("core.ui.streams.page", { current: pageIndex + 1, total: pageCount }, `${pageIndex + 1}/${pageCount}`)}
          </div>
          <button
            type="button"
            className="iconButton streamsHudNavButton"
            onClick={() => setPageIndex((previous) => Math.min(pageCount - 1, previous + 1))}
            disabled={!canGoNext}
            aria-label={t("core.ui.streams.next_page", {}, "Next page")}
          >
            <Icon name="chevron-right" />
          </button>
        </div>
      </div>
    </div>
  );
}
