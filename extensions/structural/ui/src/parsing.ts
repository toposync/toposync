import type { PlanePoint } from "@toposync/plugin-api";

import { AREA_FILL_STORAGE_KEY, DEFAULT_AREA_FILL_COLOR } from "./constants";

export function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function isValidHexColor(value: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(value);
}

export function loadAreaFillColor(): string {
  try {
    const stored = localStorage.getItem(AREA_FILL_STORAGE_KEY);
    if (stored && isValidHexColor(stored)) return stored;
  } catch {
    // ignore
  }
  return DEFAULT_AREA_FILL_COLOR;
}

export function saveAreaFillColor(fill: string): void {
  try {
    if (!isValidHexColor(fill)) return;
    localStorage.setItem(AREA_FILL_STORAGE_KEY, fill);
  } catch {
    // ignore
  }
}

export function readPlanePoint(value: unknown, fallback: PlanePoint): PlanePoint {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fallback;
  const record = value as Record<string, unknown>;
  return { x: readNumber(record.x, fallback.x), z: readNumber(record.z, fallback.z) };
}

export function readPlanePointArray(value: unknown): PlanePoint[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => readPlanePoint(item, { x: 0, z: 0 }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.z));
}
