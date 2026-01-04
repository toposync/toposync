import React, { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";

import type { CompositionElement, Element3DInstance, ElementType, ViewSettings } from "@toposync/plugin-api";

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  onElementActivated?: (elementId: string, intent?: "click" | "dblclick" | "longpress") => void;
  viewSettings: ViewSettings;
};

type Tracked = {
  type: string;
  instance: Element3DInstance;
  last: CompositionElement;
};

const ELEMENT_ID = "__toposyncElementId";

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
}: Props): React.ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const trackedRef = useRef<Map<string, Tracked>>(new Map());
  const viewRef = useRef<ViewSettings>({
    wallHeightPreset: "high",
    wallHeight: 2.7,
  });
  const viewKeyRef = useRef<string>("");

  const raycaster = useMemo(() => new THREE.Raycaster(), []);
  const mouse = useMemo(() => new THREE.Vector2(), []);

  useEffect(() => {
    viewRef.current.wallHeightPreset = viewSettings.wallHeightPreset;
    viewRef.current.wallHeight = viewSettings.wallHeight;
  }, [viewSettings.wallHeight, viewSettings.wallHeightPreset]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const containerEl: HTMLDivElement = container;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setClearColor(0x070a14, 1);
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

    const grid = new THREE.GridHelper(12, 24, 0x23304d, 0x162040);
    grid.position.y = 0;
    scene.add(grid);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.7;
    controls.zoomSpeed = 0.9;
    controls.panSpeed = 0.8;
    controls.target.set(0, 0.2, 0);
    controls.minDistance = 1.2;
    controls.maxDistance = 40;
    controls.maxPolarAngle = Math.PI / 2 - 0.02;
    controls.update();

    rendererRef.current = renderer;
    cameraRef.current = camera;
    sceneRef.current = scene;

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

    function animate() {
      raf = requestAnimationFrame(animate);
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
        if (id) return id;
      }
      return null;
    }

    function handlePointerDown(e: PointerEvent) {
      downAt = { x: e.clientX, y: e.clientY };
      dragged = false;
      longPressFired = false;
      downElementId = onElementActivated ? pickElementId(e.clientX, e.clientY) : null;

      if (longPressTimer) window.clearTimeout(longPressTimer);
      longPressTimer = null;

      if (onElementActivated && downElementId && e.pointerType === "touch") {
        longPressTimer = window.setTimeout(() => {
          if (dragged) return;
          if (!downElementId) return;
          longPressFired = true;
          if (pendingClick) {
            window.clearTimeout(pendingClick.timer);
            pendingClick = null;
          }
          onElementActivated(downElementId, "longpress");
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
      if (!onElementActivated) return;
      if (longPressFired) return;

      const id = pickElementId(e.clientX, e.clientY);
      if (!id) return;

      const now = Date.now();

      if (pendingClick && pendingClick.id === id && now - pendingClick.at <= DOUBLE_CLICK_MS) {
        window.clearTimeout(pendingClick.timer);
        pendingClick = null;
        onElementActivated(id, "dblclick");
        return;
      }

      if (pendingClick && pendingClick.id !== id) {
        window.clearTimeout(pendingClick.timer);
        const prevId = pendingClick.id;
        pendingClick = null;
        onElementActivated(prevId, "click");
      }

      pendingClick = {
        id,
        at: now,
        timer: window.setTimeout(() => {
          if (!pendingClick || pendingClick.id !== id) return;
          pendingClick = null;
          onElementActivated(id, "click");
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

      renderer.domElement.removeEventListener("pointerdown", handlePointerDown);
      renderer.domElement.removeEventListener("pointermove", handlePointerMove);
      renderer.domElement.removeEventListener("pointerup", handlePointerUp);
      renderer.domElement.removeEventListener("pointercancel", handlePointerUp);
      renderer.domElement.removeEventListener("contextmenu", handleContextMenu);
      ro.disconnect();
      cancelAnimationFrame(raf);

      controls.dispose();

      for (const tracked of trackedRef.current.values()) tracked.instance.dispose?.();
      trackedRef.current.clear();

      renderer.dispose();
      containerEl.removeChild(renderer.domElement);
      containerEl.removeChild(labelRenderer.domElement);
      rendererRef.current = null;
      cameraRef.current = null;
      sceneRef.current = null;
    };
  }, [mouse, onElementActivated, raycaster]);

  useEffect(() => {
    const scene = sceneRef.current;
    const renderer = rendererRef.current;
    const camera = cameraRef.current;
    if (!scene || !renderer || !camera) return;

    const viewKey = `${viewSettings.wallHeightPreset}:${viewSettings.wallHeight}`;
    const viewChanged = viewKeyRef.current !== viewKey;
    viewKeyRef.current = viewKey;

    const tracked = trackedRef.current;
    const elementsById = new Map(elements.map((e) => [e.id, e]));

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

        const instance = def.create3D({ THREE, scene, camera, renderer, view: viewRef.current }, element);
        (instance.object.userData as any)[ELEMENT_ID] = element.id;
        scene.add(instance.object);
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
    }
  }, [elements, elementTypesById, viewSettings.wallHeight, viewSettings.wallHeightPreset]);

  return <div className="viewportRoot" ref={containerRef} />;
}
