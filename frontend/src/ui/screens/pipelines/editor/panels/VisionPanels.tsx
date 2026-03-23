import React, { useCallback, useEffect, useMemo, useState } from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";

import type {
  ProcessingServerStatus,
  ProcessingServerVisionManifestImportResponse,
} from "../../../../../util/api";
import {
  getProcessingServerStatus,
  importProcessingServerVisionManifest,
} from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";
import { pipelinesReactSelectStyles, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { SelectOption, TelemetryFieldInspectorRequest } from "../../types";
import { PipelinesNumberInput } from "../PipelinesNumberInput";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  operatorId: string;
  stepUid: string;
  nodeId: string;
  config: Record<string, unknown>;
  processingServerId: string;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
};

type VisionModelCatalogItem = {
  modelId: string;
  displayName: string;
  availability: "available" | "manifest_only" | "incompatible";
  availabilityReason: string;
  badgeIds: string[];
  runtime: string;
  sourceKind: "official" | "custom";
  custom: boolean;
  artifactExists: boolean;
  compatibleProviderIds: string[];
  inputWidth: number;
  inputHeight: number;
  classesSource: string;
  classesCount: number;
  codeLicense: string;
  weightsLicense: string;
  commercialUseStatus: string;
  resourceTier: string;
  notes: string[];
};

type VisionTaskCatalog = {
  task: string;
  profile: string;
  items: VisionModelCatalogItem[];
};

type VisionModelOption = {
  value: string;
  label: string;
  item: VisionModelCatalogItem;
  isDisabled?: boolean;
};

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function parseCatalogItem(raw: unknown): VisionModelCatalogItem | null {
  if (!isRecord(raw)) return null;
  const modelId = String(raw.model_id || "").trim();
  if (!modelId) return null;
  const input = isRecord(raw.input) ? raw.input : null;
  const classes = isRecord(raw.classes) ? raw.classes : null;
  const license = isRecord(raw.license) ? raw.license : null;
  const availability = String(raw.availability || "").trim();
  const normalizedAvailability =
    availability === "available" || availability === "manifest_only" || availability === "incompatible"
      ? availability
      : "incompatible";
  return {
    modelId,
    displayName: String(raw.display_name || raw.model_id || "").trim() || modelId,
    availability: normalizedAvailability,
    availabilityReason: String(raw.availability_reason || "").trim(),
    badgeIds: Array.isArray(raw.badge_ids) ? raw.badge_ids.map((value) => String(value || "").trim()).filter(Boolean) : [],
    runtime: String(raw.runtime || "").trim(),
    sourceKind: String(raw.source_kind || "").trim() === "custom" ? "custom" : "official",
    custom: !!raw.custom,
    artifactExists: !!raw.artifact_exists,
    compatibleProviderIds: Array.isArray(raw.compatible_provider_ids)
      ? raw.compatible_provider_ids.map((value) => String(value || "").trim()).filter(Boolean)
      : [],
    inputWidth: Number(input?.width ?? 0),
    inputHeight: Number(input?.height ?? 0),
    classesSource: String(classes?.source || "").trim(),
    classesCount: Number(classes?.count ?? 0),
    codeLicense: String(license?.code_license || "").trim(),
    weightsLicense: String(license?.weights_license || "").trim(),
    commercialUseStatus: String(license?.commercial_use_status || "").trim(),
    resourceTier: String(raw.resource_tier || "").trim(),
    notes: Array.isArray(raw.notes) ? raw.notes.map((value) => String(value || "").trim()).filter(Boolean) : [],
  };
}

function readTaskCatalog(
  status: Record<string, unknown> | undefined,
  task: "detection" | "segmentation",
): VisionTaskCatalog | null {
  if (!status || !isRecord(status)) return null;
  const vision = isRecord(status.vision) ? status.vision : null;
  const taskCatalogs = vision && isRecord(vision.task_catalogs) ? vision.task_catalogs : null;
  const catalog = taskCatalogs && isRecord(taskCatalogs[task]) ? taskCatalogs[task] : null;
  if (!catalog) return null;
  const itemsRaw = Array.isArray(catalog.items) ? catalog.items : [];
  const items = itemsRaw.map(parseCatalogItem).filter((item): item is VisionModelCatalogItem => !!item);
  return {
    task,
    profile: String(catalog.profile || "").trim(),
    items,
  };
}

function fallbackCatalogItem(
  value: string,
  label: string,
  runtime: string,
  options: { custom: boolean },
): VisionModelCatalogItem {
  const custom = !!options.custom;
  return {
    modelId: value,
    displayName: label,
    availability: "available",
    availabilityReason: "fallback",
    badgeIds: [],
    runtime,
    sourceKind: custom ? "custom" : "official",
    custom,
    artifactExists: true,
    compatibleProviderIds: [],
    inputWidth: 0,
    inputHeight: 0,
    classesSource: "",
    classesCount: 0,
    codeLicense: "",
    weightsLicense: "",
    commercialUseStatus: "",
    resourceTier: "",
    notes: [],
  };
}

const DETECTION_FALLBACK_ITEMS = [
  fallbackCatalogItem("rtmdet_det_tiny", "RTMDet Tiny", "onnxruntime", { custom: false }),
  fallbackCatalogItem("rtmdet_det_small", "RTMDet Small", "onnxruntime", { custom: false }),
  fallbackCatalogItem("rtmdet_det_medium", "RTMDet Medium", "onnxruntime", { custom: false }),
];

const SEGMENTATION_FALLBACK_ITEMS = [
  fallbackCatalogItem("rtmdet_ins_tiny", "RTMDet-Ins Tiny", "onnxruntime", { custom: false }),
  fallbackCatalogItem("rtmdet_ins_small", "RTMDet-Ins Small", "onnxruntime", { custom: false }),
  fallbackCatalogItem("rtmdet_ins_medium", "RTMDet-Ins Medium", "onnxruntime", { custom: false }),
];

const TRACKER_CHOICES = [
  {
    value: "simple_iou_kalman",
    labelKey: "core.ui.pipelines.panels.yolo.tracker_simple_iou_kalman_label",
    hintKey: "core.ui.pipelines.panels.yolo.tracker_simple_iou_kalman_hint",
  },
  {
    value: "norfair",
    labelKey: "core.ui.pipelines.panels.yolo.tracker_norfair_label",
    hintKey: "core.ui.pipelines.panels.yolo.tracker_norfair_hint",
  },
] as const;

const DETECTION_INPUT_PRESETS = [
  {
    value: "treated,original",
    labelKey: "core.ui.pipelines.panels.yolo.input_preset.treated_first",
    hintKey: "core.ui.pipelines.panels.yolo.input_preset.treated_first_hint",
  },
  {
    value: "original,treated",
    labelKey: "core.ui.pipelines.panels.yolo.input_preset.original_first",
    hintKey: "core.ui.pipelines.panels.yolo.input_preset.original_first_hint",
  },
  {
    value: "best_frame,treated,original",
    labelKey: "core.ui.pipelines.panels.yolo.input_preset.best_frame_first",
    hintKey: "core.ui.pipelines.panels.yolo.input_preset.best_frame_first_hint",
  },
] as const;

const SEGMENTATION_INPUT_PRESETS = [
  {
    value: "treated,original",
    labelKey: "core.ui.pipelines.panels.yolo.input_preset.treated_first",
    hintKey: "core.ui.pipelines.panels.yolo.input_preset.treated_first_hint",
  },
  {
    value: "original,treated",
    labelKey: "core.ui.pipelines.panels.yolo.input_preset.original_first",
    hintKey: "core.ui.pipelines.panels.yolo.input_preset.original_first_hint",
  },
] as const;

const MODEL_HINT_KEYS: Record<string, string> = {
  rtmdet_det_tiny: "core.ui.pipelines.panels.yolo.model_rtmdet_tiny_hint",
  rtmdet_det_small: "core.ui.pipelines.panels.yolo.model_rtmdet_small_hint",
  rtmdet_det_medium: "core.ui.pipelines.panels.yolo.model_rtmdet_medium_hint",
  rtmdet_ins_tiny: "core.ui.pipelines.panels.yolo.model_rtmdet_ins_tiny_hint",
  rtmdet_ins_small: "core.ui.pipelines.panels.yolo.model_rtmdet_ins_small_hint",
  rtmdet_ins_medium: "core.ui.pipelines.panels.yolo.model_rtmdet_ins_medium_hint",
};

function availabilityTranslationKey(availability: VisionModelCatalogItem["availability"]): string {
  return `core.ui.pipelines.panels.yolo.model_availability.${availability}`;
}

function availabilityReasonTranslationKey(reason: string): string {
  return `core.ui.pipelines.panels.yolo.model_availability_reason.${reason}`;
}

function resourceTierTranslationKey(value: string): string {
  return `core.ui.pipelines.panels.yolo.resource_tier.${String(value || "").trim() || "unknown"}`;
}

function modelHintTranslationKey(modelId: string): string {
  return MODEL_HINT_KEYS[String(modelId || "").trim()] || "";
}

export function VisionConfigCard({
  operatorId,
  stepUid,
  nodeId,
  config,
  processingServerId,
  showAdvanced,
  onUpdateConfig,
  onOpenTelemetryField,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const categoriesRaw = (config as any).categories;
  const categories = Array.isArray(categoriesRaw)
    ? categoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const confidenceRaw = Number((config as any).confidence_threshold ?? 0.4);
  const confidence = Number.isFinite(confidenceRaw) ? Math.max(0, Math.min(1, confidenceRaw)) : 0.4;
  const iouRaw = Number((config as any).iou_threshold ?? 0.6);
  const iou = Number.isFinite(iouRaw) ? Math.max(0, Math.min(1, iouRaw)) : 0.6;
  const defaultIntervalRaw = Number((config as any).default_interval_seconds ?? 0.2);
  const defaultInterval = Number.isFinite(defaultIntervalRaw) ? Math.max(0, Math.min(120, defaultIntervalRaw)) : 0.2;
  const closeAfterRaw = Number((config as any).close_after_seconds ?? 4.0);
  const closeAfter = Number.isFinite(closeAfterRaw) ? Math.max(0.05, Math.min(300, closeAfterRaw)) : 4.0;
  const inferenceIntervalRaw = Number((config as any).inference_interval_seconds ?? 0);
  const inferenceInterval = Number.isFinite(inferenceIntervalRaw) ? Math.max(0, Math.min(60, inferenceIntervalRaw)) : 0;
  const trackerId = String((config as any).tracker_id ?? "simple_iou_kalman").trim() || "simple_iou_kalman";
  const trackerPreset = TRACKER_CHOICES.find((item) => item.value === trackerId) ?? null;
  const emitMode = String((config as any).emit_mode ?? "events").trim() || "events";
  const pauseWhenGateClosed = Boolean((config as any).pause_when_gate_closed ?? true);
  const useWorldAnchor = Boolean((config as any).use_world_anchor ?? false);
  const modelId = String((config as any).model_id ?? "").trim();
  const inputWithFallback = String((config as any).input_with_fallback ?? "treated,original").trim() || "treated,original";
  const attachMaskArtifacts = Boolean((config as any).attach_mask_artifacts ?? true);
  const attachPolygons = Boolean((config as any).attach_polygons ?? false);
  const maxInstancesRaw = Number((config as any).max_instances_per_frame ?? 16);
  const maxInstances = Number.isFinite(maxInstancesRaw) ? Math.max(1, Math.min(512, maxInstancesRaw)) : 16;

  const isTracking = String(operatorId || "").trim() === "vision.track";
  const isSegmentation = String(operatorId || "").trim() === "vision.segment_instances";
  const isDetection = !isTracking && !isSegmentation;
  const task = isSegmentation ? "segmentation" : "detection";
  const resolvedProcessingServerId = String(processingServerId || "").trim() || "local";

  const [serverStatus, setServerStatus] = useState<ProcessingServerStatus | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [manifestText, setManifestText] = useState("");
  const [artifactPath, setArtifactPath] = useState("");
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [importLoading, setImportLoading] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importSuccess, setImportSuccess] = useState<string | null>(null);

  const reloadCatalog = useCallback(async () => {
    if (isTracking) return;
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const nextStatus = await getProcessingServerStatus(resolvedProcessingServerId);
      setServerStatus(nextStatus);
      if (!nextStatus.ok) {
        setCatalogError(String(nextStatus.error || ""));
      }
    } catch (error: any) {
      setCatalogError(String(error?.message ?? error));
      setServerStatus(null);
    } finally {
      setCatalogLoading(false);
    }
  }, [isTracking, resolvedProcessingServerId]);

  useEffect(() => {
    void reloadCatalog();
  }, [reloadCatalog]);

  const taskCatalog = useMemo(() => {
    if (!serverStatus?.ok || !serverStatus.status) return null;
    return readTaskCatalog(serverStatus.status, task);
  }, [serverStatus, task]);

  const categoryOptions = useMemo<SelectOption[]>(() => {
    const known = new Set(YOLO_CATEGORY_OPTIONS.map((item) => item.value));
    const extras = categories
      .filter((value) => !known.has(value))
      .map((value) => ({ value, label: value }));
    return [...YOLO_CATEGORY_OPTIONS, ...extras];
  }, [categories]);

  const fallbackCatalogItems = useMemo(
    () => (isSegmentation ? SEGMENTATION_FALLBACK_ITEMS : DETECTION_FALLBACK_ITEMS),
    [isSegmentation],
  );

  const catalogItems = useMemo(() => {
    const rawItems = taskCatalog?.items ?? [];
    if (rawItems.length > 0) return rawItems;
    const fallback = [...fallbackCatalogItems];
    if (modelId && !fallback.some((item) => item.modelId === modelId)) {
      fallback.unshift(fallbackCatalogItem(modelId, modelId, "onnxruntime", { custom: true }));
    }
    return fallback;
  }, [fallbackCatalogItems, modelId, taskCatalog]);

  const selectedCatalogItem = useMemo(
    () => catalogItems.find((item) => item.modelId === modelId) ?? null,
    [catalogItems, modelId],
  );

  const modelOptions = useMemo<VisionModelOption[]>(() => {
    const hasAvailable = catalogItems.some((item) => item.availability === "available");
    const visibleItems = showAdvanced
      ? catalogItems
      : hasAvailable
      ? catalogItems.filter((item) => item.availability === "available" || item.modelId === modelId)
      : catalogItems;
    return visibleItems.map((item) => {
      const badges = item.badgeIds.map((badgeId) => t(`core.ui.processing_servers.vision_recommendations.badge.${badgeId}`, {}, badgeId));
      const availabilityText =
        item.availability === "available"
          ? ""
          : ` • ${t(availabilityTranslationKey(item.availability), {}, item.availability)}`;
      const badgeText = badges.length ? ` • ${badges.join(" • ")}` : "";
      const customText = item.custom ? ` • ${t("core.ui.pipelines.panels.yolo.model_custom_badge")}` : "";
      return {
        value: item.modelId,
        label: `${item.displayName}${badgeText}${customText}${availabilityText}`,
        item,
        isDisabled: item.availability !== "available" && item.modelId !== modelId,
      };
    });
  }, [catalogItems, modelId, showAdvanced, t]);

  const unavailableItems = useMemo(
    () => catalogItems.filter((item) => item.availability !== "available" && item.modelId !== modelId),
    [catalogItems, modelId],
  );

  const selectedModelOption = useMemo(
    () => modelOptions.find((item) => item.value === modelId) ?? null,
    [modelId, modelOptions],
  );

  const selectedModelHintKey = useMemo(() => modelHintTranslationKey(modelId), [modelId]);

  const trackerOptions = useMemo<SelectOption[]>(
    () =>
      TRACKER_CHOICES.map((item) => ({
        value: item.value,
        label: t(item.labelKey, {}, item.value),
      })),
    [t],
  );

  const selectedTrackerOption = useMemo<SelectOption | null>(
    () => trackerOptions.find((item) => item.value === trackerId) ?? null,
    [trackerId, trackerOptions],
  );

  const inputPresetChoices = useMemo(
    () => (isSegmentation ? SEGMENTATION_INPUT_PRESETS : DETECTION_INPUT_PRESETS),
    [isSegmentation],
  );

  const inputPresetOptions = useMemo<SelectOption[]>(() => {
    const options = inputPresetChoices.map((item) => ({
      value: item.value,
      label: t(item.labelKey, {}, item.value),
    }));
    if (!inputWithFallback || options.some((item) => item.value === inputWithFallback)) {
      return options;
    }
    return [
      {
        value: inputWithFallback,
        label: t(
          "core.ui.pipelines.panels.yolo.input_preset.custom_current",
          { value: inputWithFallback },
          `Custom: ${inputWithFallback}`,
        ),
      },
      ...options,
    ];
  }, [inputPresetChoices, inputWithFallback, t]);

  const selectedInputPreset = useMemo<SelectOption | null>(
    () => inputPresetOptions.find((item) => item.value === inputWithFallback) ?? null,
    [inputPresetOptions, inputWithFallback],
  );

  const selectedInputPresetHintKey = useMemo(
    () => inputPresetChoices.find((item) => item.value === inputWithFallback)?.hintKey ?? "",
    [inputPresetChoices, inputWithFallback],
  );

  const handleImport = useCallback(async () => {
    setImportLoading(true);
    setImportError(null);
    setImportSuccess(null);
    try {
      const result: ProcessingServerVisionManifestImportResponse = await importProcessingServerVisionManifest(
        resolvedProcessingServerId,
        {
          manifest_text: manifestText,
          artifact_path: artifactPath,
          replace_existing: replaceExisting,
        },
      );
      setImportSuccess(
        t(
          "core.ui.pipelines.panels.yolo.import_success",
          { modelId: result.model_id, task: result.task },
          `Imported ${result.model_id}`,
        ),
      );
      if ((isSegmentation && result.task === "segmentation") || (isDetection && result.task === "detection")) {
        onUpdateConfig((prev) => ({
          ...prev,
          model_id: result.model_id,
          ...(isDetection ? { emit_mode: "annotate" } : {}),
        }));
      }
      await reloadCatalog();
    } catch (error: any) {
      setImportError(String(error?.message ?? error));
    } finally {
      setImportLoading(false);
    }
  }, [
    artifactPath,
    isDetection,
    isSegmentation,
    manifestText,
    onUpdateConfig,
    reloadCatalog,
    replaceExisting,
    resolvedProcessingServerId,
    t,
  ]);

  return (
    <div className="pipelinesOperatorConfigCard">
      {isTracking ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.tracker_id")}</span>
            <Select<SelectOption, false>
              styles={pipelinesReactSelectStyles as any}
              options={trackerOptions}
              value={selectedTrackerOption}
              onChange={(value: SingleValue<SelectOption>) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  tracker_id: String(value?.value || "simple_iou_kalman").trim() || "simple_iou_kalman",
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.tracker_id_hint")}</div>
          {trackerPreset ? <div className="pipelinesStepHint">{t(trackerPreset.hintKey)}</div> : null}

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.track_emit_mode")}</span>
            <select
              className="pipelinesInput"
              value={emitMode}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  emit_mode: event.target.value,
                }));
              }}
            >
              <option value="events">{t("core.ui.pipelines.panels.yolo.track_emit_mode.events")}</option>
              <option value="annotate">{t("core.ui.pipelines.panels.yolo.track_emit_mode.annotate")}</option>
            </select>
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.track_emit_mode_hint")}</div>
        </>
      ) : (
        <>
          <div className="pipelinesStepHint">
            {t("core.ui.pipelines.panels.yolo.processing_server_hint", {
              serverId: resolvedProcessingServerId,
            })}
          </div>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.model_id")}</span>
            <Select<VisionModelOption, false>
              styles={pipelinesReactSelectStyles as any}
              options={modelOptions}
              value={selectedModelOption}
              isLoading={catalogLoading}
              isOptionDisabled={(option) => !!option.isDisabled}
              placeholder={t("core.ui.pipelines.panels.yolo.model_select_placeholder")}
              onChange={(value: SingleValue<VisionModelOption>) => {
                const nextModelId = String(value?.value || "").trim();
                onUpdateConfig((prev) => ({
                  ...prev,
                  model_id: nextModelId,
                  ...(isDetection ? { emit_mode: "annotate" } : {}),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">
            {isSegmentation
              ? t("core.ui.pipelines.panels.yolo.segmentation_model_id_hint")
              : t("core.ui.pipelines.panels.yolo.model_id_hint")}
          </div>
          <div className="pipelinesStepHint">
            {isSegmentation
              ? t("core.ui.pipelines.panels.yolo.segmentation_model_shortlist_hint")
              : t("core.ui.pipelines.panels.yolo.model_shortlist_hint")}
          </div>
          {taskCatalog?.profile ? (
            <div className="pipelinesStepHint">
              {t("core.ui.pipelines.panels.yolo.profile_hint", {
                profile: t(
                  `core.ui.processing_servers.vision_recommendations.profile_label.${taskCatalog.profile}`,
                  {},
                  taskCatalog.profile,
                ),
              })}
            </div>
          ) : null}
          {selectedCatalogItem?.badgeIds.length ? (
            <div className="pipelinesStepHint">
              {selectedCatalogItem.badgeIds
                .map((badgeId) => t(`core.ui.processing_servers.vision_recommendations.badge.${badgeId}`, {}, badgeId))
                .join(" • ")}
            </div>
          ) : null}
          {selectedModelHintKey ? <div className="pipelinesStepHint">{t(selectedModelHintKey)}</div> : null}
          {selectedCatalogItem ? (
            <div className="pipelinesStepHint">
              {t(availabilityTranslationKey(selectedCatalogItem.availability), {}, selectedCatalogItem.availability)}
              {selectedCatalogItem.availabilityReason ? ` • ${t(availabilityReasonTranslationKey(selectedCatalogItem.availabilityReason), {}, selectedCatalogItem.availabilityReason)}` : ""}
            </div>
          ) : null}
          {catalogError ? <div className="errorText">{catalogError}</div> : null}
          {!showAdvanced && unavailableItems.length > 0 ? (
            <div className="pipelinesStepHint">
              {t("core.ui.pipelines.panels.yolo.hidden_unavailable_count", { count: unavailableItems.length })}
            </div>
          ) : null}

          <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <button className="pillButton" type="button" onClick={() => void reloadCatalog()} disabled={catalogLoading}>
              {t("core.ui.pipelines.panels.yolo.refresh_models")}
            </button>
            {showAdvanced ? (
              <button
                className="pillButton"
                type="button"
                onClick={() => {
                  setShowImport((prev) => !prev);
                  setImportError(null);
                  setImportSuccess(null);
                }}
              >
                {t("core.ui.pipelines.panels.yolo.import_manifest")}
              </button>
            ) : null}
          </div>

          {showAdvanced && showImport ? (
            <div className="pipelinesOperatorConfigCard" style={{ marginTop: 10 }}>
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.import_manifest_text")}</span>
                <textarea
                  className="pipelinesTextArea"
                  rows={8}
                  value={manifestText}
                  placeholder='{"model_id":"custom_det","task":"detection","runtime":"onnxruntime"}'
                  onChange={(event) => setManifestText(event.target.value)}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.import_manifest_text_hint")}</div>

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.import_artifact_path")}</span>
                <input
                  className="pipelinesInput"
                  type="text"
                  value={artifactPath}
                  placeholder="/models/custom/model.onnx"
                  onChange={(event) => setArtifactPath(event.target.value)}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.import_artifact_path_hint")}</div>

              <label className="pipelinesCheckboxLabel">
                <input type="checkbox" checked={replaceExisting} onChange={(event) => setReplaceExisting(event.target.checked)} />
                <span>{t("core.ui.pipelines.panels.yolo.import_replace_existing")}</span>
              </label>

              {importError ? <div className="errorText">{importError}</div> : null}
              {importSuccess ? <div className="settingsStatusMuted">{importSuccess}</div> : null}

              <div className="row" style={{ gap: 8, marginTop: 8 }}>
                <button className="pillButton pillButtonPrimary" type="button" onClick={() => void handleImport()} disabled={importLoading}>
                  {importLoading
                    ? t("core.ui.pipelines.panels.yolo.importing_manifest")
                    : t("core.ui.pipelines.panels.yolo.apply_import_manifest")}
                </button>
              </div>
            </div>
          ) : null}

          {showAdvanced && selectedCatalogItem ? (
            <div className="pipelinesOperatorConfigCard" style={{ marginTop: 10 }}>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.selected_model_details")}</div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_runtime", { runtime: selectedCatalogItem.runtime || "onnxruntime" })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_input", {
                  width: selectedCatalogItem.inputWidth || 0,
                  height: selectedCatalogItem.inputHeight || 0,
                })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_classes", {
                  count: selectedCatalogItem.classesCount || 0,
                  source: selectedCatalogItem.classesSource || "n/a",
                })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_providers", {
                  providers: selectedCatalogItem.compatibleProviderIds.join(", ") || "CPUExecutionProvider",
                })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_license", {
                  code: selectedCatalogItem.codeLicense || "n/a",
                  weights: selectedCatalogItem.weightsLicense || "n/a",
                })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_commercial", {
                  status: selectedCatalogItem.commercialUseStatus || "n/a",
                })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_resource", {
                  tier: t(resourceTierTranslationKey(selectedCatalogItem.resourceTier), {}, selectedCatalogItem.resourceTier || "unknown"),
                })}
              </div>
              <div className="pipelinesStepHint">
                {selectedCatalogItem.custom
                  ? t("core.ui.pipelines.panels.yolo.details_source_custom")
                  : t("core.ui.pipelines.panels.yolo.details_source_official")}
              </div>
              {selectedCatalogItem.notes.map((note, index) => (
                <div key={`${selectedCatalogItem.modelId}:note:${index}`} className="pipelinesStepHint">
                  {note}
                </div>
              ))}
            </div>
          ) : null}
        </>
      )}

      {isDetection ? (
        <>
          <label className="pipelinesLabel">
            <div className="pipelinesScalarLabelHeader">
              <span>{t("core.ui.pipelines.panels.yolo.min_confidence")}</span>
              {onOpenTelemetryField ? (
                <button
                  className="iconButton pipelinesTelemetryFieldButton"
                  type="button"
                  title={t("core.ui.pipelines.telemetry.field.open_histogram")}
                  onClick={() =>
                    onOpenTelemetryField({
                      stepUid,
                      nodeId,
                      operatorId,
                      configKey: "confidence_threshold",
                      metricId: "vision.confidence",
                      label: t("core.ui.pipelines.panels.yolo.min_confidence"),
                      value: confidence,
                    })
                  }
                >
                  <i className="fa-solid fa-chart-column" aria-hidden="true" />
                </button>
              ) : null}
            </div>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1}
              step={0.01}
              value={confidence}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  confidence_threshold: Math.max(0, Math.min(1, nextValue)),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.min_confidence_hint")}</div>
        </>
      ) : null}

      {!isTracking ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.categories")}</span>
            <Select<SelectOption, true>
              isMulti
              styles={pipelinesReactSelectStyles}
              options={categoryOptions}
              value={categories.map((value) => categoryOptions.find((opt) => opt.value === value) ?? { value, label: value })}
              placeholder={t("core.ui.pipelines.panels.yolo.categories_placeholder")}
              onChange={(value: MultiValue<SelectOption>) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  categories: value.map((item) => item.value),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">
            {isSegmentation
              ? t("core.ui.pipelines.panels.yolo.segmentation_categories_hint")
              : t("core.ui.pipelines.panels.yolo.categories_hint")}
          </div>
        </>
      ) : null}

      {isDetection && showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.iou_threshold")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1}
              step={0.01}
              value={iou}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  iou_threshold: Math.max(0, Math.min(1, nextValue)),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.iou_threshold_hint")}</div>
        </>
      ) : null}

      {isTracking ? (
        <>
          {showAdvanced ? (
            <>
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.update_interval_tracking")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0}
                  max={120}
                  step={0.05}
                  value={defaultInterval}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      default_interval_seconds: Math.max(0, Math.min(120, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.update_interval_hint")}</div>

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.close_after_seconds")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0.05}
                  max={300}
                  step={0.1}
                  value={closeAfter}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      close_after_seconds: Math.max(0.05, Math.min(300, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.close_after_hint")}</div>

              <label className="pipelinesCheckboxLabel">
                <input
                  type="checkbox"
                  checked={pauseWhenGateClosed}
                  onChange={(event) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      pause_when_gate_closed: event.target.checked,
                    }));
                  }}
                />
                <span>{t("core.ui.pipelines.panels.yolo.pause_when_gate_closed")}</span>
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.pause_when_gate_closed_hint")}</div>

              <label className="pipelinesCheckboxLabel">
                <input
                  type="checkbox"
                  checked={useWorldAnchor}
                  onChange={(event) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      use_world_anchor: event.target.checked,
                    }));
                  }}
                />
                <span>{t("core.ui.pipelines.panels.yolo.use_world_anchor")}</span>
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.use_world_anchor_hint")}</div>
            </>
          ) : null}
        </>
      ) : null}

      {isDetection ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.input_source")}</span>
            <Select<SelectOption, false>
              styles={pipelinesReactSelectStyles as any}
              options={inputPresetOptions}
              value={selectedInputPreset}
              onChange={(value: SingleValue<SelectOption>) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  input_with_fallback: String(value?.value || "treated,original").trim() || "treated,original",
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.input_source_hint")}</div>
          {selectedInputPresetHintKey ? <div className="pipelinesStepHint">{t(selectedInputPresetHintKey)}</div> : null}
          {showAdvanced ? (
            <>
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.inference_interval_detection")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0}
                  max={60}
                  step={0.05}
                  value={inferenceInterval}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      inference_interval_seconds: Math.max(0, Math.min(60, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.inference_interval_hint")}</div>

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.input_with_fallback")}</span>
                <input
                  className="pipelinesInput"
                  type="text"
                  value={inputWithFallback}
                  onChange={(event) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      input_with_fallback: event.target.value,
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.input_with_fallback_hint")}</div>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.detect_annotate_only_hint")}</div>
            </>
          ) : null}
        </>
      ) : null}

      {isSegmentation ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.input_source")}</span>
            <Select<SelectOption, false>
              styles={pipelinesReactSelectStyles as any}
              options={inputPresetOptions}
              value={selectedInputPreset}
              onChange={(value: SingleValue<SelectOption>) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  input_with_fallback: String(value?.value || "treated,original").trim() || "treated,original",
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.input_source_hint")}</div>
          {selectedInputPresetHintKey ? <div className="pipelinesStepHint">{t(selectedInputPresetHintKey)}</div> : null}

          {showAdvanced ? (
            <>
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.input_with_fallback")}</span>
                <input
                  className="pipelinesInput"
                  type="text"
                  value={inputWithFallback}
                  onChange={(event) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      input_with_fallback: event.target.value,
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.segmentation_input_with_fallback_hint")}</div>

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.max_instances_per_frame")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={1}
                  max={512}
                  step={1}
                  value={maxInstances}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      max_instances_per_frame: Math.max(1, Math.min(512, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.max_instances_per_frame_hint")}</div>

              <label className="pipelinesCheckboxLabel">
                <input
                  type="checkbox"
                  checked={attachMaskArtifacts}
                  onChange={(event) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      attach_mask_artifacts: event.target.checked,
                    }));
                  }}
                />
                <span>{t("core.ui.pipelines.panels.yolo.attach_mask_artifacts")}</span>
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.attach_mask_artifacts_hint")}</div>

              <label className="pipelinesCheckboxLabel">
                <input
                  type="checkbox"
                  checked={attachPolygons}
                  onChange={(event) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      attach_polygons: event.target.checked,
                    }));
                  }}
                />
                <span>{t("core.ui.pipelines.panels.yolo.attach_polygons")}</span>
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.attach_polygons_hint")}</div>
            </>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
