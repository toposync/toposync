import type { CameraControlPointSet, ProjectionMeshData, Vector2, WorldPoint } from "./types";

export type ProjectionStrategyId = "homography_grid" | "constrained_trapezoid";

export type ProjectionStrategy = {
  id: ProjectionStrategyId;
  buildMesh: (set: CameraControlPointSet) => ProjectionMeshData | null;
};

type Pair = { image: Vector2; world: WorldPoint };
type HomographyEstimate = { hImageToWorld: number[]; inlierPairs: Pair[] };

const GRID_DIVISIONS = 34;
const PROJECTION_Y_OFFSET = 0.026;
const HOMOGRAPHY_REPROJECTION_THRESHOLD_UV = 0.02;
const MIN_TRAPEZOID_EDGE_LENGTH = 1e-4;
const MAX_TRAPEZOID_WIDTH_RATIO = 2.5;
const MAX_TRAPEZOID_CONTROL_SPAN_RATIO = 2.8;
const TRAPEZOID_IMAGE_PADDING = 0.04;

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
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
  buildMesh(set) {
    const pairs = validPairs(set);
    if (pairs.length < 4) return null;
    const estimate = estimateRobustHomography(pairs);
    if (!estimate) return null;
    const hull = convexHull(estimate.inlierPairs.map((pair) => pair.image));
    if (hull.length < 3) return null;

    const vertices: number[] = [];
    const uvs: number[] = [];
    const indices: number[] = [];
    const vertexByGrid = new Map<string, number>();

    const getVertex = (gx: number, gy: number): number | null => {
      const key = pointKey(gx, gy);
      const existing = vertexByGrid.get(key);
      if (existing != null) return existing;

      const u = gx / GRID_DIVISIONS;
      const v = gy / GRID_DIVISIONS;
      const mapped = mapHomography(estimate.hImageToWorld, { x: u, y: v });
      const world = mapped ? { x: mapped.x, z: mapped.y } : null;
      if (!world) return null;
      const index = vertices.length / 3;
      vertices.push(world.x, PROJECTION_Y_OFFSET, world.z);
      uvs.push(u, v);
      vertexByGrid.set(key, index);
      return index;
    };

    for (let gy = 0; gy < GRID_DIVISIONS; gy += 1) {
      for (let gx = 0; gx < GRID_DIVISIONS; gx += 1) {
        const center = { x: (gx + 0.5) / GRID_DIVISIONS, y: (gy + 0.5) / GRID_DIVISIONS };
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
    return {
      positions: new Float32Array(vertices),
      uvs: new Float32Array(uvs),
      indices: new Uint32Array(indices),
    };
  },
};

export const constrainedTrapezoidProjectionStrategy: ProjectionStrategy = {
  id: "constrained_trapezoid",
  buildMesh(set) {
    const pairs = validPairs(set);
    if (pairs.length < 4) return null;
    const estimate = estimateRobustHomography(pairs);
    if (!estimate) return null;
    const corners = trapezoidCornersFromHomography(estimate);
    return corners ? buildQuadMesh(corners) : null;
  },
};

export const projectionStrategies: Record<ProjectionStrategyId, ProjectionStrategy> = {
  homography_grid: homographyGridProjectionStrategy,
  constrained_trapezoid: constrainedTrapezoidProjectionStrategy,
};
