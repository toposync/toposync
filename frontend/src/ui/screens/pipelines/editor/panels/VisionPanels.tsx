import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";

import type {
  PipelineOperatorDefinition,
  ProcessingServerStatus,
  ProcessingServerVisionModelArtifactUploadResponse,
  ProcessingServerVisionManifestImportResponse,
} from "../../../../../util/api";
import {
  getProcessingServerStatus,
  installProcessingServerVisionModel,
  uploadProcessingServerVisionModelArtifact,
} from "../../../../../util/api";
import { CustomOnnxWizardModal } from "../../../../CustomOnnxWizardModal";
import { HuggingFaceImportModal } from "../../../../HuggingFaceImportModal";
import { i18n } from "../../../../../util/i18n";
import { LocalBuildConsentModal } from "../../../../LocalBuildConsentModal";
import { Modal } from "../../../../Modal";
import { pipelinesReactSelectStyles, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "../../types";
import { textConfigValue } from "../../utils";
import { PipelinesNumberInput } from "../PipelinesNumberInput";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  operatorId: string;
  stepUid: string;
  nodeId: string;
  index: number;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  config: Record<string, unknown>;
  processingServerId: string;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
  onInsertStepAfter: (afterUid: string, operatorId: string, defaultsOverride?: Record<string, unknown>) => void;
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
  artifactFormat: string;
  sourceKind: "official" | "custom";
  custom: boolean;
  artifactExists: boolean;
  acquisitionMode: "guided_upload" | "auto_download" | "local_build_assisted";
  acquisitionSupported: boolean;
  acquisitionReason: string;
  acquisitionSourceKind: string;
  acquisitionSourceLabel: string;
  acquisitionArtifactSource: string;
  acquisition: VisionModelAcquisition;
  installSupported: boolean;
  installReason: string;
  installSourceKind: string;
  installSourceLabel: string;
  installJob: VisionModelInstallJob | null;
  localBuildSupported: boolean;
  localBuildReason: string;
  localBuildBackend: string;
  localBuildRuntime: string;
  localBuildSourceLabel: string;
  localBuildMissingTools: string[];
  compatibleProviderIds: string[];
  acceleratorIds: string[];
  inputWidth: number;
  inputHeight: number;
  inputDtype: string;
  classesSource: string;
  classesCount: number;
  codeLicense: string;
  weightsLicense: string;
  commercialUseStatus: string;
  resourceTier: string;
  notes: string[];
};

type VisionModelAcquisition = {
  mode: "guided_upload" | "auto_download" | "local_build_assisted";
  artifactSource: string;
  guideUrl: string;
  exportGuideUrl: string;
  sourceUrl: string;
  checkpointUrl: string;
  configUrl: string;
  metafileUrl: string;
  paperUrl: string;
  builderBackend: string;
  supportedPlatforms: string[];
  explicitConsentRequired: boolean;
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

type LocalBuildConsentState = {
  item: VisionModelCatalogItem;
  action: "prepare" | "update";
};

const GROUP_EVENT_MODE_OPTIONS = ["session", "proximity", "disabled"] as const;
const GROUP_EVENT_WORLD_ANCHOR_OPTIONS = ["auto", "always", "never"] as const;

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
  const artifactSource = String(raw.acquisition_artifact_source || acquisition?.artifact_source || "onnx_ready").trim() || "onnx_ready";
  const rawMode = String(raw.acquisition_mode || acquisition?.mode || "guided_upload").trim();
  const mode: VisionModelAcquisition["mode"] =
    rawMode === "auto_download" || rawMode === "local_build_assisted" ? rawMode : "guided_upload";
  const builderBackend = String(acquisition?.builder_backend || "").trim();
  return {
    modelId,
    displayName: String(raw.display_name || raw.model_id || "").trim() || modelId,
    artifactPath: String(raw.artifact_path || "").trim(),
    availability: normalizedAvailability,
    availabilityReason: String(raw.availability_reason || "").trim(),
    badgeIds: Array.isArray(raw.badge_ids) ? raw.badge_ids.map((value) => String(value || "").trim()).filter(Boolean) : [],
    runtime: String(raw.runtime || "").trim(),
    artifactFormat: String(raw.artifact_format || "").trim(),
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
      checkpointUrl: String(acquisition?.checkpoint_url || "").trim(),
      configUrl: String(acquisition?.config_url || "").trim(),
      metafileUrl: String(acquisition?.metafile_url || "").trim(),
      paperUrl: String(acquisition?.paper_url || "").trim(),
      builderBackend,
      supportedPlatforms: Array.isArray(acquisition?.supported_platforms)
        ? acquisition.supported_platforms.map((value: unknown) => String(value || "").trim().toLowerCase()).filter(Boolean)
        : [],
      explicitConsentRequired: !!acquisition?.explicit_consent_required,
    },
    installSupported: !!raw.install_supported,
    installReason: String(raw.install_reason || "").trim(),
    installSourceKind: String(raw.install_source_kind || "").trim(),
    installSourceLabel: String(raw.install_source_label || "").trim(),
    installJob: parseInstallJob(raw.install_job),
    localBuildSupported: !!raw.local_build_supported,
    localBuildReason: String(raw.local_build_reason || "").trim(),
    localBuildBackend: String(raw.local_build_backend || "").trim(),
    localBuildRuntime: String(raw.local_build_runtime || "").trim(),
    localBuildSourceLabel: String(acquisition?.checkpoint_url || raw.local_build_source_label || acquisition?.source_url || "").trim(),
    localBuildMissingTools: Array.isArray(raw.local_build_missing_tools)
      ? raw.local_build_missing_tools.map((value) => String(value || "").trim()).filter(Boolean)
      : [],
    compatibleProviderIds: Array.isArray(raw.compatible_provider_ids)
      ? raw.compatible_provider_ids.map((value) => String(value || "").trim()).filter(Boolean)
      : [],
    acceleratorIds: Array.isArray(raw.accelerator_ids)
      ? raw.accelerator_ids.map((value) => String(value || "").trim()).filter(Boolean)
      : [],
    inputWidth: Number(input?.width ?? 0),
    inputHeight: Number(input?.height ?? 0),
    inputDtype: String(input?.dtype || "").trim(),
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
  task: "classification" | "detection" | "segmentation",
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
    artifactFormat: runtime === "onnxruntime" ? "onnx" : "",
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
    localBuildSupported: false,
    localBuildReason: "",
    localBuildBackend: "",
    localBuildRuntime: "",
    localBuildSourceLabel: "",
    localBuildMissingTools: [],
    compatibleProviderIds: [],
    acceleratorIds: [],
    inputWidth: 0,
    inputHeight: 0,
    inputDtype: "float32",
    classesSource: "",
    classesCount: 0,
    codeLicense: "",
    weightsLicense: "",
    commercialUseStatus: "",
    resourceTier: "",
    notes: [],
  };
}

const TRACKER_CHOICES = [
  {
    value: "byte_world",
    labelKey: "core.ui.pipelines.panels.yolo.tracker_byte_world_label",
    hintKey: "core.ui.pipelines.panels.yolo.tracker_byte_world_hint",
  },
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
const TRACKING_WORLD_ANCHOR_OPTIONS = ["auto", "always", "never"] as const;

const MODEL_HINT_KEYS: Record<string, string> = {
  rfdetr_det_nano: "core.ui.pipelines.panels.yolo.model_rfdetr_nano_hint",
  rfdetr_det_small: "core.ui.pipelines.panels.yolo.model_rfdetr_small_hint",
  rfdetr_det_medium: "core.ui.pipelines.panels.yolo.model_rfdetr_medium_hint",
  rtmdet_det_tiny: "core.ui.pipelines.panels.yolo.model_rtmdet_tiny_hint",
  rtmdet_det_small: "core.ui.pipelines.panels.yolo.model_rtmdet_small_hint",
  rtmdet_det_medium: "core.ui.pipelines.panels.yolo.model_rtmdet_medium_hint",
  rtmdet_ins_tiny: "core.ui.pipelines.panels.yolo.model_rtmdet_ins_tiny_hint",
  rtmdet_ins_small: "core.ui.pipelines.panels.yolo.model_rtmdet_ins_small_hint",
  rtmdet_ins_medium: "core.ui.pipelines.panels.yolo.model_rtmdet_ins_medium_hint",
};

const RTMDET_DETECTION_ACQUISITION_DEFAULTS: Record<
  string,
  Pick<
    VisionModelAcquisition,
    "checkpointUrl" | "configUrl" | "metafileUrl" | "paperUrl" | "builderBackend" | "supportedPlatforms" | "explicitConsentRequired"
  >
> = {
  rtmdet_det_tiny: {
    checkpointUrl:
      "https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_tiny_8xb32-300e_coco/rtmdet_tiny_8xb32-300e_coco_20220902_112414-78e30dcc.pth",
    configUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/rtmdet_tiny_8xb32-300e_coco.py",
    metafileUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/metafile.yml",
    paperUrl: "https://arxiv.org/abs/2212.07784",
    builderBackend: "container_local",
    supportedPlatforms: ["linux"],
    explicitConsentRequired: true,
  },
  rtmdet_det_small: {
    checkpointUrl:
      "https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_s_8xb32-300e_coco/rtmdet_s_8xb32-300e_coco_20220905_161602-387a891e.pth",
    configUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/rtmdet_s_8xb32-300e_coco.py",
    metafileUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/metafile.yml",
    paperUrl: "https://arxiv.org/abs/2212.07784",
    builderBackend: "container_local",
    supportedPlatforms: ["linux"],
    explicitConsentRequired: true,
  },
  rtmdet_det_medium: {
    checkpointUrl:
      "https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_m_8xb32-300e_coco/rtmdet_m_8xb32-300e_coco_20220719_112220-229f527c.pth",
    configUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/rtmdet_m_8xb32-300e_coco.py",
    metafileUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/metafile.yml",
    paperUrl: "https://arxiv.org/abs/2212.07784",
    builderBackend: "container_local",
    supportedPlatforms: ["linux"],
    explicitConsentRequired: true,
  },
};

const RFDETR_DETECTION_ACQUISITION_DEFAULTS: Record<
  string,
  Pick<
    VisionModelAcquisition,
    "guideUrl" | "exportGuideUrl" | "sourceUrl" | "checkpointUrl" | "paperUrl" | "builderBackend" | "supportedPlatforms" | "explicitConsentRequired"
  >
> = {
  rfdetr_det_nano: {
    guideUrl: "https://github.com/roboflow/rf-detr",
    exportGuideUrl: "https://rfdetr.roboflow.com/learn/export/",
    sourceUrl: "https://github.com/roboflow/rf-detr",
    checkpointUrl: "https://storage.googleapis.com/rfdetr/nano_coco/checkpoint_best_regular.pth",
    paperUrl: "https://arxiv.org/abs/2511.09554",
    builderBackend: "host_python",
    supportedPlatforms: ["linux", "darwin", "windows"],
    explicitConsentRequired: true,
  },
  rfdetr_det_small: {
    guideUrl: "https://github.com/roboflow/rf-detr",
    exportGuideUrl: "https://rfdetr.roboflow.com/learn/export/",
    sourceUrl: "https://github.com/roboflow/rf-detr",
    checkpointUrl: "https://storage.googleapis.com/rfdetr/small_coco/checkpoint_best_regular.pth",
    paperUrl: "https://arxiv.org/abs/2511.09554",
    builderBackend: "host_python",
    supportedPlatforms: ["linux", "darwin", "windows"],
    explicitConsentRequired: true,
  },
  rfdetr_det_medium: {
    guideUrl: "https://github.com/roboflow/rf-detr",
    exportGuideUrl: "https://rfdetr.roboflow.com/learn/export/",
    sourceUrl: "https://github.com/roboflow/rf-detr",
    checkpointUrl: "https://storage.googleapis.com/rfdetr/medium_coco/checkpoint_best_regular.pth",
    paperUrl: "https://arxiv.org/abs/2511.09554",
    builderBackend: "host_python",
    supportedPlatforms: ["linux", "darwin", "windows"],
    explicitConsentRequired: true,
  },
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
const DEFAULT_DETECTION_MODEL_ID = "rfdetr_det_medium";
const DETECTION_LAST_USED_MODEL_STORAGE_KEY = "toposync:pipelines:vision.detect:last_model_id";

function resourceTierTranslationKey(value: string): string {
  return `core.ui.pipelines.panels.yolo.resource_tier.${String(value || "").trim() || "unknown"}`;
}

function acquisitionModeTranslationKey(value: string): string {
  return `core.ui.pipelines.panels.yolo.acquisition_mode.${String(value || "").trim() || "guided_upload"}`;
}

function builderBackendTranslationKey(value: string): string {
  return `core.ui.pipelines.panels.yolo.builder_backend.${String(value || "").trim() || "unknown"}`;
}

function localBuildReasonLabel(
  reason: string,
  t: (key: string, vars?: Record<string, unknown>, fallback?: string) => string,
): string {
  const clean = String(reason || "").trim() || "unsupported";
  return t(`core.ui.processing_servers.local_build.reason.${clean}`, {}, clean);
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
  if (clean.startsWith("rfdetr_det_")) {
    const defaults = RFDETR_DETECTION_ACQUISITION_DEFAULTS[clean];
    return {
      mode: "local_build_assisted",
      artifactSource: "checkpoint_export_required",
      guideUrl: defaults?.guideUrl || "",
      exportGuideUrl: defaults?.exportGuideUrl || "",
      sourceUrl: defaults?.sourceUrl || "",
      checkpointUrl: defaults?.checkpointUrl || "",
      configUrl: "",
      metafileUrl: "",
      paperUrl: defaults?.paperUrl || "",
      builderBackend: defaults?.builderBackend || "",
      supportedPlatforms: [...(defaults?.supportedPlatforms || [])],
      explicitConsentRequired: !!defaults?.explicitConsentRequired,
    };
  }
  if (clean.startsWith("rtmdet_det_") || clean.startsWith("rtmdet_ins_")) {
    const detectionDefaults = RTMDET_DETECTION_ACQUISITION_DEFAULTS[clean];
    return {
      mode: "guided_upload",
      artifactSource: "checkpoint_export_required",
      guideUrl: "https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/README.md",
      exportGuideUrl: "https://mmdeploy.readthedocs.io/en/v1.2.0/04-supported-codebases/mmdet.html",
      sourceUrl: "",
      checkpointUrl: detectionDefaults?.checkpointUrl || "",
      configUrl: detectionDefaults?.configUrl || "",
      metafileUrl: detectionDefaults?.metafileUrl || "",
      paperUrl: detectionDefaults?.paperUrl || "",
      builderBackend: detectionDefaults?.builderBackend || "",
      supportedPlatforms: [...(detectionDefaults?.supportedPlatforms || [])],
      explicitConsentRequired: !!detectionDefaults?.explicitConsentRequired,
    };
  }
  return {
    mode: "guided_upload",
    artifactSource: "onnx_ready",
    guideUrl: "",
    exportGuideUrl: "",
    sourceUrl: "",
    checkpointUrl: "",
    configUrl: "",
    metafileUrl: "",
    paperUrl: "",
    builderBackend: "",
    supportedPlatforms: [],
    explicitConsentRequired: false,
  };
}

function detectionFallbackItems(): VisionModelCatalogItem[] {
  const items = [
    fallbackCatalogItem("rfdetr_det_nano", "RF-DETR Nano", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rfdetr_det_small", "RF-DETR Small", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rfdetr_det_medium", "RF-DETR Medium", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rtmdet_det_tiny", "RTMDet Tiny", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rtmdet_det_small", "RTMDet Small", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rtmdet_det_medium", "RTMDet Medium", "onnxruntime", { custom: false }),
  ];
  return items.map((item) =>
    item.modelId === DEFAULT_DETECTION_MODEL_ID ? { ...item, badgeIds: ["recommended", ...item.badgeIds] } : item,
  );
}

function segmentationFallbackItems(): VisionModelCatalogItem[] {
  return [
    fallbackCatalogItem("rtmdet_ins_tiny", "RTMDet-Ins Tiny", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rtmdet_ins_small", "RTMDet-Ins Small", "onnxruntime", { custom: false }),
    fallbackCatalogItem("rtmdet_ins_medium", "RTMDet-Ins Medium", "onnxruntime", { custom: false }),
  ];
}

function classificationFallbackItems(): VisionModelCatalogItem[] {
  return [];
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

function pickRecommendedDetectionModel(items: VisionModelCatalogItem[]): VisionModelCatalogItem | null {
  return (
    items.find((item) => item.badgeIds.includes("recommended")) ??
    items.find((item) => item.modelId === DEFAULT_DETECTION_MODEL_ID) ??
    pickSuggestedAvailableModel(items)
  );
}

function pickInitialDetectionModel(items: VisionModelCatalogItem[], lastUsedModelId: string): VisionModelCatalogItem | null {
  const clean = String(lastUsedModelId || "").trim();
  if (clean) {
    const lastUsed = items.find((item) => item.modelId === clean);
    if (lastUsed) return lastUsed;
  }
  return pickRecommendedDetectionModel(items);
}

function readLastUsedDetectionModelId(): string {
  if (typeof window === "undefined") return "";
  try {
    return String(window.localStorage.getItem(DETECTION_LAST_USED_MODEL_STORAGE_KEY) || "").trim();
  } catch {
    return "";
  }
}

function writeLastUsedDetectionModelId(modelId: string): void {
  const clean = String(modelId || "").trim();
  if (!clean || typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DETECTION_LAST_USED_MODEL_STORAGE_KEY, clean);
  } catch {
    // localStorage can be unavailable in restricted browser contexts.
  }
}

function parsePrivacyPolicyLabels(raw: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const part of String(raw || "").split(",")) {
    const value = String(part || "").trim().toLowerCase();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

function buildClassificationPrivacyMatchExpression(labels: string[], minScore: number): string {
  const normalizedScore = Number.isFinite(minScore) ? Math.max(0, Math.min(1, minScore)) : 0.85;
  return `payload.classification_label_normalized in ${JSON.stringify(labels)} and payload.classification_score is not None and payload.classification_score >= ${normalizedScore.toFixed(2)}`;
}

function buildClassificationPrivacyAllowExpression(labels: string[], minScore: number): string {
  return `not (${buildClassificationPrivacyMatchExpression(labels, minScore)})`;
}

export function VisionGroupEventsConfigCard({
  config,
  showAdvanced,
  onUpdateConfig,
}: {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const categoriesRaw = (config as any).categories;
  const categories = Array.isArray(categoriesRaw)
    ? categoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const categoryOptions = useMemo<SelectOption[]>(() => {
    const known = new Set(YOLO_CATEGORY_OPTIONS.map((item) => item.value));
    const extras = categories
      .filter((value) => !known.has(value))
      .map((value) => ({ value, label: value }));
    return [...YOLO_CATEGORY_OPTIONS, ...extras];
  }, [categories]);
  const selectedCategories = useMemo(
    () => categoryOptions.filter((option) => categories.includes(option.value)),
    [categories, categoryOptions],
  );
  const modeRaw = String((config as any).mode ?? "session").trim().toLowerCase();
  const mode = GROUP_EVENT_MODE_OPTIONS.includes(modeRaw as any) ? modeRaw : "session";
  const worldAnchorRaw = String((config as any).use_world_anchor ?? "auto").trim().toLowerCase();
  const useWorldAnchor = GROUP_EVENT_WORLD_ANCHOR_OPTIONS.includes(worldAnchorRaw as any) ? worldAnchorRaw : "auto";
  const idleTimeoutRaw = Number((config as any).idle_timeout_seconds ?? 30);
  const idleTimeout = Number.isFinite(idleTimeoutRaw) ? Math.max(1, Math.min(3600, idleTimeoutRaw)) : 30;
  const updateIntervalRaw = Number((config as any).update_interval_seconds ?? 5);
  const updateInterval = Number.isFinite(updateIntervalRaw) ? Math.max(0, Math.min(300, updateIntervalRaw)) : 5;
  const groupDistanceRaw = Number((config as any).group_distance_meters ?? 10);
  const groupDistance = Number.isFinite(groupDistanceRaw) ? Math.max(0, Math.min(1000, groupDistanceRaw)) : 10;
  const imageDistanceRaw = Number((config as any).image_center_distance ?? 0.25);
  const imageDistance = Number.isFinite(imageDistanceRaw) ? Math.max(0, Math.min(2, imageDistanceRaw)) : 0.25;
  const includeStationaryMembers = Boolean((config as any).include_stationary_members ?? false);
  const bboxPaddingRaw = Number((config as any).bbox_padding_ratio ?? 0.08);
  const bboxPadding = Number.isFinite(bboxPaddingRaw) ? Math.max(0, Math.min(1, bboxPaddingRaw)) : 0.08;
  const maxCropAreaRaw = Number((config as any).max_crop_area_ratio ?? 0.75);
  const maxCropArea = Number.isFinite(maxCropAreaRaw) ? Math.max(0.01, Math.min(1, maxCropAreaRaw)) : 0.75;

  return (
    <div className="pipelinesStepConfigForm">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.group_events.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.group_events.mode")}</span>
        <select
          className="pipelinesSelect"
          value={mode}
          onChange={(event) => {
            onUpdateConfig((prev) => ({ ...prev, mode: event.target.value }));
          }}
        >
          <option value="session">{t("core.ui.pipelines.panels.group_events.mode.session")}</option>
          <option value="proximity">{t("core.ui.pipelines.panels.group_events.mode.proximity")}</option>
          <option value="disabled">{t("core.ui.pipelines.panels.group_events.mode.disabled")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.yolo.categories")}</span>
        <Select
          classNamePrefix="pipelinesReactSelect"
          styles={pipelinesReactSelectStyles}
          isMulti
          options={categoryOptions}
          value={selectedCategories}
          placeholder={t("core.ui.pipelines.panels.yolo.categories_placeholder")}
          onChange={(nextValue: MultiValue<SelectOption>) => {
            const nextCategories = Array.from(
              new Set(nextValue.map((item) => String(item.value || "").trim().toLowerCase()).filter(Boolean)),
            );
            onUpdateConfig((prev) => ({ ...prev, categories: nextCategories }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.group_events.categories_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.group_events.idle_timeout_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={1}
          max={3600}
          step={1}
          value={idleTimeout}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              idle_timeout_seconds: Math.max(1, Math.min(3600, nextValue)),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.group_events.update_interval_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={300}
          step={0.5}
          value={updateInterval}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              update_interval_seconds: Math.max(0, Math.min(300, nextValue)),
            }));
          }}
        />
      </label>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.group_events.use_world_anchor")}</span>
            <select
              className="pipelinesSelect"
              value={useWorldAnchor}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, use_world_anchor: event.target.value }));
              }}
            >
              <option value="auto">{t("core.ui.pipelines.panels.group_events.use_world_anchor.auto")}</option>
              <option value="always">{t("core.ui.pipelines.panels.group_events.use_world_anchor.always")}</option>
              <option value="never">{t("core.ui.pipelines.panels.group_events.use_world_anchor.never")}</option>
            </select>
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.group_events.group_distance_meters")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1000}
              step={0.5}
              value={groupDistance}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  group_distance_meters: Math.max(0, Math.min(1000, nextValue)),
                }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.group_events.image_center_distance")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={2}
              step={0.01}
              value={imageDistance}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  image_center_distance: Math.max(0, Math.min(2, nextValue)),
                }));
              }}
            />
          </label>

          <label className="pipelinesCheckboxLabel">
            <input
              type="checkbox"
              checked={includeStationaryMembers}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  include_stationary_members: event.target.checked,
                }));
              }}
            />
            <span>{t("core.ui.pipelines.panels.group_events.include_stationary_members")}</span>
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.group_events.bbox_padding_ratio")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1}
              step={0.01}
              value={bboxPadding}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  bbox_padding_ratio: Math.max(0, Math.min(1, nextValue)),
                }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.group_events.max_crop_area_ratio")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.01}
              max={1}
              step={0.01}
              value={maxCropArea}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  max_crop_area_ratio: Math.max(0.01, Math.min(1, nextValue)),
                }));
              }}
            />
          </label>
        </>
      ) : null}
    </div>
  );
}

export function VisionConfigCard({
  operatorId,
  stepUid,
  nodeId,
  index,
  steps,
  operatorsById,
  config,
  processingServerId,
  showAdvanced,
  onUpdateConfig,
  onInsertStepAfter,
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
  const openConfidenceRaw = Number((config as any).open_confidence_threshold ?? 0.5);
  const openConfidence = Number.isFinite(openConfidenceRaw) ? Math.max(0, Math.min(1, openConfidenceRaw)) : 0.5;
  const continueConfidenceRaw = Number((config as any).continue_confidence_threshold ?? 0.25);
  const continueConfidence = Number.isFinite(continueConfidenceRaw) ? Math.max(0, Math.min(openConfidence, continueConfidenceRaw)) : 0.25;
  const closeAfterRaw = Number((config as any).close_after_seconds ?? 10.0);
  const closeAfter = Number.isFinite(closeAfterRaw) ? Math.max(0.05, Math.min(300, closeAfterRaw)) : 10.0;
  const stitchGapRaw = Number((config as any).stitch_gap_seconds ?? 30.0);
  const stitchGap = Number.isFinite(stitchGapRaw) ? Math.max(closeAfter, Math.min(3600, stitchGapRaw)) : 30.0;
  const worldDistanceRaw = Number((config as any).world_match_distance_meters ?? 3.0);
  const worldDistance = Number.isFinite(worldDistanceRaw) ? Math.max(0, Math.min(1000, worldDistanceRaw)) : 3.0;
  const inferenceIntervalRaw = Number((config as any).inference_interval_seconds ?? 0);
  const inferenceInterval = Number.isFinite(inferenceIntervalRaw) ? Math.max(0, Math.min(60, inferenceIntervalRaw)) : 0;
  const trackerId = String((config as any).tracker_id ?? "byte_world").trim() || "byte_world";
  const trackerPreset = TRACKER_CHOICES.find((item) => item.value === trackerId) ?? null;
  const isTracking = String(operatorId || "").trim() === "vision.track";
  const emitModeRaw = String((config as any).emit_mode ?? "events").trim().toLowerCase() || "events";
  const emitMode = ["events", "filter", "annotate"].includes(emitModeRaw) ? emitModeRaw : "events";
  const detectEmitMode = emitMode;
  const pauseWhenGateClosed = Boolean((config as any).pause_when_gate_closed ?? true);
  const useWorldAnchorRaw = String((config as any).use_world_anchor ?? "auto").trim().toLowerCase() || "auto";
  const useWorldAnchor = TRACKING_WORLD_ANCHOR_OPTIONS.includes(useWorldAnchorRaw as any) ? useWorldAnchorRaw : "auto";
  const modelId = String((config as any).model_id ?? "").trim();
  const attachMaskArtifacts = Boolean((config as any).attach_mask_artifacts ?? true);
  const attachPolygons = Boolean((config as any).attach_polygons ?? false);
  const maxInstancesRaw = Number((config as any).max_instances_per_frame ?? 16);
  const maxInstances = Number.isFinite(maxInstancesRaw) ? Math.max(1, Math.min(512, maxInstancesRaw)) : 16;
  const topKRaw = Number((config as any).top_k ?? 5);
  const topK = Number.isFinite(topKRaw) ? Math.max(1, Math.min(64, topKRaw)) : 5;

  const isClassification = String(operatorId || "").trim() === "vision.classify_image";
  const isSegmentation = String(operatorId || "").trim() === "vision.segment_instances";
  const isDetection = !isTracking && !isClassification && !isSegmentation;
  const customOnnxSupported = isDetection || isClassification || isSegmentation;
  const task = isSegmentation ? "segmentation" : isClassification ? "classification" : "detection";
  const resolvedProcessingServerId = String(processingServerId || "").trim() || "local";

  const [serverStatus, setServerStatus] = useState<ProcessingServerStatus | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [showCustomOnnxWizard, setShowCustomOnnxWizard] = useState(false);
  const [showHuggingFaceWizard, setShowHuggingFaceWizard] = useState(false);
  const [customOnnxSuccess, setCustomOnnxSuccess] = useState<string | null>(null);
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
  const [localBuildLoadingModelId, setLocalBuildLoadingModelId] = useState<string>("");
  const [localBuildError, setLocalBuildError] = useState<string | null>(null);
  const [localBuildSuccess, setLocalBuildSuccess] = useState<string | null>(null);
  const [localBuildConsent, setLocalBuildConsent] = useState<LocalBuildConsentState | null>(null);
  const [localBuildConsentChecked, setLocalBuildConsentChecked] = useState(false);
  const [localBuildConsentSubmitting, setLocalBuildConsentSubmitting] = useState(false);
  const [localBuildConsentError, setLocalBuildConsentError] = useState<string | null>(null);
  const [showProvisionDetails, setShowProvisionDetails] = useState(false);
  const [privacyPolicyLabelsText, setPrivacyPolicyLabelsText] = useState("nsfw");
  const [privacyPolicyThreshold, setPrivacyPolicyThreshold] = useState(0.85);
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

  useEffect(() => {
    if (isTracking) return undefined;
    const hasActiveInstall = (taskCatalog?.items || []).some((item) => {
      const status = String(item.installJob?.status || "").trim();
      return ["queued", "downloading", "verifying", "installing"].includes(status);
    });
    if (!hasActiveInstall) return undefined;
    const timer = window.setInterval(() => {
      void reloadCatalog();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [isTracking, reloadCatalog, taskCatalog]);

  useEffect(() => {
    setLocalBuildError(null);
    setLocalBuildSuccess(null);
    setLocalBuildConsent(null);
    setLocalBuildConsentChecked(false);
    setLocalBuildConsentError(null);
    setShowProvisionDetails(false);
  }, [modelId, resolvedProcessingServerId]);

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
    () => (isSegmentation ? segmentationFallbackItems() : isClassification ? classificationFallbackItems() : detectionFallbackItems()),
    [isClassification, isSegmentation],
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
      const recommendedText = item.badgeIds.includes("recommended")
        ? ` • ${t("core.ui.processing_servers.vision_recommendations.badge.recommended")}`
        : "";
      const customText = item.custom ? ` • ${t("core.ui.pipelines.panels.yolo.model_custom_badge")}` : "";
      return {
        value: item.modelId,
        label: `${item.displayName}${recommendedText}${customText}`,
        item,
        isDisabled: item.availability !== "available" && item.modelId !== modelId,
      };
    });
  }, [catalogItems, modelId, showAdvanced, t]);
  const availableItems = useMemo(
    () => catalogItems.filter((item) => item.availability === "available"),
    [catalogItems],
  );
  const suggestedAvailableItem = useMemo(() => pickSuggestedAvailableModel(availableItems), [availableItems]);
  const selectedModelIncompatible = selectedCatalogItem?.availability === "incompatible";
  const basicModelItems = useMemo(() => {
    const next: VisionModelCatalogItem[] = [];
    const seen = new Set<string>();
    const preferred = catalogItems.filter((item) => item.availability === "available" || item.availability === "manifest_only");
    for (const item of [selectedCatalogItem, ...preferred].filter(Boolean) as VisionModelCatalogItem[]) {
      if (seen.has(item.modelId)) continue;
      next.push(item);
      seen.add(item.modelId);
    }
    return next.slice(0, isSegmentation ? 4 : 6);
  }, [catalogItems, selectedCatalogItem]);
  const manualInstallItem = selectedCatalogItem ?? null;
  const manualInstallFile = artifactFileName(manualInstallItem?.artifactPath || "");
  const manualInstallAcquisition = manualInstallItem?.acquisition ?? defaultAcquisitionForModelId(manualInstallItem?.modelId || "");
  const manualInstallNeedsExport = manualInstallAcquisition.artifactSource === "checkpoint_export_required";
  const manualLocalBuildActionable =
    !!manualInstallItem &&
    manualInstallNeedsExport &&
    manualInstallItem.localBuildSupported;
  const manualInstallBusy = ["queued", "downloading", "verifying", "installing"].includes(
    String(manualInstallItem?.installJob?.status || "").trim(),
  );
  const manualLocalBuildAction: "prepare" | "update" = manualInstallItem?.artifactExists ? "update" : "prepare";
  const manualSuggestedReadyItem =
    manualInstallItem && !manualInstallItem.artifactExists && suggestedAvailableItem?.modelId !== manualInstallItem.modelId
      ? suggestedAvailableItem
      : null;
  const manualArtifactActionKey = manualInstallItem?.artifactExists
    ? "core.ui.pipelines.panels.yolo.provisioning.action.upload_replace"
    : "core.ui.pipelines.panels.yolo.provisioning.action.upload_add";
  const manualLocalBuildActionKey =
    manualLocalBuildAction === "update"
      ? "core.ui.pipelines.panels.yolo.provisioning.action.local_update"
      : "core.ui.pipelines.panels.yolo.provisioning.action.local_prepare";
  const selectedProfileLabel = taskCatalog?.profile
    ? t(`core.ui.processing_servers.vision_recommendations.profile_label.${taskCatalog.profile}`, {}, taskCatalog.profile)
    : "";
  const selectedBadgeText = selectedCatalogItem?.badgeIds.length
    ? selectedCatalogItem.badgeIds
        .map((badgeId) => t(`core.ui.processing_servers.vision_recommendations.badge.${badgeId}`, {}, badgeId))
        .join(" • ")
    : "";
  const selectedModelMeta = [selectedBadgeText, selectedProfileLabel].filter(Boolean).join(" • ");
  const selectedModelHintKey = useMemo(() => modelHintTranslationKey(modelId), [modelId]);
  const manualInstallFailed = !!manualInstallItem?.installJob?.error || !!localBuildError;
  const classificationNeedsModelSetup = isClassification && !manualInstallItem;
  const classificationEmptyCatalog = isClassification && catalogItems.length === 0;
  const showBasicModelPicker = showAdvanced ? modelOptions.length > 0 : basicModelItems.length > 0;
  const provisionStatusTone = selectedModelIncompatible
    ? "unavailable"
    : manualInstallFailed
      ? "failed"
      : manualInstallBusy
        ? "busy"
        : manualInstallItem?.artifactExists
          ? "ready"
          : "missing";
  const provisionStatusLabel = t(
    `core.ui.pipelines.panels.yolo.provisioning.compact_state.${provisionStatusTone}`,
    {},
    provisionStatusTone,
  );
  const provisionSummary = selectedModelIncompatible
    ? t("core.ui.pipelines.panels.yolo.provisioning.summary_incompatible")
    : manualInstallBusy
      ? t("core.ui.pipelines.panels.yolo.provisioning.summary_busy")
      : manualInstallFailed
        ? t("core.ui.pipelines.panels.yolo.provisioning.summary_failed")
        : manualInstallItem?.artifactExists
          ? t("core.ui.pipelines.panels.yolo.provisioning.summary_ready")
          : manualLocalBuildActionable
            ? t("core.ui.pipelines.panels.yolo.provisioning.summary_missing_actionable")
            : t("core.ui.pipelines.panels.yolo.provisioning.summary_missing_manual");
  const showSelectedModelNarrative =
    !!selectedModelHintKey && (!manualInstallItem?.artifactExists || selectedModelIncompatible || showAdvanced);
  const provisionDetailsOpen = showProvisionDetails || selectedModelIncompatible || manualInstallFailed;

  useEffect(() => {
    if (!isDetection || modelId) return;
    const lastUsedModelId = readLastUsedDetectionModelId();
    const lastUsedInCurrentCatalog = lastUsedModelId
      ? catalogItems.find((item) => item.modelId === lastUsedModelId) ?? null
      : null;
    if (lastUsedModelId && !lastUsedInCurrentCatalog && !taskCatalog && !catalogError && serverStatus === null) {
      return;
    }
    const initialItem = pickInitialDetectionModel(catalogItems, lastUsedModelId);
    if (!initialItem?.modelId) return;
    onUpdateConfig((prev) => {
      const currentModelId = String((prev as any).model_id ?? "").trim();
      if (currentModelId) return prev;
      return {
        ...prev,
        model_id: initialItem.modelId,
      };
    });
  }, [catalogError, catalogItems, isDetection, modelId, onUpdateConfig, serverStatus, taskCatalog]);

  useEffect(() => {
    if (!isDetection || !modelId) return;
    writeLastUsedDetectionModelId(modelId);
  }, [isDetection, modelId]);

  useEffect(() => {
    if (!manualInstallItem?.installJob) return;
    setLocalBuildSuccess(null);
  }, [manualInstallItem?.installJob?.jobId, manualInstallItem?.installJob?.status]);

  const selectedModelOption = useMemo(
    () => modelOptions.find((item) => item.value === modelId) ?? null,
    [modelId, modelOptions],
  );

  const trackerOptions = useMemo<SelectOption[]>(
    () =>
      TRACKER_CHOICES.map((item) => ({
        value: item.value,
        label: t(item.labelKey, {}, item.value),
      })),
    [t],
  );

  const privacyPolicyLabels = useMemo(() => parsePrivacyPolicyLabels(privacyPolicyLabelsText), [privacyPolicyLabelsText]);
  const privacyPolicyMatchExpression = useMemo(
    () => (privacyPolicyLabels.length > 0 ? buildClassificationPrivacyMatchExpression(privacyPolicyLabels, privacyPolicyThreshold) : ""),
    [privacyPolicyLabels, privacyPolicyThreshold],
  );
  const privacyPolicyAllowExpression = useMemo(
    () => (privacyPolicyLabels.length > 0 ? buildClassificationPrivacyAllowExpression(privacyPolicyLabels, privacyPolicyThreshold) : ""),
    [privacyPolicyLabels, privacyPolicyThreshold],
  );
  const downstreamImageExposureSteps = useMemo(
    () =>
      steps
        .slice(index + 1)
        .filter((step) =>
          ["core.store_images", "core.notify", "home_assistant.notify", "stream.publish_video"].includes(
            String(step.operatorId || "").trim(),
          ),
        ),
    [index, steps],
  );
  const downstreamPrivacyGuardIndex = useMemo(
    () =>
      steps
        .slice(index + 1)
        .findIndex((step) => ["core.filter", "camera.artifact_privacy"].includes(String(step.operatorId || "").trim())),
    [index, steps],
  );
  const downstreamExposureIndex = useMemo(
    () =>
      steps
        .slice(index + 1)
        .findIndex((step) =>
          ["core.store_images", "core.notify", "home_assistant.notify", "stream.publish_video"].includes(
            String(step.operatorId || "").trim(),
          ),
        ),
    [index, steps],
  );
  const downstreamExposureProtected =
    downstreamExposureIndex >= 0 && downstreamPrivacyGuardIndex >= 0 && downstreamPrivacyGuardIndex < downstreamExposureIndex;
  const downstreamExposureLabels = useMemo(
    () =>
      downstreamImageExposureSteps.map((step) =>
        t(`core.ui.pipelines.operator_name.${step.operatorId}`, {}, String(step.operatorId || "").trim()),
      ),
    [downstreamImageExposureSteps, t],
  );

  const handleCustomOnnxSaved = useCallback(
    async (result: ProcessingServerVisionManifestImportResponse) => {
      setCustomOnnxSuccess(
        t(
          "core.ui.pipelines.panels.yolo.import_success",
          { modelId: result.model_id, task: result.task },
          `Imported ${result.model_id}`,
        ),
      );
      if ((isDetection && result.task === "detection") || (isClassification && result.task === "classification")) {
        onUpdateConfig((prev) => ({
          ...prev,
          model_id: result.model_id,
        }));
      }
      await reloadCatalog();
      setShowCustomOnnxWizard(false);
    },
    [isClassification, isDetection, onUpdateConfig, reloadCatalog, t],
  );

  const insertClassificationFilterStep = useCallback(() => {
    if (!isClassification || !privacyPolicyAllowExpression || !operatorsById["core.filter"]) return;
    onInsertStepAfter(stepUid, "core.filter", {
      enabled: true,
      preset_id: "",
      expression: privacyPolicyAllowExpression,
      invert: false,
      categories: [],
      lifecycles: [],
      artifact_names: [],
    });
  }, [isClassification, onInsertStepAfter, operatorsById, privacyPolicyAllowExpression, stepUid]);

  const insertClassificationArtifactPrivacyStep = useCallback(() => {
    if (!isClassification || !privacyPolicyMatchExpression || !operatorsById["camera.artifact_privacy"]) return;
    onInsertStepAfter(stepUid, "camera.artifact_privacy", {
      enabled: true,
      expression: privacyPolicyMatchExpression,
      invert: false,
    });
  }, [isClassification, onInsertStepAfter, operatorsById, privacyPolicyMatchExpression, stepUid]);

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

  const handleStartLocalBuild = useCallback(
    async (item: VisionModelCatalogItem | null, options: { force?: boolean; action?: "prepare" | "update" } = {}) => {
      if (!item || localBuildLoadingModelId) return;
      const action = options.action === "update" ? "update" : "prepare";
      setLocalBuildLoadingModelId(item.modelId);
      setLocalBuildError(null);
      setLocalBuildSuccess(null);
      try {
        await installProcessingServerVisionModel(resolvedProcessingServerId, item.modelId, {
          mode: "local_build",
          acknowledge_upstream_terms: true,
          force: !!options.force,
        });
        setLocalBuildSuccess(
          t(
            action === "update"
              ? "core.ui.pipelines.panels.yolo.provisioning.local_build_started_update"
              : "core.ui.pipelines.panels.yolo.provisioning.local_build_started_prepare",
            {
              serverId: resolvedProcessingServerId,
            },
            action === "update"
              ? `Local update started on ${resolvedProcessingServerId}.`
              : `Local build started on ${resolvedProcessingServerId}.`,
          ),
        );
        await reloadCatalog();
      } catch (error: any) {
        const message = String(error?.message ?? error);
        setLocalBuildError(message);
        throw new Error(message);
      } finally {
        setLocalBuildLoadingModelId("");
      }
    },
    [localBuildLoadingModelId, reloadCatalog, resolvedProcessingServerId, t],
  );

  const openLocalBuildConsent = useCallback((item: VisionModelCatalogItem | null) => {
    if (!item) return;
    setLocalBuildConsentError(null);
    if (!item.acquisition.explicitConsentRequired) {
      void handleStartLocalBuild(item, {
        force: !!item.artifactExists,
        action: item.artifactExists ? "update" : "prepare",
      }).catch(() => undefined);
      return;
    }
    setLocalBuildConsent({
      item,
      action: item.artifactExists ? "update" : "prepare",
    });
    setLocalBuildConsentChecked(false);
  }, [handleStartLocalBuild]);

  const closeLocalBuildConsent = useCallback(() => {
    if (localBuildConsentSubmitting) return;
    setLocalBuildConsent(null);
    setLocalBuildConsentChecked(false);
    setLocalBuildConsentError(null);
  }, [localBuildConsentSubmitting]);

  const confirmLocalBuildConsent = useCallback(async () => {
    if (!localBuildConsent) return;
    if (!localBuildConsentChecked) return;
    setLocalBuildConsentSubmitting(true);
    setLocalBuildConsentError(null);
    try {
      await handleStartLocalBuild(localBuildConsent.item, {
        force: localBuildConsent.action === "update",
        action: localBuildConsent.action,
      });
      setLocalBuildConsent(null);
      setLocalBuildConsentChecked(false);
    } catch (error: any) {
      setLocalBuildConsentError(String(error?.message ?? error));
    } finally {
      setLocalBuildConsentSubmitting(false);
    }
  }, [handleStartLocalBuild, localBuildConsent, localBuildConsentChecked]);

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
                  tracker_id: String(event.target.value || "byte_world").trim() || "byte_world",
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
        </>
      ) : (
        <>
          <div className="pipelinesStepHint">
            {t("core.ui.pipelines.panels.yolo.processing_server_hint", {
              serverId: resolvedProcessingServerId,
            })}
          </div>

          {showBasicModelPicker ? (
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
                  value={basicModelItems.some((item) => item.modelId === modelId) ? modelId : ""}
                  onChange={(event) => {
                    const nextModelId = String(event.target.value || "").trim();
                    onUpdateConfig((prev) => ({
                      ...prev,
                      model_id: nextModelId,
                    }));
                  }}
                >
                  <option value="">{t("core.ui.pipelines.panels.yolo.model_select_placeholder")}</option>
                  {basicModelItems.map((item) => (
                    <option key={item.modelId} value={item.modelId}>
                      {item.displayName}
                      {item.badgeIds.includes("recommended")
                        ? ` • ${t("core.ui.processing_servers.vision_recommendations.badge.recommended")}`
                        : ""}
                    </option>
                  ))}
                </select>
              )}
            </label>
          ) : null}
          <div className="pipelinesStepHint">
            {isClassification
              ? t("core.ui.pipelines.panels.yolo.classification_model_id_hint")
              : isSegmentation
              ? t("core.ui.pipelines.panels.yolo.segmentation_model_id_hint")
              : t("core.ui.pipelines.panels.yolo.model_id_hint")}
          </div>
          {classificationNeedsModelSetup ? (
            <div className="pipelinesOperatorConfigCard pipelinesProvisionCard" style={{ marginTop: 10 }}>
              <div className="cardHeaderRow">
                <div className="cardTitle">
                  {t(
                    classificationEmptyCatalog
                      ? "core.ui.pipelines.panels.yolo.classification_empty_state.title_empty"
                      : "core.ui.pipelines.panels.yolo.classification_empty_state.title_select",
                  )}
                </div>
              </div>
              <div className="cardBody">
                {t(
                  classificationEmptyCatalog
                    ? "core.ui.pipelines.panels.yolo.classification_empty_state.body_empty"
                    : "core.ui.pipelines.panels.yolo.classification_empty_state.body_select",
                )}
              </div>
              {classificationEmptyCatalog ? (
                <div className="pipelinesProvisionActions">
                  {customOnnxSupported ? (
                    <button
                      className="pillButton pillButtonPrimary"
                      type="button"
                      onClick={() => {
                        setCustomOnnxSuccess(null);
                        setShowHuggingFaceWizard(true);
                      }}
                    >
                      {t("core.ui.pipelines.panels.yolo.huggingface")}
                    </button>
                  ) : null}
                  {customOnnxSupported ? (
                    <button
                      className="pillButton"
                      type="button"
                      onClick={() => {
                        setCustomOnnxSuccess(null);
                        setShowCustomOnnxWizard(true);
                      }}
                    >
                      {t("core.ui.pipelines.panels.yolo.custom_onnx")}
                    </button>
                  ) : null}
                </div>
              ) : null}
              <div className="pipelinesProvisionActions pipelinesProvisionSecondaryActions">
                <button className="pillButton" type="button" onClick={() => void reloadCatalog()} disabled={catalogLoading}>
                  {t("core.ui.pipelines.panels.yolo.refresh_models")}
                </button>
                {onOpenProcessingServers ? (
                  <button className="pillButton" type="button" onClick={onOpenProcessingServers}>
                    {t("core.ui.pipelines.form.processing_server.manage")}
                  </button>
                ) : null}
              </div>
            </div>
          ) : null}
          {manualInstallItem ? (
            <div className="pipelinesOperatorConfigCard pipelinesProvisionCard" style={{ marginTop: 10 }}>
              <div className="cardHeaderRow">
                <div>
                  <div className="cardTitle">{manualInstallItem.displayName}</div>
                  {selectedModelMeta ? <div className="cardMeta">{selectedModelMeta}</div> : null}
                </div>
                <div className={`pipelinesProvisionStatus pipelinesProvisionStatus-${provisionStatusTone}`}>{provisionStatusLabel}</div>
              </div>
              {showSelectedModelNarrative ? <div className="cardBody">{t(selectedModelHintKey)}</div> : null}
              <div className="cardBody">{provisionSummary}</div>
              {manualSuggestedReadyItem ? (
                <div className="pipelinesStepHint">
                  {t(
                    "core.ui.pipelines.panels.yolo.model_recovery.recommended_ready",
                    { model: manualSuggestedReadyItem.displayName },
                    `Use ${manualSuggestedReadyItem.displayName} to continue now.`,
                  )}
                </div>
              ) : null}
              {manualInstallItem.installJob && manualInstallBusy ? (
                <div className="pipelinesStepHint">
                  {t("core.ui.pipelines.panels.yolo.local_build.job_progress", {
                    phase: t(
                      `core.ui.pipelines.panels.yolo.install_phase.${manualInstallItem.installJob.phase || "queued"}`,
                      {},
                      manualInstallItem.installJob.phase || "queued",
                    ),
                    progress: Math.max(0, Math.min(100, Math.round(manualInstallItem.installJob.progressPct || 0))),
                  })}
                </div>
              ) : null}
              {manualInstallItem.installJob?.error ? (
                <div className="errorText" style={{ marginTop: 8 }}>
                  {manualInstallItem.installJob.error}
                </div>
              ) : null}
              {localBuildSuccess ? <div className="settingsStatusMuted">{localBuildSuccess}</div> : null}
              {localBuildError ? <div className="errorText">{localBuildError}</div> : null}
              {!manualInstallItem.artifactExists || selectedModelIncompatible ? (
                <div className="pipelinesProvisionActions">
                  {manualInstallNeedsExport && manualLocalBuildActionable && !selectedModelIncompatible ? (
                    <button
                      className="pillButton pillButtonPrimary"
                      type="button"
                      onClick={() => openLocalBuildConsent(manualInstallItem)}
                      disabled={!!localBuildLoadingModelId || manualInstallBusy}
                    >
                      {localBuildLoadingModelId === manualInstallItem.modelId
                        ? t("core.ui.pipelines.panels.yolo.local_build.starting")
                        : t(manualLocalBuildActionKey)}
                    </button>
                  ) : !selectedModelIncompatible ? (
                    <button className="pillButton pillButtonPrimary" type="button" onClick={() => openArtifactModal(manualInstallItem)}>
                      {t(manualArtifactActionKey)}
                    </button>
                  ) : null}
                  {manualSuggestedReadyItem ? (
                    <button className="pillButton" type="button" onClick={applySuggestedAvailableModel}>
                      {t(
                        "core.ui.pipelines.panels.yolo.model_recovery.use_recommended",
                        { model: manualSuggestedReadyItem.displayName },
                        `Use ${manualSuggestedReadyItem.displayName}`,
                      )}
                    </button>
                  ) : null}
                </div>
              ) : null}
              <div className="pipelinesProvisionActions pipelinesProvisionSecondaryActions">
                <button className="pillButton" type="button" onClick={() => setShowProvisionDetails((prev) => !prev)}>
                  {t(
                    provisionDetailsOpen
                      ? "core.ui.pipelines.panels.yolo.provisioning.details_hide"
                      : "core.ui.pipelines.panels.yolo.provisioning.details_show",
                  )}
                </button>
              </div>
              {provisionDetailsOpen ? (
                <div className="pipelinesProvisionDetails">
                  {manualInstallFile ? (
                    <div className="pipelinesStepHint">
                      {t("core.ui.pipelines.panels.yolo.provisioning.expected_file", { file: manualInstallFile })}
                    </div>
                  ) : null}
                  {manualInstallNeedsExport && manualLocalBuildActionable ? (
                    <>
                      <div className="pipelinesStepHint">
                        {t(
                          manualLocalBuildAction === "update"
                            ? "core.ui.pipelines.panels.yolo.provisioning.local_build_update_hint"
                            : "core.ui.pipelines.panels.yolo.provisioning.local_build_prepare_hint",
                          {
                            runtime: manualInstallItem.localBuildRuntime || manualInstallItem.localBuildBackend || "local",
                          },
                        )}
                      </div>
                      <div className="pipelinesStepHint">
                        {t("core.ui.pipelines.panels.yolo.provisioning.manual_fallback_hint")}
                      </div>
                    </>
                  ) : null}
                  {manualInstallNeedsExport && !manualLocalBuildActionable && manualInstallItem.localBuildReason ? (
                    <div className="pipelinesStepHint">
                      {t("core.ui.pipelines.panels.yolo.provisioning.local_build_unavailable", {
                        reason: localBuildReasonLabel(manualInstallItem.localBuildReason, t),
                      })}
                    </div>
                  ) : null}
                  {manualInstallItem.acquisition.checkpointUrl || manualInstallItem.localBuildSourceLabel || manualInstallItem.acquisition.sourceUrl ? (
                    <div className="pipelinesStepHint">
                      {t("core.ui.pipelines.panels.yolo.provisioning.source", {
                        source:
                          manualInstallItem.acquisition.checkpointUrl ||
                          manualInstallItem.localBuildSourceLabel ||
                          manualInstallItem.acquisition.sourceUrl,
                      })}
                    </div>
                  ) : null}
                  <div className="pipelinesProvisionActions">
                    {(manualLocalBuildActionable || manualInstallItem.artifactExists) && !selectedModelIncompatible ? (
                      <button className="pillButton" type="button" onClick={() => openArtifactModal(manualInstallItem)}>
                        {t(manualArtifactActionKey)}
                      </button>
                    ) : null}
                    {manualInstallItem.artifactExists && manualInstallNeedsExport && manualLocalBuildActionable ? (
                      <button
                        className="pillButton"
                        type="button"
                        onClick={() => openLocalBuildConsent(manualInstallItem)}
                        disabled={!!localBuildLoadingModelId || manualInstallBusy}
                      >
                        {localBuildLoadingModelId === manualInstallItem.modelId
                          ? t("core.ui.pipelines.panels.yolo.local_build.starting")
                          : t(manualLocalBuildActionKey)}
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
                  {manualInstallItem.acquisition.checkpointUrl || manualInstallItem.acquisition.exportGuideUrl ? (
                    <div className="pipelinesProvisionLinks">
                      {manualInstallItem.acquisition.checkpointUrl ? (
                        <a className="pillButton" href={manualInstallItem.acquisition.checkpointUrl} target="_blank" rel="noreferrer">
                          {t("core.ui.pipelines.panels.yolo.artifact_modal.open_checkpoint_page")}
                        </a>
                      ) : null}
                      {manualInstallItem.acquisition.exportGuideUrl ? (
                        <a className="pillButton" href={manualInstallItem.acquisition.exportGuideUrl} target="_blank" rel="noreferrer">
                          {t("core.ui.pipelines.panels.yolo.artifact_modal.open_export_guide")}
                        </a>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          {catalogError ? <div className="errorText">{catalogError}</div> : null}

          {showAdvanced && !classificationEmptyCatalog ? (
            <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
              {customOnnxSupported ? (
                <button
                  className="pillButton"
                  type="button"
                  onClick={() => {
                    setCustomOnnxSuccess(null);
                    setShowHuggingFaceWizard(true);
                  }}
                >
                  {t("core.ui.pipelines.panels.yolo.huggingface")}
                </button>
              ) : null}
              {customOnnxSupported ? (
                <button
                  className="pillButton"
                  type="button"
                  onClick={() => {
                    setCustomOnnxSuccess(null);
                    setShowCustomOnnxWizard(true);
                  }}
                >
                  {t("core.ui.pipelines.panels.yolo.custom_onnx")}
                </button>
              ) : null}
            </div>
          ) : null}
          {customOnnxSuccess ? <div className="settingsStatusMuted">{customOnnxSuccess}</div> : null}

          {showAdvanced && selectedCatalogItem ? (
            <div className="pipelinesOperatorConfigCard" style={{ marginTop: 10 }}>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.selected_model_details")}</div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_runtime", { runtime: selectedCatalogItem.runtime || "onnxruntime" })}
              </div>
              <div className="pipelinesStepHint">
                {t("core.ui.pipelines.panels.yolo.details_artifact", {
                  format: selectedCatalogItem.artifactFormat || "n/a",
                  dtype: selectedCatalogItem.inputDtype || "float32",
                })}
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
                  providers: selectedCatalogItem.compatibleProviderIds.join(", ") || "n/a",
                })}
              </div>
              {selectedCatalogItem.acceleratorIds.length ? (
                <div className="pipelinesStepHint">
                  {t("core.ui.pipelines.panels.yolo.details_accelerators", {
                    accelerators: selectedCatalogItem.acceleratorIds.join(", "),
                  })}
                </div>
              ) : null}
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
                {t("core.ui.pipelines.panels.yolo.details_acquisition_mode", {
                  mode: t(
                    acquisitionModeTranslationKey(selectedCatalogItem.acquisition.mode),
                    {},
                    selectedCatalogItem.acquisition.mode || "guided_upload",
                  ),
                })}
              </div>
              {selectedCatalogItem.acquisition.builderBackend ? (
                <div className="pipelinesStepHint">
                  {t("core.ui.pipelines.panels.yolo.details_builder_backend", {
                    backend: t(
                      builderBackendTranslationKey(selectedCatalogItem.acquisition.builderBackend),
                      {},
                      selectedCatalogItem.acquisition.builderBackend,
                    ),
                  })}
                </div>
              ) : null}
              {selectedCatalogItem.acquisition.supportedPlatforms.length ? (
                <div className="pipelinesStepHint">
                  {t("core.ui.pipelines.panels.yolo.details_supported_platforms", {
                    platforms: selectedCatalogItem.acquisition.supportedPlatforms.join(", "),
                  })}
                </div>
              ) : null}
              {selectedCatalogItem.acquisition.explicitConsentRequired ? (
                <div className="pipelinesStepHint">
                  {t("core.ui.pipelines.panels.yolo.details_consent_required")}
                </div>
              ) : null}
              <div className="pipelinesStepHint">
                {selectedCatalogItem.custom
                  ? t("core.ui.pipelines.panels.yolo.details_source_custom")
                  : t("core.ui.pipelines.panels.yolo.details_source_official")}
              </div>
              {selectedCatalogItem.acquisition.checkpointUrl ||
              selectedCatalogItem.acquisition.configUrl ||
              selectedCatalogItem.acquisition.metafileUrl ||
              selectedCatalogItem.acquisition.paperUrl ? (
                <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                  {selectedCatalogItem.acquisition.checkpointUrl ? (
                    <a className="pillButton" href={selectedCatalogItem.acquisition.checkpointUrl} target="_blank" rel="noreferrer">
                      {t("core.ui.pipelines.panels.yolo.upstream_link.checkpoint")}
                    </a>
                  ) : null}
                  {selectedCatalogItem.acquisition.configUrl ? (
                    <a className="pillButton" href={selectedCatalogItem.acquisition.configUrl} target="_blank" rel="noreferrer">
                      {t("core.ui.pipelines.panels.yolo.upstream_link.config")}
                    </a>
                  ) : null}
                  {selectedCatalogItem.acquisition.metafileUrl ? (
                    <a className="pillButton" href={selectedCatalogItem.acquisition.metafileUrl} target="_blank" rel="noreferrer">
                      {t("core.ui.pipelines.panels.yolo.upstream_link.metafile")}
                    </a>
                  ) : null}
                  {selectedCatalogItem.acquisition.paperUrl ? (
                    <a className="pillButton" href={selectedCatalogItem.acquisition.paperUrl} target="_blank" rel="noreferrer">
                      {t("core.ui.pipelines.panels.yolo.upstream_link.paper")}
                    </a>
                  ) : null}
                </div>
              ) : null}
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
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.detect_emit_mode")}</span>
            <select
              className="pipelinesInput"
              value={detectEmitMode}
              onChange={(event) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  emit_mode: event.target.value,
                }));
              }}
            >
              <option value="events">{t("core.ui.pipelines.panels.yolo.detect_emit_mode.events")}</option>
              <option value="filter">{t("core.ui.pipelines.panels.yolo.detect_emit_mode.filter")}</option>
              <option value="annotate">{t("core.ui.pipelines.panels.yolo.detect_emit_mode.annotate")}</option>
            </select>
          </label>
          <div className="pipelinesStepHint">
            {detectEmitMode === "events"
              ? t("core.ui.pipelines.panels.yolo.detect_emit_mode_events_hint")
              : detectEmitMode === "filter"
                ? t("core.ui.pipelines.panels.yolo.detect_emit_mode_filter_hint")
                : t("core.ui.pipelines.panels.yolo.detect_emit_mode_annotate_hint")}
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

      {!isTracking && !isClassification ? (
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
                <span>{t("core.ui.pipelines.panels.yolo.open_confidence_threshold")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0}
                  max={1}
                  step={0.01}
                  value={openConfidence}
                  onChange={(nextValue) => {
                    const nextOpen = Math.max(0, Math.min(1, nextValue));
                    onUpdateConfig((prev) => ({
                      ...prev,
                      open_confidence_threshold: nextOpen,
                      continue_confidence_threshold: Math.min(
                        nextOpen,
                        Number.isFinite(Number((prev as any).continue_confidence_threshold))
                          ? Number((prev as any).continue_confidence_threshold)
                          : continueConfidence,
                      ),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.open_confidence_threshold_hint")}</div>

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.continue_confidence_threshold")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0}
                  max={1}
                  step={0.01}
                  value={continueConfidence}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      continue_confidence_threshold: Math.max(0, Math.min(openConfidence, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.continue_confidence_threshold_hint")}</div>

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

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.stitch_gap_seconds")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0.05}
                  max={3600}
                  step={0.5}
                  value={stitchGap}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      stitch_gap_seconds: Math.max(closeAfter, Math.min(3600, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.stitch_gap_hint")}</div>

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

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.use_world_anchor")}</span>
                <select
                  className="pipelinesInput"
                  value={useWorldAnchor}
                  onChange={(event) => {
                    const nextMode = String(event.target.value || "auto").trim().toLowerCase();
                    onUpdateConfig((prev) => ({
                      ...prev,
                      use_world_anchor: TRACKING_WORLD_ANCHOR_OPTIONS.includes(nextMode as any) ? nextMode : "auto",
                    }));
                  }}
                >
                  <option value="auto">{t("core.ui.pipelines.panels.yolo.use_world_anchor.auto")}</option>
                  <option value="always">{t("core.ui.pipelines.panels.yolo.use_world_anchor.always")}</option>
                  <option value="never">{t("core.ui.pipelines.panels.yolo.use_world_anchor.never")}</option>
                </select>
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.use_world_anchor_hint")}</div>

              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.world_match_distance_meters")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0}
                  max={1000}
                  step={0.1}
                  value={worldDistance}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      world_match_distance_meters: Math.max(0, Math.min(1000, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.world_match_distance_hint")}</div>
            </>
          ) : null}
        </>
      ) : null}

      {isDetection ? (
        <>
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
            </>
          ) : null}
        </>
      ) : null}

      {isClassification && !classificationNeedsModelSetup ? (
        <>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.classification_filter_hint")}</div>
          <div className="pipelinesOperatorConfigCard" style={{ marginTop: 10 }}>
            <div className="cardHeaderRow">
              <div className="cardTitle">{t("core.ui.pipelines.panels.yolo.classification_privacy.title")}</div>
            </div>
            <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.classification_privacy.hint")}</div>
            {downstreamExposureLabels.length > 0 ? (
              <div className={downstreamExposureProtected ? "pipelinesStepHint" : "pipelinesInlineError"} style={{ marginTop: 8 }}>
                {downstreamExposureProtected
                  ? t("core.ui.pipelines.panels.yolo.classification_privacy.downstream_protected", {
                      steps: downstreamExposureLabels.join(", "),
                    })
                  : t("core.ui.pipelines.panels.yolo.classification_privacy.downstream_needs_guard", {
                      steps: downstreamExposureLabels.join(", "),
                    })}
              </div>
            ) : (
              <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
                {t("core.ui.pipelines.panels.yolo.classification_privacy.downstream_none")}
              </div>
            )}
            <div className="pipelinesScalarGrid" style={{ marginTop: 10 }}>
              <label className="pipelinesLabel pipelinesScalarLabel">
                <span>{t("core.ui.pipelines.panels.yolo.classification_privacy.labels")}</span>
                <input
                  className="pipelinesInput"
                  type="text"
                  value={privacyPolicyLabelsText}
                  placeholder={t("core.ui.pipelines.panels.yolo.classification_privacy.labels_placeholder")}
                  onChange={(event) => setPrivacyPolicyLabelsText(event.target.value)}
                />
              </label>
              <label className="pipelinesLabel pipelinesScalarLabel">
                <span>{t("core.ui.pipelines.panels.yolo.classification_privacy.threshold")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={0}
                  max={1}
                  step={0.01}
                  value={Number.isFinite(privacyPolicyThreshold) ? Math.max(0, Math.min(1, privacyPolicyThreshold)) : 0.85}
                  onChange={(nextValue) => setPrivacyPolicyThreshold(Math.max(0, Math.min(1, nextValue)))}
                />
              </label>
            </div>
            <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.classification_privacy.labels_hint")}</div>
            <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
              <button
                className="pillButton"
                type="button"
                disabled={privacyPolicyLabels.length === 0 || !operatorsById["core.filter"]}
                onClick={insertClassificationFilterStep}
              >
                {t("core.ui.pipelines.panels.yolo.classification_privacy.insert_filter")}
              </button>
              <button
                className="pillButton"
                type="button"
                disabled={privacyPolicyLabels.length === 0 || !operatorsById["camera.artifact_privacy"]}
                onClick={insertClassificationArtifactPrivacyStep}
              >
                {t("core.ui.pipelines.panels.yolo.classification_privacy.insert_strip")}
              </button>
            </div>
            <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
              {t("core.ui.pipelines.panels.yolo.classification_privacy.action_hint")}
            </div>
          </div>
          {showAdvanced ? (
            <>
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.panels.yolo.classification_top_k")}</span>
                <PipelinesNumberInput
                  className="pipelinesInput"
                  min={1}
                  max={64}
                  step={1}
                  value={topK}
                  onChange={(nextValue) => {
                    onUpdateConfig((prev) => ({
                      ...prev,
                      top_k: Math.max(1, Math.min(64, nextValue)),
                    }));
                  }}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.classification_top_k_hint")}</div>
            </>
          ) : null}
        </>
      ) : null}

      {isSegmentation ? (
        <>
          {showAdvanced ? (
            <>
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

      <LocalBuildConsentModal
        open={!!localBuildConsent}
        action={localBuildConsent?.action || "prepare"}
        serverId={resolvedProcessingServerId}
        modelName={localBuildConsent?.item.displayName || ""}
        runtimeLabel={localBuildConsent?.item.localBuildRuntime || localBuildConsent?.item.localBuildBackend || "local"}
        sourceLabel={
          localBuildConsent?.item.acquisition.checkpointUrl ||
          localBuildConsent?.item.localBuildSourceLabel ||
          localBuildConsent?.item.acquisition.sourceUrl ||
          ""
        }
        checked={localBuildConsentChecked}
        submitting={localBuildConsentSubmitting}
        error={localBuildConsentError}
        extraHint={t("core.ui.pipelines.panels.yolo.provisioning.modal_manual_hint")}
        onToggleChecked={setLocalBuildConsentChecked}
        onClose={closeLocalBuildConsent}
        onConfirm={() => void confirmLocalBuildConsent()}
      />
      <CustomOnnxWizardModal
        open={showCustomOnnxWizard}
        serverId={resolvedProcessingServerId}
        task={isClassification ? "classification" : "detection"}
        onClose={() => setShowCustomOnnxWizard(false)}
        onSaved={handleCustomOnnxSaved}
      />
      <HuggingFaceImportModal
        open={showHuggingFaceWizard}
        serverId={resolvedProcessingServerId}
        task={isClassification ? "classification" : "detection"}
        onClose={() => setShowHuggingFaceWizard(false)}
        onSaved={handleCustomOnnxSaved}
      />

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
