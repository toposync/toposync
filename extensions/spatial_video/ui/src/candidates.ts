import type { CompositionElement, ElementType } from "@toposync/plugin-api";

import { resolveAreaClipForElement } from "./areaClip";
import type { CameraControlPointSet, CameraLiveView, ProjectionCandidate } from "./types";

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  for (const item of value) {
    const normalized = readString(item);
    if (normalized && !out.includes(normalized)) out.push(normalized);
  }
  return out;
}

function hasNumberPair(value: unknown, keys: readonly string[]): boolean {
  const rec = readRecord(value);
  return keys.every((key) => typeof rec[key] === "number" && Number.isFinite(rec[key]));
}

function readRefinementPoints(value: unknown): CameraControlPointSet["refinement_points"] {
  const rec = readRecord(value);
  if (readString(rec.model || "local_rbf_v1") !== "local_rbf_v1") return [];
  const rawPoints = Array.isArray(rec.points) ? rec.points : [];
  const out: NonNullable<CameraControlPointSet["refinement_points"]> = [];
  for (const item of rawPoints.slice(0, 24)) {
    const point = readRecord(item);
    const image = readRecord(point.image);
    const world = readRecord(point.world);
    if (!hasNumberPair(image, ["x", "y"]) || !hasNumberPair(world, ["x", "z"])) continue;
    out.push({
      id: readString(point.id) || `refinement-${out.length + 1}`,
      image: { x: Number(image.x), y: Number(image.y) },
      world: { x: Number(world.x), z: Number(world.z) },
    });
  }
  return out;
}

export function completeControlPointCount(set: CameraControlPointSet): number {
  return (set.control_points ?? []).filter((point) => hasNumberPair(point.image, ["x", "y"]) && hasNumberPair(point.world, ["x", "z"])).length;
}

export function mappedControlPointSets(element: CompositionElement): CameraControlPointSet[] {
  const props = readRecord(element.props);
  const calibratedViews = Array.isArray(props.calibrated_views) ? props.calibrated_views : [];
  const mappedViews = calibratedViews
    .map((item, index): CameraControlPointSet | null => {
      const rec = readRecord(item);
      const id = readString(rec.id) || `${element.id}:calibrated-view:${index}`;
      const label = readString(rec.label) || `Vista ${index + 1}`;
      const projection = readRecord(rec.projection_model);
      const imageRegion = readRecord(projection.image_region);
      const worldQuad = readRecord(projection.world_quad);
      const topLeftImage = readRecord(imageRegion.top_left);
      const bottomRightImage = readRecord(imageRegion.bottom_right);
      const corners = {
        top_left: readRecord(worldQuad.top_left),
        top_right: readRecord(worldQuad.top_right),
        bottom_right: readRecord(worldQuad.bottom_right),
        bottom_left: readRecord(worldQuad.bottom_left),
      };
      if (
        !hasNumberPair(topLeftImage, ["x", "y"]) ||
        !hasNumberPair(bottomRightImage, ["x", "y"]) ||
        !hasNumberPair(corners.top_left, ["x", "z"]) ||
        !hasNumberPair(corners.top_right, ["x", "z"]) ||
        !hasNumberPair(corners.bottom_right, ["x", "z"]) ||
        !hasNumberPair(corners.bottom_left, ["x", "z"])
      ) {
        return null;
      }
      const streamScope = readRecord(rec.stream_scope);
      return {
        id,
        label,
        pose_reference: readRecord(rec.pose_reference),
        stream_scope: {
          compatible_roles: readStringArray(streamScope.compatible_roles),
          compatible_source_ids: readStringArray(streamScope.compatible_source_ids),
        },
        control_points: [
          { id: "top_left", image: topLeftImage as { x: number; y: number }, world: corners.top_left as { x: number; z: number } },
          {
            id: "top_right",
            image: { x: Number(bottomRightImage.x), y: Number(topLeftImage.y) },
            world: corners.top_right as { x: number; z: number },
          },
          { id: "bottom_right", image: bottomRightImage as { x: number; y: number }, world: corners.bottom_right as { x: number; z: number } },
          {
            id: "bottom_left",
            image: { x: Number(topLeftImage.x), y: Number(bottomRightImage.y) },
            world: corners.bottom_left as { x: number; z: number },
          },
        ],
        refinement_points: readRefinementPoints(projection.refinement),
      };
    })
    .filter((item): item is CameraControlPointSet => Boolean(item));
  if (mappedViews.length > 0) return mappedViews;

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
        stream_scope: { compatible_roles: ["main", "sub"], compatible_source_ids: [] },
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
  elementTypesById: Record<string, ElementType>,
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

    const preferredSet = sets[0];
    const compatibleRoles = preferredSet.stream_scope?.compatible_roles?.length
      ? preferredSet.stream_scope.compatible_roles
      : ["main", "sub"];
    const compatibleSourceIds = preferredSet.stream_scope?.compatible_source_ids ?? [];
    const compatibleVariants = variants.filter((variant) => {
      const roleOk = compatibleRoles.includes(String(variant.role || ""));
      const sourceId = String(variant.camera_source_id || "").trim();
      const sourceOk = compatibleSourceIds.length === 0 || compatibleSourceIds.includes(sourceId);
      return roleOk && sourceOk;
    });
    if (compatibleVariants.length === 0) continue;
    const areaClipResult = resolveAreaClipForElement(element, elements, elementTypesById, sets);
    const variantPool = compatibleVariants;
    const preferredVariant =
      variantPool.find((variant) => variant.role === "sub") ??
      variantPool.find((variant) => variant.role === "thumbnail") ??
      variantPool.find((variant) => variant.role === "main") ??
      variantPool.find((variant) => variant.role === "custom") ??
      variantPool[0] ??
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
      areaClip: areaClipResult.clip,
      areaClipWarning: areaClipResult.warning,
    });
  }
  return out;
}
