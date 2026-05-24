import type { CompositionElement } from "@toposync/plugin-api";

import type { CameraControlPointSet, CameraLiveView, ProjectionCandidate } from "./types";

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function hasNumberPair(value: unknown, keys: readonly string[]): boolean {
  const rec = readRecord(value);
  return keys.every((key) => typeof rec[key] === "number" && Number.isFinite(rec[key]));
}

export function completeControlPointCount(set: CameraControlPointSet): number {
  return (set.control_points ?? []).filter((point) => hasNumberPair(point.image, ["x", "y"]) && hasNumberPair(point.world, ["x", "z"])).length;
}

export function mappedControlPointSets(element: CompositionElement): CameraControlPointSet[] {
  const props = readRecord(element.props);
  const raw = Array.isArray(props.control_point_sets) ? props.control_point_sets : [];
  return raw
    .map((item, index): CameraControlPointSet | null => {
      const rec = readRecord(item);
      const id = readString(rec.id) || `${element.id}:control-set:${index}`;
      const label = readString(rec.label) || `Vista ${index + 1}`;
      const controlPoints = Array.isArray(rec.control_points) ? rec.control_points : [];
      const set: CameraControlPointSet = {
        id,
        label,
        pose_reference: readRecord(rec.pose_reference),
        control_points: controlPoints as CameraControlPointSet["control_points"],
      };
      return completeControlPointCount(set) >= 4 ? set : null;
    })
    .filter((item): item is CameraControlPointSet => Boolean(item));
}

function liveViewForCamera(liveViews: CameraLiveView[], cameraId: string): CameraLiveView | null {
  return (
    liveViews.find((view) => view.enabled !== false && String(view.camera_id || "").trim() === cameraId) ??
    null
  );
}

export function resolveProjectionCandidates(
  elements: CompositionElement[],
  liveViews: CameraLiveView[],
): ProjectionCandidate[] {
  const out: ProjectionCandidate[] = [];
  for (const element of elements) {
    const props = readRecord(element.props);
    const cameraId = readString(props.camera_id);
    if (!cameraId) continue;

    const sets = mappedControlPointSets(element);
    if (sets.length === 0) continue;

    const liveView = liveViewForCamera(liveViews, cameraId);
    if (!liveView) continue;
    const variants = (liveView.variants ?? []).filter((variant) => variant.enabled !== false);
    if (variants.length === 0) continue;

    const preferredVariant =
      variants.find((variant) => variant.role === "sub") ??
      variants.find((variant) => variant.role === "thumbnail") ??
      variants.find((variant) => variant.role === "main") ??
      variants[0] ??
      null;

    out.push({
      id: `${element.id}:${liveView.id}`,
      cameraId,
      cameraSourceId: preferredVariant?.camera_source_id ?? null,
      liveViewId: liveView.id,
      label: liveView.name || element.name || "Câmera",
      element,
      controlPointSets: sets,
      initialControlPointSet: sets[0],
      variantId: preferredVariant?.id ?? null,
    });
  }
  return out;
}
