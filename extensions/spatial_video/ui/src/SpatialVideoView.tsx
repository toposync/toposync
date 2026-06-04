import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";

import type {
  BoundsXZ,
  CompositionElement,
  ElementType,
  Notification2DPin,
  RenderViewContext,
} from "@toposync/plugin-api";

import { areaClipSignature, controlPointSetIntersectsAreaClip } from "./areaClip";
import { fetchCameraPtzPresets, fetchCameraPtzStatus, fetchLiveViews } from "./api";
import { resolveProjectionCandidates } from "./candidates";
import { markerEntries, markerVideoStatus, type MarkerVideoStatus } from "./markers";
import { controlPointSetProjectionSignature, mediaContentRectSignature, type ProjectionMeshDensity, type ProjectionStrategyId } from "./projection";
import { createProjectionGeometry } from "./projectionGeometry";
import { resolveActiveProjectionPose } from "./ptzProjection";
import { readSpatialVideoSettings, SPATIAL_VIDEO_RENDER_VIEW_ID } from "./spatialSettings";
import { SpatialVideoCompatibilityNotice } from "./SpatialVideoCompatibilityNotice";
import { StreamTextureSource } from "./streamTexture";
import type { CameraControlPointSet, CameraLiveView, PtzPreset, PtzStatus, WorldPoint } from "./types";

type ProjectionEntry = {
  candidateId: string;
  source: StreamTextureSource;
  unsubscribe: () => void;
  mesh: THREE.Mesh;
  material: THREE.MeshBasicMaterial;
  set: CameraControlPointSet;
  clipPolygon: WorldPoint[] | null;
  setId: string;
  setSignature: string;
  areaClipSignature: string;
  uvRectSignature: string;
  strategyId: ProjectionStrategyId;
  meshDensity: ProjectionMeshDensity;
};

const MAX_PIXEL_RATIO = 2;
const DEFAULT_WORLD_BOUNDS: BoundsXZ = { minX: -1, maxX: 1, minZ: -1, maxZ: 1 };
const PIN_PATH = "M 0 0 L -60.62 -105 A 70 70 0 1 1 60.62 -105 Z";

const NOTIFICATION_PRIORITY_COLOR: Record<NonNullable<Notification2DPin["priority"]>, string> = {
  high: "#ff3b3b",
  medium: "#00d1ff",
  low: "#9aa4b2",
};

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function includePoint(bounds: BoundsXZ & { empty?: boolean }, point: { x: number; z: number }, pad = 0): void {
  if (!Number.isFinite(point.x) || !Number.isFinite(point.z)) return;
  bounds.minX = Math.min(bounds.minX, point.x - pad);
  bounds.maxX = Math.max(bounds.maxX, point.x + pad);
  bounds.minZ = Math.min(bounds.minZ, point.z - pad);
  bounds.maxZ = Math.max(bounds.maxZ, point.z + pad);
  bounds.empty = false;
}

function includeBounds(bounds: BoundsXZ & { empty?: boolean }, input: BoundsXZ | null | undefined): void {
  if (!input) return;
  includePoint(bounds, { x: input.minX, z: input.minZ });
  includePoint(bounds, { x: input.maxX, z: input.maxZ });
}

function includeControlPointBounds(bounds: BoundsXZ & { empty?: boolean }, element: CompositionElement): void {
  const calibratedViews = Array.isArray(element.props?.calibrated_views) ? element.props.calibrated_views : [];
  for (const rawView of calibratedViews) {
    const view = readRecord(rawView);
    const projection = readRecord(view.projection_model);
    const quad = readRecord(projection.world_quad);
    for (const corner of ["top_left", "top_right", "bottom_right", "bottom_left"]) {
      const point = readRecord(quad[corner]);
      const x = finiteNumber(point.x);
      const z = finiteNumber(point.z);
      if (x != null && z != null) includePoint(bounds, { x, z }, 0.12);
    }
  }
  const sets = Array.isArray(element.props?.control_point_sets) ? element.props.control_point_sets : [];
  for (const rawSet of sets) {
    const set = readRecord(rawSet);
    const points = Array.isArray(set.control_points) ? set.control_points : [];
    for (const rawPoint of points) {
      const point = readRecord(rawPoint);
      const world = readRecord(point.world);
      const x = finiteNumber(world.x);
      const z = finiteNumber(world.z);
      if (x != null && z != null) includePoint(bounds, { x, z }, 0.12);
    }
  }
}

function computeSceneBounds(elements: CompositionElement[], elementTypesById: Record<string, ElementType>): BoundsXZ {
  const bounds: BoundsXZ & { empty?: boolean } = {
    minX: Infinity,
    maxX: -Infinity,
    minZ: Infinity,
    maxZ: -Infinity,
    empty: true,
  };
  for (const element of elements) {
    const def = elementTypesById[element.type];
    try {
      includeBounds(bounds, def?.getMain2DBounds?.(element));
    } catch {
      // Keep the spatial view usable if one element cannot report bounds.
    }
    includePoint(bounds, { x: element.position.x, z: element.position.z }, 0.35);
    const props = element.props ?? {};
    const vertices = Array.isArray(props.vertices) ? props.vertices : [];
    for (const item of vertices) {
      if (!item || typeof item !== "object") continue;
      const rec = item as Record<string, unknown>;
      const x = typeof rec.x === "number" ? rec.x : null;
      const z = typeof rec.z === "number" ? rec.z : null;
      if (x != null && Number.isFinite(x) && z != null && Number.isFinite(z)) includePoint(bounds, { x, z });
    }
    includeControlPointBounds(bounds, element);
  }
  if (bounds.empty) return DEFAULT_WORLD_BOUNDS;
  const spanX = Math.max(0.25, bounds.maxX - bounds.minX);
  const spanZ = Math.max(0.25, bounds.maxZ - bounds.minZ);
  const padX = Math.max(0.5, spanX * 0.08);
  const padZ = Math.max(0.5, spanZ * 0.08);
  return {
    minX: bounds.minX - padX,
    maxX: bounds.maxX + padX,
    minZ: bounds.minZ - padZ,
    maxZ: bounds.maxZ + padZ,
  };
}

function fitOrthographicCamera(camera: THREE.OrthographicCamera, bounds: BoundsXZ, width: number, height: number): void {
  const aspect = width > 0 && height > 0 ? width / height : 1;
  const spanX = Math.max(1e-6, bounds.maxX - bounds.minX);
  const spanZ = Math.max(1e-6, bounds.maxZ - bounds.minZ);
  const worldHeight = Math.max(spanZ * 1.12, 2);
  const worldWidth = Math.max(spanX * 1.12, worldHeight * aspect);
  const halfHeight = Math.max(worldHeight, worldWidth / aspect) / 2;
  const halfWidth = halfHeight * aspect;
  camera.left = -halfWidth;
  camera.right = halfWidth;
  camera.top = halfHeight;
  camera.bottom = -halfHeight;
  const centerX = (bounds.minX + bounds.maxX) / 2;
  const centerZ = (bounds.minZ + bounds.maxZ) / 2;
  camera.position.set(centerX, 80, centerZ);
  camera.lookAt(centerX, 0, centerZ);
  camera.zoom = 1;
  camera.updateProjectionMatrix();
}

function cameraWorldBounds(camera: THREE.OrthographicCamera): BoundsXZ {
  const halfWidth = (camera.right - camera.left) / Math.max(1e-6, camera.zoom) / 2;
  const halfHeight = (camera.top - camera.bottom) / Math.max(1e-6, camera.zoom) / 2;
  return {
    minX: camera.position.x - halfWidth,
    maxX: camera.position.x + halfWidth,
    minZ: camera.position.z - halfHeight,
    maxZ: camera.position.z + halfHeight,
  };
}

function boundsChanged(a: BoundsXZ, b: BoundsXZ): boolean {
  return (
    Math.abs(a.minX - b.minX) > 1e-5 ||
    Math.abs(a.maxX - b.maxX) > 1e-5 ||
    Math.abs(a.minZ - b.minZ) > 1e-5 ||
    Math.abs(a.maxZ - b.maxZ) > 1e-5
  );
}

function elementLayerRank(element: CompositionElement, elementTypesById: Record<string, ElementType>): number {
  const group = elementTypesById[element.type]?.layerGroup ?? "";
  if (group === "background") return -1;
  if (group === "areas") return 0;
  if (group === "walls") return 2;
  if (group === "measurements") return 4;
  return 3;
}

function belongsToVectorLayer(element: CompositionElement, elementTypesById: Record<string, ElementType>, layer: "below-video" | "above-video"): boolean {
  const group = elementTypesById[element.type]?.layerGroup ?? "";
  if (layer === "below-video") return group === "background" || group === "areas";
  return group !== "background" && group !== "areas";
}

function fallbackVectorElement(element: CompositionElement, key: string): React.ReactNode {
  return (
    <g key={key} className="spatialVideoFallbackElement" opacity={0.92}>
      <circle cx={element.position.x} cy={element.position.z} r={0.15} fill="rgba(56,189,248,0.24)" stroke="rgba(226,232,240,0.54)" strokeWidth={0.018} vectorEffect="non-scaling-stroke" />
      <path
        d={`M ${element.position.x} ${element.position.z + 0.22} L ${element.position.x - 0.075} ${element.position.z + 0.06} L ${element.position.x + 0.075} ${element.position.z + 0.06} Z`}
        fill="rgba(251,191,36,0.82)"
      />
      <title>{element.name || element.type}</title>
    </g>
  );
}

function vectorLayerNodes(args: {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  sceneBounds: BoundsXZ;
  layer: "below-video" | "above-video";
}): React.ReactNode[] {
  return args.elements
    .map((element, index) => ({ element, index }))
    .filter(({ element }) => belongsToVectorLayer(element, args.elementTypesById, args.layer))
    .sort((a, b) => elementLayerRank(a.element, args.elementTypesById) - elementLayerRank(b.element, args.elementTypesById) || a.index - b.index)
    .map(({ element, index }) => {
      const key = `${args.layer}:${element.id}:${index}`;
      const def = args.elementTypesById[element.type];
      if (!def?.renderMain2DVector) return args.layer === "above-video" ? fallbackVectorElement(element, key) : null;
      try {
        const rendered = def.renderMain2DVector({ element, elements: args.elements, ctx: { bounds: args.sceneBounds, scale: 1 } });
        if (rendered == null && args.layer === "above-video") return fallbackVectorElement(element, key);
        return <React.Fragment key={key}>{rendered}</React.Fragment>;
      } catch (error) {
        console.warn(`[spatial-video:renderMain2DVector:${element.type}]`, error);
        return args.layer === "above-video" ? fallbackVectorElement(element, key) : null;
      }
    })
    .filter((node): node is React.ReactNode => node != null);
}

function VectorMapLayer({
  elements,
  elementTypesById,
  sceneBounds,
  viewBounds,
  layer,
  zIndex,
}: {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  sceneBounds: BoundsXZ;
  viewBounds: BoundsXZ;
  layer: "below-video" | "above-video";
  zIndex: number;
}): React.ReactElement {
  const nodes = useMemo(
    () => vectorLayerNodes({ elements, elementTypesById, sceneBounds, layer }),
    [elements, elementTypesById, sceneBounds, layer],
  );
  const width = Math.max(1e-6, viewBounds.maxX - viewBounds.minX);
  const height = Math.max(1e-6, viewBounds.maxZ - viewBounds.minZ);
  return (
    <svg
      className="mainVector2dSvg spatialVideoMapSvg"
      viewBox={`${viewBounds.minX} ${viewBounds.minZ} ${width} ${height}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none", zIndex }}
    >
      <defs>
        <filter id={`spatialVideoSoftShadow-${layer}`} x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="0.035" stdDeviation="0.04" floodColor="rgba(0,0,0,0.24)" />
        </filter>
        <filter id="mainVector2dSoftShadow" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="0.035" stdDeviation="0.04" floodColor="rgba(0,0,0,0.24)" />
        </filter>
        <pattern id="mainVector2dGrassPattern" patternUnits="userSpaceOnUse" width="0.34" height="0.34">
          <path d="M 0.05 0.30 L 0.11 0.08 M 0.18 0.32 L 0.22 0.12 M 0.29 0.28 L 0.31 0.05" stroke="rgba(5,46,22,0.36)" strokeWidth="0.012" strokeLinecap="round" />
          <path d="M 0.08 0.32 L 0.14 0.18 M 0.23 0.31 L 0.28 0.18" stroke="rgba(134,239,172,0.28)" strokeWidth="0.008" strokeLinecap="round" />
        </pattern>
        <pattern id="mainVector2dConcretePattern" patternUnits="userSpaceOnUse" width="0.42" height="0.42">
          <path d="M 0.04 0.10 H 0.12 M 0.28 0.06 H 0.36 M 0.18 0.30 H 0.31" stroke="rgba(15,23,42,0.20)" strokeWidth="0.009" strokeLinecap="round" />
          <circle cx="0.12" cy="0.28" r="0.012" fill="rgba(255,255,255,0.14)" />
          <circle cx="0.34" cy="0.20" r="0.009" fill="rgba(15,23,42,0.16)" />
        </pattern>
        <linearGradient id="mainVector2dWaterGradient" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="rgba(186,230,253,0.50)" />
          <stop offset="45%" stopColor="rgba(14,165,233,0.28)" />
          <stop offset="100%" stopColor="rgba(3,105,161,0.46)" />
        </linearGradient>
        <pattern id="mainVector2dWaterPattern" patternUnits="userSpaceOnUse" width="0.52" height="0.30">
          <path d="M 0.02 0.17 C 0.12 0.08, 0.22 0.08, 0.32 0.17 S 0.48 0.26, 0.56 0.17" fill="none" stroke="rgba(224,242,254,0.34)" strokeWidth="0.012" strokeLinecap="round" />
          <path d="M -0.04 0.28 C 0.08 0.20, 0.18 0.20, 0.30 0.28 S 0.48 0.36, 0.60 0.28" fill="none" stroke="rgba(7,89,133,0.22)" strokeWidth="0.01" strokeLinecap="round" />
        </pattern>
      </defs>
      <g className={`spatialVideoMapLayer spatialVideoMapLayer-${layer}`}>{nodes}</g>
    </svg>
  );
}

function screenToWorld(point: { x: number; y: number }, rect: DOMRect, bounds: BoundsXZ): { x: number; z: number } {
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  return {
    x: bounds.minX + ((point.x - rect.left) / width) * (bounds.maxX - bounds.minX),
    z: bounds.minZ + ((point.y - rect.top) / height) * (bounds.maxZ - bounds.minZ),
  };
}

function worldToScreen(point: { x: number; z: number }, rect: DOMRect, bounds: BoundsXZ): { x: number; y: number } {
  return {
    x: ((point.x - bounds.minX) / Math.max(1e-6, bounds.maxX - bounds.minX)) * rect.width,
    y: ((point.z - bounds.minZ) / Math.max(1e-6, bounds.maxZ - bounds.minZ)) * rect.height,
  };
}

function SpatialNotificationPinView({
  screenX,
  screenY,
  priority,
  closed,
  trail,
}: {
  screenX: number;
  screenY: number;
  priority?: Notification2DPin["priority"];
  closed?: boolean;
  trail?: ReadonlyArray<{ x: number; y: number }>;
}): React.ReactElement {
  const tone = priority ?? "medium";
  const color = NOTIFICATION_PRIORITY_COLOR[tone];
  const reactId = React.useId();
  const gradId = `spatial-notification-pin-${reactId.replace(/:/g, "")}`;
  const trailPath =
    trail && trail.length >= 2
      ? trail.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ")
      : null;

  return (
    <>
      {trailPath ? (
        <svg
          className="notification2dTrail"
          aria-hidden="true"
          style={{ ["--notification2d-trail-color" as string]: color }}
        >
          <path d={trailPath} />
        </svg>
      ) : null}
      <div
        className={`notification2dPin${closed ? " isClosed" : ""}`}
        style={{
          left: screenX,
          top: screenY,
          ["--notification2d-pin-color" as string]: color,
        }}
        aria-hidden="true"
      >
        <span className="notification2dPinSpot" />
        <span className="notification2dPinPulse" />
        <span className="notification2dPinPulse" style={{ animationDelay: "-0.7s" }} />
        <span className="notification2dPinPulse" style={{ animationDelay: "-1.4s" }} />
        <svg className="notification2dPinShape" viewBox="-80 -240 160 240" width="32" height="48">
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ffffff" stopOpacity="0.45" />
              <stop offset="55%" stopColor="#ffffff" stopOpacity="0" />
              <stop offset="100%" stopColor="#000000" stopOpacity="0.30" />
            </linearGradient>
          </defs>
          <path d={PIN_PATH} fill="var(--notification2d-pin-color, #00d1ff)" />
          <path d={PIN_PATH} fill={`url(#${gradId})`} />
          <circle cx="0" cy="-140" r="20" fill="#ffffff" />
        </svg>
      </div>
    </>
  );
}

export function SpatialVideoView({
  elements,
  elementTypesById,
  compositionId,
  viewSettings,
  activeNotification,
  activeNotificationRenderer,
  onElementActivated,
}: RenderViewContext): React.ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.OrthographicCamera | null>(null);
  const projectionGroupRef = useRef<THREE.Group | null>(null);
  const projectionEntriesRef = useRef<Map<string, ProjectionEntry>>(new Map());
  const pointerDownRef = useRef<{ x: number; y: number; cameraX: number; cameraZ: number; moved: boolean } | null>(null);
  const lastSizeRef = useRef({ width: 1, height: 1 });
  const sceneBounds = useMemo(() => computeSceneBounds(elements, elementTypesById), [elements, elementTypesById]);
  const sceneBoundsRef = useRef<BoundsXZ>(sceneBounds);
  const [viewBounds, setViewBounds] = useState<BoundsXZ>(DEFAULT_WORLD_BOUNDS);
  const viewBoundsRef = useRef<BoundsXZ>(DEFAULT_WORLD_BOUNDS);
  const [liveViews, setLiveViews] = useState<CameraLiveView[]>([]);
  const [liveViewsLoading, setLiveViewsLoading] = useState(true);
  const [liveViewsError, setLiveViewsError] = useState<string | null>(null);
  const [ptzByCamera, setPtzByCamera] = useState<Record<string, PtzStatus | null>>({});
  const [presetsByCamera, setPresetsByCamera] = useState<Record<string, PtzPreset[]>>({});
  const [version, setVersion] = useState(0);
  const spatialSettings = useMemo(
    () => readSpatialVideoSettings(viewSettings.renderViewSettings?.[SPATIAL_VIDEO_RENDER_VIEW_ID]),
    [viewSettings.renderViewSettings],
  );
  const projectionStrategyId = spatialSettings.projectionStrategyId;
  const meshDensity = spatialSettings.meshDensity;

  const requestRender = useCallback(() => setVersion((prev) => prev + 1), []);

  const notificationOverlay = useMemo(() => {
    if (!activeNotification || !activeNotificationRenderer?.create2DOverlay) return null;
    try {
      return activeNotificationRenderer.create2DOverlay(
        { compositionId },
        activeNotification,
        { openImage: () => undefined },
      );
    } catch (error) {
      console.warn(`[spatial-video:create2DOverlay:${activeNotificationRenderer.id}]`, error);
      return null;
    }
  }, [activeNotification, activeNotificationRenderer, compositionId]);

  useEffect(() => {
    return () => {
      notificationOverlay?.dispose?.();
    };
  }, [notificationOverlay]);

  const notificationPin = useMemo(() => {
    if (!notificationOverlay) return null;
    const pinData = notificationOverlay.pin();
    if (!pinData) return null;

    const { width, height } = lastSizeRef.current;
    const rect = { width, height } as DOMRect;
    const head = worldToScreen({ x: pinData.x, z: pinData.z }, rect, viewBounds);
    const trail = pinData.trail && pinData.trail.length >= 2 ? pinData.trail.map((point) => worldToScreen(point, rect, viewBounds)) : undefined;
    return {
      screenX: head.x,
      screenY: head.y,
      trail,
      priority: pinData.priority,
      closed: pinData.closed,
    };
  }, [notificationOverlay, viewBounds]);

  useEffect(() => {
    sceneBoundsRef.current = sceneBounds;
  }, [sceneBounds]);

  const syncViewBounds = useCallback((next: BoundsXZ) => {
    if (!boundsChanged(viewBoundsRef.current, next)) return;
    viewBoundsRef.current = next;
    setViewBounds(next);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLiveViewsLoading(true);
    setLiveViewsError(null);
    void fetchLiveViews(controller.signal)
      .then((items) => {
        setLiveViews(items);
        setLiveViewsLoading(false);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setLiveViewsError(error instanceof Error ? error.message : String(error));
        setLiveViewsLoading(false);
      });
    return () => controller.abort();
  }, [compositionId]);

  const candidates = useMemo(
    () => resolveProjectionCandidates(elements, elementTypesById, liveViews),
    [elementTypesById, elements, liveViews],
  );

  const activePoses = useMemo(() => {
    const out = new Map<string, ReturnType<typeof resolveActiveProjectionPose>>();
    for (const candidate of candidates) {
      out.set(
        candidate.id,
        resolveActiveProjectionPose({
          sets: candidate.controlPointSets,
          fallback: candidate.initialControlPointSet,
          ptzStatus: ptzByCamera[candidate.cameraId] ?? null,
          previousSetId: projectionEntriesRef.current.get(candidate.id)?.setId ?? null,
          presets: presetsByCamera[candidate.cameraId] ?? [],
        }),
      );
    }
    return out;
  }, [candidates, presetsByCamera, ptzByCamera]);

  useEffect(() => {
    if (candidates.length === 0) {
      setPresetsByCamera({});
      return;
    }
    const controller = new AbortController();
    void Promise.all(
      candidates.map(async (candidate) => {
        try {
          const presets = await fetchCameraPtzPresets(candidate.cameraId, candidate.cameraSourceId, controller.signal);
          return [candidate.cameraId, presets] as const;
        } catch {
          return [candidate.cameraId, [] as PtzPreset[]] as const;
        }
      }),
    ).then((entries) => {
      if (controller.signal.aborted) return;
      const next: Record<string, PtzPreset[]> = {};
      for (const [cameraId, presets] of entries) next[cameraId] = presets;
      setPresetsByCamera(next);
    });
    return () => controller.abort();
  }, [candidates]);

  useEffect(() => {
    if (candidates.length === 0) return;
    let cancelled = false;
    let timeoutId: number | null = null;
    const poll = async () => {
      const next: Record<string, PtzStatus | null> = {};
      await Promise.all(
        candidates.map(async (candidate) => {
          try {
            next[candidate.cameraId] = await fetchCameraPtzStatus(candidate.cameraId, candidate.cameraSourceId);
          } catch {
            next[candidate.cameraId] = null;
          }
        }),
      );
      if (cancelled) return;
      setPtzByCamera(next);
      const moving = Object.values(next).some((status) => String(status?.move_status || "").toLowerCase() === "moving");
      timeoutId = window.setTimeout(poll, moving ? 300 : 1500);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timeoutId != null) window.clearTimeout(timeoutId);
    };
  }, [candidates]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
    renderer.setClearColor(0x000000, 0);
    renderer.domElement.style.display = "block";
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    renderer.domElement.style.position = "absolute";
    renderer.domElement.style.inset = "0";
    renderer.domElement.style.zIndex = "1";
    renderer.domElement.style.pointerEvents = "none";
    renderer.domElement.style.touchAction = "none";
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const projectionGroup = new THREE.Group();
    scene.add(projectionGroup);

    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 200);
    camera.up.set(0, 0, -1);
    camera.position.set(0, 80, 0);
    camera.lookAt(0, 0, 0);

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;
    projectionGroupRef.current = projectionGroup;

    const resize = () => {
      const width = Math.max(1, container.clientWidth);
      const height = Math.max(1, container.clientHeight);
      lastSizeRef.current = { width, height };
      renderer.setSize(width, height, false);
      fitOrthographicCamera(camera, sceneBoundsRef.current, width, height);
      syncViewBounds(cameraWorldBounds(camera));
      requestRender();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();

    let raf = 0;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      for (const entry of projectionEntriesRef.current.values()) {
        const map = entry.material.map;
        if (map) map.needsUpdate = true;
      }
      renderer.render(scene, camera);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      observer.disconnect();
      cancelAnimationFrame(raf);
      for (const entry of projectionEntriesRef.current.values()) {
        entry.unsubscribe();
        entry.source.destroy();
        entry.mesh.geometry.dispose();
        entry.material.dispose();
      }
      projectionEntriesRef.current.clear();
      renderer.dispose();
      container.removeChild(renderer.domElement);
      rendererRef.current = null;
      sceneRef.current = null;
      cameraRef.current = null;
      projectionGroupRef.current = null;
    };
  }, [requestRender, syncViewBounds]);

  useEffect(() => {
    const camera = cameraRef.current;
    if (!camera) return;
    const { width, height } = lastSizeRef.current;
    fitOrthographicCamera(camera, sceneBounds, width, height);
    syncViewBounds(cameraWorldBounds(camera));
    requestRender();
  }, [compositionId, requestRender, sceneBounds, syncViewBounds]);

  useEffect(() => {
    const group = projectionGroupRef.current;
    if (!group) return;
    const nextIds = new Set(candidates.map((candidate) => candidate.id));
    for (const [id, entry] of projectionEntriesRef.current) {
      if (nextIds.has(id)) continue;
      group.remove(entry.mesh);
      entry.unsubscribe();
      entry.source.destroy();
      entry.mesh.geometry.dispose();
      entry.material.dispose();
      projectionEntriesRef.current.delete(id);
    }

    for (const candidate of candidates) {
      const pose = activePoses.get(candidate.id);
      const set = pose?.set ?? candidate.initialControlPointSet;
      const setSignature = controlPointSetProjectionSignature(set);
      const clipSignature = areaClipSignature(candidate.areaClip);
      const clipPolygon = candidate.areaClip?.polygon ?? null;
      const visible = pose?.status !== "unmatched" && controlPointSetIntersectsAreaClip(set, candidate.areaClip);
      const existing = projectionEntriesRef.current.get(candidate.id);
      const currentContentRect = existing?.source.getSnapshot().contentRect ?? null;
      const uvRectSignature = mediaContentRectSignature(currentContentRect);
      if (
        existing &&
        (existing.setId !== set.id ||
          existing.setSignature !== setSignature ||
          existing.areaClipSignature !== clipSignature ||
          existing.uvRectSignature !== uvRectSignature ||
          existing.strategyId !== projectionStrategyId ||
          existing.meshDensity !== meshDensity)
      ) {
        const geometry = createProjectionGeometry(set, projectionStrategyId, meshDensity, { clipPolygon, uvRect: currentContentRect });
        if (geometry) {
          existing.mesh.geometry.dispose();
          existing.mesh.geometry = geometry;
          existing.set = set;
          existing.clipPolygon = clipPolygon;
          existing.setId = set.id;
          existing.setSignature = setSignature;
          existing.areaClipSignature = clipSignature;
          existing.uvRectSignature = uvRectSignature;
          existing.strategyId = projectionStrategyId;
          existing.meshDensity = meshDensity;
        } else {
          group.remove(existing.mesh);
          existing.unsubscribe();
          existing.source.destroy();
          existing.mesh.geometry.dispose();
          existing.material.dispose();
          projectionEntriesRef.current.delete(candidate.id);
          continue;
        }
      }
      if (existing) {
        existing.set = set;
        existing.clipPolygon = clipPolygon;
        existing.mesh.userData.spatialPoseVisible = visible;
        existing.mesh.visible = visible && Boolean(existing.material.map);
        existing.material.opacity = pose?.moving ? 0.68 : 0.82;
        continue;
      }

      const geometry = createProjectionGeometry(set, projectionStrategyId, meshDensity, { clipPolygon });
      if (!geometry) continue;
      const material = new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: pose?.moving ? 0.68 : 0.82,
        depthTest: true,
        depthWrite: false,
        side: THREE.DoubleSide,
        polygonOffset: true,
        polygonOffsetFactor: -1,
        polygonOffsetUnits: -4,
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.renderOrder = 8;
      mesh.visible = false;
      mesh.userData.spatialPoseVisible = visible;
      const source = new StreamTextureSource(candidate);
      const unsubscribe = source.subscribe(() => {
        const snapshot = source.getSnapshot();
        const entry = projectionEntriesRef.current.get(candidate.id);
        const nextUvRectSignature = mediaContentRectSignature(snapshot.contentRect);
        if (entry && entry.uvRectSignature !== nextUvRectSignature) {
          const nextGeometry = createProjectionGeometry(entry.set, entry.strategyId, entry.meshDensity, {
            clipPolygon: entry.clipPolygon,
            uvRect: snapshot.contentRect ?? null,
          });
          if (nextGeometry) {
            entry.mesh.geometry.dispose();
            entry.mesh.geometry = nextGeometry;
            entry.uvRectSignature = nextUvRectSignature;
          }
        }
        material.map = snapshot.texture;
        material.needsUpdate = true;
        mesh.visible = Boolean(mesh.userData.spatialPoseVisible) && Boolean(snapshot.texture);
        requestRender();
      });
      projectionEntriesRef.current.set(candidate.id, {
        candidateId: candidate.id,
        source,
        unsubscribe,
        mesh,
        material,
        set,
        clipPolygon,
        setId: set.id,
        setSignature,
        areaClipSignature: clipSignature,
        uvRectSignature: mediaContentRectSignature(source.getSnapshot().contentRect),
        strategyId: projectionStrategyId,
        meshDensity,
      });
      group.add(mesh);
      source.start();
    }
    requestRender();
  }, [activePoses, candidates, meshDensity, projectionStrategyId, requestRender]);

  const handleWheel = useCallback((event: React.WheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const camera = cameraRef.current;
    if (!camera) return;
    const factor = Math.exp(-event.deltaY * 0.0018);
    camera.zoom = Math.max(0.35, Math.min(8, camera.zoom * factor));
    camera.updateProjectionMatrix();
    syncViewBounds(cameraWorldBounds(camera));
    requestRender();
  }, [requestRender, syncViewBounds]);

  const handlePointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const camera = cameraRef.current;
    if (!camera || event.button !== 0) return;
    pointerDownRef.current = { x: event.clientX, y: event.clientY, cameraX: camera.position.x, cameraZ: camera.position.z, moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, []);

  const handlePointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const pointer = pointerDownRef.current;
    const camera = cameraRef.current;
    if (!pointer || !camera) return;
    const dx = event.clientX - pointer.x;
    const dy = event.clientY - pointer.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) pointer.moved = true;
    const unitsPerPixel = (camera.top - camera.bottom) / Math.max(1, lastSizeRef.current.height) / camera.zoom;
    camera.position.x = pointer.cameraX - dx * unitsPerPixel;
    camera.position.z = pointer.cameraZ - dy * unitsPerPixel;
    syncViewBounds(cameraWorldBounds(camera));
    requestRender();
  }, [requestRender, syncViewBounds]);

  const handlePointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const pointer = pointerDownRef.current;
    pointerDownRef.current = null;
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // ignore
    }
    if (!pointer || pointer.moved || !onElementActivated) return;
    const renderer = rendererRef.current;
    if (!renderer) return;
    const rect = renderer.domElement.getBoundingClientRect();
    const world = screenToWorld({ x: event.clientX, y: event.clientY }, rect, viewBoundsRef.current);
    const viewport = {
      canvas: renderer.domElement,
      width: rect.width,
      height: rect.height,
      dpr: window.devicePixelRatio || 1,
      worldToScreen: (point: { x: number; z: number }) => worldToScreen(point, rect, viewBoundsRef.current),
      screenToWorld: (point: { x: number; y: number }) => screenToWorld({ x: rect.left + point.x, y: rect.top + point.y }, rect, viewBoundsRef.current),
      scale: rect.width / Math.max(1e-6, viewBoundsRef.current.maxX - viewBoundsRef.current.minX),
    };
    const ordered = [...elements]
      .map((element, index) => ({ element, index }))
      .sort((a, b) => elementLayerRank(b.element, elementTypesById) - elementLayerRank(a.element, elementTypesById) || b.index - a.index);
    for (const { element } of ordered) {
      const def = elementTypesById[element.type];
      try {
        if (def?.hitTest2D?.({ element, world, viewport })) {
          onElementActivated(element.id, "click");
          return;
        }
      } catch {
        // Fall through to the simple proximity hit test.
      }
      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      if (dx * dx + dz * dz <= 0.18 * 0.18) {
        onElementActivated(element.id, "click");
        return;
      }
    }
  }, [elementTypesById, elements, onElementActivated]);

  const markerStatusByElementId = useMemo(() => {
    const out = new Map<string, MarkerVideoStatus>();
    for (const candidate of candidates) {
      const pose = activePoses.get(candidate.id);
      const snapshot = projectionEntriesRef.current.get(candidate.id)?.source.getSnapshot() ?? null;
      const areaClipWarning =
        candidate.areaClipWarning ||
        (candidate.areaClip && pose && !controlPointSetIntersectsAreaClip(pose.set, candidate.areaClip)
          ? "A área de recorte não cruza a pose atual da câmera."
          : null);
      const status = markerVideoStatus(snapshot, pose?.status, areaClipWarning);
      if (status) out.set(candidate.element.id, status);
    }
    return out;
  }, [activePoses, candidates, version]);
  const markers = useMemo(() => markerEntries(elements, elementTypesById), [elementTypesById, elements, version]);
  const markerButtons = useMemo(() => {
    const { width, height } = lastSizeRef.current;
    const rect = { width, height } as DOMRect;
    return markers.map(({ elementId, marker }, index) => {
      const screen = worldToScreen({ x: marker.x, z: marker.z }, rect, viewBounds);
      return { elementId, marker, status: markerStatusByElementId.get(elementId) ?? null, index, x: screen.x, y: screen.y };
    });
  }, [markerStatusByElementId, markers, viewBounds]);

  return (
    <div
      ref={containerRef}
      className="viewportRoot"
      style={{ position: "relative", overflow: "hidden", background: "var(--color-bg, #0f172a)" }}
      onWheel={handleWheel}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={() => {
        pointerDownRef.current = null;
      }}
    >
      <VectorMapLayer
        elements={elements}
        elementTypesById={elementTypesById}
        sceneBounds={sceneBounds}
        viewBounds={viewBounds}
        layer="below-video"
        zIndex={0}
      />
      <VectorMapLayer
        elements={elements}
        elementTypesById={elementTypesById}
        sceneBounds={sceneBounds}
        viewBounds={viewBounds}
        layer="above-video"
        zIndex={2}
      />
      <style>
        {`
          @keyframes spatialVideoMarkerSpin {
            to { transform: rotate(360deg); }
          }
        `}
      </style>
      <div style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 3 }}>
        {notificationPin ? (
          <SpatialNotificationPinView
            screenX={notificationPin.screenX}
            screenY={notificationPin.screenY}
            priority={notificationPin.priority}
            closed={notificationPin.closed}
            trail={notificationPin.trail}
          />
        ) : null}
        {markerButtons.map(({ elementId, marker, status, index, x, y }) => (
          <button
            key={`${elementId}:${marker.id || index}`}
            type="button"
            className={[
              "main2dMarkerButton",
              marker.className ?? "",
              marker.state === "on" ? "isOn" : "",
              marker.state === "off" ? "isOff" : "",
              marker.state === "unknown" ? "isUnknown" : "",
            ].filter(Boolean).join(" ")}
            title={[marker.subtitle ? `${marker.title} · ${marker.subtitle}` : marker.title, status?.title].filter(Boolean).join(" · ")}
            aria-label={marker.title}
            onPointerDown={(event) => event.stopPropagation()}
            onClick={() => onElementActivated?.(elementId, "click")}
            style={{
              position: "absolute",
              left: x,
              top: y,
              width: 32,
              height: 32,
              borderRadius: 999,
              transform: "translate(-50%, -50%)",
              border: "1px solid rgba(226,232,240,0.5)",
              background: "rgba(15,23,42,0.68)",
              color: "rgba(226,232,240,0.92)",
              boxShadow: "0 8px 18px rgba(0,0,0,0.24)",
              pointerEvents: "auto",
              display: "grid",
              placeItems: "center",
              padding: 0,
            }}
          >
            <i
              className={`fa-solid fa-${marker.icon || "circle-dot"}`}
              aria-hidden="true"
              style={{
                fontSize: 14,
                lineHeight: 1,
              }}
            />
            {status ? (
              <span
                aria-hidden="true"
                style={{
                  position: "absolute",
                  right: -4,
                  bottom: -4,
                  width: 17,
                  height: 17,
                  borderRadius: 999,
                  display: "grid",
                  placeItems: "center",
                  border: `1px solid ${status.border}`,
                  background: status.background,
                  color: status.color,
                  boxShadow: "0 6px 12px rgba(0,0,0,0.32)",
                }}
              >
                <i
                  className={`fa-solid fa-${status.icon}`}
                  style={{
                    fontSize: status.kind === "loading" ? 10 : 9,
                    animation: status.kind === "loading" ? "spatialVideoMarkerSpin 0.9s linear infinite" : undefined,
                  }}
                />
              </span>
            ) : null}
          </button>
        ))}
      </div>
      <SpatialVideoCompatibilityNotice
        loading={liveViewsLoading}
        error={liveViewsError}
        hasCompatibleProjection={candidates.length > 0}
      />
    </div>
  );
}
