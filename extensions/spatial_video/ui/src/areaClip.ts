import type { CompositionElement, ElementType } from "@toposync/plugin-api";

import type { AreaClip, CameraControlPointSet, WorldPoint } from "./types";

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function readAreaClipElementId(element: CompositionElement): string {
  return readString(readRecord(element.props?.spatial_video).clip_area_element_id);
}

export function readAreaPolygon(element: CompositionElement): WorldPoint[] {
  const raw = Array.isArray(element.props?.vertices) ? element.props.vertices : [];
  const out: WorldPoint[] = [];
  for (const item of raw) {
    const point = readRecord(item);
    if (!finiteNumber(point.x) || !finiteNumber(point.z)) continue;
    out.push({ x: Number(point.x), z: Number(point.z) });
  }
  return Math.abs(polygonSignedArea(out)) > 1e-8 ? out : [];
}

export function areaClipSignature(clip: AreaClip | null | undefined): string {
  if (!clip) return "";
  return `${clip.areaElementId}:${clip.signature}`;
}

export function polygonSignature(polygon: WorldPoint[]): string {
  return polygon.map((point) => `${point.x.toFixed(4)},${point.z.toFixed(4)}`).join(";");
}

export function polygonSignedArea(points: WorldPoint[]): number {
  if (points.length < 3) return 0;
  let total = 0;
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index];
    const next = points[(index + 1) % points.length];
    total += current.x * next.z - next.x * current.z;
  }
  return total / 2;
}

export function pointInPolygon(point: WorldPoint, polygon: WorldPoint[]): boolean {
  if (polygon.length < 3) return false;
  let inside = false;
  for (let index = 0, previousIndex = polygon.length - 1; index < polygon.length; previousIndex = index, index += 1) {
    const current = polygon[index];
    const previous = polygon[previousIndex];
    const crosses = current.z > point.z !== previous.z > point.z;
    if (!crosses) continue;
    const xAtZ = ((previous.x - current.x) * (point.z - current.z)) / ((previous.z - current.z) || 1e-12) + current.x;
    if (point.x < xAtZ) inside = !inside;
  }
  return inside;
}

function orientation(a: WorldPoint, b: WorldPoint, c: WorldPoint): number {
  return (b.x - a.x) * (c.z - a.z) - (b.z - a.z) * (c.x - a.x);
}

function pointOnSegment(a: WorldPoint, b: WorldPoint, p: WorldPoint): boolean {
  return (
    Math.abs(orientation(a, b, p)) < 1e-9 &&
    p.x >= Math.min(a.x, b.x) - 1e-9 &&
    p.x <= Math.max(a.x, b.x) + 1e-9 &&
    p.z >= Math.min(a.z, b.z) - 1e-9 &&
    p.z <= Math.max(a.z, b.z) + 1e-9
  );
}

function segmentsIntersect(a: WorldPoint, b: WorldPoint, c: WorldPoint, d: WorldPoint): boolean {
  const o1 = orientation(a, b, c);
  const o2 = orientation(a, b, d);
  const o3 = orientation(c, d, a);
  const o4 = orientation(c, d, b);
  if (Math.abs(o1) < 1e-9 && pointOnSegment(a, b, c)) return true;
  if (Math.abs(o2) < 1e-9 && pointOnSegment(a, b, d)) return true;
  if (Math.abs(o3) < 1e-9 && pointOnSegment(c, d, a)) return true;
  if (Math.abs(o4) < 1e-9 && pointOnSegment(c, d, b)) return true;
  return o1 > 0 !== o2 > 0 && o3 > 0 !== o4 > 0;
}

export function polygonsIntersect(a: WorldPoint[], b: WorldPoint[]): boolean {
  if (a.length < 3 || b.length < 3) return false;
  if (a.some((point) => pointInPolygon(point, b))) return true;
  if (b.some((point) => pointInPolygon(point, a))) return true;
  for (let ai = 0; ai < a.length; ai += 1) {
    const a0 = a[ai];
    const a1 = a[(ai + 1) % a.length];
    for (let bi = 0; bi < b.length; bi += 1) {
      if (segmentsIntersect(a0, a1, b[bi], b[(bi + 1) % b.length])) return true;
    }
  }
  return false;
}

export function controlPointSetFootprint(set: CameraControlPointSet): WorldPoint[] {
  const out: WorldPoint[] = [];
  for (const point of (set.control_points ?? []).slice(0, 4)) {
    const world = point.world;
    if (!world || !finiteNumber(world.x) || !finiteNumber(world.z)) continue;
    out.push({ x: world.x, z: world.z });
  }
  return Math.abs(polygonSignedArea(out)) > 1e-8 ? out : [];
}

export function controlPointSetIntersectsAreaClip(set: CameraControlPointSet, clip: AreaClip | null | undefined): boolean {
  if (!clip) return true;
  const footprint = controlPointSetFootprint(set);
  return footprint.length >= 3 && polygonsIntersect(footprint, clip.polygon);
}

export function resolveAreaClipForElement(
  element: CompositionElement,
  elements: CompositionElement[],
  elementTypesById: Record<string, ElementType>,
  sets: CameraControlPointSet[],
): { clip: AreaClip | null; warning: string | null } {
  const selectedAreaId = readAreaClipElementId(element);
  if (!selectedAreaId) return { clip: null, warning: null };
  const areaElement = elements.find((item) => item.id === selectedAreaId) ?? null;
  if (!areaElement) return { clip: null, warning: "Área de recorte não encontrada." };
  if ((elementTypesById[areaElement.type]?.layerGroup ?? "") !== "areas") {
    return { clip: null, warning: "O elemento selecionado para recorte não é uma área." };
  }
  const polygon = readAreaPolygon(areaElement);
  if (polygon.length < 3) return { clip: null, warning: "A área de recorte não tem polígono válido." };
  const clip: AreaClip = {
    areaElementId: areaElement.id,
    label: areaElement.name || areaElement.id,
    polygon,
    signature: polygonSignature(polygon),
  };
  if (!sets.some((set) => controlPointSetIntersectsAreaClip(set, clip))) {
    return { clip, warning: "A área de recorte não cruza nenhuma vista calibrada." };
  }
  return { clip, warning: null };
}
