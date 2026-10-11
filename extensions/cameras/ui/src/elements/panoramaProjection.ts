/** Canonical spherical geometry only; viewport transforms belong to the editor. */
export type PanoramaProjectionSolution = {
  type: "panorama_ray_plane_v1";
  matrix: number[][] | null;
  inverse_matrix: number[][] | null;
  support_polygon: Array<[number, number]>;
  quality: {
    status: string;
    reasons: string[];
    warnings?: string[];
    number_of_fit_points: number;
    angular_threshold_radians: number;
    median_fit_error_radians: number | null;
    p95_fit_error_radians: number | null;
    point_errors?: Array<{ id: string; error_radians: number }>;
    [key: string]: unknown;
  };
  preview: {
    eligible: boolean;
    status: "blocked" | "ready";
    reasons: string[];
    context?: {
      job_id: string;
      revision: number;
      source_id: string;
      source_artifact_id: string | null;
      source_artifact_revision: number | null;
    };
  };
};

/** One decoded grayscale coverage byte per canonical panorama pixel. */
export type PanoramaCoverageMask = {
  width: number;
  height: number;
  data: Uint8Array | Uint8ClampedArray;
};

export type PanoramaPixel = { x: number; y: number };
export type PanoramaWorldPoint = { x: number; z: number };
export type PanoramaProjector = {
  panoramaToWorld: (point: PanoramaPixel) => PanoramaWorldPoint | null;
  worldToPanorama: (point: PanoramaWorldPoint) => PanoramaPixel | null;
};

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
type Vector = [number, number, number];
const multiply = (matrix: number[][], vector: Vector): Vector => matrix.map(
  (row) => row[0] * vector[0] + row[1] * vector[1] + row[2] * vector[2],
) as Vector;
const unit = (vector: Vector): Vector | null => {
  const length = Math.hypot(...vector);
  return vector.every(finite) && finite(length) && length > 1e-12
    ? vector.map((value) => value / length) as Vector : null;
};

export function insideSupport(x: number, z: number, polygon: Array<[number, number]>): boolean {
  let sign = 0;
  for (let index = 0; index < polygon.length; index += 1) {
    const start = polygon[index];
    const end = polygon[(index + 1) % polygon.length];
    const cross = (end[0] - start[0]) * (z - start[1]) - (end[1] - start[1]) * (x - start[0]);
    if (!finite(cross)) return false;
    if (Math.abs(cross) <= 1e-9) continue;
    const current = Math.sign(cross);
    if (sign && current !== sign) return false;
    sign = current;
  }
  return true;
}

/** Validate once per revision/mask change; hovering performs no fitting or I/O.
 * The caller must discard this projector when source/revision identity changes.
 */
export function createPanoramaProjector(
  solution: PanoramaProjectionSolution | null | undefined,
  coverage: PanoramaCoverageMask | null | undefined,
): PanoramaProjector | null {
  const quality = solution?.quality;
  if (!solution || solution.type !== "panorama_ray_plane_v1" || !quality
    || solution.preview?.eligible !== true || quality.status !== "ready"
    || !Array.isArray(quality.reasons) || quality.reasons.length
    || !Array.isArray(solution.preview.reasons) || solution.preview.reasons.length
    || !finite(quality.number_of_fit_points) || !Number.isInteger(quality.number_of_fit_points)
    || quality.number_of_fit_points < 4 || quality.number_of_fit_points > 64) return null;
  const { matrix, inverse_matrix: inverse, support_polygon: support } = solution;
  if (![matrix, inverse].every((value) => Array.isArray(value) && value.length === 3
    && value.every((row) => Array.isArray(row) && row.length === 3 && row.every(finite)))
    || !matrix || !inverse || !Array.isArray(support) || support.length < 3 || support.length > 64
    || support.some((point) => !Array.isArray(point) || point.length !== 2 || !point.every(finite))) return null;
  for (const [left, right] of [[matrix, inverse], [inverse, matrix]]) {
    for (let row = 0; row < 3; row += 1) {
      for (let column = 0; column < 3; column += 1) {
        const value = left[row].reduce((sum, entry, index) => sum + entry * right[index][column], 0);
        if (!finite(value) || Math.abs(value - Number(row === column)) > 1e-6) return null;
      }
    }
  }
  // The server uses a 2-norm condition ceiling. Frobenius adds a conservative
  // factor of three here while catching singular or wildly scaled stale input.
  if (Math.hypot(...matrix.flat()) * Math.hypot(...inverse.flat()) > 3e12) return null;
  const area = support.reduce((sum, start, index) => {
    const end = support[(index + 1) % support.length];
    return sum + start[0] * end[1] - end[0] * start[1];
  }, 0) / 2;
  if (!finite(area) || Math.abs(area) <= 1e-9 || support.some(([x, z]) => !insideSupport(x, z, support))) return null;
  if (!coverage || !Number.isInteger(coverage.width) || !Number.isInteger(coverage.height)
    || coverage.width < 2 || coverage.height < 2 || coverage.width * coverage.height > 50_000_000
    || !(coverage.data instanceof Uint8Array || coverage.data instanceof Uint8ClampedArray)
    || coverage.data.length !== coverage.width * coverage.height) return null;

  const covered = ({ x, y }: PanoramaPixel): boolean => finite(x) && finite(y)
    && x >= 0 && x <= 1 && y >= 0 && y <= 1
    && coverage.data[Math.min(coverage.height - 1, Math.floor(y * coverage.height)) * coverage.width
      + Math.min(coverage.width - 1, Math.floor(x * coverage.width))] > 0;
  const rayToWorld = (ray: Vector): PanoramaWorldPoint | null => {
    const point = multiply(inverse, ray);
    // Same positive homogeneous scale/horizon guard as Python map_ray_to_world.
    if (!point.every(finite) || point[2] <= 1e-12) return null;
    const x = point[0] / point[2];
    const z = point[1] / point[2];
    return finite(x) && finite(z) ? { x, z } : null;
  };
  return {
    panoramaToWorld(point) {
      if (!covered(point)) return null;
      // Endpoints share a ray. Require its Python-canonical representative to
      // be captured too, so a masked seam/pole cannot produce a one-way ghost.
      const canonicalX = point.y === 0 || point.y === 1 ? 0.5 : point.x === 1 ? 0 : point.x;
      if (canonicalX !== point.x && !covered({ x: canonicalX, y: point.y })) return null;
      const pan = (point.x * 2 - 1) * Math.PI;
      const elevation = (0.5 - point.y) * Math.PI;
      return rayToWorld([Math.cos(elevation) * Math.cos(pan), Math.cos(elevation) * Math.sin(pan), Math.sin(elevation)]);
    },
    worldToPanorama({ x, z }) {
      if (!finite(x) || !finite(z)) return null;
      const ray = unit(multiply(matrix, [x, z, 1]));
      if (!ray || !rayToWorld(ray)) return null;
      const horizontal = Math.hypot(ray[0], ray[1]);
      const pan = horizontal > 1e-12 ? Math.atan2(ray[1], ray[0]) : 0;
      const point = { x: ((pan + Math.PI) / (2 * Math.PI)) % 1, y: 0.5 - Math.atan2(ray[2], horizontal) / Math.PI };
      return covered(point) ? point : null;
    },
  };
}
