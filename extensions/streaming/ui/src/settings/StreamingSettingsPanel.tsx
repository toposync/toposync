import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { SettingsPanel, TopoSyncHost } from "@toposync/plugin-api";

import {
  createTransmission,
  deleteTransmission,
  fetchEngineStatus,
  fetchProcessingServers,
  fetchStreamsHealth,
  fetchStreamingSettings,
  fetchTransmissionUrls,
  fetchTransmissions,
  patchStreamingSettings,
  postEngineAction,
  postEngineDownload,
  updateTransmission,
} from "../api/streamingApi";
import { STREAMING_EXTENSION_ID } from "../constants";
import type {
  EngineStatusResponse,
  ProcessingServer,
  StreamingEngineSettings,
  StreamingExtensionSettings,
  StreamAuthentication,
  StreamsHealthResponse,
  Transmission,
  TransmissionOutput,
  TransmissionUrlsResponse,
} from "../types";
import { SubModal } from "./SubModal";
import { WizardCreatePipelineFromTransmission } from "./WizardCreatePipelineFromTransmission";

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

function defaultOutput(protocol: "hls" | "rtsp" | "webrtc"): TransmissionOutput {
  return {
    id: createLocalId("output"),
    protocol,
    enabled: true,
    resolution: { width: 1280, height: 720 },
    fps_limit: 12,
    bitrate_kbps: null,
    latency_profile: "normal",
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
  const [engineStatus, setEngineStatus] = useState<EngineStatusResponse | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);

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

  useEffect(() => {
    const controller = new AbortController();
    void fetchHealthData(controller.signal);
    void fetchEngineData(controller.signal);
    void fetchSettingsData(controller.signal);
    void fetchTransmissionsData(controller.signal);
    void fetchProcessingServersData(controller.signal);
    return () => controller.abort();
  }, [fetchEngineData, fetchHealthData, fetchProcessingServersData, fetchSettingsData, fetchTransmissionsData]);

  useEffect(() => {
    if (activeTransmissionId && transmissions.some((item) => item.id === activeTransmissionId)) return;
    setActiveTransmissionId(transmissions[0]?.id ?? null);
  }, [activeTransmissionId, transmissions]);

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

  async function runEngineAction(action: "start" | "stop" | "restart"): Promise<void> {
    setEngineBusy(true);
    setEngineError(null);
    try {
      const payload = await postEngineAction(action);
      setEngineStatus(payload);
      void fetchSettingsData();
      void fetchRuntimeData();
    } catch (error) {
      setEngineError(error instanceof Error ? error.message : String(error));
    } finally {
      setEngineBusy(false);
    }
  }

  async function downloadEngine(): Promise<void> {
    setEngineBusy(true);
    setEngineError(null);
    try {
      const payload = await postEngineDownload();
      setEngineStatus(payload);
      void fetchSettingsData();
      void fetchRuntimeData();
    } catch (error) {
      setEngineError(error instanceof Error ? error.message : String(error));
    } finally {
      setEngineBusy(false);
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
          preferred_ports: {
            rtsp: engineSettingsDraft.preferred_ports?.rtsp,
            hls: engineSettingsDraft.preferred_ports?.hls,
            api: engineSettingsDraft.preferred_ports?.api,
            webrtc: engineSettingsDraft.preferred_ports?.webrtc,
          },
          webrtc_ice_servers: Array.isArray(engineSettingsDraft.webrtc_ice_servers)
            ? engineSettingsDraft.webrtc_ice_servers
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
        throw new Error(`Host server inválido: ${hostServerId}`);
      }
      const suggestedName = newTransmissionName.trim() || "Transmission";
      const suggestedPath = slugifyPath(newTransmissionPath.trim() || suggestedName) || "stream";

      const payload = await createTransmission({
        name: suggestedName,
        path: suggestedPath,
        enabled: true,
        host_server_id: hostServerId,
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
      void fetchRuntimeData();
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreateBusy(false);
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
        throw new Error(`Host server inválido: ${hostServerId}`);
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
      void fetchRuntimeData();
    } catch (error) {
      setTransmissionDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setTransmissionDraftBusy(false);
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
      void fetchRuntimeData();
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
              <li>{t("ext.streaming.settings.quickstart_step_3", {}, "Salve, carregue URLs e use o wizard para gerar o pipeline.")}</li>
            </ol>
          </div>
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">
          <div className="cardMeta">
            {t("ext.streaming.settings.transmissions", {}, "Transmissões configuradas")}: {transmissionsCount}
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

          {engineStatus?.bind_host && engineStatus?.ports ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {engineStatus.bind_host} - RTSP {engineStatus.ports.rtsp ?? "-"} - HLS {engineStatus.ports.hls ?? "-"} - WebRTC{" "}
              {engineStatus.ports.webrtc ?? "-"}
            </div>
          ) : null}

          {engineStatus?.urls?.rtsp_url ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              RTSP (test): {engineStatus.urls.rtsp_url}
            </div>
          ) : null}

          {engineStatus?.urls?.hls_url ? <div className="cardMeta">HLS (test): {engineStatus.urls.hls_url}</div> : null}
          {engineStatus?.urls?.webrtc_url ? <div className="cardMeta">WebRTC/WHEP (test): {engineStatus.urls.webrtc_url}</div> : null}

          {Array.isArray(engineStatus?.warnings) && engineStatus.warnings.length > 0 ? (
            <div className="cardMeta" style={{ marginTop: 6 }}>
              {engineStatus.warnings.join(" ")}
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
              onClick={() => void runEngineAction("start")}
            >
              {t("ext.streaming.engine.start", {}, "Start")}
            </button>
            <button className="chipButton" type="button" disabled={engineBusy} onClick={() => void downloadEngine()}>
              <i className="fa-solid fa-download" aria-hidden="true" />{" "}
              {t("ext.streaming.engine.download", {}, "Download engine")}
            </button>
            <button
              className="chipButton"
              type="button"
              disabled={engineBusy}
              onClick={() => void runEngineAction("stop")}
            >
              {t("ext.streaming.engine.stop", {}, "Stop")}
            </button>
            <button
              className="chipButton"
              type="button"
              disabled={engineBusy}
              onClick={() => void runEngineAction("restart")}
            >
              {t("ext.streaming.engine.restart", {}, "Restart")}
            </button>
            <button className="chipButton" type="button" disabled={engineBusy} onClick={() => void fetchEngineData()}>
              <i className="fa-solid fa-rotate-right" aria-hidden="true" /> {t("ext.streaming.engine.refresh", {}, "Refresh")}
            </button>
          </div>

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
              </div>

              <div className="streamingFormGrid streamingFormGridEnginePorts" style={{ marginTop: 10 }}>
                <div className="field">
                  <label className="label">RTSP port</label>
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
                  <label className="label">HLS port</label>
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
                  <label className="label">WebRTC port</label>
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
                  <label className="label">API port</label>
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
              </div>

              <div className="field" style={{ marginTop: 10 }}>
                <label className="label">STUN/TURN servers (optional, one per line)</label>
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
                  Use somente quando precisar atravessar NAT. Em LAN simples, deixe vazio.
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
          {processingServersLoading ? <div className="settingsStatusMuted" style={{ marginTop: 10 }}>Carregando processing servers…</div> : null}
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
                  <li>{t("ext.streaming.settings.quickstart_step_3", {}, "Salve, carregue URLs e use o wizard para gerar o pipeline.")}</li>
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
                const meta = `host: ${normalizeServerId(item.host_server_id)} • path: ${item.path || "-"} • outputs: ${Array.isArray(item.outputs) ? item.outputs.length : 0}`;
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
                      </div>
                      {!item.enabled ? <span className="pillBadge" title="Disabled">off</span> : null}
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
                  {t("ext.streaming.transmissions.select_hint", {}, "Dica: comece criando uma transmissão e depois abra o wizard para gerar o pipeline.")}
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
                  {transmissionDraftDirty ? <span className="pillBadge" title="Unsaved changes">edited</span> : null}
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
                          const label = server.name?.trim() ? `${serverId} (${server.name})` : serverId;
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
                        <option value="gray">gray</option>
                        <option value="black">black</option>
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
                        <option value="priority_latest">priority_latest</option>
                        <option value="latest">latest</option>
                      </select>
                    </div>
                  </div>

                  {!knownProcessingServerIds.has(normalizeServerId(transmissionDraft.host_server_id)) ? (
                    <div className="errorText" style={{ marginTop: 10 }}>
                      Host server não existe mais. Selecione outro antes de salvar.
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
                      <button className="chipButton" type="button" onClick={() => addDraftOutput("hls")}>
                        + HLS
                      </button>
                      <button className="chipButton" type="button" onClick={() => addDraftOutput("rtsp")}>
                        + RTSP
                      </button>
                      <button className="chipButton" type="button" onClick={() => addDraftOutput("webrtc")}>
                        + WebRTC
                      </button>
                    </div>
                  </div>

                  {Array.isArray(transmissionDraft.outputs) && transmissionDraft.outputs.length === 0 ? (
                    <div className="cardMeta">{t("ext.streaming.transmissions.outputs_empty", {}, "Nenhuma saída adicionada.")}</div>
                  ) : null}

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
                                  <div className="settingsListItemMeta">Output id: {output.id}</div>
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
                                    <option value="hls">hls</option>
                                    <option value="rtsp">rtsp</option>
                                    <option value="webrtc">webrtc</option>
                                  </select>
                                </div>

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
                                    <option value="normal">normal</option>
                                    <option value="low">low</option>
                                    <option value="ultra_low">ultra_low</option>
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
                        {t("ext.streaming.wizard.open", {}, "Criar pipeline com esta transmissão")}
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
                      {activeUrls.outputs.map((item) => (
                        <div key={`${activeUrls.transmission_id}-${item.output_id}`} className="card" style={{ marginTop: 10 }}>
                          <div className="cardBody">
                            <div className="settingsListItemTitle" style={{ marginBottom: 6 }}>
                              {item.protocol.toUpperCase()}
                            </div>
                            <div className="cardMeta">Engine path: {item.resolved_engine_path}</div>
                            {item.requires_auth ? (
                              <div className="cardMeta" style={{ marginTop: 6 }}>
                                Requer autenticação
                                {item.auth_username ? ` (usuário: ${item.auth_username})` : ""}.
                              </div>
                            ) : (
                              <div className="cardMeta" style={{ marginTop: 6 }}>
                                Acesso sem autenticação.
                              </div>
                            )}
                            <div className="rowWrap" style={{ gap: 8, marginTop: 10 }}>
                              <input className="input" style={{ flex: 1 }} value={item.url} readOnly />
                              <button
                                className="iconButton"
                                type="button"
                                aria-label="Copy URL"
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
                const label = server.name?.trim() ? `${serverId} (${server.name})` : serverId;
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

        <div className="rowWrap" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button className="primaryButton" type="button" disabled={createBusy} onClick={() => void createTransmissionAction()}>
            {createBusy ? t("ext.streaming.transmissions.creating", {}, "Criando…") : t("ext.streaming.transmissions.create_button", {}, "Criar transmissão")}
          </button>
        </div>
      </SubModal>

      <SubModal
        open={confirmDiscardOpen}
        title={t("ext.streaming.transmissions.discard_title", {}, "Descartar alterações?")}
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
