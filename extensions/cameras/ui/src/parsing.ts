import type { CameraConfig, CameraConnectionType, CameraOnvifConfig, ControlPoint } from "./types";

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

export function createDefaultControlPoints(count = 4): ControlPoint[] {
  const clampedCount = Math.max(1, Math.min(12, Math.floor(count)));
  return Array.from({ length: clampedCount }, (_, index) => ({
    id: createUniqueId(),
    label: labelForIndex(index),
    image: null,
    world: null,
  }));
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
