import type { CameraControlPointSet, ProjectionMeshData, Vector2, WorldPoint } from "./types";

export type ProjectionStrategy = {
  id: string;
  buildMesh: (set: CameraControlPointSet) => ProjectionMeshData | null;
};

type Pair = { image: Vector2; world: WorldPoint };
type HomographyEstimate = { hImageToWorld: number[]; inlierPairs: Pair[] };

const GRID_DIVISIONS = 34;
const PROJECTION_Y_OFFSET = 0.026;
const HOMOGRAPHY_REPROJECTION_THRESHOLD_UV = 0.02;

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
