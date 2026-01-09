export function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

export function readNumber(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

export function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

export function readBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function readImageMode(value: unknown, fallback: ImageMode): ImageMode {
  if (value === "overlay" || value === "tracing") return value;
  return fallback;
}

export function readBlendMode(value: unknown, fallback: BlendMode): BlendMode {
  if (value === "normal" || value === "multiply") return value;
  return fallback;
}

export type ImageMode = "overlay" | "tracing";
export type BlendMode = "normal" | "multiply";

