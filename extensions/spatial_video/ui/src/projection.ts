import type { CameraControlPointSet, CameraProjectionBoundaryEdge, CameraProjectionBoundaryPoint, MediaContentRect, ProjectionMeshData, Vector2, WorldPoint } from "./types";

export type ProjectionStrategyId = "homography_grid" | "constrained_trapezoid";
export type ProjectionMeshDensity = 34 | 64 | 96;

export type ProjectionBuildOptions = {
  gridDivisions?: ProjectionMeshDensity;
  clipPolygon?: WorldPoint[] | null;
  uvRect?: MediaContentRect | null;
};

export type ProjectionStrategy = {
  id: ProjectionStrategyId;
  buildMesh: (set: CameraControlPointSet, options?: ProjectionBuildOptions) => ProjectionMeshData | null;
};

type Pair = { image: Vector2; world: WorldPoint };
type HomographyEstimate = { hImageToWorld: number[]; inlierPairs: Pair[] };
type ProjectionClipVertex = { x: number; y: number; z: number; u: number; v: number };

const DEFAULT_GRID_DIVISIONS: ProjectionMeshDensity = 34;
const PROJECTION_Y_OFFSET = 0.026;
const HOMOGRAPHY_REPROJECTION_THRESHOLD_UV = 0.02;
const MIN_TRAPEZOID_EDGE_LENGTH = 1e-4;
const MAX_TRAPEZOID_WIDTH_RATIO = 2.5;
const MAX_TRAPEZOID_CONTROL_SPAN_RATIO = 2.8;
const TRAPEZOID_IMAGE_PADDING = 0.04;
const LOCAL_REFINEMENT_SIGMA_UV = 0.22;
const LOCAL_REFINEMENT_EDGE_LOW = 0.015;
const LOCAL_REFINEMENT_EDGE_HIGH = 0.12;
const BOUNDARY_REFINEMENT_FALLOFF_UV = 0.48;
const MIN_MEDIA_CONTENT_RECT_SIZE = 1e-5;
const FULL_MEDIA_CONTENT_RECT: MediaContentRect = { x: 0, y: 0, width: 1, height: 1 };
const BOUNDARY_EDGES: CameraProjectionBoundaryEdge[] = ["top", "right", "bottom", "left"];

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function sanitizeMediaContentRect(rect: MediaContentRect | null | undefined): MediaContentRect {
  if (
    !rect ||
    !finiteNumber(rect.x) ||
    !finiteNumber(rect.y) ||
    !finiteNumber(rect.width) ||
    !finiteNumber(rect.height) ||
    rect.width < MIN_MEDIA_CONTENT_RECT_SIZE ||
    rect.height < MIN_MEDIA_CONTENT_RECT_SIZE
  ) {
    return FULL_MEDIA_CONTENT_RECT;
  }
  const x = clamp(rect.x, 0, 1);
  const y = clamp(rect.y, 0, 1);
  const maxWidth = Math.max(MIN_MEDIA_CONTENT_RECT_SIZE, 1 - x);
  const maxHeight = Math.max(MIN_MEDIA_CONTENT_RECT_SIZE, 1 - y);
  return {
    x,
    y,
    width: clamp(rect.width, MIN_MEDIA_CONTENT_RECT_SIZE, maxWidth),
    height: clamp(rect.height, MIN_MEDIA_CONTENT_RECT_SIZE, maxHeight),
  };
}

export function mediaContentRectSignature(rect: MediaContentRect | null | undefined): string {
  const normalized = sanitizeMediaContentRect(rect);
  return `${normalized.x.toFixed(6)},${normalized.y.toFixed(6)},${normalized.width.toFixed(6)},${normalized.height.toFixed(6)}`;
}

function validPairs(set: CameraControlPointSet): Pair[] {
  const out: Pair[] = [];
  for (const point of set.control_points ?? []) {
    const image = point.image;
    const world = point.world;
    if (!image || !world) continue;
    if (!finiteNumber(image.x) || !finiteNumber(image.y) || !finiteNumber(world.x) || !finiteNumber(world.z)) continue;
    out.push({
      image: { x: image.x, y: image.y },
      world: { x: world.x, z: world.z },
    });
  }
  return out;
}

function validBasePairs(set: CameraControlPointSet): Pair[] {
  return validPairs(set).slice(0, 4);
}

function validRefinementPairs(set: CameraControlPointSet): Pair[] {
  const out: Pair[] = [];
  for (const point of set.refinement_points ?? []) {
    const image = point.image;
    const world = point.world;
    if (!image || !world) continue;
    if (!finiteNumber(image.x) || !finiteNumber(image.y) || !finiteNumber(world.x) || !finiteNumber(world.z)) continue;
    if (image.x < 0 || image.x > 1 || image.y < 0 || image.y > 1) continue;
    out.push({ image: { x: image.x, y: image.y }, world: { x: world.x, z: world.z } });
  }
  return out;
}

function boundaryImageForEdge(edge: CameraProjectionBoundaryEdge, t: number): Vector2 {
  const normalizedT = clamp(t, 0, 1);
  switch (edge) {
    case "top":
      return { x: normalizedT, y: 0 };
    case "right":
      return { x: 1, y: normalizedT };
    case "bottom":
      return { x: 1 - normalizedT, y: 1 };
    case "left":
      return { x: 0, y: 1 - normalizedT };
  }
}

function validBoundaryPoints(set: CameraControlPointSet): CameraProjectionBoundaryPoint[] {
  const out: CameraProjectionBoundaryPoint[] = [];
  const perEdge = new Map<CameraProjectionBoundaryEdge, number>();
  for (const point of set.boundary_refinement_points ?? []) {
    if (!BOUNDARY_EDGES.includes(point.edge)) continue;
    if (!finiteNumber(point.t) || point.t < 0 || point.t > 1) continue;
    const world = point.world;
    if (!world || !finiteNumber(world.x) || !finiteNumber(world.z)) continue;
    const edgeCount = perEdge.get(point.edge) ?? 0;
    if (edgeCount >= 8 || out.length >= 32) continue;
    perEdge.set(point.edge, edgeCount + 1);
    out.push({
      id: String(point.id || `boundary-${out.length + 1}`),
      edge: point.edge,
      t: point.t,
      image: boundaryImageForEdge(point.edge, point.t),
      world: { x: world.x, z: world.z },
    });
  }
  return out;
}

export function controlPointSetProjectionSignature(set: CameraControlPointSet): string {
  const base = validBasePairs(set)
    .map((pair) => `${pair.image.x.toFixed(6)},${pair.image.y.toFixed(6)}>${pair.world.x.toFixed(6)},${pair.world.z.toFixed(6)}`)
    .join(";");
  const refinement = validRefinementPairs(set)
    .map((pair) => `${pair.image.x.toFixed(6)},${pair.image.y.toFixed(6)}>${pair.world.x.toFixed(6)},${pair.world.z.toFixed(6)}`)
    .join(";");
  const boundary = validBoundaryPoints(set)
    .map((point) => `${point.edge},${point.t.toFixed(6)}>${point.world.x.toFixed(6)},${point.world.z.toFixed(6)}`)
    .join(";");
  return `${base}|${refinement}|${boundary}`;
}

function convexHull(points: Vector2[]): Vector2[] {
  const sorted = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  if (sorted.length <= 3) return sorted;
  const cross = (o: Vector2, a: Vector2, b: Vector2) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lower: Vector2[] = [];
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  const upper: Vector2[] = [];
  for (let i = sorted.length - 1; i >= 0; i -= 1) {
    const p = sorted[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  lower.pop();
  upper.pop();
  return [...lower, ...upper];
}

function pointInPolygon(point: Vector2, polygon: Vector2[]): boolean {
  if (polygon.length < 3) return false;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const pi = polygon[i];
    const pj = polygon[j];
    const intersects = pi.y > point.y !== pj.y > point.y && point.x < ((pj.x - pi.x) * (point.y - pi.y)) / ((pj.y - pi.y) || 1e-12) + pi.x;
    if (intersects) inside = !inside;
  }
  return inside;
}

function worldPolygonSignedArea(points: WorldPoint[]): number {
  if (points.length < 3) return 0;
  let total = 0;
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index];
    const next = points[(index + 1) % points.length];
    total += current.x * next.z - next.x * current.z;
  }
  return total / 2;
}

function sanitizeClipPolygon(polygon: WorldPoint[] | null | undefined): WorldPoint[] | null {
  if (!polygon || polygon.length < 3) return null;
  const out: WorldPoint[] = [];
  for (const point of polygon) {
    if (!finiteNumber(point.x) || !finiteNumber(point.z)) continue;
    const previous = out[out.length - 1];
    if (previous && Math.hypot(previous.x - point.x, previous.z - point.z) < 1e-8) continue;
    out.push({ x: point.x, z: point.z });
  }
  if (out.length >= 2 && Math.hypot(out[0].x - out[out.length - 1].x, out[0].z - out[out.length - 1].z) < 1e-8) out.pop();
  if (Math.abs(worldPolygonSignedArea(out)) < 1e-8) return null;
  return worldPolygonSignedArea(out) >= 0 ? out : [...out].reverse();
}

function crossWorld(a: WorldPoint, b: WorldPoint, c: WorldPoint): number {
  return (b.x - a.x) * (c.z - a.z) - (b.z - a.z) * (c.x - a.x);
}

function clipVertexInsideEdge(vertex: ProjectionClipVertex, edgeStart: WorldPoint, edgeEnd: WorldPoint): boolean {
  return crossWorld(edgeStart, edgeEnd, vertex) >= -1e-8;
}

function interpolateClipVertex(start: ProjectionClipVertex, end: ProjectionClipVertex, edgeStart: WorldPoint, edgeEnd: WorldPoint): ProjectionClipVertex | null {
  const segment = { x: end.x - start.x, z: end.z - start.z };
  const edge = { x: edgeEnd.x - edgeStart.x, z: edgeEnd.z - edgeStart.z };
  const denom = segment.x * edge.z - segment.z * edge.x;
  if (Math.abs(denom) < 1e-10) return null;
  const relative = { x: edgeStart.x - start.x, z: edgeStart.z - start.z };
  const t = (relative.x * edge.z - relative.z * edge.x) / denom;
  const clamped = clamp(t, 0, 1);
  return {
    x: start.x + (end.x - start.x) * clamped,
    y: start.y + (end.y - start.y) * clamped,
    z: start.z + (end.z - start.z) * clamped,
    u: start.u + (end.u - start.u) * clamped,
    v: start.v + (end.v - start.v) * clamped,
  };
}

function clipPolygonAgainstEdge(vertices: ProjectionClipVertex[], edgeStart: WorldPoint, edgeEnd: WorldPoint): ProjectionClipVertex[] {
  if (vertices.length === 0) return [];
  const out: ProjectionClipVertex[] = [];
  let previous = vertices[vertices.length - 1];
  let previousInside = clipVertexInsideEdge(previous, edgeStart, edgeEnd);
  for (const current of vertices) {
    const currentInside = clipVertexInsideEdge(current, edgeStart, edgeEnd);
    if (currentInside !== previousInside) {
      const intersection = interpolateClipVertex(previous, current, edgeStart, edgeEnd);
      if (intersection) out.push(intersection);
    }
    if (currentInside) out.push(current);
    previous = current;
    previousInside = currentInside;
  }
  return out;
}

function clipTriangleToPolygon(vertices: ProjectionClipVertex[], clipPolygon: WorldPoint[]): ProjectionClipVertex[] {
  let out = vertices;
  for (let index = 0; index < clipPolygon.length && out.length >= 3; index += 1) {
    out = clipPolygonAgainstEdge(out, clipPolygon[index], clipPolygon[(index + 1) % clipPolygon.length]);
  }
  return out;
}

function applyClipToProjectionMesh(meshData: ProjectionMeshData, polygon: WorldPoint[] | null | undefined): ProjectionMeshData | null {
  const clipPolygon = sanitizeClipPolygon(polygon);
  if (!clipPolygon) return meshData;
  const positions: number[] = [];
  const uvs: number[] = [];
  const indices: number[] = [];

  const pushVertex = (vertex: ProjectionClipVertex): number => {
    const index = positions.length / 3;
    positions.push(vertex.x, vertex.y, vertex.z);
    uvs.push(vertex.u, vertex.v);
    return index;
  };

  const sourceIndices = meshData.indices;
  for (let offset = 0; offset < sourceIndices.length; offset += 3) {
    const triangle = [0, 1, 2].map((localIndex) => {
      const sourceIndex = sourceIndices[offset + localIndex];
      return {
        x: meshData.positions[sourceIndex * 3],
        y: meshData.positions[sourceIndex * 3 + 1],
        z: meshData.positions[sourceIndex * 3 + 2],
        u: meshData.uvs[sourceIndex * 2],
        v: meshData.uvs[sourceIndex * 2 + 1],
      };
    });
    const clipped = clipTriangleToPolygon(triangle, clipPolygon);
    if (clipped.length < 3) continue;
    const first = pushVertex(clipped[0]);
    for (let index = 1; index < clipped.length - 1; index += 1) {
      const second = pushVertex(clipped[index]);
      const third = pushVertex(clipped[index + 1]);
      indices.push(first, second, third);
    }
  }

  if (indices.length < 3) return null;
  return {
    positions: new Float32Array(positions),
    uvs: new Float32Array(uvs),
    indices: new Uint32Array(indices),
  };
}

function applyUvRectToProjectionMesh(meshData: ProjectionMeshData, rect: MediaContentRect | null | undefined): ProjectionMeshData {
  const normalized = sanitizeMediaContentRect(rect);
  if (normalized.x === 0 && normalized.y === 0 && normalized.width === 1 && normalized.height === 1) return meshData;
  const uvs = new Float32Array(meshData.uvs.length);
  for (let index = 0; index < meshData.uvs.length; index += 2) {
    uvs[index] = normalized.x + meshData.uvs[index] * normalized.width;
    uvs[index + 1] = normalized.y + meshData.uvs[index + 1] * normalized.height;
  }
  return {
    positions: meshData.positions,
    uvs,
    indices: meshData.indices,
  };
}

function finalizeProjectionMesh(meshData: ProjectionMeshData, options: ProjectionBuildOptions | undefined): ProjectionMeshData | null {
  const uvMapped = applyUvRectToProjectionMesh(meshData, options?.uvRect);
  return applyClipToProjectionMesh(uvMapped, options?.clipPolygon);
}

function meshHasFoldover(meshData: ProjectionMeshData): boolean {
  let sign = 0;
  for (let index = 0; index < meshData.indices.length; index += 3) {
    const aIndex = meshData.indices[index] * 3;
    const bIndex = meshData.indices[index + 1] * 3;
    const cIndex = meshData.indices[index + 2] * 3;
    const ax = meshData.positions[aIndex];
    const az = meshData.positions[aIndex + 2];
    const bx = meshData.positions[bIndex];
    const bz = meshData.positions[bIndex + 2];
    const cx = meshData.positions[cIndex];
    const cz = meshData.positions[cIndex + 2];
    const area = ((bx - ax) * (cz - az) - (bz - az) * (cx - ax)) / 2;
    if (!Number.isFinite(area) || Math.abs(area) <= 1e-9) return true;
    const currentSign = Math.sign(area);
    if (sign === 0) sign = currentSign;
    else if (currentSign !== sign) return true;
  }
  return false;
}

function solveLinearSystem(matrix: number[][], rhs: number[]): number[] | null {
  const n = rhs.length;
  const a = matrix.map((row, i) => [...row, rhs[i]]);
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < n; row += 1) {
      if (Math.abs(a[row][col]) > Math.abs(a[pivot][col])) pivot = row;
    }
    if (Math.abs(a[pivot][col]) < 1e-10) return null;
    if (pivot !== col) {
      const tmp = a[col];
      a[col] = a[pivot];
      a[pivot] = tmp;
    }
    const div = a[col][col];
    for (let j = col; j <= n; j += 1) a[col][j] /= div;
    for (let row = 0; row < n; row += 1) {
      if (row === col) continue;
      const factor = a[row][col];
      if (Math.abs(factor) < 1e-12) continue;
      for (let j = col; j <= n; j += 1) a[row][j] -= factor * a[col][j];
    }
  }
  return a.map((row) => row[n]);
}

function solveHomography(src: Vector2[], dst: Vector2[]): number[] | null {
  if (src.length < 4 || dst.length < 4 || src.length !== dst.length) return null;
  const ata = Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 0));
  const atb = Array.from({ length: 8 }, () => 0);

  const addRow = (row: number[], target: number) => {
    for (let i = 0; i < 8; i += 1) {
      atb[i] += row[i] * target;
      for (let j = 0; j < 8; j += 1) ata[i][j] += row[i] * row[j];
    }
  };

  for (let i = 0; i < src.length; i += 1) {
    const a = src[i].x;
    const b = src[i].y;
    const x = dst[i].x;
    const y = dst[i].y;
    addRow([a, b, 1, 0, 0, 0, -x * a, -x * b], x);
    addRow([0, 0, 0, a, b, 1, -y * a, -y * b], y);
  }

  const solved = solveLinearSystem(ata, atb);
  return solved ? [...solved, 1] : null;
}

function mapHomography(h: number[], p: Vector2): Vector2 | null {
  const denom = h[6] * p.x + h[7] * p.y + h[8];
  if (!Number.isFinite(denom) || Math.abs(denom) < 1e-8) return null;
  const x = (h[0] * p.x + h[1] * p.y + h[2]) / denom;
  const y = (h[3] * p.x + h[4] * p.y + h[5]) / denom;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y };
}

function invertHomography(h: number[]): number[] | null {
  const a = h[0];
  const b = h[1];
  const c = h[2];
  const d = h[3];
  const e = h[4];
  const f = h[5];
  const g = h[6];
  const i = h[7];
  const j = h[8];
  const A = e * j - f * i;
  const B = c * i - b * j;
  const C = b * f - c * e;
  const D = f * g - d * j;
  const E = a * j - c * g;
  const F = c * d - a * f;
  const G = d * i - e * g;
  const I = b * g - a * i;
  const J = a * e - b * d;
  const det = a * A + b * D + c * G;
  if (!Number.isFinite(det) || Math.abs(det) < 1e-12) return null;
  const inv = [A / det, B / det, C / det, D / det, E / det, F / det, G / det, I / det, J / det];
  const scale = inv[8];
  if (Number.isFinite(scale) && Math.abs(scale) > 1e-12) return inv.map((value) => value / scale);
  return inv;
}

function pointDistance(a: Vector2, b: Vector2): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function vectorLength(point: Vector2): number {
  return Math.hypot(point.x, point.y);
}

function normalizeVector(point: Vector2): Vector2 | null {
  const length = vectorLength(point);
  if (!Number.isFinite(length) || length < MIN_TRAPEZOID_EDGE_LENGTH) return null;
  return { x: point.x / length, y: point.y / length };
}

function addVector(a: Vector2, b: Vector2): Vector2 {
  return { x: a.x + b.x, y: a.y + b.y };
}

function subtractVector(a: Vector2, b: Vector2): Vector2 {
  return { x: a.x - b.x, y: a.y - b.y };
}

function scaleVector(point: Vector2, scale: number): Vector2 {
  return { x: point.x * scale, y: point.y * scale };
}

function midpoint(a: Vector2, b: Vector2): Vector2 {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function dot(a: Vector2, b: Vector2): number {
  return a.x * b.x + a.y * b.y;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function smoothstep(edge0: number, edge1: number, value: number): number {
  if (edge0 === edge1) return value >= edge1 ? 1 : 0;
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
}

function localRefinementDelta(set: CameraControlPointSet, hImageToWorld: number[], image: Vector2): Vector2 {
  const refinementPairs = validRefinementPairs(set);
  if (refinementPairs.length === 0) return { x: 0, y: 0 };
  let totalWeight = 0;
  let totalX = 0;
  let totalY = 0;
  for (const pair of refinementPairs) {
    const base = mapHomography(hImageToWorld, pair.image);
    if (!base) continue;
    const delta = {
      x: pair.world.x - base.x,
      y: pair.world.z - base.y,
    };
    const distance = pointDistance(image, pair.image);
    if (distance <= 1e-9) return delta;
    const weight = Math.exp(-((distance / LOCAL_REFINEMENT_SIGMA_UV) ** 2));
    if (weight <= 1e-12) continue;
    totalWeight += weight;
    totalX += weight * delta.x;
    totalY += weight * delta.y;
  }
  if (totalWeight <= 1e-12) return { x: 0, y: 0 };
  const edgeDistance = Math.min(image.x, image.y, 1 - image.x, 1 - image.y);
  const edge = smoothstep(LOCAL_REFINEMENT_EDGE_LOW, LOCAL_REFINEMENT_EDGE_HIGH, edgeDistance);
  return { x: edge * (totalX / totalWeight), y: edge * (totalY / totalWeight) };
}

function boundaryAxis(edge: CameraProjectionBoundaryEdge, image: Vector2): number {
  if (edge === "top" || edge === "right") return edge === "top" ? image.x : image.y;
  return edge === "bottom" ? 1 - image.x : 1 - image.y;
}

function boundaryDistance(edge: CameraProjectionBoundaryEdge, image: Vector2): number {
  switch (edge) {
    case "top":
      return image.y;
    case "right":
      return 1 - image.x;
    case "bottom":
      return 1 - image.y;
    case "left":
      return image.x;
  }
}

function boundaryInfluence(edge: CameraProjectionBoundaryEdge, image: Vector2): number {
  const distance = boundaryDistance(edge, image);
  if (distance <= 1e-9) return 1;
  const normalized = clamp(distance / BOUNDARY_REFINEMENT_FALLOFF_UV, 0, 1);
  const eased = normalized * normalized * (3 - 2 * normalized);
  return 1 - eased;
}

function boundaryDeltaAtEdge(
  points: Array<CameraProjectionBoundaryPoint & { delta: Vector2 }>,
  edge: CameraProjectionBoundaryEdge,
  t: number,
): Vector2 {
  const edgePoints = points.filter((point) => point.edge === edge).sort((a, b) => a.t - b.t);
  if (edgePoints.length === 0) return { x: 0, y: 0 };
  const anchors = [
    { t: 0, delta: { x: 0, y: 0 } },
    ...edgePoints.map((point) => ({ t: point.t, delta: point.delta })),
    { t: 1, delta: { x: 0, y: 0 } },
  ];
  const normalizedT = clamp(t, 0, 1);
  for (let index = 0; index < anchors.length - 1; index += 1) {
    const left = anchors[index];
    const right = anchors[index + 1];
    if (normalizedT < left.t || normalizedT > right.t) continue;
    const span = right.t - left.t;
    const local = span > 1e-9 ? (normalizedT - left.t) / span : 0;
    return {
      x: left.delta.x + (right.delta.x - left.delta.x) * local,
      y: left.delta.y + (right.delta.y - left.delta.y) * local,
    };
  }
  return anchors[anchors.length - 1].delta;
}

function boundaryRefinementDelta(set: CameraControlPointSet, hImageToWorld: number[], image: Vector2): Vector2 {
  const points = validBoundaryPoints(set);
  if (points.length === 0) return { x: 0, y: 0 };
  const displacements: Array<CameraProjectionBoundaryPoint & { delta: Vector2 }> = [];
  for (const point of points) {
    const imagePoint = boundaryImageForEdge(point.edge, point.t);
    const base = mapHomography(hImageToWorld, imagePoint);
    if (!base) continue;
    displacements.push({
      ...point,
      image: imagePoint,
      delta: { x: point.world.x - base.x, y: point.world.z - base.y },
    });
  }
  if (displacements.length === 0) return { x: 0, y: 0 };
  let totalX = 0;
  let totalY = 0;
  for (const edge of BOUNDARY_EDGES) {
    const delta = boundaryDeltaAtEdge(displacements, edge, boundaryAxis(edge, image));
    const influence = boundaryInfluence(edge, image);
    totalX += delta.x * influence;
    totalY += delta.y * influence;
  }
  return { x: totalX, y: totalY };
}

function mapImageToWorldWithRefinement(set: CameraControlPointSet, hImageToWorld: number[], image: Vector2): WorldPoint | null {
  const base = mapHomography(hImageToWorld, image);
  if (!base) return null;
  const boundaryDelta = boundaryRefinementDelta(set, hImageToWorld, image);
  const delta = localRefinementDelta(set, hImageToWorld, image);
  return { x: base.x + boundaryDelta.x + delta.x, z: base.y + boundaryDelta.y + delta.y };
}

function projectionGridCoordinates(set: CameraControlPointSet, divisions: number): { xs: number[]; ys: number[] } {
  const xs = new Set<number>();
  const ys = new Set<number>();
  for (let index = 0; index <= divisions; index += 1) {
    xs.add(index / divisions);
    ys.add(index / divisions);
  }
  for (const point of validBoundaryPoints(set)) {
    const image = boundaryImageForEdge(point.edge, point.t);
    xs.add(Number(image.x.toFixed(8)));
    ys.add(Number(image.y.toFixed(8)));
  }
  return {
    xs: [...xs].sort((a, b) => a - b),
    ys: [...ys].sort((a, b) => a - b),
  };
}

function controlWorldSpan(pairs: Pair[]): number {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const pair of pairs) {
    minX = Math.min(minX, pair.world.x);
    maxX = Math.max(maxX, pair.world.x);
    minY = Math.min(minY, pair.world.z);
    maxY = Math.max(maxY, pair.world.z);
  }
  const span = Math.hypot(maxX - minX, maxY - minY);
  return Number.isFinite(span) && span > MIN_TRAPEZOID_EDGE_LENGTH ? span : 1;
}

function imageBounds(pairs: Pair[]): { minX: number; maxX: number; minY: number; maxY: number } | null {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const pair of pairs) {
    minX = Math.min(minX, pair.image.x);
    maxX = Math.max(maxX, pair.image.x);
    minY = Math.min(minY, pair.image.y);
    maxY = Math.max(maxY, pair.image.y);
  }
  if (![minX, maxX, minY, maxY].every(Number.isFinite)) return null;
  if (maxX - minX < MIN_TRAPEZOID_EDGE_LENGTH || maxY - minY < MIN_TRAPEZOID_EDGE_LENGTH) return null;
  return {
    minX: clamp(minX - TRAPEZOID_IMAGE_PADDING, 0, 1),
    maxX: clamp(maxX + TRAPEZOID_IMAGE_PADDING, 0, 1),
    minY: clamp(minY - TRAPEZOID_IMAGE_PADDING, 0, 1),
    maxY: clamp(maxY + TRAPEZOID_IMAGE_PADDING, 0, 1),
  };
}

function median(values: number[]): number {
  if (values.length === 0) return Infinity;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)] ?? Infinity;
}

function inliersForWorldToImage(pairs: Pair[], hWorldToImage: number[]): { pairs: Pair[]; errors: number[] } {
  const inlierPairs: Pair[] = [];
  const errors: number[] = [];
  for (const pair of pairs) {
    const predicted = mapHomography(hWorldToImage, { x: pair.world.x, y: pair.world.z });
    if (!predicted) continue;
    const error = pointDistance(predicted, pair.image);
    if (error <= HOMOGRAPHY_REPROJECTION_THRESHOLD_UV) {
      inlierPairs.push(pair);
      errors.push(error);
    }
  }
  return { pairs: inlierPairs, errors };
}

function estimateRobustHomography(pairs: Pair[]): HomographyEstimate | null {
  if (pairs.length < 4) return null;
  let best: { inlierPairs: Pair[]; medianError: number; hWorldToImage: number[] } | null = null;

  for (let a = 0; a < pairs.length - 3; a += 1) {
    for (let b = a + 1; b < pairs.length - 2; b += 1) {
      for (let c = b + 1; c < pairs.length - 1; c += 1) {
        for (let d = c + 1; d < pairs.length; d += 1) {
          const sample = [pairs[a], pairs[b], pairs[c], pairs[d]];
          const hWorldToImage = solveHomography(
            sample.map((pair) => ({ x: pair.world.x, y: pair.world.z })),
            sample.map((pair) => pair.image),
          );
          if (!hWorldToImage) continue;
          const inliers = inliersForWorldToImage(pairs, hWorldToImage);
          if (inliers.pairs.length < 4) continue;
          const candidate = {
            inlierPairs: inliers.pairs,
            medianError: median(inliers.errors),
            hWorldToImage,
          };
          if (
            !best ||
            candidate.inlierPairs.length > best.inlierPairs.length ||
            (candidate.inlierPairs.length === best.inlierPairs.length && candidate.medianError < best.medianError)
          ) {
            best = candidate;
          }
        }
      }
    }
  }

  const selectedPairs = best?.inlierPairs ?? pairs;
  if (selectedPairs.length < 4) return null;
  const refinedWorldToImage =
    solveHomography(
      selectedPairs.map((pair) => ({ x: pair.world.x, y: pair.world.z })),
      selectedPairs.map((pair) => pair.image),
    ) ?? best?.hWorldToImage;
  if (!refinedWorldToImage) return null;
  const hImageToWorld =
    invertHomography(refinedWorldToImage) ??
    solveHomography(
      selectedPairs.map((pair) => pair.image),
      selectedPairs.map((pair) => ({ x: pair.world.x, y: pair.world.z })),
    );
  if (!hImageToWorld) return null;
  return { hImageToWorld, inlierPairs: selectedPairs };
}

function pointKey(x: number, y: number): string {
  return `${x}:${y}`;
}

function trapezoidCornersFromHomography(estimate: HomographyEstimate): Vector2[] | null {
  const bounds = imageBounds(estimate.inlierPairs);
  if (!bounds) return null;
  const corners = [
    mapHomography(estimate.hImageToWorld, { x: bounds.minX, y: bounds.minY }),
    mapHomography(estimate.hImageToWorld, { x: bounds.maxX, y: bounds.minY }),
    mapHomography(estimate.hImageToWorld, { x: bounds.maxX, y: bounds.maxY }),
    mapHomography(estimate.hImageToWorld, { x: bounds.minX, y: bounds.maxY }),
  ];
  if (corners.some((corner) => !corner)) return null;
  const [topLeft, topRight, bottomRight, bottomLeft] = corners as Vector2[];

  const topEdge = subtractVector(topRight, topLeft);
  const bottomEdge = subtractVector(bottomRight, bottomLeft);
  const topLength = vectorLength(topEdge);
  const bottomLength = vectorLength(bottomEdge);
  if (topLength < MIN_TRAPEZOID_EDGE_LENGTH || bottomLength < MIN_TRAPEZOID_EDGE_LENGTH) return null;

  const topAxis = normalizeVector(topEdge);
  const bottomAxis = normalizeVector(bottomEdge);
  if (!topAxis || !bottomAxis) return null;

  let axisX = normalizeVector(addVector(topAxis, bottomAxis));
  if (!axisX) axisX = topAxis;

  const topCenter = midpoint(topLeft, topRight);
  const bottomCenter = midpoint(bottomLeft, bottomRight);
  const centerDelta = subtractVector(bottomCenter, topCenter);
  let axisY = { x: -axisX.y, y: axisX.x };
  if (dot(centerDelta, axisY) < 0) axisY = scaleVector(axisY, -1);
  let height = Math.abs(dot(centerDelta, axisY));
  if (height < MIN_TRAPEZOID_EDGE_LENGTH) height = vectorLength(centerDelta);
  if (height < MIN_TRAPEZOID_EDGE_LENGTH) return null;

  const controlSpan = controlWorldSpan(estimate.inlierPairs);
  const maxDimension = controlSpan * MAX_TRAPEZOID_CONTROL_SPAN_RATIO;
  let constrainedTopLength = clamp(topLength, MIN_TRAPEZOID_EDGE_LENGTH, maxDimension);
  let constrainedBottomLength = clamp(bottomLength, MIN_TRAPEZOID_EDGE_LENGTH, maxDimension);
  const smallerWidth = Math.max(MIN_TRAPEZOID_EDGE_LENGTH, Math.min(constrainedTopLength, constrainedBottomLength));
  const largerWidth = Math.max(constrainedTopLength, constrainedBottomLength);
  if (largerWidth / smallerWidth > MAX_TRAPEZOID_WIDTH_RATIO) {
    const cappedLarger = smallerWidth * MAX_TRAPEZOID_WIDTH_RATIO;
    if (constrainedTopLength > constrainedBottomLength) constrainedTopLength = cappedLarger;
    else constrainedBottomLength = cappedLarger;
  }

  const constrainedHeight = clamp(height, MIN_TRAPEZOID_EDGE_LENGTH, maxDimension);
  const center = midpoint(topCenter, bottomCenter);
  const topConstrainedCenter = addVector(center, scaleVector(axisY, -constrainedHeight / 2));
  const bottomConstrainedCenter = addVector(center, scaleVector(axisY, constrainedHeight / 2));
  const halfTop = scaleVector(axisX, constrainedTopLength / 2);
  const halfBottom = scaleVector(axisX, constrainedBottomLength / 2);

  return [
    subtractVector(topConstrainedCenter, halfTop),
    addVector(topConstrainedCenter, halfTop),
    addVector(bottomConstrainedCenter, halfBottom),
    subtractVector(bottomConstrainedCenter, halfBottom),
  ];
}

function buildQuadMesh(corners: Vector2[]): ProjectionMeshData | null {
  if (corners.length !== 4) return null;
  return {
    positions: new Float32Array([
      corners[0].x,
      PROJECTION_Y_OFFSET,
      corners[0].y,
      corners[1].x,
      PROJECTION_Y_OFFSET,
      corners[1].y,
      corners[2].x,
      PROJECTION_Y_OFFSET,
      corners[2].y,
      corners[3].x,
      PROJECTION_Y_OFFSET,
      corners[3].y,
    ]),
    uvs: new Float32Array([0, 0, 1, 0, 1, 1, 0, 1]),
    indices: new Uint32Array([0, 1, 2, 0, 2, 3]),
  };
}

export const homographyGridProjectionStrategy: ProjectionStrategy = {
  id: "homography_grid",
  buildMesh(set, options) {
    const pairs = validBasePairs(set);
    if (pairs.length < 4) return null;
    const estimate = estimateRobustHomography(pairs);
    if (!estimate) return null;
    const hull = convexHull(estimate.inlierPairs.map((pair) => pair.image));
    if (hull.length < 3) return null;
    const gridDivisions = options?.gridDivisions ?? DEFAULT_GRID_DIVISIONS;

    const vertices: number[] = [];
    const uvs: number[] = [];
    const indices: number[] = [];
    const vertexByGrid = new Map<string, number>();
    const grid = projectionGridCoordinates(set, gridDivisions);

    const getVertex = (gx: number, gy: number): number | null => {
      const key = pointKey(gx, gy);
      const existing = vertexByGrid.get(key);
      if (existing != null) return existing;

      const u = grid.xs[gx];
      const v = grid.ys[gy];
      const world = mapImageToWorldWithRefinement(set, estimate.hImageToWorld, { x: u, y: v });
      if (!world) return null;
      const index = vertices.length / 3;
      vertices.push(world.x, PROJECTION_Y_OFFSET, world.z);
      uvs.push(u, v);
      vertexByGrid.set(key, index);
      return index;
    };

    for (let gy = 0; gy < grid.ys.length - 1; gy += 1) {
      for (let gx = 0; gx < grid.xs.length - 1; gx += 1) {
        const center = { x: (grid.xs[gx] + grid.xs[gx + 1]) / 2, y: (grid.ys[gy] + grid.ys[gy + 1]) / 2 };
        if (!pointInPolygon(center, hull)) continue;
        const a = getVertex(gx, gy);
        const b = getVertex(gx + 1, gy);
        const c = getVertex(gx + 1, gy + 1);
        const d = getVertex(gx, gy + 1);
        if (a == null || b == null || c == null || d == null) continue;
        indices.push(a, b, c, a, c, d);
      }
    }

    if (indices.length < 3) return null;
    const meshData = {
      positions: new Float32Array(vertices),
      uvs: new Float32Array(uvs),
      indices: new Uint32Array(indices),
    };
    if (meshHasFoldover(meshData)) return null;
    return finalizeProjectionMesh(meshData, options);
  },
};

export const constrainedTrapezoidProjectionStrategy: ProjectionStrategy = {
  id: "constrained_trapezoid",
  buildMesh(set, options) {
    const pairs = validBasePairs(set);
    if (pairs.length < 4) return null;
    const estimate = estimateRobustHomography(pairs);
    if (!estimate) return null;
    const corners = trapezoidCornersFromHomography(estimate);
    const mesh = corners ? buildQuadMesh(corners) : null;
    return mesh ? finalizeProjectionMesh(mesh, options) : null;
  },
};

export const projectionStrategies: Record<ProjectionStrategyId, ProjectionStrategy> = {
  homography_grid: homographyGridProjectionStrategy,
  constrained_trapezoid: constrainedTrapezoidProjectionStrategy,
};
