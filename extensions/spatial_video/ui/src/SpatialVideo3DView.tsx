import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";

import type {
  CompositionElement,
  Element3DInstance,
  ElementType,
  Main2DMarker,
  RenderViewContext,
  ViewSettings,
} from "@toposync/plugin-api";

import { areaClipSignature, controlPointSetIntersectsAreaClip } from "./areaClip";
import { fetchCameraPtzPresets, fetchCameraPtzStatus, fetchLiveViews } from "./api";
import { resolveProjectionCandidates } from "./candidates";
import { markerEntries, markerVideoStatus, type MarkerVideoStatus } from "./markers";
import { controlPointSetProjectionSignature, type ProjectionMeshDensity, type ProjectionStrategyId } from "./projection";
import { createProjectionGeometry } from "./projectionGeometry";
import { resolveActiveProjectionPose } from "./ptzProjection";
import { readSpatialVideoSettings, SPATIAL_VIDEO_RENDER_VIEW_ID } from "./spatialSettings";
import { SpatialVideoCompatibilityNotice } from "./SpatialVideoCompatibilityNotice";
import { StreamTextureSource } from "./streamTexture";
import type { CameraLiveView, PtzPreset, PtzStatus } from "./types";

type TrackedElement = {
  type: string;
  instance: Element3DInstance;
  last: CompositionElement;
};

type ProjectionEntry = {
  candidateId: string;
  source: StreamTextureSource;
  unsubscribe: () => void;
  mesh: THREE.Mesh;
  material: THREE.MeshBasicMaterial;
  setId: string;
  setSignature: string;
  areaClipSignature: string;
  strategyId: ProjectionStrategyId;
  meshDensity: ProjectionMeshDensity;
};

type StatusAdornment = {
  group: THREE.Group;
  ring: THREE.Mesh<THREE.TorusGeometry, THREE.MeshBasicMaterial>;
  glow: THREE.PointLight;
  statusKind: MarkerVideoStatus["kind"];
};

const ELEMENT_ID = "__toposyncSpatialVideoElementId";
const MAX_PIXEL_RATIO = 2;
const AUTO_FIT_GRACE_MS = 3500;
const STATUS_ADORNMENT_ID = "__toposyncSpatialVideoStatusAdornment";

function expandBoundsByObject(target: THREE.Box3, root: THREE.Object3D, includeInvisible = false): boolean {
  let added = false;
  const temp = new THREE.Box3();
  const stack: THREE.Object3D[] = [root];

  while (stack.length > 0) {
    const node = stack.pop();
    if (!node || (!includeInvisible && !node.visible)) continue;
    for (const child of node.children) stack.push(child);

    const anyNode = node as any;
    if (anyNode.isInstancedMesh) {
      const instanced = anyNode as THREE.InstancedMesh;
      try {
        instanced.computeBoundingBox?.();
      } catch {
        // ignore
      }
      const bbox = instanced.boundingBox;
      if (!bbox) continue;
      temp.copy(bbox).applyMatrix4(instanced.matrixWorld);
      if (temp.isEmpty()) continue;
      target.union(temp);
      added = true;
      continue;
    }

    const geometry = anyNode.geometry as THREE.BufferGeometry | undefined;
    if (!geometry) continue;
    if (geometry.boundingBox == null) geometry.computeBoundingBox();
    const bbox = geometry.boundingBox;
    if (!bbox) continue;
    temp.copy(bbox).applyMatrix4(node.matrixWorld);
    if (temp.isEmpty()) continue;
    target.union(temp);
    added = true;
  }

  return added;
}

function computeSpatialBounds(tracked: Map<string, TrackedElement>, projections: Map<string, ProjectionEntry>): THREE.Box3 | null {
  const out = new THREE.Box3();
  out.makeEmpty();
  let hasAny = false;

  for (const entry of tracked.values()) {
    entry.instance.object.updateWorldMatrix(true, true);
    if (expandBoundsByObject(out, entry.instance.object)) hasAny = true;
  }

  for (const entry of projections.values()) {
    entry.mesh.updateWorldMatrix(true, true);
    if (expandBoundsByObject(out, entry.mesh, true)) hasAny = true;
  }

  return hasAny ? out : null;
}

function fitCameraAngledOverview(camera: THREE.PerspectiveCamera, controls: OrbitControls, bounds: THREE.Box3): void {
  const paddedBounds = bounds.clone();
  const size = new THREE.Vector3();
  paddedBounds.getSize(size);

  const padXZ = Math.max(0.35, Math.max(size.x, size.z) * 0.08);
  const padY = Math.max(0.08, size.y * 0.04);
  paddedBounds.expandByVector(new THREE.Vector3(padXZ, padY, padXZ));

  const center = new THREE.Vector3();
  paddedBounds.getCenter(center);
  paddedBounds.getSize(size);

  const targetY = paddedBounds.min.y + Math.min(0.4, size.y * 0.12);
  const target = new THREE.Vector3(center.x, targetY, center.z);
  const polar = 0.68;
  const azimuth = Math.PI * 0.25;
  const direction = new THREE.Vector3().setFromSpherical(new THREE.Spherical(1, polar, azimuth));

  const corners: THREE.Vector3[] = Array.from({ length: 8 }, () => new THREE.Vector3());
  const min = paddedBounds.min;
  const max = paddedBounds.max;
  corners[0].set(min.x, min.y, min.z);
  corners[1].set(min.x, min.y, max.z);
  corners[2].set(min.x, max.y, min.z);
  corners[3].set(min.x, max.y, max.z);
  corners[4].set(max.x, min.y, min.z);
  corners[5].set(max.x, min.y, max.z);
  corners[6].set(max.x, max.y, min.z);
  corners[7].set(max.x, max.y, max.z);

  const projected = new THREE.Vector3();
  const margin = 0.92;
  const vFov = THREE.MathUtils.degToRad(camera.fov);
  const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
  const minFov = Math.max(0.001, Math.min(vFov, hFov));

  let maxRadius = 0;
  for (const corner of corners) maxRadius = Math.max(maxRadius, corner.distanceTo(target));
  let high = maxRadius > 0 ? maxRadius / Math.sin(minFov / 2) : 2;
  if (!Number.isFinite(high) || high <= 0) high = 2;
  high *= 1.15;

  const fits = (distance: number) => {
    camera.position.copy(target).addScaledVector(direction, distance);
    camera.lookAt(target);
    camera.updateMatrixWorld(true);
    for (const corner of corners) {
      projected.copy(corner).project(camera);
      if (!Number.isFinite(projected.x) || !Number.isFinite(projected.y) || !Number.isFinite(projected.z)) return false;
      if (Math.abs(projected.x) > margin || Math.abs(projected.y) > margin) return false;
      if (projected.z < -1 || projected.z > 1) return false;
    }
    return true;
  };

  while (!fits(high) && high < 2000) high *= 1.25;
  let low = 0;
  for (let i = 0; i < 22; i += 1) {
    const mid = (low + high) / 2;
    if (fits(mid)) high = mid;
    else low = mid;
  }

  const distance = high * 1.03;
  controls.target.copy(target);
  camera.position.copy(target).addScaledVector(direction, distance);
  camera.lookAt(target);
  controls.maxDistance = Math.max(controls.maxDistance, distance * 4);
  controls.minDistance = Math.min(controls.minDistance, Math.max(0.15, distance * 0.06));
  controls.update();
}

function findElementId(obj: THREE.Object3D): string | null {
  let cur: THREE.Object3D | null = obj;
  while (cur) {
    const value = (cur.userData as Record<string, unknown> | undefined)?.[ELEMENT_ID];
    if (typeof value === "string") return value;
    cur = cur.parent;
  }
  return null;
}

function elementsEqual(a: CompositionElement, b: CompositionElement): boolean {
  return (
    a.type === b.type &&
    a.name === b.name &&
    a.position.x === b.position.x &&
    a.position.y === b.position.y &&
    a.position.z === b.position.z &&
    a.rotation.x === b.rotation.x &&
    a.rotation.y === b.rotation.y &&
    a.rotation.z === b.rotation.z &&
    JSON.stringify(a.props) === JSON.stringify(b.props)
  );
}

function applyPolygonOffsetUnits(object: THREE.Object3D, units: number): void {
  object.traverse((node) => {
    const matRaw = (node as any).material as unknown;
    if (!matRaw) return;
    const materials = Array.isArray(matRaw) ? matRaw : [matRaw];
    for (const material of materials) {
      if (!material || !(material as any).isMaterial) continue;
      const mat = material as any;
      if (mat.polygonOffset !== true) continue;
      if (typeof mat.polygonOffsetUnits !== "number") continue;
      if (mat.polygonOffsetUnits === units) continue;
      mat.polygonOffsetUnits = units;
    }
  });
}

function createMarkerButton(args: {
  marker: Main2DMarker;
  status: MarkerVideoStatus | null;
  onClick: () => void;
}): HTMLButtonElement {
  const { marker, status, onClick } = args;
  const button = document.createElement("button");
  button.type = "button";
  button.className = [
    "main2dMarkerButton",
    "spatialVideo3dMarker",
    marker.className ?? "",
    marker.state === "on" ? "isOn" : "",
    marker.state === "off" ? "isOff" : "",
    marker.state === "unknown" ? "isUnknown" : "",
  ].filter(Boolean).join(" ");
  button.title = [marker.subtitle ? `${marker.title} · ${marker.subtitle}` : marker.title, status?.title].filter(Boolean).join(" · ");
  button.setAttribute("aria-label", marker.title);
  button.style.width = "32px";
  button.style.height = "32px";
  button.style.borderRadius = "999px";
  button.style.border = "1px solid rgba(226,232,240,0.5)";
  button.style.background = "rgba(15,23,42,0.72)";
  button.style.color = "rgba(226,232,240,0.92)";
  button.style.boxShadow = "0 8px 18px rgba(0,0,0,0.28)";
  button.style.pointerEvents = "auto";
  button.style.display = "grid";
  button.style.placeItems = "center";
  button.style.padding = "0";
  button.style.position = "relative";

  const icon = document.createElement("i");
  icon.className = `fa-solid fa-${marker.icon || "circle-dot"}`;
  icon.setAttribute("aria-hidden", "true");
  icon.style.fontSize = "14px";
  icon.style.lineHeight = "1";
  button.appendChild(icon);

  if (status) {
    const badge = document.createElement("span");
    badge.setAttribute("aria-hidden", "true");
    badge.style.position = "absolute";
    badge.style.right = "-4px";
    badge.style.bottom = "-4px";
    badge.style.width = "17px";
    badge.style.height = "17px";
    badge.style.borderRadius = "999px";
    badge.style.display = "grid";
    badge.style.placeItems = "center";
    badge.style.border = `1px solid ${status.border}`;
    badge.style.background = status.background;
    badge.style.color = status.color;
    badge.style.boxShadow = "0 6px 12px rgba(0,0,0,0.32)";

    const badgeIcon = document.createElement("i");
    badgeIcon.className = `fa-solid fa-${status.icon}`;
    badgeIcon.style.fontSize = status.kind === "loading" ? "10px" : "9px";
    if (status.kind === "loading") badgeIcon.style.animation = "spatialVideoMarkerSpin 0.9s linear infinite";
    badge.appendChild(badgeIcon);
    button.appendChild(badge);
  }

  button.addEventListener("pointerdown", (event) => event.stopPropagation());
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    onClick();
  });
  return button;
}

function statusColor(status: MarkerVideoStatus): number {
  if (status.kind === "error") return 0xef4444;
  if (status.kind === "pose_warning" || status.kind === "unmatched") return 0xf59e0b;
  if (status.kind === "pose_notice") return 0x22d3ee;
  return 0x38bdf8;
}

function disposeStatusAdornment(adornment: StatusAdornment): void {
  adornment.group.parent?.remove(adornment.group);
  adornment.ring.geometry.dispose();
  adornment.ring.material.dispose();
  adornment.glow.dispose();
}

function viewForSpatial3D(viewSettings: ViewSettings): ViewSettings {
  return { ...viewSettings, ghostWalls: false };
}

export function SpatialVideo3DView({
  elements,
  elementTypesById,
  compositionId,
  viewSettings,
  onElementActivated,
}: RenderViewContext): React.ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const projectionGroupRef = useRef<THREE.Group | null>(null);
  const markerGroupRef = useRef<THREE.Group | null>(null);
  const trackedRef = useRef<Map<string, TrackedElement>>(new Map());
  const projectionEntriesRef = useRef<Map<string, ProjectionEntry>>(new Map());
  const markerObjectsRef = useRef<CSS2DObject[]>([]);
  const statusAdornmentsRef = useRef<Map<string, StatusAdornment>>(new Map());
  const renderViewSettingsRef = useRef<ViewSettings>(viewForSpatial3D(viewSettings));
  const lastElementsContextRef = useRef<CompositionElement[] | null>(null);
  const userInteractedWithCameraRef = useRef(false);
  const autoFitUntilRef = useRef(Date.now() + AUTO_FIT_GRACE_MS);
  const compositionIdRef = useRef(compositionId);
  const onElementActivatedRef = useRef(onElementActivated);
  const elementTypesByIdRef = useRef(elementTypesById);
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
  const renderViewKey = `${viewSettings.wallHeightPreset}:${viewSettings.wallHeight}:${viewSettings.graphicsQuality ?? "simplified"}`;

  const bumpVersion = useCallback(() => setVersion((prev) => prev + 1), []);

  useEffect(() => {
    const next = viewForSpatial3D(viewSettings);
    renderViewSettingsRef.current.wallHeightPreset = next.wallHeightPreset;
    renderViewSettingsRef.current.wallHeight = next.wallHeight;
    renderViewSettingsRef.current.ghostWalls = next.ghostWalls;
    renderViewSettingsRef.current.graphicsQuality = next.graphicsQuality;
  }, [viewSettings.ghostWalls, viewSettings.graphicsQuality, viewSettings.wallHeight, viewSettings.wallHeightPreset]);

  const fitContentIfAllowed = useCallback((force = false) => {
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls) return;
    if (!force && (userInteractedWithCameraRef.current || Date.now() > autoFitUntilRef.current)) return;
    const bounds = computeSpatialBounds(trackedRef.current, projectionEntriesRef.current);
    if (!bounds) return;
    fitCameraAngledOverview(camera, controls, bounds);
  }, []);

  useEffect(() => {
    compositionIdRef.current = compositionId;
    userInteractedWithCameraRef.current = false;
    autoFitUntilRef.current = Date.now() + AUTO_FIT_GRACE_MS;
  }, [compositionId]);

  useEffect(() => {
    onElementActivatedRef.current = onElementActivated;
  }, [onElementActivated]);

  useEffect(() => {
    elementTypesByIdRef.current = elementTypesById;
  }, [elementTypesById]);

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

  const candidates = useMemo(() => resolveProjectionCandidates(elements, elementTypesById, liveViews), [elementTypesById, elements, liveViews]);

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

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, stencil: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
    renderer.setClearColor(0x000000, 0);
    renderer.domElement.style.display = "block";
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    renderer.domElement.style.touchAction = "none";
    container.appendChild(renderer.domElement);

    const labelRenderer = new CSS2DRenderer();
    labelRenderer.domElement.style.position = "absolute";
    labelRenderer.domElement.style.top = "0";
    labelRenderer.domElement.style.left = "0";
    labelRenderer.domElement.style.pointerEvents = "none";
    container.appendChild(labelRenderer.domElement);

    const scene = new THREE.Scene();
    const projectionGroup = new THREE.Group();
    const markerGroup = new THREE.Group();
    scene.add(projectionGroup);
    scene.add(markerGroup);

    const camera = new THREE.PerspectiveCamera(65, 1, 0.1, 250);
    camera.position.set(0, 3.2, 5.5);
    scene.add(new THREE.AmbientLight(0xffffff, 0.58));
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.84);
    dirLight.position.set(2.5, 6, 3.5);
    scene.add(dirLight);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.7;
    controls.zoomSpeed = 0.9;
    controls.panSpeed = 0.8;
    controls.target.set(0, 0.2, 0);
    controls.minDistance = 0.3;
    controls.maxDistance = 60;
    controls.maxPolarAngle = Math.PI / 2 - 0.02;
    controls.update();

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;
    controlsRef.current = controls;
    projectionGroupRef.current = projectionGroup;
    markerGroupRef.current = markerGroup;

    const handleControlsStart = () => {
      userInteractedWithCameraRef.current = true;
      autoFitUntilRef.current = 0;
    };
    controls.addEventListener("start", handleControlsStart);

    const resize = () => {
      const width = Math.max(1, container.clientWidth);
      const height = Math.max(1, container.clientHeight);
      renderer.setSize(width, height, false);
      labelRenderer.setSize(width, height);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      fitContentIfAllowed();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();

    const raycaster = new THREE.Raycaster();
    const mouse = new THREE.Vector2();
    let pointerDown: { x: number; y: number; id: string | null } | null = null;

    const pickElementId = (clientX: number, clientY: number): string | null => {
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      mouse.y = -(((clientY - rect.top) / Math.max(1, rect.height)) * 2 - 1);
      raycaster.setFromCamera(mouse, camera);
      const hits = raycaster.intersectObjects(scene.children, true);
      for (const hit of hits) {
        const id = findElementId(hit.object);
        if (id) return id;
      }
      return null;
    };

    const handlePointerDown = (event: PointerEvent) => {
      pointerDown = {
        x: event.clientX,
        y: event.clientY,
        id: onElementActivatedRef.current ? pickElementId(event.clientX, event.clientY) : null,
      };
    };
    const handlePointerUp = (event: PointerEvent) => {
      const down = pointerDown;
      pointerDown = null;
      if (!down || !down.id) return;
      const dx = event.clientX - down.x;
      const dy = event.clientY - down.y;
      if (dx * dx + dy * dy > 36) return;
      const id = pickElementId(event.clientX, event.clientY);
      if (!id || id !== down.id) return;
      onElementActivatedRef.current?.(id, "click");
    };
    const handleContextMenu = (event: MouseEvent) => event.preventDefault();
    renderer.domElement.addEventListener("pointerdown", handlePointerDown);
    renderer.domElement.addEventListener("pointerup", handlePointerUp);
    renderer.domElement.addEventListener("pointercancel", handlePointerUp);
    renderer.domElement.addEventListener("contextmenu", handleContextMenu);

    let raf = 0;
    let lastFrameTs = 0;
    const frame = (ts: number) => {
      raf = requestAnimationFrame(frame);
      if (document.hidden) {
        lastFrameTs = 0;
        return;
      }
      const dt = lastFrameTs ? Math.min((ts - lastFrameTs) / 1000, 0.05) : 0;
      lastFrameTs = ts;
      const pulse = 0.65 + 0.35 * Math.sin(ts * 0.006);
      for (const entry of trackedRef.current.values()) {
        try {
          entry.instance.tick?.(dt);
        } catch (error) {
          console.warn(`[spatial-video-3d:element:${entry.type}.tick]`, error);
        }
      }
      for (const adornment of statusAdornmentsRef.current.values()) {
        if (adornment.statusKind === "loading") {
          const scale = 0.86 + pulse * 0.22;
          adornment.group.scale.setScalar(scale);
          adornment.ring.material.opacity = 0.48 + pulse * 0.32;
          adornment.glow.intensity = 0.25 + pulse * 0.45;
        } else {
          adornment.group.scale.setScalar(1);
          adornment.ring.material.opacity = adornment.statusKind === "error" ? 0.86 : 0.74;
          adornment.glow.intensity = adornment.statusKind === "error" ? 0.58 : 0.38;
        }
      }
      for (const entry of projectionEntriesRef.current.values()) {
        const map = entry.material.map;
        if (map) map.needsUpdate = true;
      }
      controls.update();
      renderer.render(scene, camera);
      labelRenderer.render(scene, camera);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      controls.removeEventListener("start", handleControlsStart);
      controls.dispose();
      renderer.domElement.removeEventListener("pointerdown", handlePointerDown);
      renderer.domElement.removeEventListener("pointerup", handlePointerUp);
      renderer.domElement.removeEventListener("pointercancel", handlePointerUp);
      renderer.domElement.removeEventListener("contextmenu", handleContextMenu);

      for (const label of markerObjectsRef.current) {
        markerGroup.remove(label);
        label.element.remove();
      }
      markerObjectsRef.current = [];

      for (const adornment of statusAdornmentsRef.current.values()) disposeStatusAdornment(adornment);
      statusAdornmentsRef.current.clear();

      for (const entry of projectionEntriesRef.current.values()) {
        entry.unsubscribe();
        entry.source.destroy();
        entry.mesh.geometry.dispose();
        entry.material.dispose();
      }
      projectionEntriesRef.current.clear();

      for (const tracked of trackedRef.current.values()) {
        scene.remove(tracked.instance.object);
        tracked.instance.dispose?.();
      }
      trackedRef.current.clear();

      renderer.dispose();
      container.removeChild(renderer.domElement);
      container.removeChild(labelRenderer.domElement);
      rendererRef.current = null;
      sceneRef.current = null;
      cameraRef.current = null;
      controlsRef.current = null;
      projectionGroupRef.current = null;
      markerGroupRef.current = null;
    };
  }, [fitContentIfAllowed]);

  useEffect(() => {
    const scene = sceneRef.current;
    const camera = cameraRef.current;
    const renderer = rendererRef.current;
    if (!scene || !camera || !renderer) return;

    const tracked = trackedRef.current;
    const elementsById = new Map(elements.map((element) => [element.id, element]));
    const elementsContextChanged = lastElementsContextRef.current !== elements;
    lastElementsContextRef.current = elements;
    const areaElements = elements.filter((element) => (elementTypesById[element.type]?.layerGroup ?? "") === "areas");
    const areaOrderById = new Map<string, number>();
    for (let i = 0; i < areaElements.length; i += 1) areaOrderById.set(areaElements[i].id, i);

    for (const [id, entry] of tracked.entries()) {
      const element = elementsById.get(id);
      const def = element ? elementTypesById[element.type] : null;
      if (!element || !def?.create3D) {
        scene.remove(entry.instance.object);
        entry.instance.dispose?.();
        tracked.delete(id);
        const adornment = statusAdornmentsRef.current.get(id);
        if (adornment) {
          disposeStatusAdornment(adornment);
          statusAdornmentsRef.current.delete(id);
        }
      }
    }

    for (const element of elements) {
      const def = elementTypesById[element.type];
      if (!def?.create3D) continue;

      const existing = tracked.get(element.id);
      if (!existing || existing.type !== element.type) {
        if (existing) {
          scene.remove(existing.instance.object);
          existing.instance.dispose?.();
          tracked.delete(element.id);
        }

        try {
          const instance = def.create3D(
            {
              THREE,
              scene,
              camera,
              renderer,
              view: renderViewSettingsRef.current,
              elements,
              compositionId: compositionIdRef.current,
              requestRender: () => undefined,
            },
            element,
          );
          (instance.object.userData as Record<string, unknown>)[ELEMENT_ID] = element.id;
          instance.object.position.set(element.position.x, element.position.y, element.position.z);
          instance.object.rotation.set(element.rotation.x, element.rotation.y, element.rotation.z);
          scene.add(instance.object);
          tracked.set(element.id, { type: element.type, instance, last: element });
        } catch (error) {
          console.warn(`[spatial-video-3d:create3D:${element.type}]`, error);
        }
      }

      const entry = tracked.get(element.id);
      if (!entry) continue;
      if (
        entry.instance.object.position.x !== element.position.x ||
        entry.instance.object.position.y !== element.position.y ||
        entry.instance.object.position.z !== element.position.z
      ) {
        entry.instance.object.position.set(element.position.x, element.position.y, element.position.z);
      }
      if (
        entry.instance.object.rotation.x !== element.rotation.x ||
        entry.instance.object.rotation.y !== element.rotation.y ||
        entry.instance.object.rotation.z !== element.rotation.z
      ) {
        entry.instance.object.rotation.set(element.rotation.x, element.rotation.y, element.rotation.z);
      }
      if (
        elementsContextChanged ||
        !elementsEqual(entry.last, element) ||
        (entry.instance.object.userData as Record<string, unknown>).__spatialViewKey !== renderViewKey
      ) {
        entry.instance.update?.(element, { elements });
        entry.last = element;
        (entry.instance.object.userData as Record<string, unknown>).__spatialViewKey = renderViewKey;
      }
      if (def.layerGroup === "areas") {
        const order = areaOrderById.get(element.id);
        if (typeof order === "number") {
          const stackIndex = areaElements.length - 1 - order;
          applyPolygonOffsetUnits(entry.instance.object, 1 + stackIndex);
        }
      }
    }

    fitContentIfAllowed();
  }, [
    elements,
    elementTypesById,
    renderViewKey,
    fitContentIfAllowed,
  ]);

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
      const visible = pose?.status !== "unmatched" && controlPointSetIntersectsAreaClip(set, candidate.areaClip);
      const existing = projectionEntriesRef.current.get(candidate.id);
      if (
        existing &&
        (existing.setId !== set.id ||
          existing.setSignature !== setSignature ||
          existing.areaClipSignature !== clipSignature ||
          existing.strategyId !== projectionStrategyId ||
          existing.meshDensity !== meshDensity)
      ) {
        const geometry = createProjectionGeometry(set, projectionStrategyId, meshDensity, { clipPolygon: candidate.areaClip?.polygon ?? null });
        if (geometry) {
          existing.mesh.geometry.dispose();
          existing.mesh.geometry = geometry;
          existing.setId = set.id;
          existing.setSignature = setSignature;
          existing.areaClipSignature = clipSignature;
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
        existing.mesh.userData.spatialPoseVisible = visible;
        existing.mesh.visible = visible && Boolean(existing.material.map);
        existing.material.opacity = pose?.moving ? 0.58 : 0.78;
        continue;
      }

      const geometry = createProjectionGeometry(set, projectionStrategyId, meshDensity, { clipPolygon: candidate.areaClip?.polygon ?? null });
      if (!geometry) continue;
      const material = new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: pose?.moving ? 0.58 : 0.78,
        depthTest: true,
        depthWrite: false,
        side: THREE.DoubleSide,
        polygonOffset: true,
        polygonOffsetFactor: -1,
        polygonOffsetUnits: -6,
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.renderOrder = 8;
      mesh.visible = false;
      mesh.userData.spatialPoseVisible = visible;

      const source = new StreamTextureSource(candidate);
      const unsubscribe = source.subscribe(() => {
        const snapshot = source.getSnapshot();
        material.map = snapshot.texture;
        material.needsUpdate = true;
        mesh.visible = Boolean(mesh.userData.spatialPoseVisible) && Boolean(snapshot.texture);
        bumpVersion();
      });
      projectionEntriesRef.current.set(candidate.id, {
        candidateId: candidate.id,
        source,
        unsubscribe,
        mesh,
        material,
        setId: set.id,
        setSignature,
        areaClipSignature: clipSignature,
        strategyId: projectionStrategyId,
        meshDensity,
      });
      group.add(mesh);
      source.start();
    }
    fitContentIfAllowed();
    bumpVersion();
  }, [activePoses, bumpVersion, candidates, fitContentIfAllowed, meshDensity, projectionStrategyId]);

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

  useEffect(() => {
    const group = markerGroupRef.current;
    if (!group) return;
    for (const label of markerObjectsRef.current) {
      group.remove(label);
      label.element.remove();
    }
    markerObjectsRef.current = [];

    const elementsById = new Map(elements.map((element) => [element.id, element]));
    for (const { elementId, marker } of markers) {
      const element = elementsById.get(elementId);
      if (element && elementTypesById[element.type]?.create3D) continue;
      const status = markerStatusByElementId.get(elementId) ?? null;
      const node = createMarkerButton({
        marker,
        status,
        onClick: () => onElementActivatedRef.current?.(elementId, "click"),
      });
      const label = new CSS2DObject(node);
      label.position.set(marker.x, (element?.position.y ?? 0) + 0.48, marker.z);
      markerObjectsRef.current.push(label);
      group.add(label);
    }

    return () => {
      for (const label of markerObjectsRef.current) {
        group.remove(label);
        label.element.remove();
      }
      markerObjectsRef.current = [];
    };
  }, [elementTypesById, elements, markerStatusByElementId, markers]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;

    const expectedIds = new Set<string>();
    for (const [elementId, status] of markerStatusByElementId.entries()) {
      const tracked = trackedRef.current.get(elementId);
      if (!tracked) continue;
      expectedIds.add(elementId);

      const color = statusColor(status);
      let adornment = statusAdornmentsRef.current.get(elementId);
      if (!adornment) {
        const ringGeometry = new THREE.TorusGeometry(0.22, 0.012, 12, 48);
        const ringMaterial = new THREE.MeshBasicMaterial({
          color,
          transparent: true,
          opacity: status.kind === "loading" ? 0.64 : 0.82,
          depthTest: true,
          depthWrite: false,
          toneMapped: false,
        });
        const ring = new THREE.Mesh(ringGeometry, ringMaterial);
        ring.rotation.x = -Math.PI / 2;
        ring.renderOrder = 30;

        const glow = new THREE.PointLight(color, status.kind === "loading" ? 0.42 : 0.5, 1.4, 2);
        glow.position.set(0, 0.08, 0);

        const statusGroup = new THREE.Group();
        statusGroup.userData[STATUS_ADORNMENT_ID] = true;
        statusGroup.userData[ELEMENT_ID] = elementId;
        statusGroup.add(ring);
        statusGroup.add(glow);
        scene.add(statusGroup);

        adornment = { group: statusGroup, ring, glow, statusKind: status.kind };
        statusAdornmentsRef.current.set(elementId, adornment);
      } else if (adornment.statusKind !== status.kind) {
        adornment.statusKind = status.kind;
        adornment.ring.material.color.setHex(color);
        adornment.glow.color.setHex(color);
      }

      const bounds = new THREE.Box3();
      bounds.makeEmpty();
      tracked.instance.object.updateWorldMatrix(true, true);
      if (expandBoundsByObject(bounds, tracked.instance.object, true)) {
        const center = new THREE.Vector3();
        bounds.getCenter(center);
        adornment.group.position.set(center.x, bounds.max.y + 0.08, center.z);
      } else {
        adornment.group.position.set(tracked.last.position.x, tracked.last.position.y + 0.5, tracked.last.position.z);
      }
    }

    for (const [elementId, adornment] of statusAdornmentsRef.current.entries()) {
      if (expectedIds.has(elementId)) continue;
      disposeStatusAdornment(adornment);
      statusAdornmentsRef.current.delete(elementId);
    }
  }, [elements, markerStatusByElementId, version]);

  return (
    <div
      ref={containerRef}
      className="viewportRoot"
      style={{ position: "relative", overflow: "hidden", background: "var(--color-bg, #0f172a)" }}
    >
      <style>
        {`
          @keyframes spatialVideoMarkerSpin {
            to { transform: rotate(360deg); }
          }
        `}
      </style>
      <SpatialVideoCompatibilityNotice
        loading={liveViewsLoading}
        error={liveViewsError}
        hasCompatibleProjection={candidates.length > 0}
      />
    </div>
  );
}
