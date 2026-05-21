import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n, SettingsPanel } from "@toposync/plugin-api";

import {
  discoverOnvifDevices,
  fetchCamerasIndex,
  fetchCameraSourceHealth,
  fetchOnvifStreamUri,
  fetchProcessingServers,
  fetchRtspSnapshot,
  inspectOnvif,
  probeCameraRtsp,
  probeRtsp,
} from "../api/camerasApi";
import { CAMERAS_EXTENSION_ID } from "../constants";
import { createUniqueId, parseCameras, serializeCameras } from "../parsing";
import type {
  CameraConfig,
  CameraIngestConfig,
  CameraOnvifConfig,
  ProcessingServer,
  CameraSourceHealthItem,
  CameraSourceHealthResponse,
  OnvifDiscoverResponse,
  OnvifDiscoveredDeviceInfo,
  OnvifInspectResponse,
  OnvifProfileInfo,
  RtspProbeResponse,
} from "../types";
import { SubModal } from "../ui/SubModal";
import { CameraPipelineWizardModal } from "../wizard/CameraPipelineWizardModal";

type TranslateFn = ReturnType<HostI18n["useI18n"]>["t"];

export function createCamerasSettingsPanel(): SettingsPanel {
  return {
    id: CAMERAS_EXTENSION_ID,
    icon: "video",
    name: { key: "ext.cameras.settings.name", fallback: "Cameras" },
    description: { key: "ext.cameras.settings.desc" },
    render: ({ i18n, settings, updateSettings }) => (
      <CamerasSettingsPanelContent i18n={i18n} settings={settings} updateSettings={updateSettings} />
    ),
  };
}

function normalizeQuery(value: string): string {
  return value.trim().toLowerCase();
}

function includesQuery(value: string, query: string): boolean {
  const normalized = normalizeQuery(value);
  if (!normalized) return false;
  return normalized.includes(query);
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function formatSeconds(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 60) return `${value.toFixed(1)} s`;
  return `${Math.round(value / 60)} min`;
}

function formatUnixTime(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "n/a";
  return new Date(value * 1000).toLocaleString();
}

function formatTimeRemainingUntil(value: number | null | undefined, t: TranslateFn): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "n/a";
  const remainingSeconds = Math.max(0, Math.ceil(value - Date.now() / 1000));
  if (remainingSeconds < 60) {
    return t("ext.cameras.settings.ingest.override.remaining_seconds", { seconds: remainingSeconds }, `${remainingSeconds}s`);
  }
  const remainingMinutes = Math.ceil(remainingSeconds / 60);
  if (remainingMinutes < 60) {
    return t("ext.cameras.settings.ingest.override.remaining_minutes", { minutes: remainingMinutes }, `${remainingMinutes} min`);
  }
  const remainingHours = Math.floor(remainingMinutes / 60);
  const minutes = remainingMinutes % 60;
  return t(
    "ext.cameras.settings.ingest.override.remaining_hours",
    { hours: remainingHours, minutes },
    minutes ? `${remainingHours}h ${minutes}min` : `${remainingHours}h`,
  );
}

function formatNumber(value: number | null | undefined, fractionDigits = 1): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  return value.toFixed(fractionDigits);
}

function codecScore(value: string | null | undefined): number {
  const encoding = String(value ?? "").trim().toUpperCase();
  if (encoding === "H264" || encoding === "H.264") return 3;
  if (encoding === "H265" || encoding === "H.265" || encoding === "HEVC") return 2;
  return encoding ? 1 : 0;
}

function pickBestOnvifProfile(profiles: OnvifProfileInfo[]): OnvifProfileInfo | null {
  let best: OnvifProfileInfo | null = null;
  let bestScore: [number, number, number, number, string] | null = null;
  for (const profile of profiles) {
    const score: [number, number, number, number, string] = [
      Math.max(0, Number(profile.width ?? 0)) * Math.max(0, Number(profile.height ?? 0)),
      Math.max(0, Number(profile.fps ?? 0)),
      codecScore(profile.encoding),
      profile.name?.trim() ? 1 : 0,
      String(profile.token ?? ""),
    ];
    if (!bestScore || compareProfileScore(score, bestScore) > 0) {
      best = profile;
      bestScore = score;
    }
  }
  return best;
}

function compareProfileScore(left: [number, number, number, number, string], right: [number, number, number, number, string]): number {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] > right[index]) return 1;
    if (left[index] < right[index]) return -1;
  }
  return 0;
}

function streamCredentialsForCamera(camera: CameraConfig): { username: string; password: string } {
  const streamUsername = camera.stream_username?.trim() ?? "";
  const streamPassword = camera.stream_password?.trim() ?? "";
  if (streamUsername || streamPassword) return { username: streamUsername, password: streamPassword };
  if (camera.connection_type === "onvif" && camera.stream_profile === "onvif") {
    return {
      username: camera.onvif?.username?.trim() ?? "",
      password: camera.onvif?.password?.trim() ?? "",
    };
  }
  return { username: camera.username?.trim() ?? "", password: camera.password?.trim() ?? "" };
}

function statusBadgeClass(status: string): string {
  if (status === "healthy") return "statusBadge statusBadgeSuccess";
  if (status === "starting" || status === "idle") return "statusBadge statusBadgeWarning";
  if (status === "stale" || status === "unreachable" || status === "unauthorized" || status === "error") {
    return "statusBadge statusBadgeDanger";
  }
  return "statusBadge";
}

function statusBadgeStyle(status: string): React.CSSProperties {
  const danger = status === "stale" || status === "unreachable" || status === "unauthorized" || status === "error";
  const warning = status === "starting" || status === "idle" || status === "timeout" || status === "probe_error";
  const color = status === "healthy" || status === "ok" ? "#19b56b" : danger ? "#ef4444" : warning ? "#f59e0b" : "#8a94a6";
  return {
    border: `1px solid ${color}`,
    borderRadius: 999,
    color,
    fontSize: 12,
    fontWeight: 700,
    lineHeight: "22px",
    minHeight: 24,
    padding: "0 10px",
    textTransform: "uppercase",
    whiteSpace: "nowrap",
  };
}

function sourceHealthStatusLabel(status: string | null | undefined, t: TranslateFn): string {
  const normalized = String(status || "unknown").trim().toLowerCase() || "unknown";
  return t(`ext.cameras.settings.source_health.status.${normalized}`, {}, normalized);
}

function normalizeServerId(value: string | null | undefined): string {
  return String(value || "local").trim().toLowerCase() || "local";
}

function serverDisplayName(serverId: string | null | undefined, servers: ProcessingServer[], t: TranslateFn): string {
  const normalized = normalizeServerId(serverId);
  if (normalized === "local") return t("ext.cameras.settings.ingest.host.local", {}, "Main environment");
  const server = servers.find((item) => normalizeServerId(item.id) === normalized);
  const name = String(server?.name || "").trim();
  return name ? `${name} (${normalized})` : normalized;
}

function normalizeIngestConfig(value: CameraIngestConfig | undefined): CameraIngestConfig {
  const mode = value?.mode === "runtime_local" || value?.mode === "direct" ? value.mode : "centralized";
  return {
    mode,
    host_server_id: mode === "centralized" ? normalizeServerId(value?.host_server_id) : "local",
    direct_override_until_unix:
      typeof value?.direct_override_until_unix === "number" && Number.isFinite(value.direct_override_until_unix)
        ? value.direct_override_until_unix
        : null,
  };
}

function ingestSelectValue(camera: CameraConfig): string {
  const ingest = normalizeIngestConfig(camera.ingest);
  if (ingest.mode === "centralized") return `centralized:${normalizeServerId(ingest.host_server_id)}`;
  return ingest.mode;
}

function ingestModeLabel(camera: CameraConfig, t: TranslateFn): string {
  const ingest = normalizeIngestConfig(camera.ingest);
  if (ingest.mode === "direct") return t("ext.cameras.settings.ingest.mode.direct", {}, "Direct");
  if (ingest.mode === "runtime_local") return t("ext.cameras.settings.ingest.mode.runtime_local", {}, "Per flow server");
  return t("ext.cameras.settings.ingest.mode.centralized", {}, "Centralized");
}

function ingestCentralizerLabel(camera: CameraConfig, servers: ProcessingServer[], t: TranslateFn): string {
  const ingest = normalizeIngestConfig(camera.ingest);
  if (ingest.mode === "direct") return t("ext.cameras.settings.ingest.centralizer.none", {}, "Not centralized");
  if (ingest.mode === "runtime_local") {
    return t("ext.cameras.settings.ingest.centralizer.flow_host", {}, "Server running the flow");
  }
  return serverDisplayName(ingest.host_server_id, servers, t);
}

function ingestReadPathLabel(camera: CameraConfig, t: TranslateFn): string {
  const ingest = normalizeIngestConfig(camera.ingest);
  if (ingest.mode === "direct" || directOverrideActive(ingest)) {
    return t("ext.cameras.settings.ingest.path.direct", {}, "Direct connection");
  }
  return t("ext.cameras.settings.ingest.path.ingest", {}, "RTSP from ingest");
}

function directOverrideStatusLabel(ingest: CameraIngestConfig | undefined, t: TranslateFn): string {
  const normalized = normalizeIngestConfig(ingest);
  if (directOverrideActive(normalized)) {
    return t(
      "ext.cameras.settings.ingest.override.active_until",
      {
        until: formatUnixTime(normalized.direct_override_until_unix),
        remaining: formatTimeRemainingUntil(normalized.direct_override_until_unix, t),
      },
      `Direct connection active until ${formatUnixTime(normalized.direct_override_until_unix)}`,
    );
  }
  return t("ext.cameras.settings.ingest.override.inactive", {}, "Inactive");
}

function sourceHealthIngestModeLabel(mode: string | null | undefined, t: TranslateFn): string {
  if (mode === "direct") return t("ext.cameras.settings.ingest.mode.direct", {}, "Direct");
  if (mode === "runtime_local") return t("ext.cameras.settings.ingest.mode.runtime_local", {}, "Per flow server");
  return t("ext.cameras.settings.ingest.mode.centralized", {}, "Centralized");
}

function sourceHealthCentralizerLabel(
  health: CameraSourceHealthItem | null,
  camera: CameraConfig,
  servers: ProcessingServer[],
  t: TranslateFn,
): string {
  const mode = health?.ingest_mode || normalizeIngestConfig(camera.ingest).mode;
  if (mode === "direct") return t("ext.cameras.settings.ingest.centralizer.none", {}, "Not centralized");
  if (mode === "runtime_local" && !health?.centralizer_server_id) {
    return t("ext.cameras.settings.ingest.centralizer.flow_host", {}, "Server running the flow");
  }
  const serverId = health?.centralizer_server_id || normalizeIngestConfig(camera.ingest).host_server_id;
  return serverDisplayName(serverId, servers, t);
}

function sourceHealthCurrentReadLabel(health: CameraSourceHealthItem, t: TranslateFn): string {
  return health.used_ingest
    ? t("ext.cameras.settings.ingest.path.ingest", {}, "RTSP from ingest")
    : t("ext.cameras.settings.ingest.path.direct", {}, "Direct connection");
}

function sourceHealthOverrideLabel(health: CameraSourceHealthItem, t: TranslateFn): string {
  return health.direct_override_active
    ? t("ext.cameras.settings.source_health.override.active", {}, "active")
    : t("ext.cameras.settings.source_health.override.none", {}, "inactive");
}

function directOverrideActive(ingest: CameraIngestConfig | undefined): boolean {
  const until = normalizeIngestConfig(ingest).direct_override_until_unix;
  return typeof until === "number" && until > Date.now() / 1000;
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
  return t(`ext.cameras.settings.source_health.action.${key}`, {}, raw);
}

function CamerasSettingsPanelContent({
  i18n,
  settings,
  updateSettings,
}: {
  i18n: HostI18n;
  settings: Record<string, unknown>;
  updateSettings: (patch: Record<string, unknown>) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();

  const cameras = useMemo(() => parseCameras(settings), [settings]);
  const camerasRef = React.useRef<CameraConfig[]>([]);

  useEffect(() => {
    camerasRef.current = cameras;
  }, [cameras]);

  const [cameraQuery, setCameraQuery] = useState("");

  const [activeCameraId, setActiveCameraId] = useState<string | null>(null);

  const [confirmDeleteCameraId, setConfirmDeleteCameraId] = useState<string | null>(null);

  const [snapshotModalOpen, setSnapshotModalOpen] = useState(false);
  const [snapshotTitle, setSnapshotTitle] = useState("");
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const snapshotAbortRef = React.useRef<AbortController | null>(null);

  const [sourceHealth, setSourceHealth] = useState<CameraSourceHealthResponse | null>(null);
  const [sourceHealthError, setSourceHealthError] = useState<string | null>(null);
  const [processingServers, setProcessingServers] = useState<ProcessingServer[]>([]);
  const [processingServersError, setProcessingServersError] = useState<string | null>(null);
  const [rtspProbeResult, setRtspProbeResult] = useState<RtspProbeResponse | null>(null);
  const [rtspProbeError, setRtspProbeError] = useState<string | null>(null);
  const [rtspProbeBusy, setRtspProbeBusy] = useState(false);
  const rtspProbeAbortRef = React.useRef<AbortController | null>(null);

  const [suggestionsLoading, setSuggestionsLoading] = useState(false);
  const [suggestionsErrorMessage, setSuggestionsErrorMessage] = useState<string | null>(null);
  const [suggestionsResult, setSuggestionsResult] = useState<OnvifDiscoverResponse | null>(null);
  const suggestionsAbortRef = React.useRef<AbortController | null>(null);
  const suggestionsAutoScanRef = React.useRef(false);

  const [onvifInspectResult, setOnvifInspectResult] = useState<OnvifInspectResponse | null>(null);
  const [onvifErrorMessage, setOnvifErrorMessage] = useState<string | null>(null);
  const [onvifLoading, setOnvifLoading] = useState(false);
  const [onvifStreamLoading, setOnvifStreamLoading] = useState(false);
  const onvifAbortRef = React.useRef<AbortController | null>(null);
  const onvifAutoDiscoverRef = React.useRef<Set<string>>(new Set());

  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  useEffect(() => {
    return () => {
      snapshotAbortRef.current?.abort();
      snapshotAbortRef.current = null;
      rtspProbeAbortRef.current?.abort();
      rtspProbeAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    return () => {
      suggestionsAbortRef.current?.abort();
      suggestionsAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    return () => {
      onvifAbortRef.current?.abort();
      onvifAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (activeCameraId && cameras.some((camera) => camera.id === activeCameraId)) return;
    setActiveCameraId(cameras[0]?.id ?? null);
  }, [activeCameraId, cameras]);

  useEffect(() => {
    setOnvifInspectResult(null);
    setOnvifErrorMessage(null);
    setOnvifLoading(false);
    setOnvifStreamLoading(false);
    onvifAbortRef.current?.abort();
    onvifAbortRef.current = null;
    setRtspProbeResult(null);
    setRtspProbeError(null);
    setRtspProbeBusy(false);
    rtspProbeAbortRef.current?.abort();
    rtspProbeAbortRef.current = null;
  }, [activeCameraId]);

  const filteredCameras = useMemo(() => {
    const q = normalizeQuery(cameraQuery);
    if (!q) return cameras;
    return cameras.filter((camera) => {
      const onvifXaddr = camera.onvif?.xaddr ?? "";
      return (
        includesQuery(camera.name || "", q) ||
        includesQuery(camera.id, q) ||
        includesQuery(camera.rtsp_url, q) ||
        includesQuery(onvifXaddr, q)
      );
    });
  }, [cameraQuery, cameras]);

  function hostForUrl(value: string): string {
    const raw = String(value ?? "").trim();
    if (!raw) return "";
    try {
      const parsed = new URL(raw);
      return parsed.hostname.trim().toLowerCase();
    } catch (_error) {
      return "";
    }
  }

  const suggestedDevices = useMemo(() => {
    const devices = suggestionsResult?.devices ?? [];

    const knownDeviceIds = new Set(
      cameras
        .map((camera) => camera.onvif?.device_id)
        .filter((value): value is string => typeof value === "string" && Boolean(value.trim()))
        .map((value) => value.trim()),
    );

    const knownHosts = new Set(
      cameras
        .flatMap((camera) => [camera.rtsp_url, camera.onvif?.xaddr ?? ""])
        .map((url) => hostForUrl(url))
        .filter(Boolean),
    );

    return devices.filter((device) => {
      const deviceId = String(device.device_id ?? "").trim();
      if (deviceId && knownDeviceIds.has(deviceId)) return false;
      const xaddr = String(device.xaddr ?? device.xaddrs?.[0] ?? "").trim();
      const host = hostForUrl(xaddr) || String(device.source_ip ?? "").trim().toLowerCase();
      if (host && knownHosts.has(host)) return false;
      return Boolean(xaddr || device.source_ip);
    });
  }, [cameras, suggestionsResult]);

  const suggestionsWarnings = suggestionsResult?.warnings ?? [];
  const suggestionsTargets = suggestionsResult?.targets ?? [];

  function closeSnapshotModal(): void {
    snapshotAbortRef.current?.abort();
    snapshotAbortRef.current = null;
    setSnapshotModalOpen(false);
    setSnapshotTitle("");
    setSnapshotErrorMessage(null);
    setSnapshotLoading(false);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
  }

  function openSnapshotModal(title: string): void {
    setSnapshotTitle(title);
    setSnapshotErrorMessage(null);
    setSnapshotLoading(false);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    setSnapshotModalOpen(true);
  }

  async function testCameraConnection(camera: CameraConfig): Promise<void> {
    snapshotAbortRef.current?.abort();
    const controller = new AbortController();
    snapshotAbortRef.current = controller;
    setSnapshotLoading(true);
    setSnapshotErrorMessage(null);
    try {
      const credentials = streamCredentialsForCamera(camera);
      const blob = await fetchRtspSnapshot(
        { url: camera.rtsp_url, username: credentials.username, password: credentials.password },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return url;
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    } finally {
      if (!controller.signal.aborted) setSnapshotLoading(false);
    }
  }

  async function runRtspProbe(camera: CameraConfig): Promise<void> {
    rtspProbeAbortRef.current?.abort();
    const controller = new AbortController();
    rtspProbeAbortRef.current = controller;
    setRtspProbeBusy(true);
    setRtspProbeError(null);
    setRtspProbeResult(null);
    try {
      const result = camera.id
        ? await probeCameraRtsp(camera.id, { timeout_ms: 5000 }, controller.signal)
        : await probeRtsp(
            {
              url: camera.rtsp_url,
              ...streamCredentialsForCamera(camera),
              timeout_ms: 5000,
            },
            controller.signal,
          );
      if (controller.signal.aborted) return;
      setRtspProbeResult(result);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setRtspProbeError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setRtspProbeBusy(false);
    }
  }

  function commitCameras(next: CameraConfig[]): void {
    camerasRef.current = next;
    updateSettings(serializeCameras(next));
  }

  function updateCamera(cameraId: string, patch: Partial<CameraConfig>): void {
    const next = camerasRef.current.map((camera) => (camera.id === cameraId ? { ...camera, ...patch } : camera));
    commitCameras(next);
  }

  function updateCameraOnvif(cameraId: string, patch: Partial<CameraOnvifConfig>): void {
    const current = camerasRef.current.find((camera) => camera.id === cameraId)?.onvif ?? null;
    const nextBase = current && typeof current === "object" ? current : { xaddr: "" };
    updateCamera(cameraId, { onvif: { ...nextBase, ...patch } });
  }

  function updateCameraIngest(cameraId: string, patch: Partial<CameraIngestConfig>): void {
    const current = normalizeIngestConfig(camerasRef.current.find((camera) => camera.id === cameraId)?.ingest);
    updateCamera(cameraId, { ingest: normalizeIngestConfig({ ...current, ...patch }) });
  }

  function applyIngestSelection(camera: CameraConfig, value: string): void {
    if (value === "runtime_local") {
      updateCameraIngest(camera.id, { mode: "runtime_local", host_server_id: "local" });
      return;
    }
    if (value === "direct") {
      updateCameraIngest(camera.id, { mode: "direct", host_server_id: "local", direct_override_until_unix: null });
      return;
    }
    const hostServerId = value.startsWith("centralized:") ? value.slice("centralized:".length) : "local";
    updateCameraIngest(camera.id, { mode: "centralized", host_server_id: normalizeServerId(hostServerId) });
  }

  function addCamera(): void {
    const id = createUniqueId();
    const next: CameraConfig = {
      id,
      name: "",
      connection_type: "onvif",
      channel_id: "video_main",
      stream_profile: "onvif",
      rtsp_url: "",
      stream_username: "",
      stream_password: "",
      ingest: { mode: "centralized", host_server_id: "local", direct_override_until_unix: null },
      fps: 5,
      onvif: { xaddr: "", username: "", password: "" },
    };
    commitCameras([next, ...camerasRef.current]);
    setActiveCameraId(id);
    setConfirmDeleteCameraId(null);
  }

  function addSuggestedCamera(device: OnvifDiscoveredDeviceInfo): void {
    const id = createUniqueId();
    const xaddrCandidate = (device.xaddr || device.xaddrs?.[0] || device.source_ip || "").trim();
    const name = String(device.name || device.hardware || device.source_ip || "").trim();
    const next: CameraConfig = {
      id,
      name,
      connection_type: "onvif",
      channel_id: "video_main",
      stream_profile: "onvif",
      rtsp_url: "",
      stream_username: "",
      stream_password: "",
      ingest: { mode: "centralized", host_server_id: "local", direct_override_until_unix: null },
      fps: 5,
      onvif: {
        xaddr: xaddrCandidate,
        username: "",
        password: "",
        device_id: String(device.device_id || "").trim() || undefined,
        hardware: String(device.hardware || "").trim() || undefined,
      },
    };
    commitCameras([next, ...camerasRef.current]);
    setActiveCameraId(id);
    setConfirmDeleteCameraId(null);
  }

  function deleteCamera(cameraId: string): void {
    commitCameras(camerasRef.current.filter((camera) => camera.id !== cameraId));
    setConfirmDeleteCameraId(null);
    if (activeCameraId === cameraId) setActiveCameraId(null);
  }

  async function scanOnvifSuggestions({ force }: { force?: boolean } = {}): Promise<void> {
    suggestionsAbortRef.current?.abort();
    const controller = new AbortController();
    suggestionsAbortRef.current = controller;
    setSuggestionsLoading(true);
    setSuggestionsErrorMessage(null);
    try {
      const result = await discoverOnvifDevices(
        {
          timeout_ms: 1600,
          force: Boolean(force),
          exclude_known: true,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setSuggestionsResult(result);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setSuggestionsErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setSuggestionsLoading(false);
    }
  }

  useEffect(() => {
    if (suggestionsAutoScanRef.current) return;
    suggestionsAutoScanRef.current = true;
    // Avoid eager WS-Discovery scans while settings are still loading. We first ask the backend
    // if the user has any cameras configured and only auto-scan on a truly empty setup.
    let cancelled = false;
    void (async () => {
      try {
        const index = await fetchCamerasIndex();
        if (cancelled) return;
        if ((index.cameras ?? []).length !== 0) return;
        await scanOnvifSuggestions({ force: false });
      } catch {
        // Ignore auto-scan errors; user can always click "Scan network".
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const activeCamera = activeCameraId ? cameras.find((camera) => camera.id === activeCameraId) ?? null : null;
  const activeSourceHealth = useMemo<CameraSourceHealthItem | null>(() => {
    if (!activeCamera) return null;
    const sources = sourceHealth?.sources ?? [];
    return sources.find((item) => item.camera_id === activeCamera.id) ?? null;
  }, [activeCamera, sourceHealth]);

  useEffect(() => {
    if (!activeCamera) return;
    if (activeCamera.connection_type !== "onvif") return;
    if (activeCamera.stream_profile === "custom") return;
    if (onvifLoading) return;

    const xaddr = activeCamera.onvif?.xaddr?.trim() ?? "";
    if (!xaddr) return;

    const signature = [
      activeCamera.id,
      xaddr,
      activeCamera.onvif?.username?.trim() ?? "",
      activeCamera.onvif?.password?.trim() ?? "",
    ].join("\n");
    if (onvifAutoDiscoverRef.current.has(signature)) return;
    onvifAutoDiscoverRef.current.add(signature);
    void discoverOnvifProfiles(activeCamera, { commitDiscovered: false });
  }, [
    activeCamera?.id,
    activeCamera?.connection_type,
    activeCamera?.stream_profile,
    activeCamera?.onvif?.xaddr,
    activeCamera?.onvif?.username,
    activeCamera?.onvif?.password,
    onvifLoading,
  ]);

  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;

    async function refresh(): Promise<void> {
      controller?.abort();
      controller = new AbortController();
      try {
        const result = await fetchCameraSourceHealth(controller.signal);
        if (cancelled) return;
        setSourceHealth(result);
        setSourceHealthError(null);
      } catch (error) {
        if (controller.signal.aborted) return;
        setSourceHealthError(error instanceof Error ? error.message : String(error));
      }
    }

    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      controller?.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const servers = await fetchProcessingServers(controller.signal);
        if (controller.signal.aborted) return;
        setProcessingServers(servers.filter((server) => normalizeServerId(server.id) !== "local"));
        setProcessingServersError(null);
      } catch (error) {
        if (controller.signal.aborted) return;
        setProcessingServers([]);
        setProcessingServersError(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => controller.abort();
  }, []);

  async function discoverOnvifProfiles(camera: CameraConfig, options?: { commitDiscovered?: boolean }): Promise<void> {
    const xaddr = camera.onvif?.xaddr?.trim() ?? "";
    if (!xaddr) return;
    const commitDiscovered = options?.commitDiscovered !== false;

    onvifAbortRef.current?.abort();
    const controller = new AbortController();
    onvifAbortRef.current = controller;

    setOnvifLoading(true);
    setOnvifStreamLoading(false);
    setOnvifErrorMessage(null);
    setOnvifInspectResult(null);

    try {
      const result = await inspectOnvif(
        {
          xaddr,
          username: camera.onvif?.username ?? "",
          password: camera.onvif?.password ?? "",
          timeout_ms: 3500,
          auth: "auto",
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setOnvifInspectResult(result);
      if (!commitDiscovered) return;
      if (result.xaddr && result.xaddr !== xaddr) {
        updateCameraOnvif(camera.id, { xaddr: result.xaddr });
      }
      if (typeof result.media_xaddr === "string" && result.media_xaddr.trim()) {
        updateCameraOnvif(camera.id, { media_xaddr: result.media_xaddr.trim() });
      }
      if (typeof result.ptz_xaddr === "string" && result.ptz_xaddr.trim()) {
        updateCameraOnvif(camera.id, { ptz_xaddr: result.ptz_xaddr.trim() });
      }
      const selectedProfile =
        result.profiles.find((profile) => profile.token === camera.onvif?.profile_token) ??
        pickBestOnvifProfile(result.profiles);
      if (selectedProfile) {
        const nextOnvif = {
          ...(camera.onvif ?? { xaddr }),
          xaddr: result.xaddr || xaddr,
          media_xaddr: result.media_xaddr?.trim() || camera.onvif?.media_xaddr,
          ptz_xaddr: result.ptz_xaddr?.trim() || camera.onvif?.ptz_xaddr,
          profile_token: selectedProfile.token,
          profile_name: selectedProfile.name?.trim() ?? "",
        };
        updateCamera(camera.id, { onvif: nextOnvif });
        if (camera.stream_profile !== "custom") {
          void applyOnvifProfile({ ...camera, onvif: nextOnvif }, selectedProfile, { abortPrevious: false });
        }
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setOnvifErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setOnvifLoading(false);
    }
  }

  async function applyOnvifProfile(
    camera: CameraConfig,
    profile: OnvifProfileInfo,
    options?: { abortPrevious?: boolean },
  ): Promise<void> {
    const xaddr = camera.onvif?.xaddr?.trim() ?? "";
    if (!xaddr) return;
    const token = String(profile.token ?? "").trim();
    if (!token) return;

    if (options?.abortPrevious !== false) onvifAbortRef.current?.abort();
    const controller = new AbortController();
    onvifAbortRef.current = controller;
    setOnvifStreamLoading(true);
    setOnvifErrorMessage(null);

    try {
      const result = await fetchOnvifStreamUri(
        {
          xaddr,
          media_xaddr: camera.onvif?.media_xaddr ?? onvifInspectResult?.media_xaddr ?? "",
          profile_token: token,
          username: camera.onvif?.username ?? "",
          password: camera.onvif?.password ?? "",
          timeout_ms: 4500,
          auth: "auto",
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (camera.stream_profile !== "custom") {
        updateCamera(camera.id, { rtsp_url: result.rtsp_url });
      }
      updateCameraOnvif(camera.id, { profile_token: token, profile_name: profile.name?.trim() ?? "" });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setOnvifErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setOnvifStreamLoading(false);
    }
  }

  return (
    <div>
      <div className="card">
        <div className="cardBody">
          <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
            <div>
              <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                {t("ext.cameras.settings.suggestions.title", {}, "Suggested cameras")}
              </div>
              <div className="cardMeta">{t("ext.cameras.settings.suggestions.desc")}</div>
            </div>

            <button
              className="chipButton"
              type="button"
              disabled={suggestionsLoading}
              onClick={() => void scanOnvifSuggestions({ force: true })}
            >
              {suggestionsLoading
                ? t("ext.cameras.settings.suggestions.scanning", {}, "Scanning…")
                : t("ext.cameras.settings.suggestions.scan", {}, "Scan network")}
            </button>
          </div>

          {suggestionsErrorMessage ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">{suggestionsErrorMessage}</div>
            </div>
          ) : null}

          {suggestionsWarnings.length > 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                {suggestionsWarnings.map((warning, index) => (
                  <div key={`${warning}-${index}`}>{warning}</div>
                ))}
              </div>
            </div>
          ) : null}

          {suggestionsResult && !suggestionsLoading && suggestedDevices.length === 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div>{t("ext.cameras.settings.suggestions.none", {}, "No new cameras found.")}</div>
                {suggestionsTargets.length > 0 ? (
                  <div className="cardMeta" style={{ marginTop: 6 }}>
                    {t("ext.cameras.settings.suggestions.targets", {}, "Discovery targets")}: {suggestionsTargets.join(", ")}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {suggestedDevices.length > 0 ? (
            <div className="settingsList" style={{ marginTop: 10 }}>
              {suggestedDevices.map((device) => {
                const xaddr = String(device.xaddr ?? device.xaddrs?.[0] ?? "").trim();
                const title =
                  String(device.name ?? "").trim() ||
                  String(device.hardware ?? "").trim() ||
                  String(device.source_ip ?? "").trim() ||
                  String(device.device_id ?? "").trim() ||
                  xaddr;
                const meta =
                  xaddr ||
                  String(device.source_ip ?? "").trim() ||
                  String(device.device_id ?? "").trim();
                return (
                  <div key={device.device_id || xaddr || String(device.source_ip ?? "") || title} className="choiceItem" style={{ cursor: "default" }}>
                    <div className="settingsListItemRow">
                      <div className="settingsListItemMain">
                        <div className="settingsListItemTitle" title={title}>
                          {title}
                        </div>
                        <div className="settingsListItemMeta" title={meta}>
                          {meta}
                        </div>
                      </div>
                      <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
                        <button
                          className="chipButton"
                          type="button"
                          onClick={() => addSuggestedCamera(device)}
                        >
                          {t("ext.cameras.settings.suggestions.add", {}, "Add")}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
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
              placeholder={t("ext.cameras.settings.search_cameras", {}, "Search cameras…")}
              value={cameraQuery}
              onChange={(event) => setCameraQuery(event.target.value)}
            />
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.cameras.settings.add_camera")}
              onClick={addCamera}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          {filteredCameras.length === 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>{t("ext.cameras.settings.empty_cameras")}</div>
                <button className="primaryButton" type="button" onClick={addCamera}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.cameras.settings.add_camera")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsList">
              {filteredCameras.map((camera) => {
                const selected = camera.id === activeCameraId;
                const name = camera.name.trim() || t("ext.cameras.settings.unnamed_camera", {}, "Untitled camera");
                const meta = camera.rtsp_url.trim() || t("ext.cameras.settings.missing_rtsp_url", {}, "Stream URL missing");
                return (
                  <button
                    key={camera.id}
                    type="button"
                    className={["choiceItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                    onClick={() => {
                      setActiveCameraId(camera.id);
                      setConfirmDeleteCameraId(null);
                    }}
                  >
                    <div className="settingsListItemRow">
                      <div className="settingsListItemMain">
                        <div className="settingsListItemTitle" title={name}>
                          {name}
                        </div>
                        <div className="settingsListItemMeta" title={meta}>
                          {meta}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="settingsSplitMain">
          {!activeCamera ? (
            <div className="card">
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>{t("ext.cameras.settings.select_camera", {}, "Select a camera to edit.")}</div>
                <button className="primaryButton" type="button" onClick={addCamera}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.cameras.settings.add_camera")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsDetail">
              <div className="settingsDetailHeader">
                <div>
                  <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                    {activeCamera.name.trim() || t("ext.cameras.settings.unnamed_camera", {}, "Untitled camera")}
                  </div>
                  <div className="cardMeta">ID: {activeCamera.id}</div>
                </div>

                <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                  <button className="chipButton" type="button" onClick={() => setWizardOpen(true)}>
                    {t("ext.cameras.wizard.open", {}, "Create pipeline")}
                  </button>

                  <button
                    className="chipButton"
                    type="button"
                    disabled={snapshotLoading || !activeCamera.rtsp_url.trim()}
                    onClick={() => {
                      openSnapshotModal(
                        activeCamera.name
                          ? `${t("ext.cameras.settings.snapshot")}: ${activeCamera.name}`
                          : t("ext.cameras.settings.snapshot"),
                      );
                      void testCameraConnection(activeCamera);
                    }}
                  >
                    {snapshotLoading ? t("ext.cameras.settings.testing") : t("ext.cameras.settings.test")}
                  </button>

                  <button
                    className={confirmDeleteCameraId === activeCamera.id ? "dangerButton" : "iconButton iconButtonDanger"}
                    type="button"
                    aria-label={t("core.actions.delete")}
                    title={t("core.actions.delete")}
                    onClick={() => {
                      if (confirmDeleteCameraId === activeCamera.id) {
                        deleteCamera(activeCamera.id);
                        return;
                      }
                      setConfirmDeleteCameraId(activeCamera.id);
                    }}
                  >
                    {confirmDeleteCameraId === activeCamera.id ? (
                      t("core.actions.delete")
                    ) : (
                      <i className="fa-solid fa-trash" aria-hidden="true" />
                    )}
                  </button>
                </div>
              </div>

              <div className="sectionDivider" />

              <div className="card">
                <div className="cardBody">
                  <div className="rowWrap" style={{ justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                    <div>
                      <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                        {t("ext.cameras.settings.source_health.title", {}, "Source Health")}
                      </div>
                      <div className="cardMeta">
                        {activeSourceHealth
                          ? t(
                              "ext.cameras.settings.source_health.meta",
                              {},
                              "Live camera-source status reported by running pipelines.",
                            )
                          : t(
                              "ext.cameras.settings.source_health.none",
                              {},
                              "No running camera.source has reported for this camera yet.",
                            )}
                      </div>
                    </div>
                    <div className="rowWrap" style={{ justifyContent: "flex-end", gap: 8 }}>
                      {activeSourceHealth ? (
                        <span className={statusBadgeClass(activeSourceHealth.status)} style={statusBadgeStyle(activeSourceHealth.status)}>
                          {sourceHealthStatusLabel(activeSourceHealth.status, t)}
                        </span>
                      ) : null}
                      <button
                        className="chipButton"
                        type="button"
                        disabled={rtspProbeBusy || !activeCamera.rtsp_url.trim()}
                        onClick={() => void runRtspProbe(activeCamera)}
                      >
                        {rtspProbeBusy
                          ? t("ext.cameras.settings.rtsp_probe.running", {}, "Testing RTSP…")
                          : t("ext.cameras.settings.rtsp_probe.action", {}, "Test RTSP")}
                      </button>
                    </div>
                  </div>

                  {sourceHealthError ? (
                    <div className="card" style={{ marginTop: 10 }}>
                      <div className="cardBody">{sourceHealthError}</div>
                    </div>
                  ) : null}

                  {activeSourceHealth?.ingest_blocking_errors?.length ? (
                    <div className="cardMeta" style={{ marginTop: 10, overflowWrap: "anywhere" }}>
                      <strong>{t("ext.cameras.settings.source_health.ingest_error", {}, "Ingest error")}:</strong>{" "}
                      {activeSourceHealth.ingest_blocking_errors.join(" ")}
                    </div>
                  ) : null}

                  {activeSourceHealth?.ingest_warnings?.length ? (
                    <div className="cardMeta" style={{ marginTop: 6, overflowWrap: "anywhere" }}>
                      {activeSourceHealth.ingest_warnings.join(" ")}
                    </div>
                  ) : null}

                  {activeSourceHealth ? (
                    <>
                      <div
                        className="settingsGrid"
                        style={{
                          display: "grid",
                          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                          gap: 12,
                          marginTop: 12,
                        }}
                      >
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.ingest_mode", {}, "Ingest mode")}</div>
                          <div>
                            {sourceHealthIngestModeLabel(
                              activeSourceHealth.ingest_mode || normalizeIngestConfig(activeCamera.ingest).mode,
                              t,
                            )}
                          </div>
                        </div>
                        <div>
                          <div className="label">
                            {t("ext.cameras.settings.source_health.centralizer", {}, "Centralizer")}
                          </div>
                          <div>{sourceHealthCentralizerLabel(activeSourceHealth, activeCamera, processingServers, t)}</div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.current_read", {}, "Current read")}</div>
                          <div>
                            {sourceHealthCurrentReadLabel(activeSourceHealth, t)}
                            {activeSourceHealth.ingest_path ? ` (${activeSourceHealth.ingest_path})` : ""}
                          </div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.override", {}, "Override")}</div>
                          <div>{sourceHealthOverrideLabel(activeSourceHealth, t)}</div>
                        </div>
                      </div>

                      {normalizeIngestConfig(activeCamera.ingest).mode === "direct" ? (
                        <div className="cardMeta" style={{ marginTop: 8 }}>
                          {t(
                            "ext.cameras.settings.source_health.direct_hint",
                            {},
                            "This camera may receive one connection per consuming flow.",
                          )}
                        </div>
                      ) : null}

                      <div
                        className="settingsGrid"
                        style={{
                          display: "grid",
                          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                          gap: 12,
                          marginTop: 12,
                        }}
                      >
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.age", {}, "Source age")}</div>
                          <div>{formatSeconds(activeSourceHealth.source_frame_age_seconds)}</div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.fps", {}, "Capture FPS")}</div>
                          <div>
                            {formatNumber(activeSourceHealth.capture_fps)} / {formatNumber(activeSourceHealth.target_fps)}
                          </div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.backend", {}, "Backend")}</div>
                          <div>{activeSourceHealth.backend || activeSourceHealth.configured_backend || "auto"}</div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.restarts", {}, "Restarts")}</div>
                          <div>{activeSourceHealth.restarts_total}</div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.source_health.last_frame", {}, "Last frame")}</div>
                          <div>{formatUnixTime(activeSourceHealth.last_frame_at_unix)}</div>
                        </div>
                      </div>
                    </>
                  ) : null}

                  {activeSourceHealth?.last_error ? (
                    <div className="cardMeta" style={{ marginTop: 10, overflowWrap: "anywhere" }}>
                      {t("ext.cameras.settings.source_health.last_error", {}, "Last error")}: {activeSourceHealth.last_error}
                    </div>
                  ) : null}

                  {activeSourceHealth?.recommended_action ? (
                    <div className="cardMeta" style={{ marginTop: 6 }}>
                      {sourceHealthRecommendedActionLabel(activeSourceHealth.recommended_action, t)}
                    </div>
                  ) : null}

                  {rtspProbeError ? (
                    <div className="card" style={{ marginTop: 10 }}>
                      <div className="cardBody" style={{ overflowWrap: "anywhere" }}>
                        {rtspProbeError}
                      </div>
                    </div>
                  ) : null}

                  {rtspProbeResult ? (
                    <div className="card" style={{ marginTop: 10 }}>
                      <div className="cardBody">
                        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
                          <span className={statusBadgeClass(rtspProbeResult.status)} style={statusBadgeStyle(rtspProbeResult.status)}>
                            {rtspProbeResult.status}
                          </span>
                          <span className="cardMeta">{rtspProbeResult.latency_ms} ms</span>
                        </div>
                        <div className="cardMeta" style={{ marginTop: 8, overflowWrap: "anywhere" }}>
                          {rtspProbeResult.url}
                        </div>
                        <div className="cardMeta" style={{ marginTop: 6 }}>
                          {t("ext.cameras.settings.rtsp_probe.transports", {}, "Transports")}:{" "}
                          {rtspProbeResult.transports_tested.join(", ") || "n/a"}
                        </div>
                        {rtspProbeResult.error ? (
                          <div className="cardMeta" style={{ marginTop: 6, overflowWrap: "anywhere" }}>
                            {rtspProbeResult.error}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="sectionDivider" />

              <div className="card">
                <div className="cardBody">
                  <div className="field">
                    <label className="label">{t("ext.cameras.settings.camera_name")}</label>
                    <input
                      className="input"
                      value={activeCamera.name}
                      onChange={(event) => updateCamera(activeCamera.id, { name: event.target.value })}
                    />
                  </div>

                  <div className="field">
                    <label className="label">{t("ext.cameras.settings.camera_type")}</label>
                    <select
                      className="input"
                      value={activeCamera.connection_type}
                      onChange={(event) => {
                        const next = event.target.value === "onvif" ? "onvif" : "rtsp";
                        const patch: Partial<CameraConfig> = {
                          connection_type: next,
                          stream_profile: next === "onvif" ? activeCamera.stream_profile || "onvif" : "custom",
                        };
                        if (next === "onvif" && !activeCamera.onvif) {
                          patch.onvif = { xaddr: "", username: "", password: "" };
                        }
                        updateCamera(activeCamera.id, patch);
                        setOnvifInspectResult(null);
                        setOnvifErrorMessage(null);
                      }}
                    >
                      <option value="onvif">{t("ext.cameras.settings.camera_type_onvif")}</option>
                      <option value="rtsp">{t("ext.cameras.settings.camera_type_rtsp")}</option>
                    </select>
                  </div>

                  <div className="field">
                    <label className="label">{t("ext.cameras.settings.ingest.title", {}, "Camera ingest")}</label>
                    <div className="cardMeta" style={{ marginBottom: 6 }}>
                      {t(
                        "ext.cameras.settings.ingest.help",
                        {},
                        "Centralizing reduces simultaneous connections to the camera.",
                      )}
                    </div>
                    <select
                      className="input"
                      value={ingestSelectValue(activeCamera)}
                      onChange={(event) => applyIngestSelection(activeCamera, event.target.value)}
                    >
                      <option value="centralized:local">
                        {t("ext.cameras.settings.ingest.option.main", {}, "Centralize in main environment")}
                      </option>
                      {processingServers.map((server) => {
                        const serverId = normalizeServerId(server.id);
                        return (
                          <option key={serverId} value={`centralized:${serverId}`}>
                            {t(
                              "ext.cameras.settings.ingest.option.processing",
                              { server: serverDisplayName(serverId, processingServers, t) },
                              `Centralize in processing server: ${serverDisplayName(serverId, processingServers, t)}`,
                            )}
                          </option>
                        );
                      })}
                      <option value="runtime_local">
                        {t("ext.cameras.settings.ingest.option.runtime_local", {}, "Centralize where the flow is running")}
                      </option>
                      <option value="direct">
                        {t("ext.cameras.settings.ingest.option.direct", {}, "Do not centralize with TopoSync")}
                      </option>
                    </select>
                    <div
                      style={{
                        border: "1px solid var(--color-border-subtle)",
                        borderRadius: 10,
                        marginTop: 10,
                        padding: 10,
                      }}
                    >
                      <div
                        className="settingsGrid"
                        style={{
                          display: "grid",
                          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                          gap: 10,
                        }}
                      >
                        <div>
                          <div className="label">{t("ext.cameras.settings.ingest.summary.mode", {}, "Mode")}</div>
                          <div>{ingestModeLabel(activeCamera, t)}</div>
                        </div>
                        <div>
                          <div className="label">
                            {t("ext.cameras.settings.ingest.summary.centralizer", {}, "Effective centralizer")}
                          </div>
                          <div>{ingestCentralizerLabel(activeCamera, processingServers, t)}</div>
                        </div>
                        <div>
                          <div className="label">{t("ext.cameras.settings.ingest.summary.path", {}, "Path")}</div>
                          <div>{ingestReadPathLabel(activeCamera, t)}</div>
                        </div>
                        <div>
                          <div className="label">
                            {t("ext.cameras.settings.ingest.summary.override", {}, "Temporary direct connection")}
                          </div>
                          <div>{directOverrideStatusLabel(activeCamera.ingest, t)}</div>
                        </div>
                      </div>
                      {normalizeIngestConfig(activeCamera.ingest).mode === "direct" ? (
                        <div className="cardMeta" style={{ marginTop: 8 }}>
                          {t(
                            "ext.cameras.settings.ingest.direct_hint",
                            {},
                            "This camera may receive one connection per consuming flow.",
                          )}
                        </div>
                      ) : null}
                    </div>
                    {processingServersError ? (
                      <div className="cardMeta" style={{ marginTop: 4 }}>
                        {processingServersError}
                      </div>
                    ) : null}
                    {normalizeIngestConfig(activeCamera.ingest).mode !== "direct" ? (
                      <div
                        style={{
                          borderTop: "1px solid var(--color-border-subtle)",
                          marginTop: 12,
                          paddingTop: 10,
                        }}
                      >
                        <div className="label">{t("ext.cameras.settings.ingest.diagnostics", {}, "Temporary diagnostic action")}</div>
                        <div className="cardMeta" style={{ marginBottom: 8 }}>
                          {directOverrideActive(activeCamera.ingest)
                            ? directOverrideStatusLabel(activeCamera.ingest, t)
                            : t(
                                "ext.cameras.settings.ingest.override.help",
                                {},
                                "Use only while diagnosing a centralizer issue.",
                              )}
                        </div>
                        <button
                          className="chipButton"
                          type="button"
                          onClick={() => {
                            const active = directOverrideActive(activeCamera.ingest);
                            updateCameraIngest(activeCamera.id, {
                              direct_override_until_unix: active ? null : Math.floor(Date.now() / 1000) + 3600,
                            });
                          }}
                        >
                          {directOverrideActive(activeCamera.ingest)
                            ? t("ext.cameras.settings.ingest.override.clear", {}, "End direct connection")
                            : t("ext.cameras.settings.ingest.override.enable", {}, "Use direct connection for 1h")}
                        </button>
                      </div>
                    ) : null}
                  </div>

                  {activeCamera.connection_type === "onvif" ? (
                    <>
                      <div className="field">
                        <label className="label">{t("ext.cameras.settings.onvif_xaddr")}</label>
                        <input
                          className="input"
                          value={activeCamera.onvif?.xaddr ?? ""}
                          onChange={(event) => {
                            updateCameraOnvif(activeCamera.id, { xaddr: event.target.value });
                            setOnvifInspectResult(null);
                            setOnvifErrorMessage(null);
                          }}
                          placeholder="192.168.0.10"
                        />
                        <div className="label">{t("ext.cameras.settings.onvif_xaddr_hint")}</div>
                      </div>

                      <div className="rowWrap" style={{ gap: 10 }}>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_username", {}, "ONVIF user")}</label>
                          <input
                            className="input"
                            value={activeCamera.onvif?.username ?? ""}
                            onChange={(event) => updateCameraOnvif(activeCamera.id, { username: event.target.value })}
                          />
                        </div>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_password", {}, "ONVIF password")}</label>
                          <input
                            className="input"
                            type="password"
                            value={activeCamera.onvif?.password ?? ""}
                            onChange={(event) => updateCameraOnvif(activeCamera.id, { password: event.target.value })}
                          />
                        </div>
                      </div>

                      <div className="rowWrap" style={{ gap: 10, alignItems: "center" }}>
                        <button
                          className="chipButton"
                          type="button"
                          disabled={onvifLoading || !(activeCamera.onvif?.xaddr ?? "").trim()}
                          onClick={() => void discoverOnvifProfiles(activeCamera)}
                        >
                          {onvifLoading
                            ? t("ext.cameras.settings.onvif_discovering")
                            : t("ext.cameras.settings.onvif_discover")}
                        </button>

                        {onvifStreamLoading ? (
                          <div className="cardMeta">{t("ext.cameras.settings.onvif_discovering")}</div>
                        ) : null}
                      </div>

                      {onvifErrorMessage ? (
                        <div className="card" style={{ marginTop: 10 }}>
                          <div className="cardBody">{onvifErrorMessage}</div>
                        </div>
                      ) : null}

                      {onvifInspectResult?.warnings?.length ? (
                        <div className="card" style={{ marginTop: 10 }}>
                          <div className="cardBody">
                            {onvifInspectResult.warnings.map((warning, index) => (
                              <div key={index}>{warning}</div>
                            ))}
                          </div>
                        </div>
                      ) : null}

                      <div className="field" style={{ marginTop: 12 }}>
                        <label className="label">{t("ext.cameras.settings.stream_source", {}, "Video stream")}</label>
                        <select
                          className="input"
                          value={activeCamera.stream_profile}
                          onChange={(event) => {
                            const streamProfile = event.target.value === "custom" ? "custom" : "onvif";
                            updateCamera(activeCamera.id, { stream_profile: streamProfile });
                            if (streamProfile === "onvif") {
                              const profile =
                                (onvifInspectResult?.profiles ?? []).find(
                                  (item) => item.token === activeCamera.onvif?.profile_token,
                                ) ?? null;
                              if (profile) {
                                void applyOnvifProfile({ ...activeCamera, stream_profile: "onvif" }, profile);
                              }
                            }
                          }}
                        >
                          <option value="onvif">{t("ext.cameras.settings.stream_source_onvif", {}, "ONVIF profile")}</option>
                          <option value="custom">{t("ext.cameras.settings.stream_source_custom", {}, "Custom RTSP")}</option>
                        </select>
                      </div>

                      {activeCamera.stream_profile !== "custom" && onvifInspectResult?.profiles?.length ? (
                        <div className="field" style={{ marginTop: 12 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_profile")}</label>
                          <select
                            className="input"
                            value={activeCamera.onvif?.profile_token ?? ""}
                            onChange={(event) => {
                              const token = event.target.value;
                              const profile =
                                (onvifInspectResult?.profiles ?? []).find((item) => item.token === token) ?? null;
                              if (!profile) return;
                              updateCameraOnvif(activeCamera.id, {
                                profile_token: profile.token,
                                profile_name: profile.name?.trim() ?? "",
                              });
                              if (activeCamera.stream_profile !== "custom") {
                                void applyOnvifProfile(activeCamera, profile);
                              }
                            }}
                          >
                            <option value="">{t("ext.cameras.editor.select_placeholder", {}, "Select…")}</option>
                            {(onvifInspectResult?.profiles ?? []).map((profile) => {
                              const parts = [];
                              if (profile.name) parts.push(profile.name);
                              if (profile.width && profile.height) parts.push(`${profile.width}×${profile.height}`);
                              if (profile.encoding) parts.push(profile.encoding);
                              const label = parts.join(" • ") || profile.token;
                              return (
                                <option key={profile.token} value={profile.token}>
                                  {label}
                                </option>
                              );
                            })}
                          </select>
                          <div className="label">{t("ext.cameras.settings.onvif_profile_hint")}</div>
                        </div>
                      ) : null}

                      <div className="field">
                        <label className="label">
                          {activeCamera.stream_profile === "custom"
                            ? t("ext.cameras.settings.custom_rtsp_url", {}, "Custom RTSP URL")
                            : t("ext.cameras.settings.onvif_rtsp_from_onvif")}
                        </label>
                        <input
                          className="input"
                          value={activeCamera.rtsp_url}
                          onChange={(event) => updateCamera(activeCamera.id, { rtsp_url: event.target.value })}
                          placeholder="rtsp://..."
                        />
                      </div>

                      {activeCamera.stream_profile === "custom" ? (
                        <div className="rowWrap" style={{ gap: 10 }}>
                          <div className="field" style={{ flex: 1, minWidth: 220 }}>
                            <label className="label">{t("ext.cameras.settings.stream_username", {}, "Stream user")}</label>
                            <input
                              className="input"
                              value={activeCamera.stream_username ?? ""}
                              onChange={(event) => updateCamera(activeCamera.id, { stream_username: event.target.value })}
                            />
                          </div>
                          <div className="field" style={{ flex: 1, minWidth: 220 }}>
                            <label className="label">{t("ext.cameras.settings.stream_password", {}, "Stream password")}</label>
                            <input
                              className="input"
                              type="password"
                              value={activeCamera.stream_password ?? ""}
                              onChange={(event) => updateCamera(activeCamera.id, { stream_password: event.target.value })}
                            />
                          </div>
                        </div>
                      ) : null}
                    </>
                  ) : (
                    <>
                      <div className="field">
                        <label className="label">{t("ext.cameras.settings.camera_url")}</label>
                        <input
                          className="input"
                          value={activeCamera.rtsp_url}
                          onChange={(event) => updateCamera(activeCamera.id, { rtsp_url: event.target.value })}
                          placeholder="rtsp://..."
                        />
                      </div>

                      <div className="rowWrap" style={{ gap: 10 }}>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.username")}</label>
                          <input
                            className="input"
                            value={activeCamera.stream_username ?? ""}
                            onChange={(event) => updateCamera(activeCamera.id, { stream_username: event.target.value })}
                          />
                        </div>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.password")}</label>
                          <input
                            className="input"
                            type="password"
                            value={activeCamera.stream_password ?? ""}
                            onChange={(event) => updateCamera(activeCamera.id, { stream_password: event.target.value })}
                          />
                        </div>
                      </div>
                    </>
                  )}

                  <div className="rowWrap" style={{ gap: 10 }}>
                    <div className="field" style={{ flex: 1, minWidth: 220 }}>
                      <label className="label">{t("ext.cameras.settings.camera_fps")}</label>
                      <input
                        className="input"
                        type="number"
                        min={1}
                        max={60}
                        step={1}
                        value={Number.isFinite(activeCamera.fps) ? activeCamera.fps : 5}
                        onChange={(event) => {
                          const parsed = event.target.value ? Number(event.target.value) : NaN;
                          const nextFps = Number.isFinite(parsed) ? clamp(parsed, 1, 60) : 5;
                          updateCamera(activeCamera.id, { fps: nextFps });
                        }}
                      />
                      <div className="label">{t("ext.cameras.settings.camera_fps_hint")}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <SubModal open={snapshotModalOpen} title={snapshotTitle || t("ext.cameras.settings.snapshot")} onClose={closeSnapshotModal}>
        {snapshotErrorMessage ? (
          <div className="card">
            <div className="cardBody">{snapshotErrorMessage}</div>
          </div>
        ) : snapshotUrl ? (
          <img
            src={snapshotUrl}
            alt={snapshotTitle}
            style={{
              width: "100%",
              borderRadius: 14,
              border: "1px solid rgba(255,255,255,0.14)",
              background: "rgba(0,0,0,0.35)",
            }}
          />
        ) : (
          <div className="card">
            <div className="cardBody">{snapshotLoading ? t("ext.cameras.settings.snapshot_loading") : t("ext.cameras.settings.snapshot")}</div>
          </div>
        )}
      </SubModal>

      {activeCamera ? (
        <CameraPipelineWizardModal open={wizardOpen} camera={activeCamera} i18n={i18n} onClose={() => setWizardOpen(false)} />
      ) : null}
    </div>
  );
}
