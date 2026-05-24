import type { ActiveProjectionPose, CameraControlPointSet, CameraPoseReference, PtzPreset, PtzStatus } from "./types";

const PAN_TILT_SIGMA = 0.04;
const PAN_TILT_MAX_DISTANCE = 3.0;
const ZOOM_MATCH_TOLERANCE = 0.08;
const PRESET_POSE_TOLERANCE = 0.015;
const HYSTERESIS_MARGIN = 0.12;

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function normalizedToken(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function normalizedName(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function presetMatchesStatus(pose: CameraPoseReference, status: PtzStatus): boolean {
  const posePreset = normalizedToken(pose.preset_token);
  const statusPreset = normalizedToken(status.preset_token);
  if (posePreset && statusPreset && posePreset === statusPreset) return true;
  const poseName = normalizedName(pose.preset_name);
  const statusName = normalizedName(status.preset_name);
  return Boolean(poseName && statusName && poseName === statusName);
}

function enrichStatusWithPreset(status: PtzStatus | null, presets: PtzPreset[] | undefined): PtzStatus | null {
  if (!status) return null;
  if (normalizedToken(status.preset_token) || normalizedName(status.preset_name)) return status;

  let best: { preset: PtzPreset; distance: number } | null = null;
  for (const preset of presets ?? []) {
    if (!finite(preset.pan) || !finite(preset.tilt) || !finite(status.pan) || !finite(status.tilt)) continue;
    const distance = Math.hypot(preset.pan - status.pan, preset.tilt - status.tilt);
    if (!best || distance < best.distance) best = { preset, distance };
  }
  if (!best || best.distance > PRESET_POSE_TOLERANCE) return status;
  return {
    ...status,
    preset_token: normalizedToken(best.preset.token) || status.preset_token,
    preset_name: normalizedToken(best.preset.name) || status.preset_name,
  };
}

function poseScore(pose: CameraPoseReference | null | undefined, status: PtzStatus | null): number | null {
  if (!pose || !status) return null;
  if (presetMatchesStatus(pose, status)) return 0;

  let score = 0;
  let weight = 0;
  if (finite(pose.pan) && finite(status.pan)) {
    const delta = Math.abs(pose.pan - status.pan);
    score += (delta / PAN_TILT_SIGMA) ** 2;
    weight += 1;
  }
  if (finite(pose.tilt) && finite(status.tilt)) {
    const delta = Math.abs(pose.tilt - status.tilt);
    score += (delta / PAN_TILT_SIGMA) ** 2;
    weight += 1;
  }
  if (finite(pose.zoom) && finite(status.zoom)) {
    const delta = Math.abs(pose.zoom - status.zoom);
    if (delta > ZOOM_MATCH_TOLERANCE) return null;
    score += delta / ZOOM_MATCH_TOLERANCE;
    weight += 0.7;
  }
  if (weight <= 0) return null;
  const distance = Math.sqrt(score / weight);
  return distance <= PAN_TILT_MAX_DISTANCE ? distance : null;
}

export function resolveActiveProjectionPose(args: {
  sets: CameraControlPointSet[];
  fallback: CameraControlPointSet;
  ptzStatus: PtzStatus | null;
  presets?: PtzPreset[];
  previousSetId?: string | null;
}): ActiveProjectionPose {
  const status = enrichStatusWithPreset(args.ptzStatus, args.presets);
  const moving = String(status?.move_status || "").toLowerCase() === "moving";
  let best: { set: CameraControlPointSet; score: number } | null = null;

  for (const set of args.sets) {
    const score = poseScore(set.pose_reference, status);
    if (score == null) continue;
    if (!best || score < best.score) best = { set, score };
  }

  if (best) {
    const previous = args.sets.find((set) => set.id === args.previousSetId) ?? null;
    const previousScore = previous ? poseScore(previous.pose_reference, status) : null;
    if (previous && previousScore != null && previousScore <= best.score + HYSTERESIS_MARGIN) {
      return { set: previous, status: "matched", moving };
    }
    return { set: best.set, status: "matched", moving };
  }

  const hasAnyPose = args.sets.some((set) => {
    const pose = set.pose_reference;
    return Boolean(
      normalizedToken(pose?.preset_token) ||
        finite(pose?.pan) ||
        finite(pose?.tilt) ||
        finite(pose?.zoom),
    );
  });

  return {
    set: args.fallback,
    status: hasAnyPose && status ? "unmatched" : "fallback",
    moving,
  };
}
