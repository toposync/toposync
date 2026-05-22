import type {
  CameraConfig,
  CameraConnectionType,
  CameraControlPoint,
  CameraControlPointSet,
  CameraIngestConfig,
  CameraIngestMode,
  CameraOnvifConfig,
  CameraPoseReference,
  CameraMappingQuality,
  CameraSourceConfig,
  CameraSourceOriginConfig,
  CameraSourceRole,
  CameraStreamProfile,
} from "./types";

export function readString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function createUniqueId(): string {
  const cryptoAny = crypto as unknown as { randomUUID?: () => string };
  return cryptoAny.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function labelForIndex(index: number): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  if (index >= 0 && index < alphabet.length) return alphabet[index];
  return String(index + 1);
}

export function readFiniteNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function readNormalizedPoint(value: unknown): { x: number; y: number } | null {
  const record = readRecord(value);
  const x = readFiniteNumber(record.x, NaN);
  const y = readFiniteNumber(record.y, NaN);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
}

export function readWorldPoint(value: unknown): { x: number; z: number } | null {
  const record = readRecord(value);
  const x = readFiniteNumber(record.x, NaN);
  const z = readFiniteNumber(record.z, NaN);
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  return { x, z };
}

export function readOptionalFiniteNumber(value: unknown): number | null {
  const parsed = readFiniteNumber(value, NaN);
  return Number.isFinite(parsed) ? parsed : null;
}

export function readCameraControlPoints(value: unknown): CameraControlPoint[] {
  if (!Array.isArray(value)) return [];
  const output: CameraControlPoint[] = [];
  for (let index = 0; index < value.length; index += 1) {
    const record = readRecord(value[index]);
    const id = readString(record.id).trim();
    if (!id) continue;
    const label = readString(record.label).trim() || labelForIndex(index);
    output.push({
      id,
      label,
      image: readNormalizedPoint(record.image),
      world: readWorldPoint(record.world),
    });
  }
  return output;
}

export function createDefaultControlPoints(count = 4): CameraControlPoint[] {
  const clampedCount = Math.max(1, Math.min(12, Math.floor(count)));
  return Array.from({ length: clampedCount }, (_, index) => ({
    id: createUniqueId(),
    label: labelForIndex(index),
    image: null,
    world: null,
  }));
}

export function readCameraPoseReference(value: unknown): CameraPoseReference | null {
  const record = readRecord(value);
  const pan = readOptionalFiniteNumber(record.pan);
  const tilt = readOptionalFiniteNumber(record.tilt);
  const zoom = readOptionalFiniteNumber(record.zoom);
  const presetToken = readString(record.preset_token).trim();
  const presetName = readString(record.preset_name).trim();
  if (pan === null && tilt === null && zoom === null && !presetToken && !presetName) return null;
  return {
    pan,
    tilt,
    zoom,
    preset_token: presetToken || null,
    preset_name: presetName || null,
  };
}

export function readControlPointSets(value: unknown): CameraControlPointSet[] {
  if (!Array.isArray(value)) return [];
  const output: CameraControlPointSet[] = [];
  for (let index = 0; index < value.length; index += 1) {
    const record = readRecord(value[index]);
    const id = readString(record.id).trim();
    if (!id) continue;
    output.push({
      id,
      label: readString(record.label).trim() || `View ${index + 1}`,
      pose_reference: readCameraPoseReference(record.pose_reference),
      control_points: readCameraControlPoints(record.control_points),
    });
  }
  return output;
}

export function createDefaultControlPointSet(
  index = 0,
  options?: { label?: string; controlPoints?: CameraControlPoint[]; poseReference?: CameraPoseReference | null },
): CameraControlPointSet {
  return {
    id: createUniqueId(),
    label: options?.label?.trim() || (index === 0 ? "Main view" : `View ${index + 1}`),
    pose_reference: options?.poseReference ?? null,
    control_points: options?.controlPoints?.map((point) => ({ ...point })) ?? createDefaultControlPoints(4),
  };
}

export function duplicateControlPointSetForNewView(source: CameraControlPointSet, index: number): CameraControlPointSet {
  return {
    id: createUniqueId(),
    label: `View ${index + 1}`,
    pose_reference: null,
    control_points: source.control_points.map((point) => ({
      id: createUniqueId(),
      label: point.label,
      image: null,
      world: point.world ? { ...point.world } : null,
    })),
  };
}

export function summarizeControlPointSetQuality(controlPointSet: CameraControlPointSet): CameraMappingQuality {
  const completePoints = controlPointSet.control_points.filter((point) => point.image && point.world).length;
  const imagePoints = controlPointSet.control_points
    .filter((point): point is CameraControlPoint & { image: { x: number; y: number } } => Boolean(point.image && point.world))
    .map((point) => ({ x: point.image!.x, y: point.image!.y }));
  const hullAreaRatio = convexHullAreaRatio(imagePoints);
  let status: CameraMappingQuality["status"] = "incomplete";
  if (completePoints >= 4) status = hullAreaRatio >= 0.02 ? "good" : "review";
  return {
    status,
    complete_points: completePoints,
    convex_hull_area_ratio_uv: hullAreaRatio,
    is_pose_bound: Boolean(controlPointSet.pose_reference),
  };
}

function convexHullAreaRatio(points: Array<{ x: number; y: number }>): number {
  if (points.length < 3) return 0;
  const unique = Array.from(new Map(points.map((point) => [`${point.x}:${point.y}`, point] as const)).values()).sort((a, b) =>
    a.x === b.x ? a.y - b.y : a.x - b.x,
  );
  if (unique.length < 3) return 0;
  const cross = (origin: { x: number; y: number }, a: { x: number; y: number }, b: { x: number; y: number }) =>
    (a.x - origin.x) * (b.y - origin.y) - (a.y - origin.y) * (b.x - origin.x);
  const lower: Array<{ x: number; y: number }> = [];
  for (const point of unique) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) lower.pop();
    lower.push(point);
  }
  const upper: Array<{ x: number; y: number }> = [];
  for (let index = unique.length - 1; index >= 0; index -= 1) {
    const point = unique[index];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) upper.pop();
    upper.push(point);
  }
  const hull = [...lower.slice(0, -1), ...upper.slice(0, -1)];
  if (hull.length < 3) return 0;
  let area = 0;
  for (let index = 0; index < hull.length; index += 1) {
    const point = hull[index];
    const next = hull[(index + 1) % hull.length];
    area += point.x * next.y - next.x * point.y;
  }
  return Math.max(0, Math.min(1, Math.abs(area) / 2));
}

export function parseCameras(settings: Record<string, unknown>): CameraConfig[] {
  const raw = Array.isArray(settings.devices) ? settings.devices : [];
  const output: CameraConfig[] = [];
  for (const item of raw) {
    const device = readRecord(item);
    const id = readString(device.id).trim();
    if (!id) continue;
    const controlRecord = readRecord(device.control);
    const controlType = readString(controlRecord.type).trim().toLowerCase() === "onvif" ? "onvif" : "none";
    output.push({
      id,
      name: readString(device.name).trim(),
      enabled: typeof device.enabled === "boolean" ? device.enabled : true,
      control: { type: controlType },
      onvif: controlType === "onvif" ? readOnvifConfig(device.onvif) ?? { xaddr: "", username: "", password: "" } : null,
      sources: readCameraSources(device.sources),
      metadata: readRecord(device.metadata),
    });
  }
  return output;
}

export function serializeCameras(settings: CameraConfig[]): Record<string, unknown> {
  return {
    schema_version: 4,
    devices: settings.map((camera) => ({
      id: camera.id,
      name: camera.name,
      kind: "camera",
      enabled: camera.enabled,
      clock_domain: `device:${camera.id}`,
      control: { type: camera.control?.type === "onvif" ? "onvif" : "none" },
      onvif: camera.control?.type === "onvif" ? camera.onvif ?? { xaddr: "", username: "", password: "" } : null,
      sources: normalizeCameraSourcesForSave(camera.sources),
      metadata: camera.metadata ?? {},
    })),
  };
}

export function createDefaultCameraSource(index = 0, options?: Partial<CameraSourceConfig>): CameraSourceConfig {
  const id = options?.id?.trim() || (index === 0 ? "main" : `source_${index + 1}`);
  return {
    id,
    name: options?.name?.trim() || (index === 0 ? "Principal" : `Fonte ${index + 1}`),
    enabled: options?.enabled ?? true,
    is_default: options?.is_default ?? index === 0,
    kind: options?.kind ?? "video",
    role: options?.role ?? (index === 0 ? "main" : "custom"),
    view_id: options?.view_id?.trim() || (index === 0 ? "main" : id),
    origin: normalizeSourceOrigin(options?.origin),
    video: {
      width: readOptionalFiniteNumber(options?.video?.width),
      height: readOptionalFiniteNumber(options?.video?.height),
      fps: readOptionalFiniteNumber(options?.video?.fps),
      codec: options?.video?.codec?.trim() || null,
    },
    ingest: readCameraIngestConfig(options?.ingest),
    metadata: options?.metadata ?? {},
  };
}

export function readCameraSources(value: unknown): CameraSourceConfig[] {
  if (!Array.isArray(value)) return [];
  const output: CameraSourceConfig[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const record = readRecord(item);
    const id = readString(record.id).trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const origin = normalizeSourceOrigin(record.origin);
    const video = readRecord(record.video);
    output.push({
      id,
      name: readString(record.name).trim() || id,
      enabled: typeof record.enabled === "boolean" ? record.enabled : true,
      is_default: Boolean(record.is_default),
      kind: readString(record.kind).trim().toLowerCase() === "audio" ? "audio" : readString(record.kind).trim().toLowerCase() === "data" ? "data" : "video",
      role: readCameraSourceRole(record.role),
      view_id: readString(record.view_id).trim() || id,
      origin,
      video: {
        width: readOptionalFiniteNumber(video.width),
        height: readOptionalFiniteNumber(video.height),
        fps: readOptionalFiniteNumber(video.fps),
        codec: readString(video.codec).trim() || null,
      },
      ingest: readCameraIngestConfig(record.ingest),
      metadata: readRecord(record.metadata),
    });
  }
  return normalizeCameraSourcesForUi(output);
}

export function normalizeCameraSourcesForUi(sources: CameraSourceConfig[]): CameraSourceConfig[] {
  let defaultSeen = false;
  return sources.map((source, index) => {
    const isDefault = source.kind === "video" && source.enabled && source.is_default && !defaultSeen;
    if (isDefault) defaultSeen = true;
    return {
      ...createDefaultCameraSource(index, source),
      is_default: isDefault,
    };
  });
}

function normalizeCameraSourcesForSave(sources: CameraSourceConfig[]): CameraSourceConfig[] {
  const normalized = normalizeCameraSourcesForUi(sources);
  const hasDefault = normalized.some((source) => source.kind === "video" && source.enabled && source.is_default);
  if (hasDefault) return normalized;
  let defaultAssigned = false;
  return normalized.map((source) => {
    if (!defaultAssigned && source.kind === "video" && source.enabled) {
      defaultAssigned = true;
      return { ...source, is_default: true };
    }
    return source;
  });
}

export function normalizeSourceOrigin(value: unknown): CameraSourceOriginConfig {
  const record = readRecord(value);
  const type = readString(record.type).trim().toLowerCase() === "onvif_profile" ? "onvif_profile" : "rtsp";
  return {
    type,
    rtsp_url: readString(record.rtsp_url).trim(),
    stream_username: readString(record.stream_username).trim(),
    stream_password: readString(record.stream_password).trim(),
    profile_token: readString(record.profile_token).trim() || null,
    profile_name: readString(record.profile_name).trim() || null,
    has_ptz: Boolean(record.has_ptz),
    metadata: readRecord(record.metadata),
  };
}

export function readCameraSourceRole(value: unknown): CameraSourceRole {
  const role = readString(value).trim().toLowerCase();
  if (role === "main" || role === "sub" || role === "zoom" || role === "custom") return role;
  return "custom";
}

export function readCameraIngestConfig(value: unknown): CameraIngestConfig {
  const record = readRecord(value);
  const rawMode = readString(record.mode).trim().toLowerCase();
  const mode: CameraIngestMode =
    rawMode === "runtime_local" || rawMode === "runtime-local" || rawMode === "runtime"
      ? "runtime_local"
      : rawMode === "direct" || rawMode === "external" || rawMode === "none"
        ? "direct"
        : "centralized";
  const hostServerId = readString(record.host_server_id).trim().toLowerCase() || "local";
  const directOverride = readFiniteNumber(record.direct_override_until_unix, NaN);
  return {
    mode,
    host_server_id: mode === "centralized" ? hostServerId : "local",
    direct_override_until_unix: Number.isFinite(directOverride) && directOverride > 0 ? directOverride : null,
  };
}

export function readCameraConnectionType(value: unknown): CameraConnectionType {
  const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (raw === "onvif") return "onvif";
  return "rtsp";
}

export function readCameraStreamProfile(value: unknown, connectionType: CameraConnectionType): CameraStreamProfile {
  if (connectionType !== "onvif") return "custom";
  const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (raw === "custom") return "custom";
  return "onvif";
}

export function readOnvifConfig(
  value: unknown,
  legacy?: { legacyUsername?: string; legacyPassword?: string },
): CameraOnvifConfig | null {
  const record = readRecord(value);
  const deviceId = readString(record.device_id).trim();
  const xaddr = readString(record.xaddr).trim();
  if (!xaddr) return null;
  const username = readString(record.username).trim() || legacy?.legacyUsername?.trim() || "";
  const password = readString(record.password).trim() || legacy?.legacyPassword?.trim() || "";
  const mediaXaddr = readString(record.media_xaddr).trim();
  const ptzXaddr = readString(record.ptz_xaddr).trim();
  const profileToken = readString(record.profile_token).trim();
  const profileName = readString(record.profile_name).trim();
  const ptzProfileToken = readString(record.ptz_profile_token).trim();
  const hardware = readString(record.hardware).trim();
  return {
    device_id: deviceId || undefined,
    xaddr,
    username: username || undefined,
    password: password || undefined,
    media_xaddr: mediaXaddr || undefined,
    ptz_xaddr: ptzXaddr || undefined,
    profile_token: profileToken || undefined,
    profile_name: profileName || undefined,
    ptz_profile_token: ptzProfileToken || undefined,
    hardware: hardware || undefined,
  };
}
