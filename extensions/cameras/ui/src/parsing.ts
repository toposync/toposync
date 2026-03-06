import type {
  CameraConfig,
  CameraConnectionType,
  CameraControlPoint,
  CameraControlPointSet,
  CameraOnvifConfig,
  CameraPoseReference,
  CameraMappingQuality,
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
  const raw = settings.cameras;
  if (!Array.isArray(raw)) return [];
  const output: CameraConfig[] = [];
  for (const item of raw) {
    const record = readRecord(item);
    const id = readString(record.id).trim();
    if (!id) continue;
    const connectionType = readCameraConnectionType(record.connection_type);
    const onvif = readOnvifConfig(record.onvif);
    output.push({
      id,
      name: readString(record.name).trim(),
      connection_type: connectionType,
      rtsp_url: readString(record.rtsp_url).trim(),
      username: readString(record.username).trim(),
      password: readString(record.password).trim(),
      fps: Math.max(1, Math.min(60, readFiniteNumber(record.fps, 5))),
      onvif,
    });
  }
  return output;
}

export function readCameraConnectionType(value: unknown): CameraConnectionType {
  const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (raw === "onvif") return "onvif";
  return "rtsp";
}

export function readOnvifConfig(value: unknown): CameraOnvifConfig | null {
  const record = readRecord(value);
  const deviceId = readString(record.device_id).trim();
  const xaddr = readString(record.xaddr).trim();
  if (!xaddr) return null;
  const mediaXaddr = readString(record.media_xaddr).trim();
  const ptzXaddr = readString(record.ptz_xaddr).trim();
  const profileToken = readString(record.profile_token).trim();
  const profileName = readString(record.profile_name).trim();
  const hardware = readString(record.hardware).trim();
  return {
    device_id: deviceId || undefined,
    xaddr,
    media_xaddr: mediaXaddr || undefined,
    ptz_xaddr: ptzXaddr || undefined,
    profile_token: profileToken || undefined,
    profile_name: profileName || undefined,
    hardware: hardware || undefined,
  };
}
