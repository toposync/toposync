import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { SettingsPanel, TopoSyncHost } from "@toposync/plugin-api";

import {
  clearStreamingEncoderQuarantine,
  createTransmission,
  deleteCameraLiveView,
  deleteTransmission,
  fetchCameraIngestAuth,
  fetchCameraLiveViews,
  fetchCamerasIndex,
  fetchEngineStatus,
  fetchProcessingServers,
  fetchStreamsHealth,
  fetchStreamingHlsProbe,
  fetchStreamingDiagnosticSnapshot,
  fetchStreamingRuntimeObservability,
  fetchStreamingRuntimeEncoders,
  fetchStreamingRuntimeHealth,
  fetchStreamingRuntimePipelines,
  fetchStreamingQualityProfiles,
  fetchStreamingSettings,
  fetchTransmissionUrls,
  fetchTransmissions,
  generateCameraLiveViews,
  applyTransmissionQualityProfiles,
  applyTransmissionWebRtcCompanion,
  patchStreamingSettings,
  postEngineAction,
  postEngineDownload,
  revealCameraIngestAuth,
  rotateCameraIngestAuth,
  updateTransmission,
  updateCameraLiveView,
} from "../api/streamingApi";
import { STREAMING_EXTENSION_ID } from "../constants";
import type {
  CameraIndexItem,
  CameraLiveContext,
  CameraLiveVariant,
  CameraLiveView,
  EngineStatusResponse,
  ProcessingServer,
  StreamingCameraIngestAuthResponse,
  StreamingEngineSettings,
  StreamingExtensionSettings,
  StreamingHlsProbeResponse,
  StreamingRuntimeHealthResponse,
  StreamingRuntimeEncodersResponse,
  StreamingRuntimeObservabilityResponse,
  StreamingRuntimePipelineLink,
  StreamingRuntimePipelinesResponse,
  StreamingRuntimeOutputHealth,
  StreamingRuntimeStatus,
  StreamingRuntimeTransmissionHealth,
  StreamingQualityProfile,
  StreamingQualityProfilesResponse,
  StreamingQualityProfileId,
  StreamAuthentication,
  StreamsHealthResponse,
  Transmission,
  TransmissionOutput,
  TransmissionUrlsResponse,
} from "../types";
import { SubModal } from "./SubModal";
import { WizardCreatePipelineFromTransmission } from "./WizardCreatePipelineFromTransmission";

type TranslateFn = (key: string, params?: Record<string, unknown>, fallback?: string) => string;

function toSafeInt(value: string, fallback: number): number {
  const parsed = Number.parseInt(String(value || "").trim(), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return parsed;
}

function toOptionalInt(value: string): number | null {
  const trimmed = String(value || "").trim();
  if (!trimmed) return null;
  const parsed = Number.parseInt(trimmed, 10);
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

function slugifyPath(value: string): string {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return "";
  const filtered = Array.from(text)
    .map((ch) => (/[a-z0-9_-]/.test(ch) ? ch : "-"))
    .join("");
  return filtered.replace(/-+/g, "-").replace(/^[-_]+|[-_]+$/g, "");
}

function defaultCameraSourceId(camera: CameraIndexItem | null | undefined): string {
  const sources = camera?.sources || [];
  return String(
    sources.find((source) => source.enabled !== false && source.is_default)?.id ||
      sources.find((source) => source.enabled !== false)?.id ||
      "",
  ).trim();
}

function cameraSourceOptions(camera: CameraIndexItem | null | undefined): Array<{ id: string; label: string }> {
  return (camera?.sources || [])
    .filter((source) => (String(source.kind || "video").trim().toLowerCase() || "video") === "video" && source.enabled !== false)
    .map((source) => {
      const id = String(source.id || "").trim();
      const name = String(source.name || "").trim() || id;
      const width = typeof source.video?.width === "number" ? source.video.width : null;
      const height = typeof source.video?.height === "number" ? source.video.height : null;
      const resolution = width && height ? ` · ${width}x${height}` : "";
      return { id, label: `${name}${resolution}` };
    })
    .filter((item) => item.id);
}

function cameraById(cameras: CameraIndexItem[], cameraId: string): CameraIndexItem | null {
  const normalized = String(cameraId || "").trim();
  return cameras.find((item) => String(item.id || "").trim() === normalized) ?? null;
}

function cameraSourceLabel(cameras: CameraIndexItem[], cameraId: string, sourceId: string): string {
  const camera = cameraById(cameras, cameraId);
  const source = (camera?.sources || []).find((item) => String(item.id || "").trim() === String(sourceId || "").trim());
  const name = String(source?.name || "").trim() || String(sourceId || "").trim() || "Fonte";
  const width = typeof source?.video?.width === "number" ? source.video.width : null;
  const height = typeof source?.video?.height === "number" ? source.video.height : null;
  return width && height ? `${name} · ${width}x${height}` : name;
}

function liveContextLabel(context: CameraLiveContext | "zoom" | "custom", t: TranslateFn): string {
  if (context === "thumbnail") return t("ext.streaming.live.context.thumbnail", {}, "Miniatura");
  if (context === "pip") return t("ext.streaming.live.context.pip", {}, "PiP");
  if (context === "large") return t("ext.streaming.live.context.large", {}, "Tela grande");
  if (context === "fullscreen") return t("ext.streaming.live.context.fullscreen", {}, "Tela cheia");
  if (context === "ptz") return t("ext.streaming.live.context.ptz", {}, "PTZ");
  if (context === "zoom") return t("ext.streaming.live.context.zoom", {}, "Zoom");
  return t("ext.streaming.live.context.custom", {}, "Personalizada");
}

function qualityProfileUiLabel(profileId: string | null | undefined, t: TranslateFn): string {
  if (profileId === "quad_grid") return t("ext.streaming.live.quality.light", {}, "Leve");
  if (profileId === "stable_apple_tv") return t("ext.streaming.live.quality.stable", {}, "Estável");
  if (profileId === "fullscreen_quality") return t("ext.streaming.live.quality.high", {}, "Alta");
  if (profileId === "diagnostic_low") return t("ext.streaming.live.quality.diagnostic", {}, "Diagnóstico");
  return t("ext.streaming.live.quality.auto", {}, "Automática");
}

function transportUiLabel(value: string | null | undefined, t: TranslateFn): string {
  if (value === "hls") return t("ext.streaming.live.transport.stable", {}, "Estável");
  if (value === "webrtc") return t("ext.streaming.live.transport.low_latency", {}, "Baixa latência");
  return t("ext.streaming.live.transport.auto", {}, "Automático");
}

function liveVariantConsequence(variant: CameraLiveVariant, cameras: CameraIndexItem[], cameraId: string, t: TranslateFn): string {
  const sourceLabel = cameraSourceLabel(cameras, cameraId, variant.camera_source_id);
  if (variant.preferred_transport === "webrtc") {
    return t("ext.streaming.live.consequence.low_latency", { source: sourceLabel }, `Usa ${sourceLabel} com menor latência quando disponível.`);
  }
  if (variant.quality_profile_id === "quad_grid" || variant.role === "thumbnail") {
    return t("ext.streaming.live.consequence.light", { source: sourceLabel }, `Usa ${sourceLabel} para economizar rede e CPU.`);
  }
  if (variant.quality_profile_id === "fullscreen_quality") {
    return t("ext.streaming.live.consequence.high", { source: sourceLabel }, `Usa ${sourceLabel} para melhor imagem em tela grande.`);
  }
  return t("ext.streaming.live.consequence.stable", { source: sourceLabel }, `Usa ${sourceLabel} com reprodução estável.`);
}

function deepClone<T>(value: T): T {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value)) as T;
}

function createLocalId(prefix: string): string {
  try {
    const maybeCrypto = (globalThis as unknown as { crypto?: Crypto }).crypto;
    if (maybeCrypto?.randomUUID) return `${prefix}_${maybeCrypto.randomUUID()}`;
  } catch {
    // ignore
  }
  return `${prefix}_${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
}

async function copyToClipboard(text: string): Promise<void> {
  const payload = String(text ?? "");
  try {
    await navigator.clipboard.writeText(payload);
    return;
  } catch {
    // Fallback: alguns browsers bloqueiam clipboard sem gesture/perm.
  }
  const textarea = document.createElement("textarea");
  textarea.value = payload;
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  textarea.setAttribute("readonly", "true");
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
  }
}

function defaultAuthentication(): StreamAuthentication {
  return { enabled: false, username: "", password: "" };
}

function parseIceServers(value: string): string[] {
  const rawItems = String(value || "")
    .replace(/\r/g, "")
    .split(/[\n,]/g);
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const item of rawItems) {
    const text = String(item || "").trim();
    if (!text) continue;
    const lowered = text.toLowerCase();
    if (!lowered.startsWith("stun:") && !lowered.startsWith("turn:") && !lowered.startsWith("turns:")) continue;
    if (seen.has(lowered)) continue;
    seen.add(lowered);
    normalized.push(text);
  }
  return normalized;
}

function joinIceServers(values: string[] | undefined): string {
  if (!Array.isArray(values) || values.length === 0) return "";
  return values
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .join("\n");
}

function parseStringList(value: string): string[] {
  const rawItems = String(value || "")
    .replace(/\r/g, "")
    .split(/[\n,]/g);
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const item of rawItems) {
    const text = String(item || "").trim();
    if (!text) continue;
    const lowered = text.toLowerCase();
    if (seen.has(lowered)) continue;
    seen.add(lowered);
    normalized.push(text);
  }
  return normalized;
}

function formatDuration(seconds: number | null | undefined): string {
  if (!Number.isFinite(seconds) || !seconds || seconds < 1) return "-";
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function formatRuntimeAge(seconds: number | null | undefined): string {
  if (!Number.isFinite(seconds ?? NaN)) return "-";
  const value = Math.max(0, Number(seconds));
  if (value < 10) return `${value.toFixed(1)}s`;
  if (value < 60) return `${Math.round(value)}s`;
  const minutes = Math.floor(value / 60);
  const secs = Math.round(value % 60);
  return `${minutes}m ${secs}s`;
}

function formatRuntimeUnixTime(unixSeconds: number | null | undefined): string {
  if (!Number.isFinite(unixSeconds ?? NaN) || !unixSeconds) return "-";
  try {
    return new Date(Number(unixSeconds) * 1000).toLocaleString();
  } catch {
    return "-";
  }
}

function compactRuntimeId(value: string | null | undefined): string {
  const text = String(value || "").trim();
  if (!text) return "-";
  if (text.length <= 18) return text;
  return `${text.slice(0, 10)}...${text.slice(-5)}`;
}

function sourceHealthTitle(source: { last_error?: string | null; recommended_action?: string; last_frame_at_unix?: number | null } | null | undefined): string {
  if (!source) return "";
  return [source.last_error || "", source.recommended_action || "", formatRuntimeUnixTime(source.last_frame_at_unix)]
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .join("\n");
}

function runtimeStatusLabel(status: StreamingRuntimeStatus | undefined, t: TranslateFn): string {
  if (status === "live") return t("ext.streaming.runtime.status.live", {}, "Live");
  if (status === "degraded") return t("ext.streaming.runtime.status.degraded", {}, "Degraded");
  if (status === "stale") return t("ext.streaming.runtime.status.stale", {}, "Stale");
  if (status === "offline") return t("ext.streaming.runtime.status.offline", {}, "Offline");
  return "-";
}

function runtimeStatusClass(status: StreamingRuntimeStatus | undefined): string {
  if (status === "live") return "is-live";
  if (status === "degraded") return "is-degraded";
  if (status === "stale") return "is-stale";
  if (status === "offline") return "is-offline";
  return "is-unknown";
}

function observabilityClassificationLabel(value: string | undefined): string {
  if (value === "source_stale") return "Camera source stale";
  if (value === "source_pipeline_stale") return "Source/pipeline stale";
  if (value === "publisher_down") return "Publisher down";
  if (value === "hls_playlist_stale") return "HLS playlist stale";
  if (value === "hls_tail_unavailable") return "HLS tail unavailable";
  if (value === "webrtc_transport_error") return "WebRTC transport";
  if (value === "network_contract_error") return "Network contract";
  if (value === "auth_url_error") return "Auth/URL error";
  if (value === "app_player_lifecycle") return "Player lifecycle";
  if (value === "event_gated_idle") return "Waiting event";
  if (value === "healthy") return "Healthy";
  return "Unknown";
}

function encoderModeLabel(value: string | undefined, t: TranslateFn): string {
  if (value === "cpu") return t("ext.streaming.encoder.mode.cpu", {}, "CPU only");
  if (value === "auto") return t("ext.streaming.encoder.mode.auto", {}, "Auto controlled");
  return t("ext.streaming.encoder.mode.inherit", {}, "Inherit");
}

function encoderStateLabel(value: string | undefined, t: TranslateFn): string {
  if (value === "trusted") return t("ext.streaming.encoder.state.trusted", {}, "Trusted");
  if (value === "quarantined") return t("ext.streaming.encoder.state.quarantined", {}, "Quarantined");
  return t("ext.streaming.encoder.state.candidate", {}, "Candidate");
}

function encoderStateClass(value: string | undefined): string {
  if (value === "trusted") return "is-live";
  if (value === "quarantined") return "is-stale";
  return "is-unknown";
}

function fallbackReasonLabel(reason: StreamingRuntimeTransmissionHealth["fallback_reason"], t: TranslateFn): string {
  if (reason === "no_active_writer") return t("ext.streaming.runtime.fallback.no_active_writer", {}, "No active writer");
  if (reason === "selected_writer_missing_frame") {
    return t("ext.streaming.runtime.fallback.selected_writer_missing_frame", {}, "Selected writer has no frame");
  }
  if (reason === "no_frame") return t("ext.streaming.runtime.fallback.no_frame", {}, "No frame");
  return "-";
}

function boolLabel(value: boolean | undefined, t: TranslateFn): string {
  return value ? t("ext.streaming.common.yes", {}, "Yes") : t("ext.streaming.common.no", {}, "No");
}

function streamBehaviorLabel(value: string | undefined, t: TranslateFn): string {
  if (value === "event_gated") return t("ext.streaming.runtime.stream_behavior.event_gated", {}, "Events only");
  return t("ext.streaming.runtime.stream_behavior.continuous", {}, "Continuous");
}

function qualityProfileLabel(profileId: string | null | undefined, profiles: StreamingQualityProfile[], t: TranslateFn): string {
  const normalized = String(profileId || "").trim();
  if (!normalized) return t("ext.streaming.quality.custom", {}, "Custom");
  const profile = profiles.find((item) => item.id === normalized);
  if (profile) return profile.label;
  return normalized;
}

function formatResolution(resolution: { width?: number; height?: number } | null | undefined): string {
  const width = Number(resolution?.width ?? 0);
  const height = Number(resolution?.height ?? 0);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return "-";
  return `${Math.round(width)}x${Math.round(height)}`;
}

function formatOutputNetworkCost(bitrateKbps: number | null | undefined, t: TranslateFn): string {
  const value = Number(bitrateKbps ?? 0);
  if (!Number.isFinite(value) || value <= 0) return t("ext.streaming.quality.network_custom", {}, "Network cost: custom");
  if (value >= 1000) {
    return t(
      "ext.streaming.quality.network_mbps",
      { mbps: (value / 1000).toFixed(1) },
      `Network cost: ${(value / 1000).toFixed(1)} Mbps`,
    );
  }
  return t("ext.streaming.quality.network_kbps", { kbps: Math.round(value) }, `Network cost: ${Math.round(value)} kbps`);
}

function hlsProbeStatusLabel(status: StreamingHlsProbeResponse["status"] | undefined, t: TranslateFn): string {
  if (status === "ok") return t("ext.streaming.hls_probe.status.ok", {}, "OK");
  if (status === "engine_stopped") return t("ext.streaming.hls_probe.status.engine_stopped", {}, "Engine stopped");
  if (status === "no_hls_output") return t("ext.streaming.hls_probe.status.no_hls_output", {}, "No HLS output");
  if (status === "playlist_unreachable") return t("ext.streaming.hls_probe.status.playlist_unreachable", {}, "Playlist unreachable");
  if (status === "tail_unavailable") return t("ext.streaming.hls_probe.status.tail_unavailable", {}, "Tail unavailable");
  if (status === "probe_error") return t("ext.streaming.hls_probe.status.probe_error", {}, "Probe error");
  return "-";
}

function defaultOutput(protocol: "hls" | "rtsp" | "webrtc"): TransmissionOutput {
  return {
    id: createLocalId("output"),
    protocol,
    enabled: true,
    resolution: { width: 1280, height: 720 },
    fps_limit: 12,
    bitrate_kbps: null,
    latency_profile: "normal",
    encoder_mode: "inherit",
    quality_profile_id: null,
    authentication: defaultAuthentication(),
  };
}

function normalizeServerId(value: string | undefined): string {
  const normalized = String(value || "").trim().toLowerCase();
  return normalized || "local";
}

function sortProcessingServers(servers: ProcessingServer[]): ProcessingServer[] {
  const local = servers.find((item) => normalizeServerId(item.id) === "local") ?? null;
  const rest = servers
    .filter((item) => normalizeServerId(item.id) !== "local")
    .sort((a, b) => String(a.id || "").localeCompare(String(b.id || "")));
  return local ? [local, ...rest] : [{ id: "local", name: "Local", kind: "inprocess", url: "" }, ...rest];
}

function openPipelineScreen(pipelineName: string): void {
  if (typeof window === "undefined") return;
  const name = String(pipelineName || "").trim();
  if (!name) return;
  const target = `/settings/pipelines/${encodeURIComponent(name)}`;
  if (window.location.pathname === target) return;
  window.history.pushState(null, "", target);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function createStreamingSettingsPanel(): SettingsPanel {
  return {
    id: STREAMING_EXTENSION_ID,
    icon: "tower-broadcast",
    name: { key: "ext.streaming.settings.name", fallback: "Transmissões" },
    description: { key: "ext.streaming.settings.desc" },
    render: ({ i18n, settings }) => <StreamingSettingsPanelContent i18n={i18n} settings={settings} />,
  };
}

function StreamingSettingsPanelContent({
  i18n,
  settings,
}: {
  i18n: TopoSyncHost["i18n"];
  settings: Record<string, unknown>;
}): React.ReactElement {
  const { t } = i18n.useI18n();

  const [healthLoading, setHealthLoading] = useState(true);
  const [health, setHealth] = useState<StreamsHealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  const [engineLoading, setEngineLoading] = useState(true);
  const [engineBusy, setEngineBusy] = useState(false);
  const [enginePendingAction, setEnginePendingAction] = useState<"start" | "stop" | "restart" | "reclaim" | "download" | "refresh" | null>(null);
  const [engineStatus, setEngineStatus] = useState<EngineStatusResponse | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);

  const [runtimeHealthLoading, setRuntimeHealthLoading] = useState(true);
  const [runtimeHealth, setRuntimeHealth] = useState<StreamingRuntimeHealthResponse | null>(null);
  const [runtimeHealthError, setRuntimeHealthError] = useState<string | null>(null);
  const [runtimePipelines, setRuntimePipelines] = useState<StreamingRuntimePipelinesResponse | null>(null);
  const [runtimePipelinesError, setRuntimePipelinesError] = useState<string | null>(null);
  const [runtimeObservability, setRuntimeObservability] = useState<StreamingRuntimeObservabilityResponse | null>(null);
  const [runtimeObservabilityError, setRuntimeObservabilityError] = useState<string | null>(null);
  const [runtimeEncoders, setRuntimeEncoders] = useState<StreamingRuntimeEncodersResponse | null>(null);
  const [runtimeEncodersError, setRuntimeEncodersError] = useState<string | null>(null);
  const [runtimeEncoderClearBusy, setRuntimeEncoderClearBusy] = useState(false);
  const [cameraIngestAuth, setCameraIngestAuth] = useState<StreamingCameraIngestAuthResponse | null>(null);
  const [cameraIngestAuthLoading, setCameraIngestAuthLoading] = useState(false);
  const [cameraIngestAuthBusy, setCameraIngestAuthBusy] = useState(false);
  const [cameraIngestAuthError, setCameraIngestAuthError] = useState<string | null>(null);
  const [cameraIngestRevealed, setCameraIngestRevealed] = useState(false);
  const [cameraIngestCopied, setCameraIngestCopied] = useState<string | null>(null);
  const [runtimeDiagnosticsBusy, setRuntimeDiagnosticsBusy] = useState(false);
  const [runtimeDiagnosticsError, setRuntimeDiagnosticsError] = useState<string | null>(null);
  const [hlsProbeByKey, setHlsProbeByKey] = useState<Record<string, StreamingHlsProbeResponse>>({});
  const [hlsProbeLastChangeByKey, setHlsProbeLastChangeByKey] = useState<Record<string, number>>({});
  const [hlsProbeBusyKey, setHlsProbeBusyKey] = useState<string | null>(null);
  const [hlsProbeError, setHlsProbeError] = useState<string | null>(null);

  const [settingsLoading, setSettingsLoading] = useState(true);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [extensionSettings, setExtensionSettings] = useState<StreamingExtensionSettings | null>(null);
  const [engineSettingsDraft, setEngineSettingsDraft] = useState<StreamingEngineSettings | null>(null);
  const [engineSettingsDirty, setEngineSettingsDirty] = useState(false);
  const [engineSettingsBusy, setEngineSettingsBusy] = useState(false);

  const [transmissionsLoading, setTransmissionsLoading] = useState(true);
  const [transmissionsError, setTransmissionsError] = useState<string | null>(null);
  const [transmissions, setTransmissions] = useState<Transmission[]>([]);
  const [transmissionQuery, setTransmissionQuery] = useState("");
  const [cameraLiveViewsLoading, setCameraLiveViewsLoading] = useState(true);
  const [cameraLiveViewsError, setCameraLiveViewsError] = useState<string | null>(null);
  const [cameraLiveViews, setCameraLiveViews] = useState<CameraLiveView[]>([]);
  const [cameraLiveGenerateBusy, setCameraLiveGenerateBusy] = useState(false);
  const [cameraLiveGenerateMessage, setCameraLiveGenerateMessage] = useState<string | null>(null);
  const [activeCameraLiveViewId, setActiveCameraLiveViewId] = useState<string | null>(null);
  const [cameraLiveDraft, setCameraLiveDraft] = useState<CameraLiveView | null>(null);
  const [cameraLiveDraftDirty, setCameraLiveDraftDirty] = useState(false);
  const [cameraLiveDraftBusy, setCameraLiveDraftBusy] = useState(false);
  const [cameraLiveDraftError, setCameraLiveDraftError] = useState<string | null>(null);
  const [qualityProfiles, setQualityProfiles] = useState<StreamingQualityProfilesResponse | null>(null);
  const [qualityProfilesError, setQualityProfilesError] = useState<string | null>(null);
  const [applyQualityProfilesBusy, setApplyQualityProfilesBusy] = useState(false);
  const [applyWebRtcCompanionBusy, setApplyWebRtcCompanionBusy] = useState(false);

  const [activeTransmissionId, setActiveTransmissionId] = useState<string | null>(null);
  const [pendingTransmissionId, setPendingTransmissionId] = useState<string | null>(null);
  const [confirmDiscardOpen, setConfirmDiscardOpen] = useState(false);

  const [transmissionDraft, setTransmissionDraft] = useState<Transmission | null>(null);
  const [transmissionDraftDirty, setTransmissionDraftDirty] = useState(false);
  const [transmissionDraftBusy, setTransmissionDraftBusy] = useState(false);
  const [transmissionDraftError, setTransmissionDraftError] = useState<string | null>(null);

  const [urlsByTransmissionId, setUrlsByTransmissionId] = useState<Record<string, TransmissionUrlsResponse>>({});
  const [urlsLoadingId, setUrlsLoadingId] = useState<string | null>(null);
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);
  const [processingServersLoading, setProcessingServersLoading] = useState(true);
  const [processingServersError, setProcessingServersError] = useState<string | null>(null);
  const [processingServers, setProcessingServers] = useState<ProcessingServer[]>([
    { id: "local", name: "Local", kind: "inprocess", url: "" },
  ]);

  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [newTransmissionName, setNewTransmissionName] = useState("");
  const [newTransmissionPath, setNewTransmissionPath] = useState("");
  const [newTransmissionHostServerId, setNewTransmissionHostServerId] = useState("local");
  const [newOutputProtocol, setNewOutputProtocol] = useState<"hls" | "rtsp" | "webrtc">("hls");
  const [newOutputWidth, setNewOutputWidth] = useState("1280");
  const [newOutputHeight, setNewOutputHeight] = useState("720");
  const [newOutputFps, setNewOutputFps] = useState("12");

  const [newCameraControlsEnabled, setNewCameraControlsEnabled] = useState(false);
  const [newCameraControlsCameraId, setNewCameraControlsCameraId] = useState("");
  const [newCameraControlsSourceId, setNewCameraControlsSourceId] = useState("");
  const [availableCamerasLoading, setAvailableCamerasLoading] = useState(false);
  const [availableCamerasError, setAvailableCamerasError] = useState<string | null>(null);
  const [availableCameras, setAvailableCameras] = useState<CameraIndexItem[]>([]);

  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [wizardTransmission, setWizardTransmission] = useState<Transmission | null>(null);

  const fallbackCount = useMemo(() => {
    const raw = settings.transmissions;
    return Array.isArray(raw) ? raw.length : 0;
  }, [settings.transmissions]);

  const transmissionsCount = transmissions.length > 0 ? transmissions.length : fallbackCount;

  const fetchHealthData = useCallback(async (signal?: AbortSignal) => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      const payload = await fetchStreamsHealth(signal);
      setHealth(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setHealth(null);
      setHealthError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setHealthLoading(false);
    }
  }, []);

  const fetchEngineData = useCallback(async (signal?: AbortSignal) => {
    setEngineLoading(true);
    setEngineError(null);
    try {
      const payload = await fetchEngineStatus(signal);
      setEngineStatus(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setEngineStatus(null);
      setEngineError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setEngineLoading(false);
    }
  }, []);

  const fetchRuntimeHealthData = useCallback(async (signal?: AbortSignal, showLoading = false) => {
    if (showLoading) setRuntimeHealthLoading(true);
    setRuntimeHealthError(null);
    try {
      const payload = await fetchStreamingRuntimeHealth(signal);
      if (signal?.aborted) return;
      setRuntimeHealth(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRuntimeHealthError(error instanceof Error ? error.message : String(error));
    } finally {
      if (showLoading && !signal?.aborted) setRuntimeHealthLoading(false);
    }
  }, []);

  const fetchRuntimePipelinesData = useCallback(async (signal?: AbortSignal) => {
    setRuntimePipelinesError(null);
    try {
      const payload = await fetchStreamingRuntimePipelines(signal);
      if (signal?.aborted) return;
      setRuntimePipelines(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRuntimePipelinesError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const fetchRuntimeObservabilityData = useCallback(async (signal?: AbortSignal) => {
    setRuntimeObservabilityError(null);
    try {
      const payload = await fetchStreamingRuntimeObservability(signal);
      if (signal?.aborted) return;
      setRuntimeObservability(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRuntimeObservabilityError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const fetchRuntimeEncodersData = useCallback(async (signal?: AbortSignal) => {
    setRuntimeEncodersError(null);
    try {
      const payload = await fetchStreamingRuntimeEncoders(signal);
      if (signal?.aborted) return;
      setRuntimeEncoders(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRuntimeEncodersError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const fetchCameraIngestAuthData = useCallback(async (signal?: AbortSignal) => {
    setCameraIngestAuthLoading(true);
    setCameraIngestAuthError(null);
    try {
      const payload = await fetchCameraIngestAuth(signal);
      if (signal?.aborted) return;
      setCameraIngestAuth(payload);
      setCameraIngestRevealed(Boolean(payload.password));
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setCameraIngestAuthError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setCameraIngestAuthLoading(false);
    }
  }, []);

  const fetchSettingsData = useCallback(async (signal?: AbortSignal) => {
    setSettingsLoading(true);
    setSettingsError(null);
    try {
      const payload = await fetchStreamingSettings(signal);
      if (signal?.aborted) return;
      setExtensionSettings(payload);
      setEngineSettingsDraft(deepClone(payload.engine ?? {}));
      setEngineSettingsDirty(false);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setExtensionSettings(null);
      setSettingsError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setSettingsLoading(false);
    }
  }, []);

  const fetchTransmissionsData = useCallback(async (signal?: AbortSignal) => {
    setTransmissionsLoading(true);
    setTransmissionsError(null);
    try {
      const payload = await fetchTransmissions(signal);
      setTransmissions(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setTransmissions([]);
      setTransmissionsError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setTransmissionsLoading(false);
    }
  }, []);

  const fetchCameraLiveViewsData = useCallback(async (signal?: AbortSignal) => {
    setCameraLiveViewsLoading(true);
    setCameraLiveViewsError(null);
    try {
      const payload = await fetchCameraLiveViews(signal);
      if (signal?.aborted) return;
      setCameraLiveViews(Array.isArray(payload) ? payload : []);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setCameraLiveViews([]);
      setCameraLiveViewsError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setCameraLiveViewsLoading(false);
    }
  }, []);

  const fetchQualityProfilesData = useCallback(async (signal?: AbortSignal) => {
    setQualityProfilesError(null);
    try {
      const payload = await fetchStreamingQualityProfiles(signal);
      if (signal?.aborted) return;
      setQualityProfiles(payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setQualityProfilesError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const fetchProcessingServersData = useCallback(async (signal?: AbortSignal) => {
    setProcessingServersLoading(true);
    setProcessingServersError(null);
    try {
      const payload = await fetchProcessingServers(signal);
      if (signal?.aborted) return;
      setProcessingServers(sortProcessingServers(Array.isArray(payload) ? payload : []));
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setProcessingServers([{ id: "local", name: "Local", kind: "inprocess", url: "" }]);
      setProcessingServersError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setProcessingServersLoading(false);
    }
  }, []);

  const fetchAvailableCamerasData = useCallback(async (signal?: AbortSignal) => {
    setAvailableCamerasLoading(true);
    setAvailableCamerasError(null);
    try {
      const data = await fetchCamerasIndex(signal);
      if (signal?.aborted) return;
      const next = Array.isArray(data.cameras) ? data.cameras : [];
      setAvailableCameras(next);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setAvailableCameras([]);
      setAvailableCamerasError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setAvailableCamerasLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchHealthData(controller.signal);
    void fetchEngineData(controller.signal);
    void fetchRuntimeHealthData(controller.signal, true);
    void fetchRuntimePipelinesData(controller.signal);
    void fetchRuntimeObservabilityData(controller.signal);
    void fetchRuntimeEncodersData(controller.signal);
    void fetchCameraIngestAuthData(controller.signal);
    void fetchSettingsData(controller.signal);
    void fetchTransmissionsData(controller.signal);
    void fetchCameraLiveViewsData(controller.signal);
    void fetchQualityProfilesData(controller.signal);
    void fetchProcessingServersData(controller.signal);
    void fetchAvailableCamerasData(controller.signal);
    return () => controller.abort();
  }, [
    fetchAvailableCamerasData,
    fetchEngineData,
    fetchHealthData,
    fetchCameraIngestAuthData,
    fetchProcessingServersData,
    fetchRuntimeHealthData,
    fetchRuntimeEncodersData,
    fetchRuntimeObservabilityData,
    fetchRuntimePipelinesData,
    fetchSettingsData,
    fetchTransmissionsData,
    fetchCameraLiveViewsData,
    fetchQualityProfilesData,
  ]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void fetchEngineData();
    }, 5000);
    return () => window.clearInterval(interval);
  }, [fetchEngineData]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void fetchRuntimeHealthData();
      void fetchRuntimePipelinesData();
      void fetchRuntimeObservabilityData();
      void fetchRuntimeEncodersData();
    }, 2000);
    return () => window.clearInterval(interval);
  }, [fetchRuntimeHealthData, fetchRuntimeEncodersData, fetchRuntimeObservabilityData, fetchRuntimePipelinesData]);

  useEffect(() => {
    if (activeTransmissionId && transmissions.some((item) => item.id === activeTransmissionId)) return;
    setActiveTransmissionId(transmissions[0]?.id ?? null);
  }, [activeTransmissionId, transmissions]);

  useEffect(() => {
    if (activeCameraLiveViewId && cameraLiveViews.some((item) => item.id === activeCameraLiveViewId)) return;
    setActiveCameraLiveViewId(cameraLiveViews[0]?.id ?? null);
  }, [activeCameraLiveViewId, cameraLiveViews]);

  const activeCameraLiveView = useMemo(() => {
    if (!activeCameraLiveViewId) return null;
    return cameraLiveViews.find((item) => item.id === activeCameraLiveViewId) ?? null;
  }, [activeCameraLiveViewId, cameraLiveViews]);

  useEffect(() => {
    if (!activeCameraLiveView) {
      setCameraLiveDraft(null);
      setCameraLiveDraftDirty(false);
      setCameraLiveDraftError(null);
      return;
    }
    if (cameraLiveDraftDirty) return;
    setCameraLiveDraft(deepClone(activeCameraLiveView));
    setCameraLiveDraftError(null);
  }, [activeCameraLiveView, cameraLiveDraftDirty]);

  const activeTransmission = useMemo(() => {
    if (!activeTransmissionId) return null;
    return transmissions.find((item) => item.id === activeTransmissionId) ?? null;
  }, [activeTransmissionId, transmissions]);

  useEffect(() => {
    if (!activeTransmission) {
      setTransmissionDraft(null);
      setTransmissionDraftDirty(false);
      setTransmissionDraftError(null);
      return;
    }
    if (transmissionDraftDirty) return;
    setTransmissionDraft(deepClone(activeTransmission));
    setTransmissionDraftError(null);
  }, [activeTransmission, transmissionDraftDirty]);

  const filteredTransmissions = useMemo(() => {
    const q = transmissionQuery.trim().toLowerCase();
    if (!q) return transmissions;
    return transmissions.filter((item) => {
      const name = String(item.name || "").trim().toLowerCase();
      const path = String(item.path || "").trim().toLowerCase();
      const id = String(item.id || "").trim().toLowerCase();
      return name.includes(q) || path.includes(q) || id.includes(q);
    });
  }, [transmissionQuery, transmissions]);

  const knownProcessingServerIds = useMemo(() => {
    const ids = new Set<string>(["local"]);
    for (const server of processingServers) {
      ids.add(normalizeServerId(server.id));
    }
    return ids;
  }, [processingServers]);

  useEffect(() => {
    if (!createModalOpen) return;
    setNewCameraControlsEnabled(false);
    setNewCameraControlsCameraId("");
    setNewCameraControlsSourceId("");
    if (availableCameras.length > 0) return;
    const controller = new AbortController();
    void fetchAvailableCamerasData(controller.signal);
    return () => controller.abort();
  }, [createModalOpen, fetchAvailableCamerasData]);

  useEffect(() => {
    if (!createModalOpen) return;
    if (newCameraControlsCameraId.trim()) return;
    if (availableCameras.length === 0) return;
    const camera = availableCameras[0] ?? null;
    setNewCameraControlsCameraId(String(camera?.id || "").trim());
    setNewCameraControlsSourceId(defaultCameraSourceId(camera));
  }, [availableCameras, createModalOpen, newCameraControlsCameraId]);

  useEffect(() => {
    if (!createModalOpen) return;
    const camera = availableCameras.find((item) => String(item.id || "").trim() === newCameraControlsCameraId.trim()) ?? null;
    if (!camera) {
      if (newCameraControlsSourceId) setNewCameraControlsSourceId("");
      return;
    }
    const options = cameraSourceOptions(camera);
    if (!options.some((item) => item.id === newCameraControlsSourceId.trim())) {
      setNewCameraControlsSourceId(defaultCameraSourceId(camera));
    }
  }, [availableCameras, createModalOpen, newCameraControlsCameraId, newCameraControlsSourceId]);

  async function runEngineAction(action: "start" | "stop" | "restart" | "reclaim"): Promise<void> {
    if (action === "reclaim") {
      const ok = confirm(
        t(
          "ext.streaming.engine.reclaim_confirm",
          {},
          "This will try to stop and cleanup stale MediaMTX processes for this data directory. Continue?",
        ),
      );
      if (!ok) return;
    }
    setEngineBusy(true);
    setEnginePendingAction(action);
    setEngineError(null);
    try {
      const payload = await postEngineAction(action);
      setEngineStatus(payload);
      void fetchSettingsData();
    } catch (error) {
      setEngineError(error instanceof Error ? error.message : String(error));
      void fetchEngineData();
    } finally {
      setEngineBusy(false);
      setEnginePendingAction(null);
    }
  }

  async function downloadEngine(): Promise<void> {
    setEngineBusy(true);
    setEnginePendingAction("download");
    setEngineError(null);
    try {
      const payload = await postEngineDownload();
      setEngineStatus(payload);
      void fetchSettingsData();
    } catch (error) {
      setEngineError(error instanceof Error ? error.message : String(error));
      void fetchEngineData();
    } finally {
      setEngineBusy(false);
      setEnginePendingAction(null);
    }
  }

  async function refreshEngineStatus(): Promise<void> {
    setEnginePendingAction("refresh");
    try {
      await fetchEngineData();
    } finally {
      setEnginePendingAction(null);
    }
  }

  async function downloadRuntimeDiagnostics(): Promise<void> {
    setRuntimeDiagnosticsBusy(true);
    setRuntimeDiagnosticsError(null);
    try {
      const payload = await fetchStreamingDiagnosticSnapshot();
      const text = JSON.stringify(payload, null, 2);
      const blob = new Blob([text], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "toposync-streaming-diagnostic-snapshot.json";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) {
      setRuntimeDiagnosticsError(error instanceof Error ? error.message : String(error));
    } finally {
      setRuntimeDiagnosticsBusy(false);
    }
  }

  async function runHlsProbe(transmissionId: string, outputId: string): Promise<void> {
    const key = `${transmissionId}:${outputId}`;
    setHlsProbeBusyKey(key);
    setHlsProbeError(null);
    try {
      const payload = await fetchStreamingHlsProbe(transmissionId, outputId);
      setHlsProbeByKey((previous) => ({ ...previous, [key]: payload }));
      setHlsProbeLastChangeByKey((previous) => {
        const previousProbe = hlsProbeByKey[key];
        const changed =
          !previousProbe ||
          previousProbe.media_sequence !== payload.media_sequence ||
          previousProbe.tail_segment_url !== payload.tail_segment_url;
        return {
          ...previous,
          [key]: changed ? payload.sampled_at_unix : previous[key] ?? payload.sampled_at_unix,
        };
      });
    } catch (error) {
      setHlsProbeError(error instanceof Error ? error.message : String(error));
    } finally {
      setHlsProbeBusyKey(null);
    }
  }

  async function retryHardwareEncoder(encoder?: string | null): Promise<void> {
    setRuntimeEncoderClearBusy(true);
    setRuntimeEncodersError(null);
    try {
      const payload = await clearStreamingEncoderQuarantine(encoder ?? null);
      setRuntimeEncoders(payload);
      void fetchRuntimeHealthData();
      void fetchRuntimeObservabilityData();
    } catch (error) {
      setRuntimeEncodersError(error instanceof Error ? error.message : String(error));
    } finally {
      setRuntimeEncoderClearBusy(false);
    }
  }

  async function revealIngestCredentials(): Promise<void> {
    setCameraIngestAuthBusy(true);
    setCameraIngestAuthError(null);
    try {
      const payload = await revealCameraIngestAuth();
      setCameraIngestAuth(payload);
      setCameraIngestRevealed(true);
    } catch (error) {
      setCameraIngestAuthError(error instanceof Error ? error.message : String(error));
    } finally {
      setCameraIngestAuthBusy(false);
    }
  }

  async function rotateIngestCredentials(): Promise<void> {
    const ok = confirm(
      t(
        "ext.streaming.ingest_auth.rotate_confirm",
        {},
        "This will invalidate current camera ingest RTSP URLs. Update Frigate/dev consumers after rotating. Continue?",
      ),
    );
    if (!ok) return;
    setCameraIngestAuthBusy(true);
    setCameraIngestAuthError(null);
    try {
      const payload = await rotateCameraIngestAuth();
      setCameraIngestAuth(payload);
      setCameraIngestRevealed(false);
      setCameraIngestCopied(null);
      void fetchEngineData();
    } catch (error) {
      setCameraIngestAuthError(error instanceof Error ? error.message : String(error));
    } finally {
      setCameraIngestAuthBusy(false);
    }
  }

  async function applyEngineSettings(): Promise<void> {
    if (!engineSettingsDraft) return;
    setEngineSettingsBusy(true);
    setSettingsError(null);
    try {
      const payload = await patchStreamingSettings({
        engine: {
          expose_to_lan: Boolean(engineSettingsDraft.expose_to_lan),
          metrics_enabled: engineSettingsDraft.metrics_enabled !== false,
          encoder_policy: {
            mode: engineSettingsDraft.encoder_policy?.mode === "cpu" ? "cpu" : "auto",
            quarantine_enabled: engineSettingsDraft.encoder_policy?.quarantine_enabled !== false,
            quarantine_after_restarts: engineSettingsDraft.encoder_policy?.quarantine_after_restarts ?? 2,
            quarantine_window_seconds: engineSettingsDraft.encoder_policy?.quarantine_window_seconds ?? 600,
            quarantine_duration_seconds: engineSettingsDraft.encoder_policy?.quarantine_duration_seconds ?? 3600,
            max_restarts_per_minute: engineSettingsDraft.encoder_policy?.max_restarts_per_minute ?? 4,
          },
          media_auth: {
            mode: engineSettingsDraft.media_auth?.mode === "open" ? "open" : "signed_proxy",
            token_ttl_seconds: engineSettingsDraft.media_auth?.token_ttl_seconds ?? 300,
            renew_margin_seconds: engineSettingsDraft.media_auth?.renew_margin_seconds ?? 60,
          },
          preferred_ports: {
            rtsp: engineSettingsDraft.preferred_ports?.rtsp,
            hls: engineSettingsDraft.preferred_ports?.hls,
            api: engineSettingsDraft.preferred_ports?.api,
            webrtc: engineSettingsDraft.preferred_ports?.webrtc,
            webrtc_udp: engineSettingsDraft.preferred_ports?.webrtc_udp,
            metrics: engineSettingsDraft.preferred_ports?.metrics,
          },
          webrtc_ice_servers: Array.isArray(engineSettingsDraft.webrtc_ice_servers)
            ? engineSettingsDraft.webrtc_ice_servers
            : [],
          webrtc_additional_hosts: Array.isArray(engineSettingsDraft.webrtc_additional_hosts)
            ? engineSettingsDraft.webrtc_additional_hosts
            : [],
        },
      });
      setExtensionSettings(payload);
      setEngineSettingsDraft(deepClone(payload.engine ?? {}));
      setEngineSettingsDirty(false);
      void fetchEngineData();
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : String(error));
    } finally {
      setEngineSettingsBusy(false);
    }
  }

  async function createTransmissionAction(): Promise<void> {
    setCreateBusy(true);
    setCreateError(null);
    try {
      const hostServerId = normalizeServerId(newTransmissionHostServerId);
      if (!knownProcessingServerIds.has(hostServerId)) {
        setCreateError(
          t(
            "ext.streaming.errors.invalid_host_server",
            { serverId: hostServerId },
            `Invalid host server: ${hostServerId}`,
          ),
        );
        return;
      }
      const suggestedName =
        newTransmissionName.trim() || t("ext.streaming.transmissions.default_name", {}, "Transmission");
      const suggestedPath = slugifyPath(newTransmissionPath.trim() || suggestedName) || "stream";

      let cameraControlsPayload: { enabled: boolean; camera_id: string; camera_source_id?: string | null } | null | undefined = undefined;
      if (newCameraControlsEnabled) {
        const cid = newCameraControlsCameraId.trim();
        if (!cid) {
          setCreateError(t("ext.streaming.transmissions.camera_controls.select_camera_error", {}, "Selecione uma câmera."));
          return;
        }
        cameraControlsPayload = {
          enabled: true,
          camera_id: cid,
          camera_source_id: newCameraControlsSourceId.trim() || null,
        };
      }

      const payload = await createTransmission({
        name: suggestedName,
        path: suggestedPath,
        enabled: true,
        host_server_id: hostServerId,
        camera_controls: cameraControlsPayload ?? undefined,
        outputs: [
          {
            protocol: newOutputProtocol,
            enabled: true,
            resolution: {
              width: toSafeInt(newOutputWidth, 1280),
              height: toSafeInt(newOutputHeight, 720),
            },
            fps_limit: toSafeInt(newOutputFps, 12),
          },
        ],
      });
      setTransmissions((previous) => [payload, ...previous.filter((item) => item.id !== payload.id)]);
      setActiveTransmissionId(payload.id);
      setTransmissionDraft(deepClone(payload));
      setTransmissionDraftDirty(false);
      setTransmissionDraftError(null);
      setCreateModalOpen(false);
      setNewTransmissionName("");
      setNewTransmissionPath("");
      setNewTransmissionHostServerId("local");
      setNewCameraControlsEnabled(false);
      setNewCameraControlsCameraId("");
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreateBusy(false);
    }
  }

  async function generateLiveViewsAction(cameraId?: string): Promise<void> {
    setCameraLiveGenerateBusy(true);
    setCameraLiveViewsError(null);
    setCameraLiveGenerateMessage(null);
    try {
      const payload = await generateCameraLiveViews({
        camera_id: cameraId || null,
        host_server_id: "local",
        replace_existing: true,
      });
      setCameraLiveGenerateMessage(
        t(
          "ext.streaming.live.generated",
          { count: payload.generated_count },
          `${payload.generated_count} visualização(ões) ao vivo gerada(s).`,
        ),
      );
      await fetchCameraLiveViewsData();
      await fetchTransmissionsData();
      void fetchEngineData();
      if (payload.camera_live_views?.[0]?.id) setActiveCameraLiveViewId(payload.camera_live_views[0].id);
    } catch (error) {
      setCameraLiveViewsError(error instanceof Error ? error.message : String(error));
    } finally {
      setCameraLiveGenerateBusy(false);
    }
  }

  function updateCameraLiveDraftVariant(variantId: string, patch: Partial<CameraLiveVariant>): void {
    setCameraLiveDraft((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        variants: previous.variants.map((variant) =>
          variant.id === variantId ? { ...variant, ...patch } : variant,
        ),
      };
    });
    setCameraLiveDraftDirty(true);
    setCameraLiveDraftError(null);
  }

  async function saveCameraLiveDraft(): Promise<void> {
    if (!cameraLiveDraft) return;
    setCameraLiveDraftBusy(true);
    setCameraLiveDraftError(null);
    try {
      const payload = await updateCameraLiveView(cameraLiveDraft.id, cameraLiveDraft);
      setCameraLiveViews((previous) => previous.map((item) => (item.id === payload.id ? payload : item)));
      setCameraLiveDraft(deepClone(payload));
      setCameraLiveDraftDirty(false);
    } catch (error) {
      setCameraLiveDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setCameraLiveDraftBusy(false);
    }
  }

  async function deleteActiveCameraLiveView(): Promise<void> {
    if (!activeCameraLiveViewId) return;
    const ok = confirm(
      t(
        "ext.streaming.live.delete_confirm",
        {},
        "Excluir esta visualização ao vivo e transmissões geradas por ela?",
      ),
    );
    if (!ok) return;
    setCameraLiveDraftBusy(true);
    setCameraLiveDraftError(null);
    try {
      await deleteCameraLiveView(activeCameraLiveViewId);
      setCameraLiveViews((previous) => previous.filter((item) => item.id !== activeCameraLiveViewId));
      await fetchTransmissionsData();
      setCameraLiveDraft(null);
      setCameraLiveDraftDirty(false);
    } catch (error) {
      setCameraLiveDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setCameraLiveDraftBusy(false);
    }
  }

  async function loadUrls(transmissionId: string): Promise<void> {
    setUrlsLoadingId(transmissionId);
    setTransmissionsError(null);
    try {
      const payload = await fetchTransmissionUrls(transmissionId);
      setUrlsByTransmissionId((previous) => ({ ...previous, [transmissionId]: payload }));
    } catch (error) {
      setTransmissionsError(error instanceof Error ? error.message : String(error));
    } finally {
      setUrlsLoadingId(null);
    }
  }

  function updateDraft(patch: Partial<Transmission>): void {
    setTransmissionDraft((previous) => {
      if (!previous) return previous;
      return { ...previous, ...patch };
    });
    setTransmissionDraftDirty(true);
    setTransmissionDraftError(null);
  }

  function updateDraftOutput(outputId: string, patch: Partial<TransmissionOutput>): void {
    setTransmissionDraft((previous) => {
      if (!previous) return previous;
      const outputs = Array.isArray(previous.outputs) ? previous.outputs : [];
      return {
        ...previous,
        outputs: outputs.map((output) => (output.id === outputId ? { ...output, ...patch } : output)),
      };
    });
    setTransmissionDraftDirty(true);
    setTransmissionDraftError(null);
  }

  function addDraftOutput(protocol: "hls" | "rtsp" | "webrtc"): void {
    setTransmissionDraft((previous) => {
      if (!previous) return previous;
      const outputs = Array.isArray(previous.outputs) ? previous.outputs : [];
      return { ...previous, outputs: [...outputs, defaultOutput(protocol)] };
    });
    setTransmissionDraftDirty(true);
    setTransmissionDraftError(null);
  }

  function removeDraftOutput(outputId: string): void {
    setTransmissionDraft((previous) => {
      if (!previous) return previous;
      const outputs = Array.isArray(previous.outputs) ? previous.outputs : [];
      return { ...previous, outputs: outputs.filter((output) => output.id !== outputId) };
    });
    setTransmissionDraftDirty(true);
    setTransmissionDraftError(null);
  }

  function discardDraftChanges(): void {
    if (!activeTransmission) return;
    setTransmissionDraft(deepClone(activeTransmission));
    setTransmissionDraftDirty(false);
    setTransmissionDraftError(null);
    setUrlsLoadingId(null);
  }

  async function saveDraftChanges(): Promise<void> {
    if (!transmissionDraft) return;
    setTransmissionDraftBusy(true);
    setTransmissionDraftError(null);
    try {
      const hostServerId = normalizeServerId(transmissionDraft.host_server_id);
      if (!knownProcessingServerIds.has(hostServerId)) {
        setTransmissionDraftError(
          t(
            "ext.streaming.errors.invalid_host_server",
            { serverId: hostServerId },
            `Invalid host server: ${hostServerId}`,
          ),
        );
        return;
      }
      const payload: Transmission = {
        ...transmissionDraft,
        id: transmissionDraft.id || activeTransmissionId || "",
        host_server_id: hostServerId,
      };
      const updated = await updateTransmission(payload.id, payload);
      setTransmissions((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      setTransmissionDraft(deepClone(updated));
      setTransmissionDraftDirty(false);
      void loadUrls(updated.id);
    } catch (error) {
      setTransmissionDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setTransmissionDraftBusy(false);
    }
  }

  async function applyQualityProfilesToDraft(): Promise<void> {
    if (!transmissionDraft) return;
    if (transmissionDraftDirty) {
      setTransmissionDraftError(
        t(
          "ext.streaming.quality.apply_save_first",
          {},
          "Save or discard pending output changes before applying quality profile outputs.",
        ),
      );
      return;
    }
    setApplyQualityProfilesBusy(true);
    setTransmissionDraftError(null);
    try {
      const updated = await applyTransmissionQualityProfiles(transmissionDraft.id);
      setTransmissions((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      setTransmissionDraft(deepClone(updated));
      setTransmissionDraftDirty(false);
      void loadUrls(updated.id);
      void fetchRuntimeHealthData();
      void fetchRuntimeObservabilityData();
    } catch (error) {
      setTransmissionDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setApplyQualityProfilesBusy(false);
    }
  }

  async function applyWebRtcCompanionToDraft(): Promise<void> {
    if (!transmissionDraft) return;
    if (transmissionDraftDirty) {
      setTransmissionDraftError(
        t(
          "ext.streaming.webrtc.apply_save_first",
          {},
          "Save or discard pending output changes before applying the WebRTC low-latency output.",
        ),
      );
      return;
    }
    setApplyWebRtcCompanionBusy(true);
    setTransmissionDraftError(null);
    try {
      const updated = await applyTransmissionWebRtcCompanion(transmissionDraft.id);
      setTransmissions((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      setTransmissionDraft(deepClone(updated));
      setTransmissionDraftDirty(false);
      void loadUrls(updated.id);
      void fetchRuntimeHealthData();
      void fetchRuntimeObservabilityData();
    } catch (error) {
      setTransmissionDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setApplyWebRtcCompanionBusy(false);
    }
  }

  async function deleteActiveTransmission(): Promise<void> {
    if (!activeTransmission) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteTransmission(activeTransmission.id);
      setTransmissions((previous) => previous.filter((item) => item.id !== activeTransmission.id));
      setUrlsByTransmissionId((previous) => {
        const next = { ...previous };
        delete next[activeTransmission.id];
        return next;
      });
      setActiveTransmissionId(null);
      setTransmissionDraft(null);
      setTransmissionDraftDirty(false);
      setConfirmDeleteOpen(false);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
    } finally {
      setDeleteBusy(false);
    }
  }

  function requestSelectTransmission(nextId: string): void {
    if (nextId === activeTransmissionId) return;
    if (transmissionDraftDirty) {
      setPendingTransmissionId(nextId);
      setConfirmDiscardOpen(true);
      return;
    }
    setActiveTransmissionId(nextId);
  }

  const activeUrls = activeTransmissionId ? urlsByTransmissionId[activeTransmissionId] ?? null : null;
  const runtimePipelineLinksByTransmissionId = useMemo(() => {
    const map = new Map<string, StreamingRuntimePipelineLink[]>();
    for (const link of runtimePipelines?.pipelines ?? []) {
      const transmissionId = String(link.transmission_id || "").trim();
      if (!transmissionId) continue;
      const current = map.get(transmissionId) ?? [];
      current.push(link);
      map.set(transmissionId, current);
    }
    return map;
  }, [runtimePipelines?.pipelines]);
  const runtimeRows = useMemo(() => {
    const namesById = new Map<string, string>();
    for (const transmission of transmissions) {
      const transmissionId = String(transmission.id || "").trim();
      if (!transmissionId) continue;
      const label = String(transmission.name || "").trim() || String(transmission.path || "").trim() || transmissionId;
      namesById.set(transmissionId, label);
    }

    const rows: Array<{
      key: string;
      transmission: StreamingRuntimeTransmissionHealth;
      output: StreamingRuntimeOutputHealth | null;
      transmissionLabel: string;
      outputLabel: string;
      pipelineLink: StreamingRuntimePipelineLink | null;
    }> = [];

    for (const transmission of runtimeHealth?.transmissions ?? []) {
      const transmissionId = String(transmission.transmission_id || "").trim();
      const transmissionLabel = namesById.get(transmissionId) ?? transmissionId;
      const links = runtimePipelineLinksByTransmissionId.get(transmissionId) ?? [];
      const writerIds = new Set(
        [transmission.active_writer_id, transmission.selected_writer_id]
          .map((value) => String(value || "").trim())
          .filter(Boolean),
      );
      const pipelineLink = links.find((link) => writerIds.has(String(link.writer_id || "").trim())) ?? links[0] ?? null;
      const outputs = Array.isArray(transmission.outputs) ? transmission.outputs : [];
      if (outputs.length === 0) {
        rows.push({
          key: `${transmissionId}-no-output`,
          transmission,
          output: null,
          transmissionLabel,
          outputLabel: "-",
          pipelineLink,
        });
        continue;
      }

      for (const output of outputs) {
        const outputId = String(output.output_id || output.output_key || "").trim();
        rows.push({
          key: `${transmissionId}-${output.output_key || outputId}`,
          transmission,
          output,
          transmissionLabel,
          outputLabel: `${String(output.protocol || "").toUpperCase()} ${outputId || "-"}`,
          pipelineLink,
        });
      }
    }
    return rows;
  }, [runtimeHealth?.transmissions, runtimePipelineLinksByTransmissionId, transmissions]);
  const engineRunning = Boolean(engineStatus?.running);
  const orphanPids = Array.isArray(engineStatus?.orphan_pids) ? engineStatus.orphan_pids : [];
  const engineNetworkContract = engineStatus?.network_contract ?? null;
  const engineNetworkContractStatus = engineNetworkContract?.status ?? "not_applicable";
  const engineNetworkContractMessages = [
    ...(engineNetworkContract?.blocking_errors ?? []),
    ...(engineNetworkContract?.warnings ?? []),
  ];
  const engineNetworkContractHasIssue =
    Boolean(engineNetworkContract) &&
    engineNetworkContractStatus !== "ok" &&
    engineNetworkContractStatus !== "not_applicable";
  const primaryEngineAction = engineRunning ? "restart" : "start";
  const primaryEngineLabel =
    enginePendingAction === "start"
      ? t("ext.streaming.engine.starting", {}, "Iniciando…")
      : enginePendingAction === "restart"
        ? t("ext.streaming.engine.restarting", {}, "Reiniciando…")
        : engineRunning
          ? t("ext.streaming.engine.restart", {}, "Reiniciar")
          : t("ext.streaming.engine.start", {}, "Iniciar");
  const stopLabel =
    enginePendingAction === "stop" ? t("ext.streaming.engine.stopping", {}, "Parando…") : t("ext.streaming.engine.stop", {}, "Parar");
  const reclaimLabel =
    enginePendingAction === "reclaim"
      ? t("ext.streaming.engine.reclaiming", {}, "Recuperando…")
      : t("ext.streaming.engine.reclaim", {}, "Recuperar");
  const downloadLabel =
    enginePendingAction === "download"
      ? t("ext.streaming.engine.downloading", {}, "Baixando…")
      : t("ext.streaming.engine.download", {}, "Baixar engine");
  const refreshLabel =
    enginePendingAction === "refresh"
      ? t("ext.streaming.engine.refreshing", {}, "Atualizando…")
      : t("ext.streaming.engine.refresh", {}, "Atualizar");

  return (
    <div className="streamingSettingsPanel">
      <div className="card">
        <div className="cardBody">
          <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
            {t("ext.streaming.settings.title", {}, "Transmissões")}
          </div>
          <div className="cardMeta">
            {t(
              "ext.streaming.settings.subtitle",
              {},
              "Crie transmissões, configure saídas (HLS/RTSP/WebRTC) e gere URLs a partir do MediaMTX.",
            )}
          </div>

          <div className="streamingQuickSteps">
            <div className="streamingQuickStepsTitle">{t("ext.streaming.settings.quickstart", {}, "Fluxo Recomendado")}</div>
            <ol className="streamingQuickStepsList">
              <li>{t("ext.streaming.settings.quickstart_step_1", {}, "Crie uma transmissão com ao menos uma saída.")}</li>
              <li>{t("ext.streaming.settings.quickstart_step_2", {}, "Ajuste resolução/FPS/autenticação por saída.")}</li>
              <li>{t("ext.streaming.settings.quickstart_step_3", {}, "Salve, carregue URLs e use o wizard para gerar o fluxo.")}</li>
            </ol>
          </div>
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">
          <div className="settingsDetailHeader" style={{ marginBottom: 10 }}>
            <div>
              <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                {t("ext.streaming.live.title", {}, "Câmeras ao vivo")}
              </div>
              <div className="cardMeta">
                {t(
                  "ext.streaming.live.subtitle",
                  {},
                  "Escolha qual fonte cada câmera usa em miniatura, PiP, tela grande e tela cheia.",
                )}
              </div>
            </div>
            <button
              className="primaryButton"
              type="button"
              disabled={cameraLiveGenerateBusy}
              onClick={() => void generateLiveViewsAction()}
            >
              <i className="fa-solid fa-wand-magic-sparkles" aria-hidden="true" />{" "}
              {cameraLiveGenerateBusy
                ? t("ext.streaming.live.generating", {}, "Gerando…")
                : t("ext.streaming.live.generate_all", {}, "Criar visualizações")}
            </button>
          </div>

          {cameraLiveViewsLoading ? (
            <div className="settingsStatusMuted">{t("ext.streaming.live.loading", {}, "Carregando câmeras ao vivo…")}</div>
          ) : null}
          {cameraLiveViewsError ? <div className="errorText">{cameraLiveViewsError}</div> : null}
          {cameraLiveGenerateMessage ? <div className="streamingStatusOk">{cameraLiveGenerateMessage}</div> : null}

          {!cameraLiveViewsLoading && cameraLiveViews.length === 0 ? (
            <div className="settingsStatusMuted" style={{ marginTop: 8 }}>
              {t(
                "ext.streaming.live.empty",
                {},
                "Nenhuma visualização ao vivo criada. Gere a partir das câmeras cadastradas para usar a dashboard.",
              )}
            </div>
          ) : null}

          {cameraLiveViews.length > 0 ? (
            <div className="streamingFormGrid" style={{ marginTop: 12 }}>
              <div>
                <div className="cardMeta" style={{ marginBottom: 8 }}>
                  {t("ext.streaming.live.configured_count", { count: cameraLiveViews.length }, `${cameraLiveViews.length} câmera(s) ao vivo`)}
                </div>
                <div className="settingsList">
                  {cameraLiveViews.map((liveView) => {
                    const camera = cameraById(availableCameras, liveView.camera_id);
                    const sourceSummary = liveView.variants
                      .filter((variant) => ["thumbnail", "large", "fullscreen", "pip"].includes(String(variant.role)))
                      .slice(0, 4)
                      .map((variant) => `${liveContextLabel(variant.role as CameraLiveContext, t)}: ${cameraSourceLabel(availableCameras, liveView.camera_id, variant.camera_source_id)}`)
                      .join(" · ");
                    return (
                      <button
                        key={liveView.id}
                        type="button"
                        className={["settingsListItem", activeCameraLiveViewId === liveView.id ? "isActive" : ""].filter(Boolean).join(" ")}
                        onClick={() => setActiveCameraLiveViewId(liveView.id)}
                      >
                        <span>
                          <strong>{liveView.name || camera?.name || liveView.camera_id}</strong>
                          <span className="cardMeta">{sourceSummary || t("ext.streaming.live.no_variants", {}, "Sem variantes configuradas")}</span>
                        </span>
                        {liveView.enabled === false ? <span className="badge">{t("ext.streaming.transmissions.badge_disabled", {}, "off")}</span> : null}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div>
                {cameraLiveDraft ? (
                  <>
                    <div className="settingsDetailHeader" style={{ marginBottom: 10 }}>
                      <div>
                        <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                          {cameraLiveDraft.name || cameraLiveDraft.camera_id}
                        </div>
                        <div className="cardMeta">
                          {t("ext.streaming.live.editor_hint", {}, "Ajuste o uso padrão de cada contexto de visualização.")}
                        </div>
                      </div>
                      <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
                        {cameraLiveDraftDirty ? (
                          <button
                            className="chipButton"
                            type="button"
                            disabled={cameraLiveDraftBusy}
                            onClick={() => {
                              setCameraLiveDraft(deepClone(activeCameraLiveView));
                              setCameraLiveDraftDirty(false);
                              setCameraLiveDraftError(null);
                            }}
                          >
                            {t("ext.streaming.transmissions.discard", {}, "Descartar")}
                          </button>
                        ) : null}
                        <button
                          className="primaryButton"
                          type="button"
                          disabled={!cameraLiveDraftDirty || cameraLiveDraftBusy}
                          onClick={() => void saveCameraLiveDraft()}
                        >
                          {cameraLiveDraftBusy
                            ? t("ext.streaming.transmissions.saving", {}, "Salvando…")
                            : t("ext.streaming.transmissions.save", {}, "Salvar")}
                        </button>
                      </div>
                    </div>
                    {cameraLiveDraftError ? <div className="errorText">{cameraLiveDraftError}</div> : null}
                    <div style={{ display: "grid", gap: 10 }}>
                      {cameraLiveDraft.variants.map((variant) => {
                        const sourceOptions = cameraSourceOptions(cameraById(availableCameras, cameraLiveDraft.camera_id));
                        return (
                          <div key={variant.id} className="settingsInlinePanel">
                            <div className="settingsDetailHeader" style={{ marginBottom: 8 }}>
                              <div>
                                <div className="cardTitle">{liveContextLabel(variant.role as CameraLiveContext, t)}</div>
                                <div className="cardMeta">{liveVariantConsequence(variant, availableCameras, cameraLiveDraft.camera_id, t)}</div>
                              </div>
                              <label className="settingsToggle">
                                <input
                                  type="checkbox"
                                  checked={variant.enabled !== false}
                                  onChange={(event) => updateCameraLiveDraftVariant(variant.id, { enabled: event.target.checked })}
                                />
                                <span>{t("ext.streaming.outputs.enabled", {}, "Ativa")}</span>
                              </label>
                            </div>
                            <div className="streamingFormGrid">
                              <label>
                                <span className="label">{t("ext.streaming.transmissions.camera_controls.source", {}, "Fonte da câmera")}</span>
                                <select
                                  className="input"
                                  value={variant.camera_source_id}
                                  onChange={(event) => updateCameraLiveDraftVariant(variant.id, { camera_source_id: event.target.value })}
                                >
                                  {sourceOptions.map((option) => (
                                    <option key={option.id} value={option.id}>
                                      {option.label}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <label>
                                <span className="label">{t("ext.streaming.live.quality", {}, "Qualidade")}</span>
                                <select
                                  className="input"
                                  value={variant.quality_profile_id ?? ""}
                                  onChange={(event) => {
                                    const value = event.target.value as CameraLiveVariant["quality_profile_id"];
                                    updateCameraLiveDraftVariant(variant.id, {
                                      quality_profile_id: value || null,
                                      output_id: value ? `hls_${value}` : null,
                                    });
                                  }}
                                >
                                  <option value="quad_grid">{qualityProfileUiLabel("quad_grid", t)}</option>
                                  <option value="stable_apple_tv">{qualityProfileUiLabel("stable_apple_tv", t)}</option>
                                  <option value="fullscreen_quality">{qualityProfileUiLabel("fullscreen_quality", t)}</option>
                                  <option value="diagnostic_low">{qualityProfileUiLabel("diagnostic_low", t)}</option>
                                </select>
                              </label>
                              <label>
                                <span className="label">{t("ext.streaming.live.transport", {}, "Transporte")}</span>
                                <select
                                  className="input"
                                  value={variant.preferred_transport ?? "auto"}
                                  onChange={(event) => updateCameraLiveDraftVariant(variant.id, { preferred_transport: event.target.value as CameraLiveVariant["preferred_transport"] })}
                                >
                                  <option value="auto">{transportUiLabel("auto", t)}</option>
                                  <option value="hls">{transportUiLabel("hls", t)}</option>
                                  <option value="webrtc">{transportUiLabel("webrtc", t)}</option>
                                </select>
                              </label>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    <div className="rowWrap" style={{ marginTop: 12, justifyContent: "space-between" }}>
                      <button className="chipButton" type="button" disabled={cameraLiveGenerateBusy} onClick={() => void generateLiveViewsAction(cameraLiveDraft.camera_id)}>
                        <i className="fa-solid fa-rotate" aria-hidden="true" />{" "}
                        {t("ext.streaming.live.regenerate_one", {}, "Recriar padrões desta câmera")}
                      </button>
                      <button className="dangerButton" type="button" disabled={cameraLiveDraftBusy} onClick={() => void deleteActiveCameraLiveView()}>
                        {t("ext.streaming.transmissions.delete", {}, "Excluir")}
                      </button>
                    </div>
                  </>
                ) : (
                  <div className="settingsStatusMuted">
                    {t("ext.streaming.live.select", {}, "Selecione uma câmera ao vivo para ajustar.")}
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">
          <div className="settingsDetailHeader" style={{ marginBottom: 8 }}>
            <div>
              <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                {t("ext.streaming.ingest_auth.title", {}, "Camera ingest access")}
              </div>
              <div className="cardMeta">
                {t(
                  "ext.streaming.ingest_auth.subtitle",
                  {},
                  "RTSP ingest paths are password protected. Expose 18758/tcp only when Frigate or development instances need direct ingest access.",
                )}
              </div>
            </div>
            <div className="rowWrap" style={{ gap: 8, justifyContent: "flex-end" }}>
              <button
                className="chipButton"
                type="button"
                disabled={cameraIngestAuthLoading || cameraIngestAuthBusy}
                onClick={() => void fetchCameraIngestAuthData()}
              >
                <i className="fa-solid fa-rotate-right" aria-hidden="true" />{" "}
                {cameraIngestAuthLoading
                  ? t("ext.streaming.ingest_auth.loading", {}, "Loading...")
                  : t("ext.streaming.ingest_auth.refresh", {}, "Refresh")}
              </button>
              <button
                className="chipButton"
                type="button"
                disabled={cameraIngestAuthBusy}
                onClick={() => void revealIngestCredentials()}
              >
                <i className="fa-solid fa-eye" aria-hidden="true" />{" "}
                {cameraIngestRevealed
                  ? t("ext.streaming.ingest_auth.reveal_again", {}, "Reveal again")
                  : t("ext.streaming.ingest_auth.reveal", {}, "Reveal credentials")}
              </button>
              <button
                className="chipButton"
                type="button"
                disabled={cameraIngestAuthBusy}
                onClick={() => void rotateIngestCredentials()}
              >
                <i className="fa-solid fa-arrows-rotate" aria-hidden="true" />{" "}
                {t("ext.streaming.ingest_auth.rotate", {}, "Rotate credentials")}
              </button>
            </div>
          </div>

          {cameraIngestAuthError ? <div className="errorText">{cameraIngestAuthError}</div> : null}
          {cameraIngestAuth ? (
            <>
              <div className="streamingFormGrid" style={{ marginBottom: 10 }}>
                <div className="settingsInfoBlock">
                  <div className="cardMeta">{t("ext.streaming.ingest_auth.status", {}, "Status")}</div>
                  <strong>
                    {cameraIngestAuth.credential_active
                      ? t("ext.streaming.ingest_auth.protected", {}, "Password protected")
                      : t("ext.streaming.ingest_auth.not_ready", {}, "Not ready")}
                  </strong>
                </div>
                <div className="settingsInfoBlock">
                  <div className="cardMeta">{t("ext.streaming.ingest_auth.username", {}, "Username")}</div>
                  <strong>{cameraIngestAuth.username || "-"}</strong>
                </div>
                <div className="settingsInfoBlock">
                  <div className="cardMeta">{t("ext.streaming.ingest_auth.rtsp_port", {}, "RTSP port")}</div>
                  <strong>{cameraIngestAuth.rtsp_port ?? "-"}</strong>
                </div>
                <div className="settingsInfoBlock">
                  <div className="cardMeta">{t("ext.streaming.ingest_auth.rotated", {}, "Last rotation")}</div>
                  <strong>{formatRuntimeUnixTime(cameraIngestAuth.rotated_at_unix ?? cameraIngestAuth.created_at_unix)}</strong>
                </div>
              </div>
              <div className="cardMeta" style={{ marginBottom: 8 }}>
                {cameraIngestAuth.password
                  ? t(
                      "ext.streaming.ingest_auth.revealed_hint",
                      {},
                      "Use these RTSP URLs in Frigate/dev. Diagnostic JSON and logs keep the password redacted.",
                    )
                  : t(
                      "ext.streaming.ingest_auth.hidden_hint",
                      {},
                      "Reveal credentials only when you need to configure an external consumer.",
                    )}
              </div>
              <div className="streamingRuntimeTableWrap">
                <table className="streamingRuntimeTable">
                  <thead>
                    <tr>
                      <th>{t("ext.streaming.ingest_auth.table.camera", {}, "Camera")}</th>
                      <th>{t("ext.streaming.ingest_auth.table.path", {}, "Path")}</th>
                      <th>{t("ext.streaming.ingest_auth.table.url", {}, "RTSP URL")}</th>
                      <th>{t("ext.streaming.ingest_auth.table.action", {}, "Action")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(cameraIngestAuth.paths ?? []).map((item) => {
                      const visibleUrl = item.rtsp_url || item.redacted_rtsp_url;
                      return (
                        <tr key={item.path}>
                          <td title={item.camera_id}>{compactRuntimeId(item.camera_id)}</td>
                          <td title={item.path}>{item.path}</td>
                          <td title={visibleUrl}>{visibleUrl}</td>
                          <td>
                            {item.rtsp_url ? (
                              <button
                                className="chipButton"
                                type="button"
                                onClick={() => {
                                  void copyToClipboard(item.rtsp_url || "").then(() => {
                                    setCameraIngestCopied(item.path);
                                    window.setTimeout(() => setCameraIngestCopied(null), 1400);
                                  });
                                }}
                              >
                                <i className="fa-solid fa-copy" aria-hidden="true" />{" "}
                                {cameraIngestCopied === item.path
                                  ? t("ext.streaming.ingest_auth.copied", {}, "Copied")
                                  : t("ext.streaming.ingest_auth.copy", {}, "Copy")}
                              </button>
                            ) : (
                              t("ext.streaming.ingest_auth.hidden", {}, "Hidden")
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {!(cameraIngestAuth.paths ?? []).length ? (
                <div className="cardMeta" style={{ marginTop: 8 }}>
                  {t("ext.streaming.ingest_auth.empty", {}, "No camera ingest paths are available yet. Add cameras with RTSP sources first.")}
                </div>
              ) : null}
            </>
          ) : (
            <div className="settingsStatusMuted">
              {cameraIngestAuthLoading
                ? t("ext.streaming.ingest_auth.loading", {}, "Loading...")
                : t("ext.streaming.ingest_auth.empty_state", {}, "Camera ingest auth has not been loaded yet.")}
            </div>
          )}
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">
          <div className="cardMeta">
            {t("ext.streaming.settings.transmissions", {}, "Transmissões avançadas")}: {transmissionsCount}
          </div>

          {healthLoading ? <div className="settingsStatusMuted">{t("ext.streaming.settings.health.loading")}</div> : null}

          {!healthLoading && !healthError && health?.status === "ok" ? (
            <div className="streamingStatusOk">{t("ext.streaming.settings.health.ok")}</div>
          ) : null}

          {!healthLoading && (healthError || health?.status !== "ok") ? (
            <div className="errorText">
              {t("ext.streaming.settings.health.failed")} {healthError ? `(${healthError})` : ""}
            </div>
          ) : null}

          <div className="rowWrap">
            <button className="chipButton" type="button" onClick={() => void fetchHealthData()}>
              {t("ext.streaming.settings.health.refresh")}
            </button>
          </div>
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">
          <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
            {t("ext.streaming.engine.title", {}, "Engine (MediaMTX)")}
          </div>

          {engineLoading ? <div className="settingsStatusMuted">{t("ext.streaming.engine.loading")}</div> : null}

          {!engineLoading && engineStatus?.running ? (
            <div className="streamingStatusOk">{t("ext.streaming.engine.running")}</div>
          ) : null}

          {!engineLoading && !engineStatus?.running ? (
            <div className="settingsStatusMuted">{t("ext.streaming.engine.stopped")}</div>
          ) : null}

          {engineStatus ? (
            <div className="streamingFormGrid streamingFormGridEnginePorts" style={{ marginTop: 10 }}>
              <div className="cardMeta">
                {t("ext.streaming.engine.meta_version", {}, "Versão")}: {engineStatus.mediamtx_version || "-"}
              </div>
              <div className="cardMeta">
                {t("ext.streaming.engine.meta_pid", {}, "PID")}: {engineStatus.pid ?? "-"}
              </div>
              <div className="cardMeta">
                {t("ext.streaming.engine.meta_uptime", {}, "Uptime")}: {formatDuration(engineStatus.uptime_seconds)}
              </div>
              <div className="cardMeta">
                {t("ext.streaming.engine.meta_api", {}, "API")}: {engineStatus.ports?.api ?? "-"}
              </div>
              <div className="cardMeta">
                {t("ext.streaming.engine.meta_metrics", {}, "Metrics")}:{" "}
                {engineStatus.metrics_enabled === false
                  ? t("ext.streaming.common.no", {}, "No")
                  : `${engineStatus.ports?.metrics ?? "-"} ${engineStatus.metrics_reachable ? "ok" : "unreachable"}`}
              </div>
            </div>
          ) : null}

          {engineStatus?.bind_host && engineStatus?.ports ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {engineStatus.bind_host} - RTSP {engineStatus.ports.rtsp ?? "-"} -{" "}
              {t("ext.streaming.engine.meta_internal_hls", {}, "Internal HLS")} {engineStatus.ports.hls ?? "-"} - WebRTC{" "}
              {engineStatus.ports.webrtc ?? "-"} - Metrics {engineStatus.ports.metrics ?? "-"}
            </div>
          ) : null}

          {engineStatus?.urls?.rtsp_url ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {t("ext.streaming.engine.test_rtsp", {}, "RTSP (test)")}: {engineStatus.urls.rtsp_url}
            </div>
          ) : null}

          {engineStatus?.urls?.hls_url ? (
            <div className="cardMeta">
              {t("ext.streaming.engine.test_hls", {}, "HLS (test)")}: {engineStatus.urls.hls_url}
            </div>
          ) : null}

          {engineStatus?.urls?.webrtc_url ? (
            <div className="cardMeta">
              {t("ext.streaming.engine.test_webrtc", {}, "WebRTC/WHEP (test)")}: {engineStatus.urls.webrtc_url}
            </div>
          ) : null}

          {engineNetworkContractHasIssue ? (
            <div className="errorText" style={{ marginTop: 8 }}>
              {t(
                "ext.streaming.engine.network_contract_issue",
                { status: engineNetworkContractStatus },
                `Network contract issue: ${engineNetworkContractStatus}`,
              )}
              {engineNetworkContractMessages.length > 0 ? ` ${engineNetworkContractMessages.join(" ")}` : ""}
            </div>
          ) : null}

          {engineNetworkContract?.public_hls_mode ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {t("ext.streaming.engine.hls_public_mode", {}, "HLS public mode")}: {engineNetworkContract.public_hls_mode}
            </div>
          ) : null}

          {engineStatus?.ports?.webrtc_udp || engineNetworkContract?.webrtc_additional_hosts?.length ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {t("ext.streaming.engine.webrtc_contract", {}, "WebRTC contract")}: UDP{" "}
              {engineStatus?.ports?.webrtc_udp ?? engineNetworkContract?.actual_ports?.webrtc_udp ?? "-"}
              {engineNetworkContract?.webrtc_additional_hosts?.length
                ? ` · hosts ${engineNetworkContract.webrtc_additional_hosts.join(", ")}`
                : ""}
            </div>
          ) : null}

          {Array.isArray(engineStatus?.warnings) && engineStatus.warnings.length > 0 ? (
            <div style={{ marginTop: 6 }}>
              {engineStatus.warnings.map((warning, index) => (
                <div className="cardMeta" key={`${warning}-${index}`} style={index > 0 ? { marginTop: 4 } : undefined}>
                  {warning}
                </div>
              ))}
            </div>
          ) : null}

          {orphanPids.length > 0 ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {t(
                "ext.streaming.engine.orphan_processes",
                { count: orphanPids.length, pids: orphanPids.join(", ") },
                `Found ${orphanPids.length} external MediaMTX process(es) for this data directory: ${orphanPids.join(", ")}.`,
              )}
            </div>
          ) : null}

          {engineStatus?.last_error ? (
            <div className="errorText" style={{ marginTop: 6 }}>
              {engineStatus.last_error}
            </div>
          ) : null}

          {engineError ? (
            <div className="errorText" style={{ marginTop: 6 }}>
              {engineError}
            </div>
          ) : null}

          <div className="rowWrap" style={{ gap: 8 }}>
            <button
              className="primaryButton"
              type="button"
              disabled={engineBusy}
              onClick={() => void runEngineAction(primaryEngineAction)}
            >
              {primaryEngineLabel}
            </button>
            <button className="chipButton" type="button" disabled={engineBusy} onClick={() => void downloadEngine()}>
              <i className="fa-solid fa-download" aria-hidden="true" />{" "}
              {downloadLabel}
            </button>
            <button
              className="chipButton"
              type="button"
              disabled={engineBusy || !engineRunning}
              onClick={() => void runEngineAction("stop")}
            >
              {stopLabel}
            </button>
            <button className="chipButton" type="button" disabled={engineBusy} onClick={() => void runEngineAction("reclaim")}>
              <i className="fa-solid fa-broom" aria-hidden="true" /> {reclaimLabel}
            </button>
            <button
              className="chipButton"
              type="button"
              disabled={engineBusy || enginePendingAction === "refresh"}
              onClick={() => void refreshEngineStatus()}
            >
              <i className="fa-solid fa-rotate-right" aria-hidden="true" /> {refreshLabel}
            </button>
          </div>

          {engineStatus ? (
            <div style={{ marginTop: 10 }}>
              <div className="cardMeta">
                {t("ext.streaming.engine.meta_restarts", {}, "Reinícios")}: {engineStatus.restart_count ?? 0}
              </div>
              {engineStatus.binary_path ? (
                <div className="cardMeta" style={{ marginTop: 4 }}>
                  {t("ext.streaming.engine.meta_binary", {}, "Binário")}: {engineStatus.binary_path}
                </div>
              ) : null}
              {engineStatus.config_path ? (
                <div className="cardMeta" style={{ marginTop: 4 }}>
                  {t("ext.streaming.engine.meta_config", {}, "Config")}: {engineStatus.config_path}
                </div>
              ) : null}
              {engineStatus.log_path ? (
                <div className="cardMeta" style={{ marginTop: 4 }}>
                  {t("ext.streaming.engine.meta_log", {}, "Log")}: {engineStatus.log_path}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="sectionDivider" />

          <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
            {t("ext.streaming.engine.settings_title", {}, "Configuração")}
          </div>

          {settingsLoading ? <div className="settingsStatusMuted">{t("ext.streaming.engine.settings_loading", {}, "Carregando…")}</div> : null}
          {settingsError ? <div className="errorText">{settingsError}</div> : null}

          {!settingsLoading && engineSettingsDraft ? (
            <div>
              <div className="rowWrap" style={{ gap: 10 }}>
                <label className="rowWrap" style={{ gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={Boolean(engineSettingsDraft.expose_to_lan)}
                    onChange={(event) => {
                      setEngineSettingsDraft((previous) => ({ ...(previous ?? {}), expose_to_lan: event.target.checked }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                  <span className="cardMeta">{t("ext.streaming.engine.expose_to_lan", {}, "Expor na LAN (0.0.0.0)")}</span>
                </label>
                <label className="rowWrap" style={{ gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={engineSettingsDraft.metrics_enabled !== false}
                    onChange={(event) => {
                      setEngineSettingsDraft((previous) => ({ ...(previous ?? {}), metrics_enabled: event.target.checked }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                  <span className="cardMeta">{t("ext.streaming.engine.metrics_enabled", {}, "Enable MediaMTX metrics")}</span>
                </label>
              </div>

              <div className="streamingFormGrid" style={{ marginTop: 10 }}>
                <div className="field">
                  <label className="label">{t("ext.streaming.encoder.global_mode", {}, "Encoder")}</label>
                  <select
                    className="input"
                    value={engineSettingsDraft.encoder_policy?.mode ?? "auto"}
                    onChange={(event) => {
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        encoder_policy: {
                          ...(previous?.encoder_policy ?? {}),
                          mode: event.target.value === "cpu" ? "cpu" : "auto",
                        },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  >
                    <option value="auto">{encoderModeLabel("auto", t)}</option>
                    <option value="cpu">{encoderModeLabel("cpu", t)}</option>
                  </select>
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.encoder.max_restarts", {}, "Restart limit/min")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.encoder_policy?.max_restarts_per_minute ?? 4)}
                    onChange={(event) => {
                      const value = toSafeInt(event.target.value, 4);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        encoder_policy: {
                          ...(previous?.encoder_policy ?? {}),
                          max_restarts_per_minute: Math.max(1, value),
                        },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.media_auth", {}, "HLS media access")}</label>
                  <select
                    className="input"
                    value={engineSettingsDraft.media_auth?.mode ?? "signed_proxy"}
                    onChange={(event) => {
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        media_auth: {
                          ...(previous?.media_auth ?? {}),
                          mode: event.target.value === "open" ? "open" : "signed_proxy",
                        },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  >
                    <option value="signed_proxy">{t("ext.streaming.engine.media_auth_signed", {}, "Signed proxy")}</option>
                    <option value="open">{t("ext.streaming.engine.media_auth_open", {}, "Open LAN")}</option>
                  </select>
                  <div className="cardMeta" style={{ marginTop: 6 }}>
                    {engineSettingsDraft.media_auth?.mode === "open"
                      ? t(
                          "ext.streaming.engine.media_auth_open_warning",
                          {},
                          "Open LAN exposes HLS URLs without media tokens. Use only on trusted local networks.",
                        )
                      : t(
                          "ext.streaming.engine.media_auth_signed_hint",
                          {
                            ttl: String(engineSettingsDraft.media_auth?.token_ttl_seconds ?? 300),
                            renew: String(engineSettingsDraft.media_auth?.renew_margin_seconds ?? 60),
                          },
                          "Signed URLs expire after {{ttl}}s and renew {{renew}}s before expiry.",
                        )}
                    {engineSettingsDraft.media_auth?.mode !== "open"
                      ? ` ${t(
                          "ext.streaming.engine.hls_proxy_main_port_hint",
                          {},
                          "App/web HLS uses the main Toposync port through the signed proxy; this HLS port is internal to MediaMTX.",
                        )}`
                      : ""}
                  </div>
                </div>
              </div>

              <div className="streamingFormGrid streamingFormGridEnginePorts" style={{ marginTop: 10 }}>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.port_rtsp", {}, "RTSP port")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.preferred_ports?.rtsp ?? "")}
                    onChange={(event) => {
                      const value = toOptionalInt(event.target.value);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        preferred_ports: { ...(previous?.preferred_ports ?? {}), rtsp: value ?? undefined },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.port_hls", {}, "Internal HLS port")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.preferred_ports?.hls ?? "")}
                    onChange={(event) => {
                      const value = toOptionalInt(event.target.value);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        preferred_ports: { ...(previous?.preferred_ports ?? {}), hls: value ?? undefined },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.port_webrtc", {}, "WebRTC port")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.preferred_ports?.webrtc ?? "")}
                    onChange={(event) => {
                      const value = toOptionalInt(event.target.value);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        preferred_ports: { ...(previous?.preferred_ports ?? {}), webrtc: value ?? undefined },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.port_webrtc_udp", {}, "WebRTC UDP port")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.preferred_ports?.webrtc_udp ?? "")}
                    onChange={(event) => {
                      const value = toOptionalInt(event.target.value);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        preferred_ports: { ...(previous?.preferred_ports ?? {}), webrtc_udp: value ?? undefined },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.port_api", {}, "API port")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.preferred_ports?.api ?? "")}
                    onChange={(event) => {
                      const value = toOptionalInt(event.target.value);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        preferred_ports: { ...(previous?.preferred_ports ?? {}), api: value ?? undefined },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">{t("ext.streaming.engine.port_metrics", {}, "Metrics port")}</label>
                  <input
                    className="input"
                    value={String(engineSettingsDraft.preferred_ports?.metrics ?? "")}
                    onChange={(event) => {
                      const value = toOptionalInt(event.target.value);
                      setEngineSettingsDraft((previous) => ({
                        ...(previous ?? {}),
                        preferred_ports: { ...(previous?.preferred_ports ?? {}), metrics: value ?? undefined },
                      }));
                      setEngineSettingsDirty(true);
                    }}
                  />
                </div>
              </div>

              <div className="field" style={{ marginTop: 10 }}>
                <label className="label">
                  {t("ext.streaming.engine.ice_servers_label", {}, "STUN/TURN servers (optional, one per line)")}
                </label>
                <textarea
                  className="input"
                  style={{ minHeight: 84, padding: "8px 10px" }}
                  value={joinIceServers(engineSettingsDraft.webrtc_ice_servers)}
                  placeholder={"stun:stun.l.google.com:19302\nturn:username:password@turn.example.com:3478?transport=udp"}
                  onChange={(event) => {
                    setEngineSettingsDraft((previous) => ({
                      ...(previous ?? {}),
                      webrtc_ice_servers: parseIceServers(event.target.value),
                    }));
                    setEngineSettingsDirty(true);
                  }}
                />
                <div className="cardMeta" style={{ marginTop: 6 }}>
                  {t(
                    "ext.streaming.engine.ice_servers_hint",
                    {},
                    "Use only when you need to traverse NAT. On a simple LAN, leave it empty.",
                  )}
                </div>
              </div>

              <div className="field" style={{ marginTop: 10 }}>
                <label className="label">
                  {t("ext.streaming.engine.webrtc_hosts_label", {}, "WebRTC additional hosts (optional, one per line)")}
                </label>
                <textarea
                  className="input"
                  style={{ minHeight: 70, padding: "8px 10px" }}
                  value={joinIceServers(engineSettingsDraft.webrtc_additional_hosts)}
                  placeholder={"192.168.1.10\ncamera.example.com"}
                  onChange={(event) => {
                    setEngineSettingsDraft((previous) => ({
                      ...(previous ?? {}),
                      webrtc_additional_hosts: parseStringList(event.target.value),
                    }));
                    setEngineSettingsDirty(true);
                  }}
                />
                <div className="cardMeta" style={{ marginTop: 6 }}>
                  {t(
                    "ext.streaming.engine.webrtc_hosts_hint",
                    {},
                    "Add the public/LAN hostnames that browsers use for WHEP so ICE candidates can include them.",
                  )}
                </div>
              </div>

              <div className="rowWrap" style={{ gap: 8, justifyContent: "flex-end", marginTop: 10 }}>
                <button
                  className="chipButton"
                  type="button"
                  disabled={!engineSettingsDirty || engineSettingsBusy}
                  onClick={() => {
                    if (!extensionSettings?.engine) return;
                    setEngineSettingsDraft(deepClone(extensionSettings.engine));
                    setEngineSettingsDirty(false);
                  }}
                >
                  {t("ext.streaming.engine.settings_discard", {}, "Descartar")}
                </button>
                <button
                  className="primaryButton"
                  type="button"
                  disabled={!engineSettingsDirty || engineSettingsBusy}
                  onClick={() => void applyEngineSettings()}
                >
                  {engineSettingsBusy ? t("ext.streaming.engine.settings_applying", {}, "Aplicando…") : t("ext.streaming.engine.settings_apply", {}, "Aplicar")}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">
          <div className="settingsDetailHeader" style={{ marginBottom: 2 }}>
            <div>
              <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                {t("ext.streaming.runtime.title", {}, "Runtime")}
              </div>
              <div className="cardMeta">
                {runtimeHealth
                  ? t(
                      "ext.streaming.runtime.meta",
                      {
                        stale: runtimeHealth.stale_after_seconds,
                        placeholder: runtimeHealth.placeholder_after_seconds,
                      },
                      `Stale after ${runtimeHealth.stale_after_seconds}s; placeholder after ${runtimeHealth.placeholder_after_seconds}s.`,
                    )
                  : t("ext.streaming.runtime.meta_empty", {}, "Freshness and publisher state for active streams.")}
              </div>
            </div>

            <div className="rowWrap" style={{ gap: 8, justifyContent: "flex-end" }}>
              <button
                className="chipButton"
                type="button"
                disabled={runtimeHealthLoading}
                onClick={() => {
                  void fetchRuntimeHealthData(undefined, true);
                  void fetchRuntimePipelinesData();
                  void fetchRuntimeObservabilityData();
                  void fetchRuntimeEncodersData();
                }}
              >
                <i className="fa-solid fa-rotate-right" aria-hidden="true" />{" "}
                {runtimeHealthLoading
                  ? t("ext.streaming.runtime.refreshing", {}, "Atualizando...")
                  : t("ext.streaming.runtime.refresh", {}, "Atualizar")}
              </button>
              <button
                className="chipButton"
                type="button"
                disabled={runtimeDiagnosticsBusy}
                onClick={() => void downloadRuntimeDiagnostics()}
              >
                <i className="fa-solid fa-download" aria-hidden="true" />{" "}
                {runtimeDiagnosticsBusy
                  ? t("ext.streaming.runtime.downloading", {}, "Baixando...")
                  : t("ext.streaming.runtime.download_diagnostics", {}, "Baixar diagnostics JSON")}
              </button>
            </div>
          </div>

          {runtimeHealthLoading ? (
            <div className="settingsStatusMuted">{t("ext.streaming.runtime.loading", {}, "Carregando runtime...")}</div>
          ) : null}

          {runtimeHealthError ? <div className="errorText">{runtimeHealthError}</div> : null}
          {runtimeObservabilityError ? <div className="errorText">{runtimeObservabilityError}</div> : null}
          {runtimeEncodersError ? <div className="errorText">{runtimeEncodersError}</div> : null}
          {runtimeDiagnosticsError ? <div className="errorText">{runtimeDiagnosticsError}</div> : null}

          {!runtimeHealthLoading && runtimeRows.length === 0 ? (
            <div className="cardMeta">
              {t("ext.streaming.runtime.empty", {}, "Nenhuma transmissão local aparece no runtime no momento.")}
            </div>
          ) : null}

          {runtimeRows.length > 0 ? (
            <div className="streamingRuntimeTableWrap">
              <table className="streamingRuntimeTable">
                <thead>
                  <tr>
	                    <th>{t("ext.streaming.runtime.table.transmission", {}, "Transmission")}</th>
	                    <th>{t("ext.streaming.runtime.table.output", {}, "Output")}</th>
	                    <th>{t("ext.streaming.runtime.table.profile", {}, "Profile")}</th>
	                    <th>{t("ext.streaming.runtime.table.status", {}, "Status")}</th>
                    <th>{t("ext.streaming.runtime.table.selected_age", {}, "Selected age")}</th>
                    <th>{t("ext.streaming.runtime.table.incoming_age", {}, "Incoming age")}</th>
                    <th>{t("ext.streaming.runtime.table.active_writer", {}, "Active writer")}</th>
                    <th>{t("ext.streaming.runtime.table.pipeline", {}, "Pipeline")}</th>
                    <th>{t("ext.streaming.runtime.table.behavior", {}, "Behavior")}</th>
                    <th>{t("ext.streaming.runtime.table.source", {}, "Source")}</th>
                    <th>{t("ext.streaming.runtime.table.fallback", {}, "Fallback")}</th>
                    <th>{t("ext.streaming.runtime.table.viewers", {}, "Viewers")}</th>
                    <th>{t("ext.streaming.runtime.table.publisher", {}, "Publisher")}</th>
                    <th>{t("ext.streaming.runtime.table.codec", {}, "Codec")}</th>
                    <th>{t("ext.streaming.runtime.table.encoder", {}, "Encoder")}</th>
                    <th>{t("ext.streaming.runtime.table.frames", {}, "Frames")}</th>
                    <th>{t("ext.streaming.runtime.table.restarts", {}, "Restarts")}</th>
                    <th>{t("ext.streaming.runtime.table.last_error", {}, "Last error")}</th>
                    <th>{t("ext.streaming.runtime.table.hls_probe", {}, "HLS probe")}</th>
                  </tr>
                </thead>
                <tbody>
                  {runtimeRows.map(({ key, transmission, output, transmissionLabel, outputLabel, pipelineLink }) => {
                    const status = output?.status ?? transmission.status;
                    const activeWriterId = String(transmission.active_writer_id || "").trim();
                    const fallbackText = transmission.fallback_active
                      ? fallbackReasonLabel(transmission.fallback_reason, t)
                      : boolLabel(false, t);
                    const sourceHealth = output?.source_health ?? transmission.source_health ?? null;
                    return (
                      <tr key={key}>
	                        <td title={transmission.transmission_id}>{transmissionLabel}</td>
	                        <td title={output?.output_key || ""}>{outputLabel}</td>
	                        <td title={formatResolution(output?.resolution)}>
	                          {output
	                            ? (
	                              <>
	                                {qualityProfileLabel(output.quality_profile_id, qualityProfiles?.profiles ?? [], t)}
	                                <span className="streamingRuntimeSubtle">
                                      {output.protocol.toUpperCase()}
                                      {output.latency_profile ? ` / ${output.latency_profile}` : ""}
                                    </span>
                                    <span className="streamingRuntimeSubtle">
	                                  {formatResolution(output.resolution)}
	                                  {output.fps_limit ? ` / ${output.fps_limit} FPS` : ""}
	                                  {output.bitrate_kbps ? ` / ${output.bitrate_kbps} kbps` : ""}
	                                </span>
	                              </>
	                            )
	                            : "-"}
	                        </td>
	                        <td>
                          <span className={["streamingRuntimeBadge", runtimeStatusClass(status)].join(" ")}>
                            {runtimeStatusLabel(status, t)}
                          </span>
                          {transmission.placeholder_active ? (
                            <span className="streamingRuntimeSubtle">
                              {t("ext.streaming.runtime.placeholder_active", {}, "placeholder")}
                            </span>
                          ) : null}
                        </td>
                        <td>{formatRuntimeAge(transmission.selected_frame_age_seconds)}</td>
                        <td>
                          <span title={formatRuntimeUnixTime(transmission.last_live_frame_at_unix)}>
                            {formatRuntimeAge(transmission.last_incoming_frame_age_seconds)}
                          </span>
                        </td>
                        <td title={activeWriterId}>{compactRuntimeId(activeWriterId)}</td>
                        <td title={pipelineLink?.pipeline_name || ""}>
                          {pipelineLink ? (
                            <button className="chipButton" type="button" onClick={() => openPipelineScreen(pipelineLink.pipeline_name)}>
                              {compactRuntimeId(pipelineLink.pipeline_name)}
                            </button>
                          ) : (
                            "-"
                          )}
                        </td>
                        <td>
                          <span className={["streamingRuntimeBadge", pipelineLink?.event_gated ? "is-stale" : "is-live"].join(" ")}>
                            {streamBehaviorLabel(pipelineLink?.stream_behavior ?? transmission.stream_behavior, t)}
                          </span>
                          {transmission.event_gated_idle ? (
                            <span className="streamingRuntimeSubtle">
                              {t("ext.streaming.runtime.event_gated_idle", {}, "idle")}
                            </span>
                          ) : null}
                        </td>
                        <td title={sourceHealthTitle(sourceHealth) || pipelineLink?.source_id || ""}>
                          {sourceHealth ? (
                            <>
                              <span
                                className={[
                                  "streamingRuntimeBadge",
                                  sourceHealth.status === "healthy"
                                    ? "is-live"
                                    : sourceHealth.status === "unknown"
                                      ? "is-unknown"
                                      : "is-stale",
                                ].join(" ")}
                              >
                                {sourceHealth.status || "unknown"}
                              </span>
                              <span className="streamingRuntimeSubtle">
                                {formatRuntimeAge(sourceHealth.source_frame_age_seconds)}
                              </span>
                              <span className="streamingRuntimeSubtle">
                                {sourceHealth.capture_fps != null ? `${Number(sourceHealth.capture_fps).toFixed(1)} fps` : "-"}
                              </span>
                            </>
                          ) : pipelineLink?.source_node_id ? (
                            compactRuntimeId(pipelineLink.source_node_id)
                          ) : (
                            "-"
                          )}
                        </td>
                        <td>{fallbackText}</td>
                        <td>{output ? output.viewer_count : "-"}</td>
                        <td>
                          {output
                            ? output.publisher_running
                              ? t("ext.streaming.runtime.publisher.running", {}, "Running")
                              : t("ext.streaming.runtime.publisher.stopped", {}, "Stopped")
                            : "-"}
                        </td>
                        <td title={output?.publisher_active_codec || ""}>
                          {output ? (
                            <>
                              {output.publisher_active_codec || "-"}
                              {output.publisher_hardware_accelerated ? (
                                <span className="streamingRuntimeSubtle">HW</span>
                              ) : null}
                            </>
                          ) : (
                            "-"
                          )}
                        </td>
                        <td title={output?.publisher_encoder_reason || ""}>
                          {output ? (
                            <>
                              <span className={["streamingRuntimeBadge", encoderStateClass(output.publisher_encoder_state)].join(" ")}>
                                {encoderStateLabel(output.publisher_encoder_state, t)}
                              </span>
                              {output.publisher_encoder_fallback_active ? (
                                <span className="streamingRuntimeSubtle">
                                  {t("ext.streaming.encoder.fallback_cpu", {}, "CPU fallback")}
                                </span>
                              ) : null}
                            </>
                          ) : (
                            "-"
                          )}
                        </td>
                        <td>{output ? output.publisher_frames_sent : "-"}</td>
                        <td>{output ? output.publisher_restart_count ?? 0 : "-"}</td>
                        <td title={output?.publisher_last_error || ""}>
                          {output?.publisher_last_error || "-"}
                        </td>
                        <td>
                          {output?.protocol === "hls" ? (
                            <button
                              className="chipButton"
                              type="button"
                              disabled={hlsProbeBusyKey === `${transmission.transmission_id}:${output.output_id}`}
                              onClick={() => void runHlsProbe(transmission.transmission_id, output.output_id)}
                            >
                              {hlsProbeBusyKey === `${transmission.transmission_id}:${output.output_id}`
                                ? t("ext.streaming.hls_probe.running", {}, "Probing...")
                                : t("ext.streaming.hls_probe.run", {}, "Probe")}
                            </button>
                          ) : (
                            "-"
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}

          {runtimeObservability?.items?.length ? (
            <div className="streamingRuntimeTableWrap">
              <div className="modalSectionTitle" style={{ margin: "10px 0 6px" }}>
                {t("ext.streaming.observability.title", {}, "Streaming Health")}
              </div>
              <table className="streamingRuntimeTable">
                <thead>
                  <tr>
                    <th>{t("ext.streaming.observability.table.stream", {}, "Stream")}</th>
                    <th>{t("ext.streaming.observability.table.classification", {}, "Classification")}</th>
                    <th>{t("ext.streaming.observability.table.source", {}, "Source")}</th>
                    <th>{t("ext.streaming.observability.table.evidence", {}, "Evidence")}</th>
                    <th>{t("ext.streaming.observability.table.sessions", {}, "Sessions")}</th>
                    <th>{t("ext.streaming.observability.table.frames_rate", {}, "Frames/s")}</th>
                    <th>{t("ext.streaming.observability.table.mediamtx", {}, "MediaMTX")}</th>
                    <th>{t("ext.streaming.observability.table.last_event", {}, "Last event")}</th>
                  </tr>
                </thead>
                <tbody>
                  {runtimeObservability.items.map((item) => {
                    const pathInfo = (item.mediamtx?.path ?? {}) as Record<string, unknown>;
                    const sessions = item.active_playback_sessions ?? [];
                    const lastEvent = (item.recent_events ?? []).slice(-1)[0];
                    const recentWebRtcEvent = [...(item.recent_events ?? [])]
                      .reverse()
                      .find((event) => String(event.type || "").startsWith("webrtc_"));
                    const webRtcData = (recentWebRtcEvent?.data ?? {}) as Record<string, unknown>;
                    const webRtcSummary = recentWebRtcEvent
                      ? [
                          webRtcData.iceConnectionState ?? webRtcData.ice_connection_state,
                          webRtcData.rttMs != null ? `${webRtcData.rttMs}ms RTT` : null,
                          webRtcData.packetLossPct != null ? `${Number(webRtcData.packetLossPct).toFixed(1)}% loss` : null,
                          webRtcData.jitterMs != null ? `${webRtcData.jitterMs}ms jitter` : null,
                          String(recentWebRtcEvent.type || "").includes("fallback") ? "HLS fallback" : null,
                        ]
                          .filter(Boolean)
                          .join(" · ")
                      : "";
                    const itemSourceHealth = item.health.source_health ?? null;
                    return (
                      <tr key={`${item.transmission_id}:${item.output_id || "transmission"}`}>
                        <td title={item.output_key || item.transmission_id}>
                          {compactRuntimeId(item.transmission_id)} / {item.output_id || "-"}
                        </td>
                        <td>
                          <span
                            className={[
                              "streamingRuntimeBadge",
                              item.classification === "healthy" ? "is-live" : item.classification === "unknown" ? "is-unknown" : "is-stale",
                            ].join(" ")}
                          >
                            {observabilityClassificationLabel(item.classification)}
                          </span>
                        </td>
                        <td title={sourceHealthTitle(itemSourceHealth)}>
                          {itemSourceHealth ? (
                            <>
                              <span
                                className={[
                                  "streamingRuntimeBadge",
                                  itemSourceHealth.status === "healthy" ? "is-live" : "is-stale",
                                ].join(" ")}
                              >
                                {itemSourceHealth.status || "unknown"}
                              </span>
                              <span className="streamingRuntimeSubtle">
                                {formatRuntimeAge(itemSourceHealth.source_frame_age_seconds)}
                              </span>
                            </>
                          ) : (
                            "-"
                          )}
                        </td>
                        <td title={(item.evidence ?? []).join(" ")}>
                          {(item.evidence ?? []).slice(0, 2).join(" ") || "-"}
                        </td>
                        <td title={sessions.map((session) => `${session.client_kind}:${session.playback_session_id}`).join("\n")}>
                          {sessions.length}
                        </td>
                        <td>
                          {Number.isFinite(item.publisher_frames_sent_rate ?? NaN)
                            ? Number(item.publisher_frames_sent_rate).toFixed(1)
                            : "-"}
                        </td>
                        <td title={JSON.stringify(item.mediamtx ?? {})}>
                          {pathInfo.ready === true ? "ready" : pathInfo.ready === false ? "not ready" : "-"}
                          {webRtcSummary ? <span className="streamingRuntimeSubtle">{webRtcSummary}</span> : null}
                        </td>
                        <td title={lastEvent ? JSON.stringify(lastEvent) : ""}>
                          {lastEvent ? String(lastEvent.type || "-") : "-"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}

          {(runtimeEncoders?.outputs?.length || runtimeEncoders?.states?.length) ? (
            <div className="streamingRuntimeTableWrap">
              <div className="modalSectionTitle" style={{ margin: "10px 0 6px" }}>
                {t("ext.streaming.encoder.health_title", {}, "Encoder Health")}
              </div>
              <table className="streamingRuntimeTable">
                <thead>
                  <tr>
                    <th>{t("ext.streaming.encoder.table.output", {}, "Output")}</th>
                    <th>{t("ext.streaming.encoder.table.mode", {}, "Mode")}</th>
                    <th>{t("ext.streaming.encoder.table.codec", {}, "Codec")}</th>
                    <th>{t("ext.streaming.encoder.table.state", {}, "State")}</th>
                    <th>{t("ext.streaming.encoder.table.restarts", {}, "Restarts")}</th>
                    <th>{t("ext.streaming.encoder.table.stderr", {}, "Last stderr")}</th>
                    <th>{t("ext.streaming.encoder.table.action", {}, "Action")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(runtimeEncoders.outputs ?? []).map((item) => {
                    const tail = item.stderr_tail ?? [];
                    const lastStderr = tail[tail.length - 1] ?? item.last_error ?? "";
                    const quarantinedState = (runtimeEncoders.states ?? []).find(
                      (state) => state.state === "quarantined" && state.last_output_id === item.output_key,
                    );
                    return (
                      <tr key={item.output_key}>
                        <td title={item.output_key}>{compactRuntimeId(item.output_key)}</td>
                        <td>{encoderModeLabel(item.encoder_mode, t)}</td>
                        <td title={item.active_codec || ""}>
                          {item.active_codec || "-"}
                          {item.hardware_accelerated ? <span className="streamingRuntimeSubtle">HW</span> : null}
                        </td>
                        <td title={item.encoder_reason || ""}>
                          <span className={["streamingRuntimeBadge", encoderStateClass(item.encoder_state)].join(" ")}>
                            {encoderStateLabel(item.encoder_state, t)}
                          </span>
                          {item.encoder_fallback_active ? (
                            <span className="streamingRuntimeSubtle">{t("ext.streaming.encoder.fallback_cpu", {}, "CPU fallback")}</span>
                          ) : null}
                        </td>
                        <td title={String(item.restart_window_count ?? 0)}>
                          {item.restart_count ?? 0}
                        </td>
                        <td title={[...tail, item.log_path || ""].filter(Boolean).join("\n")}>
                          {lastStderr || "-"}
                        </td>
                        <td>
                          {quarantinedState ? (
                            <button
                              className="chipButton"
                              type="button"
                              disabled={runtimeEncoderClearBusy}
                              onClick={() => void retryHardwareEncoder(quarantinedState.encoder)}
                            >
                              {t("ext.streaming.encoder.retry_hardware", {}, "Retry hardware")}
                            </button>
                          ) : (
                            "-"
                          )}
                        </td>
                      </tr>
                    );
                  })}
                  {(runtimeEncoders.states ?? [])
                    .filter((state) => state.state === "quarantined")
                    .map((state) => (
                      <tr key={`state:${state.encoder}`}>
                        <td title={state.last_output_id || ""}>{state.encoder}</td>
                        <td>{encoderModeLabel(runtimeEncoders.policy?.mode, t)}</td>
                        <td>{state.encoder}</td>
                        <td title={state.reason || state.last_error || ""}>
                          <span className={["streamingRuntimeBadge", encoderStateClass(state.state)].join(" ")}>
                            {encoderStateLabel(state.state, t)}
                          </span>
                          {state.until_unix ? (
                            <span className="streamingRuntimeSubtle">{formatRuntimeUnixTime(state.until_unix)}</span>
                          ) : null}
                        </td>
                        <td>{state.failure_count ?? 0}</td>
                        <td title={state.last_error || ""}>{state.last_error || "-"}</td>
                        <td>
                          <button
                            className="chipButton"
                            type="button"
                            disabled={runtimeEncoderClearBusy}
                            onClick={() => void retryHardwareEncoder(state.encoder)}
                          >
                            {t("ext.streaming.encoder.retry_hardware", {}, "Retry hardware")}
                          </button>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {runtimePipelinesError ? <div className="errorText">{runtimePipelinesError}</div> : null}

          {runtimePipelines?.pipelines?.length ? (
            <div className="streamingRuntimeTableWrap">
              <table className="streamingRuntimeTable">
                <thead>
                  <tr>
                    <th>{t("ext.streaming.runtime.pipeline_table.pipeline", {}, "Pipeline")}</th>
                    <th>{t("ext.streaming.runtime.pipeline_table.behavior", {}, "Behavior")}</th>
                    <th>{t("ext.streaming.runtime.pipeline_table.writer", {}, "Writer")}</th>
                    <th>{t("ext.streaming.runtime.pipeline_table.source", {}, "Source")}</th>
                    <th>{t("ext.streaming.runtime.pipeline_table.nodes", {}, "Nodes")}</th>
                    <th>{t("ext.streaming.runtime.pipeline_table.warnings", {}, "Warnings")}</th>
                  </tr>
                </thead>
                <tbody>
                  {runtimePipelines.pipelines.map((link) => {
                    const nodesText = (link.nodes ?? [])
                      .map((node) => {
                        const suffix = node.stream_publish ? "*" : node.upstream_to_publish ? "^" : "";
                        return `${node.node_id}:${node.operator_id}${suffix}`;
                      })
                      .join(" -> ");
                    const edgeText = (link.edges ?? [])
                      .map((edge) => `${edge.source_node_id}.${edge.source_port || "out"} -> ${edge.target_node_id}.${edge.target_port || "in"}`)
                      .join("\n");
                    const warnings = (link.warnings ?? []).join(" ");
                    return (
                      <tr key={`${link.transmission_id}:${link.pipeline_name}:${link.publish_node_id}`}>
                        <td title={link.pipeline_name}>
                          <button className="chipButton" type="button" onClick={() => openPipelineScreen(link.pipeline_name)}>
                            {compactRuntimeId(link.pipeline_name)}
                          </button>
                        </td>
                        <td>
                          <span className={["streamingRuntimeBadge", link.event_gated ? "is-stale" : "is-live"].join(" ")}>
                            {streamBehaviorLabel(link.stream_behavior, t)}
                          </span>
                        </td>
                        <td title={link.writer_id}>{compactRuntimeId(link.writer_id)}</td>
                        <td title={[link.source_id || "", link.camera_id || "", link.source_node_id || ""].filter(Boolean).join("\n")}>
                          {link.camera_id || link.source_node_id ? (
                            <>
                              <span>{compactRuntimeId(link.source_node_id || link.camera_id || "")}</span>
                              {link.camera_id ? <span className="streamingRuntimeSubtle">{compactRuntimeId(link.camera_id)}</span> : null}
                            </>
                          ) : (
                            "-"
                          )}
                        </td>
                        <td title={edgeText}>{nodesText || "-"}</td>
                        <td title={warnings}>{warnings || "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}

          {hlsProbeError ? <div className="errorText">{hlsProbeError}</div> : null}

          {Object.keys(hlsProbeByKey).length > 0 ? (
            <div className="streamingRuntimeTableWrap">
              <table className="streamingRuntimeTable">
                <thead>
                  <tr>
                    <th>{t("ext.streaming.hls_probe.table.output", {}, "Output")}</th>
                    <th>{t("ext.streaming.hls_probe.table.status", {}, "Status")}</th>
                    <th>{t("ext.streaming.hls_probe.table.playlist", {}, "Playlist")}</th>
                    <th>{t("ext.streaming.hls_probe.table.sequence", {}, "Sequence")}</th>
                    <th>{t("ext.streaming.hls_probe.table.target", {}, "Target")}</th>
                    <th>{t("ext.streaming.hls_probe.table.tail", {}, "Tail segment")}</th>
                    <th>{t("ext.streaming.hls_probe.table.tail_status", {}, "Tail HTTP")}</th>
                    <th>{t("ext.streaming.hls_probe.table.tail_reachable", {}, "Tail reachable")}</th>
                    <th>{t("ext.streaming.hls_probe.table.sampled", {}, "Last probe")}</th>
                    <th>{t("ext.streaming.hls_probe.table.changed", {}, "Last playlist change")}</th>
                    <th>{t("ext.streaming.hls_probe.table.error", {}, "Error")}</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(hlsProbeByKey).map(([key, probe]) => (
                    <tr key={key}>
                      <td title={key}>{probe.output_id || "-"}</td>
                      <td>
                        <span className={["streamingRuntimeBadge", probe.status === "ok" ? "is-live" : "is-stale"].join(" ")}>
                          {hlsProbeStatusLabel(probe.status, t)}
                        </span>
                      </td>
                      <td>{boolLabel(probe.playlist_reachable, t)}</td>
                      <td>{probe.media_sequence ?? "-"}</td>
                      <td>{probe.target_duration_seconds ? `${probe.target_duration_seconds}s` : "-"}</td>
                      <td title={probe.tail_segment_url || ""}>{probe.tail_segment_url || "-"}</td>
                      <td>{probe.tail_segment_http_status ?? "-"}</td>
                      <td>{boolLabel(probe.tail_segment_reachable, t)}</td>
                      <td>{formatRuntimeUnixTime(probe.sampled_at_unix)}</td>
                      <td>{formatRuntimeUnixTime(hlsProbeLastChangeByKey[key])}</td>
                      <td title={probe.error || ""}>{probe.error || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="settingsSplit">
        <div className="settingsSplitSidebar">
          <div className="settingsSplitToolbar">
            <input
              className="input"
              placeholder={t("ext.streaming.transmissions.search", {}, "Buscar transmissões…")}
              value={transmissionQuery}
              onChange={(event) => setTransmissionQuery(event.target.value)}
            />
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.streaming.transmissions.add", {}, "Adicionar transmissão")}
              onClick={() => {
                setCreateError(null);
                setNewTransmissionHostServerId("local");
                setCreateModalOpen(true);
              }}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          {transmissionsLoading ? <div className="settingsStatusMuted" style={{ marginTop: 10 }}>{t("ext.streaming.transmissions.loading", {}, "Carregando…")}</div> : null}
          {transmissionsError ? <div className="errorText" style={{ marginTop: 10 }}>{transmissionsError}</div> : null}
          {processingServersLoading ? (
            <div className="settingsStatusMuted" style={{ marginTop: 10 }}>
              {t("ext.streaming.processing_servers.loading", {}, "Loading processing servers…")}
            </div>
          ) : null}
          {processingServersError ? <div className="errorText" style={{ marginTop: 10 }}>{processingServersError}</div> : null}

          {filteredTransmissions.length === 0 && !transmissionsLoading ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div>
                  {t("ext.streaming.transmissions.empty", {}, "Nenhuma transmissão criada.")}
                </div>
                <ol className="streamingQuickStepsList streamingQuickStepsListCompact">
                  <li>{t("ext.streaming.settings.quickstart_step_1", {}, "Crie uma transmissão com ao menos uma saída.")}</li>
                  <li>{t("ext.streaming.settings.quickstart_step_2", {}, "Ajuste resolução/FPS/autenticação por saída.")}</li>
                  <li>{t("ext.streaming.settings.quickstart_step_3", {}, "Salve, carregue URLs e use o wizard para gerar o fluxo.")}</li>
                </ol>
                <button
                  className="primaryButton"
                  type="button"
                  onClick={() => {
                    setNewTransmissionHostServerId("local");
                    setCreateModalOpen(true);
                  }}
                >
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.streaming.transmissions.add", {}, "Adicionar transmissão")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsList">
              {filteredTransmissions.map((item) => {
                const selected = item.id === activeTransmissionId;
                const name = String(item.name || "").trim() || String(item.path || "").trim() || item.id;
                const hostServerId = normalizeServerId(item.host_server_id);
                const outputCount = Array.isArray(item.outputs) ? item.outputs.length : 0;
                const generatedLabel =
                  item.generated_by === "camera_live_view"
                    ? t("ext.streaming.transmissions.generated_by_live", {}, "Gerada por visualização da câmera")
                    : "";
                const meta = t(
                  "ext.streaming.transmissions.meta_line",
                  { host: hostServerId, path: item.path || "-", outputs: outputCount },
                  `host: ${hostServerId} • path: ${item.path || "-"} • outputs: ${outputCount}`,
                );
                return (
                  <button
                    key={item.id}
                    type="button"
                    className={["choiceItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                    onClick={() => requestSelectTransmission(item.id)}
                  >
                    <div className="settingsListItemRow">
                      <div className="settingsListItemMain">
                        <div className="settingsListItemTitle" title={name}>
                          {name}
                        </div>
                        <div className="settingsListItemMeta" title={meta}>
                          {meta}
                        </div>
                        {generatedLabel ? <div className="settingsListItemMeta">{generatedLabel}</div> : null}
                      </div>
                      {!item.enabled ? (
                        <span
                          className="pillBadge"
                          title={t("ext.streaming.transmissions.badge_disabled_title", {}, "Disabled")}
                        >
                          {t("ext.streaming.transmissions.badge_disabled", {}, "off")}
                        </span>
                      ) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="settingsSplitMain">
          {!activeTransmission || !transmissionDraft ? (
            <div className="card">
              <div className="cardBody">
                <div>
                  {t("ext.streaming.transmissions.select", {}, "Selecione uma transmissão para editar.")}
                </div>
                <div className="cardMeta">
                  {t("ext.streaming.transmissions.select_hint", {}, "Dica: comece criando uma transmissão e depois abra o wizard para gerar o fluxo.")}
                </div>
                <button
                  className="primaryButton"
                  type="button"
                  onClick={() => {
                    setNewTransmissionHostServerId("local");
                    setCreateModalOpen(true);
                  }}
                >
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.streaming.transmissions.add", {}, "Adicionar transmissão")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsDetail">
              <div className="settingsDetailHeader">
                <div>
                  <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                    {transmissionDraft.name?.trim() || transmissionDraft.path?.trim() || transmissionDraft.id}
                  </div>
                  <div className="cardMeta">ID: {transmissionDraft.id}</div>
                </div>

                <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                  {transmissionDraftDirty ? (
                    <span
                      className="pillBadge"
                      title={t("ext.streaming.transmissions.badge_unsaved_title", {}, "Unsaved changes")}
                    >
                      {t("ext.streaming.transmissions.badge_unsaved", {}, "pending")}
                    </span>
                  ) : null}
                  <button className="chipButton" type="button" disabled={!transmissionDraftDirty || transmissionDraftBusy} onClick={discardDraftChanges}>
                    {t("ext.streaming.transmissions.discard", {}, "Descartar")}
                  </button>
                  <button
                    className="primaryButton"
                    type="button"
                    disabled={!transmissionDraftDirty || transmissionDraftBusy}
                    onClick={() => void saveDraftChanges()}
                  >
                    {transmissionDraftBusy ? t("ext.streaming.transmissions.saving", {}, "Salvando…") : t("ext.streaming.transmissions.save", {}, "Salvar")}
                  </button>
                  <button className="chipButton" type="button" disabled={transmissionDraftBusy} onClick={() => setConfirmDeleteOpen(true)}>
                    {t("ext.streaming.transmissions.delete", {}, "Excluir")}
                  </button>
                </div>
              </div>

              {transmissionDraftError ? <div className="errorText" style={{ marginTop: 10 }}>{transmissionDraftError}</div> : null}

              <div className="sectionDivider" />

              <div className="card">
                <div className="cardBody">
                  <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                    {t("ext.streaming.transmissions.basic", {}, "Básico")}
                  </div>

                  <div className="streamingFormGrid streamingFormGridTransmissionPrimary">
                    <div className="field">
                      <label className="label">{t("ext.streaming.transmissions.name", {}, "Nome")}</label>
                      <input
                        className="input"
                        value={transmissionDraft.name ?? ""}
                        onChange={(event) => updateDraft({ name: event.target.value })}
                      />
                    </div>
                    <div className="field">
                      <label className="label">{t("ext.streaming.transmissions.path", {}, "Path/slug")}</label>
                      <input
                        className="input"
                        value={transmissionDraft.path ?? ""}
                        onChange={(event) => updateDraft({ path: event.target.value })}
                      />
                      <div className="cardMeta" style={{ marginTop: 6 }}>
                        {t("ext.streaming.transmissions.path_hint", {}, "Use apenas a-z, 0-9, - e _. O servidor normaliza automaticamente.")}
                      </div>
                    </div>
                  </div>

                  <div className="rowWrap" style={{ gap: 14, marginTop: 10 }}>
                    <label className="rowWrap" style={{ gap: 8 }}>
                      <input
                        type="checkbox"
                        checked={Boolean(transmissionDraft.enabled)}
                        onChange={(event) => updateDraft({ enabled: event.target.checked })}
                      />
                      <span className="cardMeta">{t("ext.streaming.transmissions.enabled", {}, "Ativa")}</span>
                    </label>

                  </div>

                  <div className="streamingFormGrid streamingFormGridTransmissionSecondary" style={{ marginTop: 10 }}>
                    <div className="field">
                      <label className="label">{t("ext.streaming.transmissions.host_server", {}, "Host server")}</label>
                      <select
                        className="input"
                        value={normalizeServerId(transmissionDraft.host_server_id)}
                        onChange={(event) => updateDraft({ host_server_id: normalizeServerId(event.target.value) })}
                      >
                        {processingServers.map((server) => {
                          const serverId = normalizeServerId(server.id);
                          const serverName = String(server.name || "").trim();
                          const label =
                            serverId === "local"
                              ? t("ext.streaming.processing_servers.local_label", {}, "local (this machine)")
                              : serverName
                                ? `${serverId} (${serverName})`
                                : serverId;
                          return (
                            <option key={serverId} value={serverId}>
                              {label}
                            </option>
                          );
                        })}
                        {!knownProcessingServerIds.has(normalizeServerId(transmissionDraft.host_server_id)) ? (
                          <option value={normalizeServerId(transmissionDraft.host_server_id)}>
                            {normalizeServerId(transmissionDraft.host_server_id)}
                          </option>
                        ) : null}
                      </select>
                      <div className="cardMeta" style={{ marginTop: 6 }}>
                        {t("ext.streaming.transmissions.host_server_hint", {}, "A transmissão será hospedada neste processing server.")}
                      </div>
                    </div>

                    <div className="field">
                      <label className="label">{t("ext.streaming.transmissions.placeholder", {}, "Placeholder")}</label>
                      <select
                        className="input"
                        value={transmissionDraft.placeholder ?? "gray"}
                        onChange={(event) => updateDraft({ placeholder: event.target.value as "gray" | "black" })}
                      >
                        <option value="gray">{t("ext.streaming.transmissions.placeholder.gray", {}, "Gray")}</option>
                        <option value="black">{t("ext.streaming.transmissions.placeholder.black", {}, "Black")}</option>
                      </select>
                    </div>

                    <div className="field">
                      <label className="label">{t("ext.streaming.transmissions.arbitration", {}, "Arbitragem")}</label>
                      <select
                        className="input"
                        value={transmissionDraft.arbitration ?? "priority_latest"}
                        onChange={(event) =>
                          updateDraft({ arbitration: event.target.value as "latest" | "priority_latest" })
                        }
                      >
                        <option value="priority_latest">
                          {t("ext.streaming.transmissions.arbitration.priority_latest", {}, "Priority, then latest")}
                        </option>
                        <option value="latest">
                          {t("ext.streaming.transmissions.arbitration.latest", {}, "Latest writer wins")}
                        </option>
                      </select>
                    </div>
                  </div>

                  <div style={{ marginTop: 14 }}>
                    <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                      {t("ext.streaming.transmissions.camera_controls.title", {}, "Controles de câmera")}
                    </div>

                    <div className="rowWrap" style={{ gap: 10, alignItems: "center" }}>
                      <label className="rowWrap" style={{ gap: 8 }}>
                        <input
                          type="checkbox"
                          checked={Boolean(transmissionDraft.camera_controls?.enabled)}
                          disabled={
                            !Boolean(transmissionDraft.camera_controls?.enabled) &&
                            (availableCamerasLoading || availableCameras.length === 0)
                          }
                          onChange={(event) => {
                            const enabled = event.target.checked;
                            if (!enabled) {
                              updateDraft({ camera_controls: null });
                              return;
                            }
                            const current = String(transmissionDraft.camera_controls?.camera_id || "").trim();
                            const fallbackCamera = availableCameras[0] ?? null;
                            const fallback = String(fallbackCamera?.id || "").trim();
                            const nextCameraId = current || fallback;
                            updateDraft({
                              camera_controls: {
                                enabled: true,
                                camera_id: nextCameraId,
                                camera_source_id:
                                  transmissionDraft.camera_controls?.camera_source_id ||
                                  defaultCameraSourceId(
                                    availableCameras.find((camera) => String(camera.id || "").trim() === nextCameraId) ?? fallbackCamera,
                                  ) ||
                                  null,
                              },
                            });
                          }}
                        />
                        <span className="cardMeta">
                          {t("ext.streaming.transmissions.camera_controls.enable", {}, "Habilitar controles de câmera")}
                        </span>
                      </label>

                      {availableCamerasLoading ? (
                        <div className="settingsStatusMuted">
                          {t("ext.streaming.transmissions.camera_controls.loading", {}, "Carregando câmeras…")}
                        </div>
                      ) : null}
                    </div>

                    <div className="cardMeta" style={{ marginTop: 6 }}>
                      {t(
                        "ext.streaming.transmissions.camera_controls.hint",
                        {},
                        "Quando habilitado, esta transmissão poderá controlar uma câmera (presets/PTZ) via API.",
                      )}
                    </div>

                    {availableCamerasError ? <div className="errorText" style={{ marginTop: 8 }}>{availableCamerasError}</div> : null}

                    {Boolean(transmissionDraft.camera_controls?.enabled) ? (
                      <div className="field" style={{ marginTop: 10 }}>
                        <label className="label">{t("ext.streaming.transmissions.camera_controls.camera", {}, "Câmera")}</label>
                        <select
                          className="input"
                          value={String(transmissionDraft.camera_controls?.camera_id || "").trim()}
                          onChange={(event) => {
                            const cid = String(event.target.value || "").trim();
                            const camera = availableCameras.find((item) => String(item.id || "").trim() === cid) ?? null;
                            updateDraft({
                              camera_controls: { enabled: true, camera_id: cid, camera_source_id: defaultCameraSourceId(camera) || null },
                            });
                          }}
                          disabled={availableCameras.length === 0}
                        >
                          {availableCameras.map((camera) => {
                            const cid = String(camera.id || "").trim();
                            const label = String(camera.name || "").trim() || cid;
                            return (
                              <option key={cid} value={cid}>
                                {label}
                              </option>
                            );
                          })}
                          {(() => {
                            const current = String(transmissionDraft.camera_controls?.camera_id || "").trim();
                            const known = availableCameras.some((camera) => String(camera.id || "").trim() === current);
                            if (!current || known) return null;
                            return (
                              <option value={current}>
                                {current}
                              </option>
                            );
                          })()}
                        </select>
                        {availableCameras.length === 0 && !availableCamerasLoading ? (
                          <div className="cardMeta" style={{ marginTop: 6 }}>
                            {t(
                              "ext.streaming.transmissions.camera_controls.empty",
                              {},
                              "Nenhuma câmera encontrada. Cadastre uma câmera em Configurações > Câmeras.",
                            )}
                          </div>
                        ) : null}
                        {(() => {
                          const currentCameraId = String(transmissionDraft.camera_controls?.camera_id || "").trim();
                          const camera = availableCameras.find((item) => String(item.id || "").trim() === currentCameraId) ?? null;
                          const options = cameraSourceOptions(camera);
                          if (!currentCameraId || options.length === 0) return null;
                          return (
                            <div className="field" style={{ marginTop: 10 }}>
                              <label className="label">{t("ext.streaming.transmissions.camera_controls.source", {}, "Fonte da câmera")}</label>
                              <select
                                className="input"
                                value={String(transmissionDraft.camera_controls?.camera_source_id || defaultCameraSourceId(camera) || "").trim()}
                                onChange={(event) =>
                                  updateDraft({
                                    camera_controls: {
                                      enabled: true,
                                      camera_id: currentCameraId,
                                      camera_source_id: String(event.target.value || "").trim() || null,
                                    },
                                  })
                                }
                              >
                                {options.map((source) => (
                                  <option key={source.id} value={source.id}>
                                    {source.label}
                                  </option>
                                ))}
                              </select>
                            </div>
                          );
                        })()}
                      </div>
                    ) : null}
                  </div>

                  {!knownProcessingServerIds.has(normalizeServerId(transmissionDraft.host_server_id)) ? (
                    <div className="errorText" style={{ marginTop: 10 }}>
                      {t(
                        "ext.streaming.transmissions.host_server_missing",
                        {},
                        "This host server no longer exists. Select another one before saving.",
                      )}
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="sectionDivider" />

              <div className="card">
                <div className="cardBody">
                  <div className="settingsDetailHeader" style={{ marginBottom: 10 }}>
                    <div>
                      <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                        {t("ext.streaming.transmissions.outputs", {}, "Saídas")}
                      </div>
                      <div className="cardMeta">
                        {t("ext.streaming.transmissions.outputs_hint", {}, "Cada saída pode ter resolução/FPS/bitrate diferentes.")}
                      </div>
	                    </div>
	                    <div className="rowWrap" style={{ gap: 8, justifyContent: "flex-end" }}>
	                      <button
	                        className="chipButton"
	                        type="button"
	                        disabled={applyQualityProfilesBusy || transmissionDraftDirty}
	                        onClick={() => void applyQualityProfilesToDraft()}
	                        title={
	                          transmissionDraftDirty
	                            ? t("ext.streaming.quality.apply_save_first_title", {}, "Save this transmission before applying generated profile outputs.")
	                            : t("ext.streaming.quality.apply_title", {}, "Replace generated HLS profile outputs and preserve custom outputs.")
	                        }
	                      >
	                        {applyQualityProfilesBusy
	                          ? t("ext.streaming.quality.applying", {}, "Applying profiles...")
	                          : t("ext.streaming.quality.apply", {}, "Apply quality profile outputs")}
	                      </button>
                      <button
                        className="chipButton"
                        type="button"
                        disabled={applyWebRtcCompanionBusy || transmissionDraftDirty}
                        onClick={() => void applyWebRtcCompanionToDraft()}
                        title={
                          transmissionDraftDirty
                            ? t("ext.streaming.webrtc.apply_save_first_title", {}, "Save this transmission before applying the WebRTC low-latency output.")
                            : t("ext.streaming.webrtc.apply_title", {}, "Create or update the WebRTC low-latency companion output.")
                        }
                      >
                        {applyWebRtcCompanionBusy
                          ? t("ext.streaming.webrtc.applying", {}, "Applying WebRTC...")
                          : t("ext.streaming.webrtc.apply", {}, "Apply WebRTC low-latency output")}
                      </button>
	                      <button className="chipButton" type="button" onClick={() => addDraftOutput("hls")}>
	                        {t("ext.streaming.outputs.add_hls", {}, "+ HLS")}
	                      </button>
                      <button className="chipButton" type="button" onClick={() => addDraftOutput("rtsp")}>
                        {t("ext.streaming.outputs.add_rtsp", {}, "+ RTSP")}
                      </button>
                      <button className="chipButton" type="button" onClick={() => addDraftOutput("webrtc")}>
                        {t("ext.streaming.outputs.add_webrtc", {}, "+ WebRTC")}
                      </button>
                    </div>
                  </div>

	                  {Array.isArray(transmissionDraft.outputs) && transmissionDraft.outputs.length === 0 ? (
	                    <div className="cardMeta">{t("ext.streaming.transmissions.outputs_empty", {}, "Nenhuma saída adicionada.")}</div>
	                  ) : null}
	                  {qualityProfilesError ? <div className="errorText">{qualityProfilesError}</div> : null}

                  {Array.isArray(transmissionDraft.outputs)
                    ? transmissionDraft.outputs.map((output) => {
                        const auth = output.authentication ?? defaultAuthentication();
                        const resolution = output.resolution ?? { width: 1280, height: 720 };
                        return (
                          <div key={output.id} className="card" style={{ marginTop: 10 }}>
                            <div className="cardBody">
                              <div className="settingsDetailHeader">
                                <div>
                                  <div className="settingsListItemTitle">
                                    {output.protocol.toUpperCase()}{" "}
                                    <span className="cardMeta" style={{ fontWeight: 500 }}>
                                      ({resolution.width ?? "-"}x{resolution.height ?? "-"})
                                    </span>
                                  </div>
	                                  <div className="settingsListItemMeta">
	                                    {t("ext.streaming.outputs.output_id", {}, "Output ID")}: {output.id}
	                                  </div>
	                                  {output.protocol === "hls" ? (
	                                    <div className="settingsListItemMeta">
	                                      {qualityProfileLabel(output.quality_profile_id, qualityProfiles?.profiles ?? [], t)} ·{" "}
	                                      {formatOutputNetworkCost(output.bitrate_kbps, t)}
	                                    </div>
	                                  ) : null}
	                                </div>
                                <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                                  <label className="rowWrap" style={{ gap: 8 }}>
                                    <input
                                      type="checkbox"
                                      checked={Boolean(output.enabled)}
                                      onChange={(event) => updateDraftOutput(output.id, { enabled: event.target.checked })}
                                    />
                                    <span className="cardMeta">{t("ext.streaming.outputs.enabled", {}, "Ativa")}</span>
                                  </label>
                                  <button className="chipButton" type="button" onClick={() => removeDraftOutput(output.id)}>
                                    {t("ext.streaming.outputs.remove", {}, "Remover")}
                                  </button>
                                </div>
                              </div>

                              <div className="streamingFormGrid streamingFormGridOutputMain" style={{ marginTop: 10 }}>
	                                <div className="field">
	                                  <label className="label">{t("ext.streaming.outputs.protocol", {}, "Protocolo")}</label>
	                                  <select
                                    className="input"
                                    value={output.protocol}
                                    onChange={(event) =>
                                      updateDraftOutput(output.id, { protocol: event.target.value as "hls" | "rtsp" | "webrtc" })
                                    }
                                  >
                                    <option value="hls">HLS</option>
                                    <option value="rtsp">RTSP</option>
	                                    <option value="webrtc">WebRTC</option>
	                                  </select>
	                                </div>

	                                {output.protocol === "hls" ? (
	                                  <div className="field">
	                                    <label className="label">{t("ext.streaming.outputs.quality_profile", {}, "Quality profile")}</label>
	                                    <select
	                                      className="input"
	                                      value={output.quality_profile_id ?? ""}
	                                      onChange={(event) => {
	                                        const profileId = event.target.value as StreamingQualityProfileId | "";
	                                        if (!profileId) {
	                                          updateDraftOutput(output.id, { quality_profile_id: null });
	                                          return;
	                                        }
	                                        const profile = qualityProfiles?.profiles.find((item) => item.id === profileId);
	                                        updateDraftOutput(output.id, {
	                                          quality_profile_id: profileId,
	                                          resolution: profile?.resolution ?? output.resolution ?? null,
	                                          fps_limit: profile?.fps_limit ?? output.fps_limit ?? null,
	                                          bitrate_kbps: profile?.bitrate_kbps ?? output.bitrate_kbps ?? null,
	                                          latency_profile: profile?.latency_profile ?? output.latency_profile ?? "normal",
	                                        });
	                                      }}
	                                    >
	                                      <option value="">{t("ext.streaming.quality.custom", {}, "Custom")}</option>
	                                      {(qualityProfiles?.profiles ?? []).map((profile) => (
	                                        <option key={profile.id} value={profile.id}>
	                                          {profile.label}
	                                        </option>
	                                      ))}
	                                    </select>
	                                    <div className="cardMeta" style={{ marginTop: 6 }}>
	                                      {formatOutputNetworkCost(output.bitrate_kbps, t)}
	                                    </div>
	                                  </div>
	                                ) : null}

	                                <div className="field">
	                                  <label className="label">{t("ext.streaming.outputs.width", {}, "Largura")}</label>
                                  <input
                                    className="input"
                                    value={String(resolution.width ?? "")}
                                    onChange={(event) => {
                                      const width = toOptionalInt(event.target.value);
                                      if (width === null) {
                                        // Avoid sending a partial resolution (backend requires width+height).
                                        updateDraftOutput(output.id, { resolution: null });
                                        return;
                                      }
                                      const currentHeight =
                                        typeof resolution.height === "number" && resolution.height > 0 ? resolution.height : 720;
                                      updateDraftOutput(output.id, {
                                        resolution: {
                                          width: Math.max(1, width),
                                          height: Math.max(1, currentHeight),
                                        },
                                      });
                                    }}
                                  />
                                </div>
                                <div className="field">
                                  <label className="label">{t("ext.streaming.outputs.height", {}, "Altura")}</label>
                                  <input
                                    className="input"
                                    value={String(resolution.height ?? "")}
                                    onChange={(event) => {
                                      const height = toOptionalInt(event.target.value);
                                      if (height === null) {
                                        // Avoid sending a partial resolution (backend requires width+height).
                                        updateDraftOutput(output.id, { resolution: null });
                                        return;
                                      }
                                      const currentWidth =
                                        typeof resolution.width === "number" && resolution.width > 0 ? resolution.width : 1280;
                                      updateDraftOutput(output.id, {
                                        resolution: {
                                          width: Math.max(1, currentWidth),
                                          height: Math.max(1, height),
                                        },
                                      });
                                    }}
                                  />
                                </div>
                                <div className="field">
                                  <label className="label">{t("ext.streaming.outputs.fps", {}, "FPS")}</label>
                                  <input
                                    className="input"
                                    value={output.fps_limit === null || output.fps_limit === undefined ? "" : String(output.fps_limit)}
                                    onChange={(event) => updateDraftOutput(output.id, { fps_limit: toOptionalInt(event.target.value) })}
                                  />
                                </div>
                                <div className="field">
                                  <label className="label">{t("ext.streaming.outputs.bitrate", {}, "Bitrate (kbps)")}</label>
                                  <input
                                    className="input"
                                    value={output.bitrate_kbps === null || output.bitrate_kbps === undefined ? "" : String(output.bitrate_kbps)}
                                    onChange={(event) => updateDraftOutput(output.id, { bitrate_kbps: toOptionalInt(event.target.value) })}
                                  />
                                </div>
                                <div className="field">
                                  <label className="label">{t("ext.streaming.outputs.latency", {}, "Latência")}</label>
                                  <select
                                    className="input"
                                    value={output.latency_profile ?? "normal"}
                                    onChange={(event) =>
                                      updateDraftOutput(output.id, { latency_profile: event.target.value as "normal" | "low" | "ultra_low" })
                                    }
                                  >
                                    <option value="normal">
                                      {t("ext.streaming.outputs.latency_option.normal", {}, "Normal")}
                                    </option>
                                    <option value="low">{t("ext.streaming.outputs.latency_option.low", {}, "Low")}</option>
                                    <option value="ultra_low">
                                      {t("ext.streaming.outputs.latency_option.ultra_low", {}, "Ultra low")}
                                    </option>
                                  </select>
                                </div>
                                <div className="field">
                                  <label className="label">{t("ext.streaming.outputs.encoder", {}, "Encoder")}</label>
                                  <select
                                    className="input"
                                    value={output.encoder_mode ?? "inherit"}
                                    onChange={(event) =>
                                      updateDraftOutput(output.id, {
                                        encoder_mode: event.target.value as "inherit" | "auto" | "cpu",
                                      })
                                    }
                                  >
                                    <option value="inherit">{encoderModeLabel("inherit", t)}</option>
                                    <option value="auto">{encoderModeLabel("auto", t)}</option>
                                    <option value="cpu">{encoderModeLabel("cpu", t)}</option>
                                  </select>
                                </div>
                              </div>

                              <div className="sectionDivider" />

                              <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                                {t("ext.streaming.outputs.auth", {}, "Autenticação (opcional)")}
                              </div>

                              <div className="rowWrap" style={{ gap: 14 }}>
                                <label className="rowWrap" style={{ gap: 8 }}>
                                  <input
                                    type="checkbox"
                                    checked={Boolean(auth.enabled)}
                                    onChange={(event) => {
                                      const enabled = event.target.checked;
                                      updateDraftOutput(output.id, {
                                        authentication: { ...auth, enabled },
                                      });
                                    }}
                                  />
                                  <span className="cardMeta">{t("ext.streaming.outputs.auth_enabled", {}, "Habilitar user/senha")}</span>
                                </label>
                              </div>

                              {auth.enabled ? (
                                <div className="streamingFormGrid streamingFormGridOutputAuth" style={{ marginTop: 10 }}>
                                  <div className="field">
                                    <label className="label">{t("ext.streaming.outputs.username", {}, "Usuário")}</label>
                                    <input
                                      className="input"
                                      value={String(auth.username ?? "")}
                                      onChange={(event) =>
                                        updateDraftOutput(output.id, {
                                          authentication: { ...auth, username: event.target.value },
                                        })
                                      }
                                    />
                                  </div>
                                  <div className="field">
                                    <label className="label">{t("ext.streaming.outputs.password", {}, "Senha")}</label>
                                    <input
                                      className="input"
                                      type="password"
                                      value={String(auth.password ?? "")}
                                      onChange={(event) =>
                                        updateDraftOutput(output.id, {
                                          authentication: { ...auth, password: event.target.value },
                                        })
                                      }
                                    />
                                  </div>
                                  <div className="cardMeta streamingOutputAuthNote">
                                    {t("ext.streaming.outputs.auth_note", {}, "As credenciais são aplicadas no MediaMTX para leitura/playback deste output.")}
                                  </div>
                                </div>
                              ) : null}
                            </div>
                          </div>
                        );
                      })
                    : null}
                </div>
              </div>

              <div className="sectionDivider" />

              <div className="card">
                <div className="cardBody">
                  <div className="settingsDetailHeader" style={{ marginBottom: 10 }}>
                    <div>
                      <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                        {t("ext.streaming.transmissions.urls", {}, "URLs")}
                      </div>
                      <div className="cardMeta">
                        {t("ext.streaming.transmissions.urls_hint", {}, "As URLs são geradas pelo engine (MediaMTX).")}
                      </div>
                    </div>
                    <div className="rowWrap" style={{ gap: 8, justifyContent: "flex-end" }}>
                      <button
                        className="chipButton"
                        type="button"
                        disabled={urlsLoadingId === transmissionDraft.id}
                        onClick={() => void loadUrls(transmissionDraft.id)}
                      >
                        {urlsLoadingId === transmissionDraft.id
                          ? t("ext.streaming.transmissions.loading_urls", {}, "Carregando URLs…")
                          : t("ext.streaming.transmissions.load_urls", {}, "Carregar URLs")}
                      </button>
                      <button className="primaryButton" type="button" onClick={() => setWizardTransmission(transmissionDraft)}>
                        {t("ext.streaming.wizard.open", {}, "Criar fluxo com esta transmissão")}
                      </button>
                    </div>
                  </div>

                  {!engineStatus?.running ? (
                    <div className="cardMeta" style={{ marginBottom: 10 }}>
                      {t("ext.streaming.transmissions.engine_off_warning", {}, "A engine está parada: as URLs existirão, mas não haverá playback até iniciar.")}
                    </div>
                  ) : null}

                  {transmissionDraftDirty ? (
                    <div className="cardMeta" style={{ marginBottom: 10 }}>
                      {t("ext.streaming.transmissions.urls_save_warning", {}, "Salve alterações para garantir que paths/outputs estejam atualizados antes de compartilhar URLs.")}
                    </div>
                  ) : null}

                  {activeUrls && activeUrls.transmission_id === transmissionDraft.id ? (
                    <div>
                      {Array.isArray(activeUrls.blocking_errors) && activeUrls.blocking_errors.length > 0 ? (
                        <div className="errorText" style={{ marginTop: 10 }}>
                          {activeUrls.blocking_errors.join(" ")}
                        </div>
                      ) : null}
                      {activeUrls.network_contract?.status &&
                      activeUrls.network_contract.status !== "ok" &&
                      activeUrls.network_contract.status !== "not_applicable" ? (
                        <div className="cardMeta" style={{ marginTop: 10 }}>
                          {t(
                            "ext.streaming.transmissions.network_contract_status",
                            { status: activeUrls.network_contract.status },
                            `Network contract: ${activeUrls.network_contract.status}`,
                          )}
                        </div>
                      ) : null}
                      {activeUrls.outputs.length === 0 ? (
                        <div className="cardMeta" style={{ marginTop: 10 }}>
                          {t("ext.streaming.transmissions.urls_no_outputs", {}, "No playback URLs are available for this stream.")}
                        </div>
                      ) : null}
                      {activeUrls.outputs.map((item) => (
                        <div key={`${activeUrls.transmission_id}-${item.output_id}`} className="card" style={{ marginTop: 10 }}>
                          <div className="cardBody">
                            <div className="settingsListItemTitle" style={{ marginBottom: 6 }}>
                              {item.protocol.toUpperCase()}
                            </div>
	                            <div className="cardMeta">
	                              {t("ext.streaming.transmissions.engine_path", {}, "Engine path")}: {item.resolved_engine_path}
	                            </div>
	                            {item.protocol === "hls" ? (
	                              <div className="cardMeta" style={{ marginTop: 6 }}>
	                                {t("ext.streaming.transmissions.quality_profile", {}, "Quality profile")}:{" "}
	                                {qualityProfileLabel(item.quality_profile_id, qualityProfiles?.profiles ?? [], t)} ·{" "}
	                                {formatResolution(item.resolution)}
	                                {item.fps_limit ? ` / ${item.fps_limit} FPS` : ""}
	                                {item.bitrate_kbps ? ` / ${item.bitrate_kbps} kbps` : ""}
	                              </div>
	                            ) : null}
	                            {item.requires_auth ? (
                              <div className="cardMeta" style={{ marginTop: 6 }}>
                                {item.auth_username
                                  ? t(
                                      "ext.streaming.transmissions.auth_required_user",
                                      { username: item.auth_username },
                                      `Requires authentication (username: ${item.auth_username}).`,
                                    )
                                  : t("ext.streaming.transmissions.auth_required", {}, "Requires authentication.")}
                              </div>
                            ) : (
                              <div className="cardMeta" style={{ marginTop: 6 }}>
                                {t("ext.streaming.transmissions.auth_not_required", {}, "No authentication.")}
                              </div>
                            )}
                            <div className="cardMeta" style={{ marginTop: 6 }}>
                              {t("ext.streaming.transmissions.media_auth_type", {}, "Media auth")}:{" "}
                              {item.media_auth_type === "signed_url"
                                ? t("ext.streaming.transmissions.media_auth_signed", {}, "signed URL")
                                : item.media_auth_type === "basic"
                                  ? t("ext.streaming.transmissions.media_auth_basic", {}, "Basic auth")
                                  : t("ext.streaming.transmissions.media_auth_none", {}, "none")}
                              {item.url_expires_at_unix ? (
                                <>
                                  {" "}
                                  - {t("ext.streaming.transmissions.url_expires", {}, "expires")}:{" "}
                                  {formatRuntimeUnixTime(item.url_expires_at_unix)}
                                </>
                              ) : null}
                              {item.renew_after_unix ? (
                                <>
                                  {" "}
                                  - {t("ext.streaming.transmissions.url_renews", {}, "renews")}:{" "}
                                  {formatRuntimeUnixTime(item.renew_after_unix)}
                                </>
                              ) : null}
                            </div>
                            <div className="rowWrap" style={{ gap: 8, marginTop: 10 }}>
                              <input className="input" style={{ flex: 1 }} value={item.url} readOnly />
                              <button
                                className="iconButton"
                                type="button"
                                aria-label={t("ext.streaming.transmissions.copy_url", {}, "Copy URL")}
                                onClick={() => {
                                  void copyToClipboard(item.url).then(() => {
                                    setCopiedUrl(item.url);
                                    window.setTimeout(() => setCopiedUrl(null), 1200);
                                  });
                                }}
                              >
                                <i className="fa-solid fa-copy" aria-hidden="true" />
                              </button>
                            </div>
                            {copiedUrl === item.url ? (
                              <div className="streamingStatusOk" style={{ marginTop: 8 }}>
                                {t("ext.streaming.transmissions.copied", {}, "Copiado!")}
                              </div>
                            ) : null}
                          </div>
                        </div>
                      ))}
                      {Array.isArray(activeUrls.warnings) && activeUrls.warnings.length > 0 ? (
                        <div className="cardMeta" style={{ marginTop: 10 }}>
                          {activeUrls.warnings.join(" ")}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div className="cardMeta">{t("ext.streaming.transmissions.urls_empty", {}, "Carregue as URLs para visualizar aqui.")}</div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <SubModal
        open={createModalOpen}
        title={t("ext.streaming.transmissions.create", {}, "Criar transmissão")}
        closeAriaLabel={t("core.actions.close", {}, "Close")}
        onClose={() => {
          if (createBusy) return;
          setCreateModalOpen(false);
        }}
      >
        {createError ? <div className="errorText" style={{ marginBottom: 10 }}>{createError}</div> : null}

        <div className="streamingFormGrid streamingFormGridCreatePrimary">
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.name", {}, "Nome")}</label>
            <input className="input" value={newTransmissionName} onChange={(event) => setNewTransmissionName(event.target.value)} />
          </div>
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.path", {}, "Path/slug")}</label>
            <input
              className="input"
              value={newTransmissionPath}
              onChange={(event) => setNewTransmissionPath(event.target.value)}
              placeholder={slugifyPath(newTransmissionName) || "stream"}
            />
          </div>
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.host_server", {}, "Host server")}</label>
            <select
              className="input"
              value={normalizeServerId(newTransmissionHostServerId)}
              onChange={(event) => setNewTransmissionHostServerId(normalizeServerId(event.target.value))}
            >
              {processingServers.map((server) => {
                const serverId = normalizeServerId(server.id);
                const serverName = String(server.name || "").trim();
                const label =
                  serverId === "local"
                    ? t("ext.streaming.processing_servers.local_label", {}, "local (this machine)")
                    : serverName
                      ? `${serverId} (${serverName})`
                      : serverId;
                return (
                  <option key={serverId} value={serverId}>
                    {label}
                  </option>
                );
              })}
            </select>
          </div>
        </div>

        <div className="streamingFormGrid streamingFormGridCreateOutput" style={{ marginTop: 10 }}>
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.protocol", {}, "Saída")}</label>
            <select className="input" value={newOutputProtocol} onChange={(event) => setNewOutputProtocol(event.target.value as "hls" | "rtsp" | "webrtc")}>
              <option value="hls">HLS</option>
              <option value="rtsp">RTSP</option>
              <option value="webrtc">WebRTC</option>
            </select>
          </div>
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.width", {}, "Largura")}</label>
            <input className="input" value={newOutputWidth} onChange={(event) => setNewOutputWidth(event.target.value)} />
          </div>
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.height", {}, "Altura")}</label>
            <input className="input" value={newOutputHeight} onChange={(event) => setNewOutputHeight(event.target.value)} />
          </div>
          <div className="field">
            <label className="label">{t("ext.streaming.transmissions.fps", {}, "FPS")}</label>
            <input className="input" value={newOutputFps} onChange={(event) => setNewOutputFps(event.target.value)} />
          </div>
        </div>

        <div className="sectionDivider" style={{ marginTop: 14 }} />

        <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
          {t("ext.streaming.transmissions.camera_controls.title", {}, "Controles de câmera")}
        </div>

        <div className="rowWrap" style={{ gap: 10 }}>
          <label className="rowWrap" style={{ gap: 8 }}>
            <input
              type="checkbox"
              checked={newCameraControlsEnabled}
              onChange={(event) => setNewCameraControlsEnabled(event.target.checked)}
            />
            <span className="cardMeta">
              {t("ext.streaming.transmissions.camera_controls.enable", {}, "Habilitar controles de câmera")}
            </span>
          </label>
        </div>

        <div className="cardMeta" style={{ marginTop: 6 }}>
          {t(
            "ext.streaming.transmissions.camera_controls.hint",
            {},
            "Quando habilitado, esta transmissão poderá controlar uma câmera (presets/PTZ) via API.",
          )}
        </div>

        {newCameraControlsEnabled ? (
          <div style={{ marginTop: 10 }}>
            {availableCamerasLoading ? (
              <div className="settingsStatusMuted">
                {t("ext.streaming.transmissions.camera_controls.loading", {}, "Carregando câmeras…")}
              </div>
            ) : null}
            {availableCamerasError ? <div className="errorText">{availableCamerasError}</div> : null}

            <div className="streamingFormGrid streamingFormGridCreatePrimary" style={{ marginTop: 10 }}>
              <div className="field">
                <label className="label">{t("ext.streaming.transmissions.camera_controls.camera", {}, "Câmera")}</label>
                <select
                  className="input"
                  value={newCameraControlsCameraId}
                  onChange={(event) => {
                    const cid = String(event.target.value || "").trim();
                    const camera = availableCameras.find((item) => String(item.id || "").trim() === cid) ?? null;
                    setNewCameraControlsCameraId(cid);
                    setNewCameraControlsSourceId(defaultCameraSourceId(camera));
                  }}
                  disabled={availableCameras.length === 0}
                >
                  {availableCameras.map((camera) => {
                    const cid = String(camera.id || "").trim();
                    const label = String(camera.name || "").trim() || cid;
                    return (
                      <option key={cid} value={cid}>
                        {label}
                      </option>
                    );
                  })}
                </select>
                {availableCameras.length === 0 && !availableCamerasLoading ? (
                  <div className="cardMeta" style={{ marginTop: 6 }}>
                    {t(
                      "ext.streaming.transmissions.camera_controls.empty",
                      {},
                      "Nenhuma câmera encontrada. Cadastre uma câmera em Configurações > Câmeras.",
                    )}
                  </div>
                ) : null}
              </div>
              {(() => {
                const camera = availableCameras.find((item) => String(item.id || "").trim() === newCameraControlsCameraId.trim()) ?? null;
                const options = cameraSourceOptions(camera);
                if (options.length === 0) return null;
                return (
                  <div className="field">
                    <label className="label">{t("ext.streaming.transmissions.camera_controls.source", {}, "Fonte da câmera")}</label>
                    <select
                      className="input"
                      value={newCameraControlsSourceId || defaultCameraSourceId(camera)}
                      onChange={(event) => setNewCameraControlsSourceId(String(event.target.value || "").trim())}
                    >
                      {options.map((source) => (
                        <option key={source.id} value={source.id}>
                          {source.label}
                        </option>
                      ))}
                    </select>
                  </div>
                );
              })()}
            </div>
          </div>
        ) : null}

        <div className="rowWrap" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button className="primaryButton" type="button" disabled={createBusy} onClick={() => void createTransmissionAction()}>
            {createBusy ? t("ext.streaming.transmissions.creating", {}, "Criando…") : t("ext.streaming.transmissions.create_button", {}, "Criar transmissão")}
          </button>
        </div>
      </SubModal>

      <SubModal
        open={confirmDiscardOpen}
        title={t("ext.streaming.transmissions.discard_title", {}, "Descartar alterações?")}
        closeAriaLabel={t("core.actions.close", {}, "Close")}
        onClose={() => {
          setConfirmDiscardOpen(false);
          setPendingTransmissionId(null);
        }}
      >
        <div className="cardMeta" style={{ marginBottom: 10 }}>
          {t(
            "ext.streaming.transmissions.discard_body",
            {},
            "Você tem alterações não salvas. Para trocar de transmissão, descarte ou salve primeiro.",
          )}
        </div>
        <div className="rowWrap" style={{ justifyContent: "flex-end", gap: 10 }}>
          <button
            className="chipButton"
            type="button"
            onClick={() => {
              setConfirmDiscardOpen(false);
              setPendingTransmissionId(null);
            }}
          >
            {t("ext.streaming.common.cancel", {}, "Cancelar")}
          </button>
          <button
            className="chipButton"
            type="button"
            onClick={() => {
              if (!pendingTransmissionId) return;
              setTransmissionDraftDirty(false);
              setTransmissionDraftError(null);
              setConfirmDiscardOpen(false);
              setActiveTransmissionId(pendingTransmissionId);
              setPendingTransmissionId(null);
            }}
          >
            {t("ext.streaming.transmissions.discard", {}, "Descartar")}
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              setConfirmDiscardOpen(false);
              void saveDraftChanges().then(() => {
                if (!pendingTransmissionId) return;
                setActiveTransmissionId(pendingTransmissionId);
                setPendingTransmissionId(null);
              });
            }}
          >
            {t("ext.streaming.transmissions.save", {}, "Salvar")}
          </button>
        </div>
      </SubModal>

      <SubModal
        open={confirmDeleteOpen}
        title={t("ext.streaming.transmissions.delete_title", {}, "Excluir transmissão")}
        closeAriaLabel={t("core.actions.close", {}, "Close")}
        onClose={() => {
          if (deleteBusy) return;
          setConfirmDeleteOpen(false);
          setDeleteError(null);
        }}
      >
        {deleteError ? <div className="errorText" style={{ marginBottom: 10 }}>{deleteError}</div> : null}
        <div className="cardMeta" style={{ marginBottom: 10 }}>
          {t("ext.streaming.transmissions.delete_body", {}, "Esta ação é irreversível.")}
        </div>
        <div className="rowWrap" style={{ justifyContent: "flex-end", gap: 10 }}>
          <button className="chipButton" type="button" disabled={deleteBusy} onClick={() => setConfirmDeleteOpen(false)}>
            {t("ext.streaming.common.cancel", {}, "Cancelar")}
          </button>
          <button className="primaryButton" type="button" disabled={deleteBusy} onClick={() => void deleteActiveTransmission()}>
            {deleteBusy ? t("ext.streaming.transmissions.deleting", {}, "Excluindo…") : t("ext.streaming.transmissions.delete", {}, "Excluir")}
          </button>
        </div>
      </SubModal>

      <WizardCreatePipelineFromTransmission
        open={wizardTransmission !== null}
        i18n={i18n}
        transmission={wizardTransmission}
        engineRunning={Boolean(engineStatus?.running)}
        processingServers={processingServers}
        onClose={() => setWizardTransmission(null)}
        onCreated={() => {
          void fetchTransmissionsData();
        }}
      />
    </div>
  );
}
