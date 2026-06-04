import type {
  ActiveProjectionPose,
  CameraControlPoint,
  CameraControlPointSet,
  CameraPoseReference,
  CameraProjectionRefinementPoint,
  PtzPreset,
  PtzStatus,
} from "./types";

const PAN_TILT_SIGMA = 0.04;
const PAN_TILT_MAX_DISTANCE = 3.0;
const POSE_MATCH_DISTANCE = 0.35;
const ZOOM_MATCH_TOLERANCE = 0.08;
const PRESET_POSE_TOLERANCE = 0.015;
const HYSTERESIS_MARGIN = 0.12;
const EXTRAPOLATION_LIMIT_RATIO = 0.35;
const MAX_INTERPOLATION_NEIGHBORS = 3;
const EPSILON = 1e-6;

type AxisKey = "pan" | "tilt" | "zoom";

type PoseAxis = {
  axis: AxisKey;
  current: number;
  min: number;
  max: number;
  range: number;
  scale: number;
};

type RankedPoseSet = {
  set: CameraControlPointSet;
  distance: number;
};

const AXES: AxisKey[] = ["pan", "tilt", "zoom"];

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function normalizedToken(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function normalizedName(value: unknown): string {
  return normalizedToken(value)
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function presetMatchesStatus(preset: PtzPreset, status: PtzStatus): boolean {
  const statusPreset = normalizedToken(status.preset_token ?? status.preset_name);
  if (statusPreset) {
    const presetId = normalizedToken(preset.token);
    const presetName = normalizedToken(preset.name);
    if (presetId === statusPreset || presetName === statusPreset) {
      return true;
    }
  }

  const statusName = normalizedName(status.preset_name);
  if (statusName && normalizedName(preset.name) === statusName) {
    return true;
  }

  return false;
}

function enrichStatusWithPreset(status: PtzStatus, presets: PtzPreset[] = []): PtzStatus {
  if (!presets.length) {
    return status;
  }
  let matchedPreset = presets.find((preset) => presetMatchesStatus(preset, status));
  if (!matchedPreset && finite(status.pan) && finite(status.tilt)) {
    matchedPreset =
      presets
        .map((preset) => {
          if (!finite(preset.pan) || !finite(preset.tilt)) {
            return null;
          }
          return { preset, distance: Math.hypot(preset.pan - status.pan, preset.tilt - status.tilt) };
        })
        .filter((entry): entry is { preset: PtzPreset; distance: number } => Boolean(entry))
        .sort((left, right) => left.distance - right.distance)
        .find((entry) => entry.distance <= PRESET_POSE_TOLERANCE)?.preset ?? null;
  }
  if (!matchedPreset) {
    return status;
  }
  return {
    ...status,
    preset_token: status.preset_token ?? matchedPreset.token,
    preset_name: status.preset_name ?? matchedPreset.name,
    pan: finite(status.pan) ? status.pan : matchedPreset.pan,
    tilt: finite(status.tilt) ? status.tilt : matchedPreset.tilt,
    zoom: finite(status.zoom) ? status.zoom : matchedPreset.zoom,
  };
}

function poseScore(pose: CameraPoseReference | null | undefined, status: PtzStatus): number | null {
  if (!pose) {
    return null;
  }

  const posePresetToken = normalizedToken(pose.preset_token);
  const posePresetName = normalizedToken(pose.preset_name);
  const statusPreset = normalizedToken(status.preset_token ?? status.preset_name);
  if (posePresetToken && statusPreset && posePresetToken === statusPreset) {
    return 0;
  }
  if (posePresetName && statusPreset && posePresetName === statusPreset) {
    return 0;
  }
  const statusPresetName = normalizedName(status.preset_name);
  if (pose.preset_name && statusPresetName && normalizedName(pose.preset_name) === statusPresetName) {
    return 0;
  }

  const parts: number[] = [];
  if (finite(pose.pan) && finite(status.pan)) {
    parts.push(Math.abs(pose.pan - status.pan) / PAN_TILT_SIGMA);
  }
  if (finite(pose.tilt) && finite(status.tilt)) {
    parts.push(Math.abs(pose.tilt - status.tilt) / PAN_TILT_SIGMA);
  }
  if (finite(pose.zoom) && finite(status.zoom)) {
    const zoomDistance = Math.abs(pose.zoom - status.zoom);
    if (zoomDistance <= ZOOM_MATCH_TOLERANCE) {
      parts.push(zoomDistance / ZOOM_MATCH_TOLERANCE);
    } else {
      parts.push(PAN_TILT_MAX_DISTANCE + zoomDistance / ZOOM_MATCH_TOLERANCE);
    }
  }

  if (!parts.length) {
    return null;
  }

  const score = Math.sqrt(parts.reduce((sum, value) => sum + value * value, 0) / parts.length);
  return score <= PAN_TILT_MAX_DISTANCE ? score : null;
}

function hasPoseReference(pose: CameraPoseReference | null | undefined): boolean {
  if (!pose) {
    return false;
  }
  return Boolean(
    normalizedToken(pose.preset_token) ||
      normalizedToken(pose.preset_name) ||
      finite(pose.pan) ||
      finite(pose.tilt) ||
      finite(pose.zoom),
  );
}

function poseAxisValue(pose: CameraPoseReference | null | undefined, axis: AxisKey): number | null {
  const value = pose?.[axis];
  return finite(value) ? value : null;
}

function statusAxisValue(status: PtzStatus, axis: AxisKey): number | null {
  const value = status[axis];
  return finite(value) ? value : null;
}

function axisFallbackScale(axis: AxisKey): number {
  return axis === "zoom" ? ZOOM_MATCH_TOLERANCE : PAN_TILT_SIGMA;
}

function validControlPoint(point: CameraControlPoint | null | undefined): point is CameraControlPoint & {
  image: { x: number; y: number };
  world: { x: number; z: number };
} {
  return Boolean(
    point &&
      finite(point.image?.x) &&
      finite(point.image?.y) &&
      finite(point.world?.x) &&
      finite(point.world?.z),
  );
}

function cloneControlPoint(point: CameraControlPoint): CameraControlPoint {
  return {
    ...point,
    image: { x: point.image.x, y: point.image.y },
    world: { x: point.world.x, z: point.world.z },
  };
}

function cloneRefinementPoint(point: CameraProjectionRefinementPoint): CameraProjectionRefinementPoint {
  return {
    ...point,
    image: { x: point.image.x, y: point.image.y },
    world: { x: point.world.x, z: point.world.z },
  };
}

function cloneStreamScope(scope: CameraControlPointSet["stream_scope"]): CameraControlPointSet["stream_scope"] {
  if (!scope) {
    return null;
  }
  return {
    compatible_roles: Array.isArray(scope.compatible_roles) ? [...scope.compatible_roles] : undefined,
    compatible_source_ids: Array.isArray(scope.compatible_source_ids) ? [...scope.compatible_source_ids] : undefined,
  };
}

function completeBaseControlPoints(set: CameraControlPointSet): CameraControlPoint[] {
  return (set.control_points ?? []).filter(validControlPoint).slice(0, 4).map(cloneControlPoint);
}

function resolvePoseAxes(sets: CameraControlPointSet[], status: PtzStatus): PoseAxis[] {
  return AXES.flatMap((axis) => {
    const current = statusAxisValue(status, axis);
    if (!finite(current)) {
      return [];
    }
    const values = sets
      .map((set) => poseAxisValue(set.pose_reference, axis))
      .filter((value): value is number => finite(value));
    if (!values.length) {
      return [];
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min;
    return [
      {
        axis,
        current,
        min,
        max,
        range,
        scale: range > EPSILON ? range : axisFallbackScale(axis),
      },
    ];
  });
}

function poseDistance(set: CameraControlPointSet, axes: PoseAxis[]): number | null {
  const parts = axes.flatMap((axis) => {
    const value = poseAxisValue(set.pose_reference, axis.axis);
    if (!finite(value)) {
      return [];
    }
    return [(axis.current - value) / Math.max(axis.scale, EPSILON)];
  });

  if (!parts.length) {
    return null;
  }

  return Math.sqrt(parts.reduce((sum, value) => sum + value * value, 0) / parts.length);
}

function rankPoseSets(sets: CameraControlPointSet[], axes: PoseAxis[]): RankedPoseSet[] {
  return sets
    .map((set) => {
      const distance = poseDistance(set, axes);
      return distance === null ? null : { set, distance };
    })
    .filter((entry): entry is RankedPoseSet => Boolean(entry))
    .sort((left, right) => left.distance - right.distance);
}

function envelopeOffsetRatio(axes: PoseAxis[]): number | null {
  const ratios = axes
    .filter((axis) => axis.range > EPSILON)
    .map((axis) => {
      if (axis.current < axis.min) {
        return (axis.min - axis.current) / axis.range;
      }
      if (axis.current > axis.max) {
        return (axis.current - axis.max) / axis.range;
      }
      return 0;
    });

  if (!ratios.length) {
    return null;
  }

  return Math.max(...ratios);
}

function selectBestMatchedSet(
  sets: CameraControlPointSet[],
  status: PtzStatus,
  previousSetId?: string | null,
): CameraControlPointSet | null {
  const scored = sets
    .map((set) => {
      const score = poseScore(set.pose_reference, status);
      return score === null ? null : { set, score };
    })
    .filter((entry): entry is { set: CameraControlPointSet; score: number } => Boolean(entry))
    .sort((left, right) => left.score - right.score);

  const best = scored[0];
  if (!best || best.score > POSE_MATCH_DISTANCE) {
    return null;
  }

  const previous = scored.find((entry) => entry.set.id === previousSetId);
  if (
    previous &&
    previous.score <= POSE_MATCH_DISTANCE + HYSTERESIS_MARGIN &&
    previous.score <= best.score + HYSTERESIS_MARGIN
  ) {
    return previous.set;
  }

  return best.set;
}

function selectNearestReference(
  ranked: RankedPoseSet[],
  previousSetId?: string | null,
): CameraControlPointSet | null {
  const nearest = ranked[0];
  if (!nearest) {
    return null;
  }
  const previous = ranked.find((entry) => entry.set.id === previousSetId);
  if (previous && previous.distance <= nearest.distance + HYSTERESIS_MARGIN) {
    return previous.set;
  }
  return nearest.set;
}

function normalizedWeights(entries: RankedPoseSet[]): number[] {
  const raw = entries.map((entry) => 1 / Math.max(entry.distance, EPSILON) ** 2);
  const total = raw.reduce((sum, value) => sum + value, 0);
  if (total <= EPSILON) {
    return raw.map(() => 1 / raw.length);
  }
  return raw.map((value) => value / total);
}

function weightedControlPoint(
  index: number,
  entries: RankedPoseSet[],
  weights: number[],
): CameraControlPoint | null {
  const dominantPoint = entries[0]?.set.control_points?.[index];
  if (!validControlPoint(dominantPoint)) {
    return null;
  }

  let total = 0;
  let imageX = 0;
  let imageY = 0;
  let worldX = 0;
  let worldZ = 0;

  entries.forEach((entry, entryIndex) => {
    const point = entry.set.control_points?.[index];
    if (!validControlPoint(point)) {
      return;
    }
    const weight = weights[entryIndex] ?? 0;
    total += weight;
    imageX += point.image.x * weight;
    imageY += point.image.y * weight;
    worldX += point.world.x * weight;
    worldZ += point.world.z * weight;
  });

  if (total <= EPSILON) {
    return cloneControlPoint(dominantPoint);
  }

  return {
    id: dominantPoint.id || `corner-${index + 1}`,
    label: dominantPoint.label,
    image: { x: imageX / total, y: imageY / total },
    world: { x: worldX / total, z: worldZ / total },
  };
}

function weightedRefinementPoint(
  id: string,
  contributors: { point: CameraProjectionRefinementPoint; weight: number }[],
): CameraProjectionRefinementPoint | null {
  let total = 0;
  let imageX = 0;
  let imageY = 0;
  let worldX = 0;
  let worldZ = 0;

  contributors.forEach(({ point, weight }) => {
    if (!finite(point.image.x) || !finite(point.image.y) || !finite(point.world.x) || !finite(point.world.z)) {
      return;
    }
    total += weight;
    imageX += point.image.x * weight;
    imageY += point.image.y * weight;
    worldX += point.world.x * weight;
    worldZ += point.world.z * weight;
  });

  if (total <= EPSILON) {
    return null;
  }

  return {
    id,
    image: { x: imageX / total, y: imageY / total },
    world: { x: worldX / total, z: worldZ / total },
  };
}

function interpolateRefinementPoints(
  entries: RankedPoseSet[],
  weights: number[],
): CameraProjectionRefinementPoint[] {
  const byId = new Map<string, { point: CameraProjectionRefinementPoint; weight: number }[]>();
  entries.forEach((entry, entryIndex) => {
    (entry.set.refinement_points ?? []).forEach((point) => {
      if (!point.id) {
        return;
      }
      const list = byId.get(point.id) ?? [];
      list.push({ point, weight: weights[entryIndex] ?? 0 });
      byId.set(point.id, list);
    });
  });

  const pairedIds = new Set<string>();
  const interpolated: CameraProjectionRefinementPoint[] = [];
  byId.forEach((contributors, id) => {
    if (contributors.length < 2) {
      return;
    }
    const point = weightedRefinementPoint(id, contributors);
    if (!point) {
      return;
    }
    pairedIds.add(id);
    interpolated.push(point);
  });

  const dominant = entries[0]?.set;
  if (dominant) {
    (dominant.refinement_points ?? []).forEach((point) => {
      if (!point.id || pairedIds.has(point.id)) {
        return;
      }
      interpolated.push(cloneRefinementPoint(point));
    });
  }

  return interpolated;
}

function currentPoseReference(status: PtzStatus): CameraPoseReference {
  return {
    preset_token: status.preset_token ?? null,
    preset_name: status.preset_name ?? null,
    pan: finite(status.pan) ? status.pan : null,
    tilt: finite(status.tilt) ? status.tilt : null,
    zoom: finite(status.zoom) ? status.zoom : null,
  };
}

function buildSyntheticSet(
  statusKind: "interpolated" | "extrapolated",
  ranked: RankedPoseSet[],
  status: PtzStatus,
): CameraControlPointSet | null {
  const entries = ranked.slice(0, MAX_INTERPOLATION_NEIGHBORS);
  if (entries.length < 2) {
    return null;
  }

  const weights = normalizedWeights(entries);
  const controlPoints = [0, 1, 2, 3]
    .map((index) => weightedControlPoint(index, entries, weights))
    .filter((point): point is CameraControlPoint => Boolean(point));

  if (controlPoints.length < 4) {
    return null;
  }

  const dominant = entries[0].set;
  const roundedPose = AXES.map((axis) => {
    const value = statusAxisValue(status, axis);
    return value === null ? "na" : value.toFixed(4);
  }).join(":");

  return {
    ...dominant,
    id: `synthetic:${statusKind}:${entries.map((entry) => entry.set.id).join("+")}:${roundedPose}`,
    label: statusKind === "interpolated" ? "Pose interpolada" : "Pose extrapolada",
    pose_reference: currentPoseReference(status),
    control_points: controlPoints,
    refinement_points: interpolateRefinementPoints(entries, weights),
    stream_scope: cloneStreamScope(dominant.stream_scope),
  };
}

export function interpolateControlPointSet(args: {
  sets: CameraControlPointSet[];
  fallback: CameraControlPointSet;
  ptzStatus: PtzStatus;
  previousSetId?: string | null;
}): { set: CameraControlPointSet; status: "interpolated" | "extrapolated" | "nearest_reference" | "single_reference" } | null {
  const usableSets = args.sets.filter((set) => completeBaseControlPoints(set).length >= 4);
  if (usableSets.length === 1) {
    return { set: usableSets[0], status: "single_reference" };
  }

  const posedSets = usableSets.filter((set) => hasPoseReference(set.pose_reference));
  if (posedSets.length <= 1) {
    const single = posedSets[0] ?? usableSets[0] ?? args.fallback;
    return single ? { set: single, status: "single_reference" } : null;
  }

  const axes = resolvePoseAxes(posedSets, args.ptzStatus);
  if (!axes.length) {
    const fallbackRanked = posedSets
      .map((set) => ({ set, distance: set.id === args.previousSetId ? 0 : 1 }))
      .sort((left, right) => left.distance - right.distance);
    const nearest = selectNearestReference(fallbackRanked, args.previousSetId);
    return nearest ? { set: nearest, status: "nearest_reference" } : null;
  }

  const ranked = rankPoseSets(posedSets, axes);
  if (ranked.length < 2) {
    const nearest = selectNearestReference(ranked, args.previousSetId);
    return nearest ? { set: nearest, status: "nearest_reference" } : null;
  }

  const offset = envelopeOffsetRatio(axes);
  if (offset === null || offset <= EPSILON) {
    const synthetic = buildSyntheticSet("interpolated", ranked, args.ptzStatus);
    return synthetic ? { set: synthetic, status: "interpolated" } : null;
  }

  if (offset <= EXTRAPOLATION_LIMIT_RATIO) {
    const synthetic = buildSyntheticSet("extrapolated", ranked, args.ptzStatus);
    return synthetic ? { set: synthetic, status: "extrapolated" } : null;
  }

  const nearest = selectNearestReference(ranked, args.previousSetId);
  return nearest ? { set: nearest, status: "nearest_reference" } : null;
}

export function resolveActiveProjectionPose(args: {
  sets: CameraControlPointSet[];
  fallback: CameraControlPointSet;
  ptzStatus: PtzStatus | null;
  presets?: PtzPreset[];
  previousSetId?: string | null;
}): ActiveProjectionPose {
  const status = args.ptzStatus ? enrichStatusWithPreset(args.ptzStatus, args.presets) : null;
  const moving = String(status?.move_status || "").toLowerCase() === "moving";

  if (!status) {
    return { set: args.fallback, status: "fallback", moving };
  }

  const usableSets = args.sets.filter((set) => completeBaseControlPoints(set).length >= 4);
  if (usableSets.length === 1) {
    return { set: usableSets[0], status: "single_reference", moving };
  }

  const matched = selectBestMatchedSet(usableSets, status, args.previousSetId);
  if (matched) {
    return { set: matched, status: "matched", moving };
  }

  const synthetic = interpolateControlPointSet({
    sets: usableSets,
    fallback: args.fallback,
    ptzStatus: status,
    previousSetId: args.previousSetId,
  });
  if (synthetic) {
    return { set: synthetic.set, status: synthetic.status, moving };
  }

  const hasAnyPose = usableSets.some((set) => hasPoseReference(set.pose_reference));
  return {
    set: args.fallback,
    status: hasAnyPose ? "nearest_reference" : "fallback",
    moving,
  };
}

export function isPoseWithinPresetTolerance(
  pose: CameraPoseReference | null | undefined,
  preset: PtzPreset,
): boolean {
  if (!pose) {
    return false;
  }
  const deltas: number[] = [];
  if (finite(pose.pan) && finite(preset.pan)) {
    deltas.push(Math.abs(pose.pan - preset.pan));
  }
  if (finite(pose.tilt) && finite(preset.tilt)) {
    deltas.push(Math.abs(pose.tilt - preset.tilt));
  }
  if (finite(pose.zoom) && finite(preset.zoom)) {
    deltas.push(Math.abs(pose.zoom - preset.zoom));
  }
  return Boolean(deltas.length) && deltas.every((delta) => delta <= PRESET_POSE_TOLERANCE);
}
