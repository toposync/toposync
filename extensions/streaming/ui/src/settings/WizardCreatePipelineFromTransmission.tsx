import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n } from "@toposync/plugin-api";

import { createPipelineFromTransmissionWizard, fetchCamerasIndex } from "../api/streamingApi";
import type {
  CameraIndexItem,
  ProcessingServer,
  StreamingWizardCreatePipelineResponse,
  StreamingWizardPresetId,
  Transmission,
} from "../types";
import { SubModal } from "./SubModal";

type WizardStep = "form" | "done";

type PresetOption = {
  id: StreamingWizardPresetId;
  titleKey: string;
  descriptionKey: string;
  fallbackTitle: string;
  fallbackDescription: string;
};

const PRESET_OPTIONS: PresetOption[] = [
  {
    id: "simple_stream",
    titleKey: "ext.streaming.wizard.presets.simple_stream.title",
    descriptionKey: "ext.streaming.wizard.presets.simple_stream.desc",
    fallbackTitle: "Simple stream",
    fallbackDescription: "camera.source + optional fps reducer + stream.publish_video",
  },
  {
    id: "motion_gate_stream",
    titleKey: "ext.streaming.wizard.presets.motion_gate_stream.title",
    descriptionKey: "ext.streaming.wizard.presets.motion_gate_stream.desc",
    fallbackTitle: "Motion gate stream",
    fallbackDescription: "camera.source + motion gate + fps reducer + stream.publish_video",
  },
  {
    id: "detection_stream",
    titleKey: "ext.streaming.wizard.presets.detection_stream.title",
    descriptionKey: "ext.streaming.wizard.presets.detection_stream.desc",
    fallbackTitle: "Detection stream",
    fallbackDescription: "camera.source + object detection + stream.publish_video",
  },
  {
    id: "tracking_stream",
    titleKey: "ext.streaming.wizard.presets.tracking_stream.title",
    descriptionKey: "ext.streaming.wizard.presets.tracking_stream.desc",
    fallbackTitle: "Tracking stream",
    fallbackDescription: "camera.source + object tracking + stream.publish_video",
  },
  {
    id: "segmentation_stream",
    titleKey: "ext.streaming.wizard.presets.segmentation_stream.title",
    descriptionKey: "ext.streaming.wizard.presets.segmentation_stream.desc",
    fallbackTitle: "Segmentation stream",
    fallbackDescription: "camera.source + object segmentation + stream.publish_video",
  },
];

function toSafeFloat(value: string): number | undefined {
  const trimmed = String(value || "").trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) return undefined;
  return parsed;
}

function toSafeInt(value: string): number | undefined {
  const trimmed = String(value || "").trim();
  if (!trimmed) return undefined;
  const parsed = Number.parseInt(trimmed, 10);
  if (!Number.isFinite(parsed)) return undefined;
  return parsed;
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

function openPipelinesScreen(): void {
  if (typeof window === "undefined") return;
  const target = "/settings/pipelines";
  if (window.location.pathname === target) return;
  window.history.pushState(null, "", target);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function WizardCreatePipelineFromTransmission({
  open,
  i18n,
  transmission,
  engineRunning,
  processingServers,
  onClose,
  onCreated,
}: {
  open: boolean;
  i18n: HostI18n;
  transmission: Transmission | null;
  engineRunning: boolean;
  processingServers: ProcessingServer[];
  onClose: () => void;
  onCreated: (payload: StreamingWizardCreatePipelineResponse) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [step, setStep] = useState<WizardStep>("form");
  const [presetId, setPresetId] = useState<StreamingWizardPresetId>("simple_stream");
  const [cameraId, setCameraId] = useState("");
  const [cameras, setCameras] = useState<CameraIndexItem[]>([]);
  const [camerasLoading, setCamerasLoading] = useState(false);
  const [camerasError, setCamerasError] = useState<string | null>(null);

  const [pipelineName, setPipelineName] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [processingServerId, setProcessingServerId] = useState("local");
  const [sourceBackend, setSourceBackend] = useState<"auto" | "opencv" | "ffmpeg">("auto");
  const [useFpsReducer, setUseFpsReducer] = useState(false);
  const [fpsLimit, setFpsLimit] = useState("");
  const [motionSensitivity, setMotionSensitivity] = useState("0.01");
  const [motionHoldSeconds, setMotionHoldSeconds] = useState("6");
  const [resizeMode, setResizeMode] = useState<"contain" | "none">("contain");
  const [writerPriority, setWriterPriority] = useState("0");
  const [bypassMode, setBypassMode] = useState<"auto" | "force_on" | "force_off">("auto");
  const [yoloConfidenceThreshold, setYoloConfidenceThreshold] = useState("0.55");
  const [yoloFilterEnabled, setYoloFilterEnabled] = useState(true);
  const [detectionCategories, setDetectionCategories] = useState("");
  const [trackingCategories, setTrackingCategories] = useState("");

  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [created, setCreated] = useState<StreamingWizardCreatePipelineResponse | null>(null);

  useEffect(() => {
    if (!open) return;
    setStep("form");
    setPresetId("simple_stream");
    setCameraId("");
    setCameras([]);
    setCamerasLoading(false);
    setCamerasError(null);
    setPipelineName("");
    setEnabled(true);
    setProcessingServerId(normalizeServerId(transmission?.host_server_id));
    setSourceBackend("auto");
    setUseFpsReducer(false);
    setFpsLimit("");
    setMotionSensitivity("0.01");
    setMotionHoldSeconds("6");
    setResizeMode("contain");
    setWriterPriority("0");
    setBypassMode("auto");
    setYoloConfidenceThreshold("0.55");
    setYoloFilterEnabled(true);
    setDetectionCategories("");
    setTrackingCategories("");
    setCreateBusy(false);
    setCreateError(null);
    setCreated(null);
  }, [open, transmission?.host_server_id]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setCamerasLoading(true);
    setCamerasError(null);
    setCameras([]);

    void (async () => {
      try {
        const data = await fetchCamerasIndex(controller.signal);
        if (controller.signal.aborted) return;
        const next = Array.isArray(data.cameras) ? data.cameras : [];
        setCameras(next);
        if (next.length > 0) {
          setCameraId((previous) => previous || String(next[0]?.id || "").trim());
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setCamerasError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!controller.signal.aborted) setCamerasLoading(false);
      }
    })();

    return () => controller.abort();
  }, [open]);

  const selectedTransmissionName = useMemo(() => {
    if (!transmission) return "";
    return transmission.name?.trim() || transmission.path?.trim() || transmission.id;
  }, [transmission]);

  const selectedPreset = useMemo(() => PRESET_OPTIONS.find((item) => item.id === presetId) ?? PRESET_OPTIONS[0], [presetId]);
  const sortedProcessingServers = useMemo(
    () => sortProcessingServers(Array.isArray(processingServers) ? processingServers : []),
    [processingServers],
  );
  const knownProcessingServerIds = useMemo(() => {
    const ids = new Set<string>(["local"]);
    for (const item of sortedProcessingServers) {
      ids.add(normalizeServerId(item.id));
    }
    return ids;
  }, [sortedProcessingServers]);
  const transmissionHostServerId = normalizeServerId(transmission?.host_server_id);
  const selectedProcessingServerId = normalizeServerId(processingServerId);
  const hostMismatch = transmissionHostServerId !== selectedProcessingServerId;

  function parseCategories(raw: string): string[] | undefined {
    const items = raw
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean);
    if (items.length === 0) return undefined;
    return Array.from(new Set(items));
  }

  async function createPipeline(): Promise<void> {
    if (!transmission) return;
    if (!cameraId.trim()) {
      setCreateError(t("ext.streaming.wizard.errors.select_camera", {}, "Select a camera."));
      return;
    }
    if (!knownProcessingServerIds.has(selectedProcessingServerId)) {
      setCreateError(
        t(
          "ext.streaming.wizard.errors.invalid_processing_server",
          { serverId: selectedProcessingServerId },
          `Invalid processing server: ${selectedProcessingServerId}`,
        ),
      );
      return;
    }
    if (hostMismatch) {
      setCreateError(
        t(
          "ext.streaming.wizard.errors.host_mismatch",
          { transmissionHost: transmissionHostServerId, pipelineHost: selectedProcessingServerId },
          `Transmission is hosted on '${transmissionHostServerId}'. Select the same processing server for the pipeline.`,
        ),
      );
      return;
    }

    setCreateBusy(true);
    setCreateError(null);
    try {
      const response = await createPipelineFromTransmissionWizard({
        transmission_id: transmission.id,
        camera_id: cameraId.trim(),
        preset_id: presetId,
        optional_parameters: {
          pipeline_name: pipelineName.trim() || undefined,
          enabled,
          processing_server_id: selectedProcessingServerId,
          source_backend: sourceBackend,
          use_fps_reducer: useFpsReducer,
          fps_limit: toSafeFloat(fpsLimit),
          motion_sensitivity: toSafeFloat(motionSensitivity),
          motion_hold_seconds: toSafeFloat(motionHoldSeconds),
          resize_mode: resizeMode,
          writer_priority: toSafeInt(writerPriority),
          bypass_mode: bypassMode,
          yolo_confidence_threshold: toSafeFloat(yoloConfidenceThreshold),
          yolo_filter_enabled: yoloFilterEnabled,
          detection_categories: parseCategories(detectionCategories),
          tracking_categories: parseCategories(trackingCategories),
        },
      });
      setCreated(response);
      setStep("done");
      onCreated(response);
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreateBusy(false);
    }
  }

  return (
    <SubModal
      open={open}
      title={t("ext.streaming.wizard.title", {}, "Criar pipeline para transmissão")}
      closeAriaLabel={t("core.actions.close", {}, "Close")}
      onClose={() => {
        if (createBusy) return;
        onClose();
      }}
    >
      {step === "form" ? (
        <div className="streamingWizard">
          <div className="card">
            <div className="cardBody">
              <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                {selectedTransmissionName}
              </div>
              <div className="cardMeta">{transmission?.id || ""}</div>
              {!engineRunning ? (
                <div className="cardMeta" style={{ marginTop: 8 }}>
                  {t(
                    "ext.streaming.wizard.engine_warning",
                    {},
                    "A engine de streaming está parada. Você pode criar o pipeline agora e iniciar a engine depois.",
                  )}
                </div>
              ) : null}
            </div>
          </div>

          {camerasLoading ? <div className="settingsStatusMuted streamingWizardFeedback">{t("ext.streaming.wizard.loading_cameras", {}, "Carregando câmeras…")}</div> : null}

          {camerasError ? <div className="errorText streamingWizardFeedback">{camerasError}</div> : null}

          {createError ? <div className="errorText streamingWizardFeedback">{createError}</div> : null}

          <div className="card">
            <div className="cardBody">
              <div className="field">
                <label className="label">{t("ext.streaming.wizard.camera", {}, "Câmera")}</label>
                <input
                  className="input"
                  list="streaming-wizard-cameras"
                  value={cameraId}
                  onChange={(event) => setCameraId(event.target.value)}
                  placeholder={t("ext.streaming.wizard.camera_placeholder", {}, "camera_id")}
                />
                <datalist id="streaming-wizard-cameras">
                  {cameras.map((camera) => (
                    <option key={camera.id} value={camera.id}>
                      {camera.name || camera.id}
                    </option>
                  ))}
                </datalist>
              </div>

              <div className="field">
                <label className="label">{t("ext.streaming.wizard.preset", {}, "Preset")}</label>
                <select className="input" value={presetId} onChange={(event) => setPresetId(event.target.value as StreamingWizardPresetId)}>
                  {PRESET_OPTIONS.map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {t(preset.titleKey, {}, preset.fallbackTitle)}
                    </option>
                  ))}
                </select>
                <div className="label">{t(selectedPreset.descriptionKey, {}, selectedPreset.fallbackDescription)}</div>
              </div>

              <div className="rowWrap" style={{ gap: 10 }}>
                <div className="field" style={{ flex: 1, minWidth: 220 }}>
                  <label className="label">{t("ext.streaming.wizard.pipeline_name", {}, "Nome do pipeline (opcional)")}</label>
                  <input className="input" value={pipelineName} onChange={(event) => setPipelineName(event.target.value)} />
                </div>
                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.processing_server", {}, "Processing server")}</label>
                  <select
                    className="input"
                    value={selectedProcessingServerId}
                    onChange={(event) => setProcessingServerId(normalizeServerId(event.target.value))}
                  >
                    {sortedProcessingServers.map((server) => {
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
                    {!knownProcessingServerIds.has(selectedProcessingServerId) ? (
                      <option value={selectedProcessingServerId}>{selectedProcessingServerId}</option>
                    ) : null}
                  </select>
                </div>
              </div>

              {hostMismatch ? (
                <div className="errorText">
                  {t(
                    "ext.streaming.wizard.host_mismatch_inline",
                    { transmissionHost: transmissionHostServerId, pipelineHost: selectedProcessingServerId },
                    `Transmission is hosted on ${transmissionHostServerId} and pipeline is on ${selectedProcessingServerId}. They must match.`,
                  )}
                </div>
              ) : null}

              <div className="rowWrap" style={{ gap: 10 }}>
                <div className="field" style={{ width: 170 }}>
                  <label className="label">{t("ext.streaming.wizard.fps", {}, "FPS limit")}</label>
                  <input className="input" value={fpsLimit} onChange={(event) => setFpsLimit(event.target.value)} placeholder="5" />
                </div>
                <div className="field" style={{ width: 170 }}>
                  <label className="label">{t("ext.streaming.wizard.writer_priority", {}, "Writer priority")}</label>
                  <input className="input" value={writerPriority} onChange={(event) => setWriterPriority(event.target.value)} />
                </div>
                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.source_backend", {}, "Backend da câmera")}</label>
                  <select
                    className="input"
                    value={sourceBackend}
                    onChange={(event) => setSourceBackend(event.target.value as "auto" | "opencv" | "ffmpeg")}
                  >
                    <option value="auto">{t("ext.streaming.wizard.source_backend.option.auto", {}, "Auto")}</option>
                    <option value="opencv">{t("ext.streaming.wizard.source_backend.option.opencv", {}, "OpenCV")}</option>
                    <option value="ffmpeg">{t("ext.streaming.wizard.source_backend.option.ffmpeg", {}, "FFmpeg")}</option>
                  </select>
                </div>
                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.bypass_mode", {}, "Bypass mode")}</label>
                  <select
                    className="input"
                    value={bypassMode}
                    onChange={(event) => setBypassMode(event.target.value as "auto" | "force_on" | "force_off")}
                  >
                    <option value="auto">{t("ext.streaming.wizard.bypass_mode.option.auto", {}, "Auto")}</option>
                    <option value="force_on">{t("ext.streaming.wizard.bypass_mode.option.force_on", {}, "Force on")}</option>
                    <option value="force_off">{t("ext.streaming.wizard.bypass_mode.option.force_off", {}, "Force off")}</option>
                  </select>
                </div>
              </div>

              <div className="rowWrap" style={{ gap: 10 }}>
                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.resize_mode", {}, "Resize mode")}</label>
                  <select className="input" value={resizeMode} onChange={(event) => setResizeMode(event.target.value as "contain" | "none")}>
                    <option value="contain">{t("ext.streaming.wizard.resize_mode.option.contain", {}, "Contain")}</option>
                    <option value="none">{t("ext.streaming.wizard.resize_mode.option.none", {}, "No resize")}</option>
                  </select>
                </div>

                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.motion_sensitivity", {}, "Motion sensitivity")}</label>
                  <input className="input" value={motionSensitivity} onChange={(event) => setMotionSensitivity(event.target.value)} />
                </div>
                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.motion_hold", {}, "Motion hold (s)")}</label>
                  <input className="input" value={motionHoldSeconds} onChange={(event) => setMotionHoldSeconds(event.target.value)} />
                </div>
                <div className="field" style={{ width: 180 }}>
                  <label className="label">{t("ext.streaming.wizard.yolo_conf", {}, "Vision confidence")}</label>
                  <input className="input" value={yoloConfidenceThreshold} onChange={(event) => setYoloConfidenceThreshold(event.target.value)} />
                </div>
              </div>

              <div className="field">
                <label className="rowWrap" style={{ gap: 10 }}>
                  <input
                    type="checkbox"
                    checked={yoloFilterEnabled}
                    onChange={(event) => setYoloFilterEnabled(event.target.checked)}
                  />
                  <span className="cardMeta">
                    {t("ext.streaming.wizard.yolo_filter_enabled", {}, "Filter frames after vision (recommended)")}
                  </span>
                </label>
                <div className="cardMeta" style={{ marginLeft: 28 }}>
                  {t(
                    "ext.streaming.wizard.yolo_filter_enabled.hint",
                    {},
                    "When disabled, the pipeline still runs vision inference but keeps all frames.",
                  )}
                </div>
              </div>

              <div className="rowWrap" style={{ gap: 10 }}>
                <div className="field" style={{ flex: 1, minWidth: 260 }}>
                  <label className="label">{t("ext.streaming.wizard.detection_categories", {}, "Detection categories (csv)")}</label>
                  <input className="input" value={detectionCategories} onChange={(event) => setDetectionCategories(event.target.value)} placeholder="person,car" />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 260 }}>
                  <label className="label">{t("ext.streaming.wizard.tracking_categories", {}, "Tracking categories (csv)")}</label>
                  <input className="input" value={trackingCategories} onChange={(event) => setTrackingCategories(event.target.value)} placeholder="person,car" />
                </div>
              </div>

              <div className="field">
                <label className="rowWrap" style={{ gap: 10 }}>
                  <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
                  <span className="cardMeta">{t("ext.streaming.wizard.enabled", {}, "Pipeline habilitado após criação")}</span>
                </label>
              </div>

              <div className="field">
                <label className="rowWrap" style={{ gap: 10 }}>
                  <input type="checkbox" checked={useFpsReducer} onChange={(event) => setUseFpsReducer(event.target.checked)} />
                  <span className="cardMeta">{t("ext.streaming.wizard.use_fps_reducer", {}, "Inserir step core.fps_reducer")}</span>
                </label>
              </div>
            </div>
          </div>

          <div className="rowWrap" style={{ marginTop: 14, justifyContent: "flex-end", gap: 10 }}>
            <button className="chipButton" type="button" onClick={onClose} disabled={createBusy}>
              {t("core.actions.cancel", {}, "Cancelar")}
            </button>
            <button
              className="primaryButton"
              type="button"
              onClick={() => void createPipeline()}
              disabled={
                createBusy
                || !cameraId.trim()
                || hostMismatch
                || !knownProcessingServerIds.has(selectedProcessingServerId)
              }
            >
              {createBusy ? t("ext.streaming.wizard.creating", {}, "Criando…") : t("ext.streaming.wizard.create", {}, "Criar pipeline")}
            </button>
          </div>
        </div>
      ) : null}

      {step === "done" && created ? (
        <div className="streamingWizard">
          <div className="card">
            <div className="cardBody">
              <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                {t("ext.streaming.wizard.done_title", {}, "Pipeline criado")}
              </div>
              <div className="cardMeta">
                {t("ext.streaming.wizard.pipeline_created_name", {}, "Pipeline")}: {created.pipeline_name}
              </div>
              {Array.isArray(created.warnings) && created.warnings.length > 0 ? (
                <div className="cardMeta" style={{ marginTop: 8 }}>
                  {created.warnings.join(" ")}
                </div>
              ) : null}
            </div>
          </div>

          <div className="rowWrap" style={{ marginTop: 14, justifyContent: "flex-end", gap: 10 }}>
            <button className="chipButton" type="button" onClick={onClose}>
              {t("core.actions.close", {}, "Fechar")}
            </button>
            <button
              className="primaryButton"
              type="button"
              onClick={() => {
                openPipelinesScreen();
                onClose();
              }}
            >
              {t("ext.streaming.wizard.open_pipelines", {}, "Abrir Pipelines")}
            </button>
          </div>
        </div>
      ) : null}
    </SubModal>
  );
}
