/** Canonical spherical geometry only; viewport transforms belong to the editor. */
export type PanoramaProjectionSolution = {
  type: "panorama_ray_plane_v1";
  matrix: number[][] | null;
  inverse_matrix: number[][] | null;
  support_polygon: Array<[number, number]>;
  inlier_point_ids?: string[];
  quality: {
    status: string;
    reasons: string[];
    number_of_fit_points: number;
    number_of_check_points: number;
    number_of_inliers: number;
    inlier_ratio: number;
    angular_threshold_radians: number;
    median_fit_error_radians: number | null;
    p95_fit_error_radians: number | null;
    maximum_check_error_radians: number | null;
    [key: string]: unknown;
  };
  preview: {
    eligible: boolean;
    status: "blocked" | "provisional" | "ready";
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
const missingChecks = "at_least_two_independent_check_points_required";
type Vector = [number, number, number];
const multiply = (matrix: number[][], vector: Vector): Vector => matrix.map(
  (row) => row[0] * vector[0] + row[1] * vector[1] + row[2] * vector[2],
) as Vector;
const unit = (vector: Vector): Vector | null => {
  const length = Math.hypot(...vector);
  return vector.every(finite) && finite(length) && length > 1e-12
    ? vector.map((value) => value / length) as Vector : null;
};

function insideSupport(x: number, z: number, polygon: Array<[number, number]>): boolean {
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
  const preview = solution?.preview;
  if (!solution || solution.type !== "panorama_ray_plane_v1" || !quality || preview?.eligible !== true
    || !["provisional", "ready"].includes(preview.status) || !["ready", "review"].includes(quality.status)
    || !Array.isArray(quality.reasons) || quality.reasons.some((reason) => reason !== missingChecks)
    || !Array.isArray(preview.reasons) || preview.reasons.some((reason) => reason !== missingChecks)) return null;
  const fitCount = quality.number_of_fit_points;
  const checkCount = quality.number_of_check_points;
  const inliers = quality.number_of_inliers;
  const threshold = quality.angular_threshold_radians;
  const median = quality.median_fit_error_radians;
  const percentile = quality.p95_fit_error_radians;
  const provisional = quality.reasons.includes(missingChecks);
  if (![fitCount, checkCount, inliers].every((value) => finite(value) && Number.isInteger(value))
    || inliers < 6 || fitCount < inliers || fitCount > 64 || checkCount < 0 || fitCount + checkCount > 64
    || !finite(quality.inlier_ratio) || quality.inlier_ratio < 0.8 || quality.inlier_ratio > 1
    || Math.abs(quality.inlier_ratio - inliers / fitCount) > 1e-9
    || !finite(threshold) || threshold <= 0 || threshold >= Math.PI / 4
    || !finite(median) || !finite(percentile) || median < 0 || percentile < median || percentile > threshold
    || (checkCount < 2) !== provisional || provisional !== (preview.status === "provisional")
    || (checkCount > 0 && (!finite(quality.maximum_check_error_radians)
      || quality.maximum_check_error_radians < 0 || quality.maximum_check_error_radians > threshold))) return null;
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
    return finite(x) && finite(z) && insideSupport(x, z, support) ? { x, z } : null;
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
      if (!finite(x) || !finite(z) || !insideSupport(x, z, support)) return null;
      const ray = unit(multiply(matrix, [x, z, 1]));
      if (!ray || !rayToWorld(ray)) return null;
      const horizontal = Math.hypot(ray[0], ray[1]);
      const pan = horizontal > 1e-12 ? Math.atan2(ray[1], ray[0]) : 0;
      const point = { x: ((pan + Math.PI) / (2 * Math.PI)) % 1, y: 0.5 - Math.atan2(ray[2], horizontal) / Math.PI };
      return covered(point) ? point : null;
    },
  };
}
