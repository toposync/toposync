import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { HostI18n, SettingsPanel } from "@toposync/plugin-api";

import {
  discoverOnvifDevices,
  fetchCameraContexts,
  fetchCameraPipelines,
  fetchCamerasIndex,
  fetchCameraSnapshot,
  fetchCameraSourceHealth,
  fetchProcessingServerStatus,
  fetchProcessingServers,
  fetchStreamPublications,
  installProcessingServerVisionModel,
  inspectOnvif,
  probeCameraRtsp,
  reconcileStreamPublications,
  updateCameraSourcePublication,
} from "../api/camerasApi";
import { CAMERAS_EXTENSION_ID } from "../constants";
import {
  createDefaultCameraSource,
  createUniqueId,
  parseCameras,
  readCameraIngestConfig,
  serializeCameras,
} from "../parsing";
import type {
  CameraConfig,
  CameraContextsResponse,
  CameraControlType,
  CameraIngestConfig,
  CameraOnvifConfig,
  CameraPipelinePreset,
  CameraPipelinesResponse,
  CameraSourceConfig,
  CameraSourceHealthItem,
  CameraSourceHealthResponse,
  OnvifDiscoveredDeviceInfo,
  OnvifDiscoverResponse,
  OnvifInspectResponse,
  OnvifProfileInfo,
  ProcessingServer,
  RtspProbeResponse,
  StreamPublication,
} from "../types";
import { CameraPipelinePresetModal } from "./CameraPipelinePresetModal";
import { VisionModelConsentModal } from "./VisionModelConsentModal";
import {
  DEFAULT_DETECTION_MODEL_ID,
  DEFAULT_DETECTION_MODEL_NAME,
  canPrepareDetectionModel,
  findDetectionModel,
  isActiveDetectionModelInstall,
  isDetectionModelReady,
  readDetectionModelCatalog,
  type DetectionModelCatalogItem,
} from "./visionModelCatalog";
import { SubModal } from "../ui/SubModal";

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

function normalizeServerId(value: string | null | undefined): string {
  return String(value || "local").trim().toLowerCase() || "local";
}

function normalizeIngestConfig(value: CameraIngestConfig | undefined): CameraIngestConfig {
  return readCameraIngestConfig(value);
}

function serverDisplayName(serverId: string | null | undefined, servers: ProcessingServer[], t: TranslateFn): string {
  const normalized = normalizeServerId(serverId);
  if (normalized === "local") return t("ext.cameras.settings.ingest.host.local", {}, "Ambiente principal");
  const server = servers.find((item) => normalizeServerId(item.id) === normalized);
  const name = String(server?.name || "").trim();
  return name ? `${name} (${normalized})` : normalized;
}

function modelSetupReasonLabel(reason: string, t: TranslateFn): string {
  const clean = String(reason || "").trim().toLowerCase();
  if (!clean) return t("ext.cameras.pipeline_preset.model.reason.unsupported", {}, "automatic preparation is unavailable");
  return t(`ext.cameras.pipeline_preset.model.reason.${clean}`, {}, clean.replace(/_/g, " "));
}

function modelSetupProgressLabel(item: DetectionModelCatalogItem, t: TranslateFn): string {
  const job = item.installJob;
  if (!job) return "";
  const phase = job.phase || job.status;
  if (job.progressPct === null) return phase;
  const pct = Math.max(0, Math.min(100, job.progressPct));
  return t("ext.cameras.pipeline_preset.model.progress", { phase, pct: pct.toFixed(0) }, "{{phase}} - {{pct}}%");
}

function useDelayedStatusText(active: boolean, initialText: string, delayedText: string, delayMs: number): string {
  const [delayed, setDelayed] = useState(false);

  useEffect(() => {
    if (!active) {
      setDelayed(false);
      return undefined;
    }
    setDelayed(false);
    const timer = window.setTimeout(() => setDelayed(true), Math.max(0, delayMs));
    return () => window.clearTimeout(timer);
  }, [active, delayMs]);

  return active && delayed ? delayedText : initialText;
}

function ingestSelectValue(source: CameraSourceConfig): string {
  const ingest = normalizeIngestConfig(source.ingest);
  if (ingest.mode === "centralized") return `centralized:${normalizeServerId(ingest.host_server_id)}`;
  return ingest.mode;
}

function ingestModeLabel(source: CameraSourceConfig, t: TranslateFn): string {
  const mode = normalizeIngestConfig(source.ingest).mode;
  if (mode === "direct") return t("ext.cameras.settings.ingest.mode.direct", {}, "Direto");
  if (mode === "runtime_local") return t("ext.cameras.settings.ingest.mode.runtime_local", {}, "Por servidor");
  return t("ext.cameras.settings.ingest.mode.centralized", {}, "Centralizado");
}

function ingestCentralizerLabel(source: CameraSourceConfig, servers: ProcessingServer[], t: TranslateFn): string {
  const ingest = normalizeIngestConfig(source.ingest);
  if (ingest.mode === "direct") return t("ext.cameras.settings.ingest.centralizer.none", {}, "Não centralizado");
  if (ingest.mode === "runtime_local") return t("ext.cameras.settings.ingest.centralizer.flow_host", {}, "Servidor do fluxo");
  return serverDisplayName(ingest.host_server_id, servers, t);
}

function ingestPathLabel(source: CameraSourceConfig, t: TranslateFn): string {
  const ingest = normalizeIngestConfig(source.ingest);
  if (ingest.mode === "direct") {
    return t("ext.cameras.settings.ingest.path.direct", {}, "Direta");
  }
  return t("ext.cameras.settings.ingest.path.ingest", {}, "RTSP do ingest");
}

function sourceRoleLabel(role: string, t: TranslateFn): string {
  if (role === "main") return t("ext.cameras.settings.sources.role.main", {}, "Principal");
  if (role === "sub") return t("ext.cameras.settings.sources.role.sub", {}, "Baixa resolução");
  if (role === "zoom") return t("ext.cameras.settings.sources.role.zoom", {}, "Zoom");
  return t("ext.cameras.settings.sources.role.custom", {}, "Personalizada");
}

function sourceOriginLabel(source: CameraSourceConfig, t: TranslateFn): string {
  if (source.origin.type === "onvif_profile") {
    return source.origin.profile_name?.trim()
      ? `ONVIF: ${source.origin.profile_name}`
      : t("ext.cameras.settings.sources.origin.onvif", {}, "Perfil ONVIF");
  }
  return t("ext.cameras.settings.sources.origin.rtsp", {}, "RTSP manual");
}

function sourceResolutionLabel(source: CameraSourceConfig): string {
  const width = source.video.width;
  const height = source.video.height;
  const fps = source.video.fps;
  const codec = source.video.codec?.trim();
  const parts: string[] = [];
  if (typeof width === "number" && typeof height === "number") parts.push(`${width}x${height}`);
  if (typeof fps === "number") parts.push(`${fps} fps`);
  if (codec) parts.push(codec);
  return parts.join(" / ") || "n/a";
}

const CAMERA_PIPELINE_PRESET_CARDS: {
  id: CameraPipelinePreset;
  requiresMapping: boolean;
  titleKey: string;
  titleFallback: string;
  descriptionKey: string;
  descriptionFallback: string;
  stepsKey: string;
  stepsFallback: string;
}[] = [
  {
    id: "people_individual",
    requiresMapping: true,
    titleKey: "ext.cameras.pipeline_preset.people_individual.title",
    titleFallback: "Eventos individuais de pessoas",
    descriptionKey: "ext.cameras.pipeline_preset.people_individual.card_desc",
    descriptionFallback: "Mapeia pessoas e mantém uma notificação por evento individual.",
    stepsKey: "ext.cameras.pipeline_preset.people_individual.steps",
    stepsFallback: "Movimento -> pessoas -> mapeamento -> evento individual",
  },
  {
    id: "people_quiet",
    requiresMapping: true,
    titleKey: "ext.cameras.pipeline_preset.people_quiet.title",
    titleFallback: "Presença agrupada",
    descriptionKey: "ext.cameras.pipeline_preset.people_quiet.card_desc",
    descriptionFallback: "Agrupa pessoas e pets mapeados em uma ocorrência residencial.",
    stepsKey: "ext.cameras.pipeline_preset.people_quiet.steps",
    stepsFallback: "Movimento -> pessoas/pets -> mapeamento -> grupo",
  },
  {
    id: "presence_area",
    requiresMapping: true,
    titleKey: "ext.cameras.pipeline_preset.presence_area.title",
    titleFallback: "Presença por área",
    descriptionKey: "ext.cameras.pipeline_preset.presence_area.card_desc",
    descriptionFallback: "Agrupa presença usando o mapeamento da câmera para separar ocorrências próximas.",
    stepsKey: "ext.cameras.pipeline_preset.presence_area.steps",
    stepsFallback: "Movimento -> pessoas/pets -> mapeamento -> grupo",
  },
  {
    id: "vehicle_stopped",
    requiresMapping: true,
    titleKey: "ext.cameras.pipeline_preset.vehicle_stopped.title",
    titleFallback: "Veículo parou",
    descriptionKey: "ext.cameras.pipeline_preset.vehicle_stopped.card_desc",
    descriptionFallback: "Detecta veículos, estima velocidade e notifica quando o veículo para.",
    stepsKey: "ext.cameras.pipeline_preset.vehicle_stopped.steps",
    stepsFallback: "Movimento -> veículos -> velocidade -> imagem -> notificação",
  },
  {
    id: "people_simple",
    requiresMapping: false,
    titleKey: "ext.cameras.pipeline_preset.people_simple.title",
    titleFallback: "Detecção simples de pessoas sem mapeamento",
    descriptionKey: "ext.cameras.pipeline_preset.people_simple.card_desc",
    descriptionFallback: "Detecta pessoas e acompanha eventos sem exigir mapeamento.",
    stepsKey: "ext.cameras.pipeline_preset.people_simple.steps",
    stepsFallback: "Movimento -> pessoas -> acompanhamento -> notificação",
  },
];

function sourceHealthFor(
  health: CameraSourceHealthResponse | null,
  cameraId: string,
  sourceId: string,
): CameraSourceHealthItem | null {
  return (
    health?.sources?.find(
      (item) => String(item.camera_id || "").trim() === cameraId && String(item.camera_source_id || "").trim() === sourceId,
    ) ?? null
  );
}

function navigateInApp(pathname: string): void {
  if (typeof window === "undefined") return;
  const next = String(pathname || "/");
  if (window.location.pathname !== next) window.history.pushState(null, "", next);
  try {
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch {
    window.dispatchEvent(new Event("popstate"));
  }
}

function openSettingsPanel(panelId: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("toposync:open-settings-panel", { detail: { panelId } }));
}

function slugSourceId(value: string, fallback: string): string {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80);
  return slug || fallback;
}

function suggestedCameraName(device: OnvifDiscoveredDeviceInfo, t: TranslateFn): string {
  const name = String(device.name || "").trim();
  if (name) return name;
  const hardware = String(device.hardware || "").trim();
  if (hardware) return hardware;
  const sourceIp = String(device.source_ip || "").trim();
  if (sourceIp) return `ONVIF ${sourceIp}`;
  return t("ext.cameras.settings.camera_type_onvif", {}, "ONVIF");
}

function suggestedCameraXaddr(device: OnvifDiscoveredDeviceInfo): string {
  return (
    String(device.xaddr || "").trim() ||
    String(device.xaddrs?.[0] || "").trim() ||
    (String(device.source_ip || "").trim() ? `http://${String(device.source_ip || "").trim()}/onvif/device_service` : "")
  );
}

function roleFromProfile(profile: OnvifProfileInfo, index: number): CameraSourceConfig["role"] {
  const text = `${profile.name || ""} ${profile.token || ""}`.toLowerCase();
  if (text.includes("zoom")) return "zoom";
  if (text.includes("sub") || text.includes("low") || text.includes("minor")) return "sub";
  return index === 0 ? "main" : "custom";
}

function sourceFromOnvifProfile(profile: OnvifProfileInfo, index: number): CameraSourceConfig {
  const role = roleFromProfile(profile, index);
  const id = slugSourceId(profile.token || profile.name || `source_${index + 1}`, `source_${index + 1}`);
  return createDefaultCameraSource(index, {
    id,
    name: profile.name?.trim() || (role === "sub" ? "Baixa resolução" : role === "zoom" ? "Zoom" : "Principal"),
    is_default: index === 0,
    role,
    view_id: role === "zoom" ? "zoom" : "main",
    origin: {
      type: "onvif_profile",
      rtsp_url: String(profile.stream_uri || "").trim(),
      profile_token: profile.token,
      profile_name: profile.name,
      has_ptz: Boolean(profile.has_ptz),
    },
    video: {
      width: profile.width ?? null,
      height: profile.height ?? null,
      fps: profile.fps ?? null,
      codec: profile.encoding || null,
    },
  });
}

function mergeOnvifSources(existing: CameraSourceConfig[], profiles: OnvifProfileInfo[]): CameraSourceConfig[] {
  const discovered = profiles.map((profile, index) => sourceFromOnvifProfile(profile, index));
  const byToken = new Map(
    discovered.map((source) => [String(source.origin.profile_token || "").trim(), source] as const).filter(([token]) => token),
  );
  const usedTokens = new Set<string>();
  const merged = existing.map((source) => {
    const token = String(source.origin.profile_token || "").trim();
    const next = token ? byToken.get(token) : null;
    if (!next) return source;
    usedTokens.add(token);
    return {
      ...source,
      name: source.name || next.name,
      role: next.role,
      view_id: source.view_id || next.view_id,
      origin: {
        ...source.origin,
        ...next.origin,
        rtsp_url: String(next.origin.rtsp_url || source.origin.rtsp_url || "").trim(),
      },
      video: { ...source.video, ...next.video },
    };
  });
  for (const source of discovered) {
    const token = String(source.origin.profile_token || "").trim();
    if (!token || !usedTokens.has(token)) merged.push({ ...source, is_default: merged.length === 0 });
  }
  const hasDefault = merged.some((source) => source.enabled && source.kind === "video" && source.is_default);
  return merged.map((source, index) => ({ ...source, is_default: hasDefault ? source.is_default : index === 0 }));
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
  const [activeCameraId, setActiveCameraId] = useState<string>("");
  const [activeSourceByCamera, setActiveSourceByCamera] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [sourceHealth, setSourceHealth] = useState<CameraSourceHealthResponse | null>(null);
  const [savedCameraIds, setSavedCameraIds] = useState<Set<string> | null>(null);
  const [cameraContexts, setCameraContexts] = useState<CameraContextsResponse | null>(null);
  const [cameraContextsLoading, setCameraContextsLoading] = useState(false);
  const [cameraContextsError, setCameraContextsError] = useState<string | null>(null);
  const [cameraPipelines, setCameraPipelines] = useState<CameraPipelinesResponse | null>(null);
  const [cameraPipelinesLoading, setCameraPipelinesLoading] = useState(false);
  const [cameraPipelinesError, setCameraPipelinesError] = useState<string | null>(null);
  const [pipelinePresetOpen, setPipelinePresetOpen] = useState<CameraPipelinePreset | null>(null);
  const [pipelineNotice, setPipelineNotice] = useState<string | null>(null);
  const [presetModelStatusPayload, setPresetModelStatusPayload] = useState<unknown>(null);
  const [presetModelStatusLoading, setPresetModelStatusLoading] = useState(false);
  const [presetModelStatusError, setPresetModelStatusError] = useState<string | null>(null);
  const [presetModelConsentOpen, setPresetModelConsentOpen] = useState(false);
  const [presetModelConsentChecked, setPresetModelConsentChecked] = useState(false);
  const [presetModelInstallSubmitting, setPresetModelInstallSubmitting] = useState(false);
  const [presetModelInstallError, setPresetModelInstallError] = useState<string | null>(null);
  const [streamPublications, setStreamPublications] = useState<StreamPublication[]>([]);
  const [streamPublicationError, setStreamPublicationError] = useState<string | null>(null);
  const [streamPublicationBusyByKey, setStreamPublicationBusyByKey] = useState<Record<string, boolean>>({});
  const [processingServers, setProcessingServers] = useState<ProcessingServer[]>([]);
  const [discoveryBusy, setDiscoveryBusy] = useState(false);
  const [discoveryResult, setDiscoveryResult] = useState<OnvifDiscoverResponse | null>(null);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [inspectBusy, setInspectBusy] = useState(false);
  const [inspectResult, setInspectResult] = useState<OnvifInspectResponse | null>(null);
  const [inspectError, setInspectError] = useState<string | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const [probeResult, setProbeResult] = useState<RtspProbeResponse | null>(null);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const probeBusyMessage = useDelayedStatusText(
    probeBusy,
    t("ext.cameras.settings.probe_progress", {}, "Testando RTSP... pode levar até 5s."),
    t("ext.cameras.settings.probe_progress_waiting", {}, "Ainda testando RTSP... se expirar, revise a URL e credenciais."),
    4500,
  );
  const snapshotBusyMessage = useDelayedStatusText(
    snapshotBusy,
    t(
      "ext.cameras.settings.snapshot_progress",
      {},
      "Capturando snapshot... a primeira captura pode levar alguns segundos.",
    ),
    t("ext.cameras.settings.snapshot_progress_waiting", {}, "Ainda aguardando frame da câmera."),
    8000,
  );

  useEffect(() => {
    if (activeCameraId && cameras.some((camera) => camera.id === activeCameraId)) return;
    setActiveCameraId(cameras[0]?.id ?? "");
  }, [activeCameraId, cameras]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchProcessingServers(controller.signal).then(setProcessingServers).catch(() => undefined);
    const loadHealth = () => {
      void fetchCameraSourceHealth(controller.signal).then(setSourceHealth).catch(() => undefined);
    };
    loadHealth();
    const timer = window.setInterval(loadHealth, 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchCamerasIndex(controller.signal)
      .then((index) => {
        if (controller.signal.aborted) return;
        setSavedCameraIds(new Set((index.cameras ?? []).map((camera) => String(camera.id || "").trim()).filter(Boolean)));
      })
      .catch(() => {
        if (!controller.signal.aborted) setSavedCameraIds(new Set());
      });
    return () => controller.abort();
  }, [settings]);

  const reloadStreamPublications = useCallback((signal?: AbortSignal) => {
    void fetchStreamPublications(undefined, signal)
      .then((items) => {
        setStreamPublications(items);
        setStreamPublicationError(null);
      })
      .catch((error) => {
        if (signal?.aborted) return;
        setStreamPublicationError(error instanceof Error ? error.message : String(error));
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    reloadStreamPublications(controller.signal);
    return () => controller.abort();
  }, [reloadStreamPublications]);

  useEffect(() => {
    setInspectResult(null);
    setInspectError(null);
    setProbeResult(null);
    setProbeError(null);
    setPipelineNotice(null);
  }, [activeCameraId]);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  const activeCamera = cameras.find((camera) => camera.id === activeCameraId) ?? null;
  const activeSourceId =
    (activeCamera ? activeSourceByCamera[activeCamera.id] : "") ||
    activeCamera?.sources.find((source) => source.is_default)?.id ||
    activeCamera?.sources[0]?.id ||
    "";
  const activeSource = activeCamera?.sources.find((source) => source.id === activeSourceId) ?? activeCamera?.sources[0] ?? null;
  const activeHealth = activeCamera && activeSource ? sourceHealthFor(sourceHealth, activeCamera.id, activeSource.id) : null;
  const activeCameraPersisted = Boolean(activeCamera && savedCameraIds?.has(activeCamera.id));
  const mappedCompositions = useMemo(
    () =>
      (cameraContexts?.compositions ?? []).filter((composition) =>
        (composition.camera_elements ?? []).some((element) => Boolean(element.has_mapping)),
      ),
    [cameraContexts],
  );
  const hasMappedComposition = mappedCompositions.length > 0;
  const presetDetectionModels = useMemo(() => readDetectionModelCatalog(presetModelStatusPayload), [presetModelStatusPayload]);
  const presetDefaultDetectionModel =
    findDetectionModel(presetDetectionModels, DEFAULT_DETECTION_MODEL_ID) ?? presetDetectionModels[0] ?? null;
  const presetDefaultModelReady = isDetectionModelReady(presetDefaultDetectionModel);
  const presetDefaultModelPreparing = isActiveDetectionModelInstall(presetDefaultDetectionModel);

  useEffect(() => {
    if (!activeCamera || !activeCameraPersisted) {
      setCameraContexts(null);
      setCameraContextsLoading(false);
      setCameraContextsError(null);
      setCameraPipelines(null);
      setCameraPipelinesLoading(false);
      setCameraPipelinesError(null);
      setPresetModelStatusPayload(null);
      setPresetModelStatusLoading(false);
      setPresetModelStatusError(null);
      setPresetModelInstallError(null);
      return undefined;
    }

    const controller = new AbortController();
    setCameraContextsLoading(true);
    setCameraContextsError(null);
    void fetchCameraContexts(activeCamera.id, controller.signal)
      .then((contexts) => {
        if (!controller.signal.aborted) setCameraContexts(contexts);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setCameraContexts(null);
        setCameraContextsError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setCameraContextsLoading(false);
      });

    setCameraPipelinesLoading(true);
    setCameraPipelinesError(null);
    void fetchCameraPipelines(activeCamera.id, controller.signal)
      .then((pipelines) => {
        if (!controller.signal.aborted) setCameraPipelines(pipelines);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setCameraPipelines(null);
        setCameraPipelinesError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setCameraPipelinesLoading(false);
      });

    return () => controller.abort();
  }, [activeCamera?.id, activeCameraPersisted]);

  async function loadPresetModelStatus(showLoading: boolean, signal?: AbortSignal): Promise<void> {
    if (!activeCameraPersisted) return;
    if (showLoading) setPresetModelStatusLoading(true);
    setPresetModelStatusError(null);
    try {
      const payload = await fetchProcessingServerStatus("local", signal);
      setPresetModelStatusPayload(payload);
    } catch (error) {
      if (signal?.aborted) return;
      setPresetModelStatusError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted && showLoading) setPresetModelStatusLoading(false);
    }
  }

  useEffect(() => {
    if (!activeCameraPersisted) return;
    const controller = new AbortController();
    void loadPresetModelStatus(true, controller.signal);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCameraPersisted]);

  useEffect(() => {
    if (!activeCameraPersisted || !presetDefaultModelPreparing) return;
    const timer = window.setInterval(() => {
      void loadPresetModelStatus(false);
    }, 1500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCameraPersisted, presetDefaultDetectionModel?.modelId, presetDefaultDetectionModel?.installJob?.status, presetDefaultModelPreparing]);

  const publicationBySourceKey = useMemo(() => {
    const byKey = new Map<string, StreamPublication>();
    for (const publication of streamPublications) {
      if (publication.owner_kind !== "camera_source") continue;
      const cameraId = String(publication.camera_id || "").trim();
      const sourceId = String(publication.camera_source_id || "").trim();
      if (!cameraId || !sourceId) continue;
      byKey.set(`${cameraId}:${sourceId}`, publication);
    }
    return byKey;
  }, [streamPublications]);

  function sourcePublication(cameraId: string, sourceId: string): StreamPublication | null {
    return publicationBySourceKey.get(`${cameraId}:${sourceId}`) ?? null;
  }

  function sourcePublicationEnabled(cameraId: string, source: CameraSourceConfig): boolean {
    if (!source.enabled || source.kind !== "video") return false;
    const publication = sourcePublication(cameraId, source.id);
    return publication?.enabled !== false;
  }

  function patchPublicationState(publication: StreamPublication): void {
    setStreamPublications((previous) => {
      const key = `${publication.camera_id || ""}:${publication.camera_source_id || ""}`;
      const next = previous.filter((item) => `${item.camera_id || ""}:${item.camera_source_id || ""}` !== key);
      next.push(publication);
      return next;
    });
  }

  async function updateSourcePublication(
    cameraId: string,
    source: CameraSourceConfig,
    patch: Partial<Pick<StreamPublication, "enabled" | "label" | "role" | "host_server_id" | "quality_policy" | "transport_policy">>,
  ): Promise<void> {
    const key = `${cameraId}:${source.id}`;
    setStreamPublicationBusyByKey((previous) => ({ ...previous, [key]: true }));
    setStreamPublicationError(null);
    try {
      const publication = await updateCameraSourcePublication(cameraId, source.id, {
        label: source.name || source.id,
        role: source.role,
        ...patch,
      });
      patchPublicationState(publication);
      reloadStreamPublications();
    } catch (error) {
      setStreamPublicationError(error instanceof Error ? error.message : String(error));
    } finally {
      setStreamPublicationBusyByKey((previous) => ({ ...previous, [key]: false }));
    }
  }

  function schedulePublicationReconcile(): void {
    window.setTimeout(() => {
      void reconcileStreamPublications()
        .then(() => reloadStreamPublications())
        .catch((error) => setStreamPublicationError(error instanceof Error ? error.message : String(error)));
    }, 150);
  }

  async function confirmPresetDefaultModelInstall(): Promise<void> {
    if (!presetDefaultDetectionModel || presetModelInstallSubmitting) return;
    setPresetModelInstallSubmitting(true);
    setPresetModelInstallError(null);
    try {
      await installProcessingServerVisionModel("local", presetDefaultDetectionModel.modelId, {
        mode: "local_build",
        acknowledge_upstream_terms: true,
      });
      setPresetModelConsentOpen(false);
      setPresetModelConsentChecked(false);
      await loadPresetModelStatus(false);
    } catch (error) {
      setPresetModelInstallError(error instanceof Error ? error.message : String(error));
    } finally {
      setPresetModelInstallSubmitting(false);
    }
  }

  const filteredCameras = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return cameras;
    return cameras.filter((camera) => {
      const haystack = [
        camera.id,
        camera.name,
        camera.onvif?.xaddr ?? "",
        ...camera.sources.map((source) => `${source.name} ${source.id} ${source.origin.rtsp_url}`),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [cameras, query]);

  function commit(next: CameraConfig[]): void {
    updateSettings(serializeCameras(next));
  }

  function updateCamera(cameraId: string, updater: (camera: CameraConfig) => CameraConfig): void {
    commit(cameras.map((camera) => (camera.id === cameraId ? updater(camera) : camera)));
  }

  function updateSource(cameraId: string, sourceId: string, updater: (source: CameraSourceConfig) => CameraSourceConfig): void {
    updateCamera(cameraId, (camera) => ({
      ...camera,
      sources: camera.sources.map((source) => (source.id === sourceId ? updater(source) : source)),
    }));
  }

  function addCamera(controlType: CameraControlType): void {
    const id = slugSourceId(`camera_${cameras.length + 1}_${createUniqueId().slice(0, 6)}`, `camera_${cameras.length + 1}`);
    const camera: CameraConfig = {
      id,
      name: controlType === "onvif" ? "Nova câmera ONVIF" : "Nova câmera manual",
      enabled: true,
      control: { type: controlType },
      onvif: controlType === "onvif" ? { xaddr: "", username: "", password: "" } : null,
      sources: controlType === "none" ? [createDefaultCameraSource(0)] : [],
      metadata: {},
    };
    commit([...cameras, camera]);
    setActiveCameraId(id);
  }

  function addDiscoveredCamera(device: OnvifDiscoveredDeviceInfo): void {
    const xaddr = suggestedCameraXaddr(device);
    const name = suggestedCameraName(device, t);
    const baseId = slugSourceId(
      `${name}_${device.device_id || device.source_ip || createUniqueId()}`,
      `camera_${cameras.length + 1}`,
    );
    let id = baseId;
    if (cameras.some((camera) => camera.id === id)) {
      id = slugSourceId(`${baseId}_${createUniqueId().slice(0, 6)}`, `${baseId}_${cameras.length + 1}`);
    }
    const camera: CameraConfig = {
      id,
      name,
      enabled: true,
      control: { type: "onvif" },
      onvif: {
        device_id: String(device.device_id || "").trim(),
        xaddr,
        username: "",
        password: "",
        hardware: String(device.hardware || "").trim(),
      },
      sources: [],
      metadata: {
        discovery_source_ip: String(device.source_ip || "").trim(),
      },
    };
    commit([...cameras, camera]);
    setActiveCameraId(id);
    setDiscoveryResult((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        devices: previous.devices.filter((item) => item !== device),
      };
    });
  }

  async function scanOnvifDevices(force = true): Promise<void> {
    setDiscoveryBusy(true);
    setDiscoveryError(null);
    try {
      const result = await discoverOnvifDevices({ timeout_ms: 1600, force, exclude_known: true });
      setDiscoveryResult(result);
    } catch (error) {
      setDiscoveryError(error instanceof Error ? error.message : String(error));
    } finally {
      setDiscoveryBusy(false);
    }
  }

  function addRtspSource(camera: CameraConfig): void {
    const next = createDefaultCameraSource(camera.sources.length, {
      id: `rtsp_${camera.sources.length + 1}`,
      name: "RTSP",
      role: "custom",
      is_default: camera.sources.length === 0,
    });
    updateCamera(camera.id, (current) => ({ ...current, sources: [...current.sources, next] }));
    setActiveSourceByCamera((prev) => ({ ...prev, [camera.id]: next.id }));
    schedulePublicationReconcile();
  }

  function removeSource(cameraId: string, sourceId: string): void {
    updateCamera(cameraId, (camera) => {
      const nextSources = camera.sources.filter((source) => source.id !== sourceId);
      return {
        ...camera,
        sources: nextSources.map((source, index) => ({
          ...source,
          is_default: nextSources.some((item) => item.is_default) ? source.is_default : index === 0,
        })),
      };
    });
    schedulePublicationReconcile();
  }

  function makeDefaultSource(cameraId: string, sourceId: string): void {
    updateCamera(cameraId, (camera) => ({
      ...camera,
      sources: camera.sources.map((source) => ({ ...source, is_default: source.id === sourceId })),
    }));
  }

  function applyIngestSelection(cameraId: string, sourceId: string, value: string): void {
    updateSource(cameraId, sourceId, (source) => {
      if (value === "runtime_local") return { ...source, ingest: { mode: "runtime_local", host_server_id: "local" } };
      if (value === "direct") return { ...source, ingest: { mode: "direct", host_server_id: "local" } };
      const hostServerId = value.startsWith("centralized:") ? value.slice("centralized:".length) : "local";
      return {
        ...source,
        ingest: { mode: "centralized", host_server_id: normalizeServerId(hostServerId) },
      };
    });
    schedulePublicationReconcile();
  }

  async function discoverSources(camera: CameraConfig): Promise<void> {
    const xaddr = camera.onvif?.xaddr?.trim() ?? "";
    if (!xaddr) {
      setInspectError(t("ext.cameras.settings.onvif_xaddr_required", {}, "Informe o endereço ONVIF."));
      return;
    }
    setInspectBusy(true);
    setInspectError(null);
    try {
      const result = await inspectOnvif({
        xaddr,
        username: camera.onvif?.username ?? "",
        password: camera.onvif?.password ?? "",
        timeout_ms: 5000,
      });
      setInspectResult(result);
      updateCamera(camera.id, (current) => ({
        ...current,
        onvif: {
          ...(current.onvif ?? { xaddr }),
          xaddr: result.xaddr || xaddr,
          username: current.onvif?.username ?? "",
          password: current.onvif?.password ?? "",
          media_xaddr: result.media_xaddr ?? current.onvif?.media_xaddr,
          ptz_xaddr: result.ptz_xaddr ?? current.onvif?.ptz_xaddr,
        },
        sources: mergeOnvifSources(current.sources, result.profiles ?? []),
      }));
      schedulePublicationReconcile();
    } catch (error) {
      setInspectError(error instanceof Error ? error.message : String(error));
    } finally {
      setInspectBusy(false);
    }
  }

  async function runProbe(camera: CameraConfig, source: CameraSourceConfig): Promise<void> {
    setProbeBusy(true);
    setProbeError(null);
    setProbeResult(null);
    try {
      const result = await probeCameraRtsp(camera.id, { source_id: source.id, timeout_ms: 5000 });
      setProbeResult(result);
    } catch (error) {
      setProbeError(error instanceof Error ? error.message : String(error));
    } finally {
      setProbeBusy(false);
    }
  }

  async function openSnapshot(camera: CameraConfig, source: CameraSourceConfig): Promise<void> {
    setSnapshotOpen(true);
    setSnapshotBusy(true);
    setSnapshotError(null);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    try {
      const blob = await fetchCameraSnapshot(camera.id, source.id);
      const nextUrl = URL.createObjectURL(blob);
      setSnapshotUrl(nextUrl);
    } catch (error) {
      setSnapshotError(error instanceof Error ? error.message : String(error));
    } finally {
      setSnapshotBusy(false);
    }
  }

  async function reloadActiveCameraPipelines(): Promise<void> {
    if (!activeCamera) return;
    setCameraPipelinesLoading(true);
    setCameraPipelinesError(null);
    try {
      setCameraPipelines(await fetchCameraPipelines(activeCamera.id));
    } catch (error) {
      setCameraPipelinesError(error instanceof Error ? error.message : String(error));
    } finally {
      setCameraPipelinesLoading(false);
    }
  }

  function openPipelineEditor(pipelineName: string): void {
    const name = String(pipelineName || "").trim();
    if (!name) return;
    navigateInApp(`/settings/pipelines/${encodeURIComponent(name)}`);
  }

  function handlePipelineCreated(pipelineName: string): void {
    setPipelineNotice(t("ext.cameras.pipelines.created", { name: pipelineName }, "Pipeline created: {{name}}"));
    void reloadActiveCameraPipelines();
  }

  return (
    <div className="settingsPanel">
      <div className="settingsHeaderRow">
        <div>
          <div className="settingsTitle">{t("ext.cameras.settings.name", {}, "Câmeras")}</div>
          <div className="settingsDescription">
            {t("ext.cameras.settings.multi_source_help", {}, "Cada câmera representa um dispositivo físico; fluxos escolhem uma fonte da câmera.")}
          </div>
        </div>
        <div className="rowWrap">
          <button className="primaryButton" type="button" onClick={() => addCamera("onvif")}>
            {t("ext.cameras.settings.add_onvif", {}, "Adicionar ONVIF")}
          </button>
          <button className="chipButton" type="button" onClick={() => addCamera("none")}>
            {t("ext.cameras.settings.add_manual", {}, "Adicionar manual")}
          </button>
        </div>
      </div>

      <div className="card">
        <div className="cardBody">
          <div className="settingsSectionHeader">
            <div>
              <div className="modalSectionTitle">{t("ext.cameras.settings.suggestions.title", {}, "Câmeras sugeridas")}</div>
              <div className="settingsDescription">
                {t("ext.cameras.settings.suggestions.desc", {}, "Procure câmeras ONVIF na rede e adicione com um clique.")}
              </div>
            </div>
            <button className="chipButton" type="button" disabled={discoveryBusy} onClick={() => void scanOnvifDevices(true)}>
              {discoveryBusy
                ? t("ext.cameras.settings.suggestions.scanning", {}, "Procurando...")
                : t("ext.cameras.settings.suggestions.scan", {}, "Procurar na rede")}
            </button>
          </div>

          {discoveryError ? <div className="errorText">{discoveryError}</div> : null}
          {discoveryResult?.warnings?.length ? <div className="settingsStatusMuted">{discoveryResult.warnings.join(" ")}</div> : null}
          {discoveryResult?.targets?.length ? (
            <div className="settingsStatusMuted">
              {t("ext.cameras.settings.suggestions.targets", {}, "Destinos de descoberta")}: {discoveryResult.targets.join(", ")}
            </div>
          ) : null}

          {discoveryResult && discoveryResult.devices.length === 0 ? (
            <div className="settingsStatusMuted">{t("ext.cameras.settings.suggestions.none", {}, "Nenhuma câmera nova encontrada.")}</div>
          ) : null}

          {discoveryResult?.devices.length ? (
            <div className="settingsList">
              {discoveryResult.devices.map((device) => {
                const xaddr = suggestedCameraXaddr(device);
                const label = suggestedCameraName(device, t);
                const key = String(device.device_id || xaddr || device.source_ip || label);
                return (
                  <button
                    key={key}
                    className="settingsListItem"
                    type="button"
                    disabled={!xaddr}
                    onClick={() => addDiscoveredCamera(device)}
                  >
                    <span className="settingsListTitle">
                      {label} · {t("ext.cameras.settings.suggestions.add", {}, "Adicionar")}
                    </span>
                    <span className="settingsListMeta">
                      {xaddr || device.source_ip || device.device_id}
                      {device.hardware ? ` · ${device.hardware}` : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>

      <div className="settingsTwoColumn">
        <aside className="settingsSidebar">
          <input
            className="input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("ext.cameras.settings.search", {}, "Buscar câmera ou fonte")}
          />
          <div className="settingsList">
            {filteredCameras.map((camera) => {
              const selected = camera.id === activeCameraId;
              const defaultSource = camera.sources.find((source) => source.is_default) ?? camera.sources[0];
              return (
                <button
                  key={camera.id}
                  className={`settingsListItem${selected ? " isActive" : ""}`}
                  type="button"
                  onClick={() => setActiveCameraId(camera.id)}
                >
                  <span className="settingsListTitle">{camera.name || camera.id}</span>
                  <span className="settingsListMeta">
                    {camera.control.type === "onvif" ? "ONVIF" : t("ext.cameras.settings.control.none", {}, "Manual")} ·{" "}
                    {camera.sources.length} {t("ext.cameras.settings.sources.short", {}, "fontes")}
                    {defaultSource ? ` · ${defaultSource.name}` : ""}
                  </span>
                </button>
              );
            })}
          </div>
        </aside>

        <main className="settingsMain">
          {!activeCamera ? (
            <div className="emptyState">{t("ext.cameras.settings.empty", {}, "Adicione uma câmera para começar.")}</div>
          ) : (
            <>
              <div className="card">
                <div className="cardBody">
                  <div className="rowWrap">
                    <div className="field" style={{ flex: 2, minWidth: 220 }}>
                      <label className="label">{t("ext.cameras.settings.camera_name", {}, "Nome da câmera")}</label>
                      <input
                        className="input"
                        value={activeCamera.name}
                        onChange={(event) => updateCamera(activeCamera.id, (camera) => ({ ...camera, name: event.target.value }))}
                      />
                    </div>
                    <div className="field" style={{ flex: 1, minWidth: 180 }}>
                      <label className="label">{t("ext.cameras.settings.control_protocol", {}, "Controle / descoberta")}</label>
                      <select
                        className="input"
                        value={activeCamera.control.type}
                        onChange={(event) => {
                          const type = event.target.value === "onvif" ? "onvif" : "none";
                          updateCamera(activeCamera.id, (camera) => ({
                            ...camera,
                            control: { type },
                            onvif: type === "onvif" ? camera.onvif ?? { xaddr: "", username: "", password: "" } : null,
                          }));
                        }}
                      >
                        <option value="onvif">ONVIF</option>
                        <option value="none">{t("ext.cameras.settings.control.none", {}, "Manual / sem ONVIF")}</option>
                      </select>
                    </div>
                    <button
                      className="dangerButton"
                      type="button"
                      onClick={() => commit(cameras.filter((camera) => camera.id !== activeCamera.id))}
                    >
                      {t("core.actions.delete", {}, "Excluir")}
                    </button>
                  </div>

                  {activeCamera.control.type === "onvif" ? (
                    <div className="sectionDivider">
                      <div className="rowWrap">
                        <div className="field" style={{ flex: 2, minWidth: 240 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_xaddr", {}, "Endereço ONVIF")}</label>
                          <input
                            className="input"
                            value={activeCamera.onvif?.xaddr ?? ""}
                            onChange={(event) =>
                              updateCamera(activeCamera.id, (camera) => ({
                                ...camera,
                                onvif: { ...(camera.onvif ?? { xaddr: "" }), xaddr: event.target.value },
                              }))
                            }
                          />
                        </div>
                        <div className="field" style={{ flex: 1, minWidth: 180 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_username", {}, "Usuário ONVIF")}</label>
                          <input
                            className="input"
                            value={activeCamera.onvif?.username ?? ""}
                            onChange={(event) =>
                              updateCamera(activeCamera.id, (camera) => ({
                                ...camera,
                                onvif: { ...(camera.onvif ?? { xaddr: "" }), username: event.target.value },
                              }))
                            }
                          />
                        </div>
                        <div className="field" style={{ flex: 1, minWidth: 180 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_password", {}, "Senha ONVIF")}</label>
                          <input
                            className="input"
                            type="password"
                            value={activeCamera.onvif?.password ?? ""}
                            onChange={(event) =>
                              updateCamera(activeCamera.id, (camera) => ({
                                ...camera,
                                onvif: { ...(camera.onvif ?? { xaddr: "" }), password: event.target.value },
                              }))
                            }
                          />
                        </div>
                        <button className="primaryButton" type="button" disabled={inspectBusy} onClick={() => void discoverSources(activeCamera)}>
                          {inspectBusy
                            ? t("ext.cameras.settings.onvif_discovering", {}, "Descobrindo...")
                            : t("ext.cameras.settings.onvif_discover", {}, "Descobrir fontes")}
                        </button>
                      </div>
                      {inspectError ? <div className="errorText">{inspectError}</div> : null}
                      {inspectResult?.warnings?.length ? <div className="settingsStatusMuted">{inspectResult.warnings.join(" ")}</div> : null}
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="settingsSectionHeader">
                <div>
                  <div className="modalSectionTitle">{t("ext.cameras.settings.sources.title", {}, "Fontes da câmera")}</div>
                  <div className="settingsDescription">
                    {t("ext.cameras.settings.sources.help", {}, "Fluxos, snapshots e diagnósticos escolhem uma destas fontes.")}
                  </div>
                </div>
                <button className="chipButton" type="button" onClick={() => addRtspSource(activeCamera)}>
                  {t("ext.cameras.settings.sources.add_rtsp", {}, "Adicionar RTSP")}
                </button>
              </div>

              {activeCamera.sources.length === 0 ? (
                <div className="card">
                  <div className="cardBody">
                    {t("ext.cameras.settings.sources.empty", {}, "Nenhuma fonte configurada. Descubra via ONVIF ou adicione uma fonte RTSP.")}
                  </div>
                </div>
              ) : (
                <div className="settingsSourceGrid">
                  <div className="settingsSourceList">
                    {activeCamera.sources.map((source) => {
                      const selected = source.id === activeSource?.id;
                      const health = sourceHealthFor(sourceHealth, activeCamera.id, source.id);
                      return (
                        <button
                          key={source.id}
                          type="button"
                          className={`card settingsSourceItem${selected ? " isActive" : ""}`}
                          onClick={() => setActiveSourceByCamera((prev) => ({ ...prev, [activeCamera.id]: source.id }))}
                        >
                          <span className="cardBody">
                            <span className="settingsListTitle">
                              {source.name || source.id} {source.is_default ? "· padrão" : ""}
                              {sourcePublicationEnabled(activeCamera.id, source) ? " · transmitindo" : ""}
                            </span>
                            <span className="settingsListMeta">{sourceRoleLabel(source.role, t)} · {sourceResolutionLabel(source)}</span>
                            <span className="settingsListMeta">{sourceOriginLabel(source, t)}</span>
                            {health?.status ? <span className="settingsListMeta">Saúde: {health.status}</span> : null}
                          </span>
                        </button>
                      );
                    })}
                  </div>

                  {activeSource ? (
                    <div className="card">
                      <div className="cardBody">
                        <div className="rowWrap">
                          <div className="field" style={{ flex: 1, minWidth: 180 }}>
                            <label className="label">{t("ext.cameras.settings.sources.name", {}, "Nome da fonte")}</label>
                            <input
                              className="input"
                              value={activeSource.name}
                              onChange={(event) =>
                                updateSource(activeCamera.id, activeSource.id, (source) => ({ ...source, name: event.target.value }))
                              }
                              onBlur={(event) => {
                                if (!sourcePublicationEnabled(activeCamera.id, activeSource)) return;
                                void updateSourcePublication(activeCamera.id, activeSource, {
                                  label: event.target.value || activeSource.id,
                                });
                              }}
                            />
                          </div>
                          <div className="field" style={{ width: 180 }}>
                            <label className="label">{t("ext.cameras.settings.sources.role", {}, "Papel")}</label>
                            <select
                              className="input"
                              value={activeSource.role}
                              onChange={(event) => {
                                const role = event.target.value as CameraSourceConfig["role"];
                                updateSource(activeCamera.id, activeSource.id, (source) => ({
                                  ...source,
                                  role,
                                }));
                                if (sourcePublicationEnabled(activeCamera.id, activeSource)) {
                                  void updateSourcePublication(activeCamera.id, { ...activeSource, role }, { role });
                                }
                              }}
                            >
                              <option value="main">{sourceRoleLabel("main", t)}</option>
                              <option value="sub">{sourceRoleLabel("sub", t)}</option>
                              <option value="zoom">{sourceRoleLabel("zoom", t)}</option>
                              <option value="custom">{sourceRoleLabel("custom", t)}</option>
                            </select>
                          </div>
                          <div className="field" style={{ width: 160 }}>
                            <label className="label">{t("ext.cameras.settings.sources.view", {}, "Visão")}</label>
                            <input
                              className="input"
                              value={activeSource.view_id}
                              onChange={(event) =>
                                updateSource(activeCamera.id, activeSource.id, (source) => ({ ...source, view_id: event.target.value }))
                              }
                            />
                          </div>
                        </div>

                        <div className="rowWrap">
                          <label className="chipButton">
                            <input
                              type="checkbox"
                              checked={activeSource.enabled}
                              onChange={(event) => {
                                const enabled = event.target.checked;
                                updateSource(activeCamera.id, activeSource.id, (source) => ({ ...source, enabled }));
                                if (!enabled && sourcePublication(activeCamera.id, activeSource.id)?.enabled !== false) {
                                  void updateSourcePublication(activeCamera.id, activeSource, { enabled: false });
                                } else {
                                  schedulePublicationReconcile();
                                }
                              }}
                            />
                            {t("ext.cameras.settings.sources.enabled", {}, "Ativa")}
                          </label>
                          {activeSource.kind === "video" ? (
                            <label className="chipButton">
                              <input
                                type="checkbox"
                                checked={sourcePublicationEnabled(activeCamera.id, activeSource)}
                                disabled={Boolean(streamPublicationBusyByKey[`${activeCamera.id}:${activeSource.id}`]) || !activeSource.enabled}
                                onChange={(event) => {
                                  void updateSourcePublication(activeCamera.id, activeSource, {
                                    enabled: event.target.checked,
                                    role: activeSource.role,
                                    label: activeSource.name || activeSource.id,
                                  });
                                }}
                              />
                              {t("ext.cameras.settings.sources.transmit", {}, "Transmitir esta fonte")}
                            </label>
                          ) : null}
                          <button className="chipButton" type="button" onClick={() => makeDefaultSource(activeCamera.id, activeSource.id)}>
                            {activeSource.is_default ? t("ext.cameras.settings.sources.default", {}, "Fonte padrão") : t("ext.cameras.settings.sources.make_default", {}, "Tornar padrão")}
                          </button>
                          <button className="dangerButton" type="button" onClick={() => removeSource(activeCamera.id, activeSource.id)}>
                            {t("core.actions.delete", {}, "Excluir")}
                          </button>
                        </div>
                        {streamPublicationError ? <div className="errorText">{streamPublicationError}</div> : null}

                        <div className="sectionDivider">
                          <div className="rowWrap">
                            <div className="field" style={{ width: 190 }}>
                              <label className="label">{t("ext.cameras.settings.sources.origin", {}, "Origem")}</label>
                              <select
                                className="input"
                                value={activeSource.origin.type}
                                onChange={(event) =>
                                  updateSource(activeCamera.id, activeSource.id, (source) => ({
                                    ...source,
                                    origin: { ...source.origin, type: event.target.value === "onvif_profile" ? "onvif_profile" : "rtsp" },
                                  }))
                                }
                              >
                                <option value="rtsp">RTSP</option>
                                <option value="onvif_profile">ONVIF</option>
                              </select>
                            </div>
                            {activeSource.origin.type === "rtsp" ? (
                              <div className="field" style={{ flex: 1, minWidth: 240 }}>
                                <label className="label">{t("ext.cameras.settings.sources.rtsp_url", {}, "URL RTSP")}</label>
                                <input
                                  className="input"
                                  value={activeSource.origin.rtsp_url}
                                  onChange={(event) =>
                                    updateSource(activeCamera.id, activeSource.id, (source) => ({
                                      ...source,
                                      origin: { ...source.origin, rtsp_url: event.target.value },
                                    }))
                                  }
                                />
                              </div>
                            ) : (
                              <div className="field" style={{ flex: 1, minWidth: 240 }}>
                                <label className="label">{t("ext.cameras.settings.onvif_profile", {}, "Perfil de stream")}</label>
                                <div className="settingsStatusMuted">
                                  {activeSource.origin.profile_name ||
                                    activeSource.origin.profile_token ||
                                    t("ext.cameras.settings.sources.onvif_profile_unset", {}, "Perfil ONVIF não definido.")}
                                </div>
                              </div>
                            )}
                          </div>
                          {activeSource.origin.type === "rtsp" ? (
                            <div className="rowWrap">
                              <div className="field" style={{ flex: 1, minWidth: 180 }}>
                                <label className="label">{t("ext.cameras.settings.stream_username", {}, "Usuário do stream")}</label>
                                <input
                                  className="input"
                                  value={activeSource.origin.stream_username ?? ""}
                                  onChange={(event) =>
                                    updateSource(activeCamera.id, activeSource.id, (source) => ({
                                      ...source,
                                      origin: { ...source.origin, stream_username: event.target.value },
                                    }))
                                  }
                                />
                              </div>
                              <div className="field" style={{ flex: 1, minWidth: 180 }}>
                                <label className="label">{t("ext.cameras.settings.stream_password", {}, "Senha do stream")}</label>
                                <input
                                  className="input"
                                  type="password"
                                  value={activeSource.origin.stream_password ?? ""}
                                  onChange={(event) =>
                                    updateSource(activeCamera.id, activeSource.id, (source) => ({
                                      ...source,
                                      origin: { ...source.origin, stream_password: event.target.value },
                                    }))
                                  }
                                />
                              </div>
                            </div>
                          ) : (
                            <div className="settingsStatusMuted">
                              {activeSource.origin.rtsp_url ? (
                                <>
                                  {t("ext.cameras.settings.sources.onvif_resolved_stream_uri", {}, "Endereço de stream resolvido")}:{" "}
                                  <code>{activeSource.origin.rtsp_url}</code>
                                </>
                              ) : (
                                t(
                                  "ext.cameras.settings.sources.onvif_profile_hint",
                                  {},
                                  "A URL RTSP é resolvida a partir do perfil ONVIF selecionado quando o playback começa.",
                                )
                              )}
                            </div>
                          )}
                        </div>

                        <div className="sectionDivider">
                          <label className="label">{t("ext.cameras.settings.ingest.title", {}, "Entrada da câmera")}</label>
                          <div className="settingsDescription">
                            {t("ext.cameras.settings.ingest.help", {}, "Centralizar reduz conexões simultâneas com a câmera.")}
                          </div>
                          <select className="input" value={ingestSelectValue(activeSource)} onChange={(event) => applyIngestSelection(activeCamera.id, activeSource.id, event.target.value)}>
                            <option value="centralized:local">{t("ext.cameras.settings.ingest.option.main", {}, "Centralizar no ambiente principal")}</option>
                            {processingServers.map((server) => {
                              const serverId = normalizeServerId(server.id);
                              if (serverId === "local") return null;
                              return (
                                <option key={serverId} value={`centralized:${serverId}`}>
                                  {t("ext.cameras.settings.ingest.option.processing", { name: serverDisplayName(serverId, processingServers, t) }, `Centralizar em servidor de processamento: ${serverDisplayName(serverId, processingServers, t)}`)}
                                </option>
                              );
                            })}
                            <option value="runtime_local">{t("ext.cameras.settings.ingest.option.runtime_local", {}, "Centralizar onde o fluxo estiver rodando")}</option>
                            <option value="direct">{t("ext.cameras.settings.ingest.option.direct", {}, "Não centralizar pelo Toposync")}</option>
                          </select>
                          <div className="settingsMetricGrid">
                            <div><span className="label">{t("ext.cameras.settings.ingest.summary.mode", {}, "Modo")}</span><div>{ingestModeLabel(activeSource, t)}</div></div>
                            <div><span className="label">{t("ext.cameras.settings.ingest.summary.centralizer", {}, "Centralizador efetivo")}</span><div>{ingestCentralizerLabel(activeSource, processingServers, t)}</div></div>
                            <div><span className="label">{t("ext.cameras.settings.ingest.summary.path", {}, "Caminho")}</span><div>{ingestPathLabel(activeSource, t)}</div></div>
                          </div>
                          {normalizeIngestConfig(activeSource.ingest).mode === "direct" ? (
                            <div className="settingsStatusMuted">
                              {t("ext.cameras.settings.ingest.direct_hint", {}, "Esta câmera pode receber uma conexão por fluxo consumidor.")}
                            </div>
                          ) : null}
                        </div>

                        <div className="sectionDivider">
                          <div className="rowWrap">
                            <button className="chipButton" type="button" disabled={probeBusy} onClick={() => void runProbe(activeCamera, activeSource)}>
                              {probeBusy ? t("ext.cameras.settings.probe_testing", {}, "Testando RTSP") : t("ext.cameras.settings.probe", {}, "Probe")}
                            </button>
                            <button className="chipButton" type="button" disabled={snapshotBusy} onClick={() => void openSnapshot(activeCamera, activeSource)}>
                              {t("ext.cameras.settings.snapshot", {}, "Snapshot")}
                            </button>
                          </div>
                          {probeBusy ? <div className="settingsStatusMuted" role="status">{probeBusyMessage}</div> : null}
                          {probeResult ? <div className="settingsStatusMuted">Probe: {probeResult.status} · {probeResult.latency_ms} ms</div> : null}
                          {probeError ? (
                            <>
                              <div className="errorText">{probeError}</div>
                              <div className="settingsStatusMuted">
                                {t(
                                  "ext.cameras.settings.probe_error_hint",
                                  {},
                                  "Teste RTSP novamente depois de revisar URL, credenciais e acesso de rede.",
                                )}
                              </div>
                            </>
                          ) : null}
                          {activeHealth ? (
                            <div className="settingsMetricGrid">
                              <div><span className="label">{t("ext.cameras.settings.source_health.mode", {}, "Modo")}</span><div>{activeHealth.ingest_mode}</div></div>
                              <div><span className="label">{t("ext.cameras.settings.source_health.centralizer", {}, "Centralizador")}</span><div>{serverDisplayName(activeHealth.centralizer_server_id, processingServers, t)}</div></div>
                              <div><span className="label">{t("ext.cameras.settings.source_health.current_read", {}, "Leitura atual")}</span><div>{activeHealth.used_ingest ? "Ingest" : "Direta"}</div></div>
                              <div><span className="label">Status</span><div>{activeHealth.status}</div></div>
                            </div>
                          ) : null}
                          {activeHealth?.ingest_blocking_errors?.length ? <div className="errorText">{activeHealth.ingest_blocking_errors.join(" ")}</div> : null}
                        </div>
                      </div>
                    </div>
                  ) : null}
                </div>
              )}

              <div className="settingsSectionHeader">
                <div>
                  <div className="modalSectionTitle">{t("ext.cameras.mapping.title", {}, "Mapeamento")}</div>
                  <div className="settingsDescription">
                    {t("ext.cameras.mapping.help", {}, "Composições onde esta câmera aparece e se os pontos de controle estão mapeados.")}
                  </div>
                </div>
              </div>

              {savedCameraIds === null ? (
                <div className="card">
                  <div className="cardBody">{t("core.ui.loading", {}, "Carregando...")}</div>
                </div>
              ) : !activeCameraPersisted ? (
                <div className="card">
                  <div className="cardBody">
                    {t(
                      "ext.cameras.workflow.save_first",
                      {},
                      "Salve as alterações da câmera antes de continuar com mapeamento e fluxos.",
                    )}
                  </div>
                </div>
              ) : cameraContextsLoading ? (
                <div className="card">
                  <div className="cardBody">{t("core.ui.loading", {}, "Carregando...")}</div>
                </div>
              ) : cameraContextsError ? (
                <div className="card">
                  <div className="cardBody errorText">{cameraContextsError}</div>
                </div>
              ) : (cameraContexts?.compositions ?? []).length === 0 ? (
                <div className="card">
                  <div className="cardBody">
                    <div>{t("ext.cameras.mapping.empty", {}, "Esta câmera não aparece em nenhuma composição.")}</div>
                    <div className="rowWrap">
                      <button className="chipButton" type="button" onClick={() => openSettingsPanel("__compositions__")}>
                        {t("ext.cameras.mapping.open_compositions", {}, "Abrir composições")}
                      </button>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="settingsList" role="list">
                  {(cameraContexts?.compositions ?? []).map((composition) => {
                    const calibratedViews = (composition.camera_elements ?? []).reduce(
                      (sum, element) => sum + Number(element.calibrated_views || 0),
                      0,
                    );
                    const ready = (composition.camera_elements ?? []).some((element) => Boolean(element.has_mapping));
                    return (
                      <div className="settingsListItem" key={composition.id} role="listitem">
                        <span className="settingsListTitle">{composition.name || composition.id}</span>
                        <span className="settingsListMeta">
                          {ready
                            ? t("ext.cameras.mapping.ready", {}, "Vistas calibradas")
                            : t("ext.cameras.mapping.missing", {}, "Faltam vistas calibradas")}
                          {" · "}
                          {t("ext.cameras.mapping.views_count", { count: calibratedViews }, "{{count}} vistas")}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="settingsSectionHeader">
                <div>
                  <div className="modalSectionTitle">{t("ext.cameras.pipelines.title", {}, "Fluxos")}</div>
                  <div className="settingsDescription">
                    {t("ext.cameras.pipelines.help", {}, "Atalhos para fluxos que usam esta câmera e presets para criar novos.")}
                  </div>
                </div>
              </div>

              {savedCameraIds === null ? (
                <div className="card">
                  <div className="cardBody">{t("core.ui.loading", {}, "Carregando...")}</div>
                </div>
              ) : !activeCameraPersisted ? (
                <div className="card">
                  <div className="cardBody">
                    {t(
                      "ext.cameras.workflow.save_first",
                      {},
                      "Salve as alterações da câmera antes de continuar com mapeamento e fluxos.",
                    )}
                  </div>
                </div>
              ) : (
                <div className="card">
                  <div className="cardBody">
                    {pipelineNotice ? <div className="settingsStatusMuted">{pipelineNotice}</div> : null}
                    {cameraPipelinesError ? <div className="errorText">{cameraPipelinesError}</div> : null}
                    {cameraPipelinesLoading ? <div>{t("core.ui.loading", {}, "Carregando...")}</div> : null}

                    {!cameraPipelinesLoading && (cameraPipelines?.pipelines ?? []).length === 0 ? (
                      <div className="settingsStatusMuted">{t("ext.cameras.pipelines.empty", {}, "Nenhum fluxo usa esta câmera ainda.")}</div>
                    ) : null}

                    {(cameraPipelines?.pipelines ?? []).length ? (
                      <div className="settingsList" role="list">
                        {(cameraPipelines?.pipelines ?? []).map((pipeline) => (
                          <button
                            className="settingsListItem"
                            key={pipeline.name}
                            type="button"
                            onClick={() => openPipelineEditor(pipeline.name)}
                          >
                            <span className="settingsListTitle">{pipeline.name}</span>
                            <span className="settingsListMeta">
                              {pipeline.enabled === false
                                ? t("ext.cameras.pipelines.disabled", {}, "Desativado")
                                : t("ext.cameras.pipelines.enabled", {}, "Ativo")}
                              {" · "}
                              {pipeline.processing_server_id || "local"}
                              {pipeline.source_ids?.length ? ` · ${pipeline.source_ids.join(", ")}` : ""}
                            </span>
                          </button>
                        ))}
                      </div>
                    ) : null}

                    <div className="sectionDivider">
                      <div className="modalSectionTitle">
                        {t("ext.cameras.pipelines.add_title", {}, "Adicionar fluxo")}
                      </div>
                      <div className="settingsStatusMuted">
                        <div className="settingsListTitle">
                          {t(
                            "ext.cameras.pipeline_preset.model.default_banner_title",
                            { model: presetDefaultDetectionModel?.displayName || DEFAULT_DETECTION_MODEL_NAME },
                            "{{model}} for camera presets",
                          )}
                        </div>
                        <div>
                          {presetModelStatusLoading
                            ? t("core.ui.loading", {}, "Carregando...")
                            : presetDefaultModelReady
                              ? t(
                                  "ext.cameras.pipeline_preset.model.default_ready",
                                  { model: presetDefaultDetectionModel?.displayName || DEFAULT_DETECTION_MODEL_NAME },
                                  "{{model}} is ready on the main processing server.",
                                )
                              : presetDefaultModelPreparing && presetDefaultDetectionModel
                                ? t(
                                    "ext.cameras.pipeline_preset.model.preparing_status",
                                    {
                                      model: presetDefaultDetectionModel.displayName,
                                      progress: modelSetupProgressLabel(presetDefaultDetectionModel, t),
                                    },
                                    "{{model}} is being prepared. {{progress}}",
                                  )
                                : canPrepareDetectionModel(presetDefaultDetectionModel)
                                  ? t(
                                      "ext.cameras.pipeline_preset.model.default_missing_actionable",
                                      { model: presetDefaultDetectionModel?.displayName || DEFAULT_DETECTION_MODEL_NAME },
                                      "{{model}} is recommended for these presets and needs to be prepared before pipeline creation.",
                                    )
                                  : t(
                                      "ext.cameras.pipeline_preset.model.default_missing_manual",
                                      {
                                        model: presetDefaultDetectionModel?.displayName || DEFAULT_DETECTION_MODEL_NAME,
                                        reason: modelSetupReasonLabel(presetDefaultDetectionModel?.localBuildReason ?? "", t),
                                      },
                                      "{{model}} is not ready on the main server. Automatic preparation is unavailable: {{reason}}.",
                                    )}
                        </div>
                        {presetModelStatusError ? <div className="errorText">{presetModelStatusError}</div> : null}
                        {presetDefaultDetectionModel?.installJob?.error ? (
                          <div className="errorText">{presetDefaultDetectionModel.installJob.error}</div>
                        ) : null}
                        {!presetDefaultModelReady && !canPrepareDetectionModel(presetDefaultDetectionModel) && !presetDefaultModelPreparing ? (
                          <div>
                            {t(
                              "ext.cameras.pipeline_preset.model.manual_next_step",
                              {},
                              "Choose another ready model, use another processing server, or prepare the model manually in the detection operator.",
                            )}
                          </div>
                        ) : null}
                        <div className="rowWrap">
                          {canPrepareDetectionModel(presetDefaultDetectionModel) ? (
                            <button
                              className="primaryButton"
                              type="button"
                              disabled={presetModelInstallSubmitting}
                              onClick={() => {
                                setPresetModelInstallError(null);
                                setPresetModelConsentChecked(false);
                                setPresetModelConsentOpen(true);
                              }}
                            >
                              {t("ext.cameras.pipeline_preset.model.prepare_auto", {}, "Baixar e preparar automaticamente")}
                            </button>
                          ) : null}
                          <button
                            className="chipButton"
                            type="button"
                            disabled={presetModelStatusLoading}
                            onClick={() => void loadPresetModelStatus(true)}
                          >
                            {t("ext.cameras.pipeline_preset.model.refresh", {}, "Atualizar modelos")}
                          </button>
                        </div>
                      </div>
                      <div className="cameraPipelinePresetGrid">
                        {CAMERA_PIPELINE_PRESET_CARDS.map((presetCard) => {
                          const hasVideoSource = activeCamera.sources.some((source) => source.kind === "video" && source.enabled);
                          const disabledReason = !hasVideoSource
                            ? t("ext.cameras.pipelines.no_video_source", {}, "Adicione uma fonte de vídeo ativa antes de criar um fluxo.")
                            : presetCard.requiresMapping && !hasMappedComposition
                              ? t(
                                  "ext.cameras.pipelines.mapping_required_for_preset",
                                  {},
                                  "Mapeie esta câmera em uma composição para usar este preset.",
                                )
                              : "";
                          const disabled = Boolean(disabledReason);
                          return (
                            <button
                              className="cameraPipelinePresetCard"
                              key={presetCard.id}
                              type="button"
                              disabled={disabled}
                              onClick={() => setPipelinePresetOpen(presetCard.id)}
                            >
                              <span className="cameraPipelinePresetTop">
                                <span className="cameraPipelinePresetTitle">
                                  {t(presetCard.titleKey, {}, presetCard.titleFallback)}
                                </span>
                              </span>
                              <span className="cameraPipelinePresetDescription">
                                {t(presetCard.descriptionKey, {}, presetCard.descriptionFallback)}
                              </span>
                              <span className="cameraPipelinePresetSteps">
                                {t(presetCard.stepsKey, {}, presetCard.stepsFallback)}
                              </span>
                              {disabledReason ? <span className="cameraPipelinePresetDisabledReason">{disabledReason}</span> : null}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </main>
      </div>

      <SubModal open={snapshotOpen} title={t("ext.cameras.settings.snapshot", {}, "Snapshot")} onClose={() => setSnapshotOpen(false)}>
        {snapshotError ? (
          <>
            <div className="errorText">{snapshotError}</div>
            <div className="settingsStatusMuted">
              {t(
                "ext.cameras.settings.snapshot_error_hint",
                {},
                "Se a captura falhar de novo, teste RTSP e revise a URL da fonte.",
              )}
            </div>
          </>
        ) : null}
        {snapshotUrl ? <img src={snapshotUrl} alt={t("ext.cameras.settings.snapshot", {}, "Snapshot")} style={{ width: "100%" }} /> : null}
        {!snapshotError && !snapshotUrl ? (
          <div className="cardBody">
            {snapshotBusy ? snapshotBusyMessage : t("ext.cameras.settings.snapshot", {}, "Snapshot")}
          </div>
        ) : null}
      </SubModal>

      {activeCamera ? (
        <CameraPipelinePresetModal
          open={Boolean(pipelinePresetOpen)}
          preset={pipelinePresetOpen}
          camera={activeCamera}
          activeSourceId={activeSourceId}
          pipelineOverview={cameraPipelines}
          mappedCompositions={mappedCompositions}
          processingServers={processingServers}
          i18n={i18n}
          onClose={() => setPipelinePresetOpen(null)}
          onCreated={handlePipelineCreated}
        />
      ) : null}

      <VisionModelConsentModal
        open={presetModelConsentOpen}
        serverLabel={serverDisplayName("local", processingServers, t)}
        modelName={presetDefaultDetectionModel?.displayName || DEFAULT_DETECTION_MODEL_NAME}
        runtimeLabel={presetDefaultDetectionModel?.localBuildRuntime ?? ""}
        sourceLabel={presetDefaultDetectionModel?.localBuildSourceLabel ?? ""}
        checked={presetModelConsentChecked}
        submitting={presetModelInstallSubmitting}
        error={presetModelInstallError}
        t={t}
        onToggleChecked={setPresetModelConsentChecked}
        onClose={() => {
          setPresetModelConsentOpen(false);
          setPresetModelConsentChecked(false);
          setPresetModelInstallError(null);
        }}
        onConfirm={() => void confirmPresetDefaultModelInstall()}
      />
    </div>
  );
}
