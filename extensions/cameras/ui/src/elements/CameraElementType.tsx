import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  fetchCameraPtzPresets,
  fetchCameraPtzStatus,
  fetchCameraSnapshot,
  fetchCamerasIndex,
  gotoCameraPtzPreset,
  mapControlPoint,
  moveCameraPtz,
  moveCameraPtzAbsolute,
  stopCameraPtz,
} from "../api/camerasApi";
import { CAMERA_ELEMENT_TYPE_ID, CONTROL_POINT_COLORS } from "../constants";
import {
  calibratedViewsFromControlPointSets,
  controlPointSetFromCalibratedView,
  createDefaultCalibratedView,
  createDefaultControlPointSet,
  createUniqueId,
  defaultImageRegion,
  duplicateControlPointSetForNewView,
  labelForIndex,
  readCalibratedViews,
  readControlPointSets,
  readRecord,
  readString,
  summarizeCalibratedViewQuality,
  summarizeControlPointSetQuality,
} from "../parsing";
import type {
  CameraCalibratedView,
  CameraConnectionType,
  CameraControlPoint,
  CameraControlPointSet,
  CameraProjectionCornerKey,
  CameraProjectionRefinementPoint,
  CameraProjectionWorldQuad,
  CameraPoseReference,
  CameraPtzPreset,
  CameraSourceConfig,
  CameraSourceRole,
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
const PTZ_STATUS_REFRESH_MS = 2200;
const SNAPSHOT_REFRESH_MS = 8000;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
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

const CALIBRATION_SNAPSHOT_ROLE_ORDER: CameraSourceRole[] = ["sub", "main", "custom", "zoom"];

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isTransientSnapshotError(error: unknown): boolean {
  if (isAbortError(error)) return false;
  const message = error instanceof Error ? error.message : String(error);
  return /failed to fetch|networkerror|load failed|ecconnrefused|temporarily unavailable/i.test(message);
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

async function fetchCameraSnapshotWithRetry(
  cameraId: string,
  sourceId: string,
  signal: AbortSignal,
  attempts = 3,
): Promise<Blob> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await fetchCameraSnapshot(cameraId, sourceId, signal);
    } catch (error) {
      if (isAbortError(error)) throw error;
      lastError = error;
      if (attempt >= attempts - 1 || !isTransientSnapshotError(error)) break;
      await waitForRetry(450 + attempt * 650, signal);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError ?? "Snapshot failed"));
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
  if (!enabledVideoSources.length) return "";

  const compatibleSourceIds = (view?.stream_scope?.compatible_source_ids ?? []).map((item) => String(item || "").trim()).filter(Boolean);
  for (const sourceId of compatibleSourceIds) {
    if (enabledVideoSources.some((source) => source.id === sourceId)) return sourceId;
  }

  const compatibleRoles = view?.stream_scope?.compatible_roles?.length
    ? view.stream_scope.compatible_roles.map((role) => String(role || "").trim())
    : ["main", "sub"];
  const explicitZoomAllowed = compatibleRoles.includes("zoom") || compatibleSourceIds.length > 0;
  const allowedRoles = new Set(compatibleRoles.filter((role) => explicitZoomAllowed || role !== "zoom"));
  const roleCandidates = enabledVideoSources.filter((source) => allowedRoles.has(source.role));
  const candidates = roleCandidates.length ? roleCandidates : enabledVideoSources;

  const defaultCandidate = candidates.find((source) => source.is_default);
  if (defaultCandidate && allowedRoles.has(defaultCandidate.role)) return defaultCandidate.id;

  const sorted = [...candidates].sort((left, right) => {
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
  const ptzSources = sources.filter((source) => source.enabled !== false && source.kind === "video" && source.has_ptz === true);
  if (!ptzSources.length) return "";

  const compatibleSourceIds = (view?.stream_scope?.compatible_source_ids ?? []).map((item) => String(item || "").trim()).filter(Boolean);
  for (const sourceId of compatibleSourceIds) {
    if (ptzSources.some((source) => source.id === sourceId)) return sourceId;
  }

  const compatibleRoles = view?.stream_scope?.compatible_roles?.length
    ? view.stream_scope.compatible_roles.map((role) => String(role || "").trim())
    : ["main", "sub"];
  const explicitZoomAllowed = compatibleRoles.includes("zoom") || compatibleSourceIds.length > 0;
  const allowedRoles = new Set(compatibleRoles.filter((role) => explicitZoomAllowed || role !== "zoom"));
  const roleCandidates = ptzSources.filter((source) => allowedRoles.has(source.role));
  const candidates = roleCandidates.length ? roleCandidates : ptzSources;

  const defaultCandidate = candidates.find((source) => source.is_default);
  if (defaultCandidate) return defaultCandidate.id;

  const sorted = [...candidates].sort((left, right) => {
    const leftIndex = CALIBRATION_SNAPSHOT_ROLE_ORDER.indexOf(left.role);
    const rightIndex = CALIBRATION_SNAPSHOT_ROLE_ORDER.indexOf(right.role);
    return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
  });
  return sorted[0]?.id ?? "";
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
    renderEditorModal: ({ element, update, remove, close }) => (
      <CameraEditor element={element} update={update} remove={remove} close={close} i18n={i18n} host={host} />
    ),
    renderActionModal: ({ element }) => <CameraAction element={element} i18n={i18n} host={host} />,
  };
}

function CameraEditor({
  element,
  update,
  remove,
  close,
  i18n,
  host,
}: {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
  host: ToposyncHost;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const selectedCameraId = readString(props.camera_id).trim();
  const existingControlPointSets = useMemo(() => readControlPointSets(props.control_point_sets), [props.control_point_sets]);
  const existingCalibratedViews = useMemo(() => {
    const direct = readCalibratedViews(props.calibrated_views, element.position);
    return direct.length ? direct : calibratedViewsFromControlPointSets(existingControlPointSets);
  }, [element.position, existingControlPointSets, props.calibrated_views]);
  const readySets = useMemo(
    () => existingCalibratedViews.filter((item) => summarizeCalibratedViewQuality(item).status !== "incomplete").length,
    [existingCalibratedViews],
  );
  const totalSets = existingCalibratedViews.length;
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
const MAX_CALIBRATION_REFINEMENT_POINTS = 24;
const CALIBRATION_PREVIEW_GRID_DIVISIONS = 34;
const LOCAL_REFINEMENT_SIGMA_UV = 0.22;
const LOCAL_REFINEMENT_EDGE_LOW = 0.015;
const LOCAL_REFINEMENT_EDGE_HIGH = 0.12;

type CalibrationDragState =
  | {
      kind: "move";
      startWorld: { x: number; z: number };
      startQuad: CameraProjectionWorldQuad;
      startRefinementPoints: CameraProjectionRefinementPoint[];
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
      snappedDelta: number;
    }
  | {
      kind: "refinement";
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

function cloneRefinementPoints(points: CameraProjectionRefinementPoint[]): CameraProjectionRefinementPoint[] {
  return points.map((point) => ({
    id: point.id,
    image: { ...point.image },
    world: { ...point.world },
  }));
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

function mapCalibrationImageToWorld(
  view: CameraCalibratedView,
  hImageToWorld: number[],
  image: { x: number; y: number },
): { x: number; z: number } | null {
  const base = mapCalibrationHomography(hImageToWorld, image);
  if (!base) return null;
  const delta = localRefinementDelta(view, hImageToWorld, image);
  return { x: base.x + delta.x, z: base.y + delta.z };
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
  const getVertex = (gx: number, gy: number): number | null => {
    const key = `${gx}:${gy}`;
    const existing = vertexByGrid.get(key);
    if (existing != null) return existing;
    const image = {
      x: left + ((right - left) * gx) / divisions,
      y: top + ((bottom - top) * gy) / divisions,
    };
    const world = mapCalibrationImageToWorld(view, hImageToWorld, image);
    if (!world) return null;
    const index = vertices.length;
    vertices.push({ image, world });
    vertexByGrid.set(key, index);
    return index;
  };

  for (let gy = 0; gy < divisions; gy += 1) {
    for (let gx = 0; gx < divisions; gx += 1) {
      const a = getVertex(gx, gy);
      const b = getVertex(gx + 1, gy);
      const c = getVertex(gx + 1, gy + 1);
      const d = getVertex(gx, gy + 1);
      if (a == null || b == null || c == null || d == null) continue;
      indices.push(a, b, c, a, c, d);
    }
  }
  return indices.length >= 3 ? { vertices, indices } : null;
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
  const [importingPresets, setImportingPresets] = useState(false);
  const [movingToViewId, setMovingToViewId] = useState<string | null>(null);
  const snapshotAbortRef = useRef<AbortController | null>(null);
  const snapshotUrlRef = useRef<string | null>(null);
  const selectedViewIdRef = useRef<string | null>(null);
  const viewsRef = useRef<CameraCalibratedView[]>([]);
  const dragStateRef = useRef<CalibrationDragState | null>(null);
  const hoverStateRef = useRef<CalibrationHoverState>(null);
  const viewportRef = useRef<Viewport2DContext | null>(null);
  const viewportScaleRef = useRef(30);
  const viewSelectionRequestRef = useRef(0);

  const selectedView = useMemo(
    () => views.find((view) => view.id === selectedViewId) ?? views[0] ?? null,
    [selectedViewId, views],
  );
  const preferredSnapshotSourceId = useMemo(
    () => resolvePreferredCalibrationSnapshotSourceId(selectedView, cameraSources),
    [cameraSources, selectedView?.stream_scope],
  );
  const preferredSnapshotSource = useMemo(
    () => cameraSources.find((source) => source.id === preferredSnapshotSourceId) ?? null,
    [cameraSources, preferredSnapshotSourceId],
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
    const baseViews = initialViews.length
      ? initialViews.map((view) => ({
          ...view,
          pose_reference: view.pose_reference ? { ...view.pose_reference } : null,
          stream_scope: {
            compatible_roles:
              view.stream_scope?.compatible_roles && view.stream_scope.compatible_roles.length
                ? [...view.stream_scope.compatible_roles]
                : ["main", "sub"],
            compatible_source_ids: [...(view.stream_scope?.compatible_source_ids ?? [])],
          },
          projection_model: {
            ...view.projection_model,
            image_region: defaultImageRegion(),
            world_quad: cloneWorldQuad(view.projection_model.world_quad),
            refinement: view.projection_model.refinement?.points.length
              ? { model: "local_rbf_v1", points: cloneRefinementPoints(view.projection_model.refinement.points) }
              : null,
          },
          projection_quality: { ...(view.projection_quality ?? {}) },
        }))
      : [createDefaultCalibratedView(0, element.position, { label: t("ext.cameras.calibration.default_view") })];
    setViews(baseViews);
    setSelectedViewId(baseViews[0]?.id ?? null);
    if (baseViews[0]) void selectView(baseViews[0]);
  }, [element.position, initialViews, open, t]);

  const loadCalibrationSnapshotFromSourceAsync = useCallback(
    async (sourceId: string, sourceName: string) => {
      if (!cameraId) return;
      snapshotAbortRef.current?.abort();
      const controller = new AbortController();
      snapshotAbortRef.current = controller;
      setSnapshotLoading(true);
      setSnapshotErrorMessage(null);
      try {
        const blob = await fetchCameraSnapshotWithRetry(cameraId, sourceId, controller.signal);
        const nextUrl = URL.createObjectURL(blob);
        setSnapshotUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return nextUrl;
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : String(error);
        setSnapshotErrorMessage(sourceName ? `${sourceName}: ${message}` : message);
      } finally {
        if (!controller.signal.aborted) setSnapshotLoading(false);
      }
    },
    [cameraId],
  );

  const loadCalibrationSnapshotFromSource = useCallback(
    (sourceId: string, sourceName: string) => {
      void loadCalibrationSnapshotFromSourceAsync(sourceId, sourceName);
      return () => snapshotAbortRef.current?.abort();
    },
    [loadCalibrationSnapshotFromSourceAsync],
  );

  const loadCalibrationSnapshot = useCallback(() => {
    if (!cameraId) return () => undefined;
    return loadCalibrationSnapshotFromSource(preferredSnapshotSourceId, snapshotSourceDisplayName(preferredSnapshotSource));
  }, [cameraId, loadCalibrationSnapshotFromSource, preferredSnapshotSource, preferredSnapshotSourceId]);

  const loadCalibrationSnapshotForViewAsync = useCallback(
    async (view: CameraCalibratedView | null) => {
      const sourceId = resolvePreferredCalibrationSnapshotSourceId(view, cameraSources);
      const source = cameraSources.find((item) => item.id === sourceId) ?? null;
      await loadCalibrationSnapshotFromSourceAsync(sourceId, snapshotSourceDisplayName(source));
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

  useEffect(() => {
    if (!open) {
      viewSelectionRequestRef.current += 1;
      snapshotAbortRef.current?.abort();
      setSnapshotErrorMessage(null);
      setSnapshotLoading(false);
      setSnapshotImage(null);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
      setDragging(false);
      setMovingToViewId(null);
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
      snapshotAbortRef.current?.abort();
      if (snapshotUrlRef.current) URL.revokeObjectURL(snapshotUrlRef.current);
    };
  }, []);

  function updateSelectedView(updater: (view: CameraCalibratedView) => CameraCalibratedView) {
    const currentId = selectedViewIdRef.current;
    if (!currentId) return;
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
    status: "ready" | "estimated" = "ready",
  ) {
    updateSelectedView((view) =>
      withRefinementPoints(
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
        status,
      ),
    );
  }

  function updateSelectedRefinementPoints(updater: (points: CameraProjectionRefinementPoint[]) => CameraProjectionRefinementPoint[]) {
    updateSelectedView((view) => withRefinementPoints(view, updater(cloneRefinementPoints(refinementPointsForView(view)))));
  }

  async function waitForPtzToSettle(sourceId: string): Promise<PanTiltZoomState | null> {
    await sleep(750);
    let latestStatus: PanTiltZoomState | null = null;
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try {
        const response = await fetchCameraPtzStatus(cameraId, sourceId);
        latestStatus = response.status ?? latestStatus;
      } catch (error) {
        setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
        break;
      }
      if (attempt > 0 && normalizePtzMoveStatus(latestStatus?.move_status) !== "moving") break;
      await sleep(450);
    }
    await sleep(350);
    return latestStatus;
  }

  async function selectView(view: CameraCalibratedView) {
    const requestId = viewSelectionRequestRef.current + 1;
    viewSelectionRequestRef.current = requestId;
    selectedViewIdRef.current = view.id;
    setSelectedViewId(view.id);
    setMovingToViewId(view.id);
    setSnapshotErrorMessage(null);
    setSnapshotImage(null);
    try {
      if (isPtzCamera && cameraId) {
        const poseReference = view.pose_reference ?? null;
        const presetToken = String(poseReference?.preset_token ?? "").trim();
        const sourceId = resolvePreferredCalibrationPtzSourceId(view, cameraSources);
        if (presetToken) {
          await gotoCameraPtzPreset(cameraId, presetToken, sourceId);
          await waitForPtzToSettle(sourceId);
        } else if (poseHasAbsoluteTarget(poseReference)) {
          await moveCameraPtzAbsolute(cameraId, absoluteMovePayloadForPose(sourceId, poseReference!));
          await waitForPtzToSettle(sourceId);
        }
      }
      if (viewSelectionRequestRef.current === requestId) await loadCalibrationSnapshotForViewAsync(view);
    } catch (error) {
      if (viewSelectionRequestRef.current === requestId) setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (viewSelectionRequestRef.current === requestId) setMovingToViewId(null);
    }
  }

  function addView() {
    setViews((previous) => {
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
      setSelectedViewId(nextView.id);
      return [...previous, nextView];
    });
  }

  function removeSelectedView() {
    if (!selectedViewId || views.length <= 1) return;
    setViews((previous) => {
      const filtered = previous.filter((view) => view.id !== selectedViewId);
      setSelectedViewId(filtered[0]?.id ?? null);
      return filtered;
    });
  }

  async function importPresetViews() {
    if (!cameraId || !isPtzCamera) return;
    setImportingPresets(true);
    setSnapshotErrorMessage(null);
    try {
      const response = await fetchCameraPtzPresets(cameraId, resolvePreferredCalibrationPtzSourceId(selectedView, cameraSources));
      const presets = Array.isArray(response.presets) ? response.presets : [];
      setViews((previous) => {
        const existingTokens = new Set(previous.map((view) => String(view.pose_reference?.preset_token ?? "").trim()).filter(Boolean));
        const source = selectedView ?? previous[0] ?? null;
        const additions: CameraCalibratedView[] = [];
        for (const preset of presets) {
          const token = String(preset.token || "").trim();
          if (!token || existingTokens.has(token)) continue;
          const nextView = createDefaultCalibratedView(previous.length + additions.length, element.position, {
            label: String(preset.name || "").trim() || token,
            poseReference: {
              pan: typeof preset.pan === "number" && Number.isFinite(preset.pan) ? preset.pan : null,
              tilt: typeof preset.tilt === "number" && Number.isFinite(preset.tilt) ? preset.tilt : null,
              zoom: typeof preset.zoom === "number" && Number.isFinite(preset.zoom) ? preset.zoom : null,
              preset_token: token,
              preset_name: String(preset.name || "").trim() || token,
            },
          });
          if (source?.stream_scope) {
            nextView.stream_scope = {
              compatible_roles: source.stream_scope.compatible_roles?.length ? [...source.stream_scope.compatible_roles] : ["main", "sub"],
              compatible_source_ids: [...(source.stream_scope.compatible_source_ids ?? [])],
            };
          }
          additions.push(nextView);
        }
        if (additions.length > 0) setSelectedViewId(additions[0].id);
        return [...previous, ...additions];
      });
    } catch (error) {
      setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setImportingPresets(false);
    }
  }

  function setCompatibleRole(role: string, enabled: boolean) {
    updateSelectedView((view) => {
      const current = view.stream_scope?.compatible_roles?.length ? view.stream_scope.compatible_roles : ["main", "sub"];
      const next = enabled ? Array.from(new Set([...current, role])) : current.filter((item) => item !== role);
      return {
        ...view,
        stream_scope: {
          compatible_roles: next,
          compatible_source_ids: [...(view.stream_scope?.compatible_source_ids ?? [])],
        },
      };
    });
  }

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
        if (movingToViewId || event.kind !== "down" || event.button !== 0) return false;
        const viewId = selectedViewIdRef.current;
        const currentView = viewsRef.current.find((view) => view.id === viewId) ?? viewsRef.current[0] ?? null;
        if (!currentView) return false;
        return Boolean(resolveHoverState(event, currentView, viewportRef.current));
      },
      onPointerEvent: (event: EditorToolPointerEvent) => {
        if (movingToViewId) return;
        const viewId = selectedViewIdRef.current;
        const currentView = viewsRef.current.find((view) => view.id === viewId) ?? viewsRef.current[0] ?? null;
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
              snappedDelta: 0,
            };
          } else if (hover?.kind === "move") {
            dragStateRef.current = {
              kind: "move",
              startWorld: { x: event.world.x, z: event.world.z },
              startQuad: cloneWorldQuad(quad),
              startRefinementPoints: cloneRefinementPoints(refinementPointsForView(currentView)),
            };
          } else if (hover?.kind === "refinement") {
            dragStateRef.current = {
              kind: "refinement",
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
        const delta = {
          x: event.world.x - drag.startWorld.x,
          z: event.world.z - drag.startWorld.z,
        };
        updateSelectedProjection(translateWorldQuad(drag.startQuad, delta), translateRefinementPoints(drag.startRefinementPoints, delta));
      },
      renderOverlay2D: ({ ctx, viewport }) => {
        viewportRef.current = viewport;
        viewportScaleRef.current = viewport.scale;
        if (movingToViewId) return;
        const viewId = selectedViewIdRef.current;
        const currentView = viewsRef.current.find((view) => view.id === viewId) ?? viewsRef.current[0] ?? null;
        if (!currentView) return;
        const quad = currentView.projection_model.world_quad;
        const points = worldQuadPoints(quad).map((point) => viewport.worldToScreen(point));
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
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
        ctx.closePath();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "rgba(56,189,248,0.95)";
        ctx.stroke();
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
        if (movingToViewId) return "default";
        if (dragStateRef.current) return "grabbing";
        if (hoverStateRef.current?.kind === "move") return "move";
        if (hoverStateRef.current?.kind === "new_refinement") return "crosshair";
        if (hoverStateRef.current) return "grab";
        return "default";
      },
    };
  }, [dragging, movingToViewId, snapshotImage]);

  if (!open) return null;

  const selectedQuality = selectedView ? summarizeCalibratedViewQuality(selectedView) : null;
  const compatibleRoles = selectedView?.stream_scope?.compatible_roles?.length ? selectedView.stream_scope.compatible_roles : ["main", "sub"];

  return (
    <SubModal
      open={open}
      onClose={onClose}
      title={t("ext.cameras.calibration.title")}
      panelStyle={{ width: "min(1440px, calc(100vw - 28px))", height: "calc(100vh - 28px)", maxHeight: "calc(100vh - 28px)" }}
      bodyStyle={{ padding: 0, overflow: "hidden", display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
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
              <>
                <button className="chipButton" type="button" onClick={addView}>
                  <i className="fa-solid fa-plus" aria-hidden="true" />
                  <span>{t("ext.cameras.calibration.add_view")}</span>
                </button>
                <button className="chipButton" type="button" onClick={() => void importPresetViews()} disabled={importingPresets}>
                  {importingPresets ? t("ext.cameras.control.loading") : t("ext.cameras.calibration.import_presets")}
                </button>
              </>
            ) : null}
            <button
              className="iconButton"
              type="button"
              onClick={removeSelectedView}
              aria-label={t("core.actions.delete")}
              disabled={!isPtzCamera || views.length <= 1}
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
              onClick={loadCalibrationSnapshot}
              disabled={snapshotLoading || !cameraId}
              aria-label={t("ext.cameras.control.refresh_snapshot")}
              title={t("ext.cameras.control.refresh_snapshot")}
            >
              <i className="fa-solid fa-rotate-right" aria-hidden="true" />
            </button>
          </div>
        </div>

        {selectedView ? (
          <div style={{ display: "grid", gridTemplateColumns: isPtzCamera ? "minmax(260px, 1fr) auto" : "minmax(260px, 1fr)", gap: 10, alignItems: "end" }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="label">{t("ext.cameras.control.position_name")}</label>
              <input
                className="input"
                value={selectedView.label}
                onChange={(event) => updateSelectedView((view) => ({ ...view, label: event.target.value }))}
              />
            </div>
            {isPtzCamera ? (
              <button className="chipButton" type="button" onClick={() => setPoseModalOpen(true)}>
                <i className="fa-solid fa-video" aria-hidden="true" />
                <span>{t("ext.cameras.calibration.position_camera")}</span>
              </button>
            ) : null}
          </div>
        ) : null}

        <div style={{ position: "relative", flex: 1, minHeight: 0, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", overflow: "hidden", background: "rgba(0,0,0,0.20)" }}>
          <host.ui.Viewport2DReplica
            initialFit="content"
            interactionMode="navigate"
            minScale={2}
            session={toolSession}
            style={{ width: "100%", height: "100%" }}
          />
        </div>

        {selectedView ? (
          <div className="card" style={{ marginBottom: 0 }}>
            <div className="cardBody" style={{ display: "flex", flexDirection: "column", gap: 10, padding: 12 }}>
              <button className="chipButton" type="button" onClick={() => setAdvancedOpen((value) => !value)} style={{ alignSelf: "flex-start" }}>
                {t("ext.cameras.calibration.advanced_streams")}
              </button>
              {advancedOpen ? (
                <div className="rowWrap" style={{ gap: 12 }}>
                  {["main", "sub", "zoom", "custom"].map((role) => (
                    <label key={role} className="cardMeta" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <input
                        type="checkbox"
                        checked={compatibleRoles.includes(role)}
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
            onClick={() => {
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
                    image_region: defaultImageRegion(),
                    refinement: view.projection_model.refinement?.points.length
                      ? { model: "local_rbf_v1", points: cloneRefinementPoints(view.projection_model.refinement.points) }
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
      <CameraPoseModal
        open={poseModalOpen}
        onClose={() => setPoseModalOpen(false)}
        i18n={i18n}
        cameraId={cameraId}
        cameraSources={cameraSources}
        selectedView={selectedView}
        onSnapshotRefreshRequested={refreshCurrentCalibrationSnapshot}
        onCapture={(poseReference, label) => {
          updateSelectedView((view) => ({
            ...view,
            label: label || view.label,
            pose_reference: poseReference,
          }));
        }}
      />
    </SubModal>
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
  onCapture: (poseReference: CameraPoseReference, label?: string | null) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [presets, setPresets] = useState<CameraPtzPreset[]>([]);
  const [status, setStatus] = useState<PanTiltZoomState | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeMoveId, setActiveMoveId] = useState<string | null>(null);
  const [selectedPresetToken, setSelectedPresetToken] = useState("");
  const moveTimerRef = useRef<number | null>(null);
  const moveVectorRef = useRef<{ pan: number; tilt: number; zoom: number } | null>(null);
  const snapshotUrlRef = useRef<string | null>(null);
  const preferredSnapshotSourceId = useMemo(
    () => resolvePreferredCalibrationSnapshotSourceId(selectedView, cameraSources),
    [cameraSources, selectedView?.stream_scope],
  );
  const preferredPtzSourceId = useMemo(
    () => resolvePreferredCalibrationPtzSourceId(selectedView, cameraSources),
    [cameraSources, selectedView?.stream_scope],
  );
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

  useEffect(() => {
    if (!open) return;
    setSelectedPresetToken(String(selectedView?.pose_reference?.preset_token ?? "").trim());
  }, [open, selectedView?.id]);

  const refreshStatus = useCallback(async () => {
    if (!cameraId) return null;
    try {
      const response = await fetchCameraPtzStatus(cameraId, preferredPtzSourceId);
      const nextStatus = response.status ?? null;
      setStatus(nextStatus);
      return nextStatus;
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
      return null;
    }
  }, [cameraId, preferredPtzSourceId]);

  const refreshSnapshot = useCallback(async () => {
    if (!cameraId) return;
    const controller = new AbortController();
    try {
      setErrorMessage(null);
      const blob = await fetchCameraSnapshotWithRetry(cameraId, preferredSnapshotSourceId, controller.signal);
      const nextUrl = URL.createObjectURL(blob);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return nextUrl;
      });
    } catch (error) {
      if (isAbortError(error)) return;
      setErrorMessage(error instanceof Error ? error.message : String(error));
    }
  }, [cameraId, preferredSnapshotSourceId]);

  const waitForPtzSettle = useCallback(async () => {
    await sleep(750);
    let nextStatus: PanTiltZoomState | null = null;
    for (let attempt = 0; attempt < 10; attempt += 1) {
      nextStatus = await refreshStatus();
      if (attempt > 0 && normalizePtzMoveStatus(nextStatus?.move_status) !== "moving") break;
      await sleep(450);
    }
    await sleep(350);
    return nextStatus;
  }, [refreshStatus]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setErrorMessage(null);
    void fetchCameraPtzPresets(cameraId, preferredPtzSourceId)
      .then((items) => {
        if (!cancelled) setPresets(Array.isArray(items.presets) ? items.presets : []);
      })
      .catch((error) => {
        if (!cancelled) setErrorMessage(error instanceof Error ? error.message : String(error));
      });
    void (async () => {
      const poseReference = selectedView?.pose_reference ?? null;
      const presetToken = String(poseReference?.preset_token ?? "").trim();
      const needsMove = Boolean(presetToken) || poseHasAbsoluteTarget(poseReference);
      if (needsMove) {
        setBusy(true);
        setSnapshotUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return null;
        });
      }
      try {
        if (presetToken) {
          await gotoCameraPtzPreset(cameraId, presetToken, preferredPtzSourceId);
          if (cancelled) return;
          await waitForPtzSettle();
        } else if (poseHasAbsoluteTarget(poseReference)) {
          await moveCameraPtzAbsolute(cameraId, absoluteMovePayloadForPose(preferredPtzSourceId, poseReference!));
          if (cancelled) return;
          await waitForPtzSettle();
        } else {
          await refreshStatus();
        }
        if (cancelled) return;
        await refreshSnapshot();
        onSnapshotRefreshRequested();
      } catch (error) {
        if (!cancelled) setErrorMessage(error instanceof Error ? error.message : String(error));
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    const interval = window.setInterval(() => {
      void refreshStatus();
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [cameraId, onSnapshotRefreshRequested, open, preferredPtzSourceId, refreshSnapshot, refreshStatus, selectedView?.id, waitForPtzSettle]);

  useEffect(() => {
    if (open) return;
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    setActiveMoveId(null);
    moveVectorRef.current = null;
    if (moveTimerRef.current !== null) {
      window.clearInterval(moveTimerRef.current);
      moveTimerRef.current = null;
    }
  }, [open]);

  useEffect(() => {
    return () => {
      if (snapshotUrlRef.current) URL.revokeObjectURL(snapshotUrlRef.current);
      if (moveTimerRef.current !== null) window.clearInterval(moveTimerRef.current);
    };
  }, []);

  function poseFromStatus(nextStatus: PanTiltZoomState | null, preset?: CameraPtzPreset | null): CameraPoseReference | null {
    if (!nextStatus && !preset) return null;
    return {
      pan: typeof nextStatus?.pan === "number" && Number.isFinite(nextStatus.pan) ? nextStatus.pan : preset?.pan ?? null,
      tilt: typeof nextStatus?.tilt === "number" && Number.isFinite(nextStatus.tilt) ? nextStatus.tilt : preset?.tilt ?? null,
      zoom: typeof nextStatus?.zoom === "number" && Number.isFinite(nextStatus.zoom) ? nextStatus.zoom : preset?.zoom ?? null,
      preset_token: preset?.token ?? null,
      preset_name: preset?.name ?? null,
    };
  }

  async function stopMove(force?: boolean, options?: { refresh?: boolean }) {
    const vector = moveVectorRef.current;
    moveVectorRef.current = null;
    setActiveMoveId(null);
    if (moveTimerRef.current !== null) {
      window.clearInterval(moveTimerRef.current);
      moveTimerRef.current = null;
    }
    if (!cameraId || (!force && !vector)) return;
    try {
      await stopCameraPtz(cameraId, {
        ...(preferredPtzSourceId ? { source_id: preferredPtzSourceId } : {}),
        pan_tilt: force || Boolean(vector && (Math.abs(vector.pan) > 1e-6 || Math.abs(vector.tilt) > 1e-6)),
        zoom: force || Boolean(vector && Math.abs(vector.zoom) > 1e-6),
      });
      if (options?.refresh === false) return;
      await waitForPtzSettle();
      await refreshSnapshot();
      onSnapshotRefreshRequested();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
    }
  }

  function beginMove(moveId: string, vector: { pan: number; tilt: number; zoom: number }) {
    if (!cameraId || busy) return;
    moveVectorRef.current = vector;
    setSelectedPresetToken("");
    setActiveMoveId(moveId);
    const send = async () => {
      try {
        await moveCameraPtz(cameraId, {
          ...(preferredPtzSourceId ? { source_id: preferredPtzSourceId } : {}),
          ...vector,
          timeout_s: PTZ_MOVE_TIMEOUT_S,
        });
      } catch (error) {
        setErrorMessage(error instanceof Error ? error.message : String(error));
        await stopMove(true);
      }
    };
    void send();
    if (moveTimerRef.current !== null) window.clearInterval(moveTimerRef.current);
    moveTimerRef.current = window.setInterval(() => {
      void send();
    }, PTZ_MOVE_REPEAT_MS);
  }

  async function gotoPreset(token: string) {
    const preset = presets.find((item) => item.token === token) ?? null;
    if (!preset) return;
    setSelectedPresetToken(token);
    setBusy(true);
    setErrorMessage(null);
    try {
      await gotoCameraPtzPreset(cameraId, token, preferredPtzSourceId);
      const nextStatus = await waitForPtzSettle();
      await refreshSnapshot();
      const pose = poseFromStatus(nextStatus, preset);
      if (pose) onCapture(pose);
      onSnapshotRefreshRequested();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  function renderMoveButton(control: (typeof panTiltControls)[number] | (typeof zoomControls)[number]) {
    return (
      <button
        key={control.id}
        type="button"
        className="iconButton"
        aria-label={control.label}
        title={control.label}
        onMouseDown={() => (control.id === "stop" ? void stopMove(true) : beginMove(control.id, control.vector))}
        onMouseUp={() => void stopMove()}
        onMouseLeave={() => void stopMove()}
        style={{ background: activeMoveId === control.id ? "rgba(56,189,248,0.14)" : undefined }}
      >
        <i className={`fa-solid ${control.icon}`} aria-hidden="true" />
      </button>
    );
  }

  if (!open) return null;

  return (
    <SubModal open={open} onClose={() => void stopMove(true, { refresh: false }).then(onClose)} title={t("ext.cameras.calibration.position_camera")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="card" style={{ marginBottom: 0 }}>
          <div className="cardBody" style={{ padding: 10 }}>
            {snapshotUrl ? (
              <img src={snapshotUrl} alt="" style={{ display: "block", width: "100%", maxHeight: "48vh", objectFit: "contain", borderRadius: 10 }} />
            ) : (
              <div className="cardMeta">{t("ext.cameras.control.loading")}</div>
            )}
          </div>
        </div>
        <div className="rowWrap" style={{ gap: 8 }}>
          <select
            className="input"
            style={{ maxWidth: 280 }}
            value={selectedPresetToken}
            onChange={(event) => {
              const token = event.target.value;
              setSelectedPresetToken(token);
              if (token) void gotoPreset(token);
            }}
            disabled={busy}
          >
            <option value="">{t("ext.cameras.control.preset_optional")}</option>
            {presets.map((preset) => (
              <option key={preset.token} value={preset.token}>
                {preset.name || preset.token}
              </option>
            ))}
          </select>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              const pose = poseFromStatus(status, null);
              if (pose) onCapture(pose, selectedView?.label ?? null);
              onSnapshotRefreshRequested();
              onClose();
            }}
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
        {errorMessage ? <div className="errorText">{errorMessage}</div> : null}
      </div>
    </SubModal>
  );
}

function ControlPointsModal({
  open,
  onClose,
  host,
  i18n,
  cameraId,
  cameraConnectionType,
  initialSets,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  host: ToposyncHost;
  i18n: HostI18n;
  cameraId: string;
  cameraConnectionType: CameraConnectionType | null;
  initialSets: CameraControlPointSet[];
  onSave: (controlPointSets: CameraControlPointSet[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [sets, setSets] = useState<CameraControlPointSet[]>([]);
  const [selectedSetId, setSelectedSetId] = useState<string | null>(null);
  const [selectedPointId, setSelectedPointId] = useState<string | null>(null);
  const [hoverImagePoint, setHoverImagePoint] = useState<{ x: number; y: number } | null>(null);
  const [hoverWorldPoint, setHoverWorldPoint] = useState<{ x: number; z: number } | null>(null);
  const [ghostWorldPoint, setGhostWorldPoint] = useState<{ x: number; z: number } | null>(null);
  const [ghostImagePoint, setGhostImagePoint] = useState<{ x: number; y: number } | null>(null);

  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [ptzPresets, setPtzPresets] = useState<CameraPtzPreset[]>([]);
  const [ptzStatus, setPtzStatus] = useState<PanTiltZoomState | null>(null);
  const [ptzLoading, setPtzLoading] = useState(false);
  const [ptzErrorMessage, setPtzErrorMessage] = useState<string | null>(null);
  const [ptzCommandBusy, setPtzCommandBusy] = useState(false);
  const [selectedPresetToken, setSelectedPresetToken] = useState("");
  const [activeMoveId, setActiveMoveId] = useState<string | null>(null);

  const selectedSet = useMemo(
    () => sets.find((item) => item.id === selectedSetId) ?? sets[0] ?? null,
    [selectedSetId, sets],
  );
  const selectedPoints = selectedSet?.control_points ?? [];
  const completePairs = useMemo(
    () => selectedPoints.filter((point) => Boolean(point.image) && Boolean(point.world)).length,
    [selectedPoints],
  );
  const selectedSetQuality = useMemo(
    () => (selectedSet ? summarizeControlPointSetQuality(selectedSet) : null),
    [selectedSet],
  );
  const mappingControlPointSet = useMemo<CameraControlPointSet | null>(() => {
    if (!selectedSet) return null;
    return {
      ...selectedSet,
      pose_reference: selectedSet.pose_reference ? { ...selectedSet.pose_reference } : null,
      control_points: selectedSet.control_points.map((point) => ({
        ...point,
        image: point.image ? { ...point.image } : null,
        world: point.world ? { ...point.world } : null,
      })),
    };
  }, [selectedSet]);
  const isPtzCamera = cameraConnectionType === "onvif";
  const selectedPreset = useMemo(
    () => ptzPresets.find((preset) => String(preset.token || "").trim() === selectedPresetToken) ?? null,
    [ptzPresets, selectedPresetToken],
  );
  const normalizedMoveStatus = normalizePtzMoveStatus(ptzStatus?.move_status);
  const selectedSetIdRef = useRef<string | null>(null);
  const ptzStatusRef = useRef<PanTiltZoomState | null>(null);
  const moveVectorRef = useRef<{ pan: number; tilt: number; zoom: number } | null>(null);
  const moveHeldRef = useRef(false);
  const moveTimerRef = useRef<number | null>(null);
  const moveRequestInFlightRef = useRef(false);
  const stopRequestInFlightRef = useRef(false);
  const snapshotAbortRef = useRef<AbortController | null>(null);
  const snapshotTimerRef = useRef<number | null>(null);
  const snapshotIntervalRef = useRef<number | null>(null);
  const snapshotUrlRef = useRef<string | null>(null);
  const ptzPresetsAbortRef = useRef<AbortController | null>(null);
  const ptzStatusAbortRef = useRef<AbortController | null>(null);
  const ptzStatusIntervalRef = useRef<number | null>(null);

  useEffect(() => {
    selectedSetIdRef.current = selectedSetId;
  }, [selectedSetId]);

  useEffect(() => {
    ptzStatusRef.current = ptzStatus;
  }, [ptzStatus]);

  useEffect(() => {
    snapshotUrlRef.current = snapshotUrl;
  }, [snapshotUrl]);

  useEffect(() => {
    if (!open) return;
    const baseSets = initialSets.length
      ? initialSets.map((item) => ({
          ...item,
          pose_reference: item.pose_reference ? { ...item.pose_reference } : null,
          control_points: padControlPoints(
            item.control_points.map((point) => ({
              ...point,
              image: point.image ?? null,
              world: point.world ?? null,
            })),
          ),
        }))
      : [createDefaultControlPointSet(0, { label: t("ext.cameras.control.set_default") })];
    setSets(baseSets);
    setSelectedSetId(baseSets[0]?.id ?? null);
    setSelectedPointId(baseSets[0]?.control_points[0]?.id ?? null);
  }, [initialSets, open, t]);

  useEffect(() => {
    if (open) return;
    setHoverImagePoint(null);
    setHoverWorldPoint(null);
    setGhostWorldPoint(null);
    setGhostImagePoint(null);
  }, [open]);

  useEffect(() => {
    if (!selectedSet) {
      setSelectedPointId(null);
      return;
    }
    if (!selectedSet.control_points.some((point) => point.id === selectedPointId)) {
      setSelectedPointId(selectedSet.control_points[0]?.id ?? null);
    }
  }, [selectedPointId, selectedSet]);

  useEffect(() => {
    setSelectedPresetToken(String(selectedSet?.pose_reference?.preset_token ?? "").trim());
  }, [selectedSet?.id, selectedSet?.pose_reference?.preset_token]);

  const captureCurrentPoseIntoSelectedSet = useCallback(
    (
      nextStatus: PanTiltZoomState | null,
      options?: {
        presetToken?: string | null;
        presetName?: string | null;
        renameFromPreset?: boolean;
      },
    ) => {
      if (!nextStatus) return;
      const setId = selectedSetIdRef.current;
      if (!setId) return;
      const pan = typeof nextStatus.pan === "number" && Number.isFinite(nextStatus.pan) ? nextStatus.pan : null;
      const tilt = typeof nextStatus.tilt === "number" && Number.isFinite(nextStatus.tilt) ? nextStatus.tilt : null;
      const zoom = typeof nextStatus.zoom === "number" && Number.isFinite(nextStatus.zoom) ? nextStatus.zoom : null;
      const presetToken = options?.presetToken ?? null;
      const presetName = options?.presetName ?? null;
      if (pan === null && tilt === null && zoom === null && !presetToken && !presetName) return;
      setSets((previous) =>
        previous.map((item) =>
          item.id !== setId
            ? item
            : {
                ...item,
                label: options?.renameFromPreset && presetName ? presetName : item.label,
                pose_reference: {
                  pan,
                  tilt,
                  zoom,
                  preset_token: presetToken,
                  preset_name: presetName,
                },
              },
        ),
      );
    },
    [],
  );

  const loadSnapshot = useCallback(
    async (options?: { silent?: boolean }) => {
      if (!cameraId) return;
      snapshotAbortRef.current?.abort();
      const controller = new AbortController();
      snapshotAbortRef.current = controller;
      if (!options?.silent) {
        setSnapshotLoading(true);
        setSnapshotErrorMessage(null);
      }
      try {
        const blob = await fetchCameraSnapshot(cameraId, controller.signal);
        const nextUrl = URL.createObjectURL(blob);
        setSnapshotUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return nextUrl;
        });
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
        setSnapshotUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return null;
        });
      } finally {
        if (!options?.silent) setSnapshotLoading(false);
      }
    },
    [cameraId],
  );

  const scheduleSnapshotRefresh = useCallback(
    (delayMs: number) => {
      if (snapshotTimerRef.current !== null) {
        window.clearTimeout(snapshotTimerRef.current);
        snapshotTimerRef.current = null;
      }
      snapshotTimerRef.current = window.setTimeout(() => {
        snapshotTimerRef.current = null;
        void loadSnapshot({ silent: false });
      }, Math.max(0, delayMs));
    },
    [loadSnapshot],
  );

  const refreshPtzStatus = useCallback(
    async (options?: { silent?: boolean }) => {
      if (!cameraId || !isPtzCamera) return null;
      ptzStatusAbortRef.current?.abort();
      const controller = new AbortController();
      ptzStatusAbortRef.current = controller;
      if (!options?.silent) {
        setPtzLoading(true);
        setPtzErrorMessage(null);
      }
      try {
        const response = await fetchCameraPtzStatus(cameraId, controller.signal);
        const nextStatus = response.status ?? null;
        setPtzStatus(nextStatus);
        return nextStatus;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return null;
        setPtzErrorMessage(error instanceof Error ? error.message : String(error));
        setPtzStatus(null);
        return null;
      } finally {
        if (!options?.silent) setPtzLoading(false);
      }
    },
    [cameraId, isPtzCamera],
  );

  const loadPtzPresets = useCallback(async () => {
    if (!cameraId || !isPtzCamera) return;
    ptzPresetsAbortRef.current?.abort();
    const controller = new AbortController();
    ptzPresetsAbortRef.current = controller;
    try {
      const response = await fetchCameraPtzPresets(cameraId, controller.signal);
      setPtzPresets(Array.isArray(response.presets) ? response.presets : []);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setPtzErrorMessage(error instanceof Error ? error.message : String(error));
      setPtzPresets([]);
    }
  }, [cameraId, isPtzCamera]);

  const settlePtzAndRefresh = useCallback(
    async (options?: { presetToken?: string | null; presetName?: string | null; renameFromPreset?: boolean }) => {
      if (!cameraId || !isPtzCamera) {
        scheduleSnapshotRefresh(400);
        return;
      }
      let finalStatus = ptzStatusRef.current;
      for (let attempt = 0; attempt < 8; attempt += 1) {
        const nextStatus = await refreshPtzStatus({ silent: attempt > 0 });
        if (nextStatus) finalStatus = nextStatus;
        if (normalizePtzMoveStatus(nextStatus?.move_status) !== "moving") break;
        await sleep(420);
      }
      captureCurrentPoseIntoSelectedSet(finalStatus, options);
      scheduleSnapshotRefresh(550);
    },
    [cameraId, captureCurrentPoseIntoSelectedSet, isPtzCamera, refreshPtzStatus, scheduleSnapshotRefresh],
  );

  useEffect(() => {
    return () => {
      snapshotAbortRef.current?.abort();
      ptzPresetsAbortRef.current?.abort();
      ptzStatusAbortRef.current?.abort();
      if (snapshotTimerRef.current !== null) window.clearTimeout(snapshotTimerRef.current);
      if (snapshotIntervalRef.current !== null) window.clearInterval(snapshotIntervalRef.current);
      if (ptzStatusIntervalRef.current !== null) window.clearInterval(ptzStatusIntervalRef.current);
      if (snapshotUrlRef.current) URL.revokeObjectURL(snapshotUrlRef.current);
    };
  }, []);

  useEffect(() => {
    if (!open) {
      setSnapshotErrorMessage(null);
      setSnapshotLoading(false);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
      setPtzErrorMessage(null);
      setPtzLoading(false);
      setPtzPresets([]);
      setPtzStatus(null);
      setSelectedPresetToken("");
      setActiveMoveId(null);
      moveHeldRef.current = false;
      moveVectorRef.current = null;
      if (moveTimerRef.current !== null) {
        window.clearInterval(moveTimerRef.current);
        moveTimerRef.current = null;
      }
      if (snapshotTimerRef.current !== null) {
        window.clearTimeout(snapshotTimerRef.current);
        snapshotTimerRef.current = null;
      }
      if (snapshotIntervalRef.current !== null) {
        window.clearInterval(snapshotIntervalRef.current);
        snapshotIntervalRef.current = null;
      }
      if (ptzStatusIntervalRef.current !== null) {
        window.clearInterval(ptzStatusIntervalRef.current);
        ptzStatusIntervalRef.current = null;
      }
      snapshotAbortRef.current?.abort();
      ptzPresetsAbortRef.current?.abort();
      ptzStatusAbortRef.current?.abort();
      return;
    }

    if (cameraId) {
      void loadSnapshot();
      if (snapshotIntervalRef.current !== null) window.clearInterval(snapshotIntervalRef.current);
      snapshotIntervalRef.current = window.setInterval(() => {
        void loadSnapshot({ silent: true });
      }, SNAPSHOT_REFRESH_MS);
    }

    if (cameraId && isPtzCamera) {
      void loadPtzPresets();
      void refreshPtzStatus();
      if (ptzStatusIntervalRef.current !== null) window.clearInterval(ptzStatusIntervalRef.current);
      ptzStatusIntervalRef.current = window.setInterval(() => {
        void refreshPtzStatus({ silent: true });
      }, PTZ_STATUS_REFRESH_MS);
    } else {
      setPtzPresets([]);
      setPtzStatus(null);
      setPtzErrorMessage(null);
    }

    return () => {
      snapshotAbortRef.current?.abort();
      ptzPresetsAbortRef.current?.abort();
      ptzStatusAbortRef.current?.abort();
      if (snapshotTimerRef.current !== null) {
        window.clearTimeout(snapshotTimerRef.current);
        snapshotTimerRef.current = null;
      }
      if (snapshotIntervalRef.current !== null) {
        window.clearInterval(snapshotIntervalRef.current);
        snapshotIntervalRef.current = null;
      }
      if (ptzStatusIntervalRef.current !== null) {
        window.clearInterval(ptzStatusIntervalRef.current);
        ptzStatusIntervalRef.current = null;
      }
      if (moveTimerRef.current !== null) {
        window.clearInterval(moveTimerRef.current);
        moveTimerRef.current = null;
      }
      moveHeldRef.current = false;
      moveVectorRef.current = null;
      setActiveMoveId(null);
    };
  }, [cameraId, isPtzCamera, loadPtzPresets, loadSnapshot, open, refreshPtzStatus]);

  const imageToWorldAbortRef = React.useRef<AbortController | null>(null);
  const worldToImageAbortRef = React.useRef<AbortController | null>(null);
  const imageToWorldTimerRef = React.useRef<number | null>(null);
  const worldToImageTimerRef = React.useRef<number | null>(null);
  const mapDebounceMs = 80;

  useEffect(() => {
    if (!open) return;
    if (!hoverImagePoint || completePairs < 4 || !mappingControlPointSet) {
      if (imageToWorldTimerRef.current) {
        window.clearTimeout(imageToWorldTimerRef.current);
        imageToWorldTimerRef.current = null;
      }
      imageToWorldAbortRef.current?.abort();
      setGhostWorldPoint(null);
      return;
    }

    imageToWorldAbortRef.current?.abort();
    if (imageToWorldTimerRef.current) window.clearTimeout(imageToWorldTimerRef.current);
    imageToWorldTimerRef.current = window.setTimeout(() => {
      imageToWorldTimerRef.current = null;
      const controller = new AbortController();
      imageToWorldAbortRef.current = controller;
      void mapControlPoint(mappingControlPointSet, { kind: "image", x: hoverImagePoint.x, y: hoverImagePoint.y }, controller.signal)
        .then((result) => {
          setGhostWorldPoint(result.world ?? null);
        })
        .catch((error) => {
          if (error instanceof DOMException && error.name === "AbortError") return;
          console.warn("[cameras] hover map image->world failed", error);
          setGhostWorldPoint(null);
        });
    }, mapDebounceMs);

    return () => {
      if (imageToWorldTimerRef.current) {
        window.clearTimeout(imageToWorldTimerRef.current);
        imageToWorldTimerRef.current = null;
      }
      imageToWorldAbortRef.current?.abort();
    };
  }, [completePairs, hoverImagePoint, mappingControlPointSet, open]);

  useEffect(() => {
    if (!open) return;
    if (!hoverWorldPoint || completePairs < 4 || !mappingControlPointSet) {
      if (worldToImageTimerRef.current) {
        window.clearTimeout(worldToImageTimerRef.current);
        worldToImageTimerRef.current = null;
      }
      worldToImageAbortRef.current?.abort();
      setGhostImagePoint(null);
      return;
    }

    worldToImageAbortRef.current?.abort();
    if (worldToImageTimerRef.current) window.clearTimeout(worldToImageTimerRef.current);
    worldToImageTimerRef.current = window.setTimeout(() => {
      worldToImageTimerRef.current = null;
      const controller = new AbortController();
      worldToImageAbortRef.current = controller;
      void mapControlPoint(mappingControlPointSet, { kind: "world", x: hoverWorldPoint.x, z: hoverWorldPoint.z }, controller.signal)
        .then((result) => {
          setGhostImagePoint(result.image ?? null);
        })
        .catch((error) => {
          if (error instanceof DOMException && error.name === "AbortError") return;
          console.warn("[cameras] hover map world->image failed", error);
          setGhostImagePoint(null);
        });
    }, mapDebounceMs);

    return () => {
      if (worldToImageTimerRef.current) {
        window.clearTimeout(worldToImageTimerRef.current);
        worldToImageTimerRef.current = null;
      }
      worldToImageAbortRef.current?.abort();
    };
  }, [completePairs, hoverWorldPoint, mappingControlPointSet, open]);

  const toolSession = useMemo<EditorToolSession>(() => {
    return {
      onPointerEvent: (event: EditorToolPointerEvent) => {
        if (event.kind === "cancel") {
          setHoverWorldPoint(null);
          setGhostImagePoint(null);
          return;
        }
        if (event.kind === "move") {
          if (completePairs >= 4) {
            setHoverWorldPoint({ x: event.world.x, z: event.world.z });
            setHoverImagePoint(null);
            setGhostWorldPoint(null);
          }
          return;
        }
        if (event.kind !== "down" || !selectedSetId || !selectedPointId) return;
        setSets((previous) =>
          previous.map((controlPointSet) =>
            controlPointSet.id !== selectedSetId
              ? controlPointSet
              : {
                  ...controlPointSet,
                  control_points: controlPointSet.control_points.map((point) =>
                    point.id === selectedPointId ? { ...point, world: { x: event.world.x, z: event.world.z } } : point,
                  ),
                },
          ),
        );
      },
      renderOverlay2D: ({
        ctx: canvasContext,
        viewport,
      }: {
        ctx: CanvasRenderingContext2D;
        viewport: Viewport2DContext;
      }) => {
        canvasContext.save();
        canvasContext.font = "700 12px system-ui, -apple-system, Segoe UI, Roboto, Arial";
        canvasContext.textAlign = "center";
        canvasContext.textBaseline = "middle";

        for (let index = 0; index < selectedPoints.length; index += 1) {
          const point = selectedPoints[index];
          if (!point.world) continue;
          const color = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
          const screen = viewport.worldToScreen(point.world);
          const isSelected = selectedPointId === point.id;

          canvasContext.beginPath();
          canvasContext.arc(screen.x, screen.y, isSelected ? 10 : 8, 0, Math.PI * 2);
          canvasContext.fillStyle = color;
          canvasContext.fill();
          canvasContext.lineWidth = 2;
          canvasContext.strokeStyle = isSelected ? "rgba(255,255,255,0.92)" : "rgba(0,0,0,0.65)";
          canvasContext.stroke();

          canvasContext.fillStyle = "rgba(0,0,0,0.82)";
          canvasContext.fillText(point.label || labelForIndex(index), screen.x, screen.y + 0.5);
        }

        if (ghostWorldPoint && completePairs >= 4) {
          const screen = viewport.worldToScreen(ghostWorldPoint);
          canvasContext.beginPath();
          canvasContext.arc(screen.x, screen.y, 9, 0, Math.PI * 2);
          canvasContext.fillStyle = "rgba(251,191,36,0.10)";
          canvasContext.fill();
          canvasContext.lineWidth = 2;
          canvasContext.strokeStyle = "rgba(251,191,36,0.88)";
          canvasContext.setLineDash([6, 4]);
          canvasContext.stroke();
          canvasContext.setLineDash([]);

          canvasContext.beginPath();
          canvasContext.arc(screen.x, screen.y, 2.6, 0, Math.PI * 2);
          canvasContext.fillStyle = "rgba(251,191,36,0.95)";
          canvasContext.fill();
        }

        canvasContext.restore();
      },
      getCursor: () => "crosshair",
    };
  }, [completePairs, ghostWorldPoint, selectedPointId, selectedPoints, selectedSetId]);

  function updateSelectedSet(patch: Partial<CameraControlPointSet>) {
    if (!selectedSetId) return;
    setSets((previous) =>
      previous.map((item) =>
        item.id === selectedSetId
          ? {
              ...item,
              ...patch,
              control_points: patch.control_points ?? item.control_points,
              pose_reference: patch.pose_reference === undefined ? item.pose_reference ?? null : patch.pose_reference,
            }
          : item,
      ),
    );
  }

  function addPoint() {
    if (!selectedSetId) return;
    const id = createUniqueId();
    setSets((previous) =>
      previous.map((controlPointSet) =>
        controlPointSet.id !== selectedSetId
          ? controlPointSet
          : {
              ...controlPointSet,
              control_points: [
                ...controlPointSet.control_points,
                { id, label: labelForIndex(controlPointSet.control_points.length), image: null, world: null },
              ],
            },
      ),
    );
    setSelectedPointId(id);
  }

  function addPosition() {
    setSets((previous) => {
      const nextIndex = previous.length;
      const nextSet = selectedSet
        ? {
            ...duplicateControlPointSetForNewView(selectedSet, nextIndex),
            label: t("ext.cameras.control.set_label", { index: nextIndex + 1 }),
          }
        : createDefaultControlPointSet(nextIndex, { label: t("ext.cameras.control.set_label", { index: nextIndex + 1 }) });
      const paddedSet = { ...nextSet, control_points: padControlPoints(nextSet.control_points) };
      setSelectedSetId(paddedSet.id);
      setSelectedPointId(paddedSet.control_points[0]?.id ?? null);
      return [...previous, paddedSet];
    });
  }

  function removeSelectedPosition() {
    if (!selectedSetId || sets.length <= 1) return;
    setSets((previous) => {
      const filtered = previous.filter((item) => item.id !== selectedSetId);
      const fallback = filtered[0] ?? null;
      setSelectedSetId(fallback?.id ?? null);
      setSelectedPointId(fallback?.control_points[0]?.id ?? null);
      return filtered;
    });
  }

  function getImagePointFromEvent(event: React.MouseEvent<HTMLImageElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    const normalizedX = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
    const normalizedY = Math.max(0, Math.min(1, (event.clientY - rect.top) / Math.max(1, rect.height)));
    return { x: normalizedX, y: normalizedY };
  }

  function setImagePointFromEvent(event: React.MouseEvent<HTMLImageElement>) {
    const imgPoint = getImagePointFromEvent(event);
    if (!imgPoint || !selectedSetId || !selectedPointId) return;
    setSets((previous) =>
      previous.map((controlPointSet) =>
        controlPointSet.id !== selectedSetId
          ? controlPointSet
          : {
              ...controlPointSet,
              control_points: controlPointSet.control_points.map((point) =>
                point.id === selectedPointId ? { ...point, image: imgPoint } : point,
              ),
            },
      ),
    );
  }

  function readPoseAxis(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  async function moveCameraToPresetToken(
    nextPresetToken: string,
    options?: { fallbackPresetName?: string | null; bindSelectedSet?: boolean; renameFromPreset?: boolean },
  ) {
    const token = String(nextPresetToken || "").trim();
    if (!token || !cameraId || !isPtzCamera) return;
    const preset = ptzPresets.find((item) => String(item.token || "").trim() === token) ?? null;
    const presetName = String(preset?.name || options?.fallbackPresetName || "").trim() || token;
    const presetPose = {
      pan: typeof preset?.pan === "number" && Number.isFinite(preset.pan) ? preset.pan : null,
      tilt: typeof preset?.tilt === "number" && Number.isFinite(preset.tilt) ? preset.tilt : null,
      zoom: typeof preset?.zoom === "number" && Number.isFinite(preset.zoom) ? preset.zoom : null,
      preset_token: token,
      preset_name: presetName,
    };
    setSelectedPresetToken(token);
    if (options?.bindSelectedSet) updateSelectedSet({ label: presetName, pose_reference: presetPose });
    setPtzCommandBusy(true);
    setPtzErrorMessage(null);
    try {
      await gotoCameraPtzPreset(cameraId, token);
      await settlePtzAndRefresh({
        presetToken: token,
        presetName,
        renameFromPreset: options?.renameFromPreset === true,
      });
    } catch (error) {
      setPtzErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setPtzCommandBusy(false);
    }
  }

  async function moveCameraToPoseReference(poseReference: CameraPoseReference | null | undefined) {
    if (!poseReference || !cameraId || !isPtzCamera) return;
    const presetToken = String(poseReference.preset_token ?? "").trim();
    const presetName = String(poseReference.preset_name ?? "").trim() || presetToken || null;
    const pan = readPoseAxis(poseReference.pan);
    const tilt = readPoseAxis(poseReference.tilt);
    const zoom = readPoseAxis(poseReference.zoom);
    const hasPanTilt = pan !== null && tilt !== null;
    const hasAbsolutePosition = hasPanTilt || zoom !== null;
    if (!hasAbsolutePosition) {
      if (presetToken) {
        await moveCameraToPresetToken(presetToken, {
          fallbackPresetName: presetName,
          bindSelectedSet: false,
        });
      }
      return;
    }

    setPtzCommandBusy(true);
    setPtzErrorMessage(null);
    try {
      await moveCameraPtzAbsolute(cameraId, {
        pan: hasPanTilt ? pan : null,
        tilt: hasPanTilt ? tilt : null,
        zoom,
      });
      await settlePtzAndRefresh({
        presetToken: presetToken || null,
        presetName,
      });
    } catch (error) {
      setPtzErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setPtzCommandBusy(false);
    }
  }

  async function handlePresetSelection(nextPresetToken: string) {
    const token = String(nextPresetToken || "").trim();
    setSelectedPresetToken(token);
    if (!token) {
      captureCurrentPoseIntoSelectedSet(ptzStatusRef.current, { presetToken: null, presetName: null });
      return;
    }
    await moveCameraToPresetToken(token, { bindSelectedSet: true, renameFromPreset: true });
  }

  async function handleControlPointSetSelection(controlPointSet: CameraControlPointSet) {
    const nextPresetToken = String(controlPointSet.pose_reference?.preset_token ?? "").trim();
    if (moveHeldRef.current || moveVectorRef.current) {
      await stopActivePtzMove({ force: true });
    }
    selectedSetIdRef.current = controlPointSet.id;
    setSelectedSetId(controlPointSet.id);
    setSelectedPointId(controlPointSet.control_points[0]?.id ?? null);
    setSelectedPresetToken(nextPresetToken);
    await moveCameraToPoseReference(controlPointSet.pose_reference);
  }

  async function stopActivePtzMove(options?: { force?: boolean }) {
    const force = options?.force === true;
    const currentMove = moveVectorRef.current;
    const shouldStop = force || moveHeldRef.current || currentMove !== null || activeMoveId !== null;

    moveHeldRef.current = false;
    moveVectorRef.current = null;
    setActiveMoveId(null);
    if (moveTimerRef.current !== null) {
      window.clearInterval(moveTimerRef.current);
      moveTimerRef.current = null;
    }

    if (!cameraId || !shouldStop || stopRequestInFlightRef.current) return;
    stopRequestInFlightRef.current = true;
    setPtzCommandBusy(true);
    try {
      await stopCameraPtz(cameraId, {
        pan_tilt: force || Boolean(currentMove && (Math.abs(currentMove.pan) > 1e-6 || Math.abs(currentMove.tilt) > 1e-6)),
        zoom: force || Boolean(currentMove && Math.abs(currentMove.zoom) > 1e-6),
      });
      setSelectedPresetToken("");
      setPtzErrorMessage(null);
      await settlePtzAndRefresh({ presetToken: null, presetName: null });
    } catch (error) {
      if (force) setPtzErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      stopRequestInFlightRef.current = false;
      setPtzCommandBusy(false);
    }
  }

  function beginPtzMove(moveId: string, vector: { pan: number; tilt: number; zoom: number }) {
    if (!cameraId || !isPtzCamera || ptzCommandBusy) return;
    const clampedVector = {
      pan: clamp(vector.pan, -1, 1),
      tilt: clamp(vector.tilt, -1, 1),
      zoom: clamp(vector.zoom, -1, 1),
    };
    moveVectorRef.current = clampedVector;
    moveHeldRef.current = true;
    setSelectedPresetToken("");
    setActiveMoveId(moveId);

    const sendMove = async () => {
      if (!cameraId || !moveHeldRef.current || !moveVectorRef.current || moveRequestInFlightRef.current) return;
      moveRequestInFlightRef.current = true;
      try {
        await moveCameraPtz(cameraId, { ...moveVectorRef.current, timeout_s: PTZ_MOVE_TIMEOUT_S });
        setPtzErrorMessage(null);
      } catch (error) {
        setPtzErrorMessage(error instanceof Error ? error.message : String(error));
        await stopActivePtzMove({ force: true });
      } finally {
        moveRequestInFlightRef.current = false;
      }
    };

    void sendMove();
    if (moveTimerRef.current !== null) window.clearInterval(moveTimerRef.current);
    moveTimerRef.current = window.setInterval(() => {
      void sendMove();
    }, PTZ_MOVE_REPEAT_MS);
  }

  return (
    <SubModal
      open={open}
      onClose={() => {
        void stopActivePtzMove({ force: true });
        onClose();
      }}
      title={t("ext.cameras.control.title")}
      panelStyle={{
        width: "min(1440px, calc(100vw - 28px))",
        height: "calc(100vh - 28px)",
        maxHeight: "calc(100vh - 28px)",
      }}
      bodyStyle={{
        padding: 0,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 12, flex: 1, minHeight: 0 }}>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <div className="rowWrap" style={{ gap: 8, flexWrap: "wrap" }}>
            {sets.map((controlPointSet, index) => {
              const quality = summarizeControlPointSetQuality(controlPointSet);
              const isSelected = selectedSet?.id === controlPointSet.id;
              const statusColor =
                quality.status === "good"
                  ? "rgba(34,197,94,0.92)"
                  : quality.status === "review"
                    ? "rgba(251,191,36,0.92)"
                    : "rgba(148,163,184,0.88)";
              return (
                <button
                  key={controlPointSet.id}
                  type="button"
                  className="chipButton"
                  onClick={() => {
                    void handleControlPointSetSelection(controlPointSet);
                  }}
                  disabled={isPtzCamera && ptzCommandBusy}
                  style={{
                    minWidth: 190,
                    justifyContent: "space-between",
                    borderColor: isSelected ? "rgba(56,189,248,0.55)" : "rgba(255,255,255,0.14)",
                    background: isSelected ? "rgba(56,189,248,0.10)" : undefined,
                  }}
                >
                  <span style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 2 }}>
                    <span>{controlPointSet.label || t("ext.cameras.control.set_label", { index: index + 1 })}</span>
                    <span className="cardMeta">
                      {quality.status === "good"
                        ? t("ext.cameras.control.quality_good")
                        : quality.status === "review"
                          ? t("ext.cameras.control.quality_review")
                          : t("ext.cameras.control.quality_incomplete")}
                    </span>
                  </span>
                  <span
                    aria-hidden="true"
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: 999,
                      background: statusColor,
                      boxShadow: "0 0 0 2px rgba(0,0,0,0.25)",
                    }}
                  />
                </button>
              );
            })}

            <button className="chipButton" type="button" onClick={addPosition}>
              <i className="fa-solid fa-plus" aria-hidden="true" />
              <span>{t("ext.cameras.control.add_position")}</span>
            </button>

            <button
              className="iconButton"
              type="button"
              onClick={removeSelectedPosition}
              aria-label={t("core.actions.delete")}
              disabled={sets.length <= 1}
            >
              <i className="fa-solid fa-trash" aria-hidden="true" />
            </button>
          </div>

          <div className="cardMeta" style={{ textAlign: "right" }}>
            {t("ext.cameras.control.help_sets")}
            {completePairs > 0 && completePairs < 4 ? ` ${t("ext.cameras.control.min_points")}` : ""}
          </div>
        </div>

        {selectedSet ? (
          <>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: isPtzCamera
                  ? "minmax(220px, 1.2fr) minmax(220px, 1.6fr) minmax(180px, 1fr)"
                  : "minmax(220px, 1fr) minmax(320px, 1.8fr)",
                gap: 10,
                alignItems: "end",
              }}
            >
              {isPtzCamera ? (
                <div className="field" style={{ marginBottom: 0 }}>
                  <label className="label">{t("ext.cameras.control.preset_label")}</label>
                  <select
                    className="input"
                    value={selectedPresetToken}
                    disabled={ptzLoading || ptzCommandBusy}
                    onChange={(event) => {
                      void handlePresetSelection(event.target.value);
                    }}
                  >
                    <option value="">{t("ext.cameras.control.preset_optional")}</option>
                    {ptzPresets.map((preset) => {
                      const token = String(preset.token || "").trim();
                      if (!token) return null;
                      const name = String(preset.name || "").trim() || token;
                      return (
                        <option key={token} value={token}>
                          {name}
                        </option>
                      );
                    })}
                  </select>
                </div>
              ) : null}

              <div className="field" style={{ marginBottom: 0 }}>
                <label className="label">{t("ext.cameras.control.position_name")}</label>
                <input
                  className="input"
                  value={selectedSet.label}
                  onChange={(event) => updateSelectedSet({ label: event.target.value })}
                />
              </div>

              <div
                className="card"
                style={{ marginBottom: 0, minHeight: 44, display: "flex", alignItems: "center" }}
              >
                <div className="cardBody" style={{ padding: "10px 12px" }}>
                  <div className="cardMeta">
                    {isPtzCamera ? t("ext.cameras.control.ptz_help_auto") : t("ext.cameras.control.pose_unbound")}
                  </div>
                  <div style={{ fontWeight: 600 }}>
                    {selectedPreset ? String(selectedPreset.name || "").trim() || selectedPreset.token : t("ext.cameras.control.preset_current")}
                  </div>
                </div>
              </div>
            </div>

            {isPtzCamera ? (
              <div className="card" style={{ marginBottom: 0 }}>
                <div
                  className="cardBody"
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(240px, 260px) minmax(220px, 1fr)",
                    gap: 16,
                    alignItems: "center",
                  }}
                >
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, alignItems: "stretch" }}>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("up-left", { pan: -PTZ_PAN_SPEED, tilt: PTZ_TILT_SPEED, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "up-left" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-up-left" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("up", { pan: 0, tilt: PTZ_TILT_SPEED, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "up" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-up" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("up-right", { pan: PTZ_PAN_SPEED, tilt: PTZ_TILT_SPEED, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "up-right" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-up-right" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("left", { pan: -PTZ_PAN_SPEED, tilt: 0, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "left" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-left" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onClick={() => void stopActivePtzMove({ force: true })}
                      disabled={ptzCommandBusy && !activeMoveId}
                    >
                      <i className="fa-solid fa-stop" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("right", { pan: PTZ_PAN_SPEED, tilt: 0, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "right" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-right" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("down-left", { pan: -PTZ_PAN_SPEED, tilt: -PTZ_TILT_SPEED, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "down-left" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-down-left" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("down", { pan: 0, tilt: -PTZ_TILT_SPEED, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "down" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-down" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="iconButton"
                      onMouseDown={() => beginPtzMove("down-right", { pan: PTZ_PAN_SPEED, tilt: -PTZ_TILT_SPEED, zoom: 0 })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ background: activeMoveId === "down-right" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      <i className="fa-solid fa-arrow-down-right" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="chipButton"
                      onMouseDown={() => beginPtzMove("zoom-in", { pan: 0, tilt: 0, zoom: PTZ_ZOOM_SPEED })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ justifyContent: "center", background: activeMoveId === "zoom-in" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      {t("ext.cameras.control.zoom_in")}
                    </button>
                    <button
                      type="button"
                      className="chipButton"
                      onMouseDown={() => beginPtzMove("zoom-out", { pan: 0, tilt: 0, zoom: -PTZ_ZOOM_SPEED })}
                      onMouseUp={() => void stopActivePtzMove()}
                      onMouseLeave={() => void stopActivePtzMove()}
                      disabled={ptzCommandBusy}
                      style={{ justifyContent: "center", background: activeMoveId === "zoom-out" ? "rgba(56,189,248,0.14)" : undefined }}
                    >
                      {t("ext.cameras.control.zoom_out")}
                    </button>
                    <button type="button" className="chipButton" onClick={() => void loadSnapshot()} disabled={snapshotLoading}>
                      {t("ext.cameras.control.refresh_snapshot")}
                    </button>
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 10 }}>
                    {[
                      { label: t("ext.cameras.control.pose_pan"), value: formatPtzTelemetryValue(ptzStatus?.pan) },
                      { label: t("ext.cameras.control.pose_tilt"), value: formatPtzTelemetryValue(ptzStatus?.tilt) },
                      { label: t("ext.cameras.control.pose_zoom"), value: formatPtzTelemetryValue(ptzStatus?.zoom) },
                      {
                        label: t("ext.cameras.control.ptz_status_label"),
                        value:
                          normalizedMoveStatus === "moving"
                            ? t("ext.cameras.control.ptz_status_moving")
                            : normalizedMoveStatus === "idle"
                              ? t("ext.cameras.control.ptz_status_idle")
                              : t("ext.cameras.control.ptz_status_unknown"),
                      },
                    ].map((item) => (
                      <div
                        key={item.label}
                        className="card"
                        style={{ marginBottom: 0, minHeight: 64, display: "flex", alignItems: "center" }}
                      >
                        <div className="cardBody" style={{ padding: "10px 12px" }}>
                          <div className="cardMeta">{item.label}</div>
                          <div style={{ fontWeight: 700 }}>{item.value}</div>
                        </div>
                      </div>
                    ))}
                    {(ptzErrorMessage || ptzLoading) && (
                      <div className="card" style={{ gridColumn: "1 / -1", marginBottom: 0 }}>
                        <div className="cardBody" style={{ padding: "10px 12px" }}>
                          {ptzLoading ? t("ext.cameras.control.loading") : ptzErrorMessage}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ) : null}

            <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <div className="cardMeta">{isPtzCamera ? t("ext.cameras.control.ptz_help_auto") : t("ext.cameras.control.pose_help")}</div>
              <div className="cardMeta">
                {selectedSetQuality?.status === "good"
                  ? t("ext.cameras.control.quality_good")
                  : selectedSetQuality?.status === "review"
                    ? t("ext.cameras.control.quality_review")
                    : t("ext.cameras.control.quality_incomplete")}
              </div>
            </div>

            <div className="rowWrap" style={{ gap: 8, flexWrap: "wrap" }}>
              {selectedPoints.map((point, index) => {
                const isSelected = selectedPointId === point.id;
                const color = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
                const ready = Boolean(point.image && point.world);
                return (
                  <button
                    key={point.id}
                    type="button"
                    className="chipButton"
                    onClick={() => setSelectedPointId(point.id)}
                    style={{
                      minWidth: 52,
                      justifyContent: "center",
                      borderColor: isSelected ? "rgba(56,189,248,0.55)" : "rgba(255,255,255,0.14)",
                      background: isSelected ? "rgba(56,189,248,0.10)" : undefined,
                    }}
                  >
                    <span
                      aria-hidden="true"
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: 999,
                        background: color,
                        boxShadow: "0 0 0 2px rgba(0,0,0,0.25)",
                        opacity: ready ? 1 : 0.4,
                      }}
                    />
                    <span>{point.label || labelForIndex(index)}</span>
                  </button>
                );
              })}

              <button className="iconButton" type="button" onClick={addPoint} aria-label={t("core.actions.add")}>
                <i className="fa-solid fa-plus" aria-hidden="true" />
              </button>
            </div>
          </>
        ) : null}

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="label">{t("ext.cameras.control.image")}</div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.14)",
                background: "rgba(0,0,0,0.30)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 10,
                overflow: "hidden",
              }}
            >
              {snapshotErrorMessage ? (
                <div className="card">
                  <div className="cardBody">{snapshotErrorMessage}</div>
                </div>
              ) : snapshotUrl ? (
                <div style={{ position: "relative", display: "inline-block", maxWidth: "100%", maxHeight: "100%" }}>
                  <img
                    src={snapshotUrl}
                    alt={t("ext.cameras.control.image")}
                    style={{
                      display: "block",
                      maxWidth: "100%",
                      maxHeight: "100%",
                      borderRadius: 14,
                      border: "1px solid rgba(255,255,255,0.10)",
                    }}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      setImagePointFromEvent(event);
                    }}
                    onMouseMove={(event) => {
                      const p = getImagePointFromEvent(event);
                      if (!p || completePairs < 4) return;
                      setHoverImagePoint(p);
                      setHoverWorldPoint(null);
                      setGhostImagePoint(null);
                    }}
                    onMouseLeave={() => {
                      setHoverImagePoint(null);
                      setGhostWorldPoint(null);
                    }}
                  />

                  {selectedPoints.map((point, index) => {
                    if (!point.image) return null;
                    const isSelected = selectedPointId === point.id;
                    const color = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
                    return (
                      <div
                        key={point.id}
                        style={{
                          position: "absolute",
                          left: `${point.image.x * 100}%`,
                          top: `${point.image.y * 100}%`,
                          transform: "translate(-50%,-50%)",
                          width: isSelected ? 22 : 20,
                          height: isSelected ? 22 : 20,
                          borderRadius: 999,
                          background: color,
                          border: isSelected ? "2px solid rgba(255,255,255,0.92)" : "2px solid rgba(0,0,0,0.65)",
                          boxShadow: "0 8px 18px rgba(0,0,0,0.28)",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontSize: 12,
                          fontWeight: 800,
                          color: "rgba(0,0,0,0.82)",
                          pointerEvents: "none",
                        }}
                      >
                        {point.label || labelForIndex(index)}
                      </div>
                    );
                  })}

                  {ghostImagePoint && completePairs >= 4 ? (
                    <div
                      aria-hidden="true"
                      style={{
                        position: "absolute",
                        left: `${ghostImagePoint.x * 100}%`,
                        top: `${ghostImagePoint.y * 100}%`,
                        transform: "translate(-50%,-50%)",
                        width: 18,
                        height: 18,
                        borderRadius: 999,
                        background: "rgba(251,191,36,0.10)",
                        border: "2px dashed rgba(251,191,36,0.88)",
                        boxShadow: "0 8px 18px rgba(0,0,0,0.22)",
                        pointerEvents: "none",
                      }}
                    />
                  ) : null}
                </div>
              ) : (
                <div className="card">
                  <div className="cardBody">
                    {snapshotLoading ? t("ext.cameras.control.loading") : t("ext.cameras.control.image")}
                  </div>
                </div>
              )}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="label">{t("ext.cameras.control.canvas")}</div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.14)",
                background: "rgba(0,0,0,0.30)",
                overflow: "hidden",
              }}
            >
              <host.ui.Viewport2DReplica interactionMode="select" session={toolSession} style={{ width: "100%", height: "100%" }} />
            </div>
          </div>
        </div>

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <button
            className="chipButton"
            type="button"
            onClick={() => {
              void stopActivePtzMove({ force: true });
              onClose();
            }}
          >
            {t("core.actions.cancel")}
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              onSave(
                sets.map((controlPointSet, index) => ({
                  ...controlPointSet,
                  label:
                    controlPointSet.label.trim() ||
                    (index === 0 ? t("ext.cameras.control.set_default") : t("ext.cameras.control.set_label", { index: index + 1 })),
                  pose_reference: normalizePoseReference(controlPointSet.pose_reference),
                  control_points: controlPointSet.control_points.map((point, pointIndex) => ({
                    ...point,
                    label: point.label || labelForIndex(pointIndex),
                    image: point.image ?? null,
                    world: point.world ?? null,
                  })),
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
  );
}

function padControlPoints(controlPoints: CameraControlPoint[]): CameraControlPoint[] {
  const padded = controlPoints.map((point, index) => ({
    ...point,
    label: point.label || labelForIndex(index),
    image: point.image ?? null,
    world: point.world ?? null,
  }));
  while (padded.length < 4) {
    padded.push({ id: createUniqueId(), label: labelForIndex(padded.length), image: null, world: null });
  }
  return padded;
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
