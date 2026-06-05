export const DEFAULT_DETECTION_MODEL_ID = "rfdetr_det_medium";
export const DEFAULT_DETECTION_MODEL_NAME = "RF-DETR Medium";

export type DetectionModelAvailability = "available" | "missing" | "preparing" | "incompatible" | "unknown";

export type DetectionModelInstallJob = {
  status: string;
  phase: string;
  progressPct: number | null;
  error: string;
};

export type DetectionModelCatalogItem = {
  modelId: string;
  displayName: string;
  availability: DetectionModelAvailability;
  artifactExists: boolean;
  localBuildSupported: boolean;
  localBuildReason: string;
  localBuildRuntime: string;
  localBuildSourceLabel: string;
  localBuildMissingTools: string[];
  explicitConsentRequired: boolean;
  installJob: DetectionModelInstallJob | null;
  recommended: boolean;
};

const ACTIVE_INSTALL_STATUSES = new Set(["queued", "running", "downloading", "verifying", "installing", "building"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function readRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function readString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function readBoolean(value: unknown): boolean {
  return value === true;
}

function readNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function readInstallJob(value: unknown): DetectionModelInstallJob | null {
  const raw = readRecord(value);
  const status = readString(raw.status);
  if (!status) return null;
  return {
    status,
    phase: readString(raw.phase),
    progressPct: readNumber(raw.progress_pct ?? raw.progressPct ?? raw.progress),
    error: readString(raw.error),
  };
}

export function isActiveDetectionModelInstall(item: DetectionModelCatalogItem | null | undefined): boolean {
  const status = readString(item?.installJob?.status).toLowerCase();
  return Boolean(status && ACTIVE_INSTALL_STATUSES.has(status));
}

function normalizeAvailability(raw: unknown, artifactExists: boolean, installJob: DetectionModelInstallJob | null): DetectionModelAvailability {
  if (installJob && ACTIVE_INSTALL_STATUSES.has(installJob.status.toLowerCase())) return "preparing";
  const value = readString(raw).toLowerCase();
  if (value === "available" || value === "ready" || value === "installed") return "available";
  if (value === "preparing" || value === "installing" || value === "building") return "preparing";
  if (value === "incompatible" || value === "unsupported") return "incompatible";
  if (value === "missing" || value === "manifest_only" || value === "unavailable" || value === "not_available") return "missing";
  if (artifactExists) return "available";
  return "missing";
}

function normalizeModel(rawValue: unknown): DetectionModelCatalogItem | null {
  const raw = readRecord(rawValue);
  const acquisition = readRecord(raw.acquisition);
  const modelId = readString(raw.model_id ?? raw.modelId ?? raw.id);
  if (!modelId) return null;
  const artifactExists = readBoolean(raw.artifact_exists ?? raw.artifactExists);
  const installJob = readInstallJob(raw.install_job ?? raw.installJob ?? raw.job);
  const localBuildMissingToolsRaw = raw.local_build_missing_tools ?? raw.localBuildMissingTools;
  return {
    modelId,
    displayName: readString(raw.display_name ?? raw.displayName ?? raw.name) || modelId,
    availability: normalizeAvailability(raw.availability ?? raw.status, artifactExists, installJob),
    artifactExists,
    localBuildSupported: readBoolean(raw.local_build_supported ?? raw.localBuildSupported),
    localBuildReason:
      readString(raw.local_build_reason ?? raw.localBuildReason) || (artifactExists ? "ok" : "unsupported"),
    localBuildRuntime: readString(raw.local_build_runtime ?? raw.localBuildRuntime),
    localBuildSourceLabel:
      readString(raw.local_build_source_label ?? raw.localBuildSourceLabel) ||
      readString(acquisition.checkpoint_url ?? acquisition.source_url ?? acquisition.url ?? acquisition.source_label),
    localBuildMissingTools: Array.isArray(localBuildMissingToolsRaw)
      ? localBuildMissingToolsRaw.map((value: unknown) => readString(value)).filter(Boolean)
      : [],
    explicitConsentRequired: readBoolean(raw.explicit_consent_required ?? acquisition.explicit_consent_required),
    installJob,
    recommended: modelId === DEFAULT_DETECTION_MODEL_ID,
  };
}

export function readDetectionModelCatalog(statusPayload: unknown): DetectionModelCatalogItem[] {
  const root = readRecord(statusPayload);
  const status = readRecord(root.status);
  const source = Object.keys(status).length ? status : root;
  const vision = readRecord(source.vision);
  const taskCatalogs = readRecord(vision.task_catalogs ?? vision.taskCatalogs);
  const detection = readRecord(taskCatalogs.detection);
  const rawItems = Array.isArray(detection.items) ? detection.items : [];
  const byId = new Map<string, DetectionModelCatalogItem>();
  for (const rawItem of rawItems) {
    const item = normalizeModel(rawItem);
    if (item) byId.set(item.modelId, item);
  }
  if (!byId.has(DEFAULT_DETECTION_MODEL_ID)) {
    byId.set(DEFAULT_DETECTION_MODEL_ID, {
      modelId: DEFAULT_DETECTION_MODEL_ID,
      displayName: DEFAULT_DETECTION_MODEL_NAME,
      availability: "missing",
      artifactExists: false,
      localBuildSupported: false,
      localBuildReason: "catalog_missing",
      localBuildRuntime: "",
      localBuildSourceLabel: "",
      localBuildMissingTools: [],
      explicitConsentRequired: true,
      installJob: null,
      recommended: true,
    });
  }
  return [...byId.values()].sort((left, right) => {
    if (left.modelId === DEFAULT_DETECTION_MODEL_ID) return -1;
    if (right.modelId === DEFAULT_DETECTION_MODEL_ID) return 1;
    return left.displayName.localeCompare(right.displayName);
  });
}

export function findDetectionModel(
  items: DetectionModelCatalogItem[],
  modelId: string,
): DetectionModelCatalogItem | null {
  const normalized = String(modelId || "").trim();
  return items.find((item) => item.modelId === normalized) ?? null;
}

export function isDetectionModelReady(item: DetectionModelCatalogItem | null | undefined): boolean {
  return Boolean(item && item.availability === "available");
}

export function canPrepareDetectionModel(item: DetectionModelCatalogItem | null | undefined): boolean {
  return Boolean(item && !isDetectionModelReady(item) && !isActiveDetectionModelInstall(item) && item.localBuildSupported);
}
