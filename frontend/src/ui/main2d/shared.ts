import type { BoundsXZ, CompositionElement, ElementType, Main2DMarker, Vector3 } from "@toposync/plugin-api";

export type BoundsAccumulator = BoundsXZ & { empty: boolean };
export type ViewTransform = { scale: number; x: number; y: number };

export type Main2DMarkerStage = Main2DMarker & {
  id: string;
  elementId: string;
  stageX: number;
  stageY: number;
};

export type Main2DMarkerEntry =
  | (Main2DMarkerStage & {
      kind: "single";
      screenX: number;
      screenY: number;
    })
  | {
      kind: "cluster";
      id: string;
      markers: Array<Main2DMarkerStage & { screenX: number; screenY: number }>;
      screenX: number;
      screenY: number;
      title: string;
    };

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return {};
}

export function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function readString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function readVector3(value: unknown, fallback: Vector3): Vector3 {
  const rec = asRecord(value);
  return {
    x: readNumber(rec.x, fallback.x),
    y: readNumber(rec.y, fallback.y),
    z: readNumber(rec.z, fallback.z),
  };
}

export function stableStringify(value: unknown): string {
  const seen = new Set<unknown>();
  function inner(v: unknown): any {
    if (v === null) return null;
    const t = typeof v;
    if (t === "string" || t === "number" || t === "boolean") return v;
    if (t !== "object") return null;
    if (seen.has(v)) return null;
    seen.add(v);

    if (Array.isArray(v)) return v.map(inner);

    const rec = v as Record<string, unknown>;
    const keys = Object.keys(rec).sort((a, b) => a.localeCompare(b));
    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = inner(rec[key]);
    return out;
  }
  return JSON.stringify(inner(value));
}

function fallbackHashHex(text: string): string {
  let h1 = 0xdeadbeef ^ text.length;
  let h2 = 0x41c6ce57 ^ text.length;
  let h3 = 0x9e3779b9 ^ text.length;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
    h3 = Math.imul(h3 ^ ch, 2246822507);
  }

  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h3 ^ (h3 >>> 13), 3266489909);
  h3 = Math.imul(h3 ^ (h3 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);

  return [h1, h2, h3].map((part) => (part >>> 0).toString(16).padStart(8, "0")).join("");
}

export async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (subtle && typeof subtle.digest === "function") {
    try {
      const data = new TextEncoder().encode(text);
      const hash = await subtle.digest("SHA-256", data);
      return Array.from(new Uint8Array(hash))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
    } catch {
      // Fall through.
    }
  }
  return fallbackHashHex(text);
}

export function createBoundsAccumulator(): BoundsAccumulator {
  return { minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity, empty: true };
}

export function includeBoundsPoint(bounds: BoundsAccumulator, point: { x: number; z: number }): void {
  if (!Number.isFinite(point.x) || !Number.isFinite(point.z)) return;
  bounds.minX = Math.min(bounds.minX, point.x);
  bounds.maxX = Math.max(bounds.maxX, point.x);
  bounds.minZ = Math.min(bounds.minZ, point.z);
  bounds.maxZ = Math.max(bounds.maxZ, point.z);
  bounds.empty = false;
}

export function includeBounds(bounds: BoundsAccumulator, input: BoundsXZ | null | undefined): void {
  if (!input) return;
  includeBoundsPoint(bounds, { x: input.minX, z: input.minZ });
  includeBoundsPoint(bounds, { x: input.maxX, z: input.maxZ });
}

export function includeBoundsExpandedPoint(bounds: BoundsAccumulator, point: { x: number; z: number }, expand: number): void {
  includeBoundsPoint(bounds, { x: point.x - expand, z: point.z - expand });
  includeBoundsPoint(bounds, { x: point.x + expand, z: point.z + expand });
}

export function includeBoundsRotatedRect(
  bounds: BoundsAccumulator,
  center: { x: number; z: number },
  size: { x: number; z: number },
  rotationY: number,
): void {
  const halfX = Math.abs(size.x) / 2;
  const halfZ = Math.abs(size.z) / 2;
  if (!Number.isFinite(halfX) || !Number.isFinite(halfZ) || (halfX < 1e-9 && halfZ < 1e-9)) return;

  const cos = Math.cos(rotationY);
  const sin = Math.sin(rotationY);
  const corners = [
    { x: -halfX, z: -halfZ },
    { x: halfX, z: -halfZ },
    { x: halfX, z: halfZ },
    { x: -halfX, z: halfZ },
  ];

  for (const corner of corners) {
    const rx = corner.x * cos - corner.z * sin;
    const rz = corner.x * sin + corner.z * cos;
    includeBoundsPoint(bounds, { x: center.x + rx, z: center.z + rz });
  }
}

export function normalizeBounds(bounds: BoundsAccumulator): BoundsXZ {
  if (bounds.empty) return { minX: -1, maxX: 1, minZ: -1, maxZ: 1 };
  const minSpan = 0.25;
  const spanX = bounds.maxX - bounds.minX;
  const spanZ = bounds.maxZ - bounds.minZ;
  if (spanX < minSpan) {
    const cx = (bounds.minX + bounds.maxX) / 2;
    bounds.minX = cx - minSpan / 2;
    bounds.maxX = cx + minSpan / 2;
  }
  if (spanZ < minSpan) {
    const cz = (bounds.minZ + bounds.maxZ) / 2;
    bounds.minZ = cz - minSpan / 2;
    bounds.maxZ = cz + minSpan / 2;
  }
  return { minX: bounds.minX, maxX: bounds.maxX, minZ: bounds.minZ, maxZ: bounds.maxZ };
}

export function padBounds(bounds: BoundsXZ, ratio: number, minimum = 0.5): BoundsXZ {
  const spanX = Math.max(1e-6, bounds.maxX - bounds.minX);
  const spanZ = Math.max(1e-6, bounds.maxZ - bounds.minZ);
  const padX = Math.max(minimum, spanX * ratio);
  const padZ = Math.max(minimum, spanZ * ratio);
  return {
    minX: bounds.minX - padX,
    maxX: bounds.maxX + padX,
    minZ: bounds.minZ - padZ,
    maxZ: bounds.maxZ + padZ,
  };
}

export function computeMain2DBounds(
  elements: CompositionElement[],
  elementTypesById: Record<string, ElementType>,
  extraPoints: Array<{ x: number; z: number }> = [],
): BoundsXZ {
  const acc = createBoundsAccumulator();
  for (const element of elements) {
    const def = elementTypesById[element.type];
    if (def?.getMain2DBounds) {
      try {
        includeBounds(acc, def.getMain2DBounds(element));
        continue;
      } catch (err) {
        console.warn(`[main2d:getMain2DBounds:${element.type}]`, err);
      }
    }
    includeBoundsExpandedPoint(acc, { x: element.position.x, z: element.position.z }, 0.35);
  }
  for (const point of extraPoints) includeBoundsExpandedPoint(acc, point, 0.35);
  return normalizeBounds(acc);
}

export function main2DElementRank(el: CompositionElement, elementTypesById: Record<string, ElementType>): number {
  const group = elementTypesById[el.type]?.layerGroup ?? "";
  if (group === "background") {
    const mode = asRecord(el.props).mode;
    if (mode === "tracing") return 0.5;
    return -1;
  }
  if (group === "areas") return 0;
  if (group === "walls") return 1;
  if (group === "measurements") return 3;
  return 2;
}

export function orderElementsForMain2D(
  elements: CompositionElement[],
  elementTypesById: Record<string, ElementType>,
): CompositionElement[] {
  return elements
    .map((el, idx) => ({ el, idx }))
    .sort((a, b) => main2DElementRank(a.el, elementTypesById) - main2DElementRank(b.el, elementTypesById) || a.idx - b.idx)
    .map((entry) => entry.el);
}

export function computeFitTransform(containerWidth: number, containerHeight: number, stageWidth: number, stageHeight: number): ViewTransform {
  const scale = Math.min(containerWidth / Math.max(1, stageWidth), containerHeight / Math.max(1, stageHeight)) * 0.96;
  return {
    scale,
    x: (containerWidth - stageWidth * scale) / 2,
    y: (containerHeight - stageHeight * scale) / 2,
  };
}

export function projectWorldToStage(point: { x: number; z: number }, bounds: BoundsXZ, stageWidth: number, stageHeight: number): { x: number; y: number } {
  const spanX = Math.max(1e-6, bounds.maxX - bounds.minX);
  const spanZ = Math.max(1e-6, bounds.maxZ - bounds.minZ);
  return {
    x: ((point.x - bounds.minX) / spanX) * stageWidth,
    y: ((point.z - bounds.minZ) / spanZ) * stageHeight,
  };
}

export function clusterMain2DMarkers(args: {
  markers: Main2DMarkerStage[];
  transform: ViewTransform;
  thresholdPx: number;
  clusterTitle: (count: number) => string;
}): Main2DMarkerEntry[] {
  const markersWithScreen = args.markers.map((marker) => ({
    ...marker,
    screenX: args.transform.x + marker.stageX * args.transform.scale,
    screenY: args.transform.y + marker.stageY * args.transform.scale,
  }));

  if (markersWithScreen.length === 0) return [];

  const parent = Array.from({ length: markersWithScreen.length }, (_, i) => i);
  const find = (x: number): number => {
    let cur = x;
    while (parent[cur] !== cur) {
      parent[cur] = parent[parent[cur]];
      cur = parent[cur];
    }
    return cur;
  };
  const union = (a: number, b: number): void => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent[rb] = ra;
  };

  for (let i = 0; i < markersWithScreen.length; i += 1) {
    for (let j = i + 1; j < markersWithScreen.length; j += 1) {
      const dx = Math.abs(markersWithScreen[i].screenX - markersWithScreen[j].screenX);
      const dy = Math.abs(markersWithScreen[i].screenY - markersWithScreen[j].screenY);
      if (dx < args.thresholdPx && dy < args.thresholdPx) union(i, j);
    }
  }

  const groups = new Map<number, number[]>();
  for (let i = 0; i < markersWithScreen.length; i += 1) {
    const root = find(i);
    const list = groups.get(root) ?? [];
    list.push(i);
    groups.set(root, list);
  }

  const out: Main2DMarkerEntry[] = [];
  for (const indices of groups.values()) {
    if (indices.length === 1) {
      out.push({ kind: "single", ...markersWithScreen[indices[0]] });
      continue;
    }

    const markers = indices.map((idx) => markersWithScreen[idx]);
    markers.sort((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
    out.push({
      kind: "cluster",
      id: markers.map((marker) => marker.id).join("|"),
      markers,
      screenX: markers.reduce((sum, marker) => sum + marker.screenX, 0) / markers.length,
      screenY: markers.reduce((sum, marker) => sum + marker.screenY, 0) / markers.length,
      title: args.clusterTitle(markers.length),
    });
  }

  out.sort((a, b) => {
    const dy = a.screenY - b.screenY;
    if (Math.abs(dy) > 0.1) return dy;
    return a.screenX - b.screenX;
  });
  return out;
}

export function buildMain2DSignatureElements(elements: CompositionElement[]): unknown[] {
  return elements
    .map((el) => ({
      id: el.id,
      type: el.type,
      name: el.name,
      position: el.position,
      rotation: el.rotation,
      props: el.props,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
}
