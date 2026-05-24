import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type Hls from "hls.js";

import {
  getStreamingCameraLiveViewPlayback,
  getStreamingRuntimeHealth,
  heartbeatStreamingTransmissionDemand,
  listStreamingCameraLiveViews,
  postStreamingPlaybackEvents,
  primeStreamingTransmissionDemand,
  updateStreamingCameraLiveView,
  type StreamingCameraLiveContext,
  type StreamingCameraLiveVariant,
  type StreamingCameraLiveView,
  type StreamingCameraLiveViewPlaybackResponse,
  type StreamingPlaybackPlanResponse,
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
import { createJsmpegPlayer } from "./jsmpegPlayer";

type GridMode = "1x1" | "2x2";
export type StreamsDashboardContext = StreamingCameraLiveContext;

type Props = {
  uiVisible: boolean;
  isActive: boolean;
  embedded?: boolean;
  cameraId?: string;
  liveViewId?: string;
  defaultContext?: StreamingCameraLiveContext;
};

type TilePlaybackStatus = "idle" | "loading" | "playing" | "error" | "unsupported";
type TileHealthTone = "muted" | "warn" | "error";
type TilePlaybackTransport = "none" | "mse" | "webrtc" | "hls" | "jsmpeg";
type StreamProtocol = "hls" | "rtsp" | "webrtc" | "mse" | "jsmpeg";
type StreamQualityPreference = "auto" | "low" | "stable" | "high" | "diagnostic";
type StreamTransportPreference = "auto" | "webrtc" | "hls";
type EffectiveTransportMode = "auto_mse" | "auto_hls" | "auto_webrtc" | "auto_jsmpeg" | "ptz_webrtc" | "mse" | "hls" | "webrtc" | "jsmpeg" | "hls_fallback" | "auto";
type TranslateFn = ReturnType<typeof i18n.useI18n>["t"];
type LiveVariantQuickOption = {
  id: string;
  label: string;
  title: string;
};

type StreamPlaybackPlan = {
  allowMse: boolean;
  allowHls: boolean;
  allowWebRtc: boolean;
  allowJsmpeg: boolean;
  preferMseFirst: boolean;
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
const LIVE_VARIANT_OVERRIDE_STORAGE_KEY = "toposync.streams.live_variant_override.v1";
const TRANSMISSIONS_REFRESH_MS = 15000;
const RETRY_BASE_MS = 900;
const RETRY_MAX_MS = 8000;
const WEBRTC_SIGNAL_TIMEOUT_MS = 5000;
const WEBRTC_CONNECT_TIMEOUT_MS = 5000;
const WEBRTC_WHEP_READY_ATTEMPTS = 8;
const WEBRTC_WHEP_READY_RETRY_MS = 500;
const RUNTIME_HEALTH_REFRESH_MS = 2000;
const HLS_BROWSER_PROBE_TIMEOUT_MS = 2500;
const HLS_LIVENESS_STALE_GRACE_MS = 5000;
const HLS_PLAYBACK_WARMUP_MS = 12000;
const MSE_INIT_TIMEOUT_MS = 16000;
const MSE_FIRST_FRAME_TIMEOUT_MS = 12000;
const MSE_CONNECT_ATTEMPTS = 3;
const MSE_RETRY_DELAY_MS = 900;
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

function readLiveVariantOverrideByLiveViewId(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(String(localStorage.getItem(LIVE_VARIANT_OVERRIDE_STORAGE_KEY) || "{}"));
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, string> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const normalizedKey = String(key || "").trim();
      const normalizedValue = String(value || "").trim();
      if (normalizedKey && normalizedValue) out[normalizedKey] = normalizedValue;
    }
    return out;
  } catch {
    return {};
  }
}

function playbackKeyFor(liveViewId: string, context: StreamingCameraLiveContext, variantId: string | null | undefined): string {
  return `${liveViewId}:${context}:${String(variantId || "").trim() || "auto"}`;
}

function parsePlaybackKey(key: string): {
  liveViewId: string;
  context: StreamingCameraLiveContext;
  variantId: string | null;
} {
  const [liveViewId = "", rawContext = "thumbnail", rawVariantId = "auto"] = String(key || "").split(":");
  const context =
    rawContext === "pip" ||
    rawContext === "large" ||
    rawContext === "fullscreen" ||
    rawContext === "ptz"
      ? rawContext
      : "thumbnail";
  const variantId = rawVariantId && rawVariantId !== "auto" ? rawVariantId : null;
  return { liveViewId, context, variantId };
}

function defaultVariantIdForContext(liveView: StreamingCameraLiveView, context: StreamingCameraLiveContext): string {
  if (context === "pip") return String(liveView.defaults?.pip_variant_id || "").trim();
  if (context === "large") return String(liveView.defaults?.large_variant_id || "").trim();
  if (context === "fullscreen") return String(liveView.defaults?.fullscreen_variant_id || "").trim();
  if (context === "ptz") return String(liveView.defaults?.ptz_variant_id || "").trim();
  return String(liveView.defaults?.thumbnail_variant_id || "").trim();
}

function liveViewWithDefaultVariant(
  liveView: StreamingCameraLiveView,
  context: StreamingCameraLiveContext,
  variantId: string,
): StreamingCameraLiveView {
  const defaults = { ...liveView.defaults };
  if (context === "pip") defaults.pip_variant_id = variantId;
  else if (context === "large") defaults.large_variant_id = variantId;
  else if (context === "fullscreen") defaults.fullscreen_variant_id = variantId;
  else if (context === "ptz") defaults.ptz_variant_id = variantId;
  else defaults.thumbnail_variant_id = variantId;
  return { ...liveView, defaults };
}

function liveVariantRoleLabel(role: string | null | undefined, t: TranslateFn): string {
  if (role === "main") return t("core.ui.streams.variant.main", {}, "Principal");
  if (role === "sub") return t("core.ui.streams.variant.sub", {}, "Baixa resolução");
  if (role === "thumbnail") return t("core.ui.streams.variant.low", {}, "Low");
  if (role === "pip") return t("core.ui.streams.variant.stable", {}, "Stable");
  if (role === "large" || role === "fullscreen") return t("core.ui.streams.variant.high", {}, "High");
  if (role === "zoom") return t("core.ui.streams.variant.zoom", {}, "Zoom");
  if (role === "ptz") return t("core.ui.streams.variant.low_latency", {}, "Low latency");
  return t("core.ui.streams.variant.custom", {}, "Custom");
}

function liveContextLabel(context: StreamingCameraLiveContext, t: TranslateFn): string {
  if (context === "pip") return t("core.ui.streams.context.pip", {}, "PiP");
  if (context === "large") return t("core.ui.streams.context.large", {}, "Large");
  if (context === "fullscreen") return t("core.ui.streams.context.fullscreen", {}, "Fullscreen");
  if (context === "ptz") return t("core.ui.streams.context.ptz", {}, "PTZ");
  return t("core.ui.streams.context.thumbnail", {}, "Thumbnail");
}

function liveVariantQuickLabel(variant: StreamingCameraLiveVariant, t: TranslateFn): string {
  const label = String(variant.label || "").trim();
  if (label) return label;
  return liveVariantRoleLabel(variant.role, t);
}

function qualityProfileIdForPreference(
  preference: StreamQualityPreference,
  gridMode: GridMode,
): StreamingQualityProfileId {
  if (preference === "low") return "quad_grid";
  if (preference === "stable") return "stable_apple_tv";
  if (preference === "high") return "fullscreen_quality";
  if (preference === "diagnostic") return "diagnostic_low";
  return gridMode === "2x2" ? "quad_grid" : "fullscreen_quality";
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
  if (mode === "auto_mse") return t("core.ui.streams.transport.effective_auto_mse", {}, "Auto -> MSE");
  if (mode === "auto_hls") return t("core.ui.streams.transport.effective_auto_hls", {}, "Auto -> HLS");
  if (mode === "auto_webrtc") return t("core.ui.streams.transport.effective_auto_webrtc", {}, "Auto -> WebRTC");
  if (mode === "auto_jsmpeg") return "Auto -> JSMpeg";
  if (mode === "ptz_webrtc") return t("core.ui.streams.transport.effective_ptz_webrtc", {}, "PTZ -> WebRTC");
  if (mode === "mse") return "MSE";
  if (mode === "hls") return t("core.ui.streams.transport.hls", {}, "HLS");
  if (mode === "webrtc") return t("core.ui.streams.transport.low_latency", {}, "Low latency");
  if (mode === "jsmpeg") return "JSMpeg";
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

function sourceHealthBlockingMessage(
  sourceHealth: StreamingRuntimeTransmissionHealth["source_health"] | undefined,
  t: TranslateFn,
): string {
  if (!sourceHealth) return "";
  const blockingErrors = (sourceHealth.ingest_blocking_errors ?? [])
    .map((item) => String(item || "").trim())
    .filter(Boolean);
  if (blockingErrors.length > 0) {
    const details = blockingErrors.slice(0, 2).join(" ");
    return t(
      "core.ui.streams.health.camera_source_blocked",
      { details },
      `Camera source is not feeding frames through the configured ingest/source. ${details}`,
    );
  }
  const status = String(sourceHealth.status || "unknown").trim().toLowerCase();
  if (status && status !== "healthy") {
    return (
      sourceHealthRecommendedActionLabel(sourceHealth.recommended_action, t) ||
      sourceHealth.last_error ||
      t(
        "core.ui.streams.health.camera_source_not_feeding",
        {},
        "Camera source is not feeding frames yet.",
      )
    );
  }
  return "";
}

function runtimeHasFreshSourceAndWriter(health: StreamingRuntimeTransmissionHealth | undefined): boolean {
  if (!health) return false;
  const activeWriter = String(health.active_writer_id || "").trim();
  const selectedWriter = String(health.selected_writer_id || "").trim();
  if (!activeWriter && !selectedWriter) return false;
  if (health.stale || health.status === "stale") return false;
  const sourceHealth = health.source_health ?? null;
  if (sourceHealth?.ingest_blocking_errors?.some((item) => String(item || "").trim())) return false;
  const sourceStatus = String(sourceHealth?.status || "").trim().toLowerCase();
  if (["stale", "unreachable", "unauthorized", "error"].includes(sourceStatus)) return false;
  const selectedAge = health.selected_frame_age_seconds;
  if (typeof selectedAge === "number" && Number.isFinite(selectedAge)) return selectedAge <= 10;
  const sourceAge = sourceHealth?.source_frame_age_seconds;
  if (typeof sourceAge === "number" && Number.isFinite(sourceAge)) return sourceAge <= 10;
  return health.status !== "offline" || Boolean(activeWriter || selectedWriter);
}

function isHlsWarmupRecoverableError(
  message: string,
  health: StreamingRuntimeTransmissionHealth | undefined,
): boolean {
  if (isHlsAuthProbeErrorMessage(message)) return false;
  if (!runtimeHasFreshSourceAndWriter(health)) return false;
  const lowered = String(message || "").toLowerCase();
  if (lowered.includes("expired") || lowered.includes("forbidden") || lowered.includes("unauthorized")) return false;
  return (
    lowered.includes("failed to fetch") ||
    lowered.includes("playlist probe failed") ||
    lowered.includes("tail segment probe failed") ||
    lowered.includes("no media segment") ||
    lowered.includes("hls.js fatal") ||
    lowered.includes("native hls playback error") ||
    lowered.includes("network")
  );
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
  const scopedWarnings = urls?.webrtc_warnings ?? [];
  const rawMessages = scopedWarnings.length
    ? [
        ...scopedWarnings,
        ...(urls?.blocking_errors ?? []),
        ...(contract?.blocking_errors ?? []),
      ]
    : [
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
    if (!lowered.includes("webrtc") && !lowered.includes("whep") && !/\bice\b/.test(lowered)) continue;
    if (seen.has(message)) continue;
    seen.add(message);
    out.push(message);
  }
  return out;
}

function getHlsIssueMessages(urls: StreamingTransmissionUrlsResponse | undefined): string[] {
  const rawMessages = [...(urls?.hls_warnings ?? [])];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of rawMessages) {
    const message = String(raw || "").trim();
    if (!message || seen.has(message)) continue;
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
  serverPlaybackPlan?: StreamingPlaybackPlanResponse | null;
  mseUrl: string | null;
  hlsUrl: string | null;
  webrtcUrl: string | null;
  jsmpegUrl: string | null;
  lowLatencyRequested: boolean;
}): StreamPlaybackPlan {
  const hasMse = Boolean(options.mseUrl) && canUseMse();
  const hasHls = Boolean(options.hlsUrl);
  const hasWebRtc = Boolean(options.webrtcUrl);
  const hasJsmpeg = Boolean(options.jsmpegUrl);
  const webRtcIssueMessages = getWebRtcIssueMessages(options.urls);
  const webRtcBlocked = webRtcIssueMessages.length > 0;
  const homeAssistantProxyHls = hasHomeAssistantProxyHlsContract(options.urls);
  const mobileTouchBrowser = isProbablyMobileTouchBrowser();
  const serverSelectedTransport = String(options.serverPlaybackPlan?.selected_transport || "").trim().toLowerCase();
  const serverPrefersMse = serverSelectedTransport === "mse";
  const serverPrefersHls = serverSelectedTransport === "hls";
  const serverPrefersWebRtc = serverSelectedTransport === "webrtc";
  const serverPrefersJsmpeg = serverSelectedTransport === "jsmpeg";

  if (options.transportPreference === "hls") {
    return {
      allowMse: false,
      allowHls: hasHls,
      allowWebRtc: false,
      allowJsmpeg: false,
      preferMseFirst: false,
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
      allowMse: false,
      allowHls: false,
      allowWebRtc: hasWebRtc,
      allowJsmpeg: false,
      preferMseFirst: false,
      preferWebRtcFirst: true,
      effectiveMode: "webrtc",
      webRtcBlocked,
      webRtcIssueMessages,
      homeAssistantProxyHls,
      mobileTouchBrowser,
    };
  }

  const hlsFirstForHomeAssistant = homeAssistantProxyHls && hasHls && !options.lowLatencyRequested;
  const hlsFirstForServerPlan = serverPrefersHls && hasHls && !options.lowLatencyRequested;
  const hlsFirstForContract = webRtcBlocked && hasHls;
  const preferHlsFirst = hlsFirstForHomeAssistant || hlsFirstForServerPlan || hlsFirstForContract;
  const allowMse = hasMse && !hlsFirstForHomeAssistant;
  const allowWebRtc = hasWebRtc && !webRtcBlocked && (options.lowLatencyRequested || !preferHlsFirst || !hasHls);
  const allowJsmpeg = hasJsmpeg;
  const preferWebRtcFirst = allowWebRtc && (options.lowLatencyRequested || serverPrefersWebRtc || !preferHlsFirst);
  const preferMseFirst = allowMse && !preferWebRtcFirst && (serverPrefersMse || (!preferHlsFirst && !serverPrefersHls));
  const effectiveMode: EffectiveTransportMode = preferWebRtcFirst
    ? options.lowLatencyRequested
      ? "ptz_webrtc"
      : "auto_webrtc"
    : preferMseFirst
      ? "auto_mse"
    : hasHls
      ? "auto_hls"
    : allowMse
      ? "auto_mse"
      : allowJsmpeg || serverPrefersJsmpeg
        ? "auto_jsmpeg"
      : allowWebRtc
        ? "auto_webrtc"
        : "auto";

  return {
    allowMse,
    allowHls: hasHls,
    allowWebRtc,
    allowJsmpeg,
    preferMseFirst,
    preferWebRtcFirst,
    effectiveMode,
    webRtcBlocked,
    webRtcIssueMessages,
    homeAssistantProxyHls,
    mobileTouchBrowser,
  };
}

function plannedPrimaryTransport(
  playbackPlan: StreamPlaybackPlan,
  mseUrl: string | null,
  hlsUrl: string | null,
  webrtcUrl: string | null,
  jsmpegUrl: string | null,
): TilePlaybackTransport {
  if (playbackPlan.preferWebRtcFirst && playbackPlan.allowWebRtc && webrtcUrl) return "webrtc";
  if (playbackPlan.preferMseFirst && playbackPlan.allowMse && mseUrl) return "mse";
  if (playbackPlan.allowHls && hlsUrl) return "hls";
  if (playbackPlan.allowJsmpeg && jsmpegUrl) return "jsmpeg";
  if (playbackPlan.allowMse && mseUrl) return "mse";
  if (playbackPlan.allowWebRtc && webrtcUrl) return "webrtc";
  return "none";
}

function transportScopedWarnings(
  urls: StreamingTransmissionUrlsResponse | undefined,
  transport: TilePlaybackTransport,
  playbackPlan: StreamPlaybackPlan,
): string[] {
  if (transport === "webrtc") return playbackPlan.webRtcIssueMessages;
  if (transport === "hls") return getHlsIssueMessages(urls);
  if (transport === "jsmpeg") return [];
  return urls?.warnings ?? [];
}

function isAdvisoryLivePlaybackWarning(message: string): boolean {
  const normalized = String(message || "").trim().toLowerCase();
  if (!normalized) return false;
  return (
    normalized.includes("conexão direta com a origem") ||
    normalized.includes("abrir conexão direta") ||
    normalized.includes("direct connection to the camera") ||
    normalized.includes("open a direct connection") ||
    /\b(api|metrics|rtsp|webrtc|hls)?\s*port\s+\d+\s+unavailable;\s+using\s+\d+/.test(normalized)
  );
}

function primaryLivePlaybackWarning(playback: StreamingCameraLiveViewPlaybackResponse | null | undefined): string | null {
  for (const raw of playback?.warnings ?? []) {
    const message = String(raw || "").trim();
    if (!message || isAdvisoryLivePlaybackWarning(message)) continue;
    return message;
  }
  return null;
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
    requestedProfileId && ["hls", "mse", "jsmpeg"].includes(protocol)
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

const MSE_CODEC_REQUEST = "avc1.640029,avc1.64002A,avc1.640033,avc1.42E01E,mp4a.40.2,opus";

function canUseMse(): boolean {
  return typeof MediaSource !== "undefined";
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
    const messageType = String(parsed.type || "").trim().toLowerCase();
    const messageValue = String(parsed.value || "").trim();
    if (messageType === "mse" && messageValue.includes("video/mp4")) return messageValue;
    if (messageType === "mse" && /(avc1|hvc1|hev1|mp4a)/i.test(messageValue)) {
      return `video/mp4; codecs="${messageValue.replace(/^codecs=/i, "").replace(/^"|"$/g, "")}"`;
    }
    const explicitCodecs = String(parsed.codecs || parsed.codec || "").trim();
    if (explicitCodecs) return `video/mp4; codecs="${explicitCodecs}"`;
  } catch {
    // Some sidecars send a plain MIME/codecs string as the first message.
  }
  if (text.includes("video/mp4")) return text;
  if (/(avc1|hvc1|hev1|mp4a)/i.test(text)) return `video/mp4; codecs="${text.replace(/^codecs=/i, "").replace(/^"|"$/g, "")}"`;
  return null;
}

function errorFromMseControlMessage(raw: string): string | null {
  const text = String(raw || "").trim();
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>;
    const messageType = String(parsed.type || "").trim().toLowerCase();
    if (messageType !== "error") return null;
    return String(parsed.value || parsed.message || parsed.error || text).trim() || text;
  } catch {
    return /^error[:\s]/i.test(text) ? text : null;
  }
}

function isRetriableMseStartupError(message: string): boolean {
  const lowered = String(message || "").toLowerCase();
  return (
    lowered.includes("describe") ||
    lowered.includes("not found") ||
    lowered.includes("404") ||
    lowered.includes("no one is publishing") ||
    lowered.includes("source is unavailable") ||
    lowered.includes("connection refused")
  );
}

function resolveHlsRelativeUrl(baseUrl: string, rawUrl: string): string {
  const absoluteBaseUrl = new URL(String(baseUrl || "").trim(), window.location.href).toString();
  return new URL(String(rawUrl || "").trim(), absoluteBaseUrl).toString();
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
  if (health.demand_idle || health.classification === "demand_idle") {
    return {
      message: t(
        "core.ui.streams.health.demand_idle",
        {},
        "Waiting for a viewer to start capture.",
      ),
      tone: "warn",
    };
  }
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
  const hasNoWriter = !String(health.active_writer_id || "").trim() && !String(health.selected_writer_id || "").trim();
  const sourceBlockingMessage = sourceHealthBlockingMessage(sourceHealth, t);
  if (health.fallback_reason === "no_frame" && hasNoWriter && sourceBlockingMessage) {
    return {
      message: sourceBlockingMessage,
      tone: "error",
    };
  }
  if (health.fallback_reason === "no_frame" && hasNoWriter) {
    return {
      message: t(
        "core.ui.streams.health.no_pipeline_frame",
        {},
        "No pipeline is feeding this transmission.",
      ),
      tone: "error",
    };
  }
  if (health.classification === "publisher_down" && !hasNoWriter) {
    return {
      message: t(
        "core.ui.streams.health.publisher_down",
        {},
        "The pipeline has a selected frame, but the HLS/WebRTC publisher is not running.",
      ),
      tone: "error",
    };
  }
  if (health.classification === "source_pipeline_stale") {
    const lastLive = formatLastLiveTime(health.last_live_frame_at_unix);
    const suffix = lastLive
      ? t("core.ui.streams.health.last_live_suffix", { time: lastLive }, ` Last live frame: ${lastLive}.`)
      : "";
    return {
      message: t(
        "core.ui.streams.health.source_pipeline_stale",
        { suffix },
        `Pipeline stopped feeding fresh frames.${suffix}`,
      ),
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
  demandIdle = false,
): string | null {
  if (demandIdle) return t("core.ui.streams.health.demand_idle_label", {}, "Waiting viewer");
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

function waitForVideoElementFrame(videoElement: HTMLVideoElement, timeoutMs: number, timeoutMessage: string): Promise<void> {
  if (videoElement.videoWidth > 0 && videoElement.videoHeight > 0 && videoElement.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      cleanup();
      reject(new Error(timeoutMessage));
    }, Math.max(1000, timeoutMs));
    const intervalId = window.setInterval(checkReady, 100);
    const onError = () => {
      cleanup();
      reject(new Error(videoElement.error?.message || `Video element failed with code ${videoElement.error?.code ?? "unknown"}.`));
    };
    function cleanup() {
      window.clearTimeout(timeoutId);
      window.clearInterval(intervalId);
      videoElement.removeEventListener("loadeddata", checkReady);
      videoElement.removeEventListener("canplay", checkReady);
      videoElement.removeEventListener("playing", checkReady);
      videoElement.removeEventListener("timeupdate", checkReady);
      videoElement.removeEventListener("error", onError);
    }
    function checkReady() {
      if (videoElement.videoWidth <= 0 || videoElement.videoHeight <= 0 || videoElement.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
      cleanup();
      resolve();
    }
    videoElement.addEventListener("loadeddata", checkReady);
    videoElement.addEventListener("canplay", checkReady);
    videoElement.addEventListener("playing", checkReady);
    videoElement.addEventListener("timeupdate", checkReady);
    videoElement.addEventListener("error", onError);
    checkReady();
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
  jsmpegOutputId,
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
  jsmpegOutputId: string | null;
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
        : playbackTransport === "jsmpeg"
          ? jsmpegOutputId
        : selectedOutputId;
  const activeUrlOutput = findUrlOutput(urls, activeOutputId);
  const activeRuntimeOutput = findRuntimeOutput(runtimeHealth, activeOutputId);
  const sourceHealth = runtimeHealth?.source_health ?? null;
  const evidence = runtimeHealth?.evidence ?? [];
  const warnings = urls?.warnings ?? [];
  const activeTransportWarnings = transportScopedWarnings(urls, playbackTransport, playbackPlan);
  const publicBasePath = urls?.public_base_path || urls?.network_contract?.public_base_path || "/";
  const mediaOrigin = urls?.media_url_origin || urls?.network_contract?.media_url_origin || "-";
  const hlsUsesPublicBasePath =
    Boolean(hlsUrl) && publicBasePath !== "/" && String(hlsUrl || "").includes(publicBasePath);
  const showWebRtcIssues = playbackTransport === "webrtc" || lowLatencyRequested || transportPreference === "webrtc";
  const hlsHealthy = playbackTransport === "hls" && Boolean(hlsProbeSummary && !hlsProbeSummary.startsWith("failed"));
  const activeTransportWarningText = activeTransportWarnings.join(" ");
  const webRtcIssueText = playbackPlan.webRtcIssueMessages.join(" ");
  const webRtcContractStatus = playbackPlan.webRtcBlocked
    ? showWebRtcIssues
      ? "warning"
      : "not selected"
    : "ok";
  const webRtcGuidance =
    playbackPlan.homeAssistantProxyHls && (playbackTransport === "webrtc" || lowLatencyRequested || transportPreference === "webrtc")
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
          <TechnicalDetailRow label="HLS health" value={hlsHealthy ? "healthy" : playbackTransport === "hls" ? "checking" : "-"} />
          <TechnicalDetailRow label="WebRTC contract" value={webRtcContractStatus} />
          <TechnicalDetailRow label="Player error" value={errorText || "-"} />
          <TechnicalDetailRow label="Current hint" value={sourceHint || "-"} />
        </div>
        {activeTransportWarningText ? <div className="streamsAdvancedNote isWarn">{activeTransportWarningText}</div> : null}
        {showWebRtcIssues && webRtcIssueText && webRtcIssueText !== activeTransportWarningText ? (
          <div className="streamsAdvancedNote isWarn">{webRtcIssueText}</div>
        ) : null}
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
          <TechnicalDetailRow label="Runtime status" value={runtimeStatusLabel(runtimeHealth?.status, t, Boolean(runtimeHealth?.event_gated_idle), Boolean(runtimeHealth?.demand_idle)) || "-"} />
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
  mseOutputId,
  jsmpegOutputId,
  webrtcOutputId,
  hlsOutputId,
  hlsQualityProfileId,
  label,
  urls,
  playbackPlan: serverPlaybackPlan,
  overlayVisible,
  sourceHint,
  sourceHintTone,
  mseUrl,
  jsmpegUrl,
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
  variantOptions,
  variantOverrideId,
  currentVariantLabel,
  canSetVariantDefault,
  savingVariantDefault,
  onQualityPreferenceChange,
  onTransportPreferenceChange,
  onVariantOverrideChange,
  onSetVariantDefault,
  onRefreshUrls,
  onOpenPtz,
  onDisplayContextChange,
}: {
  transmissionId: string;
  outputId: string | null;
  mseOutputId: string | null;
  jsmpegOutputId: string | null;
  webrtcOutputId: string | null;
  hlsOutputId: string | null;
  hlsQualityProfileId: StreamingQualityProfileId | null;
  label: string;
  urls?: StreamingTransmissionUrlsResponse;
  playbackPlan?: StreamingPlaybackPlanResponse | null;
  overlayVisible: boolean;
  sourceHint: string | null;
  sourceHintTone: "muted" | "warn" | "error";
  mseUrl: string | null;
  jsmpegUrl: string | null;
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
  variantOptions: LiveVariantQuickOption[];
  variantOverrideId: string;
  currentVariantLabel: string;
  canSetVariantDefault: boolean;
  savingVariantDefault: boolean;
  onQualityPreferenceChange: (preference: StreamQualityPreference) => void;
  onTransportPreferenceChange: (preference: StreamTransportPreference) => void;
  onVariantOverrideChange: (variantId: string) => void;
  onSetVariantDefault: () => Promise<void>;
  onRefreshUrls: () => Promise<void>;
  onOpenPtz: () => void;
  onDisplayContextChange?: (context: "pip" | "fullscreen" | null) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const jsmpegCanvasRef = useRef<HTMLCanvasElement | null>(null);
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
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [playbackWarmupUntilMs, setPlaybackWarmupUntilMs] = useState(0);
  const runtimeHealthRef = useRef(runtimeHealth);
  const playbackActive = active || pictureInPictureActive;
  const playbackWarmupActive = playbackWarmupUntilMs > Date.now();
  useEffect(() => {
    onRefreshUrlsRef.current = onRefreshUrls;
  }, [onRefreshUrls]);
  useEffect(() => {
    runtimeHealthRef.current = runtimeHealth;
  }, [runtimeHealth]);
  useEffect(() => {
    if (!playbackWarmupUntilMs) return;
    const delayMs = Math.max(250, playbackWarmupUntilMs - Date.now());
    const timeoutId = window.setTimeout(() => setPlaybackWarmupUntilMs(0), delayMs);
    return () => window.clearTimeout(timeoutId);
  }, [playbackWarmupUntilMs]);
  const playbackPlan = useMemo(
    () =>
      buildPlaybackPlan({
        transportPreference,
        urls,
        serverPlaybackPlan,
        mseUrl,
        hlsUrl,
        webrtcUrl,
        jsmpegUrl,
        lowLatencyRequested,
      }),
    [hlsUrl, jsmpegUrl, lowLatencyRequested, mseUrl, serverPlaybackPlan, transportPreference, urls, webrtcUrl],
  );
  const { allowMse, allowHls, allowWebRtc, allowJsmpeg, preferMseFirst, preferWebRtcFirst } = playbackPlan;
  const transportTelemetryBase = useMemo(
    () => ({
      transport_preference: transportPreference,
      effective_transport: playbackPlan.effectiveMode,
      mse_available: playbackPlan.allowMse,
      jsmpeg_available: playbackPlan.allowJsmpeg,
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
      playbackPlan.allowMse,
      playbackPlan.allowJsmpeg,
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
    onDisplayContextChange?.(pictureInPictureActive ? "pip" : fullscreenActive ? "fullscreen" : null);
  }, [fullscreenActive, onDisplayContextChange, pictureInPictureActive]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const updateFullscreenState = () => {
      setFullscreenActive(document.fullscreenElement === frameRef.current);
    };
    document.addEventListener("fullscreenchange", updateFullscreenState);
    updateFullscreenState();
    return () => document.removeEventListener("fullscreenchange", updateFullscreenState);
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
      const heartbeatTransport =
        transport === "webrtc" && !webRtcFallbackActive
          ? "webrtc"
          : transport === "mse"
            ? "mse"
            : transport === "jsmpeg"
              ? "jsmpeg"
              : "hls";
      const selectedOutputId =
        heartbeatTransport === "webrtc"
          ? webrtcOutputId ?? outputId
          : heartbeatTransport === "mse"
            ? mseOutputId ?? hlsOutputId ?? outputId
            : heartbeatTransport === "jsmpeg"
              ? jsmpegOutputId ?? hlsOutputId ?? outputId
            : hlsOutputId ?? outputId;
      if (!selectedOutputId) return;
      void heartbeatStreamingTransmissionDemand(transmissionId, {
        playbackSessionId,
        outputId: selectedOutputId,
        qualityProfileId: heartbeatTransport === "hls" || heartbeatTransport === "mse" || heartbeatTransport === "jsmpeg" ? hlsQualityProfileId : null,
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
    jsmpegOutputId,
    mseOutputId,
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
    let hlsLivenessTimerId: number | null = null;
    let hlsLastLiveKey: string | null = null;
    let hlsLastChangedAtMs = 0;
    let mediaSource: MediaSource | null = null;
    let mseSocket: WebSocket | null = null;
    let mseObjectUrl: string | null = null;
    let mseSessionGeneration = 0;
    let jsmpegPlayer: { destroy: () => void } | null = null;
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
      if (hlsLivenessTimerId != null) {
        window.clearInterval(hlsLivenessTimerId);
        hlsLivenessTimerId = null;
      }
      try {
        hlsPlayer?.destroy();
      } catch {
        // ignore
      }
      hlsPlayer = null;
    };

    const startHlsLivenessMonitor = (sourceUrl: string) => {
      if (hlsLivenessTimerId != null) {
        window.clearInterval(hlsLivenessTimerId);
        hlsLivenessTimerId = null;
      }
      hlsLastLiveKey = null;
      hlsLastChangedAtMs = Date.now();

      const sample = async () => {
        try {
          const probe = await probeBrowserHlsPlayback(sourceUrl, hlsAuthHeader);
          if (cancelled) return;
          const liveKey = `${probe.mediaSequence || ""}|${probe.tailSegmentUrl || ""}`;
          const targetDurationMs = Math.max(2000, Number(probe.targetDuration || 0) * 1000 || 4000);
          if (!hlsLastLiveKey || liveKey !== hlsLastLiveKey) {
            hlsLastLiveKey = liveKey;
            hlsLastChangedAtMs = Date.now();
            setHlsProbeSummary(
              ["live", probe.mediaSequence ? `seq ${probe.mediaSequence}` : null, probe.targetDuration ? `target ${probe.targetDuration}s` : null]
                .filter(Boolean)
                .join(" · "),
            );
            return;
          }

          const staleForMs = Date.now() - hlsLastChangedAtMs;
          const staleThresholdMs = targetDurationMs * 3 + HLS_LIVENESS_STALE_GRACE_MS;
          if (staleForMs >= staleThresholdMs) {
            const message = i18n.t(
              "core.ui.streams.errors.hls_liveness_stale",
              {},
              "HLS playlist stopped advancing.",
            );
            recordWebPlaybackEvent("hls_liveness_stale", {
              severity: "warn",
              message,
              data: withTransportTelemetry({
                media_playlist_url: probe.mediaPlaylistUrl,
                tail_segment_url: probe.tailSegmentUrl,
                media_sequence: probe.mediaSequence,
                target_duration_seconds: probe.targetDuration,
                stale_for_ms: staleForMs,
              }),
            });
            setStatus("error");
            setErrorText(message);
            destroyPlayback();
            scheduleRetry(message);
          }
        } catch (error) {
          if (cancelled) return;
          const message = asErrorMessage(error);
          recordWebPlaybackEvent("hls_liveness_probe_error", {
            severity: "warn",
            message,
            data: withTransportTelemetry({ playback_transport: "hls" }),
          });
        }
      };

      void sample();
      hlsLivenessTimerId = window.setInterval(() => {
        void sample();
      }, 3000);
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

    const destroyMse = () => {
      mseSessionGeneration += 1;
      const socket = mseSocket;
      mseSocket = null;
      if (socket) {
        try {
          socket.close();
        } catch {
          // ignore
        }
      }
      mediaSource = null;
      if (mseObjectUrl) {
        try {
          URL.revokeObjectURL(mseObjectUrl);
        } catch {
          // ignore
        }
      }
      mseObjectUrl = null;
    };

    const destroyJsmpeg = () => {
      const player = jsmpegPlayer;
      jsmpegPlayer = null;
      if (!player) return;
      try {
        player.destroy();
      } catch {
        // ignore
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
      destroyMse();
      destroyJsmpeg();
      destroyWebRtc();
      clearVideoSource();
    };

    const scheduleRetry = (reason: string) => {
      if (
        cancelled ||
        !playbackActive ||
        ((!allowMse || !mseUrl) && (!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl) && (!allowJsmpeg || !jsmpegUrl)) ||
        retryTimerId != null
      ) return;
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

    const startMsePlayback = async (video: HTMLVideoElement): Promise<void> => {
      if (!mseUrl) throw new Error("MSE URL is not available.");
      if (!canUseMse()) throw new Error("MediaSource is not available in this browser.");
      setTransport("mse");
      const wsUrl = normalizeWebSocketUrl(mseUrl);
      recordWebPlaybackEvent("mse_start", {
        severity: "info",
        data: withTransportTelemetry({ url: wsUrl, output_id: mseOutputId ?? hlsOutputId ?? outputId }),
      });
      const sessionGeneration = mseSessionGeneration + 1;
      mseSessionGeneration = sessionGeneration;
      const localMediaSource = new MediaSource();
      mediaSource = localMediaSource;
      mseObjectUrl = URL.createObjectURL(localMediaSource);
      video.src = mseObjectUrl;
      await new Promise<void>((resolve, reject) => {
        if (!mediaSource) {
          reject(new Error("MediaSource was not created."));
          return;
        }
        let settled = false;
        let sourceBuffer: SourceBuffer | null = null;
        const queue: ArrayBuffer[] = [];
        const isActiveMseSession = (socket?: WebSocket | null) =>
          !cancelled &&
          mediaSource === localMediaSource &&
          mseSessionGeneration === sessionGeneration &&
          (!socket || mseSocket === socket);
        const timeoutId = window.setTimeout(() => {
          if (!isActiveMseSession()) return;
          rejectOnce(new Error("Timed out waiting for MSE initialization data."));
        }, MSE_INIT_TIMEOUT_MS);
        const flush = () => {
          if (!isActiveMseSession() || localMediaSource.readyState !== "open") return;
          if (!sourceBuffer || sourceBuffer.updating || queue.length === 0) return;
          const next = queue.shift();
          if (!next) return;
          try {
            sourceBuffer.appendBuffer(next);
          } catch (error) {
            rejectOnce(error);
          }
        };
        const resolveOnce = () => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timeoutId);
          resolve();
        };
        const rejectOnce = (error: unknown) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timeoutId);
          reject(error);
        };
        const openMseSocket = (attempt: number) => {
          if (!isActiveMseSession() || settled || sourceBuffer) return;
          const socket = new WebSocket(wsUrl);
          mseSocket = socket;
          socket.binaryType = "arraybuffer";
          socket.addEventListener("open", () => {
            if (!isActiveMseSession(socket)) return;
            socket.send(JSON.stringify({ type: "mse", value: MSE_CODEC_REQUEST }));
            recordWebPlaybackEvent("mse_websocket_open", {
              severity: "debug",
              data: withTransportTelemetry({ output_id: mseOutputId ?? hlsOutputId ?? outputId, attempt }),
            });
          });
          socket.addEventListener("close", (event) => {
            if (!isActiveMseSession(socket)) return;
            recordWebPlaybackEvent("mse_websocket_close", {
              severity: event.wasClean ? "debug" : "warn",
              data: withTransportTelemetry({ code: event.code, reason: event.reason, was_clean: event.wasClean, attempt }),
            });
          });
          socket.addEventListener("error", () => {
            if (!isActiveMseSession(socket)) return;
            if (attempt < MSE_CONNECT_ATTEMPTS) {
              recordWebPlaybackEvent("mse_warmup_retry", {
                severity: "warn",
                data: withTransportTelemetry({ output_id: mseOutputId ?? hlsOutputId ?? outputId, attempt }),
              });
              window.setTimeout(() => openMseSocket(attempt + 1), MSE_RETRY_DELAY_MS);
              return;
            }
            rejectOnce(new Error("MSE WebSocket failed."));
          });
          socket.addEventListener("message", (event) => {
            if (!isActiveMseSession(socket)) return;
            if (typeof event.data === "string") {
              if (!sourceBuffer) {
                const mseError = errorFromMseControlMessage(event.data);
                if (mseError) {
                  if (attempt < MSE_CONNECT_ATTEMPTS && isRetriableMseStartupError(mseError)) {
                    recordWebPlaybackEvent("mse_warmup_retry", {
                      severity: "warn",
                      message: mseError,
                      data: withTransportTelemetry({ output_id: mseOutputId ?? hlsOutputId ?? outputId, attempt }),
                    });
                    socket.close(4000, "retrying MSE startup");
                    window.setTimeout(() => openMseSocket(attempt + 1), MSE_RETRY_DELAY_MS);
                    return;
                  }
                  rejectOnce(new Error(`MSE sidecar error: ${mseError}`));
                  return;
                }
                const mime = mimeFromMseControlMessage(event.data);
                if (!mime) return;
                if (!MediaSource.isTypeSupported(mime)) {
                  rejectOnce(new Error(`Browser does not support MSE mime type: ${mime}`));
                  return;
                }
                if (localMediaSource.readyState !== "open") {
                  rejectOnce(new Error(`MSE MediaSource is ${localMediaSource.readyState}; cannot create SourceBuffer.`));
                  return;
                }
                try {
                  sourceBuffer = localMediaSource.addSourceBuffer(mime);
                } catch (error) {
                  rejectOnce(error);
                  return;
                }
                sourceBuffer.addEventListener("updateend", flush);
                sourceBuffer.addEventListener("error", () => {
                  if (!isActiveMseSession()) return;
                  rejectOnce(new Error("MSE SourceBuffer error."));
                });
                recordWebPlaybackEvent("mse_source_buffer", {
                  severity: "info",
                  data: withTransportTelemetry({ mime, output_id: mseOutputId ?? hlsOutputId ?? outputId }),
                });
                void video.play().catch(() => {});
              }
              return;
            }
            if (!(event.data instanceof ArrayBuffer)) return;
            queue.push(event.data);
            flush();
            resolveOnce();
          });
        };
        const onSourceOpen = () => {
          if (!isActiveMseSession() || localMediaSource.readyState !== "open") return;
          openMseSocket(1);
        };
        localMediaSource.addEventListener("sourceopen", onSourceOpen, { once: true });
      });
      await waitForVideoElementFrame(
        video,
        MSE_FIRST_FRAME_TIMEOUT_MS,
        i18n.t("core.ui.streams.errors.mse_first_frame_timeout", {}, "Timed out waiting for MSE video frame."),
      );
      setStatus("playing");
      setErrorText(null);
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
      startHlsLivenessMonitor(sourceUrl);
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

      const onPlaying = () => {
        setStatus("playing");
        setErrorText(null);
      };
      video.addEventListener("playing", onPlaying);
      video.addEventListener("loadeddata", onPlaying);
      nativeCleanup = () => {
        video.removeEventListener("playing", onPlaying);
        video.removeEventListener("loadeddata", onPlaying);
      };

      hls.on(HlsConstructor.Events.MANIFEST_PARSED, () => {
        setStatus("loading");
        setErrorText(null);
        startHlsLivenessMonitor(hlsUrl ?? "");
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

    const startJsmpegPlayback = async (): Promise<void> => {
      if (!jsmpegUrl) throw new Error("JSMpeg URL is not available.");
      const canvas = jsmpegCanvasRef.current;
      if (!canvas) throw new Error("JSMpeg canvas is not available.");
      setTransport("jsmpeg");
      const wsUrl = normalizeWebSocketUrl(jsmpegUrl);
      recordWebPlaybackEvent("jsmpeg_start", {
        severity: "info",
        data: withTransportTelemetry({ url: wsUrl, output_id: jsmpegOutputId ?? hlsOutputId ?? outputId }),
      });
      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const timeoutId = window.setTimeout(() => {
          if (settled) return;
          settled = true;
          reject(new Error("Timed out waiting for JSMpeg video frame."));
        }, MSE_FIRST_FRAME_TIMEOUT_MS);
        const resolveOnce = () => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timeoutId);
          resolve();
        };
        const rejectOnce = (error: unknown) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timeoutId);
          reject(error instanceof Error ? error : new Error(asErrorMessage(error)));
        };
        try {
          jsmpegPlayer = createJsmpegPlayer(wsUrl, {
            canvas,
            onSourceEstablished: () => {
              recordWebPlaybackEvent("jsmpeg_websocket_open", {
                severity: "debug",
                data: withTransportTelemetry({ output_id: jsmpegOutputId ?? hlsOutputId ?? outputId }),
              });
            },
            onSourceCompleted: () => {
              recordWebPlaybackEvent("jsmpeg_websocket_close", {
                severity: "debug",
                data: withTransportTelemetry({ output_id: jsmpegOutputId ?? hlsOutputId ?? outputId }),
              });
            },
            onStalled: () => {
              recordWebPlaybackEvent("jsmpeg_stalled", {
                severity: "warn",
                data: withTransportTelemetry({ output_id: jsmpegOutputId ?? hlsOutputId ?? outputId }),
              });
            },
            onError: (error) => {
              recordWebPlaybackEvent("jsmpeg_error", {
                severity: "warn",
                message: asErrorMessage(error),
                data: withTransportTelemetry({ output_id: jsmpegOutputId ?? hlsOutputId ?? outputId }),
              });
              rejectOnce(error);
            },
            onVideoDecode: () => {
              recordWebPlaybackEvent("jsmpeg_video_decode", {
                severity: "debug",
                data: withTransportTelemetry({ output_id: jsmpegOutputId ?? hlsOutputId ?? outputId }),
              });
              resolveOnce();
            },
          });
        } catch (error) {
          rejectOnce(error);
        }
      });
      setStatus("playing");
      setErrorText(null);
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

    const retryHlsWarmupError = (message: string): boolean => {
      if (!isHlsWarmupRecoverableError(message, runtimeHealthRef.current)) return false;
      setStatus("loading");
      setTransport("hls");
      setErrorText(null);
      recordWebPlaybackEvent("hls_warmup_retry", {
        severity: "debug",
        message,
        data: withTransportTelemetry({ output_id: hlsOutputId ?? outputId, playback_transport: "hls" }),
      });
      destroyPlayback();
      scheduleRetry(message);
      return true;
    };

    const startPlayback = async () => {
      if (
        cancelled ||
        !playbackActive ||
        ((!allowMse || !mseUrl) && (!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl) && (!allowJsmpeg || !jsmpegUrl))
      ) return;
      const video = videoRef.current;
      if (!video) return;
      if (!playbackSessionIdRef.current) {
        playbackSessionIdRef.current = `${transmissionId || "stream"}:web:${Date.now()}:${Math.floor(Math.random() * 1_000_000)}`;
      }
      recordWebPlaybackEvent("load", {
        severity: "info",
        data: withTransportTelemetry({
          has_webrtc: Boolean(webrtcUrl),
          has_mse: Boolean(mseUrl),
          has_hls: Boolean(hlsUrl),
          has_jsmpeg: Boolean(jsmpegUrl),
          selected_mse_output_id: mseOutputId,
          selected_jsmpeg_output_id: jsmpegOutputId,
          selected_hls_output_id: hlsOutputId,
          selected_webrtc_output_id: webrtcOutputId,
          fallback_transport: preferWebRtcFirst && (allowMse || allowHls || allowJsmpeg)
            ? (allowMse ? "mse" : allowHls ? "hls" : "jsmpeg")
            : preferMseFirst && (allowHls || allowJsmpeg)
              ? (allowHls ? "hls" : "jsmpeg")
              : null,
        }),
      });

      destroyPlayback();
      configureVideo(video);
      setStatus("loading");
      setErrorText(null);
      setTransport("none");
      setPlaybackWarmupUntilMs(Date.now() + HLS_PLAYBACK_WARMUP_MS);
      setWebRtcFallbackActive(false);
      if (cancelled) return;

      let webRtcError: string | null = null;
      const startHlsWithTelemetry = async (fallbackFromError: string | null): Promise<void> => {
        await primePlaybackOutput(hlsOutputId ?? outputId, hlsQualityProfileId);
        recordWebPlaybackEvent("hls_start", {
          severity: "info",
          data: withTransportTelemetry({
            output_id: hlsOutputId ?? outputId,
            quality_profile_id: hlsQualityProfileId,
            fallback_transport: fallbackFromError ? "hls" : null,
            fallback_successful: fallbackFromError ? true : null,
          }),
        });
        if (fallbackFromError) {
          setWebRtcFallbackActive(true);
        }
        await startHlsPlayback(video);
        if (fallbackFromError) {
          recordWebPlaybackEvent("playback_fallback_hls", {
            severity: "warn",
            message: fallbackFromError,
            data: withTransportTelemetry({
              output_id: hlsOutputId ?? outputId,
              fallback_transport: "hls",
              fallback_successful: true,
              effective_transport: "hls_fallback",
            }),
          });
        }
      };

      const startMseWithTelemetry = async (fallbackFromError: string | null): Promise<void> => {
        await primePlaybackOutput(mseOutputId ?? hlsOutputId ?? outputId, hlsQualityProfileId);
        recordWebPlaybackEvent("mse_start", {
          severity: "info",
          data: withTransportTelemetry({
            output_id: mseOutputId ?? hlsOutputId ?? outputId,
            quality_profile_id: hlsQualityProfileId,
            fallback_transport: fallbackFromError ? "mse" : null,
            fallback_successful: fallbackFromError ? true : null,
          }),
        });
        await startMsePlayback(video);
        if (fallbackFromError) {
          recordWebPlaybackEvent("mse_fallback_success", {
            severity: "warn",
            message: fallbackFromError,
            data: withTransportTelemetry({
              output_id: mseOutputId ?? hlsOutputId ?? outputId,
              fallback_transport: "mse",
              fallback_successful: true,
              effective_transport: "mse",
            }),
          });
        }
      };

      const startJsmpegWithTelemetry = async (fallbackFromError: string | null): Promise<void> => {
        await primePlaybackOutput(jsmpegOutputId ?? hlsOutputId ?? outputId, hlsQualityProfileId);
        recordWebPlaybackEvent("jsmpeg_start", {
          severity: "info",
          data: withTransportTelemetry({
            output_id: jsmpegOutputId ?? hlsOutputId ?? outputId,
            quality_profile_id: hlsQualityProfileId,
            fallback_transport: fallbackFromError ? "jsmpeg" : null,
            fallback_successful: fallbackFromError ? true : null,
          }),
        });
        await startJsmpegPlayback();
        if (fallbackFromError) {
          recordWebPlaybackEvent("playback_fallback_jsmpeg", {
            severity: "warn",
            message: fallbackFromError,
            data: withTransportTelemetry({
              output_id: jsmpegOutputId ?? hlsOutputId ?? outputId,
              fallback_transport: "jsmpeg",
              fallback_successful: true,
              effective_transport: "jsmpeg",
            }),
          });
        }
      };

      if (preferMseFirst && allowMse && mseUrl) {
        try {
          await startMseWithTelemetry(null);
          return;
        } catch (error) {
          const mseError = asErrorMessage(error);
          recordWebPlaybackEvent("mse_error", {
            severity: "warn",
            message: mseError,
            data: withTransportTelemetry({ output_id: mseOutputId ?? hlsOutputId ?? outputId, fallback_transport: allowHls ? "hls" : null }),
          });
          destroyMse();
          if (allowHls && hlsUrl) {
            try {
              await startHlsWithTelemetry(mseError);
              return;
            } catch (hlsFallbackError) {
              const hlsError = asErrorMessage(hlsFallbackError);
              if (allowJsmpeg && jsmpegUrl) {
                try {
                  await startJsmpegWithTelemetry(`MSE failed: ${mseError}. HLS failed: ${hlsError}`);
                  return;
                } catch (jsmpegError) {
                  const jsmpegMessage = asErrorMessage(jsmpegError);
                  setStatus("error");
                  setErrorText(`MSE failed: ${mseError}. HLS fallback failed: ${hlsError}. JSMpeg fallback failed: ${jsmpegMessage}`);
                  destroyPlayback();
                  scheduleRetry(jsmpegMessage);
                  return;
                }
              }
              setStatus("error");
              setErrorText(`MSE failed: ${mseError}. HLS fallback failed: ${hlsError}`);
              destroyPlayback();
              scheduleRetry(hlsError);
              return;
            }
          }
          if (allowJsmpeg && jsmpegUrl) {
            try {
              await startJsmpegWithTelemetry(mseError);
              return;
            } catch (jsmpegError) {
              const jsmpegMessage = asErrorMessage(jsmpegError);
              setStatus("error");
              setErrorText(`MSE failed: ${mseError}. JSMpeg fallback failed: ${jsmpegMessage}`);
              destroyPlayback();
              scheduleRetry(jsmpegMessage);
              return;
            }
          }
          setStatus("error");
          setErrorText(mseError);
          destroyPlayback();
          scheduleRetry(mseError);
          return;
        }
      }

      if (!preferWebRtcFirst && allowHls && hlsUrl) {
        try {
          await startHlsWithTelemetry(null);
          return;
        } catch (error) {
          const hlsError = asErrorMessage(error);
          if (await refreshSignedHlsAfterAuthError(hlsError)) return;
          if (retryHlsWarmupError(hlsError)) return;
          if (allowJsmpeg && jsmpegUrl) {
            try {
              await startJsmpegWithTelemetry(hlsError);
              return;
            } catch (jsmpegError) {
              const jsmpegMessage = asErrorMessage(jsmpegError);
              setStatus("error");
              setErrorText(`HLS failed: ${hlsError}. JSMpeg fallback failed: ${jsmpegMessage}`);
              recordWebPlaybackEvent("playback_error", {
                severity: "error",
                message: jsmpegMessage,
                data: withTransportTelemetry({ output_id: jsmpegOutputId ?? hlsOutputId ?? outputId, playback_transport: "jsmpeg" }),
              });
              destroyPlayback();
              scheduleRetry(jsmpegMessage);
              return;
            }
          }
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

      if (allowMse && mseUrl) {
        try {
          await startMseWithTelemetry(webRtcError);
          return;
        } catch (error) {
          const mseError = asErrorMessage(error);
          recordWebPlaybackEvent("mse_error", {
            severity: "warn",
            message: mseError,
            data: withTransportTelemetry({ output_id: mseOutputId ?? hlsOutputId ?? outputId, fallback_transport: allowHls ? "hls" : null }),
          });
          destroyMse();
          webRtcError = webRtcError ? `${webRtcError}. MSE failed: ${mseError}` : mseError;
        }
      }

      if (allowHls && hlsUrl) {
        try {
          await startHlsWithTelemetry(webRtcError);
          return;
        } catch (error) {
          const hlsError = asErrorMessage(error);
          if (await refreshSignedHlsAfterAuthError(hlsError)) return;
          if (retryHlsWarmupError(hlsError)) return;
          const combinedError = webRtcError
            ? i18n.t(
                "core.ui.streams.errors.webrtc_hls_fallback_failed",
                { webrtcError: webRtcError, hlsError },
                "WebRTC failed: {{webrtcError}}. HLS fallback failed: {{hlsError}}",
              )
            : hlsError;
          if (allowJsmpeg && jsmpegUrl) {
            try {
              await startJsmpegWithTelemetry(combinedError);
              return;
            } catch (jsmpegError) {
              const jsmpegMessage = asErrorMessage(jsmpegError);
              setStatus("error");
              setErrorText(`${combinedError}. JSMpeg fallback failed: ${jsmpegMessage}`);
              destroyPlayback();
              scheduleRetry(jsmpegMessage);
              return;
            }
          }
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

      if (allowJsmpeg && jsmpegUrl) {
        try {
          await startJsmpegWithTelemetry(webRtcError);
          return;
        } catch (error) {
          const jsmpegError = asErrorMessage(error);
          setStatus("error");
          setErrorText(webRtcError ? `${webRtcError}. JSMpeg fallback failed: ${jsmpegError}` : jsmpegError);
          recordWebPlaybackEvent("playback_error", {
            severity: "error",
            message: jsmpegError,
            data: withTransportTelemetry({
              output_id: jsmpegOutputId ?? hlsOutputId ?? outputId,
              fallback_transport: "jsmpeg",
              fallback_successful: false,
            }),
          });
          destroyPlayback();
          scheduleRetry(jsmpegError);
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

    if (!playbackActive || ((!allowMse || !mseUrl) && (!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl) && (!allowJsmpeg || !jsmpegUrl))) {
      attempt = 0;
      recordWebPlaybackEvent("stop", { severity: "info" });
      playbackSessionIdRef.current = null;
      destroyPlayback();
      setStatus("idle");
      setTransport("none");
      setErrorText(null);
      setWebRtcFallbackActive(false);
      setHlsProbeSummary(null);
      setPlaybackWarmupUntilMs(0);
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
    mseOutputId,
    mseUrl,
    outputId,
    allowMse,
    allowHls,
    allowWebRtc,
    allowJsmpeg,
    preferMseFirst,
    preferWebRtcFirst,
    jsmpegOutputId,
    jsmpegUrl,
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
        : transport === "mse"
          ? "MSE"
        : transport === "jsmpeg"
          ? "JSMpeg"
        : transport === "hls"
          ? t("core.ui.streams.transport.hls", {}, "HLS")
          : onlineLabel;

    const healthLabel = runtimeStatusLabel(runtimeHealth?.status, t, Boolean(runtimeHealth?.event_gated_idle), Boolean(runtimeHealth?.demand_idle));
    if (status === "playing") {
      return healthLabel ?? (transport === "none" ? onlineLabel : transportLabel);
    }
    if (transport === "none") return statusLabel;
    return t(
      "core.ui.streams.status.with_transport",
      { status: statusLabel, transport: transportLabel },
      `${statusLabel} (${transportLabel})`,
    );
  }, [runtimeHealth?.demand_idle, runtimeHealth?.event_gated_idle, runtimeHealth?.status, status, t, transport]);

  const playbackDotStatus = useMemo<TilePlaybackStatus>(() => {
    if (status === "error" || status === "unsupported") return "error";
    if (runtimeHealth?.demand_idle) return "loading";
    if (runtimeHealth?.event_gated_idle) return "loading";
    if (runtimeHealth?.status === "stale" || runtimeHealth?.status === "offline") return "error";
    if (runtimeHealth?.status === "degraded") return "loading";
    return status;
  }, [runtimeHealth?.demand_idle, runtimeHealth?.event_gated_idle, runtimeHealth?.status, status]);

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
  const hlsWarmupHintActive =
    playbackWarmupActive &&
    transport === "hls" &&
    runtimeHasFreshSourceAndWriter(runtimeHealth) &&
    (status === "loading" || (status === "error" && Boolean(errorText) && isHlsWarmupRecoverableError(errorText ?? "", runtimeHealth)));
  const displayErrorText = hlsWarmupHintActive ? null : errorText;
  const displaySourceHint = hlsWarmupHintActive
    ? t("core.ui.streams.health.hls_warming_up", {}, "Aquecendo transmissão HLS...")
    : sourceHint;
  const displaySourceHintTone = hlsWarmupHintActive ? "muted" : sourceHintTone;

  return (
    <div className="streamsPlayerFrame" ref={frameRef}>
      <canvas
        ref={jsmpegCanvasRef}
        className={["streamsJsmpegCanvas", transport === "jsmpeg" ? "isVisible" : "isHidden"].join(" ")}
      />
      <video
        ref={videoRef}
        className={["streamsVideo", transport === "jsmpeg" ? "isHidden" : ""].join(" ")}
        muted
        playsInline
        autoPlay
      />

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
          {variantOptions.length > 0 ? (
            <div className="streamsTileVariantControl" title={currentVariantLabel}>
              <select
                className="streamsTileVariantSelect"
                value={variantOverrideId}
                aria-label={t("core.ui.streams.variant.select", {}, "Live camera variant")}
                onChange={(event) => onVariantOverrideChange(event.target.value)}
              >
                <option value="">
                  {currentVariantLabel
                    ? t("core.ui.streams.variant.automatic_source", { label: currentVariantLabel }, `Auto · ${currentVariantLabel}`)
                    : t("core.ui.streams.variant.auto", {}, "Auto")}
                </option>
                {variantOptions.map((option) => (
                  <option key={option.id} value={option.id} title={option.title}>
                    {option.label}
                  </option>
                ))}
              </select>
              {canSetVariantDefault ? (
                <button
                  type="button"
                  className="iconButton streamsTileOverlayButton"
                  aria-label={t("core.ui.streams.variant.set_default", {}, "Set as default for this use")}
                  title={t("core.ui.streams.variant.set_default", {}, "Set as default for this use")}
                  onClick={() => {
                    void onSetVariantDefault();
                  }}
                  disabled={savingVariantDefault}
                >
                  <Icon name="check" />
                </button>
              ) : null}
            </div>
          ) : null}
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

      {overlayVisible && (displaySourceHint || displayErrorText) ? (
        <div className={["streamsTileOverlayHint", `is-${displayErrorText ? "error" : displaySourceHintTone}`].join(" ")}>
          {displayErrorText || displaySourceHint}
        </div>
      ) : null}

      <StreamAdvancedSettingsModal
        open={advancedOpen}
        label={label}
        onClose={() => setAdvancedOpen(false)}
        urls={urls}
        runtimeHealth={runtimeHealth}
        hlsOutputId={hlsOutputId}
        jsmpegOutputId={jsmpegOutputId}
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
        errorText={displayErrorText}
        sourceHint={displaySourceHint}
        qualityPreference={qualityPreference}
        transportPreference={transportPreference}
        onQualityPreferenceChange={onQualityPreferenceChange}
        onTransportPreferenceChange={onTransportPreferenceChange}
      />
    </div>
  );
}

export function StreamsDashboard({
  uiVisible,
  isActive,
  embedded = false,
  cameraId,
  liveViewId,
  defaultContext,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [gridMode, setGridMode] = useState<GridMode>(() => (embedded ? "1x1" : readGridMode()));
  const [pageIndex, setPageIndex] = useState(0);

  const [liveViews, setLiveViews] = useState<StreamingCameraLiveView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [playbackByKey, setPlaybackByKey] = useState<Record<string, StreamingCameraLiveViewPlaybackResponse>>({});
  const [playbackLoadingByKey, setPlaybackLoadingByKey] = useState<Record<string, boolean>>({});
  const [playbackErrorByKey, setPlaybackErrorByKey] = useState<Record<string, string>>({});
  const [runtimeHealthByTransmissionId, setRuntimeHealthByTransmissionId] = useState<Record<string, StreamingRuntimeTransmissionHealth>>({});
  const [runtimeHealthError, setRuntimeHealthError] = useState<string | null>(null);
  const [qualityPreferenceByTransmissionId, setQualityPreferenceByTransmissionId] = useState<
    Record<string, StreamQualityPreference>
  >(() => readQualityPreferenceByTransmissionId());
  const [transportPreferenceByTransmissionId, setTransportPreferenceByTransmissionId] = useState<
    Record<string, StreamTransportPreference>
  >(() => readTransportPreferenceByTransmissionId());
  const [variantOverrideByLiveViewId, setVariantOverrideByLiveViewId] = useState<Record<string, string>>(
    () => readLiveVariantOverrideByLiveViewId(),
  );
  const [savingVariantDefaultByLiveViewId, setSavingVariantDefaultByLiveViewId] = useState<Record<string, boolean>>({});
  const [displayContextByLiveViewId, setDisplayContextByLiveViewId] = useState<Record<string, StreamingCameraLiveContext>>({});

  const [tabVisible, setTabVisible] = useState<boolean>(() => {
    if (typeof document === "undefined") return true;
    return document.visibilityState === "visible";
  });

  useEffect(() => {
    if (embedded || typeof window === "undefined") return;
    try {
      localStorage.setItem(GRID_MODE_STORAGE_KEY, gridMode);
    } catch {
      // ignore
    }
  }, [embedded, gridMode]);

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
    if (typeof window === "undefined") return;
    try {
      localStorage.setItem(LIVE_VARIANT_OVERRIDE_STORAGE_KEY, JSON.stringify(variantOverrideByLiveViewId));
    } catch {
      // ignore
    }
  }, [variantOverrideByLiveViewId]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisibilityChange = () => setTabVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadLiveViews = async (isFirstLoad: boolean) => {
      if (isFirstLoad) setLoading(true);
      try {
        const payload = await listStreamingCameraLiveViews();
        if (cancelled) return;
        setLiveViews(Array.isArray(payload) ? payload : []);
        setError(null);
      } catch (loadError) {
        if (cancelled) return;
        setError(asErrorMessage(loadError));
      } finally {
        if (!cancelled && isFirstLoad) setLoading(false);
      }
    };

    void loadLiveViews(true);
    const intervalId = window.setInterval(() => {
      void loadLiveViews(false);
    }, TRANSMISSIONS_REFRESH_MS);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  const enabledLiveViews = useMemo(() => {
    const normalizedCameraId = String(cameraId || "").trim();
    const normalizedLiveViewId = String(liveViewId || "").trim();
    return liveViews.filter((item) => {
      if (!item || item.enabled === false) return false;
      if (normalizedLiveViewId && String(item.id || "").trim() !== normalizedLiveViewId) return false;
      if (normalizedCameraId && String(item.camera_id || "").trim() !== normalizedCameraId) return false;
      return true;
    });
  }, [cameraId, liveViewId, liveViews]);

  const pageSize = embedded || gridMode === "1x1" ? 1 : 4;
  const pageCount = Math.max(1, Math.ceil(enabledLiveViews.length / pageSize));
  const basePlaybackContext: StreamingCameraLiveContext = embedded ? defaultContext ?? "large" : gridMode === "1x1" ? "large" : "thumbnail";
  const contextForLiveView = useCallback(
    (liveViewId: string): StreamingCameraLiveContext => displayContextByLiveViewId[liveViewId] ?? basePlaybackContext,
    [basePlaybackContext, displayContextByLiveViewId],
  );

  const saveDefaultVariantForUse = useCallback(
    async (liveView: StreamingCameraLiveView, context: StreamingCameraLiveContext, variantId: string): Promise<void> => {
      const liveViewId = String(liveView.id || "").trim();
      const normalizedVariantId = String(variantId || "").trim();
      if (!liveViewId || !normalizedVariantId) return;
      const draft = liveViewWithDefaultVariant(liveView, context, normalizedVariantId);
      setSavingVariantDefaultByLiveViewId((previous) => ({ ...previous, [liveViewId]: true }));
      try {
        const updated = await updateStreamingCameraLiveView(liveViewId, draft);
        setLiveViews((previous) => previous.map((item) => (item.id === liveViewId ? updated : item)));
        setPlaybackByKey((previous) => {
          const next = { ...previous };
          for (const key of Object.keys(next)) {
            if (parsePlaybackKey(key).liveViewId === liveViewId && next[key]) {
              next[key] = { ...next[key], live_view: updated };
            }
          }
          return next;
        });
      } finally {
        setSavingVariantDefaultByLiveViewId((previous) => ({ ...previous, [liveViewId]: false }));
      }
    },
    [],
  );

  useEffect(() => {
    setPageIndex((previous) => Math.min(previous, Math.max(0, pageCount - 1)));
  }, [pageCount]);

  const currentPageItems = useMemo(() => {
    const start = pageIndex * pageSize;
    return enabledLiveViews.slice(start, start + pageSize);
  }, [enabledLiveViews, pageIndex, pageSize]);

  const currentPagePlaybackKeys = useMemo(
    () =>
      currentPageItems
        .map((item) => {
          const liveViewId = String(item.id || "").trim();
          return liveViewId
            ? playbackKeyFor(liveViewId, contextForLiveView(liveViewId), variantOverrideByLiveViewId[liveViewId])
            : "";
        })
        .filter(Boolean),
    [contextForLiveView, currentPageItems, variantOverrideByLiveViewId],
  );

  useEffect(() => {
    for (const liveView of currentPageItems) {
      const liveViewId = String(liveView.id || "").trim();
      if (!liveViewId) continue;
      const playbackContext = contextForLiveView(liveViewId);
      const variantId = variantOverrideByLiveViewId[liveViewId] ?? null;
      const playbackKey = playbackKeyFor(liveViewId, playbackContext, variantId);
      if (playbackByKey[playbackKey]) continue;
      if (playbackLoadingByKey[playbackKey]) continue;

      setPlaybackLoadingByKey((previous) => ({ ...previous, [playbackKey]: true }));
      void getStreamingCameraLiveViewPlayback(liveViewId, { context: playbackContext, variantId })
        .then((payload) => {
          setPlaybackByKey((previous) => ({ ...previous, [playbackKey]: payload }));
          setPlaybackErrorByKey((previous) => {
            if (!previous[playbackKey]) return previous;
            const next = { ...previous };
            delete next[playbackKey];
            return next;
          });
        })
        .catch((loadError) => {
          setPlaybackErrorByKey((previous) => ({
            ...previous,
            [playbackKey]: asErrorMessage(loadError),
          }));
        })
        .finally(() => {
          setPlaybackLoadingByKey((previous) => ({ ...previous, [playbackKey]: false }));
      });
    }
  }, [contextForLiveView, currentPageItems, playbackByKey, playbackLoadingByKey, variantOverrideByLiveViewId]);

  const currentPageTransmissionIds = useMemo(
    () =>
      currentPagePlaybackKeys
        .map((key) => String(playbackByKey[key]?.transmission?.id || "").trim())
        .filter(Boolean),
    [currentPagePlaybackKeys, playbackByKey],
  );

  useEffect(() => {
    if (!tabVisible || currentPagePlaybackKeys.length === 0) return;

    const inFlight = new Set<string>();
    const renewSignedUrls = () => {
      const nowUnix = Date.now() / 1000;
      for (const playbackKey of currentPagePlaybackKeys) {
        const playback = playbackByKey[playbackKey];
        const liveViewId = String(playback?.live_view?.id || playbackKey.split(":")[0] || "").trim();
        const urls = playback?.urls;
        const signedHlsOutput = urls?.outputs?.find(
          (output) =>
            output.protocol === "hls" &&
            output.media_auth_type === "signed_url" &&
            typeof output.renew_after_unix === "number" &&
            nowUnix >= Number(output.renew_after_unix),
        );
        if (!signedHlsOutput || !liveViewId || inFlight.has(playbackKey)) continue;

        inFlight.add(playbackKey);
        const keyParts = parsePlaybackKey(playbackKey);
        const context = keyParts.context || basePlaybackContext;
        void getStreamingCameraLiveViewPlayback(liveViewId, { context, variantId: keyParts.variantId })
          .then((payload) => {
            setPlaybackByKey((previous) => ({ ...previous, [playbackKey]: payload }));
            setPlaybackErrorByKey((previous) => {
              if (!previous[playbackKey]) return previous;
              const next = { ...previous };
              delete next[playbackKey];
              return next;
            });
          })
          .catch((loadError) => {
            setPlaybackErrorByKey((previous) => ({
              ...previous,
              [playbackKey]: asErrorMessage(loadError),
            }));
          })
          .finally(() => {
            inFlight.delete(playbackKey);
          });
      }
    };

    renewSignedUrls();
    const interval = window.setInterval(renewSignedUrls, 10_000);
    return () => window.clearInterval(interval);
  }, [basePlaybackContext, currentPagePlaybackKeys, playbackByKey, tabVisible]);

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
    const out: Array<StreamingCameraLiveView | null> = [...currentPageItems];
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
    <div className={["viewportRoot", "streamsRoot", embedded ? "isEmbedded" : ""].filter(Boolean).join(" ")}>
      {error ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">
              {t("core.ui.streams.unavailable", {}, "Streaming extension unavailable.")} {error}
            </div>
          </div>
        </div>
      ) : null}

      {!error && enabledLiveViews.length === 0 ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">
              {t("core.ui.streams.empty", {}, "Nenhuma transmissão ao vivo habilitada. Crie em Configurações > Transmissões.")}
            </div>
          </div>
        </div>
      ) : null}

      {!error && enabledLiveViews.length > 0 ? (
        <div className={["streamsGrid", embedded || gridMode === "1x1" ? "is1x1" : "is2x2"].join(" ")}>
          {pageTiles.map((liveView, slotIndex) => {
            if (!liveView) {
              return <div key={`slot-empty-${slotIndex}`} className="streamsTile streamsTileEmpty" />;
            }

            const liveViewId = String(liveView.id || "").trim();
            const playbackContext = contextForLiveView(liveViewId);
            const variantOverrideId = String(variantOverrideByLiveViewId[liveViewId] || "").trim();
            const playbackKey = playbackKeyFor(liveViewId, playbackContext, variantOverrideId);
            const playback = playbackByKey[playbackKey] ?? null;
            const transmission = playback?.transmission ?? null;
            const transmissionId = String(transmission?.id || "").trim();
            const qualityPreference = qualityPreferenceByTransmissionId[liveViewId] ?? "auto";
            const transportPreference = transportPreferenceByTransmissionId[liveViewId] ?? "auto";
            const desiredQualityProfileId = qualityProfileIdForPreference(qualityPreference, gridMode);
            const transmissionName = normalizeText(
              playback?.camera_name,
              normalizeText(liveView.name, liveViewId || `camera-${slotIndex + 1}`),
            );
            const urls = playback?.urls;
            const hlsOutput = transmission ? selectOutputByProtocol(transmission, urls, "hls", {
              qualityProfileId: desiredQualityProfileId,
            }) : null;
            const mseOutput = transmission ? selectOutputByProtocol(transmission, urls, "mse", {
              qualityProfileId: desiredQualityProfileId,
            }) : null;
            const jsmpegOutput = transmission ? selectOutputByProtocol(transmission, urls, "jsmpeg", {
              qualityProfileId: desiredQualityProfileId,
            }) : null;
            const webrtcOutput = transmission ? selectOutputByProtocol(transmission, urls, "webrtc") : null;
            const urlError = playbackErrorByKey[playbackKey];
            const urlLoading = Boolean(playbackLoadingByKey[playbackKey]);
            const runtimeHealth = transmissionId ? runtimeHealthByTransmissionId[transmissionId] : undefined;
            const webrtcUrl = webrtcOutput?.url ?? null;
            const mseUrl = mseOutput?.url ?? null;
            const jsmpegUrl = jsmpegOutput?.url ?? null;
            const hlsUrl = hlsOutput?.url ?? null;
            const webrtcAuthHeader = buildBasicAuthHeader(webrtcOutput?.auth ?? null);
            const hlsAuthHeader = buildBasicAuthHeader(hlsOutput?.auth ?? null);
            const hlsNativeUrl = hlsUrl ? withBasicAuthInUrl(hlsUrl, hlsOutput?.auth ?? null) : null;
            const tileActive =
              playersActive &&
              Boolean(
                (transportPreference !== "hls" && webrtcUrl) ||
                  (transportPreference === "auto" && mseUrl) ||
                  (transportPreference !== "webrtc" && hlsUrl) ||
                  (transportPreference === "auto" && jsmpegUrl),
              );
            const ptzEnabled = Boolean(transmission?.camera_controls?.enabled && liveView.defaults?.ptz_variant_id);
            const lowLatencyRequested = ptzOverlay?.transmissionId === transmissionId && transportPreference === "auto";
            const runtimeHint = buildRuntimeHealthHint(runtimeHealth, t, {
              suppressRecoveredClientTransportErrors: transportPreference !== "webrtc",
            });
            const clientPlaybackPlan = buildPlaybackPlan({
              transportPreference,
              urls,
              serverPlaybackPlan: playback?.playback_plan ?? null,
              mseUrl,
              hlsUrl,
              webrtcUrl,
              jsmpegUrl,
              lowLatencyRequested,
            });
            const plannedTransport = plannedPrimaryTransport(clientPlaybackPlan, mseUrl, hlsUrl, webrtcUrl, jsmpegUrl);
            const activeTransportWarnings = transportScopedWarnings(urls, plannedTransport, clientPlaybackPlan);
            const variantOptions = (liveView.variants ?? [])
              .filter((variant) => variant && variant.enabled !== false && String(variant.id || "").trim())
              .map((variant) => {
                const roleLabel = liveVariantRoleLabel(variant.role, t);
                const label = liveVariantQuickLabel(variant, t);
                const sourceSuffix = playback?.variant?.id === variant.id && playback?.camera_source_name
                  ? ` - ${playback.camera_source_name}`
                  : "";
                return {
                  id: String(variant.id || "").trim(),
                  label,
                  title: `${roleLabel}${sourceSuffix}`,
                };
              });
            const selectedVariantLabel = variantOverrideId
              ? variantOptions.find((option) => option.id === variantOverrideId)?.label || t("core.ui.streams.variant.custom", {}, "Custom")
              : playback?.variant?.label || liveContextLabel(playbackContext, t);
            const defaultVariantId = defaultVariantIdForContext(liveView, playbackContext);
            const canSetVariantDefault =
              Boolean(variantOverrideId) &&
              Boolean(variantOptions.some((option) => option.id === variantOverrideId)) &&
              variantOverrideId !== defaultVariantId;
            const savingVariantDefault = Boolean(savingVariantDefaultByLiveViewId[liveViewId]);

            let sourceHint: string | null = null;
            let sourceHintTone: "muted" | "warn" | "error" = "muted";
            if (urlLoading) {
              sourceHint = t("core.ui.streams.hint.loading_url", {}, "Carregando visualização…");
              sourceHintTone = "muted";
            } else if (urlError) {
              sourceHint = urlError;
              sourceHintTone = "error";
            } else if (
              (transportPreference === "webrtc" && !webrtcUrl) ||
              (transportPreference === "hls" && !hlsUrl) ||
              (transportPreference === "auto" && !mseUrl && !webrtcUrl && !hlsUrl && !jsmpegUrl)
            ) {
              sourceHint = t("core.ui.streams.hint.no_outputs", {}, "Nenhuma saída MSE/WebRTC/HLS/JSMpeg disponível para esta câmera.");
              sourceHintTone = "warn";
            } else if (playback?.blocking_errors?.length) {
              sourceHint = playback.blocking_errors.join(" ");
              sourceHintTone = "error";
            } else if (runtimeHint) {
              sourceHint = runtimeHint.message;
              sourceHintTone = runtimeHint.tone;
            } else if (runtimeHealthError) {
              sourceHint = runtimeHealthError;
              sourceHintTone = "warn";
            } else if (activeTransportWarnings.length) {
              sourceHint = activeTransportWarnings[0] || null;
              sourceHintTone = "warn";
            } else {
              const playbackWarning = primaryLivePlaybackWarning(playback);
              if (playbackWarning) {
                sourceHint = playbackWarning;
                sourceHintTone = "warn";
              } else if (playback?.camera_source_name) {
                sourceHint = `${liveContextLabel(playbackContext, t)}: ${playback.camera_source_name}`;
                sourceHintTone = "muted";
              }
            }

            return (
              <div key={liveViewId} className="streamsTile">
                <StreamTilePlayer
                  transmissionId={transmissionId}
                  outputId={webrtcOutput?.outputId ?? hlsOutput?.outputId ?? null}
                  mseOutputId={mseOutput?.outputId ?? null}
                  jsmpegOutputId={jsmpegOutput?.outputId ?? null}
                  webrtcOutputId={webrtcOutput?.outputId ?? null}
                  hlsOutputId={hlsOutput?.outputId ?? null}
                  hlsQualityProfileId={hlsOutput?.qualityProfileId ?? null}
                  label={transmissionName}
                  urls={urls}
                  playbackPlan={playback?.playback_plan ?? null}
                  overlayVisible={uiVisible}
                  sourceHint={sourceHint}
                  sourceHintTone={sourceHintTone}
                  mseUrl={mseUrl}
                  jsmpegUrl={jsmpegUrl}
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
                  variantOptions={variantOptions}
                  variantOverrideId={variantOverrideId}
                  currentVariantLabel={selectedVariantLabel}
                  canSetVariantDefault={canSetVariantDefault}
                  savingVariantDefault={savingVariantDefault}
                  onQualityPreferenceChange={(preference) => {
                    setQualityPreferenceByTransmissionId((previous) => ({
                      ...previous,
                      [liveViewId]: preference,
                    }));
                  }}
                  onTransportPreferenceChange={(preference) => {
                    setTransportPreferenceByTransmissionId((previous) => ({
                      ...previous,
                      [liveViewId]: preference,
                    }));
                  }}
                  onVariantOverrideChange={(variantId) => {
                    const normalizedVariantId = String(variantId || "").trim();
                    setVariantOverrideByLiveViewId((previous) => {
                      if (!normalizedVariantId) {
                        if (!previous[liveViewId]) return previous;
                        const next = { ...previous };
                        delete next[liveViewId];
                        return next;
                      }
                      if (previous[liveViewId] === normalizedVariantId) return previous;
                      return { ...previous, [liveViewId]: normalizedVariantId };
                    });
                  }}
                  onSetVariantDefault={() => saveDefaultVariantForUse(liveView, playbackContext, variantOverrideId)}
                  onRefreshUrls={async () => {
                    setPlaybackLoadingByKey((previous) => ({ ...previous, [playbackKey]: true }));
                    try {
                      const payload = await getStreamingCameraLiveViewPlayback(liveViewId, {
                        context: playbackContext,
                        variantId: variantOverrideId,
                      });
                      setPlaybackByKey((previous) => ({ ...previous, [playbackKey]: payload }));
                      setPlaybackErrorByKey((previous) => {
                        if (!previous[playbackKey]) return previous;
                        const next = { ...previous };
                        delete next[playbackKey];
                        return next;
                      });
                    } catch (refreshError) {
                      setPlaybackErrorByKey((previous) => ({
                        ...previous,
                        [playbackKey]: asErrorMessage(refreshError),
                      }));
                      throw refreshError;
                    } finally {
                      setPlaybackLoadingByKey((previous) => ({ ...previous, [playbackKey]: false }));
                    }
                  }}
                  onOpenPtz={() => {
                    if (transmissionId) setPtzOverlay({ transmissionId, label: transmissionName });
                  }}
                  onDisplayContextChange={(context) => {
                    setDisplayContextByLiveViewId((previous) => {
                      const normalizedContext = context === "pip" || context === "fullscreen" ? context : null;
                      if (!normalizedContext) {
                        if (!previous[liveViewId]) return previous;
                        const next = { ...previous };
                        delete next[liveViewId];
                        return next;
                      }
                      if (previous[liveViewId] === normalizedContext) return previous;
                      return { ...previous, [liveViewId]: normalizedContext };
                    });
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

      {!embedded ? <div className={["streamsHud", uiVisible ? "isVisible" : "isHidden"].join(" ")}>
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
      </div> : null}
    </div>
  );
}
