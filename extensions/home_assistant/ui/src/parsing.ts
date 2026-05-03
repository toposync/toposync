import { DEFAULT_AIRFLOW_INTENSITY, DEFAULT_LAMP_INTENSITY, MAX_LAMP_INTENSITY, MIN_LAMP_INTENSITY } from "./constants";
import type { HomeAssistantItemRef, HomeAssistantServer } from "./types";

export function readString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function readFiniteNumber(value: unknown, fallback: number): number {
  const num = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(num) ? num : fallback;
}

export function readOptionalFiniteNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const num = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(num) ? num : null;
}

export function readLampIntensity(value: unknown): number {
  return clamp(readFiniteNumber(value, DEFAULT_LAMP_INTENSITY), MIN_LAMP_INTENSITY, MAX_LAMP_INTENSITY);
}

export function readAirflowIntensity(value: unknown): number {
  return clamp(readFiniteNumber(value, DEFAULT_AIRFLOW_INTENSITY), 0.2, 3.0);
}

export function readAirflowWidth(value: unknown, fallback: number): number {
  return clamp(readFiniteNumber(value, fallback), 0.06, 1.5);
}

export function readHexColor(value: unknown, fallback: string): string {
  const s = typeof value === "string" ? value.trim() : "";
  const match = /^#?([0-9a-fA-F]{6})$/.exec(s);
  if (!match) return fallback;
  return `#${match[1].toLowerCase()}`;
}

export function createUniqueId(): string {
  const cryptoAny = crypto as unknown as { randomUUID?: () => string };
  return cryptoAny.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function readHomeAssistantServers(settings: Record<string, unknown>): HomeAssistantServer[] {
  const raw = settings.servers;
  if (!Array.isArray(raw)) return [];
  const out: HomeAssistantServer[] = [];
  for (const item of raw) {
    const rec = readRecord(item);
    const id = readString(rec.id).trim();
    const name = readString(rec.name).trim();
    const host = readString(rec.host).trim();
    const apiKey = readString(rec.apiKey).trim();
    if (!id && !name && !host && !apiKey) continue;
    out.push({
      id: id || createUniqueId(),
      name,
      host,
      apiKey,
    });
  }
  return out;
}

export function isValidUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function readHomeAssistantItemRefs(value: unknown): HomeAssistantItemRef[] {
  if (!Array.isArray(value)) return [];
  const out: HomeAssistantItemRef[] = [];
  for (const item of value) {
    const rec = readRecord(item);
    const kind = readString(rec.kind);
    const id = readString(rec.id).trim();
    if ((kind !== "entity" && kind !== "device") || !id) continue;
    out.push({
      kind,
      id,
      name: readString(rec.name).trim(),
      domain: readString(rec.domain).trim(),
      icon: readString(rec.icon).trim(),
      device_id: readString(rec.device_id).trim(),
    });
  }
  return out;
}

export function itemValue(kind: "entity" | "device", id: string): string {
  return `${kind}:${id}`;
}
