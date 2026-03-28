import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";

import type {
  ProcessingServerStatus,
  ProcessingServerVisionModelArtifactUploadResponse,
  ProcessingServerVisionManifestImportResponse,
} from "../../../../../util/api";
import {
  getProcessingServerStatus,
  importProcessingServerVisionManifest,
  uploadProcessingServerVisionModelArtifact,
} from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";
import { Modal } from "../../../../Modal";
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
  onOpenProcessingServers?: () => void;
};

type VisionModelCatalogItem = {
  modelId: string;
  displayName: string;
  artifactPath: string;
  availability: "available" | "manifest_only" | "incompatible";
  availabilityReason: string;
  badgeIds: string[];
  runtime: string;
  sourceKind: "official" | "custom";
  custom: boolean;
  artifactExists: boolean;
  acquisitionMode: "guided_upload" | "auto_download";
  acquisitionSupported: boolean;
  acquisitionReason: string;
  acquisitionSourceKind: string;
  acquisitionSourceLabel: string;
  acquisitionArtifactSource: "onnx_ready" | "checkpoint_export_required";
  acquisition: VisionModelAcquisition;
  installSupported: boolean;
  installReason: string;
  installSourceKind: string;
  installSourceLabel: string;
  installJob: VisionModelInstallJob | null;
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

type VisionModelAcquisition = {
  mode: "guided_upload" | "auto_download";
  artifactSource: "onnx_ready" | "checkpoint_export_required";
  guideUrl: string;
  exportGuideUrl: string;
  sourceUrl: string;
};

type VisionModelInstallJob = {
  jobId: string;
  modelId: string;
  displayName: string;
  status: string;
  phase: string;
  progressPct: number;
  bytesCompleted: number;
  bytesTotal: number;
  error: string;
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

function parseInstallJob(raw: unknown): VisionModelInstallJob | null {
  if (!isRecord(raw)) return null;
  const jobId = String(raw.job_id || "").trim();
  const modelId = String(raw.model_id || "").trim();
  if (!jobId || !modelId) return null;
  return {
    jobId,
    modelId,
    displayName: String(raw.display_name || raw.model_id || "").trim() || modelId,
    status: String(raw.status || "").trim(),
    phase: String(raw.phase || "").trim(),
    progressPct: Number(raw.progress_pct ?? 0),
    bytesCompleted: Number(raw.bytes_completed ?? 0),
    bytesTotal: Number(raw.bytes_total ?? 0),
    error: String(raw.error || "").trim(),
  };
}

function parseCatalogItem(raw: unknown): VisionModelCatalogItem | null {
  if (!isRecord(raw)) return null;
  const modelId = String(raw.model_id || "").trim();
  if (!modelId) return null;
  const input = isRecord(raw.input) ? raw.input : null;
  const classes = isRecord(raw.classes) ? raw.classes : null;
  const license = isRecord(raw.license) ? raw.license : null;
  const acquisition = isRecord(raw.acquisition) ? raw.acquisition : null;
  const availability = String(raw.availability || "").trim();
  const normalizedAvailability =
    availability === "available" || availability === "manifest_only" || availability === "incompatible"
      ? availability
      : "incompatible";
  const rawArtifactSource = String(raw.acquisition_artifact_source || acquisition?.artifact_source || "onnx_ready").trim();
  const artifactSource =
    rawArtifactSource === "checkpoint_export_required" ? "checkpoint_export_required" : "onnx_ready";
  const rawMode = String(raw.acquisition_mode || acquisition?.mode || "guided_upload").trim();
  const mode = rawMode === "auto_download" ? "auto_download" : "guided_upload";
  return {
    modelId,
    displayName: String(raw.display_name || raw.model_id || "").trim() || modelId,
    artifactPath: String(raw.artifact_path || "").trim(),
    availability: normalizedAvailability,
    availabilityReason: String(raw.availability_reason || "").trim(),
    badgeIds: Array.isArray(raw.badge_ids) ? raw.badge_ids.map((value) => String(value || "").trim()).filter(Boolean) : [],
    runtime: String(raw.runtime || "").trim(),
    sourceKind: String(raw.source_kind || "").trim() === "custom" ? "custom" : "official",
    custom: !!raw.custom,
    artifactExists: !!raw.artifact_exists,
    acquisitionMode: mode,
    acquisitionSupported: !!raw.acquisition_supported,
    acquisitionReason: String(raw.acquisition_reason || "").trim(),
    acquisitionSourceKind: String(raw.acquisition_source_kind || "").trim(),
    acquisitionSourceLabel: String(raw.acquisition_source_label || "").trim(),
    acquisitionArtifactSource: artifactSource,
    acquisition: {
      mode,
      artifactSource,
      guideUrl: String(acquisition?.guide_url || "").trim(),
      exportGuideUrl: String(acquisition?.export_guide_url || "").trim(),
      sourceUrl: String(acquisition?.source_url || "").trim(),
    },
    installSupported: !!raw.install_supported,
    installReason: String(raw.install_reason || "").trim(),
    installSourceKind: String(raw.install_source_kind || "").trim(),
    installSourceLabel: String(raw.install_source_label || "").trim(),
    installJob: parseInstallJob(raw.install_job),
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
  const acquisition = defaultAcquisitionForModelId(value);
  return {
    modelId: value,
    displayName: label,
    artifactPath: "",
    availability: "available",
    availabilityReason: "fallback",
    badgeIds: [],
    runtime,
    sourceKind: custom ? "custom" : "official",
    custom,
    artifactExists: true,
    acquisitionMode: acquisition.mode,
    acquisitionSupported: acquisition.mode === "guided_upload",
    acquisitionReason: acquisition.mode === "guided_upload" ? "guided_upload_ready" : "",
    acquisitionSourceKind: "",
    acquisitionSourceLabel: "",
    acquisitionArtifactSource: acquisition.artifactSource,
    acquisition,
    installSupported: false,
    installReason: "",
    installSourceKind: "",
    installSourceLabel: "",
    installJob: null,
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

const BASIC_CATEGORY_VALUES = ["person", "car", "truck", "bus", "bicycle", "motorcycle", "dog", "cat"];
const BASIC_CATEGORY_LABEL_KEYS: Record<string, string> = {
  person: "core.ui.pipelines.panels.yolo.category.person",
  car: "core.ui.pipelines.panels.yolo.category.car",
  truck: "core.ui.pipelines.panels.yolo.category.truck",
  bus: "core.ui.pipelines.panels.yolo.category.bus",
  bicycle: "core.ui.pipelines.panels.yolo.category.bicycle",
  motorcycle: "core.ui.pipelines.panels.yolo.category.motorcycle",
  dog: "core.ui.pipelines.panels.yolo.category.dog",
  cat: "core.ui.pipelines.panels.yolo.category.cat",
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

function formatProgressBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  const kb = bytes / 1024;
  if (kb >= 1) return `${kb.toFixed(kb >= 10 ? 0 : 1)} KB`;
  return `${bytes.toFixed(0)} B`;
}

function artifactFileName(artifactPath: string): string {
  const clean = String(artifactPath || "").trim();
  if (!clean) return "";
  const normalized = clean.replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] || clean;
}

function defaultAcquisitionForModelId(modelId: string): VisionModelAcquisition {
  const clean = String(modelId || "").trim().toLowerCase();
  if (clean.startsWith("rtmdet_det_") || clean.startsWith("rtmdet_ins_")) {
    return {
      mode: "guided_upload",
      artifactSource: "checkpoint_export_required",
      guideUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/README.md",
      exportGuideUrl: "https://mmdeploy.readthedocs.io/en/latest/01-how-to-build/build_from_docker.html",
      sourceUrl: "",
    };
  }
  return {
    mode: "guided_upload",
    artifactSource: "onnx_ready",
    guideUrl: "",
    exportGuideUrl: "",
    sourceUrl: "",
  };
}

function normalizeArtifactUploadError(
  error: unknown,
  t: (key: string, vars?: Record<string, unknown>, fallback?: string) => string,
  item: VisionModelCatalogItem | null,
): string {
  const raw = String((error as any)?.message ?? error ?? "").trim();
  if (!raw) {
    return t(
      "core.ui.pipelines.panels.yolo.artifact_modal.upload_error_generic",
      { model: item?.displayName || "" },
      "Could not validate this file for the selected model.",
    );
  }
  const lower = raw.toLowerCase();
  if (lower.includes("does not match the selected model") || lower.includes("checksum mismatch")) {
    return t(
      "core.ui.pipelines.panels.yolo.artifact_modal.upload_error_mismatch",
      { model: item?.displayName || "" },
      "This file does not match the selected model. Check that you chose the correct ONNX file.",
    );
  }
  if (lower.includes("checkpoint") && lower.includes("exported .onnx")) {
    return t(
      "core.ui.pipelines.panels.yolo.artifact_modal.upload_error_checkpoint_generic",
      {},
      "You selected a checkpoint file. This step still needs the exported .onnx file.",
    );
  }
  return raw;
}

function pickSuggestedAvailableModel(items: VisionModelCatalogItem[]): VisionModelCatalogItem | null {
  if (!items.length) return null;
  const preferredBadges = ["recommended", "fastest", "best_quality", "edge"];
  for (const badgeId of preferredBadges) {
    const match = items.find((item) => item.badgeIds.includes(badgeId));
    if (match) return match;
  }
  return items[0] ?? null;
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
  onOpenProcessingServers,
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
  const detectionFilterFrames = String((config as any).emit_mode ?? "events").trim().toLowerCase() !== "annotate";
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
  const [artifactModalItem, setArtifactModalItem] = useState<VisionModelCatalogItem | null>(null);
  const [artifactModalFile, setArtifactModalFile] = useState<File | null>(null);
  const [artifactModalDragActive, setArtifactModalDragActive] = useState(false);
  const [artifactUploadLoading, setArtifactUploadLoading] = useState(false);
  const [artifactUploadProgressPct, setArtifactUploadProgressPct] = useState(0);
  const [artifactUploadProgressBytes, setArtifactUploadProgressBytes] = useState<{ uploaded: number; total: number }>({
    uploaded: 0,
    total: 0,
  });
  const [artifactUploadError, setArtifactUploadError] = useState<string | null>(null);
  const [artifactUploadSuccess, setArtifactUploadSuccess] = useState<string | null>(null);
  const artifactFileInputRef = useRef<HTMLInputElement | null>(null);

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
  const basicCategoryValues = useMemo(() => {
    const values = [...BASIC_CATEGORY_VALUES, ...categories.filter((value) => !BASIC_CATEGORY_VALUES.includes(value))];
    return values.slice(0, 12);
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
  const availableItems = useMemo(
    () => catalogItems.filter((item) => item.availability === "available"),
    [catalogItems],
  );
  const suggestedAvailableItem = useMemo(() => pickSuggestedAvailableModel(availableItems), [availableItems]);
  const selectedModelNeedsInstall = selectedCatalogItem?.availability === "manifest_only";
  const selectedModelIncompatible = selectedCatalogItem?.availability === "incompatible";
  const noReadyModels = !isTracking && !catalogLoading && availableItems.length === 0;
  const showModelRecoveryCard = !isTracking && (!!selectedModelNeedsInstall || !!selectedModelIncompatible || noReadyModels);
  const basicModelItems = useMemo(() => {
    const next: VisionModelCatalogItem[] = [];
    const seen = new Set<string>();
    for (const item of [...availableItems, selectedCatalogItem].filter(Boolean) as VisionModelCatalogItem[]) {
      if (seen.has(item.modelId)) continue;
      next.push(item);
      seen.add(item.modelId);
    }
    return next.slice(0, 4);
  }, [availableItems, selectedCatalogItem]);
  const manualInstallItem = selectedCatalogItem ?? null;
  const manualInstallFile = artifactFileName(manualInstallItem?.artifactPath || "");
  const manualInstallAcquisition = manualInstallItem?.acquisition ?? defaultAcquisitionForModelId(manualInstallItem?.modelId || "");
  const manualInstallNeedsExport = manualInstallAcquisition.artifactSource === "checkpoint_export_required";

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

  const applySuggestedAvailableModel = useCallback(() => {
    if (!suggestedAvailableItem) return;
    onUpdateConfig((prev) => ({
      ...prev,
      model_id: suggestedAvailableItem.modelId,
    }));
  }, [onUpdateConfig, suggestedAvailableItem]);

  const openArtifactModal = useCallback((item: VisionModelCatalogItem | null) => {
    if (!item) return;
    setArtifactModalItem(item);
    setArtifactModalFile(null);
    setArtifactModalDragActive(false);
    setArtifactUploadLoading(false);
    setArtifactUploadProgressPct(0);
    setArtifactUploadProgressBytes({ uploaded: 0, total: 0 });
    setArtifactUploadError(null);
    setArtifactUploadSuccess(null);
  }, []);

  const closeArtifactModal = useCallback(() => {
    if (artifactUploadLoading) return;
    setArtifactModalItem(null);
    setArtifactModalFile(null);
    setArtifactModalDragActive(false);
    setArtifactUploadProgressPct(0);
    setArtifactUploadProgressBytes({ uploaded: 0, total: 0 });
    setArtifactUploadError(null);
    setArtifactUploadSuccess(null);
  }, [artifactUploadLoading]);

  const handleArtifactFileChosen = useCallback((file: File | null) => {
    if (!file) return;
    const fileName = String(file.name || "").trim();
    const fileNameLower = fileName.toLowerCase();
    const acquisition = artifactModalItem?.acquisition ?? defaultAcquisitionForModelId(artifactModalItem?.modelId || "");
    if (!fileNameLower.endsWith(".onnx")) {
      setArtifactModalFile(null);
      setArtifactUploadSuccess(null);
      if (fileNameLower.endsWith(".pth") || fileNameLower.endsWith(".pt") || fileNameLower.endsWith(".ckpt")) {
        setArtifactUploadError(
          t(
            "core.ui.pipelines.panels.yolo.artifact_modal.upload_error_checkpoint_selected",
            { file: fileName },
            "You selected a checkpoint file. This step still needs the exported .onnx file.",
          ),
        );
        return;
      }
      setArtifactUploadError(
        t(
          "core.ui.pipelines.panels.yolo.artifact_modal.upload_error_extension",
          {
            fileType:
              acquisition.artifactSource === "checkpoint_export_required" ? ".onnx (exported from the checkpoint)" : ".onnx",
          },
          "This step only accepts .onnx files. If you downloaded a checkpoint like .pth, export it to ONNX first.",
        ),
      );
      return;
    }
    setArtifactModalFile(file);
    setArtifactUploadError(null);
    setArtifactUploadSuccess(null);
  }, [artifactModalItem, t]);

  const handleArtifactUpload = useCallback(async () => {
    if (!artifactModalItem || !artifactModalFile) return;
    setArtifactUploadLoading(true);
    setArtifactUploadError(null);
    setArtifactUploadSuccess(null);
    setArtifactUploadProgressPct(0);
    setArtifactUploadProgressBytes({ uploaded: 0, total: artifactModalFile.size || 0 });
    try {
      const result: ProcessingServerVisionModelArtifactUploadResponse = await uploadProcessingServerVisionModelArtifact(
        resolvedProcessingServerId,
        artifactModalItem.modelId,
        artifactModalFile,
        {
          onProgress: (progressPct, bytesUploaded, bytesTotal) => {
            setArtifactUploadProgressPct(progressPct);
            setArtifactUploadProgressBytes({ uploaded: bytesUploaded, total: bytesTotal });
          },
        },
      );
      setArtifactUploadSuccess(
        t(
          result.replaced
            ? "core.ui.pipelines.panels.yolo.artifact_modal.upload_success_replaced"
            : "core.ui.pipelines.panels.yolo.artifact_modal.upload_success_added",
          { model: result.display_name || artifactModalItem.displayName },
          result.replaced
            ? `Updated ${result.display_name || artifactModalItem.displayName}`
            : `Added ${result.display_name || artifactModalItem.displayName}`,
        ),
      );
      await reloadCatalog();
    } catch (error: unknown) {
      setArtifactUploadError(normalizeArtifactUploadError(error, t, artifactModalItem));
    } finally {
      setArtifactUploadLoading(false);
    }
  }, [artifactModalFile, artifactModalItem, reloadCatalog, resolvedProcessingServerId, t]);

  const artifactModalAcquisition = artifactModalItem?.acquisition ?? defaultAcquisitionForModelId(artifactModalItem?.modelId || "");
  const artifactModalNeedsExport = artifactModalAcquisition.artifactSource === "checkpoint_export_required";
  const artifactModalGuideUrl = artifactModalAcquisition.guideUrl;
  const artifactModalExportGuideUrl = artifactModalAcquisition.exportGuideUrl;

  return (
    <div className="pipelinesOperatorConfigCard">
      {isTracking ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.tracker_id")}</span>
            <select
              className="pipelinesInput"
              value={trackerId}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  tracker_id: String(event.target.value || "simple_iou_kalman").trim() || "simple_iou_kalman",
                }));
              }}
            >
              {trackerOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
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
            {showAdvanced ? (
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
                  }));
                }}
              />
            ) : (
              <select
                className="pipelinesInput"
                value={modelId}
                onChange={(event) => {
                  const nextModelId = String(event.target.value || "").trim();
                  onUpdateConfig((prev) => ({
                    ...prev,
                    model_id: nextModelId,
                  }));
                }}
              >
                {basicModelItems.map((item) => (
                  <option key={item.modelId} value={item.modelId}>
                    {item.displayName}
                    {item.badgeIds.includes("recommended") ? ` • ${t("core.ui.processing_servers.vision_recommendations.badge.recommended")}` : ""}
                    {item.availability !== "available"
                      ? ` • ${t(availabilityTranslationKey(item.availability), {}, item.availability)}`
                      : ""}
                  </option>
                ))}
              </select>
            )}
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
          {showModelRecoveryCard ? (
            <div className="pipelinesOperatorConfigCard" style={{ marginTop: 10 }}>
              <div className="pipelinesInlineError">
                {selectedModelNeedsInstall
                  ? t(
                      "core.ui.pipelines.panels.yolo.model_recovery.install_needed",
                      {
                        model: selectedCatalogItem?.displayName || modelId || t("core.ui.pipelines.panels.yolo.model_id"),
                        serverId: resolvedProcessingServerId,
                      },
                      "This model still needs to be installed on the selected machine.",
                    )
                  : selectedModelIncompatible
                    ? t(
                        "core.ui.pipelines.panels.yolo.model_recovery.incompatible",
                        {
                          model: selectedCatalogItem?.displayName || modelId || t("core.ui.pipelines.panels.yolo.model_id"),
                          serverId: resolvedProcessingServerId,
                        },
                        "This model is not compatible with the selected machine.",
                      )
                    : t(
                        "core.ui.pipelines.panels.yolo.model_recovery.none_ready",
                        { serverId: resolvedProcessingServerId },
                        "No ready-to-run models were found on the selected machine.",
                      )}
              </div>
              {suggestedAvailableItem ? (
                <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
                  {t(
                    "core.ui.pipelines.panels.yolo.model_recovery.recommended_ready",
                    { model: suggestedAvailableItem.displayName },
                    `Use ${suggestedAvailableItem.displayName} to continue now.`,
                  )}
                </div>
              ) : (
                <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
                  {t(
                    "core.ui.pipelines.panels.yolo.model_recovery.no_ready_action",
                    { serverId: resolvedProcessingServerId },
                    "Add a model file on this processing server or switch to another server.",
                  )}
                </div>
              )}
              {manualInstallItem && manualInstallFile ? (
                <div style={{ marginTop: 10 }}>
                  <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.artifact_modal.recovery_intro")}</div>
                  {manualInstallNeedsExport ? (
                    <>
                      <div className="pipelinesStepHint">
                        {t(
                          "core.ui.pipelines.panels.yolo.artifact_modal.recovery_checkpoint_page",
                          { model: manualInstallItem.displayName },
                          `1. Open the checkpoint page for ${manualInstallItem.displayName}.`,
                        )}
                      </div>
                      <div className="pipelinesStepHint">
                        {t(
                          "core.ui.pipelines.panels.yolo.artifact_modal.recovery_export_onnx",
                          { file: manualInstallFile },
                          `2. Export the ONNX file ${manualInstallFile}.`,
                        )}
                      </div>
                    </>
                  ) : (
                    <div className="pipelinesStepHint">
                      {t("core.ui.pipelines.panels.yolo.artifact_modal.recovery_file", { file: manualInstallFile }, manualInstallFile)}
                    </div>
                  )}
                  <div className="pipelinesStepHint">
                    {manualInstallNeedsExport
                      ? t("core.ui.pipelines.panels.yolo.artifact_modal.recovery_send_prepare")
                      : t("core.ui.pipelines.panels.yolo.artifact_modal.recovery_send")}
                  </div>
                  <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.artifact_modal.recovery_refresh")}</div>
                </div>
              ) : null}
              <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                {manualInstallItem ? (
                  <button
                    className="pillButton pillButtonPrimary"
                    type="button"
                    onClick={() => openArtifactModal(manualInstallItem)}
                  >
                    {manualInstallItem.artifactExists
                      ? t("core.ui.pipelines.panels.yolo.artifact_modal.open_update")
                      : manualInstallNeedsExport
                        ? t("core.ui.pipelines.panels.yolo.artifact_modal.open_prepare")
                        : t("core.ui.pipelines.panels.yolo.artifact_modal.open_get")}
                  </button>
                ) : null}
                {suggestedAvailableItem ? (
                  <button className="pillButton pillButtonPrimary" type="button" onClick={applySuggestedAvailableModel}>
                    {t(
                      "core.ui.pipelines.panels.yolo.model_recovery.use_recommended",
                      { model: suggestedAvailableItem.displayName },
                      `Use ${suggestedAvailableItem.displayName}`,
                    )}
                  </button>
                ) : null}
                {onOpenProcessingServers ? (
                  <button className="pillButton" type="button" onClick={onOpenProcessingServers}>
                    {t("core.ui.pipelines.form.processing_server.manage")}
                  </button>
                ) : null}
                <button className="pillButton" type="button" onClick={() => void reloadCatalog()} disabled={catalogLoading}>
                  {t("core.ui.pipelines.panels.yolo.refresh_models")}
                </button>
              </div>
            </div>
          ) : null}
          {catalogError ? <div className="errorText">{catalogError}</div> : null}
          {!showAdvanced && unavailableItems.length > 0 ? (
            <div className="pipelinesStepHint">
              {t("core.ui.pipelines.panels.yolo.hidden_unavailable_count", { count: unavailableItems.length })}
            </div>
          ) : null}

          <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            {selectedCatalogItem ? (
              <button className="pillButton" type="button" onClick={() => openArtifactModal(selectedCatalogItem)}>
                {selectedCatalogItem.artifactExists
                  ? t("core.ui.pipelines.panels.yolo.artifact_modal.open_update")
                  : selectedCatalogItem.acquisition.artifactSource === "checkpoint_export_required"
                    ? t("core.ui.pipelines.panels.yolo.artifact_modal.open_prepare")
                    : t("core.ui.pipelines.panels.yolo.artifact_modal.open_get")}
              </button>
            ) : null}
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
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.artifact_modal.advanced_hint", {
                  file: artifactFileName(selectedCatalogItem.artifactPath) || selectedCatalogItem.modelId,
                })}
              </div>
            </div>
          ) : null}
        </>
      )}

      {isDetection ? (
        <>
          <label className="pipelinesCheckboxLabel">
            <input
              type="checkbox"
              checked={detectionFilterFrames}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  emit_mode: event.target.checked ? "events" : "annotate",
                }));
              }}
            />
            <span>{t("core.ui.pipelines.panels.yolo.filter_frames")}</span>
          </label>
          <div className="pipelinesStepHint">
            {detectionFilterFrames
              ? t("core.ui.pipelines.panels.yolo.filter_frames_hint")
              : t("core.ui.pipelines.panels.yolo.detect_annotate_only_hint")}
          </div>
        </>
      ) : null}

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
            {showAdvanced ? (
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
            ) : (
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <button
                  className={["pillButton", categories.length === 0 ? "pillButtonPrimary" : ""].filter(Boolean).join(" ")}
                  type="button"
                  onClick={() => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      categories: [],
                    }));
                  }}
                >
                  {t("core.ui.pipelines.panels.yolo.categories_all")}
                </button>
                {basicCategoryValues.map((value) => {
                  const active = categories.includes(value);
                  return (
                    <button
                      key={value}
                      className={["pillButton", active ? "pillButtonPrimary" : ""].filter(Boolean).join(" ")}
                      type="button"
                      onClick={() => {
                        onUpdateConfig((prev) => {
                          const current = Array.isArray(prev.categories)
                            ? prev.categories.map((item) => String(item || "").trim().toLowerCase()).filter(Boolean)
                            : [];
                          const next = active ? current.filter((item) => item !== value) : [...current, value];
                          return {
                            ...prev,
                            categories: next,
                          };
                        });
                      }}
                    >
                      {t(BASIC_CATEGORY_LABEL_KEYS[value] || "", {}, value)}
                    </button>
                  );
                })}
              </div>
            )}
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
            <select
              className="pipelinesInput"
              value={inputWithFallback}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  input_with_fallback: String(event.target.value || "treated,original").trim() || "treated,original",
                }));
              }}
            >
              {inputPresetOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
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
            </>
          ) : null}
        </>
      ) : null}

      {isSegmentation ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.input_source")}</span>
            <select
              className="pipelinesInput"
              value={inputWithFallback}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  input_with_fallback: String(event.target.value || "treated,original").trim() || "treated,original",
                }));
              }}
            >
              {inputPresetOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
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

      <Modal
        open={!!artifactModalItem}
        title={
          artifactModalItem?.artifactExists
            ? t(
                "core.ui.pipelines.panels.yolo.artifact_modal.title_update",
                { model: artifactModalItem?.displayName || "" },
                `Update ${artifactModalItem?.displayName || ""}`,
              )
            : artifactModalNeedsExport
              ? t(
                  "core.ui.pipelines.panels.yolo.artifact_modal.title_prepare",
                  { model: artifactModalItem?.displayName || "" },
                  `Prepare ${artifactModalItem?.displayName || ""}`,
                )
            : t(
                "core.ui.pipelines.panels.yolo.artifact_modal.title_get",
                { model: artifactModalItem?.displayName || "" },
                `Get ${artifactModalItem?.displayName || ""}`,
              )
        }
        onClose={closeArtifactModal}
      >
        {artifactModalItem ? (
          <div>
            <div className="pipelinesStepHint">
              {artifactModalNeedsExport
                ? t(
                    "core.ui.pipelines.panels.yolo.artifact_modal.intro_checkpoint_export",
                    { model: artifactModalItem.displayName },
                    `The official page for ${artifactModalItem.displayName} gives you a checkpoint (.pth). This step needs the exported .onnx file.`,
                  )
                : t(
                    "core.ui.pipelines.panels.yolo.artifact_modal.intro",
                    { model: artifactModalItem.displayName },
                    `Use this flow when the selected machine still does not have the ONNX file for ${artifactModalItem.displayName}.`,
                  )}
            </div>
            <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
              {t("core.ui.pipelines.panels.yolo.artifact_modal.expected_file", {
                file: artifactFileName(artifactModalItem.artifactPath) || artifactModalItem.modelId,
              })}
            </div>
            <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
              {artifactModalNeedsExport
                ? t("core.ui.pipelines.panels.yolo.artifact_modal.steps_intro_checkpoint_export")
                : t("core.ui.pipelines.panels.yolo.artifact_modal.steps_intro")}
            </div>
            <ol className="pipelinesArtifactSteps">
              {artifactModalNeedsExport ? (
                <>
                  <li>{t("core.ui.pipelines.panels.yolo.artifact_modal.step_checkpoint_page")}</li>
                  <li>{t("core.ui.pipelines.panels.yolo.artifact_modal.step_export_onnx")}</li>
                  <li>{t("core.ui.pipelines.panels.yolo.artifact_modal.step_upload_exported_onnx")}</li>
                </>
              ) : (
                <>
                  <li>{t("core.ui.pipelines.panels.yolo.artifact_modal.step_find")}</li>
                  <li>{t("core.ui.pipelines.panels.yolo.artifact_modal.step_download")}</li>
                  <li>{t("core.ui.pipelines.panels.yolo.artifact_modal.step_drop")}</li>
                </>
              )}
            </ol>

            <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
              {artifactModalGuideUrl ? (
                <a
                  className="pillButton"
                  href={artifactModalGuideUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {artifactModalNeedsExport
                    ? t("core.ui.pipelines.panels.yolo.artifact_modal.open_checkpoint_page")
                    : t("core.ui.pipelines.panels.yolo.artifact_modal.open_official_page")}
                </a>
              ) : null}
              {artifactModalExportGuideUrl ? (
                <a
                  className="pillButton"
                  href={artifactModalExportGuideUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t("core.ui.pipelines.panels.yolo.artifact_modal.open_export_guide")}
                </a>
              ) : null}
            </div>

            <input
              ref={artifactFileInputRef}
              type="file"
              accept=".onnx,.pth,.pt,.ckpt,application/octet-stream"
              style={{ display: "none" }}
              onChange={(event) => {
                handleArtifactFileChosen(event.target.files?.[0] ?? null);
                event.currentTarget.value = "";
              }}
            />

            <div
              className={["pipelinesArtifactDropzone", artifactModalDragActive ? "isActive" : ""].filter(Boolean).join(" ")}
              style={{ marginTop: 14 }}
              role="button"
              tabIndex={0}
              onClick={() => artifactFileInputRef.current?.click()}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  artifactFileInputRef.current?.click();
                }
              }}
              onDragEnter={(event) => {
                event.preventDefault();
                setArtifactModalDragActive(true);
              }}
              onDragOver={(event) => {
                event.preventDefault();
                setArtifactModalDragActive(true);
              }}
              onDragLeave={(event) => {
                if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
                setArtifactModalDragActive(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setArtifactModalDragActive(false);
                handleArtifactFileChosen(event.dataTransfer.files?.[0] ?? null);
              }}
            >
              <div className="pipelinesArtifactDropzoneTitle">
                {artifactModalNeedsExport
                  ? t("core.ui.pipelines.panels.yolo.artifact_modal.drop_title_exported_onnx")
                  : t("core.ui.pipelines.panels.yolo.artifact_modal.drop_title")}
              </div>
              <div className="pipelinesStepHint">
                {artifactModalNeedsExport
                  ? t("core.ui.pipelines.panels.yolo.artifact_modal.drop_subtitle_checkpoint_export")
                  : t("core.ui.pipelines.panels.yolo.artifact_modal.drop_subtitle")}
              </div>
              {artifactModalFile ? (
                <div className="pipelinesArtifactDropzoneFile">
                  {artifactModalFile.name} • {formatProgressBytes(artifactModalFile.size)}
                </div>
              ) : null}
            </div>

            {artifactUploadLoading ? (
              <div className="pipelinesStepHint" style={{ marginTop: 10 }}>
                {t(
                  "core.ui.pipelines.panels.yolo.artifact_modal.upload_progress",
                  { progress: Math.max(0, Math.min(100, Math.round(artifactUploadProgressPct))) },
                  `Uploading ${Math.round(artifactUploadProgressPct)}%`,
                )}
                {artifactUploadProgressBytes.total > 0
                  ? ` • ${formatProgressBytes(artifactUploadProgressBytes.uploaded)} / ${formatProgressBytes(artifactUploadProgressBytes.total)}`
                  : ""}
              </div>
            ) : null}
            {artifactUploadError ? <div className="errorText" style={{ marginTop: 10 }}>{artifactUploadError}</div> : null}
            {artifactUploadSuccess ? (
              <div className="settingsStatusMuted" style={{ marginTop: 10 }}>
                {artifactUploadSuccess}
              </div>
            ) : null}

            <div className="row" style={{ gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <button
                className="pillButton pillButtonPrimary"
                type="button"
                disabled={!artifactModalFile || artifactUploadLoading}
                onClick={() => void handleArtifactUpload()}
              >
                {artifactUploadLoading
                  ? t("core.ui.pipelines.panels.yolo.artifact_modal.uploading")
                  : artifactModalItem.artifactExists
                    ? t("core.ui.pipelines.panels.yolo.artifact_modal.apply_update")
                    : t("core.ui.pipelines.panels.yolo.artifact_modal.apply_add")}
              </button>
              <button className="pillButton" type="button" onClick={closeArtifactModal} disabled={artifactUploadLoading}>
                {t("core.ui.pipelines.panels.yolo.artifact_modal.close")}
              </button>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
