export type WallOpeningKind = "opening" | "door" | "window";

export type WallOpening = {
  id: string;
  kind: WallOpeningKind;
  center_m: number;
  width_m: number;
  y_min_m?: number;
  y_max_m?: number;
  color?: string;
  texture?: string;
  external_ref?: string;
};

export type ResolvedWallOpening = {
  id: string;
  kind: WallOpeningKind;
  center_m: number;
  width_m: number;
  start_m: number;
  end_m: number;
  y_min_m: number;
  y_max_m: number;
  color?: string;
  texture?: string;
  external_ref?: string;
};

export const MIN_OPENING_WIDTH_M = 0.25;
export const MIN_OPENING_HEIGHT_M = 0.08;

const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function asFiniteNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asOptionalFiniteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asNonEmptyString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function isOpeningKind(value: unknown): value is WallOpeningKind {
  return value === "opening" || value === "door" || value === "window";
}

function defaultWidthForKind(kind: WallOpeningKind): number {
  if (kind === "door") return 0.9;
  if (kind === "window") return 1.2;
  return 1.0;
}

export function defaultColorForKind(kind: WallOpeningKind): string | undefined {
  if (kind === "door") return "#8f806a";
  if (kind === "window") return "#b9d8f4";
  return undefined;
}

export function defaultTextureForKind(kind: WallOpeningKind): string {
  if (kind === "door") return "wood";
  if (kind === "window") return "glass";
  return "none";
}

export function newWallOpeningId(): string {
  const c = crypto as unknown as { randomUUID?: () => string };
  return c.randomUUID?.() ?? `opening-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeOpening(value: unknown): WallOpening | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;

  const kind = isOpeningKind(record.kind) ? record.kind : "opening";
  const width = Math.max(MIN_OPENING_WIDTH_M, asFiniteNumber(record.width_m, defaultWidthForKind(kind)));
  const colorRaw = asNonEmptyString(record.color);
  const textureRaw = asNonEmptyString(record.texture);

  return {
    id: asNonEmptyString(record.id) ?? newWallOpeningId(),
    kind,
    center_m: asFiniteNumber(record.center_m, 0),
    width_m: width,
    y_min_m: asOptionalFiniteNumber(record.y_min_m),
    y_max_m: asOptionalFiniteNumber(record.y_max_m),
    color: colorRaw && HEX_COLOR_RE.test(colorRaw) ? colorRaw : undefined,
    texture: textureRaw,
    external_ref: asNonEmptyString(record.external_ref),
  };
}

export function readWallOpenings(value: unknown): WallOpening[] {
  if (!Array.isArray(value)) return [];
  const out: WallOpening[] = [];
  const usedIds = new Set<string>();
  for (const item of value) {
    const normalized = normalizeOpening(item);
    if (!normalized) continue;
    while (usedIds.has(normalized.id)) normalized.id = newWallOpeningId();
    usedIds.add(normalized.id);
    out.push(normalized);
  }
  return out;
}

export function defaultVerticalBand(kind: WallOpeningKind, wallHeight: number): { yMin: number; yMax: number } {
  const safeHeight = Math.max(0.15, wallHeight);
  if (kind === "opening") return { yMin: 0, yMax: safeHeight };
  if (kind === "door") return { yMin: 0, yMax: Math.min(safeHeight, 2.1) };

  const defaultSill = Math.min(0.9, Math.max(0, safeHeight - 0.3));
  const defaultHead = Math.min(safeHeight, Math.max(defaultSill + 0.25, 2.0));
  return { yMin: defaultSill, yMax: defaultHead };
}

export function createDefaultOpening(args: {
  kind: WallOpeningKind;
  center_m: number;
  width_m: number;
}): WallOpening {
  const width = Math.max(MIN_OPENING_WIDTH_M, args.width_m);
  return {
    id: newWallOpeningId(),
    kind: args.kind,
    center_m: args.center_m,
    width_m: width,
    color: defaultColorForKind(args.kind),
    texture: defaultTextureForKind(args.kind),
  };
}

export function resolveWallOpenings(openings: WallOpening[], wallLength: number, wallHeight: number): ResolvedWallOpening[] {
  const length = Math.max(0.001, wallLength);
  const height = Math.max(0.15, wallHeight);
  const out: ResolvedWallOpening[] = [];
  for (const opening of openings) {
    const width = clamp(
      Number.isFinite(opening.width_m) ? opening.width_m : defaultWidthForKind(opening.kind),
      MIN_OPENING_WIDTH_M,
      length,
    );
    const center = clamp(
      Number.isFinite(opening.center_m) ? opening.center_m : length / 2,
      width / 2,
      length - width / 2,
    );
    const start = center - width / 2;
    const end = center + width / 2;
    if (end - start < MIN_OPENING_WIDTH_M) continue;

    const defaults = defaultVerticalBand(opening.kind, height);
    let yMin = Number.isFinite(opening.y_min_m ?? NaN) ? (opening.y_min_m as number) : defaults.yMin;
    let yMax = Number.isFinite(opening.y_max_m ?? NaN) ? (opening.y_max_m as number) : defaults.yMax;
    yMin = clamp(yMin, 0, height);
    yMax = clamp(yMax, 0, height);
    if (yMax < yMin) {
      const t = yMax;
      yMax = yMin;
      yMin = t;
    }
    if (yMax - yMin < MIN_OPENING_HEIGHT_M) continue;

    out.push({
      id: opening.id,
      kind: opening.kind,
      center_m: center,
      width_m: width,
      start_m: start,
      end_m: end,
      y_min_m: yMin,
      y_max_m: yMax,
      color: opening.color,
      texture: opening.texture,
      external_ref: opening.external_ref,
    });
  }
  out.sort((a, b) => a.start_m - b.start_m || a.id.localeCompare(b.id));
  return out;
}

export function openingsToProps(openings: WallOpening[]): Array<Record<string, unknown>> {
  return openings.map((opening) => {
    const out: Record<string, unknown> = {
      id: opening.id,
      kind: opening.kind,
      center_m: opening.center_m,
      width_m: opening.width_m,
    };
    if (typeof opening.y_min_m === "number" && Number.isFinite(opening.y_min_m)) out.y_min_m = opening.y_min_m;
    if (typeof opening.y_max_m === "number" && Number.isFinite(opening.y_max_m)) out.y_max_m = opening.y_max_m;
    if (opening.color) out.color = opening.color;
    if (opening.texture) out.texture = opening.texture;
    if (opening.external_ref) out.external_ref = opening.external_ref;
    return out;
  });
}
