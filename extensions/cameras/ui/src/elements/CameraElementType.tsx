import React, { useEffect, useMemo, useRef, useState } from "react";
import Select, { type SingleValue, type StylesConfig } from "react-select";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import cameraSvg from "@fortawesome/fontawesome-free/svgs/solid/camera.svg";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import type {
  BoundsXZ,
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostI18n,
  ToposyncHost,
} from "@toposync/plugin-api";

import {
  fetchCameraPanoramaContext,
  fetchCameraPanoramaJob,
  fetchCameraSnapshot,
  fetchCamerasIndex,
} from "../api/camerasApi";
import { CAMERA_ELEMENT_TYPE_ID } from "../constants";
import {
  readCalibratedViews,
  legacyMetricCalibrations,
  readRecord,
  readString,
  summarizeCalibratedViewQuality,
} from "../parsing";
import type {
  CameraCalibratedView,
  CameraConnectionType,
  CameraPanoramaContext,
  CameraPanoramaJob,
  CameraSourceConfig,
  CameraSourceRole,
  CamerasIndex,
} from "../types";
import { CameraPanoramaMappingModal } from "./CameraPanoramaMappingModal";

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

function cameraBounds(element: CompositionElement): BoundsXZ {
  return {
    minX: element.position.x - 0.42,
    maxX: element.position.x + 0.42,
    minZ: element.position.z - 0.42,
    maxZ: element.position.z + 0.42,
  };
}

type CameraSnapshotSourceOption = Pick<
  CameraSourceConfig,
  "id" | "name" | "enabled" | "is_default" | "kind" | "role" | "view_id" | "origin" | "video" | "ingest" | "metadata"
> & {
  has_ptz?: boolean;
};

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
      // Positive Three.js Y rotation turns +Z toward +X on the x/z canvas.
      canvasContext.rotate(-rotation);
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
    renderEditorModal: ({ compositionId, initialAction, element, elements, elementTypesById, update, remove, close }) => (
      <CameraEditor
        compositionId={compositionId}
        initialAction={initialAction}
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
  compositionId,
  initialAction,
  element,
  elements,
  elementTypesById,
  update,
  remove,
  close,
  i18n,
  host,
}: {
  compositionId?: string;
  initialAction?: string;
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
  const metricRecords = legacyMetricCalibrations(props);
  const selectedCameraId = readString(props.camera_id).trim();
  const existingCalibratedViews = useMemo(() => {
    return readCalibratedViews(props.calibrated_views, element.position);
  }, [element.position, props.calibrated_views]);
  const readyCalibratedViews = useMemo(
    () => Object.prototype.hasOwnProperty.call(props, "panorama_mapping") ? []
      : existingCalibratedViews.filter((item) => summarizeCalibratedViewQuality(item).status !== "incomplete"),
    [existingCalibratedViews, props.panorama_mapping],
  );
  const spatialClipAreaId = readSpatialVideoClipAreaId(props);
  const [isPanoramaMappingOpen, setIsPanoramaMappingOpen] = useState(false);
  useEffect(() => {
    // Open after the host editor has registered its modal focus layer.
    if (initialAction === "panorama-mapping") setIsPanoramaMappingOpen(true);
  }, [initialAction]);
  const [legacyPanoramaContext, setLegacyPanoramaContext] = useState<CameraPanoramaContext | null>(null);
  const [activePanorama, setActivePanorama] = useState<CameraPanoramaJob | null>(null);

  const [camerasIndex, setCamerasIndex] = useState<CamerasIndex | null>(null);
  const [indexErrorMessage, setIndexErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    setLegacyPanoramaContext(null);
    setActivePanorama(null);
    if (!selectedCameraId) return;
    const controller = new AbortController();
    void fetchCameraPanoramaContext(selectedCameraId, element.id, controller.signal)
      .then(async (context) => {
        if (controller.signal.aborted) return;
        setLegacyPanoramaContext(context);
        if (context.active) {
          const active = context.job?.id === context.active.job_id && context.job.revision === context.active.revision
            ? context.job : await fetchCameraPanoramaJob(selectedCameraId, context.active.job_id, controller.signal);
          if (!controller.signal.aborted) setActivePanorama(active);
        }
      })
      .catch(() => { /* A saved mapping reference remains available if context cannot load. */ });
    return () => controller.abort();
  }, [selectedCameraId, element.id]);

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
                view_id: readString(source?.view_id).trim() || readString(source?.id).trim(),
                has_ptz: source?.has_ptz === true || source?.origin?.has_ptz === true,
                origin: {
                  type: source?.origin?.type === "onvif_profile" ? "onvif_profile" : "rtsp",
                  rtsp_url: "",
                  profile_token: null,
                  profile_name: readString(source?.origin?.profile_name).trim() || null,
                  has_ptz: source?.has_ptz === true || source?.origin?.has_ptz === true,
                  metadata: {},
                },
                video: {
                  width: typeof source?.video?.width === "number" ? source.video.width : null,
                  height: typeof source?.video?.height === "number" ? source.video.height : null,
                  fps: typeof source?.video?.fps === "number" ? source.video.fps : null,
                  codec: readString(source?.video?.codec).trim() || null,
                },
                ingest: {
                  mode: "direct",
                  host_server_id: "",
                },
                metadata: {},
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

  function openPanoramaSettings(requestedSourceId?: string): void {
    if (!selectedCameraId) return;
    const sourceId = requestedSourceId
      || readString(readRecord(props.panorama_mapping).source_id)
      || legacyPanoramaContext?.job?.source_id
      || readString(props.source_id)
      || selectedCamera?.sources.find((source) => source.enabled && source.is_default)?.id
      || selectedCamera?.sources.find((source) => source.enabled)?.id;
    const parameters = new URLSearchParams({ panel: "com.toposync.cameras", camera_id: selectedCameraId });
    if (sourceId) {
      parameters.set("source_id", sourceId);
      try { sessionStorage.setItem(`toposync.panorama-source.${selectedCameraId}.${element.id}`, sourceId); }
      catch { /* The settings URL still retains the selected source. */ }
    }
    if (compositionId) {
      parameters.set("return_composition_id", compositionId);
      parameters.set("return_element_id", element.id);
    }
    setIsPanoramaMappingOpen(false);
    close();
    window.history.pushState({}, "", resolveToposyncUrl(`/settings?${parameters.toString()}`));
    window.dispatchEvent(new Event("popstate"));
  }
  const panoramaFootprint = useMemo(() => activePanorama?.active && activePanorama.source_panorama?.compatible
    && activePanorama.solution?.quality.status === "ready" && activePanorama.solution.preview?.eligible
    ? activePanorama.solution.support_polygon.map(([x, z]) => ({ x, z })) : [], [activePanorama]);
  const hasAreaMapping = readyCalibratedViews.length > 0 || panoramaFootprint.length >= 3;
  const areaClipOptions = useMemo<CameraAreaClipOption[]>(() => {
    const footprints = [...readyCalibratedViews.map(calibratedViewFootprint), panoramaFootprint].filter((footprint) => footprint.length >= 3);
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
  }, [element.id, elementTypesById, elements, readyCalibratedViews, panoramaFootprint]);
  const selectedAreaClipOption = areaClipOptions.find((option) => option.value === spatialClipAreaId) ?? null;
  const areaClipDisabled = !selectedCameraId || !hasAreaMapping || areaClipOptions.length === 0;
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
        <label className="label">{t("ext.cameras.panorama.mapping_label")}</label>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="cardMeta">
            {t(activePanorama?.active && activePanorama.permissions?.can_activate
              ? "ext.cameras.mapping.active"
              : props.panorama_mapping || existingCalibratedViews.length
                ? "ext.cameras.mapping.review_required" : "ext.cameras.mapping.absent")}
            {activePanorama?.active ? ` · ${t(activePanorama.permissions?.aim_enabled
              ? "ext.cameras.mapping.positioning_available" : "ext.cameras.mapping.positioning_unqualified")}` : ""}
          </div>

          <button
            className="primaryButton"
            style={{ minHeight: 44, whiteSpace: "normal" }}
            type="button"
            disabled={!selectedCameraId}
            onClick={() => setIsPanoramaMappingOpen(true)}
          >
            {t(readRecord(props.panorama_mapping).status === "ready"
              ? "ext.cameras.panorama.review_calibration"
              : legacyPanoramaContext?.job?.points.length
              ? "ext.cameras.panorama.continue_calibration"
              : "ext.cameras.panorama.calibrate_camera")}
          </button>
        </div>
      </div>

      {metricRecords.length > 0 ? <div className="field">
        <p className="cardMeta">{t("ext.cameras.mapping.metric_preservation")}</p>
        <button className="chipButton" type="button" onClick={() => update({ props: { metric_calibrations: metricRecords } })}>
          {t("ext.cameras.mapping.metric_separate")}
        </button>
      </div> : null}

      <div className="field">
        <label className="label">{t("ext.cameras.editor.spatial_clip")}</label>
        <Select<CameraAreaClipOption, false>
          aria-label={t("ext.cameras.editor.spatial_clip")}
          styles={cameraAreaSelectStyles}
          value={selectedAreaClipOption}
          options={areaClipOptions}
          isClearable
          isDisabled={areaClipDisabled}
          placeholder={t("ext.cameras.editor.spatial_clip_placeholder")}
          onChange={(option: SingleValue<CameraAreaClipOption>) => updateSpatialVideoClipArea(option?.value ?? "")}
        />
        <div className="cardMeta" style={{ marginTop: 6 }}>
          {!hasAreaMapping
            ? t("ext.cameras.panorama.area_requires_mapping")
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

      <CameraPanoramaMappingModal
        open={isPanoramaMappingOpen}
        onClose={() => setIsPanoramaMappingOpen(false)}
        onOpenSettings={openPanoramaSettings}
        host={host}
        i18n={i18n}
        element={element}
        cameraId={selectedCameraId}
        onActivate={(job) => { setActivePanorama(job); update({ props: {
          panorama_mapping: { job_id: job.id, revision: job.revision, source_id: job.source_id, status: "ready" },
          previous_panorama_mapping: props.panorama_mapping,
        } }); }}
      />

    </div>
  );
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
