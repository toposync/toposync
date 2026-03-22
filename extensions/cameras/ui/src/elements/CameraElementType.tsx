import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import cameraSvg from "@fortawesome/fontawesome-free/svgs/solid/camera.svg";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorToolPointerEvent,
  EditorToolSession,
  ElementType,
  HostI18n,
  TopoSyncHost,
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
  stopCameraPtz,
} from "../api/camerasApi";
import { CAMERA_ELEMENT_TYPE_ID, CONTROL_POINT_COLORS } from "../constants";
import {
  createDefaultControlPointSet,
  createUniqueId,
  duplicateControlPointSetForNewView,
  labelForIndex,
  readControlPointSets,
  readRecord,
  readString,
  summarizeControlPointSetQuality,
} from "../parsing";
import type {
  CameraConnectionType,
  CameraControlPoint,
  CameraControlPointSet,
  CameraPoseReference,
  CameraPtzPreset,
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

function formatPtzTelemetryValue(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function createCameraElementType(host: TopoSyncHost): ElementType {
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
    renderActionModal: ({ element }) => <CameraAction element={element} i18n={i18n} />,
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
  host: TopoSyncHost;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const selectedCameraId = readString(props.camera_id).trim();
  const existingControlPointSets = useMemo(() => readControlPointSets(props.control_point_sets), [props.control_point_sets]);
  const readySets = useMemo(
    () => existingControlPointSets.filter((item) => summarizeControlPointSetQuality(item).status !== "incomplete").length,
    [existingControlPointSets],
  );
  const totalSets = existingControlPointSets.length;
  const [isControlPointsOpen, setIsControlPointsOpen] = useState(false);

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
      .map((camera) => ({
        id: readString((camera as any).id),
        name: readString((camera as any).name),
        connectionType: readString((camera as any).connection_type).trim().toLowerCase() as CameraConnectionType | "",
      }))
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
        <label className="label">{t("ext.cameras.editor.control_points")}</label>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="cardMeta">
            {totalSets > 0
              ? t("ext.cameras.editor.control_sets_some", { ready: readySets, total: totalSets })
              : t("ext.cameras.editor.control_points_none")}
          </div>

          <button
            className="chipButton"
            type="button"
            disabled={!selectedCameraId}
            onClick={() => setIsControlPointsOpen(true)}
          >
            {t("ext.cameras.editor.control_sets_open")}
          </button>
        </div>
        {totalSets > 0 && readySets === 0 ? (
          <div className="cardMeta" style={{ marginTop: 6 }}>
            {t("ext.cameras.editor.control_sets_hint")}
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

      <ControlPointsModal
        open={isControlPointsOpen}
        onClose={() => setIsControlPointsOpen(false)}
        host={host}
        i18n={i18n}
        cameraId={selectedCameraId}
        cameraConnectionType={selectedCamera?.connectionType || null}
        initialSets={existingControlPointSets}
        onSave={(controlPointSets) => update({ props: { control_point_sets: controlPointSets } })}
      />
    </div>
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
  host: TopoSyncHost;
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

  async function handlePresetSelection(nextPresetToken: string) {
    const token = String(nextPresetToken || "").trim();
    setSelectedPresetToken(token);
    if (!token) {
      captureCurrentPoseIntoSelectedSet(ptzStatusRef.current, { presetToken: null, presetName: null });
      return;
    }
    if (!cameraId) return;
    const preset = ptzPresets.find((item) => String(item.token || "").trim() === token) ?? null;
    const presetName = String(preset?.name || "").trim() || token;
    const presetPose = {
      pan: typeof preset?.pan === "number" && Number.isFinite(preset.pan) ? preset.pan : null,
      tilt: typeof preset?.tilt === "number" && Number.isFinite(preset.tilt) ? preset.tilt : null,
      zoom: typeof preset?.zoom === "number" && Number.isFinite(preset.zoom) ? preset.zoom : null,
      preset_token: token,
      preset_name: presetName,
    };
    updateSelectedSet({ label: presetName, pose_reference: presetPose });
    setPtzCommandBusy(true);
    setPtzErrorMessage(null);
    try {
      await gotoCameraPtzPreset(cameraId, token);
      await settlePtzAndRefresh({ presetToken: token, presetName, renameFromPreset: true });
    } catch (error) {
      setPtzErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setPtzCommandBusy(false);
    }
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
                    setSelectedSetId(controlPointSet.id);
                    setSelectedPointId(controlPointSet.control_points[0]?.id ?? null);
                  }}
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
              <host.ui.Viewport2DReplica session={toolSession} style={{ width: "100%", height: "100%" }} />
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

function CameraAction({ element, i18n }: { element: CompositionElement; i18n: HostI18n }): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const cameraId = readString(props.camera_id).trim();

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
    if (!cameraId) return;
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
  }, [cameraId]);

  if (!cameraId) {
    return <div className="cardBody">{t("ext.cameras.action.no_camera")}</div>;
  }

  return (
    <div>
      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <div className="label">{readString(props.camera_name) || cameraId}</div>
        <button className="chipButton" type="button" onClick={refresh} disabled={loading}>
          {loading ? t("ext.cameras.action.loading") : t("ext.cameras.action.refresh")}
        </button>
      </div>

      <div className="sectionDivider" />

      {errorMessage ? (
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
