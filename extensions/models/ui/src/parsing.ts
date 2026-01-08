import { MAXIMUM_MODEL_SCALE, MINIMUM_MODEL_SCALE } from "./constants";
import type { Vector3 } from "./types";

export function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

export function readVector3(value: unknown, fallback: Vector3): Vector3 {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fallback;
  const record = value as Record<string, unknown>;
  return {
    x: readNumber(record.x, fallback.x),
    y: readNumber(record.y, fallback.y),
    z: readNumber(record.z, fallback.z),
  };
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function readScale(value: unknown, fallback = 1): number {
  return clamp(readNumber(value, fallback), MINIMUM_MODEL_SCALE, MAXIMUM_MODEL_SCALE);
}

