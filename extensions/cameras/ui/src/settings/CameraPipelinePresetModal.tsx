import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n } from "@toposync/plugin-api";

import {
  createCameraPipelinePreset,
  fetchProcessingServerStatus,
  installProcessingServerVisionModel,
} from "../api/camerasApi";
import type {
  CameraConfig,
  CameraContextComposition,
  CameraNotificationPriority,
  CameraPipelinePreset,
  CameraPipelinesResponse,
  CameraSourceConfig,
  ProcessingServer,
} from "../types";
import { SubModal } from "../ui/SubModal";
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

type TranslateFn = ReturnType<HostI18n["useI18n"]>["t"];

const PYTHON_KEYWORDS = new Set([
  "False",
  "None",
  "True",
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
]);

function safePipelineName(value: string): string {
  const cleaned = String(value || "")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  let out = cleaned || "pipeline";
  if (!/^[A-Za-z_]/.test(out)) out = `_${out}`;
  if (PYTHON_KEYWORDS.has(out)) out = `${out}_`;
  return out.slice(0, 120);
}

function pipelineNamePart(value: string, fallback: string): string {
  const cleaned = String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
  return cleaned || fallback;
}

function presetNamePart(preset: CameraPipelinePreset): string {
  if (preset === "people_simple") return "deteccao_simples_de_pessoas";
  if (preset === "people_quiet") return "presenca_agrupada_de_pessoas";
  if (preset === "presence_area") return "presenca_agrupada_em_area";
  if (preset === "vehicle_stopped") return "veiculo_parou";
  return "evento_individual_de_pessoas";
}

function presetRequiresMapping(preset: CameraPipelinePreset): boolean {
  return preset !== "people_simple";
}

function defaultPipelineName(camera: CameraConfig, preset: CameraPipelinePreset): string {
  const cameraPart = pipelineNamePart(camera.name || camera.id, "camera");
  return safePipelineName(`${cameraPart}_${presetNamePart(preset)}`);
}

function sourceHasVideoOrigin(camera: CameraConfig, source: CameraSourceConfig | null): boolean {
  if (!source) return false;
  if (String(source.origin.rtsp_url || "").trim()) return true;
  return source.origin.type === "onvif_profile" && Boolean(String(camera.onvif?.xaddr || "").trim());
}

function normalizeServerId(value: string | null | undefined): string {
  return String(value || "local").trim().toLowerCase() || "local";
}

function serverLabel(server: ProcessingServer): string {
  const id = normalizeServerId(server.id);
  const name = String(server.name || "").trim();
  return name ? `${name} (${id})` : id;
}

function processingServerLabel(serverId: string, servers: ProcessingServer[], t: TranslateFn): string {
  const normalized = normalizeServerId(serverId);
  if (normalized === "local") return t("ext.cameras.settings.ingest.host.local", {}, "Main environment");
  const server = servers.find((item) => normalizeServerId(item.id) === normalized);
  return server ? serverLabel(server) : normalized;
}

function modelReasonLabel(reason: string, t: TranslateFn): string {
  const clean = String(reason || "").trim().toLowerCase();
  if (!clean) return t("ext.cameras.pipeline_preset.model.reason.unsupported", {}, "automatic preparation is unavailable");
  return t(`ext.cameras.pipeline_preset.model.reason.${clean}`, {}, clean.replace(/_/g, " "));
}

function modelAvailabilityLabel(item: DetectionModelCatalogItem, t: TranslateFn): string {
  if (isDetectionModelReady(item)) return t("ext.cameras.pipeline_preset.model.state.ready", {}, "ready");
  if (isActiveDetectionModelInstall(item) || item.availability === "preparing") {
    return t("ext.cameras.pipeline_preset.model.state.preparing", {}, "preparing");
  }
  if (item.availability === "incompatible") return t("ext.cameras.pipeline_preset.model.state.incompatible", {}, "incompatible");
  return t("ext.cameras.pipeline_preset.model.state.needs_prepare", {}, "needs preparation");
}

function modelOptionLabel(item: DetectionModelCatalogItem, t: TranslateFn): string {
  const badges = [modelAvailabilityLabel(item, t)];
  if (item.recommended) badges.unshift(t("ext.cameras.pipeline_preset.model.badge.recommended", {}, "recommended"));
  return `${item.displayName} (${badges.join(", ")})`;
}

function modelProgressLabel(item: DetectionModelCatalogItem, t: TranslateFn): string {
  const job = item.installJob;
  if (!job) return "";
  const phase = job.phase || job.status;
  if (job.progressPct === null) return phase;
  const pct = Math.max(0, Math.min(100, job.progressPct));
  return t("ext.cameras.pipeline_preset.model.progress", { phase, pct: pct.toFixed(0) }, "{{phase}} - {{pct}}%");
}

function modelArtifactLabel(item: DetectionModelCatalogItem | null, t: TranslateFn): string {
  if (!item) {
    return t("ext.cameras.pipeline_preset.model.artifact.unknown", {}, "Artifact status unavailable");
  }
  if (isDetectionModelReady(item) || item.artifactExists) {
    return t("ext.cameras.pipeline_preset.model.artifact.ready", {}, "Artifact present");
  }
  if (item.availability === "incompatible") {
    return t("ext.cameras.pipeline_preset.model.artifact.incompatible", {}, "Incompatible with this server");
  }
  return t("ext.cameras.pipeline_preset.model.artifact.missing", {}, "Artifact missing");
}

function modelPreparationLabel(item: DetectionModelCatalogItem | null, t: TranslateFn): string {
  if (!item) {
    return t(
      "ext.cameras.pipeline_preset.model.preparation.unavailable",
      { reason: modelReasonLabel("", t) },
      "Automatic preparation is unavailable: {{reason}}.",
    );
  }
  if (isDetectionModelReady(item)) {
    return t("ext.cameras.pipeline_preset.model.preparation.not_needed", {}, "No preparation needed.");
  }
  if (isActiveDetectionModelInstall(item) || item.availability === "preparing") {
    return t(
      "ext.cameras.pipeline_preset.model.preparation.in_progress",
      { progress: modelProgressLabel(item, t) },
      "Preparation is running. {{progress}}",
    );
  }
  if (canPrepareDetectionModel(item)) {
    return t(
      "ext.cameras.pipeline_preset.model.preparation.available",
      {},
      "Automatic preparation is available on this server.",
    );
  }
  return t(
    "ext.cameras.pipeline_preset.model.preparation.unavailable",
    { reason: modelReasonLabel(item.localBuildReason, t) },
    "Automatic preparation is unavailable: {{reason}}.",
  );
}

const VEHICLE_STOPPED_DEFAULT_SPEED_KMH = 1.0;
const NOTIFICATION_PRIORITIES: CameraNotificationPriority[] = ["low", "medium", "high"];

function compositionIdForArea(compositions: CameraContextComposition[], areaId: string): string {
  const normalizedAreaId = String(areaId || "").trim();
  if (!normalizedAreaId) return "";
  for (const composition of compositions) {
    if ((composition.areas ?? []).some((area) => area.id === normalizedAreaId)) {
      return composition.id;
    }
  }
  return "";
}

export function CameraPipelinePresetModal({
  open,
  preset,
  camera,
  activeSourceId,
  pipelineOverview,
  mappedCompositions,
  processingServers,
  i18n,
  onClose,
  onCreated,
}: {
  open: boolean;
  preset: CameraPipelinePreset | null;
  camera: CameraConfig;
  activeSourceId: string;
  pipelineOverview: CameraPipelinesResponse | null;
  mappedCompositions: CameraContextComposition[];
  processingServers: ProcessingServer[];
  i18n: HostI18n;
  onClose: () => void;
  onCreated: (pipelineName: string) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [sourceId, setSourceId] = useState("");
  const [pipelineName, setPipelineName] = useState("");
  const [suggestedName, setSuggestedName] = useState("");
  const [compositionId, setCompositionId] = useState("");
  const [areaId, setAreaId] = useState("");
  const [stoppedSpeedKmh, setStoppedSpeedKmh] = useState(VEHICLE_STOPPED_DEFAULT_SPEED_KMH);
  const [notificationPriority, setNotificationPriority] = useState<CameraNotificationPriority>("medium");
  const [enabled, setEnabled] = useState(true);
  const [processingServerId, setProcessingServerId] = useState("local");
  const [modelId, setModelId] = useState(DEFAULT_DETECTION_MODEL_ID);
  const [modelStatusPayload, setModelStatusPayload] = useState<unknown>(null);
  const [modelStatusLoading, setModelStatusLoading] = useState(false);
  const [modelStatusError, setModelStatusError] = useState<string | null>(null);
  const [modelDetailsOpen, setModelDetailsOpen] = useState(false);
  const [modelConsentOpen, setModelConsentOpen] = useState(false);
  const [modelConsentChecked, setModelConsentChecked] = useState(false);
  const [modelInstallSubmitting, setModelInstallSubmitting] = useState(false);
  const [modelInstallError, setModelInstallError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const videoSources = useMemo(
    () => camera.sources.filter((source) => source.kind === "video" && source.enabled),
    [camera.sources],
  );
  const selectedSource = videoSources.find((source) => source.id === sourceId) ?? videoSources[0] ?? null;

  useEffect(() => {
    if (!open || !preset) return;
    const nextSource =
      videoSources.find((source) => source.id === activeSourceId) ??
      videoSources.find((source) => source.is_default) ??
      videoSources[0] ??
      null;
    const nextSourceId = nextSource?.id ?? "";
    const nextSuggested =
      pipelineOverview?.suggested_pipeline_names?.[preset] ??
      defaultPipelineName(camera, preset);
    setSourceId(nextSourceId);
    setSuggestedName(nextSuggested);
    setPipelineName(nextSuggested);
    setCompositionId(mappedCompositions[0]?.id ?? "");
    setAreaId("");
    setStoppedSpeedKmh(VEHICLE_STOPPED_DEFAULT_SPEED_KMH);
    setNotificationPriority("medium");
    setEnabled(sourceHasVideoOrigin(camera, nextSource));
    setProcessingServerId("local");
    setModelId(DEFAULT_DETECTION_MODEL_ID);
    setModelStatusPayload(null);
    setModelStatusLoading(false);
    setModelStatusError(null);
    setModelDetailsOpen(false);
    setModelConsentOpen(false);
    setModelConsentChecked(false);
    setModelInstallSubmitting(false);
    setModelInstallError(null);
    setCreating(false);
    setError(null);
  }, [activeSourceId, camera, mappedCompositions, open, pipelineOverview, preset, videoSources]);

  const normalizedProcessingServerId = normalizeServerId(processingServerId);
  const detectionModels = useMemo(() => readDetectionModelCatalog(modelStatusPayload), [modelStatusPayload]);
  const selectedModel = findDetectionModel(detectionModels, modelId) ?? detectionModels[0] ?? null;
  const selectedModelReady = isDetectionModelReady(selectedModel);

  async function loadModelStatus(showLoading: boolean): Promise<void> {
    if (!open) return;
    if (showLoading) setModelStatusLoading(true);
    setModelStatusError(null);
    try {
      const payload = await fetchProcessingServerStatus(normalizedProcessingServerId);
      setModelStatusPayload(payload);
    } catch (err) {
      setModelStatusError(err instanceof Error ? err.message : String(err));
    } finally {
      if (showLoading) setModelStatusLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return;
    void loadModelStatus(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, normalizedProcessingServerId]);

  useEffect(() => {
    if (!open || !isActiveDetectionModelInstall(selectedModel)) return;
    const timer = window.setInterval(() => {
      void loadModelStatus(false);
    }, 1500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, normalizedProcessingServerId, selectedModel?.modelId, selectedModel?.installJob?.status]);

  function updateSource(nextSourceId: string): void {
    const nextSource = videoSources.find((source) => source.id === nextSourceId) ?? null;
    const nextSuggested = preset ? defaultPipelineName(camera, preset) : "";
    const previousSuggested = suggestedName;
    setSourceId(nextSourceId);
    setEnabled(sourceHasVideoOrigin(camera, nextSource));
    setSuggestedName(nextSuggested);
    if (!pipelineName.trim() || pipelineName === previousSuggested) setPipelineName(nextSuggested);
  }

  function updateArea(nextAreaId: string): void {
    setAreaId(nextAreaId);
    const nextCompositionId = compositionIdForArea(mappedCompositions, nextAreaId);
    if (nextCompositionId) setCompositionId(nextCompositionId);
  }

  async function submit(): Promise<void> {
    if (!preset || creating) return;
    if (createBlockedReasons.length) {
      setError(
        t(
          "ext.cameras.pipeline_preset.model.create_blocked",
          {},
          "Resolve the blocking items before creating this pipeline.",
        ),
      );
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const response = await createCameraPipelinePreset(camera.id, {
        preset,
        source_id: sourceId,
        pipeline_name: pipelineName.trim() && pipelineName.trim() !== suggestedName ? pipelineName.trim() : "",
        enabled,
        processing_server_id: processingServerId,
        model_id: modelId,
        composition_id: presetRequiresMapping(preset) ? compositionId : "",
        area_id: preset === "vehicle_stopped" ? areaId : "",
        stopped_speed_threshold:
          preset === "vehicle_stopped" ? Math.max(0, Number(stoppedSpeedKmh) || 0) / 3.6 : undefined,
        notification_priority: notificationPriority,
      });
      onCreated(response.pipeline_name);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  async function confirmModelInstall(): Promise<void> {
    if (!selectedModel || modelInstallSubmitting) return;
    setModelInstallSubmitting(true);
    setModelInstallError(null);
    try {
      await installProcessingServerVisionModel(normalizedProcessingServerId, selectedModel.modelId, {
        mode: "local_build",
        acknowledge_upstream_terms: true,
      });
      setModelConsentOpen(false);
      setModelConsentChecked(false);
      await loadModelStatus(false);
    } catch (err) {
      setModelInstallError(err instanceof Error ? err.message : String(err));
    } finally {
      setModelInstallSubmitting(false);
    }
  }

  if (!open || !preset) return null;

  const isMappingPreset = presetRequiresMapping(preset);
  const isVehicleStoppedPreset = preset === "vehicle_stopped";
  const title =
    preset === "people_simple"
      ? t("ext.cameras.pipeline_preset.people_simple.title", {}, "Simple detection")
      : preset === "people_quiet"
      ? t("ext.cameras.pipeline_preset.people_quiet.title", {}, "Grouped presence")
      : preset === "presence_area"
        ? t("ext.cameras.pipeline_preset.presence_area.title", {}, "Presence by area")
      : preset === "vehicle_stopped"
        ? t("ext.cameras.pipeline_preset.vehicle_stopped.title", {}, "Vehicle stopped")
        : t("ext.cameras.pipeline_preset.people_individual.title", {}, "Individual people events");
  const noSource = videoSources.length === 0;
  const noMapping = isMappingPreset && mappedCompositions.length === 0;
  const selectedServerLabel = processingServerLabel(normalizedProcessingServerId, processingServers, t);
  const selectedModelName = selectedModel?.displayName || DEFAULT_DETECTION_MODEL_NAME;
  const selectedModelId = selectedModel?.modelId || modelId || DEFAULT_DETECTION_MODEL_ID;
  const selectedModelReason = selectedModel ? modelReasonLabel(selectedModel.localBuildReason, t) : "";
  const selectedModelMissingTools = selectedModel?.localBuildMissingTools ?? [];
  const modelStatusHasPayload = Boolean(modelStatusPayload);
  const modelStatusUnavailable = Boolean(modelStatusError && !modelStatusHasPayload && !selectedModelReady);
  const modelStatusWaiting = Boolean(!modelStatusHasPayload && !modelStatusError);
  const showModelReadinessNotice = modelStatusWaiting || modelStatusUnavailable || !selectedModelReady;
  const createBlockedReasons: string[] = [];
  if (noSource) {
    createBlockedReasons.push(
      t("ext.cameras.pipeline_preset.blocked.no_source", {}, "Add an active video source before creating a pipeline."),
    );
  }
  if (noMapping) {
    createBlockedReasons.push(
      t("ext.cameras.pipeline_preset.blocked.mapping_required", {}, "Map this camera in a composition before using this preset."),
    );
  }
  if (modelStatusWaiting) {
    createBlockedReasons.push(
      t(
        "ext.cameras.pipeline_preset.blocked.model_status_loading",
        { server: selectedServerLabel },
        "Wait for model status from {{server}}.",
      ),
    );
  } else if (modelStatusUnavailable) {
    createBlockedReasons.push(
      t(
        "ext.cameras.pipeline_preset.blocked.model_status_error",
        { server: selectedServerLabel },
        "Refresh model status for {{server}} before creating this pipeline.",
      ),
    );
  } else if (!selectedModel) {
    createBlockedReasons.push(
      t("ext.cameras.pipeline_preset.blocked.no_model", {}, "Choose a detection model before creating this pipeline."),
    );
  } else if (isActiveDetectionModelInstall(selectedModel)) {
    createBlockedReasons.push(
      t(
        "ext.cameras.pipeline_preset.blocked.model_preparing",
        { model: selectedModelName, server: selectedServerLabel },
        "{{model}} is still being prepared on {{server}}.",
      ),
    );
  } else if (!selectedModelReady && canPrepareDetectionModel(selectedModel)) {
    createBlockedReasons.push(
      t(
        "ext.cameras.pipeline_preset.blocked.model_prepare_available",
        { model: selectedModelName, server: selectedServerLabel },
        "Prepare {{model}} on {{server}} before creating this pipeline.",
      ),
    );
  } else if (!selectedModelReady) {
    createBlockedReasons.push(
      t(
        "ext.cameras.pipeline_preset.blocked.model_prepare_unavailable",
        { model: selectedModelName, server: selectedServerLabel },
        "{{model}} is not ready on {{server}} and automatic preparation is unavailable.",
      ),
    );
  }
  const createDisabled = creating || createBlockedReasons.length > 0;
  const modelReadinessStatus = modelStatusWaiting
    ? t("core.ui.loading", {}, "Loading...")
    : modelStatusUnavailable
      ? t(
          "ext.cameras.pipeline_preset.model.status_unavailable",
          { server: selectedServerLabel },
          "Could not load model status from {{server}}.",
        )
      : selectedModelReady
        ? t(
            "ext.cameras.pipeline_preset.model.ready",
            { model: selectedModelName, server: selectedServerLabel },
            "{{model}} is ready on {{server}}.",
          )
        : isActiveDetectionModelInstall(selectedModel)
          ? t(
              "ext.cameras.pipeline_preset.model.preparing_status",
              { model: selectedModelName, progress: selectedModel ? modelProgressLabel(selectedModel, t) : "" },
              "{{model}} is being prepared. {{progress}}",
            )
          : canPrepareDetectionModel(selectedModel)
            ? t(
                "ext.cameras.pipeline_preset.model.missing_actionable",
                { model: selectedModelName, server: selectedServerLabel },
                "{{model}} needs preparation on {{server}} before this preset can create a pipeline.",
              )
            : t(
                "ext.cameras.pipeline_preset.model.missing_manual",
                { model: selectedModelName, server: selectedServerLabel, reason: selectedModelReason },
                "{{model}} is not ready on {{server}} and automatic preparation is unavailable: {{reason}}.",
              );

  return (
    <>
      <SubModal open={open} title={title} onClose={() => (creating ? undefined : onClose())} panelStyle={{ width: "min(760px, calc(100vw - 28px))" }}>
        <div className="settingsPanel">
          {error ? <div className="errorText">{error}</div> : null}
          {noSource ? <div className="settingsStatusMuted">{t("ext.cameras.pipelines.no_video_source", {}, "Add an active video source before creating a pipeline.")}</div> : null}
          {noMapping ? <div className="settingsStatusMuted">{t("ext.cameras.pipelines.mapping_required", {}, "Map this camera in a composition before using this preset.")}</div> : null}

        <div className="rowWrap">
          <div className="field">
            <label className="label">{t("ext.cameras.pipeline_preset.source", {}, "Camera source")}</label>
            <select className="input" value={sourceId} onChange={(event) => updateSource(event.target.value)} disabled={noSource || creating}>
              {videoSources.map((source) => (
                <option key={source.id} value={source.id}>
                  {source.name || source.id}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="label">{t("ext.cameras.pipeline_preset.processing_server", {}, "Processing server")}</label>
            <select className="input" value={processingServerId} onChange={(event) => setProcessingServerId(event.target.value)} disabled={creating}>
              <option value="local">{t("ext.cameras.settings.ingest.host.local", {}, "Main environment")}</option>
              {processingServers
                .filter((server) => normalizeServerId(server.id) !== "local")
                .map((server) => {
                  const id = normalizeServerId(server.id);
                  return (
                    <option key={id} value={id}>
                      {serverLabel(server)}
                    </option>
                  );
                })}
            </select>
          </div>
        </div>

        <div className="field">
          <label className="label">{t("ext.cameras.pipeline_preset.model.label", {}, "Detection model")}</label>
          <div className="cameraPipelineModelSelectRow">
            <select
              className="input"
              value={modelId}
              onChange={(event) => {
                setModelId(event.target.value);
                setError(null);
                setModelInstallError(null);
                setModelConsentOpen(false);
                setModelConsentChecked(false);
              }}
              disabled={creating}
            >
              {detectionModels.map((item) => (
                <option key={item.modelId} value={item.modelId}>
                  {modelOptionLabel(item, t)}
                </option>
              ))}
            </select>
            <button
              className={`iconButton${modelDetailsOpen ? " iconButtonPrimary" : ""}`}
              type="button"
              aria-label={
                modelDetailsOpen
                  ? t("ext.cameras.pipeline_preset.model.details_hide", {}, "Hide model details")
                  : t("ext.cameras.pipeline_preset.model.details_show", {}, "Show model details")
              }
              title={
                modelDetailsOpen
                  ? t("ext.cameras.pipeline_preset.model.details_hide", {}, "Hide model details")
                  : t("ext.cameras.pipeline_preset.model.details_show", {}, "Show model details")
              }
              onClick={() => setModelDetailsOpen((value) => !value)}
              disabled={creating}
            >
              <i className="fa-solid fa-circle-info" aria-hidden="true" />
            </button>
          </div>
          {showModelReadinessNotice ? (
            <div className={`cameraPipelineModelNotice${modelStatusWaiting ? "" : " isAttention"}`} role="status">
              <div className="cameraPipelineModelNoticeMain">
                <span className="cameraPipelineModelNoticeIcon" aria-hidden="true">
                  <i
                    className={`fa-solid ${
                      modelStatusWaiting || isActiveDetectionModelInstall(selectedModel)
                        ? "fa-circle-notch"
                        : "fa-triangle-exclamation"
                    }`}
                  />
                </span>
                <div>
                  <div className="settingsListTitle">
                    {modelStatusWaiting
                      ? t("ext.cameras.pipeline_preset.model.notice_checking", {}, "Checking model")
                      : isActiveDetectionModelInstall(selectedModel)
                        ? t("ext.cameras.pipeline_preset.model.notice_preparing", {}, "Preparing model")
                        : modelStatusUnavailable
                          ? t(
                              "ext.cameras.pipeline_preset.model.notice_unavailable",
                              {},
                              "Model status unavailable",
                            )
                          : t("ext.cameras.pipeline_preset.model.notice_required", {}, "Detection model required")}
                  </div>
                  <div className="settingsStatusMuted">{modelReadinessStatus}</div>
                </div>
              </div>
              {modelStatusError ? <div className="errorText">{modelStatusError}</div> : null}
              {selectedModel?.installJob?.error ? <div className="errorText">{selectedModel.installJob.error}</div> : null}
              {!selectedModelReady && selectedModelMissingTools.length ? (
                <div className="settingsStatusMuted">
                  {t(
                    "ext.cameras.pipeline_preset.model.missing_tools",
                    { tools: selectedModelMissingTools.join(", ") },
                    "Missing tools: {{tools}}",
                  )}
                </div>
              ) : null}
              {!selectedModelReady && selectedModel && !canPrepareDetectionModel(selectedModel) && !isActiveDetectionModelInstall(selectedModel) ? (
                <div className="settingsStatusMuted">
                  {t(
                    "ext.cameras.pipeline_preset.model.manual_next_step",
                    {},
                    "Choose another ready model, use another processing server, or prepare the model manually in the detection operator.",
                  )}
                </div>
              ) : null}
              <div className="rowWrap">
                {canPrepareDetectionModel(selectedModel) ? (
                  <button
                    className="primaryButton"
                    type="button"
                    disabled={modelInstallSubmitting || creating}
                    onClick={() => {
                      setModelInstallError(null);
                      setModelConsentChecked(false);
                      setModelConsentOpen(true);
                    }}
                  >
                    {t("ext.cameras.pipeline_preset.model.prepare_auto", {}, "Download and prepare automatically")}
                  </button>
                ) : null}
                {!modelStatusWaiting ? (
                  <button className="chipButton" type="button" disabled={modelStatusLoading || creating} onClick={() => void loadModelStatus(true)}>
                    {t("ext.cameras.pipeline_preset.model.refresh", {}, "Refresh models")}
                  </button>
                ) : null}
              </div>
            </div>
          ) : null}
          {modelDetailsOpen ? (
            <div className="cameraPipelineModelDetails">
              <div className="settingsList" role="list">
                <div className="settingsListItem" role="listitem">
                  <span className="settingsListTitle">
                    {t("ext.cameras.pipeline_preset.model.selected_server", {}, "Selected server")}
                  </span>
                  <span className="settingsListMeta">{selectedServerLabel}</span>
                </div>
                <div className="settingsListItem" role="listitem">
                  <span className="settingsListTitle">
                    {t("ext.cameras.pipeline_preset.model.selected_model", {}, "Selected model")}
                  </span>
                  <span className="settingsListMeta">
                    {selectedModelName} ({selectedModelId})
                  </span>
                </div>
                <div className="settingsListItem" role="listitem">
                  <span className="settingsListTitle">{t("ext.cameras.pipeline_preset.model.artifact", {}, "Artifact")}</span>
                  <span className="settingsListMeta">{modelArtifactLabel(selectedModel, t)}</span>
                </div>
                <div className="settingsListItem" role="listitem">
                  <span className="settingsListTitle">
                    {t("ext.cameras.pipeline_preset.model.preparation", {}, "Preparation")}
                  </span>
                  <span className="settingsListMeta">{modelPreparationLabel(selectedModel, t)}</span>
                </div>
              </div>
              {!showModelReadinessNotice && modelStatusError ? <div className="errorText">{modelStatusError}</div> : null}
              {!showModelReadinessNotice && selectedModel?.installJob?.error ? (
                <div className="errorText">{selectedModel.installJob.error}</div>
              ) : null}
              {!showModelReadinessNotice ? (
                <div className="rowWrap">
                  <button className="chipButton" type="button" disabled={modelStatusLoading || creating} onClick={() => void loadModelStatus(true)}>
                    {t("ext.cameras.pipeline_preset.model.refresh", {}, "Refresh models")}
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>

        {isMappingPreset ? (
          <div className="field">
            <label className="label">{t("ext.cameras.pipeline_preset.composition", {}, "Mapped composition")}</label>
            <select className="input" value={compositionId} onChange={(event) => setCompositionId(event.target.value)} disabled={noMapping || creating}>
              {mappedCompositions.map((composition) => (
                <option key={composition.id} value={composition.id}>
                  {composition.name || composition.id}
                </option>
              ))}
            </select>
          </div>
        ) : null}

        {isVehicleStoppedPreset ? (
          <div className="rowWrap">
            <div className="field">
              <label className="label">{t("ext.cameras.pipeline_preset.area", {}, "Optional area")}</label>
              <select className="input" value={areaId} onChange={(event) => updateArea(event.target.value)} disabled={noMapping || creating}>
                <option value="">
                  {t("ext.cameras.pipeline_preset.area.whole_composition", {}, "Whole mapped composition")}
                </option>
                {mappedCompositions.map((composition) => {
                  const areas = (composition.areas ?? []).filter((area) => Number(area.vertices_count || 0) >= 3);
                  if (!areas.length) return null;
                  return (
                    <optgroup key={composition.id} label={composition.name || composition.id}>
                      {areas.map((area) => (
                        <option key={`${composition.id}:${area.id}`} value={area.id}>
                          {area.name || area.id}
                        </option>
                      ))}
                    </optgroup>
                  );
                })}
              </select>
            </div>
            <div className="field">
              <label className="label">{t("ext.cameras.pipeline_preset.stopped_speed", {}, "Stopped sensitivity (km/h)")}</label>
              <input
                className="input"
                type="number"
                min="0"
                step="0.1"
                value={String(stoppedSpeedKmh)}
                onChange={(event) => {
                  const next = Number(event.target.value);
                  setStoppedSpeedKmh(Number.isFinite(next) ? next : VEHICLE_STOPPED_DEFAULT_SPEED_KMH);
                }}
                disabled={creating}
              />
            </div>
          </div>
        ) : null}

        <div className="field">
          <label className="label">{t("ext.cameras.pipeline_preset.pipeline_name", {}, "Pipeline name")}</label>
          <input className="input" value={pipelineName} onChange={(event) => setPipelineName(safePipelineName(event.target.value))} disabled={creating} />
        </div>

        <div className="field">
          <label className="label">{t("ext.cameras.pipeline_preset.notification_priority", {}, "Notification priority")}</label>
          <select
            className="input"
            value={notificationPriority}
            onChange={(event) => setNotificationPriority(event.target.value as CameraNotificationPriority)}
            disabled={creating}
          >
            {NOTIFICATION_PRIORITIES.map((priority) => (
              <option key={priority} value={priority}>
                {t(`ext.cameras.pipeline_preset.notification_priority.${priority}`, {}, priority)}
              </option>
            ))}
          </select>
        </div>

        <label className="chipButton" style={{ justifyContent: "flex-start" }}>
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={creating || !selectedSource} />
          {t("ext.cameras.pipeline_preset.enabled", {}, "Enable after creation")}
        </label>

        <div className="settingsStatusMuted">
          {preset === "people_simple"
            ? t(
                "ext.cameras.pipeline_preset.people_simple.summary",
                {},
                "Uses motion, person detection, tracking, throttling, object crops, storage and notifications without requiring mapping.",
              )
            : preset === "people_quiet"
            ? t(
                "ext.cameras.pipeline_preset.people_quiet.summary",
                {},
                "Uses motion, mapping, person/pet detection, tracking, grouped session events, throttling, crops, storage and notifications.",
              )
            : preset === "presence_area"
              ? t(
                  "ext.cameras.pipeline_preset.presence_area.summary",
                  {},
                  "Uses mapping, tracking and proximity grouping to create quieter presence notifications.",
                )
            : isVehicleStoppedPreset
              ? t(
                  "ext.cameras.pipeline_preset.vehicle_stopped.summary",
                  {},
                  "Uses motion, vehicle detection, tracking, mapping, optional area restriction, speed estimation, regular image storage and notification when the vehicle stops.",
                )
              : t(
                  "ext.cameras.pipeline_preset.people_individual.summary",
                  {},
                  "Uses motion, mapping, person detection, tracking, 10s throttling, object crops, storage and notifications.",
                )}
        </div>

        {createBlockedReasons.length ? (
          <div className="settingsStatusMuted" role="status">
            <div className="settingsListTitle">
              {t("ext.cameras.pipeline_preset.blocked.title", {}, "Resolve before creating")}
            </div>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {createBlockedReasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
          <button className="chipButton" type="button" onClick={onClose} disabled={creating}>
            {t("core.actions.cancel", {}, "Cancel")}
          </button>
          <button className="primaryButton" type="button" onClick={() => void submit()} disabled={createDisabled}>
            {creating ? t("ext.cameras.pipeline_preset.creating", {}, "Creating...") : t("ext.cameras.pipeline_preset.create", {}, "Create pipeline")}
          </button>
        </div>
      </div>
    </SubModal>
      <VisionModelConsentModal
        open={modelConsentOpen}
        serverLabel={processingServerLabel(normalizedProcessingServerId, processingServers, t)}
        modelName={selectedModelName}
        runtimeLabel={selectedModel?.localBuildRuntime ?? ""}
        sourceLabel={selectedModel?.localBuildSourceLabel ?? ""}
        checked={modelConsentChecked}
        submitting={modelInstallSubmitting}
        error={modelInstallError}
        t={t}
        onToggleChecked={setModelConsentChecked}
        onClose={() => {
          setModelConsentOpen(false);
          setModelConsentChecked(false);
          setModelInstallError(null);
        }}
        onConfirm={() => void confirmModelInstall()}
      />
    </>
  );
}
