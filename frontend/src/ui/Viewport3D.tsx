import React, { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";

import type {
  CompositionElement,
  Element3DInstance,
  ElementType,
  Notification,
  Notification3DOverlay,
  NotificationOverlayActions,
  NotificationRenderer,
  ViewSettings,
} from "@toposync/plugin-api";

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  onElementActivated?: (elementId: string, intent?: "click" | "dblclick" | "longpress") => void;
  viewSettings: ViewSettings;
  compositionId?: string;
  activeNotification?: Notification | null;
  activeNotificationRenderer?: NotificationRenderer | null;
  onOpenImage?: (args: { url: string; title?: string; subtitle?: string }) => void;
};

type Tracked = {
  type: string;
  instance: Element3DInstance;
  last: CompositionElement;
};

const ELEMENT_ID = "__toposyncElementId";
const FULL_WALL_HEIGHT = 2.7;
const FOCUS_HIGHLIGHT_COLOR = 0xfbbf24;
const GHOST_WALLS_OPACITY = 0.22;
const GHOST_WALLS_MATERIAL_STATE_KEY = "__toposyncGhostWallsOriginal";
const AUTO_FIT_GRACE_MS = 3500;
const AUTO_FIT_THROTTLE_MS = 220;

function expandBoundsByVisibleObject(target: THREE.Box3, root: THREE.Object3D): boolean {
  let added = false;
  const temp = new THREE.Box3();

  const stack: THREE.Object3D[] = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node || !node.visible) continue;

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
      temp.copy(bbox);
      temp.applyMatrix4(instanced.matrixWorld);
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

    temp.copy(bbox);
    temp.applyMatrix4(node.matrixWorld);
    if (temp.isEmpty()) continue;
    target.union(temp);
    added = true;
  }

  return added;
}

function computeTrackedBounds(tracked: Map<string, Tracked>): THREE.Box3 | null {
  const out = new THREE.Box3();
  out.makeEmpty();
  let hasAny = false;
  for (const entry of tracked.values()) {
    entry.instance.object.updateWorldMatrix(true, true);
    if (expandBoundsByVisibleObject(out, entry.instance.object)) hasAny = true;
  }
  return hasAny ? out : null;
}

function fitCameraTopDown(camera: THREE.PerspectiveCamera, controls: OrbitControls, bounds: THREE.Box3): void {
  const size = new THREE.Vector3();
  bounds.getSize(size);

  const center = new THREE.Vector3();
  bounds.getCenter(center);

  const target = new THREE.Vector3(center.x, 0, center.z);

  const halfWidth = Math.max(0.5, size.x / 2);
  const halfHeight = Math.max(0.5, size.z / 2);

  const vFov = THREE.MathUtils.degToRad(camera.fov);
  const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
  const distance = Math.max(halfHeight / Math.tan(vFov / 2), halfWidth / Math.tan(hFov / 2));

  const padding = 1.25;
  const height = distance * padding;
  const depthEpsilon = Math.max(0.01, height * 0.001);

  controls.target.copy(target);
  const cameraY = Math.max(target.y, bounds.max.y) + height;
  camera.position.set(target.x, cameraY, target.z + depthEpsilon);

  controls.maxDistance = Math.max(controls.maxDistance, height * 4);
  controls.minDistance = Math.min(controls.minDistance, Math.max(0.5, height * 0.05));
  controls.update();
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

  // Keep the orbit target close to the ground, but fit using the real camera frustum
  // so tall walls/models don't get clipped or appear shifted.
  const targetY = paddedBounds.min.y + Math.min(0.4, size.y * 0.12);
  const target = new THREE.Vector3(center.x, targetY, center.z);

  // Bird's-eye / 3-quarters view (from a corner) to give depth while keeping the full plan visible.
  const polar = 0.68; // angle from +Y axis (0 = top-down, PI/2 = horizon)
  const azimuth = Math.PI * 0.25; // 45° corner view

  const direction = new THREE.Vector3();
  direction.setFromSpherical(new THREE.Spherical(1, polar, azimuth));

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
  let high = maxRadius > 0 ? maxRadius / Math.sin(minFov / 2) : 2.0;
  if (!Number.isFinite(high) || high <= 0) high = 2.0;
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

  if (!fits(high)) {
    while (!fits(high) && high < 2000) high *= 1.25;
  }

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

function applyGhostWalls(object: THREE.Object3D, enabled: boolean): void {
  object.traverse((node) => {
    const matRaw = (node as any).material as unknown;
    if (!matRaw) return;

    const mats = Array.isArray(matRaw) ? matRaw : [matRaw];
    for (const m of mats) {
      if (!m || !(m as any).isMaterial) continue;
      const mat = m as THREE.Material;
      const userData = (mat.userData ??= {});

      if (enabled) {
        if (!(GHOST_WALLS_MATERIAL_STATE_KEY in userData)) {
          (userData as any)[GHOST_WALLS_MATERIAL_STATE_KEY] = {
            opacity: (mat as any).opacity,
            transparent: mat.transparent,
            depthWrite: (mat as any).depthWrite,
          };
        }

        if (typeof (mat as any).opacity === "number") (mat as any).opacity = GHOST_WALLS_OPACITY;
        mat.transparent = true;
        if (typeof (mat as any).depthWrite === "boolean") (mat as any).depthWrite = false;
        mat.needsUpdate = true;
        continue;
      }

      const original = (userData as any)[GHOST_WALLS_MATERIAL_STATE_KEY];
      if (original && typeof original === "object") {
        if (typeof original.opacity === "number" && typeof (mat as any).opacity === "number") (mat as any).opacity = original.opacity;
        if (typeof original.transparent === "boolean") mat.transparent = original.transparent;
        if (typeof original.depthWrite === "boolean" && typeof (mat as any).depthWrite === "boolean")
          (mat as any).depthWrite = original.depthWrite;
        delete (userData as any)[GHOST_WALLS_MATERIAL_STATE_KEY];
        mat.needsUpdate = true;
      }
    }
  });
}

function applyPolygonOffsetUnits(object: THREE.Object3D, units: number): void {
  object.traverse((node) => {
    const matRaw = (node as any).material as unknown;
    if (!matRaw) return;

    const mats = Array.isArray(matRaw) ? matRaw : [matRaw];
    for (const m of mats) {
      if (!m || !(m as any).isMaterial) continue;
      const matAny = m as any;
      if (matAny.polygonOffset !== true) continue;
      if (typeof matAny.polygonOffsetUnits !== "number") continue;
      if (matAny.polygonOffsetUnits === units) continue;
      matAny.polygonOffsetUnits = units;
    }
  });
}

function findElementId(obj: THREE.Object3D): string | null {
  let cur: THREE.Object3D | null = obj;
  while (cur) {
    const value = (cur.userData as any)?.[ELEMENT_ID];
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

export function Viewport3D({
  elements,
  elementTypesById,
  onElementActivated,
  viewSettings,
  compositionId,
  activeNotification,
  activeNotificationRenderer,
  onOpenImage,
}: Props): React.ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const trackedRef = useRef<Map<string, Tracked>>(new Map());
  const notificationOverlayRef = useRef<Notification3DOverlay | null>(null);
  const notificationOverlayNotificationIdRef = useRef<string | null>(null);
  const notificationOverlayRendererIdRef = useRef<string | null>(null);
  const notificationOverlayCompositionIdRef = useRef<string | null>(null);
  const viewRef = useRef<ViewSettings>({
    wallHeightPreset: "high",
    wallHeight: FULL_WALL_HEIGHT,
    ghostWalls: false,
    graphicsQuality: "simplified",
  });
  const elementViewRef = useRef<ViewSettings>({
    wallHeightPreset: "high",
    wallHeight: FULL_WALL_HEIGHT,
    ghostWalls: false,
    graphicsQuality: "simplified",
  });
  const viewKeyRef = useRef<string>("");
  const onElementActivatedRef = useRef<Props["onElementActivated"]>(onElementActivated);
  const elementTypesByIdRef = useRef<Record<string, ElementType>>(elementTypesById);
  const activeNotificationRef = useRef<Notification | null>(activeNotification ?? null);
  const activeNotificationRendererRef = useRef<NotificationRenderer | null>(activeNotificationRenderer ?? null);
  const onOpenImageRef = useRef<Props["onOpenImage"]>(onOpenImage);
  const compositionIdRef = useRef<string | undefined>(compositionId);
  const lastAutoFitCompositionIdRef = useRef<string | null>(null);
  const userInteractedWithCameraRef = useRef(false);
  const autoFitUntilRef = useRef<number>(0);
  const autoFitLastBoundsRef = useRef<THREE.Box3 | null>(null);
  const autoFitLastCheckTsRef = useRef<number>(0);

  const [focusedElementId, setFocusedElementId] = useState<string | null>(null);
  const focusHelperRef = useRef<THREE.BoxHelper | null>(null);

  const raycaster = useMemo(() => new THREE.Raycaster(), []);
  const mouse = useMemo(() => new THREE.Vector2(), []);

  useEffect(() => {
    viewRef.current.wallHeightPreset = viewSettings.wallHeightPreset;
    viewRef.current.wallHeight = viewSettings.wallHeight;
    viewRef.current.ghostWalls = Boolean(viewSettings.ghostWalls);
    viewRef.current.graphicsQuality = viewSettings.graphicsQuality ?? "simplified";
    elementViewRef.current.wallHeightPreset = viewSettings.wallHeightPreset;
    elementViewRef.current.wallHeight =
      viewSettings.wallHeightPreset === "low" ? FULL_WALL_HEIGHT : viewSettings.wallHeight;
    elementViewRef.current.ghostWalls = Boolean(viewSettings.ghostWalls);
    elementViewRef.current.graphicsQuality = viewSettings.graphicsQuality ?? "simplified";
  }, [viewSettings.ghostWalls, viewSettings.graphicsQuality, viewSettings.wallHeight, viewSettings.wallHeightPreset]);

  useEffect(() => {
    onElementActivatedRef.current = onElementActivated;
  }, [onElementActivated]);

  useEffect(() => {
    elementTypesByIdRef.current = elementTypesById;
  }, [elementTypesById]);

  function syncNotificationOverlay(): void {
    const overlay = notificationOverlayRef.current;
    const scene = sceneRef.current;
    const renderer = rendererRef.current;
    const camera = cameraRef.current;
    if (!scene || !renderer || !camera) return;

    const notification = activeNotificationRef.current;
    const rendererDef = activeNotificationRendererRef.current;
    const create3DOverlay = rendererDef?.create3DOverlay;
    const currentCompositionId = compositionIdRef.current ?? null;

    if (!notification || !rendererDef || !create3DOverlay) {
      if (overlay) {
        scene.remove(overlay.object);
        overlay.dispose?.();
        notificationOverlayRef.current = null;
      }
      notificationOverlayNotificationIdRef.current = null;
      notificationOverlayRendererIdRef.current = null;
      notificationOverlayCompositionIdRef.current = null;
      return;
    }

    const needsRecreate =
      !overlay ||
      notificationOverlayNotificationIdRef.current !== notification.id ||
      notificationOverlayRendererIdRef.current !== rendererDef.id ||
      notificationOverlayCompositionIdRef.current !== currentCompositionId;

    if (!needsRecreate) {
      overlay.update?.(notification);
      return;
    }

    if (overlay) {
      scene.remove(overlay.object);
      overlay.dispose?.();
      notificationOverlayRef.current = null;
    }

    const actions: NotificationOverlayActions = {
      openImage: (args) => onOpenImageRef.current?.(args),
    };

    try {
      const created = create3DOverlay(
        { THREE, scene, camera, renderer, view: viewRef.current, compositionId: currentCompositionId ?? undefined },
        notification,
        actions,
      );
      if (!created) {
        notificationOverlayNotificationIdRef.current = null;
        notificationOverlayRendererIdRef.current = null;
        notificationOverlayCompositionIdRef.current = null;
        return;
      }
      scene.add(created.object);
      notificationOverlayRef.current = created;
      notificationOverlayNotificationIdRef.current = notification.id;
      notificationOverlayRendererIdRef.current = rendererDef.id;
      notificationOverlayCompositionIdRef.current = currentCompositionId;
    } catch (err) {
      console.warn("[notificationOverlay]", err);
      notificationOverlayNotificationIdRef.current = null;
      notificationOverlayRendererIdRef.current = null;
      notificationOverlayCompositionIdRef.current = null;
    }
  }

  useEffect(() => {
    activeNotificationRef.current = activeNotification ?? null;
    syncNotificationOverlay();
  }, [activeNotification]);

  useEffect(() => {
    activeNotificationRendererRef.current = activeNotificationRenderer ?? null;
    syncNotificationOverlay();
  }, [activeNotificationRenderer]);

  useEffect(() => {
    onOpenImageRef.current = onOpenImage;
  }, [onOpenImage]);

  useEffect(() => {
    compositionIdRef.current = compositionId;
    if (compositionId !== lastAutoFitCompositionIdRef.current) {
      lastAutoFitCompositionIdRef.current = null;
      userInteractedWithCameraRef.current = false;
      autoFitUntilRef.current = Date.now() + AUTO_FIT_GRACE_MS;
      autoFitLastBoundsRef.current = null;
      autoFitLastCheckTsRef.current = 0;
    }
    syncNotificationOverlay();
  }, [compositionId]);

  const focusables = useMemo(() => {
    const out: Array<{ id: string; x: number; z: number }> = [];
    for (const el of elements) {
      const def = elementTypesById[el.type];
      if (!def?.create3D) continue;
      if (def.layerGroup === "walls") continue;
      const interactive = Boolean(def.primaryAction || def.renderActionModal);
      if (!interactive) continue;
      out.push({ id: el.id, x: el.position.x, z: el.position.z });
    }
    out.sort((a, b) => a.z - b.z || a.x - b.x || a.id.localeCompare(b.id));
    return out;
  }, [elements, elementTypesById]);

  useEffect(() => {
    if (!focusedElementId) return;
    if (!focusables.some((f) => f.id === focusedElementId)) setFocusedElementId(null);
  }, [focusables, focusedElementId]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const containerEl: HTMLDivElement = container;

    const renderer = new THREE.WebGLRenderer({ antialias: true, stencil: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setClearColor(0x070a14, 1);
    renderer.shadowMap.enabled = false;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    renderer.domElement.style.display = "block";
    renderer.domElement.style.touchAction = "none";
    containerEl.appendChild(renderer.domElement);

    const labelRenderer = new CSS2DRenderer();
    labelRenderer.domElement.style.position = "absolute";
    labelRenderer.domElement.style.top = "0";
    labelRenderer.domElement.style.left = "0";
    labelRenderer.domElement.style.pointerEvents = "none";
    containerEl.appendChild(labelRenderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(65, 1, 0.1, 200);
    camera.position.set(0, 1.6, 4.2);

    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.85);
    dirLight.position.set(2.2, 6, 3);
    scene.add(dirLight);

	    const controls = new OrbitControls(camera, renderer.domElement);
	    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.7;
    controls.zoomSpeed = 0.9;
    controls.panSpeed = 0.8;
    controls.target.set(0, 0.2, 0);
    controls.minDistance = 0.3;
    controls.maxDistance = 40;
	    controls.maxPolarAngle = Math.PI / 2 - 0.02;
	    controls.update();
	    controlsRef.current = controls;

	    const handleControlsStart = () => {
	      userInteractedWithCameraRef.current = true;
      autoFitUntilRef.current = 0;
	    };
	    controls.addEventListener("start", handleControlsStart);

    rendererRef.current = renderer;
    cameraRef.current = camera;
    sceneRef.current = scene;
    syncNotificationOverlay();

    function resize() {
      const w = containerEl.clientWidth;
      const h = containerEl.clientHeight;
      renderer.setSize(w, h);
      labelRenderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(containerEl);

    let raf = 0;
    const clock = new THREE.Clock();
    const autoFitSize = new THREE.Vector3();

    function animate() {
      raf = requestAnimationFrame(animate);
      const dt = Math.min(clock.getDelta(), 0.05);
      for (const tracked of trackedRef.current.values()) tracked.instance.tick?.(dt);
      notificationOverlayRef.current?.tick?.(dt);
      focusHelperRef.current?.update();

      const now = Date.now();
      const autoFitUntil = autoFitUntilRef.current;
      if (autoFitUntil && now <= autoFitUntil && !userInteractedWithCameraRef.current) {
        const lastCheckTs = autoFitLastCheckTsRef.current;
        if (!lastCheckTs || now - lastCheckTs >= AUTO_FIT_THROTTLE_MS) {
          autoFitLastCheckTsRef.current = now;
          const bounds = computeTrackedBounds(trackedRef.current);
          if (bounds) {
            const prev = autoFitLastBoundsRef.current;
            let changed = !prev;
            if (prev) {
              prev.getSize(autoFitSize);
              const tol = Math.max(0.06, autoFitSize.length() * 0.01);
              changed = bounds.min.distanceTo(prev.min) > tol || bounds.max.distanceTo(prev.max) > tol;
            }

            if (changed) {
              fitCameraAngledOverview(camera, controls, bounds);
              autoFitLastBoundsRef.current = bounds.clone();
              lastAutoFitCompositionIdRef.current = compositionIdRef.current ?? null;
            }
          }
        }
      } else if (autoFitUntil && now > autoFitUntil) {
        autoFitUntilRef.current = 0;
      }

      controls.update();
      renderer.render(scene, camera);
      labelRenderer.render(scene, camera);
    }

    animate();

    let downAt: { x: number; y: number } | null = null;
    let dragged = false;
    const DRAG_THRESHOLD_PX = 6;
    const LONG_PRESS_MS = 520;
    const DOUBLE_CLICK_MS = 320;
    const CLICK_DELAY_MS = 240;

    let downElementId: string | null = null;
    let longPressTimer: number | null = null;
    let longPressFired = false;
    let pendingClick: { id: string; at: number; timer: number } | null = null;

    function pickElementId(clientX: number, clientY: number): string | null {
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -(((clientY - rect.top) / rect.height) * 2 - 1);
      raycaster.setFromCamera(mouse, camera);

      const hits = raycaster.intersectObjects(scene.children, true);
      for (const hit of hits) {
        const id = findElementId(hit.object);
        if (!id) continue;
        if (viewRef.current.ghostWalls) {
          const tracked = trackedRef.current.get(id);
          const def = tracked ? elementTypesByIdRef.current[tracked.type] : null;
          if (def?.layerGroup === "walls") continue;
        }
        return id;
      }
      return null;
    }

    function tryHandleNotificationOverlay(clientX: number, clientY: number): boolean {
      const overlay = notificationOverlayRef.current;
      const notification = activeNotificationRef.current;
      if (!overlay || !notification) return false;

      const handler = overlay.onPointerEvent;
      if (!handler) return false;

      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -(((clientY - rect.top) / rect.height) * 2 - 1);
      raycaster.setFromCamera(mouse, camera);

      const hits = raycaster.intersectObject(overlay.object, true);
      if (hits.length === 0) return false;

      try {
        return Boolean(handler({ kind: "click", intersection: hits[0], notification }));
      } catch (err) {
        console.warn("[notificationOverlay.onPointerEvent]", err);
        return false;
      }
    }

    function handlePointerDown(e: PointerEvent) {
      containerEl.focus();
      downAt = { x: e.clientX, y: e.clientY };
      dragged = false;
      longPressFired = false;
      downElementId = onElementActivatedRef.current ? pickElementId(e.clientX, e.clientY) : null;

      if (longPressTimer) window.clearTimeout(longPressTimer);
      longPressTimer = null;

      if (onElementActivatedRef.current && downElementId && e.pointerType === "touch") {
        longPressTimer = window.setTimeout(() => {
          if (dragged) return;
          if (!downElementId) return;
          const handler = onElementActivatedRef.current;
          if (!handler) return;
          longPressFired = true;
          if (pendingClick) {
            window.clearTimeout(pendingClick.timer);
            pendingClick = null;
          }
          handler(downElementId, "longpress");
        }, LONG_PRESS_MS);
      }
    }

    function handlePointerMove(e: PointerEvent) {
      if (!downAt) return;
      const dx = e.clientX - downAt.x;
      const dy = e.clientY - downAt.y;
      if (dx * dx + dy * dy >= DRAG_THRESHOLD_PX * DRAG_THRESHOLD_PX) {
        dragged = true;
        if (longPressTimer) window.clearTimeout(longPressTimer);
        longPressTimer = null;
      }
    }

    function handlePointerUp(e: PointerEvent) {
      if (longPressTimer) window.clearTimeout(longPressTimer);
      longPressTimer = null;

      if (!downAt) return;
      downAt = null;
      if (dragged) return;
      const handler = onElementActivatedRef.current;
      if (!handler) {
        tryHandleNotificationOverlay(e.clientX, e.clientY);
        return;
      }
      if (longPressFired) return;

      if (tryHandleNotificationOverlay(e.clientX, e.clientY)) {
        if (pendingClick) window.clearTimeout(pendingClick.timer);
        pendingClick = null;
        return;
      }

      const id = pickElementId(e.clientX, e.clientY);
      if (!id) return;

      setFocusedElementId(id);

      const now = Date.now();

      if (pendingClick && pendingClick.id === id && now - pendingClick.at <= DOUBLE_CLICK_MS) {
        window.clearTimeout(pendingClick.timer);
        pendingClick = null;
        handler(id, "dblclick");
        return;
      }

      if (pendingClick && pendingClick.id !== id) {
        window.clearTimeout(pendingClick.timer);
        const prevId = pendingClick.id;
        pendingClick = null;
        handler(prevId, "click");
      }

      pendingClick = {
        id,
        at: now,
        timer: window.setTimeout(() => {
          if (!pendingClick || pendingClick.id !== id) return;
          pendingClick = null;
          handler(id, "click");
        }, CLICK_DELAY_MS),
      };
    }

    function handleContextMenu(e: MouseEvent) {
      e.preventDefault();
    }

    renderer.domElement.addEventListener("pointerdown", handlePointerDown);
    renderer.domElement.addEventListener("pointermove", handlePointerMove);
    renderer.domElement.addEventListener("pointerup", handlePointerUp);
    renderer.domElement.addEventListener("pointercancel", handlePointerUp);
    renderer.domElement.addEventListener("contextmenu", handleContextMenu);

	    return () => {
      if (longPressTimer) window.clearTimeout(longPressTimer);
      longPressTimer = null;
      if (pendingClick) window.clearTimeout(pendingClick.timer);
      pendingClick = null;

      if (focusHelperRef.current) {
        scene.remove(focusHelperRef.current);
        focusHelperRef.current.geometry.dispose();
        const mat = focusHelperRef.current.material as unknown as THREE.Material | THREE.Material[];
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else mat.dispose();
        focusHelperRef.current = null;
      }

      renderer.domElement.removeEventListener("pointerdown", handlePointerDown);
      renderer.domElement.removeEventListener("pointermove", handlePointerMove);
      renderer.domElement.removeEventListener("pointerup", handlePointerUp);
      renderer.domElement.removeEventListener("pointercancel", handlePointerUp);
      renderer.domElement.removeEventListener("contextmenu", handleContextMenu);
      ro.disconnect();
	      cancelAnimationFrame(raf);

	      controls.dispose();
	      controls.removeEventListener("start", handleControlsStart);
	      controlsRef.current = null;

      if (notificationOverlayRef.current) {
        scene.remove(notificationOverlayRef.current.object);
        notificationOverlayRef.current.dispose?.();
        notificationOverlayRef.current = null;
      }

      for (const tracked of trackedRef.current.values()) tracked.instance.dispose?.();
      trackedRef.current.clear();

      renderer.dispose();
      containerEl.removeChild(renderer.domElement);
      containerEl.removeChild(labelRenderer.domElement);
      rendererRef.current = null;
      cameraRef.current = null;
      sceneRef.current = null;
    };
  }, [mouse, raycaster]);

	  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;

    if (focusHelperRef.current) {
      scene.remove(focusHelperRef.current);
      focusHelperRef.current.geometry.dispose();
      const mat = focusHelperRef.current.material as unknown as THREE.Material | THREE.Material[];
      if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
      else mat.dispose();
      focusHelperRef.current = null;
    }

    if (!focusedElementId) return;
    const tracked = trackedRef.current.get(focusedElementId);
    if (!tracked) return;

    const helper = new THREE.BoxHelper(tracked.instance.object, FOCUS_HIGHLIGHT_COLOR);
    const mat = helper.material as unknown as THREE.Material | THREE.Material[];
    if (Array.isArray(mat)) {
      for (const m of mat) {
        (m as any).depthTest = false;
        m.transparent = true;
      }
    } else {
      (mat as any).depthTest = false;
      mat.transparent = true;
    }
    helper.renderOrder = 9999;
    scene.add(helper);
    focusHelperRef.current = helper;
  }, [focusedElementId]);

  useEffect(() => {
    const scene = sceneRef.current;
    const renderer = rendererRef.current;
    const camera = cameraRef.current;
    if (!scene || !renderer || !camera) return;

	    const viewKey = `${viewSettings.wallHeightPreset}:${viewSettings.wallHeight}:${Boolean(viewSettings.ghostWalls)}:${viewSettings.graphicsQuality ?? "simplified"}`;
	    const viewChanged = viewKeyRef.current !== viewKey;
	    viewKeyRef.current = viewKey;
	    const ghostWallsEnabled = Boolean(viewSettings.ghostWalls);
    const shadowsEnabled = ghostWallsEnabled;
    if (renderer.shadowMap.enabled !== shadowsEnabled) {
      renderer.shadowMap.enabled = shadowsEnabled;
      renderer.shadowMap.needsUpdate = true;
    }

	    const tracked = trackedRef.current;
	    const elementsById = new Map(elements.map((e) => [e.id, e]));
	    const areaElements = elements.filter((e) => (elementTypesById[e.type]?.layerGroup ?? "") === "areas");
	    const areaOrderById = new Map<string, number>();
	    for (let i = 0; i < areaElements.length; i += 1) areaOrderById.set(areaElements[i].id, i);

    for (const [id, entry] of tracked.entries()) {
      const element = elementsById.get(id);
      const def = element ? elementTypesById[element.type] : null;
      if (!element || !def?.create3D) {
        scene.remove(entry.instance.object);
        entry.instance.dispose?.();
        tracked.delete(id);
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

        const view = def.layerGroup === "walls" ? viewRef.current : elementViewRef.current;
        const instance = def.create3D({ THREE, scene, camera, renderer, view, compositionId: compositionIdRef.current }, element);
        (instance.object.userData as any)[ELEMENT_ID] = element.id;
        scene.add(instance.object);
        if (def.layerGroup === "walls") applyGhostWalls(instance.object, ghostWallsEnabled);
        tracked.set(element.id, { type: element.type, instance, last: element });
      }

      const entry = tracked.get(element.id);
      if (!entry) continue;

      entry.instance.object.position.set(element.position.x, element.position.y, element.position.z);
      entry.instance.object.rotation.set(element.rotation.x, element.rotation.y, element.rotation.z);

	      if (viewChanged || !elementsEqual(entry.last, element)) {
	        entry.instance.update?.(element);
	        entry.last = element;
	      }

	      if (viewChanged && def.layerGroup === "walls") applyGhostWalls(entry.instance.object, ghostWallsEnabled);
	      if (def.layerGroup === "areas") {
	        const order = areaOrderById.get(element.id);
	        if (typeof order === "number") {
	          const stackIndex = areaElements.length - 1 - order;
	          applyPolygonOffsetUnits(entry.instance.object, 1 + stackIndex);
	        }
	      }
	    }

	    const fitKey = compositionId ?? null;
	    const controls = controlsRef.current;
	    if (controls && !userInteractedWithCameraRef.current && lastAutoFitCompositionIdRef.current !== fitKey) {
	      const bounds = computeTrackedBounds(tracked);
	      if (bounds) {
	        fitCameraAngledOverview(camera, controls, bounds);
	        lastAutoFitCompositionIdRef.current = fitKey;
          autoFitLastBoundsRef.current = bounds.clone();
          autoFitUntilRef.current = Date.now() + AUTO_FIT_GRACE_MS;
          autoFitLastCheckTsRef.current = 0;
	      }
	    }
	  }, [
	    elements,
	    elementTypesById,
	    viewSettings.ghostWalls,
	    viewSettings.graphicsQuality,
	    viewSettings.wallHeight,
	    viewSettings.wallHeightPreset,
	    compositionId,
	  ]);

  function focusNext(delta: number) {
    if (focusables.length === 0) return;
    const idx = focusables.findIndex((f) => f.id === focusedElementId);
    const nextIdx = idx === -1 ? (delta >= 0 ? 0 : focusables.length - 1) : (idx + delta + focusables.length) % focusables.length;
    setFocusedElementId(focusables[nextIdx].id);
  }

  function focusDirection(direction: "left" | "right" | "up" | "down") {
    if (focusables.length === 0) return;
    const current = focusables.find((f) => f.id === focusedElementId) ?? focusables[0];

    let best: { id: string; score: number } | null = null;
    for (const cand of focusables) {
      if (cand.id === current.id) continue;
      const dx = cand.x - current.x;
      const dz = cand.z - current.z;
      const dist = Math.hypot(dx, dz);
      if (dist < 1e-6) continue;

      let parallel = 0;
      let perp = 0;
      if (direction === "right") {
        if (dx <= 1e-6) continue;
        parallel = dx;
        perp = Math.abs(dz);
      } else if (direction === "left") {
        if (dx >= -1e-6) continue;
        parallel = -dx;
        perp = Math.abs(dz);
      } else if (direction === "down") {
        if (dz <= 1e-6) continue;
        parallel = dz;
        perp = Math.abs(dx);
      } else {
        // up => negative Z
        if (dz >= -1e-6) continue;
        parallel = -dz;
        perp = Math.abs(dx);
      }

      const angle = Math.atan2(perp, parallel);
      const score = angle * 1000 + dist;
      if (!best || score < best.score) best = { id: cand.id, score };
    }

    if (best) setFocusedElementId(best.id);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.defaultPrevented) return;
    if (e.key === "Escape") {
      if (focusedElementId) {
        e.preventDefault();
        setFocusedElementId(null);
      }
      return;
    }

    if (e.key === "Tab") {
      if (!focusedElementId) return;
      if (focusables.length === 0) return;
      e.preventDefault();
      focusNext(e.shiftKey ? -1 : 1);
      return;
    }

    if (e.key === "ArrowLeft") {
      if (focusables.length === 0) return;
      e.preventDefault();
      if (!focusedElementId) setFocusedElementId(focusables[0].id);
      else focusDirection("left");
      return;
    }
    if (e.key === "ArrowRight") {
      if (focusables.length === 0) return;
      e.preventDefault();
      if (!focusedElementId) setFocusedElementId(focusables[0].id);
      else focusDirection("right");
      return;
    }
    if (e.key === "ArrowUp") {
      if (focusables.length === 0) return;
      e.preventDefault();
      if (!focusedElementId) setFocusedElementId(focusables[0].id);
      else focusDirection("up");
      return;
    }
    if (e.key === "ArrowDown") {
      if (focusables.length === 0) return;
      e.preventDefault();
      if (!focusedElementId) setFocusedElementId(focusables[0].id);
      else focusDirection("down");
      return;
    }

    if (e.key === "Enter" || e.key === " ") {
      const handler = onElementActivatedRef.current;
      if (!handler || !focusedElementId) return;
      e.preventDefault();
      handler(focusedElementId, "click");
    }
  }

  return <div className="viewportRoot" ref={containerRef} tabIndex={0} onKeyDown={handleKeyDown} />;
}
