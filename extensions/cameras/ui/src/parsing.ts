import type { CameraConfig, CameraDetection, ControlPoint, DetectionCondition, ProcessingServer } from "./types";
import { YOLO_LEGACY_CATEGORY_MAP, YOLO_V12_CATEGORIES } from "./yolo";

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

export function readControlPoints(value: unknown): ControlPoint[] {
  if (!Array.isArray(value)) return [];
  const output: ControlPoint[] = [];
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

export function readDetectionCondition(value: unknown): DetectionCondition | null {
  const record = readRecord(value);
  const kind = readString(record.kind).trim();
  if (kind === "motion") return { kind: "motion" };
  if (kind === "ha_sensor") return { kind: "ha_sensor", entity_id: readString(record.entity_id).trim() };
  if (kind === "ha_state") {
    return {
      kind: "ha_state",
      entity_id: readString(record.entity_id).trim(),
      state: readString(record.state).trim(),
    };
  }
  if (kind === "object") {
    const rawCategory = readString(record.category).trim();
    const normalized = YOLO_LEGACY_CATEGORY_MAP[rawCategory] ?? rawCategory;
    const category = YOLO_V12_CATEGORIES.find((c) => c === normalized);
    if (!category) return null;
    return { kind: "object", category };
  }
  return null;
}

export function readCameraDetections(value: unknown): CameraDetection[] {
  if (!Array.isArray(value)) return [];
  const output: CameraDetection[] = [];
  for (const item of value) {
    const record = readRecord(item);
    const id = readString(record.id).trim();
    if (!id) continue;
    const trigger = readDetectionCondition(record.trigger) ?? { kind: "motion" };
    const filters = Array.isArray(record.filters) ? record.filters.map(readDetectionCondition).filter(Boolean) : [];
    output.push({ id, trigger, filters: filters as DetectionCondition[] });
  }
  return output;
}

export function createDefaultControlPoints(count = 4): ControlPoint[] {
  const clampedCount = Math.max(1, Math.min(12, Math.floor(count)));
  return Array.from({ length: clampedCount }, (_, index) => ({
    id: createUniqueId(),
    label: labelForIndex(index),
    image: null,
    world: null,
  }));
}

export function parseProcessingServers(settings: Record<string, unknown>): ProcessingServer[] {
  const raw = settings.processing_servers;
  if (!Array.isArray(raw)) return [];
  const output: ProcessingServer[] = [];
  for (const item of raw) {
    const record = readRecord(item);
    const id = readString(record.id).trim();
    if (!id) continue;
    output.push({
      id,
      name: readString(record.name).trim(),
      url: readString(record.url).trim(),
      username: readString(record.username).trim(),
      password: readString(record.password).trim(),
    });
  }
  return output;
}

export function parseCameras(settings: Record<string, unknown>): CameraConfig[] {
  const raw = settings.cameras;
  if (!Array.isArray(raw)) return [];
  const output: CameraConfig[] = [];
  for (const item of raw) {
    const record = readRecord(item);
    const id = readString(record.id).trim();
    if (!id) continue;
    output.push({
      id,
      name: readString(record.name).trim(),
      connection_type: "rtsp",
      rtsp_url: readString(record.rtsp_url).trim(),
      username: readString(record.username).trim(),
      password: readString(record.password).trim(),
      fps: Math.max(1, Math.min(60, readFiniteNumber(record.fps, 5))),
      processing_server_id: readString(record.processing_server_id).trim(),
      detections: readCameraDetections(record.detections),
    });
  }
  return output;
}

