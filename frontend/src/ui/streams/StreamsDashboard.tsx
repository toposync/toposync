import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type Hls from "hls.js";

import {
  getStreamingTransmissionUrls,
  getStreamingRuntimeHealth,
  listStreamingTransmissions,
  postStreamingPlaybackEvents,
  primeStreamingTransmissionDemand,
  type StreamingQualityProfileId,
  type StreamingRuntimeTransmissionHealth,
  type StreamingTransmission,
  type StreamingTransmissionUrlsResponse,
} from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
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

function qualityPreferenceLabel(preference: StreamQualityPreference, t: ReturnType<typeof i18n.useI18n>["t"]): string {
  if (preference === "low") return t("core.ui.streams.quality.low", {}, "Low");
  if (preference === "stable") return t("core.ui.streams.quality.stable", {}, "Stable");
  if (preference === "high") return t("core.ui.streams.quality.high", {}, "High");
  if (preference === "diagnostic") return t("core.ui.streams.quality.diagnostic", {}, "Diagnostic");
  return t("core.ui.streams.quality.auto", {}, "Auto");
}

function transportPreferenceLabel(preference: StreamTransportPreference, t: ReturnType<typeof i18n.useI18n>["t"]): string {
  if (preference === "webrtc") return t("core.ui.streams.transport.low_latency", {}, "Low latency");
  if (preference === "hls") return t("core.ui.streams.transport.hls", {}, "HLS");
  return t("core.ui.streams.transport.auto", {}, "Auto");
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

function canPlayNativeHls(video: HTMLVideoElement): boolean {
  const check = video.canPlayType("application/vnd.apple.mpegurl");
  return check === "probably" || check === "maybe";
}

function canUseWebRtc(): boolean {
  return typeof RTCPeerConnection !== "undefined";
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

function formatWebRtcStats(stats: WebRtcStatsSummary | null): string {
  if (!stats) return "";
  const parts = [
    stats.iceConnectionState,
    stats.rttMs !== null ? `${stats.rttMs}ms RTT` : null,
    stats.packetLossPct !== null ? `${stats.packetLossPct.toFixed(1)}% loss` : null,
    stats.framesPerSecond !== null ? `${Math.round(stats.framesPerSecond)}fps` : null,
  ].filter(Boolean);
  return parts.join(" · ");
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

function buildRuntimeHealthHint(
  health: StreamingRuntimeTransmissionHealth | undefined,
  t: ReturnType<typeof i18n.useI18n>["t"],
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
      message: sourceHealth.recommended_action || sourceHealth.last_error || "Camera source error.",
      tone: "error",
    };
  }
  if (health.classification && health.classification !== "healthy" && health.classification !== "unknown") {
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

function StreamTilePlayer({
  transmissionId,
  outputId,
  webrtcOutputId,
  hlsOutputId,
  hlsQualityProfileId,
  label,
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
  qualityPreference,
  transportPreference,
  onQualityPreferenceChange,
  onTransportPreferenceChange,
  onOpenPtz,
}: {
  transmissionId: string;
  outputId: string | null;
  webrtcOutputId: string | null;
  hlsOutputId: string | null;
  hlsQualityProfileId: StreamingQualityProfileId | null;
  label: string;
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
  qualityPreference: StreamQualityPreference;
  transportPreference: StreamTransportPreference;
  onQualityPreferenceChange: (preference: StreamQualityPreference) => void;
  onTransportPreferenceChange: (preference: StreamTransportPreference) => void;
  onOpenPtz: () => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);
  const playbackSessionIdRef = useRef<string | null>(null);

  const [status, setStatus] = useState<TilePlaybackStatus>("idle");
  const [transport, setTransport] = useState<TilePlaybackTransport>("none");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [webRtcStats, setWebRtcStats] = useState<WebRtcStatsSummary | null>(null);
  const [webRtcFallbackActive, setWebRtcFallbackActive] = useState(false);
  const [pictureInPictureActive, setPictureInPictureActive] = useState(false);
  const playbackActive = active || pictureInPictureActive;

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
    const onPlay = () => recordWebPlaybackEvent("play", { severity: "debug" });
    const onPlaying = () => recordWebPlaybackEvent("playing", { severity: "info" });
    const onWaiting = () => recordWebPlaybackEvent("waiting", { severity: "warn" });
    const onStalled = () => recordWebPlaybackEvent("stalled", { severity: "warn" });
    const onError = () =>
      recordWebPlaybackEvent("error", {
        severity: "error",
        message: video.error?.message || "HTML video playback error.",
        data: { code: video.error?.code },
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
  }, [playbackActive, recordWebPlaybackEvent]);

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
    const allowWebRtc = transportPreference !== "hls";
    const allowHls = transportPreference !== "webrtc";

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
        data: { attempt, delay_ms: delayMs },
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

    const startNativeHlsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      setTransport("hls");
      const sourceUrl = String(hlsNativeUrl || hlsUrl || "").trim();
      const onPlaying = () => {
        setStatus("playing");
        setErrorText(null);
      };
      const onError = () => {
        setStatus("error");
        const message = i18n.t("core.ui.streams.errors.hls_native_error", {}, "Native HLS playback error.");
        setErrorText(message);
        destroyPlayback();
        scheduleRetry(message);
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
        setErrorText(details);
        destroyPlayback();
        scheduleRetry(details);
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
          data: {
            ice_connection_state: nextPeerConnection.iceConnectionState,
            connection_state: nextPeerConnection.connectionState,
          },
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
            recordWebPlaybackEvent("webrtc_stats", { severity: "debug", data: stats });
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
          data: { output_id: selectedOutputId, quality_profile_id: selectedQualityProfileId },
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

    const startPlayback = async () => {
      if (cancelled || !playbackActive || ((!allowHls || !hlsUrl) && (!allowWebRtc || !webrtcUrl))) return;
      const video = videoRef.current;
      if (!video) return;
      if (!playbackSessionIdRef.current) {
        playbackSessionIdRef.current = `${transmissionId || "stream"}:web:${Date.now()}:${Math.floor(Math.random() * 1_000_000)}`;
      }
      recordWebPlaybackEvent("load", {
        severity: "info",
        data: { has_webrtc: Boolean(webrtcUrl), has_hls: Boolean(hlsUrl), transport_preference: transportPreference },
      });

      destroyPlayback();
      configureVideo(video);
      setStatus("loading");
      setErrorText(null);
      setTransport("none");
      setWebRtcFallbackActive(false);
      if (cancelled) return;

      let webRtcError: string | null = null;
      if (allowWebRtc && webrtcUrl) {
        await primePlaybackOutput(webrtcOutputId ?? outputId, null);
        for (let attemptIndex = 0; attemptIndex < WEBRTC_WHEP_READY_ATTEMPTS; attemptIndex += 1) {
          try {
            recordWebPlaybackEvent("webrtc_start", { severity: "info", data: { attempt_index: attemptIndex } });
            await startWebRtcPlayback(video);
            return;
          } catch (error) {
            const message = asErrorMessage(error);
            webRtcError = message;
            recordWebPlaybackEvent("webrtc_signaling_error", {
              severity: "warn",
              message,
              data: { attempt_index: attemptIndex },
            });
            const normalizedMessage = message.toLowerCase();

            const shouldRetry =
              attemptIndex < WEBRTC_WHEP_READY_ATTEMPTS - 1 &&
              (normalizedMessage.includes("(404)") ||
                normalizedMessage.includes("no stream is available") ||
                normalizedMessage.includes("path has no one publishing"));
            if (!shouldRetry) break;

            await new Promise((resolve) => window.setTimeout(resolve, WEBRTC_WHEP_READY_RETRY_MS));
            if (cancelled) return;
          }
        }
      }

      if (allowHls && hlsUrl) {
        try {
          if (webRtcError) {
            setWebRtcFallbackActive(true);
            recordWebPlaybackEvent("webrtc_fallback_hls", {
              severity: "warn",
              message: webRtcError,
              data: { transport_preference: transportPreference },
            });
          }
          await primePlaybackOutput(hlsOutputId ?? outputId, hlsQualityProfileId);
          recordWebPlaybackEvent("hls_start", { severity: "info" });
          await startHlsPlayback(video);
          return;
        } catch (error) {
          const hlsError = asErrorMessage(error);
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
    playbackActive,
    recordWebPlaybackEvent,
    transmissionId,
    transportPreference,
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

  const webRtcStatsLabel = transport === "webrtc" ? formatWebRtcStats(webRtcStats) : "";

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

  return (
    <div className="streamsPlayerFrame" ref={frameRef}>
      <video ref={videoRef} className="streamsVideo" muted playsInline autoPlay />

	      <div className={["streamsTileOverlay", overlayVisible ? "isVisible" : "isHidden"].join(" ")}>
        <div className="streamsTileOverlayLeft" title={label}>
          <span className={["streamsPlaybackDot", `is-${playbackDotStatus}`].join(" ")} />
          <span className="streamsTileOverlayTitle">{label}</span>
          <span className="streamsTileOverlayMeta">{playbackStatusLabel}</span>
          {webRtcFallbackActive ? (
            <span className="streamsTileOverlayMeta" title={t("core.ui.streams.transport.hls_fallback_hint", {}, "WebRTC failed; using HLS fallback.")}>
              {t("core.ui.streams.transport.hls_fallback", {}, "HLS fallback")}
            </span>
          ) : null}
          {webRtcStatsLabel ? (
            <span className="streamsTileOverlayMeta" title={webRtcStatsLabel}>
              {webRtcStatsLabel}
            </span>
          ) : null}
        </div>

	        <div className="streamsTileOverlayActions">
          <select
            className="streamsTileQualitySelect"
            aria-label={t("core.ui.streams.transport.label", {}, "Stream transport")}
            title={t("core.ui.streams.transport.label", {}, "Stream transport")}
            value={transportPreference}
            onChange={(event) => onTransportPreferenceChange(event.target.value as StreamTransportPreference)}
          >
            {(["auto", "webrtc", "hls"] as const).map((preference) => (
              <option key={preference} value={preference}>
                {transportPreferenceLabel(preference, t)}
              </option>
            ))}
          </select>
          <select
            className="streamsTileQualitySelect"
            aria-label={t("core.ui.streams.quality.label", {}, "Stream quality")}
            title={t("core.ui.streams.quality.label", {}, "Stream quality")}
            value={qualityPreference}
            onChange={(event) => onQualityPreferenceChange(event.target.value as StreamQualityPreference)}
          >
            {(["auto", "low", "stable", "high", "diagnostic"] as const).map((preference) => (
              <option key={preference} value={preference}>
                {qualityPreferenceLabel(preference, t)}
              </option>
            ))}
          </select>
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
            const runtimeHint = buildRuntimeHealthHint(runtimeHealth, t);
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
                  onOpenPtz={() => {
                    if (transportPreference === "hls" && webrtcUrl) {
                      setTransportPreferenceByTransmissionId((previous) => ({
                        ...previous,
                        [transmissionId]: "auto",
                      }));
                    }
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
