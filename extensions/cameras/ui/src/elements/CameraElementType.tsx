import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Select, { type SingleValue, type StylesConfig } from "react-select";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import cameraSvg from "@fortawesome/fontawesome-free/svgs/solid/camera.svg";

import type {
  BoundsXZ,
  CompositionElement,
  CompositionElementPatch,
  EditorToolPointerEvent,
  EditorToolSession,
  ElementType,
  HostI18n,
  ToposyncHost,
  Viewport2DContext,
} from "@toposync/plugin-api";

import {
  captureCameraPtzViewAnchor,
  fetchCameraPtzStatus,
  fetchCameraSnapshot,
  fetchCamerasIndex,
  gotoCameraPtzPreset,
  isCameraSnapshotFreshnessUnverifiableError,
  moveCameraPtz,
  moveCameraPtzAbsolute,
  propagateCameraProjection,
  stopCameraPtz,
} from "../api/camerasApi";
import { CAMERA_ELEMENT_TYPE_ID, CONTROL_POINT_COLORS } from "../constants";
import {
  createDefaultCalibratedView,
  createUniqueId,
  readCalibratedViews,
  readRecord,
  readString,
  summarizeCalibratedViewQuality,
} from "../parsing";
import type {
  CameraCalibratedView,
  CameraConnectionType,
  CameraProjectionBoundaryEdge,
  CameraProjectionBoundaryPoint,
  CameraProjectionCornerKey,
  CameraProjectionRefinementPoint,
  CameraProjectionWorldQuad,
  CameraPoseReference,
  CameraSourceConfig,
  CameraSourceRole,
  CameraVisualCalibrationResult,
  CamerasIndex,
  PanTiltZoomState,
} from "../types";
import { SubModal } from "../ui/SubModal";

function roundRectPath(
  canvasContext: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const anyContext = canvasContext as unknown as {
    roundRect?: (x: number, y: number, width: number, height: number, radius: number) => void;
  };
  if (typeof anyContext.roundRect === "function") {
    anyContext.roundRect(x, y, width, height, radius);
    return;
  }

  const clampedRadius = Math.max(0, Math.min(radius, Math.min(width, height) / 2));
  canvasContext.moveTo(x + clampedRadius, y);
  canvasContext.lineTo(x + width - clampedRadius, y);
  canvasContext.quadraticCurveTo(x + width, y, x + width, y + clampedRadius);
  canvasContext.lineTo(x + width, y + height - clampedRadius);
  canvasContext.quadraticCurveTo(x + width, y + height, x + width - clampedRadius, y + height);
  canvasContext.lineTo(x + clampedRadius, y + height);
  canvasContext.quadraticCurveTo(x, y + height, x, y + height - clampedRadius);
  canvasContext.lineTo(x, y + clampedRadius);
  canvasContext.quadraticCurveTo(x, y, x + clampedRadius, y);
}

const PTZ_MOVE_REPEAT_MS = 260;
const PTZ_MOVE_TIMEOUT_S = 0.8;
const PTZ_PAN_SPEED = 0.55;
const PTZ_TILT_SPEED = 0.55;
const PTZ_ZOOM_SPEED = 0.65;

type CameraAreaClipOption = {
  value: string;
  label: string;
  polygon: Array<{ x: number; z: number }>;
};

const cameraAreaSelectStyles: StylesConfig<CameraAreaClipOption, false> = {
  container: (base) => ({ ...base }),
  control: (base, state) => ({
    ...base,
    minHeight: 40,
    borderRadius: "var(--radius-control)",
    border: `1px solid ${state.isFocused ? "var(--color-accent-border)" : "var(--color-border-subtle)"}`,
    backgroundColor: "var(--color-surface-frost)",
    boxShadow: state.isFocused
      ? "0 0 0 2px var(--color-focus-ring-inner), 0 0 0 4px var(--color-focus-ring-outer)"
      : "inset 0 1px 0 color-mix(in srgb, var(--color-surface-solid) 28%, transparent)",
    cursor: state.isDisabled ? "not-allowed" : "text",
    backdropFilter: "blur(var(--frost-blur-small)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    WebkitBackdropFilter: "blur(var(--frost-blur-small)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    transition: "border-color var(--motion-medium) var(--ease-standard), box-shadow var(--motion-medium) var(--ease-standard)",
  }),
  menu: (base) => ({
    ...base,
    backgroundColor: "var(--color-surface-frost-strong)",
    border: "1px solid var(--color-border-subtle)",
    borderRadius: "var(--radius-panel)",
    overflow: "hidden",
    boxShadow: "var(--shadow-elevation-3)",
    backdropFilter: "blur(var(--frost-blur-large)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    WebkitBackdropFilter: "blur(var(--frost-blur-large)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    zIndex: 50,
  }),
  menuList: (base) => ({
    ...base,
    paddingTop: 4,
    paddingBottom: 4,
  }),
  option: (base, state) => ({
    ...base,
    padding: "10px 12px",
    backgroundColor: "transparent",
    background: state.isSelected
      ? "linear-gradient(135deg, var(--color-accent-background-strong), var(--color-accent-background-strong-2))"
      : state.isFocused
        ? "linear-gradient(135deg, var(--color-accent-background-soft), var(--color-accent-background-soft-2))"
        : "transparent",
    color: "var(--color-text-primary)",
    cursor: "pointer",
    borderBottom: "1px solid color-mix(in srgb, var(--color-border-subtle) 35%, transparent)",
  }),
  input: (base) => ({ ...base, color: "var(--color-text-primary)" }),
  placeholder: (base) => ({ ...base, color: "var(--color-text-subtle)" }),
  singleValue: (base) => ({ ...base, color: "var(--color-text-primary)" }),
  indicatorSeparator: (base) => ({ ...base, backgroundColor: "var(--color-border-subtle)" }),
  dropdownIndicator: (base) => ({ ...base, color: "var(--color-text-muted)" }),
  clearIndicator: (base) => ({ ...base, color: "var(--color-text-muted)" }),
};

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function readAreaPolygon(element: CompositionElement): Array<{ x: number; z: number }> {
  const vertices = Array.isArray(element.props?.vertices) ? element.props.vertices : [];
  const out: Array<{ x: number; z: number }> = [];
  for (const item of vertices) {
    const point = readRecord(item);
    if (!finiteNumber(point.x) || !finiteNumber(point.z)) continue;
    out.push({ x: Number(point.x), z: Number(point.z) });
  }
  return out.length >= 3 ? out : [];
}

function polygonSignedArea(points: Array<{ x: number; z: number }>): number {
  let total = 0;
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index];
    const next = points[(index + 1) % points.length];
    total += current.x * next.z - next.x * current.z;
  }
  return total / 2;
}

function pointInPolygon(point: { x: number; z: number }, polygon: Array<{ x: number; z: number }>): boolean {
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

function orientation(a: { x: number; z: number }, b: { x: number; z: number }, c: { x: number; z: number }): number {
  return (b.x - a.x) * (c.z - a.z) - (b.z - a.z) * (c.x - a.x);
}

function pointOnSegment(a: { x: number; z: number }, b: { x: number; z: number }, p: { x: number; z: number }): boolean {
  return (
    Math.abs(orientation(a, b, p)) < 1e-9 &&
    p.x >= Math.min(a.x, b.x) - 1e-9 &&
    p.x <= Math.max(a.x, b.x) + 1e-9 &&
    p.z >= Math.min(a.z, b.z) - 1e-9 &&
    p.z <= Math.max(a.z, b.z) + 1e-9
  );
}

function segmentsIntersect(a: { x: number; z: number }, b: { x: number; z: number }, c: { x: number; z: number }, d: { x: number; z: number }): boolean {
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

function polygonsIntersect(a: Array<{ x: number; z: number }>, b: Array<{ x: number; z: number }>): boolean {
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

function calibratedViewFootprint(view: CameraCalibratedView): Array<{ x: number; z: number }> {
  const quad = view.projection_model.world_quad;
  const points = [quad.top_left, quad.top_right, quad.bottom_right, quad.bottom_left].filter((point) => finiteNumber(point.x) && finiteNumber(point.z));
  return Math.abs(polygonSignedArea(points)) > 1e-8 ? points : [];
}

function readSpatialVideoClipAreaId(props: Record<string, unknown>): string {
  return readString(readRecord(props.spatial_video).clip_area_element_id).trim();
}

function normalizePtzMoveStatus(value: string | null | undefined): "moving" | "idle" | "unknown" {
  const normalized = String(value ?? "")
    .trim()
    .toLowerCase();
  if (!normalized) return "unknown";
  if (normalized.includes("move")) return "moving";
  if (normalized.includes("idle") || normalized.includes("stop")) return "idle";
  return "unknown";
}

function poseHasAbsoluteTarget(poseReference: CameraPoseReference | null | undefined): boolean {
  return (
    (typeof poseReference?.pan === "number" && Number.isFinite(poseReference.pan) && typeof poseReference?.tilt === "number" && Number.isFinite(poseReference.tilt)) ||
    (typeof poseReference?.zoom === "number" && Number.isFinite(poseReference.zoom))
  );
}

function poseHasCompleteAbsoluteTarget(poseReference: CameraPoseReference | null | undefined): boolean {
  return Boolean(
    poseReference &&
      typeof poseReference.pan === "number" &&
      Number.isFinite(poseReference.pan) &&
      typeof poseReference.tilt === "number" &&
      Number.isFinite(poseReference.tilt) &&
      typeof poseReference.zoom === "number" &&
      Number.isFinite(poseReference.zoom),
  );
}

function ptzStatusMatchesPose(
  status: PanTiltZoomState | null,
  poseReference: CameraPoseReference,
  tolerance = 0.03,
): boolean {
  let compared = false;
  const hasPanTiltTarget =
    typeof poseReference.pan === "number" &&
    Number.isFinite(poseReference.pan) &&
    typeof poseReference.tilt === "number" &&
    Number.isFinite(poseReference.tilt);
  if (hasPanTiltTarget) {
    if (
      typeof status?.pan !== "number" ||
      !Number.isFinite(status.pan) ||
      typeof status.tilt !== "number" ||
      !Number.isFinite(status.tilt)
    ) {
      return false;
    }
    compared = true;
    if (
      Math.abs(status.pan - poseReference.pan!) > tolerance ||
      Math.abs(status.tilt - poseReference.tilt!) > tolerance
    ) {
      return false;
    }
  }
  if (typeof poseReference.zoom === "number" && Number.isFinite(poseReference.zoom)) {
    if (typeof status?.zoom !== "number" || !Number.isFinite(status.zoom)) return false;
    compared = true;
    if (Math.abs(status.zoom - poseReference.zoom) > tolerance) return false;
  }
  return compared;
}

function ptzStatusContradictsPose(
  status: PanTiltZoomState | null,
  poseReference: CameraPoseReference,
  tolerance = 0.03,
): boolean {
  const hasPanTiltTarget =
    typeof poseReference.pan === "number" &&
    Number.isFinite(poseReference.pan) &&
    typeof poseReference.tilt === "number" &&
    Number.isFinite(poseReference.tilt);
  if (hasPanTiltTarget) {
    if (
      typeof status?.pan === "number" &&
      Number.isFinite(status.pan) &&
      Math.abs(status.pan - poseReference.pan!) > tolerance
    ) {
      return true;
    }
    if (
      typeof status?.tilt === "number" &&
      Number.isFinite(status.tilt) &&
      Math.abs(status.tilt - poseReference.tilt!) > tolerance
    ) {
      return true;
    }
  }
  if (
    typeof poseReference.zoom === "number" &&
    Number.isFinite(poseReference.zoom) &&
    typeof status?.zoom === "number" &&
    Number.isFinite(status.zoom) &&
    Math.abs(status.zoom - poseReference.zoom) > tolerance
  ) {
    return true;
  }
  return false;
}

function ptzTelemetryIsStable(
  previous: PanTiltZoomState | null,
  current: PanTiltZoomState | null,
  tolerance = 0.002,
): boolean {
  let compared = 0;
  for (const axis of ["pan", "tilt", "zoom"] as const) {
    const previousValue = previous?.[axis];
    const currentValue = current?.[axis];
    if (
      typeof previousValue !== "number" ||
      !Number.isFinite(previousValue) ||
      typeof currentValue !== "number" ||
      !Number.isFinite(currentValue)
    ) {
      continue;
    }
    compared += 1;
    if (Math.abs(currentValue - previousValue) > tolerance) return false;
  }
  return compared > 0;
}

type PtzSettleExpectation = {
  baselineVisualFingerprint?: VisualStabilityFingerprint | null;
  targetPose?: CameraPoseReference | null;
  requireTargetEvidence?: boolean;
  requireVisualTransition?: boolean;
};

type VisualStabilityFingerprint = {
  luminance: Float32Array;
  meanLuminance: number;
};

function ptzSettleTargetIsConfirmed(
  status: PanTiltZoomState | null,
  expectation?: PtzSettleExpectation,
): boolean {
  const targetPose = expectation?.targetPose;
  if (!targetPose) return false;

  const targetPresetToken = String(targetPose.preset_token ?? "").trim();
  const currentPresetToken = String(status?.preset_token ?? "").trim();
  if (targetPresetToken && currentPresetToken === targetPresetToken) return true;

  return poseHasAbsoluteTarget(targetPose) && ptzStatusMatchesPose(status, targetPose);
}

function ptzSettleRequiresVisualConfirmation(expectation?: PtzSettleExpectation): boolean {
  return expectation?.requireTargetEvidence === true;
}

function visualFingerprintDistance(
  left: VisualStabilityFingerprint,
  right: VisualStabilityFingerprint,
): number {
  if (left.luminance.length !== right.luminance.length || !left.luminance.length) return Number.POSITIVE_INFINITY;
  let centeredDifference = 0;
  for (let index = 0; index < left.luminance.length; index += 1) {
    const leftCentered = left.luminance[index] - left.meanLuminance;
    const rightCentered = right.luminance[index] - right.meanLuminance;
    centeredDifference += Math.abs(leftCentered - rightCentered);
  }
  const exposureDifference = Math.abs(left.meanLuminance - right.meanLuminance) * 0.25;
  return centeredDifference / left.luminance.length + exposureDifference;
}

async function visualStabilityFingerprintFromBlob(
  blob: Blob,
  signal: AbortSignal,
): Promise<VisualStabilityFingerprint | null> {
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  if (typeof createImageBitmap !== "function") return null;
  const bitmap = await createImageBitmap(blob);
  try {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    const canvas = document.createElement("canvas");
    canvas.width = 32;
    canvas.height = 18;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return null;
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    const luminance = new Float32Array(canvas.width * canvas.height);
    let sum = 0;
    for (let sourceIndex = 0, targetIndex = 0; sourceIndex < pixels.length; sourceIndex += 4, targetIndex += 1) {
      const value = (pixels[sourceIndex] * 0.2126 + pixels[sourceIndex + 1] * 0.7152 + pixels[sourceIndex + 2] * 0.0722) / 255;
      luminance[targetIndex] = value;
      sum += value;
    }
    return { luminance, meanLuminance: sum / luminance.length };
  } finally {
    bitmap.close();
  }
}

function absoluteMovePayloadForPose(
  sourceId: string,
  poseReference: CameraPoseReference,
): { source_id?: string; pan?: number | null; tilt?: number | null; zoom?: number | null } {
  const hasPanTilt =
    typeof poseReference.pan === "number" &&
    Number.isFinite(poseReference.pan) &&
    typeof poseReference.tilt === "number" &&
    Number.isFinite(poseReference.tilt);
  return {
    ...(sourceId ? { source_id: sourceId } : {}),
    pan: hasPanTilt ? poseReference.pan : null,
    tilt: hasPanTilt ? poseReference.tilt : null,
    zoom: typeof poseReference.zoom === "number" && Number.isFinite(poseReference.zoom) ? poseReference.zoom : null,
  };
}

function formatPtzTelemetryValue(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
}

function cameraBounds(element: CompositionElement): BoundsXZ {
  return {
    minX: element.position.x - 0.42,
    maxX: element.position.x + 0.42,
    minZ: element.position.z - 0.42,
    maxZ: element.position.z + 0.42,
  };
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => window.setTimeout(resolve, ms));
}

type CameraSnapshotSourceOption = Pick<CameraSourceConfig, "id" | "name" | "enabled" | "is_default" | "kind" | "role"> & {
  has_ptz?: boolean;
};

type VisualCalibrationReference = {
  view: CameraCalibratedView;
  image: Blob;
  sourceId: string;
};

type VisualCalibrationRun = {
  status: "idle" | "analyzing" | "proposed" | "accepted" | "rejected" | "error";
  targetViewId: string | null;
  result: CameraVisualCalibrationResult | null;
  proposedView: CameraCalibratedView | null;
  errorMessage: string | null;
};

function emptyVisualCalibrationRun(): VisualCalibrationRun {
  return {
    status: "idle",
    targetViewId: null,
    result: null,
    proposedView: null,
    errorMessage: null,
  };
}

function isVisualCalibrationView(view: CameraCalibratedView): boolean {
  return String(view.projection_quality?.note ?? "")
    .trim()
    .toLowerCase()
    .startsWith("visual calibration;");
}

function invalidateVisualCalibrationApproval(view: CameraCalibratedView): CameraCalibratedView {
  const invalidatedStatus = view.projection_quality?.status === "incomplete" ? "incomplete" : "estimated";
  return {
    ...view,
    projection_model: {
      ...view.projection_model,
      visual_pose_signature: null,
    },
    projection_quality: {
      ...(view.projection_quality ?? {}),
      status: invalidatedStatus,
      estimated: invalidatedStatus === "estimated",
    },
  };
}

const CALIBRATION_SNAPSHOT_ROLE_ORDER: CameraSourceRole[] = ["sub", "main", "custom", "zoom"];

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isTransientSnapshotError(error: unknown): boolean {
  if (isAbortError(error)) return false;
  const message = error instanceof Error ? error.message : String(error);
  return /failed to fetch|networkerror|load failed|ecconnrefused|temporarily unavailable/i.test(message);
}

function calibrationSnapshotErrorMessage(error: unknown, freshnessUnverifiableMessage: string): string {
  if (isCameraSnapshotFreshnessUnverifiableError(error)) return freshnessUnverifiableMessage;
  return error instanceof Error ? error.message : String(error);
}

async function waitForRetry(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", onAbort);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function runPtzMutationWithFence<T>(startMutation: () => Promise<T>): Promise<T> {
  // The mutable request intentionally outlives the UI AbortController. Its backend-owned
  // timeout must settle before a queued Stop can be sent, preserving Goto -> Stop ordering.
  return Promise.resolve().then(startMutation);
}

const pendingCameraPtzStopCompletions = new Map<string, Promise<void>>();

function cameraPtzStopScopeKey(cameraId: string): string {
  return cameraId;
}

function pendingCameraPtzStopCompletion(cameraId: string): Promise<void> | null {
  if (!cameraId) return null;
  return pendingCameraPtzStopCompletions.get(cameraPtzStopScopeKey(cameraId)) ?? null;
}

function trackCameraPtzStopCompletion(
  cameraId: string,
  completion: Promise<void>,
): Promise<void> {
  const key = cameraPtzStopScopeKey(cameraId);
  pendingCameraPtzStopCompletions.set(key, completion);
  void completion.then(
    () => {
      if (pendingCameraPtzStopCompletions.get(key) === completion) {
        pendingCameraPtzStopCompletions.delete(key);
      }
    },
    () => {
      if (pendingCameraPtzStopCompletions.get(key) === completion) {
        pendingCameraPtzStopCompletions.delete(key);
      }
    },
  );
  return completion;
}

async function waitForPendingCameraPtzStops(cameraId: string): Promise<void> {
  let pendingCompletion = pendingCameraPtzStopCompletion(cameraId);
  while (pendingCompletion) {
    await pendingCompletion.catch(() => undefined);
    const latestCompletion = pendingCameraPtzStopCompletion(cameraId);
    if (!latestCompletion || latestCompletion === pendingCompletion) return;
    pendingCompletion = latestCompletion;
  }
}

function enqueueCameraPtzStop(
  cameraId: string,
  sourceId: string,
  mutationPromise: Promise<unknown> | null,
): Promise<void> {
  const previousCompletion = pendingCameraPtzStopCompletion(cameraId);
  const completion = (async () => {
    if (previousCompletion) await previousCompletion.catch(() => undefined);
    if (mutationPromise) await mutationPromise.catch(() => undefined);
    await stopCameraPtz(cameraId, {
      source_id: sourceId,
      pan_tilt: true,
      zoom: true,
    }).catch(() => undefined);
  })();
  return trackCameraPtzStopCompletion(cameraId, completion);
}

async function fetchCameraSnapshotWithRetry(
  cameraId: string,
  sourceId: string,
  signal: AbortSignal,
  attempts = 3,
): Promise<Blob> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      // PTZ causality is established by the required visual transition below;
      // this only drains newly published decoder frames.
      return await fetchCameraSnapshot(cameraId, sourceId, signal, true, "decoder");
    } catch (error) {
      if (isAbortError(error)) throw error;
      lastError = error;
      if (attempt >= attempts - 1 || !isTransientSnapshotError(error)) break;
      await waitForRetry(450 + attempt * 650, signal);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError ?? "Snapshot failed"));
}

async function captureVisualStabilityFingerprint(
  cameraId: string,
  sourceId: string,
  signal: AbortSignal,
): Promise<VisualStabilityFingerprint | null> {
  const snapshot = await fetchCameraSnapshotWithRetry(cameraId, sourceId, signal, 2);
  return visualStabilityFingerprintFromBlob(snapshot, signal);
}

async function capturePtzMovementBaseline(
  cameraId: string,
  ptzSourceId: string,
  snapshotSourceId: string,
  targetPose: CameraPoseReference | null | undefined,
  signal: AbortSignal,
  fingerprintUnavailableMessage: string,
  requireCompletePoseForTargetConfirmation = false,
): Promise<{
  visualFingerprint: VisualStabilityFingerprint;
  targetAlreadyConfirmed: boolean;
}> {
  const [status, visualFingerprint] = await Promise.all([
    fetchCameraPtzStatus(cameraId, ptzSourceId, signal)
      .then((response) => response.status ?? null)
      .catch((error) => {
        if (isAbortError(error) || signal.aborted) throw error;
        return null;
      }),
    captureVisualStabilityFingerprint(cameraId, snapshotSourceId, signal),
  ]);
  if (!visualFingerprint) throw new Error(fingerprintUnavailableMessage);
  return {
    visualFingerprint,
    targetAlreadyConfirmed:
      ptzSettleTargetIsConfirmed(status, { targetPose }) &&
      (!requireCompletePoseForTargetConfirmation || poseHasCompleteAbsoluteTarget(targetPose)),
  };
}

async function waitForCameraVisualStability(
  cameraId: string,
  sourceId: string,
  signal: AbortSignal,
  baseline: VisualStabilityFingerprint | null | undefined,
  requireTransition: boolean,
): Promise<boolean> {
  await waitForRetry(900, signal);
  let previous: VisualStabilityFingerprint | null = null;
  let consecutiveStablePairs = 0;
  let observedTransition = !requireTransition;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const current = await captureVisualStabilityFingerprint(cameraId, sourceId, signal);
    if (current) {
      if (baseline && visualFingerprintDistance(baseline, current) >= 0.055) observedTransition = true;
      if (previous && visualFingerprintDistance(previous, current) <= 0.025) {
        consecutiveStablePairs += 1;
      } else {
        consecutiveStablePairs = 0;
      }
      if (observedTransition && consecutiveStablePairs >= 2) return true;
      previous = current;
    } else {
      consecutiveStablePairs = 0;
    }
    await waitForRetry(650, signal);
  }
  return false;
}

function snapshotSourceDisplayName(source: CameraSnapshotSourceOption | null): string {
  if (!source) return "";
  return source.name || source.id;
}

function resolvePreferredCalibrationSnapshotSourceId(
  view: CameraCalibratedView | null,
  sources: CameraSnapshotSourceOption[],
): string {
  const enabledVideoSources = sources.filter((source) => source.enabled !== false && source.kind === "video");
  if (!view || !enabledVideoSources.length) return "";

  const compatibleSourceIds = (view?.stream_scope?.compatible_source_ids ?? []).map((item) => String(item || "").trim()).filter(Boolean);
  for (const sourceId of compatibleSourceIds) {
    if (enabledVideoSources.some((source) => source.id === sourceId)) return sourceId;
  }
  if (compatibleSourceIds.length > 0) return "";

  const explicitCompatibleRoles = (view?.stream_scope?.compatible_roles ?? [])
    .map((role) => String(role || "").trim())
    .filter(Boolean);
  if (!explicitCompatibleRoles.length) return "";
  const allowedRoles = new Set(explicitCompatibleRoles);
  const roleCandidates = enabledVideoSources.filter((source) => allowedRoles.has(source.role));
  if (!roleCandidates.length) return "";

  const defaultCandidate = roleCandidates.find((source) => source.is_default);
  if (defaultCandidate) return defaultCandidate.id;

  const sorted = [...roleCandidates].sort((left, right) => {
    const leftIndex = CALIBRATION_SNAPSHOT_ROLE_ORDER.indexOf(left.role);
    const rightIndex = CALIBRATION_SNAPSHOT_ROLE_ORDER.indexOf(right.role);
    return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
  });
  return sorted[0]?.id ?? "";
}

function resolvePreferredCalibrationPtzSourceId(
  view: CameraCalibratedView | null,
  sources: CameraSnapshotSourceOption[],
): string {
  const snapshotSourceId = resolvePreferredCalibrationSnapshotSourceId(view, sources);
  if (!snapshotSourceId) return "";
  const snapshotSource = sources.find(
    (source) => source.id === snapshotSourceId && source.enabled !== false && source.kind === "video",
  );
  if (!snapshotSource || snapshotSource.has_ptz === false) return "";
  return snapshotSource.id;
}

export function createCameraElementType(host: ToposyncHost): ElementType {
  const i18n = host.i18n;
  const iconGeometryCache = new Map<string, { geometry: any; scale: number }>();
  const iconTargetSize = 0.14;

  const buttonRadius = 0.18;
  const buttonThetaTopCut = 1.05;
  const ceilingTopMargin = 0.0;

  return {
    type: CAMERA_ELEMENT_TYPE_ID,
    name: { key: "ext.cameras.element.name", fallback: "Camera" },
    description: { key: "ext.cameras.element.desc" },
    placeable: false,
    defaultProps: { camera_id: "", camera_name: "", view_mode: "ceiling" },
    getMain2DBounds: cameraBounds,
    getMain2DMarker: ({ element }) => {
      const props = readRecord(element.props);
      const cameraName = readString(props.camera_name).trim();
      const cameraId = readString(props.camera_id).trim();
      return {
        elementId: element.id,
        x: element.position.x,
        z: element.position.z,
        title: element.name || cameraName || cameraId || i18n.t("ext.cameras.element.name", {}, "Camera"),
        subtitle: cameraName && cameraId && cameraName !== cameraId ? cameraId : "",
        icon: "camera",
        state: "neutral",
        className: "main2dCameraMarker",
      };
    },
    renderMain2DVector: () => null,
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const rotation = typeof element.rotation?.y === "number" ? element.rotation.y : 0;
      const scale = viewport.scale;
      const width = Math.max(14, Math.min(32, 0.28 * scale));
      const height = Math.max(10, Math.min(26, 0.18 * scale));

      canvasContext.save();
      canvasContext.translate(center.x, center.y);
      canvasContext.rotate(rotation);
      canvasContext.fillStyle = "rgba(56,189,248,0.12)";
      canvasContext.strokeStyle = "rgba(230,232,242,0.24)";
      canvasContext.lineWidth = 2;
      canvasContext.beginPath();
      roundRectPath(canvasContext, -width / 2, -height / 2, width, height, Math.min(10, height / 2));
      canvasContext.fill();
      canvasContext.stroke();

      // Direction marker (forward = +Z in 3D, maps to +Y on canvas after rotation).
      canvasContext.fillStyle = "rgba(251,191,36,0.92)";
      canvasContext.beginPath();
      canvasContext.moveTo(0, height / 2 + 6);
      canvasContext.lineTo(-5, height / 2 - 4);
      canvasContext.lineTo(5, height / 2 - 4);
      canvasContext.closePath();
      canvasContext.fill();

      canvasContext.restore();
    },
    hitTest2D: ({ element, world }) => {
      const deltaX = world.x - element.position.x;
      const deltaZ = world.z - element.position.z;
      return deltaX * deltaX + deltaZ * deltaZ <= 0.32 * 0.32;
    },
    translate2D: ({ element, delta }) => ({
      position: { x: element.position.x + delta.x, z: element.position.z + delta.z },
    }),
    create3D: ({ THREE, view }, element) => {
      function getIconGeometry(): { geometry: any; scale: number } {
        const cached = iconGeometryCache.get("camera");
        if (cached) return cached;

        const data = new SVGLoader().parse(cameraSvg);
        const shapes: any[] = [];
        for (const path of data.paths) shapes.push(...SVGLoader.createShapes(path));

        const geometry = new THREE.ShapeGeometry(shapes);
        geometry.computeBoundingBox();
        const boundingBox = geometry.boundingBox;
        if (boundingBox) {
          const centerX = (boundingBox.min.x + boundingBox.max.x) / 2;
          const centerY = (boundingBox.min.y + boundingBox.max.y) / 2;
          geometry.translate(-centerX, -centerY, 0);
        }

        geometry.scale(1, -1, 1);
        geometry.rotateX(-Math.PI / 2);

        geometry.computeBoundingBox();
        const boundingBox3d = geometry.boundingBox;
        const sizeX = boundingBox3d ? boundingBox3d.max.x - boundingBox3d.min.x : 1;
        const sizeZ = boundingBox3d ? boundingBox3d.max.z - boundingBox3d.min.z : 1;
        const maxXZ = Math.max(sizeX, sizeZ, 1e-9);
        const scale = iconTargetSize / maxXZ;

        const entry = { geometry, scale };
        iconGeometryCache.set("camera", entry);
        return entry;
      }

      const neonColor = 0x38bdf8;

      const group = new THREE.Group();
      const mountGroup = new THREE.Group();
      group.add(mountGroup);

      const topY = buttonRadius * Math.cos(buttonThetaTopCut);
      const topRadius = buttonRadius * Math.sin(buttonThetaTopCut);

      const domeCeilingGeometry = new THREE.SphereGeometry(
        buttonRadius,
        56,
        34,
        0,
        Math.PI * 2,
        buttonThetaTopCut,
        Math.PI - buttonThetaTopCut,
      );

      const sphereMaterial = new THREE.MeshStandardMaterial({
        color: 0x0b1220,
        emissive: new THREE.Color(neonColor),
        emissiveIntensity: 0.36,
        roughness: 0.32,
        metalness: 0.0,
      });
      const cutMaterial = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      const iconMaterial = new THREE.MeshBasicMaterial({ color: neonColor, side: THREE.DoubleSide });
      iconMaterial.depthWrite = false;
      iconMaterial.polygonOffset = true;
      iconMaterial.polygonOffsetFactor = -1;
      iconMaterial.polygonOffsetUnits = -1;

      const dome = new THREE.Mesh(domeCeilingGeometry, sphereMaterial);
      mountGroup.add(dome);

      const topCapGeometry = new THREE.CircleGeometry(topRadius, 48);
      const topCap = new THREE.Mesh(topCapGeometry, cutMaterial);
      topCap.rotation.x = -Math.PI / 2;
      topCap.position.set(0, topY, 0);
      mountGroup.add(topCap);

      const topIconGeometry = getIconGeometry();
      const topIcon = new THREE.Mesh(topIconGeometry.geometry, iconMaterial);
      topIcon.scale.setScalar(topIconGeometry.scale);
      topIcon.position.set(0, topY + 0.002, 0);
      topIcon.renderOrder = 10;
      mountGroup.add(topIcon);

      // Dome camera lens "window" on the underside, slightly angled.
      const lensCutMaterial = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      lensCutMaterial.depthWrite = false;
      lensCutMaterial.polygonOffset = true;
      lensCutMaterial.polygonOffsetFactor = -1;
      lensCutMaterial.polygonOffsetUnits = -1;

      const lensRadius = 0.055;
      const lensCutGeometry = new THREE.CircleGeometry(lensRadius, 42);
      const lensCut = new THREE.Mesh(lensCutGeometry, lensCutMaterial);
      lensCut.renderOrder = 9;
      mountGroup.add(lensCut);

      const light = new THREE.PointLight(neonColor, 0.18, 0.9, 2.2);
      light.position.set(0, buttonRadius * 0.45, 0);
      light.castShadow = false;
      light.shadow.mapSize.set(128, 128);
      light.shadow.bias = -0.00035;
      light.shadow.normalBias = 0.02;
      light.shadow.camera.near = 0.05;
      light.shadow.camera.far = 2.0;
      mountGroup.add(light);

      function apply() {
        const wantsShadow = Boolean(view.ghostWalls);
        if (light.castShadow !== wantsShadow) {
          light.castShadow = wantsShadow;
          light.shadow.needsUpdate = true;
        }

        // Ceiling-only for now.
        mountGroup.rotation.set(0, 0, 0);
        mountGroup.position.set(0, 0, 0);

        // Hang from ceiling: top cut flush at wallHeight.
        mountGroup.position.y = view.wallHeight - topY - ceilingTopMargin;

        const lensDirection = new THREE.Vector3(0.12, -0.72, 1).normalize();
        const lensPosition = lensDirection.clone().multiplyScalar(buttonRadius * 0.92);
        lensCut.position.copy(lensPosition);
        lensCut.lookAt(lensPosition.clone().add(lensDirection));
        lensCut.rotateZ(0.55);
        lensCut.position.add(lensDirection.clone().multiplyScalar(0.002));
      }

      apply();

      return {
        object: group,
        update: apply,
        dispose: () => {
          domeCeilingGeometry.dispose();
          topCapGeometry.dispose();
          lensCutGeometry.dispose();
          sphereMaterial.dispose();
          cutMaterial.dispose();
          iconMaterial.dispose();
          lensCutMaterial.dispose();
        },
      };
    },
    renderEditorModal: ({ element, elements, elementTypesById, update, remove, close }) => (
      <CameraEditor
        element={element}
        elements={elements}
        elementTypesById={elementTypesById}
        update={update}
        remove={remove}
        close={close}
        i18n={i18n}
        host={host}
      />
    ),
    renderActionModal: ({ element }) => <CameraAction element={element} i18n={i18n} host={host} />,
  };
}

function CameraEditor({
  element,
  elements,
  elementTypesById,
  update,
  remove,
  close,
  i18n,
  host,
}: {
  element: CompositionElement;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
  host: ToposyncHost;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const selectedCameraId = readString(props.camera_id).trim();
  const existingCalibratedViews = useMemo(() => {
    return readCalibratedViews(props.calibrated_views, element.position);
  }, [element.position, props.calibrated_views]);
  const readySets = useMemo(
    () => existingCalibratedViews.filter((item) => summarizeCalibratedViewQuality(item).status !== "incomplete").length,
    [existingCalibratedViews],
  );
  const readyCalibratedViews = useMemo(
    () => existingCalibratedViews.filter((item) => summarizeCalibratedViewQuality(item).status !== "incomplete"),
    [existingCalibratedViews],
  );
  const totalSets = existingCalibratedViews.length;
  const spatialClipAreaId = readSpatialVideoClipAreaId(props);
  const [isCalibrationOpen, setIsCalibrationOpen] = useState(false);

  const [camerasIndex, setCamerasIndex] = useState<CamerasIndex | null>(null);
  const [indexErrorMessage, setIndexErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIndexErrorMessage(null);
    fetchCamerasIndex()
      .then((data) => {
        if (!cancelled) setCamerasIndex(data);
      })
      .catch((error) => {
        if (!cancelled) setIndexErrorMessage(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const cameraOptions = useMemo(() => {
    const cameras = camerasIndex?.cameras ?? [];
    return cameras
      .map((camera) => {
        const sources = Array.isArray((camera as any).sources)
          ? ((camera as any).sources as any[])
              .map((source) => ({
                id: readString(source?.id).trim(),
                name: readString(source?.name).trim(),
                enabled: source?.enabled !== false,
                is_default: source?.is_default === true,
                kind: (readString(source?.kind).trim() || "video") as CameraSourceConfig["kind"],
                role: (readString(source?.role).trim() || "custom") as CameraSourceRole,
                has_ptz: source?.origin?.has_ptz === true,
              }))
              .filter((source) => Boolean(source.id)) as CameraSnapshotSourceOption[]
          : [];
        return {
          id: readString((camera as any).id),
          name: readString((camera as any).name),
          connectionType: readString((camera as any).control?.type).trim().toLowerCase() as CameraConnectionType | "",
          sources,
        };
      })
      .filter((camera) => Boolean(camera.id));
  }, [camerasIndex]);
  const selectedCamera = useMemo(
    () => cameraOptions.find((camera) => camera.id === selectedCameraId) ?? null,
    [cameraOptions, selectedCameraId],
  );
  const areaClipOptions = useMemo<CameraAreaClipOption[]>(() => {
    if (readyCalibratedViews.length === 0) return [];
    const footprints = readyCalibratedViews.map(calibratedViewFootprint).filter((footprint) => footprint.length >= 3);
    if (footprints.length === 0) return [];
    return elements
      .filter((item) => item.id !== element.id && (elementTypesById[item.type]?.layerGroup ?? "") === "areas")
      .map((item): CameraAreaClipOption | null => {
        const polygon = readAreaPolygon(item);
        if (polygon.length < 3) return null;
        if (!footprints.some((footprint) => polygonsIntersect(footprint, polygon))) return null;
        const typeName = elementTypesById[item.type]?.name;
        const typeFallback = typeof typeName === "string" ? typeName : typeName?.fallback;
        return {
          value: item.id,
          label: item.name || typeFallback || item.id,
          polygon,
        };
      })
      .filter((item): item is CameraAreaClipOption => Boolean(item));
  }, [element.id, elementTypesById, elements, readyCalibratedViews]);
  const selectedAreaClipOption = areaClipOptions.find((option) => option.value === spatialClipAreaId) ?? null;
  const areaClipDisabled = !selectedCameraId || readyCalibratedViews.length === 0 || areaClipOptions.length === 0;
  const areaClipInvalid = Boolean(spatialClipAreaId && !selectedAreaClipOption);

  function updateSpatialVideoClipArea(nextAreaId: string) {
    const existingSpatialVideo = readRecord(props.spatial_video);
    update({
      props: {
        spatial_video: {
          ...existingSpatialVideo,
          clip_area_element_id: nextAreaId,
        },
      },
    });
  }

  return (
    <div>
      {indexErrorMessage ? (
        <div className="card">
          <div className="cardBody">{indexErrorMessage}</div>
        </div>
      ) : null}

      {cameraOptions.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.cameras.editor.no_cameras")}</div>
        </div>
      ) : (
        <div className="field">
          <label className="label">{t("ext.cameras.editor.camera")}</label>
          <select
            className="input"
            value={selectedCameraId}
            onChange={(event) => {
              const nextCameraId = event.target.value;
              const selected = cameraOptions.find((camera) => camera.id === nextCameraId) ?? null;
              update({
                name: selected?.name ?? "",
                props: { camera_id: nextCameraId, camera_name: selected?.name ?? "" },
              });
            }}
          >
            <option value="">{t("ext.cameras.editor.select_placeholder")}</option>
            {cameraOptions.map((camera) => (
              <option key={camera.id} value={camera.id}>
                {camera.name || camera.id}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="field">
        <label className="label">{t("ext.cameras.editor.calibration")}</label>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="cardMeta">
            {totalSets > 0
              ? t("ext.cameras.editor.control_sets_some", { ready: readySets, total: totalSets })
              : t("ext.cameras.editor.calibration_none")}
          </div>

          <button
            className="chipButton"
            type="button"
            disabled={!selectedCameraId}
            onClick={() => setIsCalibrationOpen(true)}
          >
            {t("ext.cameras.editor.calibration_open")}
          </button>
        </div>
        {totalSets > 0 && readySets === 0 ? (
          <div className="cardMeta" style={{ marginTop: 6 }}>
            {t("ext.cameras.editor.calibration_hint")}
          </div>
        ) : null}
      </div>

      <div className="field">
        <label className="label">{t("ext.cameras.editor.spatial_clip")}</label>
        <Select<CameraAreaClipOption, false>
          styles={cameraAreaSelectStyles}
          value={selectedAreaClipOption}
          options={areaClipOptions}
          isClearable
          isDisabled={areaClipDisabled}
          placeholder={t("ext.cameras.editor.spatial_clip_placeholder")}
          onChange={(option: SingleValue<CameraAreaClipOption>) => updateSpatialVideoClipArea(option?.value ?? "")}
        />
        <div className="cardMeta" style={{ marginTop: 6 }}>
          {readyCalibratedViews.length === 0
            ? t("ext.cameras.editor.spatial_clip_disabled_no_calibration")
            : areaClipOptions.length === 0
              ? t("ext.cameras.editor.spatial_clip_none")
              : t("ext.cameras.editor.spatial_clip_hint")}
        </div>
        {areaClipInvalid ? (
          <div className="cardMeta" style={{ marginTop: 4, color: "rgb(251,191,36)" }}>
            {t("ext.cameras.editor.spatial_clip_invalid")}
          </div>
        ) : null}
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <button
          className="dangerButton"
          type="button"
          onClick={() => {
            remove();
            close();
          }}
        >
          {t("core.actions.delete")}
        </button>

        <button className="primaryButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>

      <CameraCalibrationModal
        open={isCalibrationOpen}
        onClose={() => setIsCalibrationOpen(false)}
        host={host}
        i18n={i18n}
        element={element}
        cameraId={selectedCameraId}
        cameraConnectionType={selectedCamera?.connectionType || null}
        cameraSources={selectedCamera?.sources ?? []}
        initialViews={existingCalibratedViews}
        onSave={(calibratedViews) => update({ props: { calibrated_views: calibratedViews, control_point_sets: undefined } })}
      />
    </div>
  );
}

const CALIBRATION_CORNERS: CameraProjectionCornerKey[] = ["top_left", "top_right", "bottom_right", "bottom_left"];
const CALIBRATION_BOUNDARY_EDGES: CameraProjectionBoundaryEdge[] = ["top", "right", "bottom", "left"];
const MAX_CALIBRATION_REFINEMENT_POINTS = 24;
const MAX_CALIBRATION_BOUNDARY_POINTS = 32;
const MAX_CALIBRATION_BOUNDARY_POINTS_PER_EDGE = 8;
const CALIBRATION_PREVIEW_GRID_DIVISIONS = 34;
const LOCAL_REFINEMENT_SIGMA_UV = 0.22;
const LOCAL_REFINEMENT_EDGE_LOW = 0.015;
const LOCAL_REFINEMENT_EDGE_HIGH = 0.12;
const BOUNDARY_REFINEMENT_FALLOFF_UV = 0.48;

type CalibrationDragState =
  | {
      kind: "move";
      startWorld: { x: number; z: number };
      startQuad: CameraProjectionWorldQuad;
      startRefinementPoints: CameraProjectionRefinementPoint[];
      startBoundaryPoints: CameraProjectionBoundaryPoint[];
    }
  | {
      kind: "corner";
      corner: CameraProjectionCornerKey;
      startQuad: CameraProjectionWorldQuad;
    }
  | {
      kind: "rotate";
      centerWorld: { x: number; z: number };
      startAngle: number;
      startQuad: CameraProjectionWorldQuad;
      startRefinementPoints: CameraProjectionRefinementPoint[];
      startBoundaryPoints: CameraProjectionBoundaryPoint[];
      snappedDelta: number;
    }
  | {
      kind: "refinement";
      pointId: string;
      pointerStartWorld: { x: number; z: number };
      created: boolean;
      moved: boolean;
    }
  | {
      kind: "boundary";
      pointId: string;
      pointerStartWorld: { x: number; z: number };
      created: boolean;
      moved: boolean;
    };

type CalibrationHoverState =
  | { kind: "move" }
  | { kind: "corner"; corner: CameraProjectionCornerKey }
  | { kind: "rotate" }
  | { kind: "refinement"; pointId: string }
  | { kind: "new_refinement"; image: { x: number; y: number }; world: { x: number; z: number } }
  | { kind: "boundary"; pointId: string }
  | { kind: "new_boundary"; edge: CameraProjectionBoundaryEdge; t: number; image: { x: number; y: number }; world: { x: number; z: number } }
  | null;

type CalibrationRotateHandleInfo = {
  centerWorld: { x: number; z: number };
  centerScreen: { x: number; y: number };
  handleScreen: { x: number; y: number };
  radiusPx: number;
  hitRadiusPx: number;
};

function cloneWorldQuad(quad: CameraProjectionWorldQuad): CameraProjectionWorldQuad {
  return {
    top_left: { ...quad.top_left },
    top_right: { ...quad.top_right },
    bottom_right: { ...quad.bottom_right },
    bottom_left: { ...quad.bottom_left },
  };
}

function worldQuadPoints(quad: CameraProjectionWorldQuad): Array<{ x: number; z: number }> {
  return CALIBRATION_CORNERS.map((corner) => quad[corner]);
}

function translateWorldQuad(quad: CameraProjectionWorldQuad, delta: { x: number; z: number }): CameraProjectionWorldQuad {
  return {
    top_left: { x: quad.top_left.x + delta.x, z: quad.top_left.z + delta.z },
    top_right: { x: quad.top_right.x + delta.x, z: quad.top_right.z + delta.z },
    bottom_right: { x: quad.bottom_right.x + delta.x, z: quad.bottom_right.z + delta.z },
    bottom_left: { x: quad.bottom_left.x + delta.x, z: quad.bottom_left.z + delta.z },
  };
}

function quadCenter(quad: CameraProjectionWorldQuad): { x: number; z: number } {
  const points = worldQuadPoints(quad);
  return {
    x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
    z: points.reduce((sum, point) => sum + point.z, 0) / points.length,
  };
}

function rotateWorldQuad(quad: CameraProjectionWorldQuad, radians: number): CameraProjectionWorldQuad {
  const center = quadCenter(quad);
  const sin = Math.sin(radians);
  const cos = Math.cos(radians);
  const next = cloneWorldQuad(quad);
  for (const corner of CALIBRATION_CORNERS) {
    const dx = quad[corner].x - center.x;
    const dz = quad[corner].z - center.z;
    next[corner] = {
      x: center.x + dx * cos - dz * sin,
      z: center.z + dx * sin + dz * cos,
    };
  }
  return next;
}

function normalizeAngleRad(angle: number): number {
  return Math.atan2(Math.sin(angle), Math.cos(angle));
}

function calibrationRotationDelta(rawDelta: number, event: EditorToolPointerEvent): number {
  const stepDegrees = event.shiftKey ? 5 : 15;
  const stepRadians = (stepDegrees * Math.PI) / 180;
  return event.altKey ? rawDelta : Math.round(rawDelta / stepRadians) * stepRadians;
}

function screenDistanceSquared(a: { x: number; y: number }, b: { x: number; y: number }): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return dx * dx + dy * dy;
}

function refinementPointsForView(view: CameraCalibratedView): CameraProjectionRefinementPoint[] {
  return view.projection_model.refinement?.points ?? [];
}

function boundaryPointsForView(view: CameraCalibratedView): CameraProjectionBoundaryPoint[] {
  return view.projection_model.boundary_refinement?.points ?? [];
}

function cloneRefinementPoints(points: CameraProjectionRefinementPoint[]): CameraProjectionRefinementPoint[] {
  return points.map((point) => ({
    id: point.id,
    image: { ...point.image },
    world: { ...point.world },
  }));
}

function boundaryImageForEdge(edge: CameraProjectionBoundaryEdge, t: number): { x: number; y: number } {
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

function cloneBoundaryPoints(points: CameraProjectionBoundaryPoint[]): CameraProjectionBoundaryPoint[] {
  return points.map((point) => ({
    id: point.id,
    edge: point.edge,
    t: point.t,
    image: { ...point.image },
    world: { ...point.world },
  }));
}

function cloneCalibratedView(view: CameraCalibratedView): CameraCalibratedView {
  return {
    ...view,
    pose_reference: view.pose_reference ? { ...view.pose_reference } : null,
    stream_scope: {
      compatible_roles: [...(view.stream_scope?.compatible_roles ?? [])],
      compatible_source_ids: [...(view.stream_scope?.compatible_source_ids ?? [])],
    },
    projection_model: {
      ...view.projection_model,
      visual_pose_signature: view.projection_model.visual_pose_signature
        ? { ...view.projection_model.visual_pose_signature }
        : null,
      image_region: {
        top_left: { ...view.projection_model.image_region.top_left },
        bottom_right: { ...view.projection_model.image_region.bottom_right },
      },
      world_quad: cloneWorldQuad(view.projection_model.world_quad),
      refinement: view.projection_model.refinement?.points.length
        ? { model: "local_rbf_v1", points: cloneRefinementPoints(view.projection_model.refinement.points) }
        : null,
      boundary_refinement: view.projection_model.boundary_refinement?.points.length
        ? { model: "edge_handles_v1", points: cloneBoundaryPoints(view.projection_model.boundary_refinement.points) }
        : null,
    },
    projection_quality: { ...(view.projection_quality ?? {}) },
  };
}

function normalizeBoundaryPoints(points: CameraProjectionBoundaryPoint[]): CameraProjectionBoundaryPoint[] {
  const perEdge = new Map<CameraProjectionBoundaryEdge, number>();
  const normalized: CameraProjectionBoundaryPoint[] = [];
  for (const point of points) {
    if (!CALIBRATION_BOUNDARY_EDGES.includes(point.edge)) continue;
    if (
      !Number.isFinite(point.t) ||
      point.t < 0 ||
      point.t > 1 ||
      !Number.isFinite(point.world.x) ||
      !Number.isFinite(point.world.z)
    ) {
      continue;
    }
    const edgeCount = perEdge.get(point.edge) ?? 0;
    if (edgeCount >= MAX_CALIBRATION_BOUNDARY_POINTS_PER_EDGE || normalized.length >= MAX_CALIBRATION_BOUNDARY_POINTS) continue;
    perEdge.set(point.edge, edgeCount + 1);
    normalized.push({
      id: point.id || createUniqueId(),
      edge: point.edge,
      t: point.t,
      image: boundaryImageForEdge(point.edge, point.t),
      world: { x: point.world.x, z: point.world.z },
    });
  }
  return normalized;
}

function withRefinementPoints(
  view: CameraCalibratedView,
  points: CameraProjectionRefinementPoint[],
  status: "ready" | "estimated" = "ready",
): CameraCalibratedView {
  const normalized = points
    .filter(
      (point) =>
        Number.isFinite(point.image.x) &&
        Number.isFinite(point.image.y) &&
        Number.isFinite(point.world.x) &&
        Number.isFinite(point.world.z) &&
        point.image.x >= 0 &&
        point.image.x <= 1 &&
        point.image.y >= 0 &&
        point.image.y <= 1,
    )
    .slice(0, MAX_CALIBRATION_REFINEMENT_POINTS)
    .map((point) => ({
      id: point.id || createUniqueId(),
      image: { x: point.image.x, y: point.image.y },
      world: { x: point.world.x, z: point.world.z },
    }));
  return {
    ...view,
    projection_model: {
      ...view.projection_model,
      refinement: normalized.length > 0 ? { model: "local_rbf_v1", points: normalized } : null,
    },
    projection_quality: {
      ...(view.projection_quality ?? {}),
      status,
      estimated: status === "estimated",
    },
  };
}

function withBoundaryPoints(
  view: CameraCalibratedView,
  points: CameraProjectionBoundaryPoint[],
  status: "ready" | "estimated" = "ready",
): CameraCalibratedView {
  const normalized = normalizeBoundaryPoints(points);
  return {
    ...view,
    projection_model: {
      ...view.projection_model,
      boundary_refinement: normalized.length > 0 ? { model: "edge_handles_v1", points: normalized } : null,
    },
    projection_quality: {
      ...(view.projection_quality ?? {}),
      status,
      estimated: status === "estimated",
    },
  };
}

function withRefinementAndBoundaryPoints(
  view: CameraCalibratedView,
  refinementPoints: CameraProjectionRefinementPoint[],
  boundaryPoints: CameraProjectionBoundaryPoint[],
  status: "ready" | "estimated" = "ready",
): CameraCalibratedView {
  return withBoundaryPoints(withRefinementPoints(view, refinementPoints, status), boundaryPoints, status);
}

function translateRefinementPoints(
  points: CameraProjectionRefinementPoint[],
  delta: { x: number; z: number },
): CameraProjectionRefinementPoint[] {
  return points.map((point) => ({
    ...point,
    image: { ...point.image },
    world: {
      x: point.world.x + delta.x,
      z: point.world.z + delta.z,
    },
  }));
}

function translateBoundaryPoints(
  points: CameraProjectionBoundaryPoint[],
  delta: { x: number; z: number },
): CameraProjectionBoundaryPoint[] {
  return points.map((point) => ({
    ...point,
    image: { ...point.image },
    world: {
      x: point.world.x + delta.x,
      z: point.world.z + delta.z,
    },
  }));
}

function rotateWorldPoint(point: { x: number; z: number }, center: { x: number; z: number }, radians: number): { x: number; z: number } {
  const sin = Math.sin(radians);
  const cos = Math.cos(radians);
  const dx = point.x - center.x;
  const dz = point.z - center.z;
  return {
    x: center.x + dx * cos - dz * sin,
    z: center.z + dx * sin + dz * cos,
  };
}

function rotateRefinementPoints(
  points: CameraProjectionRefinementPoint[],
  center: { x: number; z: number },
  radians: number,
): CameraProjectionRefinementPoint[] {
  return points.map((point) => ({
    ...point,
    image: { ...point.image },
    world: rotateWorldPoint(point.world, center, radians),
  }));
}

function rotateBoundaryPoints(
  points: CameraProjectionBoundaryPoint[],
  center: { x: number; z: number },
  radians: number,
): CameraProjectionBoundaryPoint[] {
  return points.map((point) => ({
    ...point,
    image: { ...point.image },
    world: rotateWorldPoint(point.world, center, radians),
  }));
}

function calibrationRotateHandleInfo(
  quad: CameraProjectionWorldQuad,
  viewport: Viewport2DContext,
): CalibrationRotateHandleInfo {
  const centerWorld = quadCenter(quad);
  const centerScreen = viewport.worldToScreen(centerWorld);
  const points = worldQuadPoints(quad).map((point) => viewport.worldToScreen(point));
  const minX = Math.min(...points.map((point) => point.x));
  const maxX = Math.max(...points.map((point) => point.x));
  const minY = Math.min(...points.map((point) => point.y));
  const maxY = Math.max(...points.map((point) => point.y));
  const extent = Math.max(maxX - minX, maxY - minY, 36);
  const radiusPx = Math.max(34, Math.min(92, extent / 2 + 34));
  const topMid = {
    x: (points[0].x + points[1].x) / 2,
    y: (points[0].y + points[1].y) / 2,
  };
  let dx = topMid.x - centerScreen.x;
  let dy = topMid.y - centerScreen.y;
  const length = Math.hypot(dx, dy);
  if (length < 1e-6) {
    dx = 0;
    dy = -1;
  } else {
    dx /= length;
    dy /= length;
  }
  return {
    centerWorld,
    centerScreen,
    handleScreen: {
      x: centerScreen.x + dx * radiusPx,
      y: centerScreen.y + dy * radiusPx,
    },
    radiusPx,
    hitRadiusPx: 13,
  };
}

function nearestWorldQuadCornerByScreen(
  screen: { x: number; y: number },
  quad: CameraProjectionWorldQuad,
  viewport: Viewport2DContext,
  thresholdPx: number,
): CameraProjectionCornerKey | null {
  let best: { corner: CameraProjectionCornerKey; distanceSquared: number } | null = null;
  for (const corner of CALIBRATION_CORNERS) {
    const point = viewport.worldToScreen(quad[corner]);
    const distanceSquared = screenDistanceSquared(screen, point);
    if (distanceSquared > thresholdPx * thresholdPx) continue;
    if (!best || distanceSquared < best.distanceSquared) best = { corner, distanceSquared };
  }
  return best?.corner ?? null;
}

function nearestWorldQuadCorner(
  point: { x: number; z: number },
  quad: CameraProjectionWorldQuad,
  thresholdWorld: number,
): CameraProjectionCornerKey | null {
  let best: { corner: CameraProjectionCornerKey; distance: number } | null = null;
  for (const corner of CALIBRATION_CORNERS) {
    const candidate = quad[corner];
    const distance = Math.hypot(candidate.x - point.x, candidate.z - point.z);
    if (distance > thresholdWorld) continue;
    if (!best || distance < best.distance) best = { corner, distance };
  }
  return best?.corner ?? null;
}

function sourceRegionPixels(view: CameraCalibratedView, image: HTMLImageElement): {
  topLeft: { x: number; y: number };
  topRight: { x: number; y: number };
  bottomRight: { x: number; y: number };
  bottomLeft: { x: number; y: number };
} {
  const region = view.projection_model.image_region;
  const width = Math.max(1, image.naturalWidth || image.width || 1);
  const height = Math.max(1, image.naturalHeight || image.height || 1);
  const left = region.top_left.x * width;
  const top = region.top_left.y * height;
  const right = region.bottom_right.x * width;
  const bottom = region.bottom_right.y * height;
  return {
    topLeft: { x: left, y: top },
    topRight: { x: right, y: top },
    bottomRight: { x: right, y: bottom },
    bottomLeft: { x: left, y: bottom },
  };
}

function drawImageTriangle(
  ctx: CanvasRenderingContext2D,
  image: HTMLImageElement,
  source: [{ x: number; y: number }, { x: number; y: number }, { x: number; y: number }],
  dest: [{ x: number; y: number }, { x: number; y: number }, { x: number; y: number }],
): void {
  const [s0, s1, s2] = source;
  const [d0, d1, d2] = dest;
  const denominator = s0.x * (s1.y - s2.y) + s1.x * (s2.y - s0.y) + s2.x * (s0.y - s1.y);
  if (Math.abs(denominator) < 1e-8) return;
  const a = (d0.x * (s1.y - s2.y) + d1.x * (s2.y - s0.y) + d2.x * (s0.y - s1.y)) / denominator;
  const b = (d0.y * (s1.y - s2.y) + d1.y * (s2.y - s0.y) + d2.y * (s0.y - s1.y)) / denominator;
  const c = (d0.x * (s2.x - s1.x) + d1.x * (s0.x - s2.x) + d2.x * (s1.x - s0.x)) / denominator;
  const d = (d0.y * (s2.x - s1.x) + d1.y * (s0.x - s2.x) + d2.y * (s1.x - s0.x)) / denominator;
  const e =
    (d0.x * (s1.x * s2.y - s2.x * s1.y) +
      d1.x * (s2.x * s0.y - s0.x * s2.y) +
      d2.x * (s0.x * s1.y - s1.x * s0.y)) /
    denominator;
  const f =
    (d0.y * (s1.x * s2.y - s2.x * s1.y) +
      d1.y * (s2.x * s0.y - s0.x * s2.y) +
      d2.y * (s0.x * s1.y - s1.x * s0.y)) /
    denominator;

  ctx.save();
  ctx.beginPath();
  ctx.moveTo(d0.x, d0.y);
  ctx.lineTo(d1.x, d1.y);
  ctx.lineTo(d2.x, d2.y);
  ctx.closePath();
  ctx.clip();
  ctx.transform(a, b, c, d, e, f);
  ctx.drawImage(image, 0, 0);
  ctx.restore();
}

type CalibrationProjectionPair = {
  image: { x: number; y: number };
  world: { x: number; z: number };
};

type CalibrationMeshVertex = {
  image: { x: number; y: number };
  world: { x: number; z: number };
};

type CalibrationMeshData = {
  vertices: CalibrationMeshVertex[];
  indices: number[];
};

function calibrationPairsFromView(view: CameraCalibratedView): CalibrationProjectionPair[] {
  const region = view.projection_model.image_region;
  const quad = view.projection_model.world_quad;
  return [
    { image: region.top_left, world: quad.top_left },
    { image: { x: region.bottom_right.x, y: region.top_left.y }, world: quad.top_right },
    { image: region.bottom_right, world: quad.bottom_right },
    { image: { x: region.top_left.x, y: region.bottom_right.y }, world: quad.bottom_left },
  ];
}

function solveCalibrationLinearSystem(matrix: number[][], rhs: number[]): number[] | null {
  const n = rhs.length;
  const a = matrix.map((row, index) => [...row, rhs[index]]);
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

function solveCalibrationHomography(src: Array<{ x: number; y: number }>, dst: Array<{ x: number; y: number }>): number[] | null {
  if (src.length < 4 || dst.length < 4 || src.length !== dst.length) return null;
  const ata = Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 0));
  const atb = Array.from({ length: 8 }, () => 0);
  const addRow = (row: number[], target: number) => {
    for (let i = 0; i < 8; i += 1) {
      atb[i] += row[i] * target;
      for (let j = 0; j < 8; j += 1) ata[i][j] += row[i] * row[j];
    }
  };
  for (let index = 0; index < src.length; index += 1) {
    const a = src[index].x;
    const b = src[index].y;
    const x = dst[index].x;
    const y = dst[index].y;
    addRow([a, b, 1, 0, 0, 0, -x * a, -x * b], x);
    addRow([0, 0, 0, a, b, 1, -y * a, -y * b], y);
  }
  const solved = solveCalibrationLinearSystem(ata, atb);
  return solved ? [...solved, 1] : null;
}

function mapCalibrationHomography(h: number[], point: { x: number; y: number }): { x: number; y: number } | null {
  const denominator = h[6] * point.x + h[7] * point.y + h[8];
  if (!Number.isFinite(denominator) || Math.abs(denominator) < 1e-8) return null;
  const x = (h[0] * point.x + h[1] * point.y + h[2]) / denominator;
  const y = (h[3] * point.x + h[4] * point.y + h[5]) / denominator;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y };
}

function estimateCalibrationImageToWorld(view: CameraCalibratedView): number[] | null {
  const pairs = calibrationPairsFromView(view);
  return solveCalibrationHomography(
    pairs.map((pair) => pair.image),
    pairs.map((pair) => ({ x: pair.world.x, y: pair.world.z })),
  );
}

function smoothstep(edge0: number, edge1: number, value: number): number {
  if (edge0 === edge1) return value >= edge1 ? 1 : 0;
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
}

function localRefinementDelta(view: CameraCalibratedView, hImageToWorld: number[], image: { x: number; y: number }): { x: number; z: number } {
  const refinementPoints = refinementPointsForView(view);
  if (refinementPoints.length === 0) return { x: 0, z: 0 };
  let totalWeight = 0;
  let totalX = 0;
  let totalZ = 0;
  for (const point of refinementPoints) {
    const base = mapCalibrationHomography(hImageToWorld, point.image);
    if (!base) continue;
    const delta = {
      x: point.world.x - base.x,
      z: point.world.z - base.y,
    };
    const distance = Math.hypot(image.x - point.image.x, image.y - point.image.y);
    if (distance <= 1e-9) return delta;
    const weight = Math.exp(-((distance / LOCAL_REFINEMENT_SIGMA_UV) ** 2));
    if (weight <= 1e-12) continue;
    totalWeight += weight;
    totalX += weight * delta.x;
    totalZ += weight * delta.z;
  }
  if (totalWeight <= 1e-12) return { x: 0, z: 0 };
  const edgeDistance = Math.min(image.x, image.y, 1 - image.x, 1 - image.y);
  const edge = smoothstep(LOCAL_REFINEMENT_EDGE_LOW, LOCAL_REFINEMENT_EDGE_HIGH, edgeDistance);
  return { x: edge * (totalX / totalWeight), z: edge * (totalZ / totalWeight) };
}

function boundaryAxis(edge: CameraProjectionBoundaryEdge, image: { x: number; y: number }): number {
  if (edge === "top" || edge === "right") return edge === "top" ? image.x : image.y;
  return edge === "bottom" ? 1 - image.x : 1 - image.y;
}

function boundaryDistance(edge: CameraProjectionBoundaryEdge, image: { x: number; y: number }): number {
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

function boundaryInfluence(edge: CameraProjectionBoundaryEdge, image: { x: number; y: number }): number {
  const distance = boundaryDistance(edge, image);
  if (distance <= 1e-9) return 1;
  const normalized = clamp(distance / BOUNDARY_REFINEMENT_FALLOFF_UV, 0, 1);
  const eased = normalized * normalized * (3 - 2 * normalized);
  return 1 - eased;
}

function boundaryDeltaAtEdge(
  points: Array<CameraProjectionBoundaryPoint & { delta: { x: number; z: number } }>,
  edge: CameraProjectionBoundaryEdge,
  t: number,
): { x: number; z: number } {
  const edgePoints = points
    .filter((point) => point.edge === edge)
    .sort((a, b) => a.t - b.t);
  if (edgePoints.length === 0) return { x: 0, z: 0 };
  const anchors = [
    { t: 0, delta: { x: 0, z: 0 } },
    ...edgePoints.map((point) => ({ t: point.t, delta: point.delta })),
    { t: 1, delta: { x: 0, z: 0 } },
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
      z: left.delta.z + (right.delta.z - left.delta.z) * local,
    };
  }
  return anchors[anchors.length - 1].delta;
}

function boundaryRefinementDelta(view: CameraCalibratedView, hImageToWorld: number[], image: { x: number; y: number }): { x: number; z: number } {
  const sourcePoints = boundaryPointsForView(view);
  if (sourcePoints.length === 0) return { x: 0, z: 0 };
  const displacements: Array<CameraProjectionBoundaryPoint & { delta: { x: number; z: number } }> = [];
  for (const point of sourcePoints) {
    const boundaryImage = boundaryImageForEdge(point.edge, point.t);
    const base = mapCalibrationHomography(hImageToWorld, boundaryImage);
    if (!base) continue;
    displacements.push({
      ...point,
      image: boundaryImage,
      delta: { x: point.world.x - base.x, z: point.world.z - base.y },
    });
  }
  if (displacements.length === 0) return { x: 0, z: 0 };
  let totalX = 0;
  let totalZ = 0;
  for (const edge of CALIBRATION_BOUNDARY_EDGES) {
    const t = boundaryAxis(edge, image);
    const delta = boundaryDeltaAtEdge(displacements, edge, t);
    const influence = boundaryInfluence(edge, image);
    totalX += delta.x * influence;
    totalZ += delta.z * influence;
  }
  return { x: totalX, z: totalZ };
}

function mapCalibrationImageToWorld(
  view: CameraCalibratedView,
  hImageToWorld: number[],
  image: { x: number; y: number },
): { x: number; z: number } | null {
  const base = mapCalibrationHomography(hImageToWorld, image);
  if (!base) return null;
  const boundaryDelta = boundaryRefinementDelta(view, hImageToWorld, image);
  const refinementDelta = localRefinementDelta(view, hImageToWorld, image);
  return { x: base.x + boundaryDelta.x + refinementDelta.x, z: base.y + boundaryDelta.z + refinementDelta.z };
}

function calibrationGridCoordinates(view: CameraCalibratedView, divisions: number): { xs: number[]; ys: number[] } {
  const xs = new Set<number>();
  const ys = new Set<number>();
  for (let index = 0; index <= divisions; index += 1) {
    xs.add(index / divisions);
    ys.add(index / divisions);
  }
  for (const point of boundaryPointsForView(view)) {
    const image = boundaryImageForEdge(point.edge, point.t);
    xs.add(Number(image.x.toFixed(8)));
    ys.add(Number(image.y.toFixed(8)));
  }
  return {
    xs: [...xs].sort((a, b) => a - b),
    ys: [...ys].sort((a, b) => a - b),
  };
}

function triangleSignedArea(a: { x: number; z: number }, b: { x: number; z: number }, c: { x: number; z: number }): number {
  return ((b.x - a.x) * (c.z - a.z) - (b.z - a.z) * (c.x - a.x)) / 2;
}

function calibrationMeshHasFoldover(mesh: CalibrationMeshData): boolean {
  let sign = 0;
  for (let index = 0; index < mesh.indices.length; index += 3) {
    const a = mesh.vertices[mesh.indices[index]];
    const b = mesh.vertices[mesh.indices[index + 1]];
    const c = mesh.vertices[mesh.indices[index + 2]];
    if (!a || !b || !c) continue;
    const area = triangleSignedArea(a.world, b.world, c.world);
    if (!Number.isFinite(area) || Math.abs(area) <= 1e-9) return true;
    const currentSign = Math.sign(area);
    if (sign === 0) sign = currentSign;
    else if (currentSign !== sign) return true;
  }
  return false;
}

function buildCalibrationMesh(view: CameraCalibratedView, divisions = CALIBRATION_PREVIEW_GRID_DIVISIONS): CalibrationMeshData | null {
  const hImageToWorld = estimateCalibrationImageToWorld(view);
  if (!hImageToWorld) return null;
  const region = view.projection_model.image_region;
  const left = region.top_left.x;
  const top = region.top_left.y;
  const right = region.bottom_right.x;
  const bottom = region.bottom_right.y;
  if (right <= left || bottom <= top) return null;

  const vertices: CalibrationMeshVertex[] = [];
  const indices: number[] = [];
  const vertexByGrid = new Map<string, number>();
  const grid = calibrationGridCoordinates(view, divisions);
  const getVertex = (gx: number, gy: number): number | null => {
    const key = `${gx}:${gy}`;
    const existing = vertexByGrid.get(key);
    if (existing != null) return existing;
    const image = {
      x: left + (right - left) * grid.xs[gx],
      y: top + (bottom - top) * grid.ys[gy],
    };
    const world = mapCalibrationImageToWorld(view, hImageToWorld, image);
    if (!world) return null;
    const index = vertices.length;
    vertices.push({ image, world });
    vertexByGrid.set(key, index);
    return index;
  };

  for (let gy = 0; gy < grid.ys.length - 1; gy += 1) {
    for (let gx = 0; gx < grid.xs.length - 1; gx += 1) {
      const a = getVertex(gx, gy);
      const b = getVertex(gx + 1, gy);
      const c = getVertex(gx + 1, gy + 1);
      const d = getVertex(gx, gy + 1);
      if (a == null || b == null || c == null || d == null) continue;
      indices.push(a, b, c, a, c, d);
    }
  }
  if (indices.length < 3) return null;
  const mesh = { vertices, indices };
  return calibrationMeshHasFoldover(mesh) ? null : mesh;
}

function barycentricForWorldPoint(
  point: { x: number; z: number },
  a: { x: number; z: number },
  b: { x: number; z: number },
  c: { x: number; z: number },
): { a: number; b: number; c: number } | null {
  const denominator = (b.z - c.z) * (a.x - c.x) + (c.x - b.x) * (a.z - c.z);
  if (Math.abs(denominator) < 1e-10) return null;
  const alpha = ((b.z - c.z) * (point.x - c.x) + (c.x - b.x) * (point.z - c.z)) / denominator;
  const beta = ((c.z - a.z) * (point.x - c.x) + (a.x - c.x) * (point.z - c.z)) / denominator;
  const gamma = 1 - alpha - beta;
  const tolerance = 1e-4;
  if (alpha < -tolerance || beta < -tolerance || gamma < -tolerance) return null;
  if (alpha > 1 + tolerance || beta > 1 + tolerance || gamma > 1 + tolerance) return null;
  return { a: alpha, b: beta, c: gamma };
}

function resolveImageFromCalibrationMesh(mesh: CalibrationMeshData, world: { x: number; z: number }): { x: number; y: number } | null {
  for (let index = 0; index < mesh.indices.length; index += 3) {
    const va = mesh.vertices[mesh.indices[index]];
    const vb = mesh.vertices[mesh.indices[index + 1]];
    const vc = mesh.vertices[mesh.indices[index + 2]];
    if (!va || !vb || !vc) continue;
    const barycentric = barycentricForWorldPoint(world, va.world, vb.world, vc.world);
    if (!barycentric) continue;
    return {
      x: barycentric.a * va.image.x + barycentric.b * vb.image.x + barycentric.c * vc.image.x,
      y: barycentric.a * va.image.y + barycentric.b * vb.image.y + barycentric.c * vc.image.y,
    };
  }
  return null;
}

function drawCalibrationImageMesh(
  ctx: CanvasRenderingContext2D,
  image: HTMLImageElement,
  view: CameraCalibratedView,
  viewport: Viewport2DContext,
): boolean {
  const mesh = buildCalibrationMesh(view);
  if (!mesh) return false;
  const width = Math.max(1, image.naturalWidth || image.width || 1);
  const height = Math.max(1, image.naturalHeight || image.height || 1);
  for (let index = 0; index < mesh.indices.length; index += 3) {
    const va = mesh.vertices[mesh.indices[index]];
    const vb = mesh.vertices[mesh.indices[index + 1]];
    const vc = mesh.vertices[mesh.indices[index + 2]];
    if (!va || !vb || !vc) continue;
    drawImageTriangle(
      ctx,
      image,
      [
        { x: va.image.x * width, y: va.image.y * height },
        { x: vb.image.x * width, y: vb.image.y * height },
        { x: vc.image.x * width, y: vc.image.y * height },
      ],
      [viewport.worldToScreen(va.world), viewport.worldToScreen(vb.world), viewport.worldToScreen(vc.world)],
    );
  }
  return true;
}

function nearestRefinementPointByScreen(
  screen: { x: number; y: number },
  view: CameraCalibratedView,
  viewport: Viewport2DContext,
  thresholdPx: number,
): CameraProjectionRefinementPoint | null {
  let best: { point: CameraProjectionRefinementPoint; distanceSquared: number } | null = null;
  for (const point of refinementPointsForView(view)) {
    const pointScreen = viewport.worldToScreen(point.world);
    const distanceSquared = screenDistanceSquared(screen, pointScreen);
    if (distanceSquared > thresholdPx * thresholdPx) continue;
    if (!best || distanceSquared < best.distanceSquared) best = { point, distanceSquared };
  }
  return best?.point ?? null;
}

function nearestBoundaryPointByScreen(
  screen: { x: number; y: number },
  view: CameraCalibratedView,
  viewport: Viewport2DContext,
  thresholdPx: number,
): CameraProjectionBoundaryPoint | null {
  let best: { point: CameraProjectionBoundaryPoint; distanceSquared: number } | null = null;
  for (const point of boundaryPointsForView(view)) {
    const pointScreen = viewport.worldToScreen(point.world);
    const distanceSquared = screenDistanceSquared(screen, pointScreen);
    if (distanceSquared > thresholdPx * thresholdPx) continue;
    if (!best || distanceSquared < best.distanceSquared) best = { point, distanceSquared };
  }
  return best?.point ?? null;
}

function canAddBoundaryPoint(view: CameraCalibratedView, edge: CameraProjectionBoundaryEdge): boolean {
  const points = boundaryPointsForView(view);
  if (points.length >= MAX_CALIBRATION_BOUNDARY_POINTS) return false;
  return points.filter((point) => point.edge === edge).length < MAX_CALIBRATION_BOUNDARY_POINTS_PER_EDGE;
}

function distanceSquaredToSegment(
  point: { x: number; y: number },
  start: { x: number; y: number },
  end: { x: number; y: number },
): { distanceSquared: number; t: number } {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared <= 1e-12) return { distanceSquared: screenDistanceSquared(point, start), t: 0 };
  const t = clamp(((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared, 0, 1);
  const projected = { x: start.x + dx * t, y: start.y + dy * t };
  return { distanceSquared: screenDistanceSquared(point, projected), t };
}

function boundaryWorldPoint(view: CameraCalibratedView, imageToWorld: number[], edge: CameraProjectionBoundaryEdge, t: number): { x: number; z: number } | null {
  return mapCalibrationImageToWorld(view, imageToWorld, boundaryImageForEdge(edge, t));
}

function resolveBoundaryGhostByScreen(
  screen: { x: number; y: number },
  view: CameraCalibratedView,
  viewport: Viewport2DContext,
  thresholdPx: number,
): Extract<CalibrationHoverState, { kind: "new_boundary" }> | null {
  const imageToWorld = estimateCalibrationImageToWorld(view);
  if (!imageToWorld) return null;
  let best: { edge: CameraProjectionBoundaryEdge; t: number; world: { x: number; z: number }; distanceSquared: number } | null = null;
  const samplesPerEdge = 64;
  for (const edge of CALIBRATION_BOUNDARY_EDGES) {
    if (!canAddBoundaryPoint(view, edge)) continue;
    let previous = boundaryWorldPoint(view, imageToWorld, edge, 0);
    if (!previous) continue;
    for (let index = 1; index <= samplesPerEdge; index += 1) {
      const endT = index / samplesPerEdge;
      const current = boundaryWorldPoint(view, imageToWorld, edge, endT);
      if (!current) continue;
      const previousScreen = viewport.worldToScreen(previous);
      const currentScreen = viewport.worldToScreen(current);
      const segment = distanceSquaredToSegment(screen, previousScreen, currentScreen);
      if (segment.distanceSquared <= thresholdPx * thresholdPx && (!best || segment.distanceSquared < best.distanceSquared)) {
        const t = (index - 1 + segment.t) / samplesPerEdge;
        const world = boundaryWorldPoint(view, imageToWorld, edge, t);
        if (world) best = { edge, t, world, distanceSquared: segment.distanceSquared };
      }
      previous = current;
    }
  }
  if (!best) return null;
  return {
    kind: "new_boundary",
    edge: best.edge,
    t: best.t,
    image: boundaryImageForEdge(best.edge, best.t),
    world: best.world,
  };
}

function deformedBoundaryScreenPoints(
  view: CameraCalibratedView,
  viewport: Viewport2DContext,
  samplesPerEdge = 48,
): Array<{ x: number; y: number }> {
  const imageToWorld = estimateCalibrationImageToWorld(view);
  if (!imageToWorld) {
    return worldQuadPoints(view.projection_model.world_quad).map((point) => viewport.worldToScreen(point));
  }
  const points: Array<{ x: number; y: number }> = [];
  for (const edge of CALIBRATION_BOUNDARY_EDGES) {
    for (let index = 0; index < samplesPerEdge; index += 1) {
      const world = boundaryWorldPoint(view, imageToWorld, edge, index / samplesPerEdge);
      if (world) points.push(viewport.worldToScreen(world));
    }
  }
  return points.length >= 4 ? points : worldQuadPoints(view.projection_model.world_quad).map((point) => viewport.worldToScreen(point));
}

function isEditableKeyboardTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
}

function isUndoRedoKeyboardShortcut(event: KeyboardEvent): boolean {
  if (!(event.metaKey || event.ctrlKey) || event.altKey) return false;
  const key = event.key.toLowerCase();
  return key === "z" || key === "y";
}

function CameraCalibrationModal({
  open,
  onClose,
  host,
  i18n,
  element,
  cameraId,
  cameraConnectionType,
  cameraSources,
  initialViews,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  host: ToposyncHost;
  i18n: HostI18n;
  element: CompositionElement;
  cameraId: string;
  cameraConnectionType: CameraConnectionType | null;
  cameraSources: CameraSnapshotSourceOption[];
  initialViews: CameraCalibratedView[];
  onSave: (views: CameraCalibratedView[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const isPtzCamera = cameraConnectionType === "onvif";
  const [views, setViews] = useState<CameraCalibratedView[]>([]);
  const [selectedViewId, setSelectedViewId] = useState<string | null>(null);
  const [poseModalOpen, setPoseModalOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotImage, setSnapshotImage] = useState<HTMLImageElement | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [movingToViewId, setMovingToViewId] = useState<string | null>(null);
  const [visualReference, setVisualReference] = useState<VisualCalibrationReference | null>(null);
  const [visualReferenceLoading, setVisualReferenceLoading] = useState(false);
  const [visualRun, setVisualRun] = useState<VisualCalibrationRun>(emptyVisualCalibrationRun);
  const snapshotAbortRef = useRef<AbortController | null>(null);
  const snapshotErrorMessageRef = useRef<string | null>(null);
  const snapshotUrlRef = useRef<string | null>(null);
  const selectedViewIdRef = useRef<string | null>(null);
  const viewsRef = useRef<CameraCalibratedView[]>([]);
  const dragStateRef = useRef<CalibrationDragState | null>(null);
  const hoverStateRef = useRef<CalibrationHoverState>(null);
  const viewportRef = useRef<Viewport2DContext | null>(null);
  const viewportScaleRef = useRef(30);
  const viewSelectionRequestRef = useRef(0);
  const movingToViewIdRef = useRef<string | null>(null);
  const viewMovementAbortRef = useRef<AbortController | null>(null);
  const viewMovementCameraIdRef = useRef("");
  const viewMovementSourceIdRef = useRef("");
  const viewMovementMutationPromiseRef = useRef<Promise<unknown> | null>(null);
  const viewMovementIssuedRef = useRef(false);
  const viewMovementStopIssuedRef = useRef(false);
  const viewMovementStopCompletionRef = useRef<Promise<void> | null>(null);
  const visualReferenceRequestRef = useRef(0);
  const visualReferenceAbortRef = useRef<AbortController | null>(null);
  const visualCalibrationAbortRef = useRef<AbortController | null>(null);

  const selectedView = useMemo(
    () => views.find((view) => view.id === selectedViewId) ?? views[0] ?? null,
    [selectedViewId, views],
  );
  useEffect(() => {
    selectedViewIdRef.current = selectedViewId;
  }, [selectedViewId]);

  useEffect(() => {
    viewsRef.current = views;
  }, [views]);

  useEffect(() => {
    snapshotUrlRef.current = snapshotUrl;
  }, [snapshotUrl]);

  useEffect(() => {
    if (!open) return;

    function preventCompositionHistoryShortcut(event: KeyboardEvent): void {
      if (!isUndoRedoKeyboardShortcut(event) || isEditableKeyboardTarget(event.target)) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    }

    window.addEventListener("keydown", preventCompositionHistoryShortcut, true);
    cancelActiveViewMovement(true);
    visualCalibrationAbortRef.current?.abort();
    visualReferenceAbortRef.current?.abort();
    visualReferenceRequestRef.current += 1;
    setVisualReference(null);
    setVisualReferenceLoading(false);
    setVisualRun(emptyVisualCalibrationRun());
    const baseViews: CameraCalibratedView[] = initialViews.length
      ? initialViews.map(cloneCalibratedView)
      : [createDefaultCalibratedView(0, element.position, { label: t("ext.cameras.calibration.default_view") })];
    setViews(baseViews);
    setSelectedViewId(baseViews[0]?.id ?? null);
    return () => window.removeEventListener("keydown", preventCompositionHistoryShortcut, true);
  }, [element.position, initialViews, open, t]);

  const loadCalibrationSnapshotFromSourceAsync = useCallback(
    async (sourceId: string, sourceName: string): Promise<Blob | null> => {
      if (!cameraId) return null;
      if (!sourceId) {
        const message = t("ext.cameras.visual_calibration.source_unavailable");
        snapshotErrorMessageRef.current = message;
        setSnapshotErrorMessage(message);
        return null;
      }
      snapshotAbortRef.current?.abort();
      const controller = new AbortController();
      snapshotAbortRef.current = controller;
      setSnapshotLoading(true);
      snapshotErrorMessageRef.current = null;
      setSnapshotErrorMessage(null);
      try {
        const blob = await fetchCameraSnapshotWithRetry(cameraId, sourceId, controller.signal);
        const nextUrl = URL.createObjectURL(blob);
        setSnapshotUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return nextUrl;
        });
        return blob;
      } catch (error) {
        if (controller.signal.aborted) return null;
        const message = calibrationSnapshotErrorMessage(
          error,
          t("ext.cameras.visual_calibration.freshness_unverifiable"),
        );
        const displayMessage = sourceName ? `${sourceName}: ${message}` : message;
        snapshotErrorMessageRef.current = displayMessage;
        setSnapshotErrorMessage(displayMessage);
        return null;
      } finally {
        if (!controller.signal.aborted) setSnapshotLoading(false);
      }
    },
    [cameraId, t],
  );

  const loadCalibrationSnapshotFromSource = useCallback(
    (sourceId: string, sourceName: string) => {
      void loadCalibrationSnapshotFromSourceAsync(sourceId, sourceName);
      return () => snapshotAbortRef.current?.abort();
    },
    [loadCalibrationSnapshotFromSourceAsync],
  );

  const loadCalibrationSnapshotForViewAsync = useCallback(
    async (view: CameraCalibratedView | null) => {
      const sourceId = resolvePreferredCalibrationSnapshotSourceId(view, cameraSources);
      const source = cameraSources.find((item) => item.id === sourceId) ?? null;
      return loadCalibrationSnapshotFromSourceAsync(sourceId, snapshotSourceDisplayName(source));
    },
    [cameraSources, loadCalibrationSnapshotFromSourceAsync],
  );

  const loadCalibrationSnapshotForView = useCallback(
    (view: CameraCalibratedView | null) => {
      const sourceId = resolvePreferredCalibrationSnapshotSourceId(view, cameraSources);
      const source = cameraSources.find((item) => item.id === sourceId) ?? null;
      return loadCalibrationSnapshotFromSource(sourceId, snapshotSourceDisplayName(source));
    },
    [cameraSources, loadCalibrationSnapshotFromSource],
  );

  const refreshCurrentCalibrationSnapshot = useCallback(() => {
    const currentView = viewsRef.current.find((view) => view.id === selectedViewIdRef.current) ?? viewsRef.current[0] ?? null;
    loadCalibrationSnapshotForView(currentView);
  }, [loadCalibrationSnapshotForView]);

  function cancelActiveViewMovement(sendStop: boolean): void {
    const controller = viewMovementAbortRef.current;
    const movementCameraId = viewMovementCameraIdRef.current;
    const sourceId = viewMovementSourceIdRef.current;
    const mutationPromise = viewMovementMutationPromiseRef.current;
    const shouldStop =
      sendStop &&
      Boolean(movementCameraId) &&
      Boolean(sourceId) &&
      viewMovementIssuedRef.current &&
      !viewMovementStopIssuedRef.current;
    if (shouldStop) viewMovementStopIssuedRef.current = true;
    controller?.abort();
    viewMovementAbortRef.current = null;
    viewMovementCameraIdRef.current = "";
    viewMovementSourceIdRef.current = "";
    viewMovementMutationPromiseRef.current = null;
    viewMovementIssuedRef.current = false;
    if (shouldStop) {
      const stopCompletion = enqueueCameraPtzStop(
        movementCameraId,
        sourceId,
        mutationPromise,
      );
      viewMovementStopCompletionRef.current = stopCompletion;
      void stopCompletion.finally(() => {
        if (viewMovementStopCompletionRef.current === stopCompletion) {
          viewMovementStopCompletionRef.current = null;
        }
      });
    }
  }

  async function registerActiveViewMovement(
    controller: AbortController,
    sourceId: string,
  ): Promise<boolean> {
    cancelActiveViewMovement(true);
    viewMovementAbortRef.current = controller;
    viewMovementCameraIdRef.current = cameraId;
    viewMovementSourceIdRef.current = sourceId;
    viewMovementMutationPromiseRef.current = null;
    viewMovementIssuedRef.current = false;
    viewMovementStopIssuedRef.current = false;
    const pendingStop = viewMovementStopCompletionRef.current;
    if (pendingStop) await pendingStop;
    await waitForPendingCameraPtzStops(cameraId);
    return viewMovementAbortRef.current === controller && !controller.signal.aborted;
  }

  function markActiveViewMovementIssued(
    controller: AbortController,
    mutationPromise: Promise<unknown>,
  ): void {
    if (viewMovementAbortRef.current !== controller) return;
    viewMovementMutationPromiseRef.current = mutationPromise;
    viewMovementIssuedRef.current = true;
  }

  function releaseActiveViewMovement(controller: AbortController): void {
    if (viewMovementAbortRef.current !== controller) return;
    viewMovementAbortRef.current = null;
    viewMovementCameraIdRef.current = "";
    viewMovementSourceIdRef.current = "";
    viewMovementMutationPromiseRef.current = null;
    viewMovementIssuedRef.current = false;
    viewMovementStopIssuedRef.current = false;
  }

  useEffect(() => {
    if (!open) {
      setPoseModalOpen(false);
      viewSelectionRequestRef.current += 1;
      cancelActiveViewMovement(true);
      snapshotAbortRef.current?.abort();
      visualCalibrationAbortRef.current?.abort();
      visualReferenceAbortRef.current?.abort();
      visualReferenceRequestRef.current += 1;
      snapshotErrorMessageRef.current = null;
      setSnapshotErrorMessage(null);
      setSnapshotLoading(false);
      setSnapshotImage(null);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
      setDragging(false);
      movingToViewIdRef.current = null;
      setMovingToViewId(null);
      setVisualReference(null);
      setVisualReferenceLoading(false);
      setVisualRun(emptyVisualCalibrationRun());
      dragStateRef.current = null;
      return;
    }
  }, [open]);

  useEffect(() => {
    if (!snapshotUrl) {
      setSnapshotImage(null);
      return;
    }
    let cancelled = false;
    const image = new Image();
    image.onload = () => {
      if (!cancelled) setSnapshotImage(image);
    };
    image.onerror = () => {
      if (!cancelled) setSnapshotImage(null);
    };
    image.src = snapshotUrl;
    return () => {
      cancelled = true;
    };
  }, [snapshotUrl]);

  useEffect(() => {
    return () => {
      viewSelectionRequestRef.current += 1;
      cancelActiveViewMovement(true);
      snapshotAbortRef.current?.abort();
      visualCalibrationAbortRef.current?.abort();
      visualReferenceAbortRef.current?.abort();
      visualReferenceRequestRef.current += 1;
      if (snapshotUrlRef.current) URL.revokeObjectURL(snapshotUrlRef.current);
    };
  }, []);

  function updateSelectedView(updater: (view: CameraCalibratedView) => CameraCalibratedView) {
    if (movingToViewIdRef.current) return;
    const currentId = selectedViewIdRef.current;
    if (!currentId) return;
    visualCalibrationAbortRef.current?.abort();
    visualReferenceAbortRef.current?.abort();
    visualReferenceRequestRef.current += 1;
    setVisualReferenceLoading(false);
    setVisualReference((previous) => (previous?.view.id === currentId ? null : previous));
    setVisualRun((previous) =>
      previous.targetViewId === currentId || visualReference?.view.id === currentId
        ? emptyVisualCalibrationRun()
        : previous,
    );
    setViews((previous) => previous.map((view) => (view.id === currentId ? updater(view) : view)));
  }

  function updateSelectedQuad(nextQuad: CameraProjectionWorldQuad, status: "ready" | "estimated" = "ready") {
    updateSelectedView((view) => ({
      ...view,
      projection_model: {
        ...view.projection_model,
        world_quad: cloneWorldQuad(nextQuad),
      },
      projection_quality: {
        ...(view.projection_quality ?? {}),
        status,
        estimated: status === "estimated",
      },
    }));
  }

  function updateSelectedProjection(
    nextQuad: CameraProjectionWorldQuad,
    nextRefinementPoints: CameraProjectionRefinementPoint[],
    nextBoundaryPoints: CameraProjectionBoundaryPoint[],
    status: "ready" | "estimated" = "ready",
  ) {
    updateSelectedView((view) =>
      withRefinementAndBoundaryPoints(
        {
          ...view,
          projection_model: {
            ...view.projection_model,
            world_quad: cloneWorldQuad(nextQuad),
          },
          projection_quality: {
            ...(view.projection_quality ?? {}),
            status,
            estimated: status === "estimated",
          },
        },
        nextRefinementPoints,
        nextBoundaryPoints,
        status,
      ),
    );
  }

  function updateSelectedRefinementPoints(updater: (points: CameraProjectionRefinementPoint[]) => CameraProjectionRefinementPoint[]) {
    updateSelectedView((view) => withRefinementPoints(view, updater(cloneRefinementPoints(refinementPointsForView(view)))));
  }

  function updateSelectedBoundaryPoints(updater: (points: CameraProjectionBoundaryPoint[]) => CameraProjectionBoundaryPoint[]) {
    updateSelectedView((view) => withBoundaryPoints(view, updater(cloneBoundaryPoints(boundaryPointsForView(view)))));
  }

  async function waitForPtzToSettle(
    sourceId: string,
    signal?: AbortSignal,
    expectation?: PtzSettleExpectation,
  ): Promise<PanTiltZoomState | null> {
    if (signal) await waitForRetry(500, signal);
    else await sleep(500);
    let latestStatus: PanTiltZoomState | null = null;
    let previousStatus: PanTiltZoomState | null = null;
    let consecutiveIdleReads = 0;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      try {
        const response = await fetchCameraPtzStatus(cameraId, sourceId, signal);
        latestStatus = response.status ?? null;
      } catch (error) {
        if (isAbortError(error) || signal?.aborted) throw error;
        return null;
      }
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const moveStatus = normalizePtzMoveStatus(latestStatus?.move_status);
      const targetPoseIsContradicted = Boolean(
        expectation?.targetPose &&
          poseHasAbsoluteTarget(expectation.targetPose) &&
          ptzStatusContradictsPose(latestStatus, expectation.targetPose),
      );
      if (
        !targetPoseIsContradicted &&
        (moveStatus === "idle" ||
          (moveStatus === "unknown" && ptzTelemetryIsStable(previousStatus, latestStatus)))
      ) {
        consecutiveIdleReads += 1;
        if (consecutiveIdleReads >= 2) {
          if (ptzSettleRequiresVisualConfirmation(expectation)) {
            if (!signal) return null;
            const visuallyStable = await waitForCameraVisualStability(
              cameraId,
              sourceId,
              signal,
              expectation?.baselineVisualFingerprint,
              expectation?.requireVisualTransition === true &&
                !ptzSettleTargetIsConfirmed(latestStatus, expectation),
            );
            if (!visuallyStable) return null;
          }
          if (signal) await waitForRetry(350, signal);
          else await sleep(350);
          return latestStatus;
        }
      } else {
        consecutiveIdleReads = 0;
      }
      previousStatus = latestStatus;
      if (signal) await waitForRetry(450, signal);
      else await sleep(450);
    }
    return null;
  }

  async function useSelectedViewAsVisualReference(): Promise<void> {
    if (movingToViewIdRef.current) return;
    const sourceView = selectedView;
    if (
      !sourceView ||
      summarizeCalibratedViewQuality(sourceView).status !== "good" ||
      sourceView.projection_quality?.status !== "ready" ||
      sourceView.projection_quality?.estimated === true
    ) {
      setVisualRun({
        status: "error",
        targetViewId: sourceView?.id ?? null,
        result: null,
        proposedView: null,
        errorMessage: t("ext.cameras.visual_calibration.reference_not_ready"),
      });
      return;
    }

    const sourcePose = sourceView.pose_reference ?? null;
    const sourcePresetToken = String(sourcePose?.preset_token ?? "").trim();
    if (isPtzCamera && !sourcePresetToken && !poseHasAbsoluteTarget(sourcePose)) {
      setVisualRun({
        status: "error",
        targetViewId: sourceView.id,
        result: null,
        proposedView: null,
        errorMessage: t("ext.cameras.visual_calibration.reference_pose_required"),
      });
      return;
    }

    setVisualReferenceLoading(true);
    cancelActiveViewMovement(true);
    visualReferenceAbortRef.current?.abort();
    const controller = new AbortController();
    visualReferenceAbortRef.current = controller;
    const referenceRequestId = visualReferenceRequestRef.current + 1;
    visualReferenceRequestRef.current = referenceRequestId;
    try {
      const sourceId = resolvePreferredCalibrationSnapshotSourceId(sourceView, cameraSources);
      if (!sourceId) {
        throw new Error(t("ext.cameras.visual_calibration.source_unavailable"));
      }
      if (isPtzCamera) {
        const ptzSourceId = resolvePreferredCalibrationPtzSourceId(sourceView, cameraSources);
        if (!ptzSourceId) {
          throw new Error(t("ext.cameras.visual_calibration.source_unavailable"));
        }
        if (!(await registerActiveViewMovement(controller, ptzSourceId))) return;
        const baseline = await capturePtzMovementBaseline(
          cameraId,
          ptzSourceId,
          sourceId,
          sourcePose,
          controller.signal,
          t("ext.cameras.visual_calibration.snapshot_failed"),
          Boolean(sourcePresetToken),
        );
        if (!baseline.targetAlreadyConfirmed) {
          const mutationPromise = sourcePresetToken
            ? runPtzMutationWithFence(() => gotoCameraPtzPreset(cameraId, sourcePresetToken, ptzSourceId))
            : runPtzMutationWithFence(() =>
                moveCameraPtzAbsolute(cameraId, absoluteMovePayloadForPose(ptzSourceId, sourcePose!)),
              );
          markActiveViewMovementIssued(controller, mutationPromise);
          await mutationPromise;
        }
        if (controller.signal.aborted) return;
        const status = await waitForPtzToSettle(ptzSourceId, controller.signal, {
          baselineVisualFingerprint: baseline.visualFingerprint,
          targetPose: sourcePose,
          requireTargetEvidence: true,
          requireVisualTransition: !baseline.targetAlreadyConfirmed,
        });
        if (visualReferenceRequestRef.current !== referenceRequestId) return;
        if (
          !status ||
          (sourcePose &&
            poseHasAbsoluteTarget(sourcePose) &&
            ptzStatusContradictsPose(status, sourcePose))
        ) {
          cancelActiveViewMovement(true);
          setVisualRun({
            status: "error",
            targetViewId: sourceView.id,
            result: null,
            proposedView: null,
            errorMessage: t("ext.cameras.visual_calibration.camera_status_unavailable"),
          });
          return;
        }
        releaseActiveViewMovement(controller);
      }
      const image = await loadCalibrationSnapshotForViewAsync(sourceView);
      if (controller.signal.aborted || visualReferenceRequestRef.current !== referenceRequestId) return;
      if (!image) {
        setVisualRun({
          status: "error",
          targetViewId: sourceView.id,
          result: null,
          proposedView: null,
          errorMessage:
            snapshotErrorMessageRef.current ?? t("ext.cameras.visual_calibration.snapshot_failed"),
        });
        return;
      }

      const frozenView = cloneCalibratedView(sourceView);
      const frozenSource = cameraSources.find((source) => source.id === sourceId) ?? null;
      frozenView.stream_scope = {
        compatible_roles: frozenSource?.role ? [frozenSource.role] : [],
        compatible_source_ids: [sourceId],
      };
      setVisualReference({ view: frozenView, image, sourceId });
      setVisualRun(emptyVisualCalibrationRun());
    } catch (error) {
      if (
        controller.signal.aborted ||
        isAbortError(error) ||
        visualReferenceRequestRef.current !== referenceRequestId
      ) {
        return;
      }
      setVisualRun({
        status: "error",
        targetViewId: sourceView.id,
        result: null,
        proposedView: null,
        errorMessage: calibrationSnapshotErrorMessage(
          error,
          t("ext.cameras.visual_calibration.freshness_unverifiable"),
        ),
      });
    } finally {
      if (viewMovementAbortRef.current === controller) cancelActiveViewMovement(true);
      if (visualReferenceAbortRef.current === controller) {
        visualReferenceAbortRef.current = null;
      }
      if (visualReferenceRequestRef.current === referenceRequestId) {
        setVisualReferenceLoading(false);
      }
    }
  }

  async function analyzeSelectedViewVisually(): Promise<void> {
    if (movingToViewIdRef.current) return;
    const reference = visualReference;
    const targetView = selectedView;
    if (!reference || !targetView || targetView.id === reference.view.id) return;

    const targetSourceId = resolvePreferredCalibrationSnapshotSourceId(targetView, cameraSources);
    if (!targetSourceId) {
      setVisualRun({
        status: "error",
        targetViewId: targetView.id,
        result: null,
        proposedView: null,
        errorMessage: t("ext.cameras.visual_calibration.source_unavailable"),
      });
      return;
    }
    if (targetSourceId !== reference.sourceId) {
      setVisualRun({
        status: "error",
        targetViewId: targetView.id,
        result: null,
        proposedView: null,
        errorMessage: t("ext.cameras.visual_calibration.same_lens_required"),
      });
      return;
    }

    cancelActiveViewMovement(true);
    visualCalibrationAbortRef.current?.abort();
    const controller = new AbortController();
    visualCalibrationAbortRef.current = controller;
    setVisualRun({
      status: "analyzing",
      targetViewId: targetView.id,
      result: null,
      proposedView: null,
      errorMessage: null,
    });

    try {
      let poseReference = targetView.pose_reference ? { ...targetView.pose_reference } : null;
      if (isPtzCamera) {
        const targetPresetToken = String(poseReference?.preset_token ?? "").trim();
        if (!targetPresetToken && !poseHasAbsoluteTarget(poseReference)) {
          throw new Error(t("ext.cameras.visual_calibration.target_pose_required"));
        }
        const ptzSourceId = resolvePreferredCalibrationPtzSourceId(targetView, cameraSources);
        if (!ptzSourceId) {
          throw new Error(t("ext.cameras.visual_calibration.source_unavailable"));
        }
        if (!(await registerActiveViewMovement(controller, ptzSourceId))) return;
        const baseline = await capturePtzMovementBaseline(
          cameraId,
          ptzSourceId,
          targetSourceId,
          poseReference,
          controller.signal,
          t("ext.cameras.visual_calibration.snapshot_failed"),
          Boolean(targetPresetToken),
        );
        if (!baseline.targetAlreadyConfirmed) {
          const mutationPromise = targetPresetToken
            ? runPtzMutationWithFence(() => gotoCameraPtzPreset(cameraId, targetPresetToken, ptzSourceId))
            : runPtzMutationWithFence(() =>
                moveCameraPtzAbsolute(cameraId, absoluteMovePayloadForPose(ptzSourceId, poseReference!)),
              );
          markActiveViewMovementIssued(controller, mutationPromise);
          await mutationPromise;
        }
        if (controller.signal.aborted) return;
        const status = await waitForPtzToSettle(ptzSourceId, controller.signal, {
          baselineVisualFingerprint: baseline.visualFingerprint,
          targetPose: poseReference,
          requireTargetEvidence: true,
          requireVisualTransition: !baseline.targetAlreadyConfirmed,
        });
        if (controller.signal.aborted) return;
        if (
          !status ||
          (poseReference &&
            poseHasAbsoluteTarget(poseReference) &&
            ptzStatusContradictsPose(status, poseReference))
        ) {
          cancelActiveViewMovement(true);
          throw new Error(t("ext.cameras.visual_calibration.camera_status_unavailable"));
        }
        releaseActiveViewMovement(controller);
        poseReference = {
          ...poseReference,
          pan: typeof status.pan === "number" && Number.isFinite(status.pan) ? status.pan : poseReference?.pan ?? null,
          tilt: typeof status.tilt === "number" && Number.isFinite(status.tilt) ? status.tilt : poseReference?.tilt ?? null,
          zoom: typeof status.zoom === "number" && Number.isFinite(status.zoom) ? status.zoom : poseReference?.zoom ?? null,
        };
      }

      if (controller.signal.aborted) return;
      const targetImage = await loadCalibrationSnapshotForViewAsync(targetView);
      if (controller.signal.aborted) return;
      if (!targetImage) {
        throw new Error(
          snapshotErrorMessageRef.current ?? t("ext.cameras.visual_calibration.snapshot_failed"),
        );
      }
      const result = await propagateCameraProjection(
        reference.view,
        reference.sourceId,
        reference.image,
        targetImage,
        controller.signal,
      );
      if (controller.signal.aborted) return;

      if (!result.accepted || !result.projection_model) {
        setVisualRun({
          status: "rejected",
          targetViewId: targetView.id,
          result,
          proposedView: null,
          errorMessage: null,
        });
        return;
      }
      if (!result.source_visual_pose_signature || !result.projection_model.visual_pose_signature) {
        setVisualRun({
          status: "rejected",
          targetViewId: targetView.id,
          result: {
            ...result,
            accepted: false,
            reason: "visual_pose_signature_failed",
            projection_model: null,
          },
          proposedView: null,
          errorMessage: null,
        });
        return;
      }

      const targetSource = cameraSources.find((source) => source.id === targetSourceId) ?? null;
      const proposedView: CameraCalibratedView = {
        ...cloneCalibratedView(targetView),
        pose_reference: poseReference,
        stream_scope: {
          compatible_roles: targetSource?.role ? [targetSource.role] : [],
          compatible_source_ids: [targetSourceId],
        },
        projection_model: result.projection_model,
        projection_quality: {
          status: "estimated",
          estimated: true,
          note: `visual calibration; ${result.quality.inliers} inliers; p95=${result.quality.p95_reprojection_error_px ?? "n/a"}px`,
        },
      };
      setVisualRun({
        status: "proposed",
        targetViewId: targetView.id,
        result,
        proposedView,
        errorMessage: null,
      });
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      setVisualRun({
        status: "error",
        targetViewId: targetView.id,
        result: null,
        proposedView: null,
        errorMessage: calibrationSnapshotErrorMessage(
          error,
          t("ext.cameras.visual_calibration.freshness_unverifiable"),
        ),
      });
    } finally {
      if (viewMovementAbortRef.current === controller) cancelActiveViewMovement(true);
      if (visualCalibrationAbortRef.current === controller) visualCalibrationAbortRef.current = null;
    }
  }

  function acceptVisualCalibration(): void {
    if (movingToViewIdRef.current) return;
    const targetViewId = visualRun.targetViewId;
    const proposedView = visualRun.proposedView;
    if (!targetViewId || visualRun.status !== "proposed" || !proposedView) return;
    const sourceViewId = visualReference?.view.id ?? null;
    const frozenSourceScope = visualReference?.view.stream_scope ?? null;
    const sourceVisualPoseSignature = visualRun.result?.source_visual_pose_signature ?? null;
    setViews((previous) =>
      previous.map((view) => {
        if (view.id === targetViewId) {
          return {
            ...cloneCalibratedView(proposedView),
            projection_quality: {
              ...(proposedView.projection_quality ?? {}),
              status: "ready",
              estimated: false,
            },
          };
        }
        if (sourceViewId && view.id === sourceViewId && sourceVisualPoseSignature) {
          return {
            ...view,
            stream_scope: frozenSourceScope
              ? {
                  compatible_roles: [...(frozenSourceScope.compatible_roles ?? [])],
                  compatible_source_ids: [...(frozenSourceScope.compatible_source_ids ?? [])],
                }
              : view.stream_scope,
            projection_model: {
              ...view.projection_model,
              visual_pose_signature: { ...sourceVisualPoseSignature },
            },
          };
        }
        return view;
      }),
    );
    setVisualRun((previous) => ({ ...previous, status: "accepted", proposedView: null }));
  }

  function discardVisualCalibration(): void {
    if (movingToViewIdRef.current) return;
    visualCalibrationAbortRef.current?.abort();
    setVisualRun(emptyVisualCalibrationRun());
  }

  async function selectView(view: CameraCalibratedView) {
    if (movingToViewIdRef.current) return;
    const poseReference = view.pose_reference ?? null;
    const presetToken = String(poseReference?.preset_token ?? "").trim();
    const shouldRepositionCamera =
      isPtzCamera &&
      Boolean(cameraId) &&
      (Boolean(presetToken) || poseHasAbsoluteTarget(poseReference));
    const movementFenceSourceId = isPtzCamera
      ? resolvePreferredCalibrationPtzSourceId(view, cameraSources)
      : "";
    const pendingMovementStop = isPtzCamera
      ? pendingCameraPtzStopCompletion(cameraId)
      : null;
    const requestId = viewSelectionRequestRef.current + 1;
    viewSelectionRequestRef.current = requestId;
    selectedViewIdRef.current = view.id;
    setSelectedViewId(view.id);
    if (shouldRepositionCamera || pendingMovementStop) {
      movingToViewIdRef.current = view.id;
      setMovingToViewId(view.id);
    }
    snapshotErrorMessageRef.current = null;
    setSnapshotErrorMessage(null);
    setSnapshotImage(null);
    let movementController: AbortController | null = null;
    try {
      if (pendingMovementStop) {
        await waitForPendingCameraPtzStops(cameraId);
        if (viewSelectionRequestRef.current !== requestId) return;
      }
      if (shouldRepositionCamera) {
        const sourceId = movementFenceSourceId;
        if (!sourceId) {
          throw new Error(t("ext.cameras.visual_calibration.source_unavailable"));
        }
        movementController = new AbortController();
        if (!(await registerActiveViewMovement(movementController, sourceId))) return;
        const targetPose: CameraPoseReference | null = presetToken
          ? {
              pan: poseReference?.pan ?? null,
              tilt: poseReference?.tilt ?? null,
              zoom: poseReference?.zoom ?? null,
              preset_token: presetToken,
              preset_name: poseReference?.preset_name ?? null,
            }
          : poseReference;
        const baseline = await capturePtzMovementBaseline(
          cameraId,
          sourceId,
          sourceId,
          targetPose,
          movementController.signal,
          t("ext.cameras.visual_calibration.snapshot_failed"),
          Boolean(presetToken),
        );
        if (presetToken) {
          if (!baseline.targetAlreadyConfirmed) {
            const mutationPromise = runPtzMutationWithFence(() =>
              gotoCameraPtzPreset(cameraId, presetToken, sourceId),
            );
            markActiveViewMovementIssued(movementController, mutationPromise);
            await mutationPromise;
          }
          if (!(await waitForPtzToSettle(sourceId, movementController.signal, {
            baselineVisualFingerprint: baseline.visualFingerprint,
            targetPose,
            requireTargetEvidence: true,
            requireVisualTransition: !baseline.targetAlreadyConfirmed,
          }))) {
            throw new Error(t("ext.cameras.visual_calibration.camera_status_unavailable"));
          }
        } else if (poseHasAbsoluteTarget(poseReference)) {
          if (!baseline.targetAlreadyConfirmed) {
            const mutationPromise = runPtzMutationWithFence(() =>
              moveCameraPtzAbsolute(cameraId, absoluteMovePayloadForPose(sourceId, poseReference!)),
            );
            markActiveViewMovementIssued(movementController, mutationPromise);
            await mutationPromise;
          }
          if (!(await waitForPtzToSettle(sourceId, movementController.signal, {
            baselineVisualFingerprint: baseline.visualFingerprint,
            targetPose: poseReference,
            requireTargetEvidence: true,
            requireVisualTransition: !baseline.targetAlreadyConfirmed,
          }))) {
            throw new Error(t("ext.cameras.visual_calibration.camera_status_unavailable"));
          }
        }
        releaseActiveViewMovement(movementController);
      }
      if (viewSelectionRequestRef.current === requestId) await loadCalibrationSnapshotForViewAsync(view);
    } catch (error) {
      if (viewSelectionRequestRef.current === requestId && !isAbortError(error)) {
        setSnapshotErrorMessage(
          calibrationSnapshotErrorMessage(
            error,
            t("ext.cameras.visual_calibration.freshness_unverifiable"),
          ),
        );
      }
    } finally {
      if (movementController && viewMovementAbortRef.current === movementController) {
        cancelActiveViewMovement(true);
      }
      if (viewSelectionRequestRef.current === requestId) {
        movingToViewIdRef.current = null;
        setMovingToViewId(null);
      }
    }
  }

  function addView(options?: { visualTarget?: boolean }) {
    if (movingToViewIdRef.current) return;
    const previous = viewsRef.current;
    const source = selectedView ?? previous[0] ?? null;
    const nextView = createDefaultCalibratedView(previous.length, element.position, {
      label: t("ext.cameras.calibration.view_label", { index: previous.length + 1 }),
    });
    if (source?.stream_scope) {
      nextView.stream_scope = {
        compatible_roles: source.stream_scope.compatible_roles?.length ? [...source.stream_scope.compatible_roles] : ["main", "sub"],
        compatible_source_ids: [...(source.stream_scope.compatible_source_ids ?? [])],
      };
    }
    selectedViewIdRef.current = nextView.id;
    setSelectedViewId(nextView.id);
    setViews((current) => [...current, nextView]);
    if (options?.visualTarget) {
      setVisualRun({
        ...emptyVisualCalibrationRun(),
        targetViewId: nextView.id,
      });
      window.setTimeout(() => setPoseModalOpen(true), 0);
    }
  }

  function removeSelectedView() {
    if (movingToViewIdRef.current) return;
    if (!selectedViewId || views.length <= 1) return;
    if (visualReference?.view.id === selectedViewId) setVisualReference(null);
    if (visualRun.targetViewId === selectedViewId) setVisualRun(emptyVisualCalibrationRun());
    setViews((previous) => {
      const filtered = previous.filter((view) => view.id !== selectedViewId);
      setSelectedViewId(filtered[0]?.id ?? null);
      return filtered;
    });
  }

  function setCompatibleRole(role: string, enabled: boolean) {
    updateSelectedView((view) => {
      const current = view.stream_scope?.compatible_roles?.length ? view.stream_scope.compatible_roles : ["main", "sub"];
      const next = enabled ? Array.from(new Set([...current, role])) : current.filter((item) => item !== role);
      return invalidateVisualCalibrationApproval({
        ...view,
        stream_scope: {
          compatible_roles: next,
          compatible_source_ids: [...(view.stream_scope?.compatible_source_ids ?? [])],
        },
      });
    });
  }

  const visualInteractionLocked =
    visualReferenceLoading || visualRun.status === "analyzing" || visualRun.status === "proposed";
  const calibrationInteractionLocked = visualInteractionLocked || movingToViewId !== null;

  const toolSession = useMemo<EditorToolSession>(() => {
    function resolveHoverState(
      event: EditorToolPointerEvent,
      view: CameraCalibratedView,
      viewport: Viewport2DContext | null,
    ): CalibrationHoverState {
      const quad = view.projection_model.world_quad;
      if (viewport) {
        const corner = nearestWorldQuadCornerByScreen(event.screen, quad, viewport, 13);
        if (corner) return { kind: "corner", corner };
        const rotateInfo = calibrationRotateHandleInfo(quad, viewport);
        if (screenDistanceSquared(event.screen, rotateInfo.handleScreen) <= rotateInfo.hitRadiusPx * rotateInfo.hitRadiusPx) {
          return { kind: "rotate" };
        }
        const boundaryPoint = nearestBoundaryPointByScreen(event.screen, view, viewport, 12);
        if (boundaryPoint) return { kind: "boundary", pointId: boundaryPoint.id };
        const boundaryGhost = resolveBoundaryGhostByScreen(event.screen, view, viewport, 11);
        if (boundaryGhost) return boundaryGhost;
        const refinementPoint = nearestRefinementPointByScreen(event.screen, view, viewport, 12);
        if (refinementPoint) return { kind: "refinement", pointId: refinementPoint.id };
        const centerScreen = viewport.worldToScreen(quadCenter(quad));
        if (screenDistanceSquared(event.screen, centerScreen) <= 15 * 15) {
          return { kind: "move" };
        }
      } else {
        const thresholdWorld = Math.max(0.08, 18 / Math.max(1, viewportScaleRef.current));
        const corner = nearestWorldQuadCorner(event.world, quad, thresholdWorld);
        if (corner) return { kind: "corner", corner };
        const center = quadCenter(quad);
        if (Math.hypot(event.world.x - center.x, event.world.z - center.z) <= thresholdWorld) return { kind: "move" };
      }
      if (refinementPointsForView(view).length >= MAX_CALIBRATION_REFINEMENT_POINTS) return null;
      const mesh = buildCalibrationMesh(view);
      const image = mesh ? resolveImageFromCalibrationMesh(mesh, event.world) : null;
      return image ? { kind: "new_refinement", image, world: { x: event.world.x, z: event.world.z } } : null;
    }

    return {
      shouldCapturePointer: (event: EditorToolPointerEvent) => {
        if (movingToViewId || visualInteractionLocked || event.kind !== "down" || event.button !== 0) return false;
        const viewId = selectedViewIdRef.current;
        const currentView = viewsRef.current.find((view) => view.id === viewId) ?? viewsRef.current[0] ?? null;
        if (!currentView) return false;
        return Boolean(resolveHoverState(event, currentView, viewportRef.current));
      },
      onPointerEvent: (event: EditorToolPointerEvent) => {
        if (movingToViewId || visualInteractionLocked) return;
        const viewId = selectedViewIdRef.current;
        const storedView = viewsRef.current.find((view) => view.id === viewId) ?? viewsRef.current[0] ?? null;
        const currentView =
          visualRun.status === "proposed" &&
          visualRun.targetViewId === storedView?.id &&
          visualRun.proposedView
            ? visualRun.proposedView
            : storedView;
        if (!currentView) return;
        const quad = currentView.projection_model.world_quad;
        if (event.kind === "down") {
          const viewport = viewportRef.current;
          const hover = resolveHoverState(event, currentView, viewport);
          hoverStateRef.current = hover;
          if (hover?.kind === "corner") {
            dragStateRef.current = { kind: "corner", corner: hover.corner, startQuad: cloneWorldQuad(quad) };
          } else if (hover?.kind === "rotate" && viewport) {
            const rotateInfo = calibrationRotateHandleInfo(quad, viewport);
            dragStateRef.current = {
              kind: "rotate",
              centerWorld: rotateInfo.centerWorld,
              startAngle: Math.atan2(event.world.z - rotateInfo.centerWorld.z, event.world.x - rotateInfo.centerWorld.x),
              startQuad: cloneWorldQuad(quad),
              startRefinementPoints: cloneRefinementPoints(refinementPointsForView(currentView)),
              startBoundaryPoints: cloneBoundaryPoints(boundaryPointsForView(currentView)),
              snappedDelta: 0,
            };
          } else if (hover?.kind === "move") {
            dragStateRef.current = {
              kind: "move",
              startWorld: { x: event.world.x, z: event.world.z },
              startQuad: cloneWorldQuad(quad),
              startRefinementPoints: cloneRefinementPoints(refinementPointsForView(currentView)),
              startBoundaryPoints: cloneBoundaryPoints(boundaryPointsForView(currentView)),
            };
          } else if (hover?.kind === "refinement") {
            dragStateRef.current = {
              kind: "refinement",
              pointId: hover.pointId,
              pointerStartWorld: { x: event.world.x, z: event.world.z },
              created: false,
              moved: false,
            };
          } else if (hover?.kind === "boundary") {
            dragStateRef.current = {
              kind: "boundary",
              pointId: hover.pointId,
              pointerStartWorld: { x: event.world.x, z: event.world.z },
              created: false,
              moved: false,
            };
          } else if (hover?.kind === "new_refinement") {
            const pointId = createUniqueId();
            updateSelectedRefinementPoints((points) => [
              ...points,
              {
                id: pointId,
                image: { x: hover.image.x, y: hover.image.y },
                world: { x: event.world.x, z: event.world.z },
              },
            ]);
            dragStateRef.current = {
              kind: "refinement",
              pointId,
              pointerStartWorld: { x: event.world.x, z: event.world.z },
              created: true,
              moved: false,
            };
          } else if (hover?.kind === "new_boundary") {
            const pointId = createUniqueId();
            updateSelectedBoundaryPoints((points) => [
              ...points,
              {
                id: pointId,
                edge: hover.edge,
                t: hover.t,
                image: { x: hover.image.x, y: hover.image.y },
                world: { x: event.world.x, z: event.world.z },
              },
            ]);
            dragStateRef.current = {
              kind: "boundary",
              pointId,
              pointerStartWorld: { x: event.world.x, z: event.world.z },
              created: true,
              moved: false,
            };
          } else {
            dragStateRef.current = null;
          }
          setDragging(Boolean(dragStateRef.current));
          return;
        }
        if (event.kind === "up" || event.kind === "cancel") {
          const drag = dragStateRef.current;
          if (event.kind === "up" && drag?.kind === "refinement" && !drag.created && !drag.moved) {
            updateSelectedRefinementPoints((points) => points.filter((point) => point.id !== drag.pointId));
          }
          if (event.kind === "up" && drag?.kind === "boundary" && !drag.created && !drag.moved) {
            updateSelectedBoundaryPoints((points) => points.filter((point) => point.id !== drag.pointId));
          }
          dragStateRef.current = null;
          setDragging(false);
          return;
        }
        if (event.kind !== "move") return;
        if (!dragStateRef.current) {
          hoverStateRef.current = resolveHoverState(event, currentView, viewportRef.current);
          return;
        }
        const drag = dragStateRef.current;
        if (drag.kind === "corner") {
          const nextQuad = cloneWorldQuad(drag.startQuad);
          nextQuad[drag.corner] = { x: event.world.x, z: event.world.z };
          updateSelectedQuad(nextQuad);
          return;
        }
        if (drag.kind === "rotate") {
          const currentAngle = Math.atan2(event.world.z - drag.centerWorld.z, event.world.x - drag.centerWorld.x);
          const snappedDelta = calibrationRotationDelta(normalizeAngleRad(currentAngle - drag.startAngle), event);
          dragStateRef.current = { ...drag, snappedDelta };
          updateSelectedProjection(
            rotateWorldQuad(drag.startQuad, snappedDelta),
            rotateRefinementPoints(drag.startRefinementPoints, drag.centerWorld, snappedDelta),
            rotateBoundaryPoints(drag.startBoundaryPoints, drag.centerWorld, snappedDelta),
          );
          return;
        }
        if (drag.kind === "refinement") {
          const movedThresholdWorld = Math.max(0.01, 4 / Math.max(1, viewportScaleRef.current));
          const moved =
            drag.moved ||
            Math.hypot(event.world.x - drag.pointerStartWorld.x, event.world.z - drag.pointerStartWorld.z) > movedThresholdWorld;
          dragStateRef.current = { ...drag, moved };
          updateSelectedRefinementPoints((points) =>
            points.map((point) =>
              point.id === drag.pointId
                ? {
                    ...point,
                    image: { ...point.image },
                    world: { x: event.world.x, z: event.world.z },
                  }
                : point,
            ),
          );
          return;
        }
        if (drag.kind === "boundary") {
          const movedThresholdWorld = Math.max(0.01, 4 / Math.max(1, viewportScaleRef.current));
          const moved =
            drag.moved ||
            Math.hypot(event.world.x - drag.pointerStartWorld.x, event.world.z - drag.pointerStartWorld.z) > movedThresholdWorld;
          dragStateRef.current = { ...drag, moved };
          updateSelectedBoundaryPoints((points) =>
            points.map((point) =>
              point.id === drag.pointId
                ? {
                    ...point,
                    image: boundaryImageForEdge(point.edge, point.t),
                    world: { x: event.world.x, z: event.world.z },
                  }
                : point,
            ),
          );
          return;
        }
        const delta = {
          x: event.world.x - drag.startWorld.x,
          z: event.world.z - drag.startWorld.z,
        };
        updateSelectedProjection(
          translateWorldQuad(drag.startQuad, delta),
          translateRefinementPoints(drag.startRefinementPoints, delta),
          translateBoundaryPoints(drag.startBoundaryPoints, delta),
        );
      },
      renderOverlay2D: ({ ctx, viewport }) => {
        viewportRef.current = viewport;
        viewportScaleRef.current = viewport.scale;
        if (movingToViewId) return;
        const viewId = selectedViewIdRef.current;
        const storedView = viewsRef.current.find((view) => view.id === viewId) ?? viewsRef.current[0] ?? null;
        const currentView =
          visualRun.status === "proposed" &&
          visualRun.targetViewId === storedView?.id &&
          visualRun.proposedView
            ? visualRun.proposedView
            : storedView;
        if (!currentView) return;
        const quad = currentView.projection_model.world_quad;
        const points = worldQuadPoints(quad).map((point) => viewport.worldToScreen(point));
        const contourPoints = deformedBoundaryScreenPoints(currentView, viewport);
        const rotateInfo = calibrationRotateHandleInfo(quad, viewport);
        const activeDrag = dragStateRef.current;
        const hover = hoverStateRef.current;
        ctx.save();
        ctx.globalAlpha = dragging ? 0.46 : 0.72;
        if (snapshotImage) {
          const renderedMesh = drawCalibrationImageMesh(ctx, snapshotImage, currentView, viewport);
          if (!renderedMesh) {
            const source = sourceRegionPixels(currentView, snapshotImage);
            drawImageTriangle(ctx, snapshotImage, [source.topLeft, source.topRight, source.bottomRight], [points[0], points[1], points[2]]);
            drawImageTriangle(ctx, snapshotImage, [source.topLeft, source.bottomRight, source.bottomLeft], [points[0], points[2], points[3]]);
          }
        } else {
          ctx.beginPath();
          ctx.moveTo(points[0].x, points[0].y);
          for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
          ctx.closePath();
          ctx.fillStyle = "rgba(56,189,248,0.18)";
          ctx.fill();
        }
        ctx.globalAlpha = 1;
        if (contourPoints.length >= 4) {
          ctx.beginPath();
          ctx.moveTo(contourPoints[0].x, contourPoints[0].y);
          for (const point of contourPoints.slice(1)) ctx.lineTo(point.x, point.y);
          ctx.closePath();
          ctx.lineWidth = 2;
          ctx.strokeStyle = "rgba(56,189,248,0.95)";
          ctx.stroke();
        }
        const centerHot = activeDrag?.kind === "move" || hover?.kind === "move";
        ctx.save();
        ctx.shadowColor = centerHot ? "rgba(56,189,248,0.42)" : "rgba(0,0,0,0)";
        ctx.shadowBlur = centerHot ? 12 : 0;
        ctx.beginPath();
        ctx.arc(rotateInfo.centerScreen.x, rotateInfo.centerScreen.y, centerHot ? 10 : 9, 0, Math.PI * 2);
        ctx.fillStyle = centerHot ? "rgba(14,165,233,0.96)" : "rgba(15,23,42,0.92)";
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.lineWidth = 2;
        ctx.strokeStyle = centerHot ? "rgba(226,232,240,0.96)" : "rgba(148,163,184,0.72)";
        ctx.stroke();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = "rgba(241,245,249,0.92)";
        ctx.beginPath();
        ctx.moveTo(rotateInfo.centerScreen.x - 5, rotateInfo.centerScreen.y);
        ctx.lineTo(rotateInfo.centerScreen.x + 5, rotateInfo.centerScreen.y);
        ctx.moveTo(rotateInfo.centerScreen.x, rotateInfo.centerScreen.y - 5);
        ctx.lineTo(rotateInfo.centerScreen.x, rotateInfo.centerScreen.y + 5);
        ctx.stroke();
        ctx.restore();
        const rotateHot = activeDrag?.kind === "rotate" || hover?.kind === "rotate";
        ctx.lineWidth = 2;
        ctx.strokeStyle = rotateHot ? "rgba(125,211,252,0.95)" : "rgba(226,232,240,0.72)";
        ctx.beginPath();
        ctx.moveTo(rotateInfo.centerScreen.x, rotateInfo.centerScreen.y);
        ctx.lineTo(rotateInfo.handleScreen.x, rotateInfo.handleScreen.y);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(rotateInfo.centerScreen.x, rotateInfo.centerScreen.y, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(241,245,249,0.82)";
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = "rgba(15,23,42,0.72)";
        ctx.stroke();
        ctx.shadowColor = rotateHot ? "rgba(56,189,248,0.48)" : "rgba(0,0,0,0)";
        ctx.shadowBlur = rotateHot ? 12 : 0;
        ctx.beginPath();
        ctx.arc(rotateInfo.handleScreen.x, rotateInfo.handleScreen.y, 8, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(15,23,42,0.96)";
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.lineWidth = 2;
        ctx.strokeStyle = rotateHot ? "rgba(56,189,248,0.98)" : "rgba(226,232,240,0.72)";
        ctx.stroke();
        if (activeDrag?.kind === "rotate") {
          const text = `${Math.round((activeDrag.snappedDelta * 180) / Math.PI)}°`;
          ctx.font = "12px system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          const metrics = ctx.measureText(text);
          const boxWidth = metrics.width + 18;
          const boxHeight = 24;
          const x0 = rotateInfo.handleScreen.x - boxWidth / 2;
          const y0 = rotateInfo.handleScreen.y - 28;
          ctx.fillStyle = "rgba(15,23,42,0.92)";
          ctx.strokeStyle = "rgba(148,163,184,0.42)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          roundRectPath(ctx, x0, y0, boxWidth, boxHeight, 999);
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = "rgba(241,245,249,0.96)";
          ctx.fillText(text, rotateInfo.handleScreen.x, y0 + boxHeight / 2);
        }
        for (const boundaryPoint of boundaryPointsForView(currentView)) {
          const point = viewport.worldToScreen(boundaryPoint.world);
          const hot =
            (activeDrag?.kind === "boundary" && activeDrag.pointId === boundaryPoint.id) ||
            (hover?.kind === "boundary" && hover.pointId === boundaryPoint.id);
          ctx.save();
          ctx.translate(point.x, point.y);
          ctx.shadowColor = hot ? "rgba(45,212,191,0.42)" : "rgba(0,0,0,0)";
          ctx.shadowBlur = hot ? 10 : 0;
          ctx.beginPath();
          ctx.moveTo(0, hot ? -9 : -8);
          ctx.lineTo(hot ? 9 : 8, 0);
          ctx.lineTo(0, hot ? 9 : 8);
          ctx.lineTo(hot ? -9 : -8, 0);
          ctx.closePath();
          ctx.fillStyle = hot ? "rgba(45,212,191,0.96)" : "rgba(20,184,166,0.86)";
          ctx.fill();
          ctx.shadowBlur = 0;
          ctx.lineWidth = hot ? 2.4 : 1.8;
          ctx.strokeStyle = "rgba(15,23,42,0.9)";
          ctx.stroke();
          ctx.restore();
        }
        if (hover?.kind === "new_boundary" && !activeDrag) {
          const point = viewport.worldToScreen(hover.world);
          ctx.save();
          ctx.setLineDash([5, 4]);
          ctx.lineWidth = 1.9;
          ctx.strokeStyle = "rgba(45,212,191,0.92)";
          ctx.fillStyle = "rgba(45,212,191,0.15)";
          ctx.beginPath();
          ctx.moveTo(point.x, point.y - 10);
          ctx.lineTo(point.x + 10, point.y);
          ctx.lineTo(point.x, point.y + 10);
          ctx.lineTo(point.x - 10, point.y);
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(point.x - 5, point.y);
          ctx.lineTo(point.x + 5, point.y);
          ctx.moveTo(point.x, point.y - 5);
          ctx.lineTo(point.x, point.y + 5);
          ctx.stroke();
          ctx.restore();
        }
        for (const refinementPoint of refinementPointsForView(currentView)) {
          const point = viewport.worldToScreen(refinementPoint.world);
          const hot =
            (activeDrag?.kind === "refinement" && activeDrag.pointId === refinementPoint.id) ||
            (hover?.kind === "refinement" && hover.pointId === refinementPoint.id);
          ctx.save();
          ctx.translate(point.x, point.y);
          ctx.rotate(Math.PI / 4);
          ctx.shadowColor = hot ? "rgba(251,191,36,0.42)" : "rgba(0,0,0,0)";
          ctx.shadowBlur = hot ? 10 : 0;
          ctx.beginPath();
          ctx.rect(hot ? -7 : -6, hot ? -7 : -6, hot ? 14 : 12, hot ? 14 : 12);
          ctx.fillStyle = hot ? "rgba(251,191,36,0.96)" : "rgba(250,204,21,0.84)";
          ctx.fill();
          ctx.shadowBlur = 0;
          ctx.lineWidth = hot ? 2.4 : 1.8;
          ctx.strokeStyle = "rgba(15,23,42,0.86)";
          ctx.stroke();
          ctx.restore();
        }
        if (hover?.kind === "new_refinement" && !activeDrag) {
          const point = viewport.worldToScreen(hover.world);
          ctx.save();
          ctx.setLineDash([4, 4]);
          ctx.lineWidth = 1.8;
          ctx.strokeStyle = "rgba(250,204,21,0.88)";
          ctx.fillStyle = "rgba(250,204,21,0.14)";
          ctx.beginPath();
          ctx.arc(point.x, point.y, 9, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(point.x - 5, point.y);
          ctx.lineTo(point.x + 5, point.y);
          ctx.moveTo(point.x, point.y - 5);
          ctx.lineTo(point.x, point.y + 5);
          ctx.stroke();
          ctx.restore();
        }
        points.forEach((point, index) => {
          const corner = CALIBRATION_CORNERS[index];
          const hot =
            (activeDrag?.kind === "corner" && activeDrag.corner === corner) ||
            (hover?.kind === "corner" && hover.corner === corner);
          ctx.beginPath();
          ctx.arc(point.x, point.y, hot ? 8 : 7, 0, Math.PI * 2);
          ctx.fillStyle = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
          ctx.fill();
          ctx.lineWidth = hot ? 2.5 : 2;
          ctx.strokeStyle = hot ? "rgba(226,232,240,0.95)" : "rgba(0,0,0,0.75)";
          ctx.stroke();
        });
        ctx.restore();
      },
      getCursor: () => {
        if (movingToViewId || visualInteractionLocked) return "default";
        if (dragStateRef.current) return "grabbing";
        if (hoverStateRef.current?.kind === "move") return "move";
        if (hoverStateRef.current?.kind === "new_refinement" || hoverStateRef.current?.kind === "new_boundary") return "crosshair";
        if (hoverStateRef.current) return "grab";
        return "default";
      },
    };
  }, [dragging, movingToViewId, snapshotImage, visualInteractionLocked, visualRun.proposedView, visualRun.status, visualRun.targetViewId]);

  const selectedQuality = selectedView ? summarizeCalibratedViewQuality(selectedView) : null;
  const compatibleRoles = selectedView?.stream_scope?.compatible_roles?.length ? selectedView.stream_scope.compatible_roles : ["main", "sub"];
  const invalidMeshViewLabels = views
    .filter((view) => !buildCalibrationMesh(view))
    .map((view, index) => view.label.trim() || t("ext.cameras.calibration.view_label", { index: index + 1 }));
  const unapprovedViewLabels = views
    .filter(
      (view) =>
        (visualRun.status === "proposed" && visualRun.targetViewId === view.id) ||
        (isVisualCalibrationView(view) &&
          (view.projection_quality?.status !== "ready" || view.projection_quality?.estimated === true)),
    )
    .map((view, index) => view.label.trim() || t("ext.cameras.calibration.view_label", { index: index + 1 }));
  const selectedCanBeReference = Boolean(
    selectedView &&
      summarizeCalibratedViewQuality(selectedView).status === "good" &&
      selectedView.projection_quality?.status === "ready" &&
      selectedView.projection_quality?.estimated !== true,
  );
  const visualRunForSelected = Boolean(selectedView && visualRun.targetViewId === selectedView.id);
  const visualResult = visualRunForSelected ? visualRun.result : null;
  const visualReason = visualResult?.reason
    ? t(
        `ext.cameras.visual_calibration.reason.${visualResult.reason}`,
        {},
        t("ext.cameras.visual_calibration.reason.generic"),
      )
    : null;
  const visualBusy =
    visualReferenceLoading ||
    visualRun.status === "analyzing" ||
    snapshotLoading ||
    movingToViewId !== null;

  return (
    <>
      {open ? (
        <SubModal
          open
          onClose={onClose}
          title={t("ext.cameras.calibration.title")}
          panelStyle={{ width: "min(1440px, calc(100vw - 28px))", height: "calc(100dvh - 28px)", maxHeight: "calc(100dvh - 28px)" }}
          bodyStyle={{ padding: 0, overflowX: "hidden", overflowY: "auto", display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
        >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 12, flex: 1, minHeight: 0 }}>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <div className="rowWrap" style={{ gap: 8, flexWrap: "wrap" }}>
            {(isPtzCamera || views.length > 1 ? views : views.slice(0, 1)).map((view, index) => {
              const quality = summarizeCalibratedViewQuality(view);
              const isSelected = selectedView?.id === view.id;
              const statusColor =
                quality.status === "good"
                  ? "rgba(34,197,94,0.92)"
                  : quality.status === "review"
                    ? "rgba(251,191,36,0.92)"
                    : "rgba(148,163,184,0.88)";
              return (
                <button
                  key={view.id}
                  type="button"
                  className="chipButton"
                  onClick={() => void selectView(view)}
                  aria-current={isSelected ? "true" : undefined}
                  disabled={calibrationInteractionLocked}
                  style={{
                    minWidth: 190,
                    justifyContent: "space-between",
                    borderColor: isSelected ? "rgba(56,189,248,0.55)" : "rgba(255,255,255,0.14)",
                    background: isSelected ? "rgba(56,189,248,0.10)" : undefined,
                  }}
                >
                  <span style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 2 }}>
                    <span>{view.label || t("ext.cameras.calibration.view_label", { index: index + 1 })}</span>
                    <span className="cardMeta">
                      {movingToViewId === view.id
                        ? t("ext.cameras.control.ptz_status_moving")
                        : quality.status === "good"
                        ? t("ext.cameras.control.quality_good")
                        : quality.status === "review"
                          ? t("ext.cameras.calibration.quality_estimated")
                          : t("ext.cameras.calibration.quality_incomplete")}
                    </span>
                  </span>
                  <span aria-hidden="true" style={{ width: 10, height: 10, borderRadius: 999, background: statusColor }} />
                </button>
              );
            })}
            {isPtzCamera ? (
              <button className="chipButton" type="button" onClick={() => addView()} disabled={calibrationInteractionLocked}>
                <i className="fa-solid fa-plus" aria-hidden="true" />
                <span>{t("ext.cameras.calibration.add_view")}</span>
              </button>
            ) : null}
            <button
              className="iconButton"
              type="button"
              onClick={removeSelectedView}
              aria-label={t("core.actions.delete")}
              disabled={!isPtzCamera || views.length <= 1 || calibrationInteractionLocked}
            >
              <i className="fa-solid fa-trash" aria-hidden="true" />
            </button>
          </div>
          <div className="rowWrap" style={{ justifyContent: "flex-end", alignItems: "center", gap: 8 }}>
            <div className="cardMeta" style={{ textAlign: "right" }}>
              {movingToViewId
                ? t("ext.cameras.control.ptz_status_moving")
                : snapshotLoading
                  ? t("ext.cameras.control.loading")
                : snapshotErrorMessage
                  ? snapshotErrorMessage
                  : selectedQuality?.status === "good"
                    ? t("ext.cameras.calibration.ready")
                    : t("ext.cameras.calibration.drag_help")}
            </div>
            <button
              className="iconButton"
              type="button"
              onClick={() => {
                if (selectedView) void selectView(selectedView);
              }}
              disabled={snapshotLoading || !cameraId || !selectedView || calibrationInteractionLocked}
              aria-label={t("ext.cameras.calibration.reposition_and_refresh")}
              title={t("ext.cameras.calibration.reposition_and_refresh")}
            >
              <i className="fa-solid fa-rotate-right" aria-hidden="true" />
            </button>
          </div>
        </div>

        {selectedView ? (
          <div style={{ display: "grid", gridTemplateColumns: isPtzCamera ? "minmax(260px, 1fr) auto" : "minmax(260px, 1fr)", gap: 10, alignItems: "end" }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="label" htmlFor="camera-calibration-view-name">
                {t("ext.cameras.control.position_name")}
              </label>
              <input
                id="camera-calibration-view-name"
                name="camera-calibration-view-name"
                className="input"
                value={selectedView.label}
                disabled={calibrationInteractionLocked}
                onChange={(event) => updateSelectedView((view) => ({ ...view, label: event.target.value }))}
              />
            </div>
            {isPtzCamera && !String(selectedView.pose_reference?.preset_token ?? "").trim() && !poseHasAbsoluteTarget(selectedView.pose_reference) ? (
              <button
                className="chipButton"
                type="button"
                onClick={() => {
                  if (!movingToViewIdRef.current) setPoseModalOpen(true);
                }}
                disabled={calibrationInteractionLocked}
              >
                <i className="fa-solid fa-video" aria-hidden="true" />
                <span>{t("ext.cameras.calibration.position_camera")}</span>
              </button>
            ) : null}
          </div>
        ) : null}

        {isPtzCamera && selectedView ? (
          <section className="card" style={{ marginBottom: 0 }} aria-busy={visualBusy}>
            <div
              className="cardBody"
              style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, padding: 12, flexWrap: "wrap" }}
            >
              <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 240, flex: "1 1 460px" }}>
                <strong>{t("ext.cameras.visual_calibration.title")}</strong>
                <span className="cardMeta">
                  {visualReference
                    ? t("ext.cameras.visual_calibration.reference_selected", { view: visualReference.view.label })
                    : t("ext.cameras.visual_calibration.description")}
                </span>
                {visualRun.status === "analyzing" && visualRunForSelected ? (
                  <span className="cardMeta" role="status" aria-live="polite">
                    {t("ext.cameras.visual_calibration.analyzing")}
                  </span>
                ) : null}
                {visualRun.status === "proposed" && visualRunForSelected ? (
                  <span className="cardMeta" role="status" aria-live="polite">
                    {t("ext.cameras.visual_calibration.proposal_ready")}
                  </span>
                ) : null}
                {visualRun.status === "accepted" && visualRunForSelected ? (
                  <span className="cardMeta" role="status" aria-live="polite">
                    {t("ext.cameras.visual_calibration.accepted")}
                  </span>
                ) : null}
                {visualRun.status === "rejected" && visualRunForSelected ? (
                  <span className="errorText" role="alert">
                    {visualReason ?? t("ext.cameras.visual_calibration.reason.generic")}
                  </span>
                ) : null}
                {visualRun.status === "error" && visualRunForSelected ? (
                  <span className="errorText" role="alert">
                    {visualRun.errorMessage}
                  </span>
                ) : null}
                {visualResult ? (
                  <details>
                    <summary className="cardMeta" style={{ cursor: "pointer" }}>
                      {t("ext.cameras.visual_calibration.quality_details")}
                    </summary>
                    <div className="cardMeta" style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 6 }}>
                      <span>
                        {t("ext.cameras.visual_calibration.matches", {
                          inliers: visualResult.quality.inliers,
                          matches: visualResult.quality.candidate_matches,
                        })}
                      </span>
                      <span>
                        {t("ext.cameras.visual_calibration.coverage", {
                          coverage: Math.round(100 * Math.min(visualResult.quality.source_coverage_ratio, visualResult.quality.target_coverage_ratio)),
                        })}
                      </span>
                      <span>
                        {t("ext.cameras.visual_calibration.overlap", {
                          overlap: Math.round(100 * visualResult.quality.overlap_ratio),
                        })}
                      </span>
                      {visualResult.quality.p95_reprojection_error_px !== null ? (
                        <span>
                          {t("ext.cameras.visual_calibration.reprojection_error", {
                            error: visualResult.quality.p95_reprojection_error_px.toFixed(1),
                          })}
                        </span>
                      ) : null}
                    </div>
                  </details>
                ) : null}
              </div>
              <div className="rowWrap" style={{ gap: 8, justifyContent: "flex-end" }}>
                {!visualReference ? (
                  <button
                    className="primaryButton"
                    type="button"
                    onClick={() => void useSelectedViewAsVisualReference()}
                    disabled={!selectedCanBeReference || visualBusy}
                  >
                    {visualReferenceLoading || snapshotLoading
                      ? t("ext.cameras.control.loading")
                      : t("ext.cameras.visual_calibration.use_reference")}
                  </button>
                ) : visualReference.view.id === selectedView.id ? (
                  <button className="primaryButton" type="button" onClick={() => addView({ visualTarget: true })} disabled={visualBusy}>
                    {t("ext.cameras.visual_calibration.add_target")}
                  </button>
                ) : visualRun.status === "proposed" && visualRunForSelected ? (
                  <>
                    <button className="chipButton" type="button" onClick={discardVisualCalibration} disabled={calibrationInteractionLocked}>
                      {t("ext.cameras.visual_calibration.discard")}
                    </button>
                    <button className="primaryButton" type="button" onClick={acceptVisualCalibration} disabled={calibrationInteractionLocked}>
                      {t("ext.cameras.visual_calibration.accept")}
                    </button>
                  </>
                ) : visualRun.status === "accepted" && visualRunForSelected ? (
                  <button
                    className="chipButton"
                    type="button"
                    onClick={() => void useSelectedViewAsVisualReference()}
                    disabled={!selectedCanBeReference || visualBusy}
                  >
                    {t("ext.cameras.visual_calibration.use_as_next_reference")}
                  </button>
                ) : (
                  <button
                    className="primaryButton"
                    type="button"
                    onClick={() => void analyzeSelectedViewVisually()}
                    disabled={visualBusy}
                  >
                    {visualRun.status === "analyzing" && visualRunForSelected
                      ? t("ext.cameras.visual_calibration.analyzing")
                      : visualRunForSelected && (visualRun.status === "rejected" || visualRun.status === "error")
                        ? t("ext.cameras.visual_calibration.retry")
                        : t("ext.cameras.visual_calibration.analyze")}
                  </button>
                )}
              </div>
            </div>
          </section>
        ) : null}

        <div style={{ position: "relative", flex: 1, minHeight: 280, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", overflow: "hidden", background: "rgba(0,0,0,0.20)" }}>
          <host.ui.Viewport2DReplica
            initialFit="content"
            interactionMode="navigate"
            minScale={2}
            session={toolSession}
            style={{ width: "100%", height: "100%" }}
          />
        </div>

        {invalidMeshViewLabels.length > 0 ? (
          <div className="errorText" role="status">
            {t(
              "ext.cameras.calibration.invalid_boundary_mesh",
              { views: invalidMeshViewLabels.join(", ") },
              "A deformação criou uma malha inválida em: {{views}}. Ajuste os pontos antes de salvar.",
            )}
          </div>
        ) : null}

        {unapprovedViewLabels.length > 0 ? (
          <div className="errorText" role="status">
            {t("ext.cameras.visual_calibration.unapproved_views", { views: unapprovedViewLabels.join(", ") })}
          </div>
        ) : null}

        {selectedView ? (
          <div className="card" style={{ marginBottom: 0 }}>
            <div className="cardBody" style={{ display: "flex", flexDirection: "column", gap: 10, padding: 12 }}>
              <button className="chipButton" type="button" onClick={() => setAdvancedOpen((value) => !value)} disabled={calibrationInteractionLocked} style={{ alignSelf: "flex-start" }}>
                {t("ext.cameras.calibration.advanced_streams")}
              </button>
              {advancedOpen ? (
                <div className="rowWrap" style={{ gap: 12 }}>
                  {["main", "sub", "zoom", "custom"].map((role) => (
                    <label key={role} className="cardMeta" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <input
                        type="checkbox"
                        checked={compatibleRoles.includes(role)}
                        disabled={calibrationInteractionLocked}
                        onChange={(event) => setCompatibleRole(role, event.target.checked)}
                      />
                      {role === "main"
                        ? t("ext.cameras.calibration.role_main")
                        : role === "sub"
                          ? t("ext.cameras.calibration.role_sub")
                          : role === "zoom"
                            ? t("ext.cameras.calibration.role_zoom")
                            : t("ext.cameras.calibration.role_custom")}
                    </label>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <button className="chipButton" type="button" onClick={onClose}>
            {t("core.actions.cancel")}
          </button>
          <button
            className="primaryButton"
            type="button"
            disabled={invalidMeshViewLabels.length > 0 || unapprovedViewLabels.length > 0 || calibrationInteractionLocked}
            onClick={() => {
              if (movingToViewIdRef.current) return;
              onSave(
                views.map((view, index) => ({
                  ...view,
                  label: view.label.trim() || t("ext.cameras.calibration.view_label", { index: index + 1 }),
                  pose_reference: normalizePoseReference(view.pose_reference),
                  stream_scope: {
                    compatible_roles:
                      view.stream_scope?.compatible_roles && view.stream_scope.compatible_roles.length
                        ? view.stream_scope.compatible_roles
                        : ["main", "sub"],
                    compatible_source_ids: view.stream_scope?.compatible_source_ids ?? [],
                  },
                  projection_model: {
                    ...view.projection_model,
                    visual_pose_signature: view.projection_model.visual_pose_signature
                      ? { ...view.projection_model.visual_pose_signature }
                      : null,
                    image_region: {
                      top_left: { ...view.projection_model.image_region.top_left },
                      bottom_right: { ...view.projection_model.image_region.bottom_right },
                    },
                    refinement: view.projection_model.refinement?.points.length
                      ? { model: "local_rbf_v1", points: cloneRefinementPoints(view.projection_model.refinement.points) }
                      : null,
                    boundary_refinement: view.projection_model.boundary_refinement?.points.length
                      ? { model: "edge_handles_v1", points: cloneBoundaryPoints(view.projection_model.boundary_refinement.points) }
                      : null,
                  },
                })),
              );
              onClose();
            }}
          >
            {t("core.actions.save")}
          </button>
        </div>
      </div>
        </SubModal>
      ) : null}
      <CameraPoseModal
        open={open && poseModalOpen}
        onClose={() => setPoseModalOpen(false)}
        i18n={i18n}
        cameraId={cameraId}
        cameraSources={cameraSources}
        selectedView={selectedView}
        onSnapshotRefreshRequested={refreshCurrentCalibrationSnapshot}
        onCapture={(poseReference, label, viewId) => {
          if (selectedViewIdRef.current !== viewId) return;
          updateSelectedView((view) =>
            invalidateVisualCalibrationApproval({
              ...view,
              label: label || view.label,
              pose_reference: poseReference,
            }),
          );
        }}
      />
    </>
  );
}

function CameraPoseModal({
  open,
  onClose,
  i18n,
  cameraId,
  cameraSources,
  selectedView,
  onSnapshotRefreshRequested,
  onCapture,
}: {
  open: boolean;
  onClose: () => void;
  i18n: HostI18n;
  cameraId: string;
  cameraSources: CameraSnapshotSourceOption[];
  selectedView: CameraCalibratedView | null;
  onSnapshotRefreshRequested: () => void;
  onCapture: (poseReference: CameraPoseReference, label: string | null | undefined, viewId: string) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<PanTiltZoomState | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeMoveId, setActiveMoveId] = useState<string | null>(null);
  type ContinuousMoveRegistration = {
    cameraId: string;
    sourceId: string;
    freshnessGeneration: number;
    vector: { pan: number; tilt: number; zoom: number };
    baselineVisualFingerprint: VisualStabilityFingerprint;
    mutationPromise: Promise<unknown> | null;
    motionIssued: boolean;
  };
  const moveTimerRef = useRef<number | null>(null);
  const moveVectorRef = useRef<{ pan: number; tilt: number; zoom: number } | null>(null);
  const moveFreshnessGenerationRef = useRef<number | null>(null);
  const moveSourceIdRef = useRef("");
  const moveCommandAbortRef = useRef<AbortController | null>(null);
  const continuousMoveRef = useRef<ContinuousMoveRegistration | null>(null);
  const moveStopInProgressRef = useRef(false);
  const moveStopCompletionRef = useRef<Promise<void> | null>(null);
  const snapshotRefreshAbortRef = useRef<AbortController | null>(null);
  const snapshotUrlRef = useRef<string | null>(null);
  const freshSnapshotFingerprintRef = useRef<VisualStabilityFingerprint | null>(null);
  const operationGenerationRef = useRef(0);
  const operationAbortRef = useRef<AbortController | null>(null);
  type PoseMovementRegistration = {
    generation: number;
    cameraId: string;
    sourceId: string;
    baselineVisualFingerprint: VisualStabilityFingerprint;
    mutationPromise: Promise<unknown> | null;
    motionIssued: boolean;
    stopIssued: boolean;
  };
  const poseMovementRef = useRef<PoseMovementRegistration | null>(null);
  const poseStopCompletionRef = useRef<Promise<void> | null>(null);
  const openRef = useRef(open);
  const selectedViewIdRef = useRef(selectedView?.id ?? null);
  const preferredPtzSourceIdRef = useRef("");
  const preferredSnapshotSourceIdRef = useRef("");
  const preferredSnapshotSourceId = useMemo(
    () => resolvePreferredCalibrationSnapshotSourceId(selectedView, cameraSources),
    [cameraSources, selectedView?.stream_scope],
  );
  const preferredPtzSourceId = useMemo(
    () => resolvePreferredCalibrationPtzSourceId(selectedView, cameraSources),
    [cameraSources, selectedView?.stream_scope],
  );
  type FreshSnapshotGate = {
    generation: number;
    status: "checking" | "ready" | "blocked";
    retryable: boolean;
    requiresStop: boolean;
    requiresVisualTransition: boolean;
  };
  const freshnessScope = open
    ? JSON.stringify([cameraId, selectedView?.id ?? "", preferredSnapshotSourceId, preferredPtzSourceId])
    : "";
  const freshnessScopeRef = useRef("");
  const freshnessGenerationRef = useRef(0);
  if (freshnessScopeRef.current !== freshnessScope) {
    freshnessScopeRef.current = freshnessScope;
    freshnessGenerationRef.current += 1;
  }
  const freshnessGeneration = freshnessGenerationRef.current;
  const [freshSnapshotGate, setFreshSnapshotGate] = useState<FreshSnapshotGate>({
    generation: -1,
    status: "checking",
    retryable: false,
    requiresStop: false,
    requiresVisualTransition: false,
  });
  const currentFreshSnapshotGate =
    freshSnapshotGate.generation === freshnessGeneration
      ? freshSnapshotGate
      : {
          generation: freshnessGeneration,
          status: "checking" as const,
          retryable: false,
          requiresStop: false,
          requiresVisualTransition: false,
        };
  const freshSnapshotReady = Boolean(freshnessScope) && currentFreshSnapshotGate.status === "ready";
  const freshSnapshotBlocked = currentFreshSnapshotGate.status === "blocked";
  openRef.current = open;
  selectedViewIdRef.current = selectedView?.id ?? null;
  preferredPtzSourceIdRef.current = preferredPtzSourceId;
  preferredSnapshotSourceIdRef.current = preferredSnapshotSourceId;
  const panTiltControls = useMemo(
    () => [
      {
        id: "up",
        icon: "fa-arrow-up",
        label: t("ext.cameras.control.tilt_up"),
        vector: { pan: 0, tilt: PTZ_TILT_SPEED, zoom: 0 },
      },
      {
        id: "left",
        icon: "fa-arrow-left",
        label: t("ext.cameras.control.pan_left"),
        vector: { pan: -PTZ_PAN_SPEED, tilt: 0, zoom: 0 },
      },
      {
        id: "stop",
        icon: "fa-stop",
        label: t("ext.cameras.control.stop"),
        vector: { pan: 0, tilt: 0, zoom: 0 },
      },
      {
        id: "right",
        icon: "fa-arrow-right",
        label: t("ext.cameras.control.pan_right"),
        vector: { pan: PTZ_PAN_SPEED, tilt: 0, zoom: 0 },
      },
      {
        id: "down",
        icon: "fa-arrow-down",
        label: t("ext.cameras.control.tilt_down"),
        vector: { pan: 0, tilt: -PTZ_TILT_SPEED, zoom: 0 },
      },
    ],
    [t],
  );
  const zoomControls = useMemo(
    () => [
      {
        id: "zoom-in",
        icon: "fa-plus",
        label: t("ext.cameras.control.zoom_in"),
        vector: { pan: 0, tilt: 0, zoom: PTZ_ZOOM_SPEED },
      },
      {
        id: "zoom-out",
        icon: "fa-minus",
        label: t("ext.cameras.control.zoom_out"),
        vector: { pan: 0, tilt: 0, zoom: -PTZ_ZOOM_SPEED },
      },
    ],
    [t],
  );

  useEffect(() => {
    snapshotUrlRef.current = snapshotUrl;
  }, [snapshotUrl]);

  type PoseOperation = {
    generation: number;
    freshnessGeneration: number;
    controller: AbortController;
    viewId: string;
    ptzSourceId: string;
    snapshotSourceId: string;
  };

  function claimRegisteredPoseMovementStop(): PoseMovementRegistration | null {
    const movement = poseMovementRef.current;
    poseMovementRef.current = null;
    if (!movement || !movement.motionIssued || movement.stopIssued) return null;
    movement.stopIssued = true;
    return movement;
  }

  function cancelRegisteredPoseMovement(sendStop: boolean): void {
    if (!sendStop) {
      poseMovementRef.current = null;
      return;
    }
    const movement = claimRegisteredPoseMovementStop();
    if (!movement) return;
    const stopCompletion = enqueueCameraPtzStop(
      movement.cameraId,
      movement.sourceId,
      movement.mutationPromise,
    );
    poseStopCompletionRef.current = stopCompletion;
    void stopCompletion.finally(() => {
      if (poseStopCompletionRef.current === stopCompletion) poseStopCompletionRef.current = null;
    });
  }

  function cancelPoseOperation(sendStop = true): void {
    operationAbortRef.current?.abort();
    operationAbortRef.current = null;
    operationGenerationRef.current += 1;
    cancelRegisteredPoseMovement(sendStop);
  }

  function blockFreshSnapshot(
    error: unknown,
    generation = freshnessGenerationRef.current,
    requiresStop = false,
    requiresVisualTransition = false,
  ): void {
    if (freshnessGenerationRef.current !== generation) return;
    setFreshSnapshotGate({
      generation,
      status: "blocked",
      retryable: !isCameraSnapshotFreshnessUnverifiableError(error),
      requiresStop,
      requiresVisualTransition,
    });
  }

  function beginPoseOperation(options?: {
    stopPreviousMovement?: boolean;
    allowFreshSnapshotCheck?: boolean;
  }): PoseOperation | null {
    const viewId = selectedViewIdRef.current;
    cancelPoseOperation(options?.stopPreviousMovement !== false);
    if (!freshSnapshotReady && !options?.allowFreshSnapshotCheck) return null;
    if (!openRef.current || !cameraId || !viewId || !preferredPtzSourceId || !preferredSnapshotSourceId) {
      setErrorMessage(t("ext.cameras.visual_calibration.source_unavailable"));
      return null;
    }
    const controller = new AbortController();
    const generation = operationGenerationRef.current;
    operationAbortRef.current = controller;
    return {
      generation,
      freshnessGeneration,
      controller,
      viewId,
      ptzSourceId: preferredPtzSourceId,
      snapshotSourceId: preferredSnapshotSourceId,
    };
  }

  function registerPoseMovement(
    operation: PoseOperation,
    baselineVisualFingerprint: VisualStabilityFingerprint,
  ): void {
    cancelRegisteredPoseMovement(true);
    poseMovementRef.current = {
      generation: operation.generation,
      cameraId,
      sourceId: operation.ptzSourceId,
      baselineVisualFingerprint,
      mutationPromise: null,
      motionIssued: false,
      stopIssued: false,
    };
  }

  function markPoseMovementIssued(operation: PoseOperation, mutationPromise: Promise<unknown>): void {
    const movement = poseMovementRef.current;
    if (movement?.generation !== operation.generation) return;
    movement.mutationPromise = mutationPromise;
    movement.motionIssued = true;
  }

  function releasePoseMovement(operation: PoseOperation): void {
    if (poseMovementRef.current?.generation === operation.generation) poseMovementRef.current = null;
  }

  function poseOperationIsCurrent(operation: PoseOperation): boolean {
    return (
      !operation.controller.signal.aborted &&
      operationGenerationRef.current === operation.generation &&
      freshnessGenerationRef.current === operation.freshnessGeneration &&
      openRef.current &&
      selectedViewIdRef.current === operation.viewId &&
      preferredPtzSourceIdRef.current === operation.ptzSourceId &&
      preferredSnapshotSourceIdRef.current === operation.snapshotSourceId
    );
  }

  const refreshStatus = useCallback(async (sourceId: string, signal?: AbortSignal) => {
    if (!cameraId || !sourceId || signal?.aborted) return null;
    try {
      const response = await fetchCameraPtzStatus(cameraId, sourceId, signal);
      if (signal?.aborted) return null;
      const nextStatus = response.status ?? null;
      setStatus(nextStatus);
      return nextStatus;
    } catch (error) {
      if (isAbortError(error) || signal?.aborted) return null;
      setErrorMessage(error instanceof Error ? error.message : String(error));
      return null;
    }
  }, [cameraId]);

  const refreshSnapshot = useCallback(async (sourceId: string, signal?: AbortSignal) => {
    if (!cameraId || !sourceId || signal?.aborted) return false;
    const requestFreshnessGeneration = freshnessGeneration;
    const localController = signal ? null : new AbortController();
    if (localController) {
      snapshotRefreshAbortRef.current?.abort();
      snapshotRefreshAbortRef.current = localController;
    }
    const effectiveSignal = signal ?? localController!.signal;
    try {
      const blob = await fetchCameraSnapshotWithRetry(cameraId, sourceId, effectiveSignal);
      const fingerprint = await visualStabilityFingerprintFromBlob(blob, effectiveSignal);
      if (!fingerprint) {
        throw new Error(t("ext.cameras.visual_calibration.snapshot_failed"));
      }
      if (
        effectiveSignal.aborted ||
        freshnessGenerationRef.current !== requestFreshnessGeneration
      ) {
        return false;
      }
      const nextUrl = URL.createObjectURL(blob);
      if (
        effectiveSignal.aborted ||
        freshnessGenerationRef.current !== requestFreshnessGeneration
      ) {
        URL.revokeObjectURL(nextUrl);
        return false;
      }
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return nextUrl;
      });
      freshSnapshotFingerprintRef.current = fingerprint;
      setFreshSnapshotGate({
        generation: requestFreshnessGeneration,
        status: "ready",
        retryable: false,
        requiresStop: false,
        requiresVisualTransition: false,
      });
      return true;
    } catch (error) {
      if (
        isAbortError(error) ||
        effectiveSignal.aborted ||
        freshnessGenerationRef.current !== requestFreshnessGeneration
      ) {
        return false;
      }
      blockFreshSnapshot(error, requestFreshnessGeneration);
      setErrorMessage(
        calibrationSnapshotErrorMessage(
          error,
          t("ext.cameras.visual_calibration.freshness_unverifiable"),
        ),
      );
      throw error;
    } finally {
      if (localController && snapshotRefreshAbortRef.current === localController) {
        snapshotRefreshAbortRef.current = null;
      }
    }
  }, [cameraId, freshnessGeneration, t]);

  const waitForPtzSettle = useCallback(async (
    sourceId: string,
    signal: AbortSignal,
    expectation?: PtzSettleExpectation,
  ) => {
    await waitForRetry(500, signal);
    let nextStatus: PanTiltZoomState | null = null;
    let previousStatus: PanTiltZoomState | null = null;
    let consecutiveIdleReads = 0;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      nextStatus = await refreshStatus(sourceId, signal);
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const moveStatus = normalizePtzMoveStatus(nextStatus?.move_status);
      const targetPoseIsContradicted = Boolean(
        expectation?.targetPose &&
          poseHasAbsoluteTarget(expectation.targetPose) &&
          ptzStatusContradictsPose(nextStatus, expectation.targetPose),
      );
      if (
        !targetPoseIsContradicted &&
        (moveStatus === "idle" ||
          (moveStatus === "unknown" && ptzTelemetryIsStable(previousStatus, nextStatus)))
      ) {
        consecutiveIdleReads += 1;
        if (consecutiveIdleReads >= 2) {
          if (ptzSettleRequiresVisualConfirmation(expectation)) {
            const visuallyStable = await waitForCameraVisualStability(
              cameraId,
              sourceId,
              signal,
              expectation?.baselineVisualFingerprint,
              expectation?.requireVisualTransition === true &&
                !ptzSettleTargetIsConfirmed(nextStatus, expectation),
            );
            if (!visuallyStable) return null;
          }
          await waitForRetry(350, signal);
          return nextStatus;
        }
      } else {
        consecutiveIdleReads = 0;
      }
      previousStatus = nextStatus;
      await waitForRetry(450, signal);
    }
    return null;
  }, [refreshStatus]);

  useEffect(() => {
    cancelPoseOperation();
    snapshotRefreshAbortRef.current?.abort();
    snapshotRefreshAbortRef.current = null;
    setStatus(null);
    freshSnapshotFingerprintRef.current = null;
    setFreshSnapshotGate({
      generation: freshnessGeneration,
      status: "checking",
      retryable: false,
      requiresStop: false,
      requiresVisualTransition: false,
    });
    if (!open) {
      setBusy(false);
      return;
    }
    if (!preferredPtzSourceId || !preferredSnapshotSourceId) {
      setBusy(false);
      setErrorMessage(t("ext.cameras.visual_calibration.source_unavailable"));
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    setErrorMessage(null);
    setBusy(true);
    void (async () => {
      try {
        const pendingPoseStop = poseStopCompletionRef.current;
        if (pendingPoseStop) await pendingPoseStop;
        const pendingMoveStop = moveStopCompletionRef.current;
        if (pendingMoveStop) await pendingMoveStop;
        await waitForPendingCameraPtzStops(cameraId);
        if (cancelled || controller.signal.aborted) return;
        const settledStatus = await waitForPtzSettle(preferredPtzSourceId, controller.signal, {
          requireTargetEvidence: true,
          requireVisualTransition: false,
        });
        if (cancelled || controller.signal.aborted) return;
        if (!settledStatus) {
          const error = new Error(t("ext.cameras.visual_calibration.camera_status_unavailable"));
          blockFreshSnapshot(error, freshnessGeneration, true);
          throw error;
        }
        await refreshSnapshot(preferredSnapshotSourceId, controller.signal);
      } catch (error) {
        if (!cancelled && !controller.signal.aborted && !isAbortError(error)) {
          setErrorMessage(
            calibrationSnapshotErrorMessage(
              error,
              t("ext.cameras.visual_calibration.freshness_unverifiable"),
            ),
          );
        }
      } finally {
        if (!cancelled && !controller.signal.aborted) setBusy(false);
      }
    })();
    const interval = window.setInterval(() => {
      if (!operationAbortRef.current && !moveVectorRef.current) {
        void refreshStatus(preferredPtzSourceId, controller.signal);
      }
    }, 1500);
    return () => {
      cancelled = true;
      controller.abort();
      snapshotRefreshAbortRef.current?.abort();
      snapshotRefreshAbortRef.current = null;
      cancelPoseOperation();
      window.clearInterval(interval);
    };
  }, [cameraId, freshnessGeneration, open, preferredPtzSourceId, preferredSnapshotSourceId, refreshSnapshot, refreshStatus, selectedView?.id, t, waitForPtzSettle]);

  useEffect(() => {
    if (open) return;
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    setActiveMoveId(null);
    moveVectorRef.current = null;
    moveSourceIdRef.current = "";
    moveCommandAbortRef.current?.abort();
    moveCommandAbortRef.current = null;
    if (moveTimerRef.current !== null) {
      window.clearInterval(moveTimerRef.current);
      moveTimerRef.current = null;
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const stopAfterLostControl = () => {
      if (moveVectorRef.current) void stopMove(false, { refresh: false });
    };
    const stopAfterVisibilityLoss = () => {
      if (document.visibilityState !== "visible") stopAfterLostControl();
    };
    window.addEventListener("blur", stopAfterLostControl);
    document.addEventListener("visibilitychange", stopAfterVisibilityLoss);
    return () => {
      window.removeEventListener("blur", stopAfterLostControl);
      document.removeEventListener("visibilitychange", stopAfterVisibilityLoss);
      stopAfterLostControl();
    };
  }, [freshnessGeneration, open, preferredPtzSourceId]);

  useEffect(() => {
    return () => {
      openRef.current = false;
      cancelPoseOperation();
      snapshotRefreshAbortRef.current?.abort();
      if (continuousMoveRef.current || moveVectorRef.current) {
        void stopMove(false, { refresh: false });
      }
      moveCommandAbortRef.current?.abort();
      if (snapshotUrlRef.current) URL.revokeObjectURL(snapshotUrlRef.current);
      if (moveTimerRef.current !== null) window.clearInterval(moveTimerRef.current);
    };
  }, []);

  function poseFromStatus(nextStatus: PanTiltZoomState | null): CameraPoseReference | null {
    if (!nextStatus) return null;
    return {
      pan: typeof nextStatus.pan === "number" && Number.isFinite(nextStatus.pan) ? nextStatus.pan : null,
      tilt: typeof nextStatus.tilt === "number" && Number.isFinite(nextStatus.tilt) ? nextStatus.tilt : null,
      zoom: typeof nextStatus.zoom === "number" && Number.isFinite(nextStatus.zoom) ? nextStatus.zoom : null,
      preset_token: null,
      preset_name: null,
    };
  }

  async function stopMove(force?: boolean, options?: { refresh?: boolean }) {
    if (moveStopInProgressRef.current) {
      const requestedFreshnessGeneration = freshnessGenerationRef.current;
      const pendingStop = moveStopCompletionRef.current;
      if (pendingStop) await pendingStop;
      if (
        force &&
        options?.refresh !== false &&
        openRef.current &&
        freshnessGenerationRef.current === requestedFreshnessGeneration
      ) {
        await stopMove(force, options);
      }
      return;
    }
    const continuousMovement = continuousMoveRef.current;
    continuousMoveRef.current = null;
    const vector = continuousMovement?.vector ?? moveVectorRef.current;
    const movementFreshnessGeneration =
      continuousMovement?.freshnessGeneration ??
      moveFreshnessGenerationRef.current ??
      freshnessGenerationRef.current;
    const registeredMovement = force ? claimRegisteredPoseMovementStop() : null;
    const registeredMovementSourceId = registeredMovement?.sourceId ?? "";
    const moveSourceId =
      continuousMovement?.sourceId ||
      moveSourceIdRef.current ||
      registeredMovementSourceId ||
      preferredPtzSourceId;
    const movementCameraId =
      continuousMovement?.cameraId || registeredMovement?.cameraId || cameraId;
    const movementBaselineVisualFingerprint =
      continuousMovement?.baselineVisualFingerprint ||
      registeredMovement?.baselineVisualFingerprint ||
      freshSnapshotFingerprintRef.current;
    const movementWasIssued = Boolean(
      continuousMovement?.motionIssued ||
      registeredMovement?.motionIssued ||
      currentFreshSnapshotGate.requiresVisualTransition
    );
    moveVectorRef.current = null;
    moveFreshnessGenerationRef.current = null;
    moveSourceIdRef.current = "";
    moveCommandAbortRef.current?.abort();
    moveCommandAbortRef.current = null;
    setActiveMoveId(null);
    if (moveTimerRef.current !== null) {
      window.clearInterval(moveTimerRef.current);
      moveTimerRef.current = null;
    }
    if (
      !movementCameraId ||
      (!force && !vector && !continuousMovement?.motionIssued)
    ) {
      return;
    }
    if (!moveSourceId) {
      setErrorMessage(t("ext.cameras.visual_calibration.source_unavailable"));
      return;
    }
    moveStopInProgressRef.current = true;
    const previousGlobalStop = pendingCameraPtzStopCompletion(movementCameraId);
    let resolveMoveStopCompletion!: () => void;
    const moveStopCompletion = new Promise<void>((resolve) => {
      resolveMoveStopCompletion = resolve;
    });
    moveStopCompletionRef.current = moveStopCompletion;
    trackCameraPtzStopCompletion(movementCameraId, moveStopCompletion);
    const refreshAfterStop = options?.refresh !== false && movementCameraId === cameraId;
    const operation = refreshAfterStop
      ? beginPoseOperation({
          stopPreviousMovement: false,
          allowFreshSnapshotCheck: true,
        })
      : null;
    if (refreshAfterStop) setBusy(true);
    try {
      if (previousGlobalStop) await previousGlobalStop.catch(() => undefined);
      const pendingPoseStop = poseStopCompletionRef.current;
      if (pendingPoseStop) await pendingPoseStop;
      if (continuousMovement?.mutationPromise) {
        await continuousMovement.mutationPromise.catch(() => undefined);
      }
      if (registeredMovement?.mutationPromise) {
        await registeredMovement.mutationPromise.catch(() => undefined);
      }
      await stopCameraPtz(movementCameraId, {
        source_id: moveSourceId,
        pan_tilt: force || Boolean(vector && (Math.abs(vector.pan) > 1e-6 || Math.abs(vector.tilt) > 1e-6)),
        zoom: force || Boolean(vector && Math.abs(vector.zoom) > 1e-6),
      });
      if (!refreshAfterStop) {
        if (freshnessGenerationRef.current === movementFreshnessGeneration) {
          blockFreshSnapshot(
            new Error("fresh snapshot required"),
            movementFreshnessGeneration,
            true,
            true,
          );
          setErrorMessage(t("ext.cameras.visual_calibration.freshness_refresh_required"));
        }
        return;
      }
      if (!operation || !poseOperationIsCurrent(operation)) return;
      if (!(await waitForPtzSettle(operation.ptzSourceId, operation.controller.signal, {
        baselineVisualFingerprint: movementBaselineVisualFingerprint,
        requireTargetEvidence: true,
        requireVisualTransition: movementWasIssued,
      }))) {
        throw new Error(t("ext.cameras.visual_calibration.camera_status_unavailable"));
      }
      if (!poseOperationIsCurrent(operation)) return;
      await refreshSnapshot(operation.snapshotSourceId, operation.controller.signal);
    } catch (error) {
      const failureFreshnessGeneration =
        operation?.freshnessGeneration ?? movementFreshnessGeneration;
      if (
        (!operation || poseOperationIsCurrent(operation)) &&
        freshnessGenerationRef.current === failureFreshnessGeneration &&
        !isAbortError(error)
      ) {
        blockFreshSnapshot(
          error,
          failureFreshnessGeneration,
          true,
          movementWasIssued,
        );
        setErrorMessage(
          calibrationSnapshotErrorMessage(
            error,
            t("ext.cameras.visual_calibration.freshness_unverifiable"),
          ),
        );
      }
    } finally {
      moveStopInProgressRef.current = false;
      resolveMoveStopCompletion();
      if (moveStopCompletionRef.current === moveStopCompletion) {
        moveStopCompletionRef.current = null;
      }
      if (refreshAfterStop) {
        if (operation && operationGenerationRef.current === operation.generation) {
          operationAbortRef.current = null;
          setBusy(false);
        } else if (!operation) {
          setBusy(false);
        }
      }
    }
  }

  function beginMove(moveId: string, vector: { pan: number; tilt: number; zoom: number }) {
    if (!cameraId || busy || !freshSnapshotReady || moveStopInProgressRef.current) return;
    const baselineVisualFingerprint = freshSnapshotFingerprintRef.current;
    if (!baselineVisualFingerprint) {
      const error = new Error(t("ext.cameras.visual_calibration.snapshot_failed"));
      blockFreshSnapshot(error);
      setErrorMessage(error.message);
      return;
    }
    if (!preferredPtzSourceId) {
      setErrorMessage(t("ext.cameras.visual_calibration.source_unavailable"));
      return;
    }
    cancelPoseOperation();
    moveCommandAbortRef.current?.abort();
    const controller = new AbortController();
    moveCommandAbortRef.current = controller;
    moveVectorRef.current = vector;
    moveFreshnessGenerationRef.current = freshnessGenerationRef.current;
    moveSourceIdRef.current = preferredPtzSourceId;
    const continuousMovement: ContinuousMoveRegistration = {
      cameraId,
      sourceId: preferredPtzSourceId,
      freshnessGeneration: freshnessGenerationRef.current,
      vector,
      baselineVisualFingerprint,
      mutationPromise: null,
      motionIssued: false,
    };
    continuousMoveRef.current = continuousMovement;
    setFreshSnapshotGate({
      generation: freshnessGenerationRef.current,
      status: "checking",
      retryable: false,
      requiresStop: false,
      requiresVisualTransition: false,
    });
    setErrorMessage(null);
    setActiveMoveId(moveId);
    const send = () => {
      if (
        controller.signal.aborted ||
        moveCommandAbortRef.current !== controller ||
        continuousMoveRef.current !== continuousMovement ||
        moveVectorRef.current !== vector ||
        continuousMovement.freshnessGeneration !== freshnessGenerationRef.current ||
        continuousMovement.mutationPromise
      ) {
        return;
      }
      const mutationPromise = runPtzMutationWithFence(() =>
        moveCameraPtz(cameraId, {
          source_id: continuousMovement.sourceId,
          ...vector,
          timeout_s: PTZ_MOVE_TIMEOUT_S,
        }),
      );
      continuousMovement.mutationPromise = mutationPromise;
      continuousMovement.motionIssued = true;
      void mutationPromise.then(
        () => {
          if (continuousMovement.mutationPromise === mutationPromise) {
            continuousMovement.mutationPromise = null;
          }
        },
        (error) => {
          if (continuousMovement.mutationPromise === mutationPromise) {
            continuousMovement.mutationPromise = null;
          }
          if (
            controller.signal.aborted ||
            continuousMoveRef.current !== continuousMovement ||
            continuousMovement.freshnessGeneration !== freshnessGenerationRef.current
          ) {
            return;
          }
          setErrorMessage(error instanceof Error ? error.message : String(error));
          void stopMove(true);
        },
      );
    };
    send();
    if (moveTimerRef.current !== null) window.clearInterval(moveTimerRef.current);
    moveTimerRef.current = window.setInterval(() => {
      send();
    }, PTZ_MOVE_REPEAT_MS);
  }

  async function useCurrentFraming() {
    if (!cameraId || busy || activeMoveId || !freshSnapshotReady) return;
    const operation = beginPoseOperation();
    if (!operation) return;
    setBusy(true);
    setErrorMessage(null);
    try {
      let pose = poseFromStatus(status);
      if (!pose || !poseHasAbsoluteTarget(pose)) {
        const compactViewId = operation.viewId.replace(/[^a-zA-Z0-9]/g, "");
        const anchor = await captureCameraPtzViewAnchor(
          cameraId,
          {
            source_id: operation.ptzSourceId,
            name: `TSV-${compactViewId.slice(-20) || "view"}`,
            idempotency_key: `camera-view-anchor:${operation.viewId}`,
          },
          operation.controller.signal,
        );
        if (!poseOperationIsCurrent(operation)) return;
        const token = String(anchor.token ?? "").trim();
        if (!token) throw new Error(t("ext.cameras.visual_calibration.view_capture_failed"));
        pose = {
          pan: typeof status?.pan === "number" && Number.isFinite(status.pan) ? status.pan : null,
          tilt: typeof status?.tilt === "number" && Number.isFinite(status.tilt) ? status.tilt : null,
          zoom: typeof status?.zoom === "number" && Number.isFinite(status.zoom) ? status.zoom : null,
          preset_token: token,
          preset_name: null,
        };
      }
      if (!poseOperationIsCurrent(operation)) return;
      onCapture(pose, null, operation.viewId);
      onSnapshotRefreshRequested();
      operationAbortRef.current = null;
      onClose();
    } catch (error) {
      if (poseOperationIsCurrent(operation) && !isAbortError(error)) {
        setErrorMessage(
          calibrationSnapshotErrorMessage(
            error,
            t("ext.cameras.visual_calibration.view_capture_failed"),
          ),
        );
      }
    } finally {
      if (operationGenerationRef.current === operation.generation) {
        operationAbortRef.current = null;
        setBusy(false);
      }
    }
  }

  function renderMoveButton(control: (typeof panTiltControls)[number] | (typeof zoomControls)[number]) {
    const stopCapturedMove = (element: HTMLButtonElement, pointerId: number) => {
      if (element.hasPointerCapture(pointerId)) element.releasePointerCapture(pointerId);
      if (control.id !== "stop" && moveVectorRef.current) void stopMove();
    };
    return (
      <button
        key={control.id}
        type="button"
        className="iconButton"
        aria-label={control.label}
        title={control.label}
        disabled={
          !preferredPtzSourceId ||
          ((busy || (!freshSnapshotReady && activeMoveId !== control.id)) && control.id !== "stop")
        }
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          if (control.id === "stop") {
            void stopMove(true);
            return;
          }
          event.currentTarget.setPointerCapture(event.pointerId);
          beginMove(control.id, control.vector);
        }}
        onPointerUp={(event) => stopCapturedMove(event.currentTarget, event.pointerId)}
        onPointerCancel={(event) => stopCapturedMove(event.currentTarget, event.pointerId)}
        onLostPointerCapture={() => {
          if (control.id !== "stop" && moveVectorRef.current) void stopMove();
        }}
        onKeyDown={(event) => {
          if (event.repeat || (event.key !== " " && event.key !== "Enter")) return;
          event.preventDefault();
          if (control.id === "stop") void stopMove(true);
          else beginMove(control.id, control.vector);
        }}
        onKeyUp={(event) => {
          if ((event.key === " " || event.key === "Enter") && control.id !== "stop" && moveVectorRef.current) {
            event.preventDefault();
            void stopMove();
          }
        }}
        style={{
          background: activeMoveId === control.id ? "rgba(56,189,248,0.14)" : undefined,
          touchAction: "none",
        }}
      >
        <i className={`fa-solid ${control.icon}`} aria-hidden="true" />
      </button>
    );
  }

  function requestClose(): void {
    cancelPoseOperation();
    setBusy(false);
    if (moveVectorRef.current) void stopMove(false, { refresh: false });
    onClose();
  }

  if (!open) return null;

  return (
    <SubModal open={open} onClose={requestClose} title={t("ext.cameras.calibration.position_camera")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }} aria-busy={busy}>
        <div className="card" style={{ marginBottom: 0 }}>
          <div className="cardBody" style={{ padding: 10 }}>
            {snapshotUrl ? (
              <img src={snapshotUrl} alt="" style={{ display: "block", width: "100%", maxHeight: "48vh", objectFit: "contain", borderRadius: 10 }} />
            ) : (
              <div className="cardMeta">{t("ext.cameras.control.loading")}</div>
            )}
          </div>
        </div>
        <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
          <button
            className="primaryButton"
            type="button"
            disabled={busy || !freshSnapshotReady || Boolean(activeMoveId) || !preferredPtzSourceId}
            onClick={() => void useCurrentFraming()}
          >
            {t("ext.cameras.calibration.capture_pose")}
          </button>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 18, alignItems: "flex-start" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 46px)", gap: 8 }}>
            <div aria-hidden="true" />
            {panTiltControls.slice(0, 1).map(renderMoveButton)}
            <div aria-hidden="true" />
            {panTiltControls.slice(1, 4).map(renderMoveButton)}
            <div aria-hidden="true" />
            {panTiltControls.slice(4).map(renderMoveButton)}
            <div aria-hidden="true" />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "46px", gap: 8 }}>
            {zoomControls.map(renderMoveButton)}
          </div>
        </div>
        <div className="cardMeta">
          {t("ext.cameras.control.pose_pan")}: {formatPtzTelemetryValue(status?.pan)} · {t("ext.cameras.control.pose_tilt")}:{" "}
          {formatPtzTelemetryValue(status?.tilt)} · {t("ext.cameras.control.pose_zoom")}: {formatPtzTelemetryValue(status?.zoom)}
        </div>
        {freshSnapshotBlocked && currentFreshSnapshotGate.retryable ? (
          <button
            className="chipButton"
            type="button"
            disabled={busy || !preferredSnapshotSourceId}
            onClick={() => {
              if (!preferredSnapshotSourceId) return;
              const retryFreshnessGeneration = freshnessGenerationRef.current;
              const retryRequiresStop = currentFreshSnapshotGate.requiresStop;
              const retryRequiresVisualTransition =
                currentFreshSnapshotGate.requiresVisualTransition;
              setBusy(true);
              setErrorMessage(null);
              setFreshSnapshotGate({
                generation: retryFreshnessGeneration,
                status: "checking",
                retryable: false,
                requiresStop: retryRequiresStop,
                requiresVisualTransition: retryRequiresVisualTransition,
              });
              if (retryRequiresStop) {
                void stopMove(true);
                return;
              }
              void refreshSnapshot(preferredSnapshotSourceId)
                .catch(() => false)
                .finally(() => {
                  if (
                    openRef.current &&
                    freshnessGenerationRef.current === retryFreshnessGeneration
                  ) {
                    setBusy(false);
                  }
                });
            }}
          >
            {t("ext.cameras.visual_calibration.retry")}
          </button>
        ) : null}
        {errorMessage ? <div className="errorText" role="alert">{errorMessage}</div> : null}
      </div>
    </SubModal>
  );
}

function normalizePoseReference(poseReference: CameraPoseReference | null | undefined): CameraPoseReference | null {
  if (!poseReference) return null;
  const pan = poseReference.pan ?? null;
  const tilt = poseReference.tilt ?? null;
  const zoom = poseReference.zoom ?? null;
  const presetToken = (poseReference.preset_token ?? "").trim();
  const presetName = (poseReference.preset_name ?? "").trim();
  if (pan === null && tilt === null && zoom === null && !presetToken && !presetName) return null;
  return {
    pan,
    tilt,
    zoom,
    preset_token: presetToken || null,
    preset_name: presetName || null,
  };
}

function CameraAction({ element, i18n, host }: { element: CompositionElement; i18n: HostI18n; host: ToposyncHost }): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const cameraId = readString(props.camera_id).trim();
  const LiveViewPlayer = host.ui.LiveViewPlayer;

  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const refreshAbortRef = React.useRef<AbortController | null>(null);

  const refresh = () => {
    refreshAbortRef.current?.abort();
    const controller = new AbortController();
    refreshAbortRef.current = controller;
    setLoading(true);
    setErrorMessage(null);
    fetchCameraSnapshot(cameraId, controller.signal)
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        setImageUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return url;
        });
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setErrorMessage(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        setLoading(false);
      });
  };

  useEffect(() => {
    if (!cameraId || LiveViewPlayer) return;
    refresh();
    return () => {
      refreshAbortRef.current?.abort();
      refreshAbortRef.current = null;
      setImageUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId, LiveViewPlayer]);

  if (!cameraId) {
    return <div className="cardBody">{t("ext.cameras.action.no_camera")}</div>;
  }

  return (
    <div>
      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <div className="label">{readString(props.camera_name) || cameraId}</div>
        {!LiveViewPlayer ? (
          <button className="chipButton" type="button" onClick={refresh} disabled={loading}>
            {loading ? t("ext.cameras.action.loading") : t("ext.cameras.action.refresh")}
          </button>
        ) : null}
      </div>

      <div className="sectionDivider" />

      {LiveViewPlayer ? (
        <div
          style={{
            position: "relative",
            height: "min(62vh, 560px)",
            minHeight: 320,
            borderRadius: 14,
            overflow: "hidden",
            border: "1px solid rgba(255,255,255,0.14)",
            background: "rgba(0,0,0,0.35)",
          }}
        >
          <LiveViewPlayer cameraId={cameraId} context="large" style={{ width: "100%", height: "100%" }} />
        </div>
      ) : errorMessage ? (
        <div className="card">
          <div className="cardBody">{errorMessage}</div>
        </div>
      ) : imageUrl ? (
        <img
          src={imageUrl}
          alt={readString(props.camera_name) || cameraId}
          style={{
            width: "100%",
            borderRadius: 14,
            border: "1px solid rgba(255,255,255,0.14)",
            background: "rgba(0,0,0,0.35)",
          }}
        />
      ) : (
        <div className="card">
          <div className="cardBody">{t("ext.cameras.action.loading")}</div>
        </div>
      )}
    </div>
  );
}
