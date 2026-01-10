import type { CompositionElement, Element3DInstance, ElementType, ViewSettings } from "@toposync/plugin-api";
import * as THREE from "three";

const RENDER_DIR_ID = "render2d";
const RENDER_VERSION = 6 as const;
const HOME_ASSISTANT_ELEMENT_TYPE_ID = "com.toposync.home_assistant.item";
const MODEL_ELEMENT_TYPE_ID = "com.toposync.models.gltf";
const IMAGE_ELEMENT_TYPE_ID = "com.toposync.images.image";
const AREA_ELEMENT_TYPE_ID = "com.toposync.structural.area";
const POOL_ELEMENT_TYPE_ID = "com.toposync.structural.pool";
const WALL_ELEMENT_TYPE_ID = "com.toposync.structural.wall";

type BoundsXZ = {
  minX: number;
  maxX: number;
  minZ: number;
  maxZ: number;
};

export type Main2DOverlayManifest =
  | { elementId: string; kind: "lamp"; url: string }
  | { elementId: string; kind: "airflow"; mode: "cool" | "heat" | "neutral"; url: string };

export type Main2DRenderManifest = {
  version: typeof RENDER_VERSION;
  compositionId: string;
  signature: string;
  widthPx: number;
  heightPx: number;
  bounds: BoundsXZ;
  base: { url: string };
  overlays: Main2DOverlayManifest[];
};

type UploadFileResponse = {
  dir: string;
  path: string;
  url: string;
  filename: string;
  content_type: string | null;
  size_bytes: number;
};

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return {};
}

function stableStringify(value: unknown): string {
  const seen = new Set<unknown>();
  function inner(v: unknown): any {
    if (v === null) return null;
    const t = typeof v;
    if (t === "string" || t === "number" || t === "boolean") return v;
    if (t !== "object") return null;
    if (seen.has(v)) return null;
    seen.add(v);

    if (Array.isArray(v)) return v.map(inner);

    const rec = v as Record<string, unknown>;
    const keys = Object.keys(rec).sort((a, b) => a.localeCompare(b));
    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = inner(rec[key]);
    return out;
  }
  return JSON.stringify(inner(value));
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function readPlanePoint(value: unknown, fallback: { x: number; z: number }): { x: number; z: number } {
  const rec = asRecord(value);
  return {
    x: readNumber(rec.x, fallback.x),
    z: readNumber(rec.z, fallback.z),
  };
}

function readPlanePointArray(value: unknown): Array<{ x: number; z: number }> {
  if (!Array.isArray(value)) return [];
  const out: Array<{ x: number; z: number }> = [];
  for (const entry of value) {
    const rec = asRecord(entry);
    const x = readNumber(rec.x, NaN);
    const z = readNumber(rec.z, NaN);
    if (!Number.isFinite(x) || !Number.isFinite(z)) continue;
    out.push({ x, z });
  }
  return out;
}

function readVector3(value: unknown, fallback: { x: number; y: number; z: number }): { x: number; y: number; z: number } {
  const rec = asRecord(value);
  return {
    x: readNumber(rec.x, fallback.x),
    y: readNumber(rec.y, fallback.y),
    z: readNumber(rec.z, fallback.z),
  };
}

function readSpecialView(value: unknown): "none" | "lamp" | "airflow" {
  const v = readString(value).trim().toLowerCase();
  return v === "lamp" || v === "airflow" ? v : "none";
}

function computeViewBoundsXZ(bounds: THREE.Box3, paddingRatio: number): { viewBounds: BoundsXZ; viewWidth: number; viewHeight: number } {
  const size = new THREE.Vector3();
  bounds.getSize(size);

  const center = new THREE.Vector3();
  bounds.getCenter(center);

  const safeX = Math.max(1, Math.abs(size.x));
  const safeZ = Math.max(1, Math.abs(size.z));

  const viewWidth = safeX * (1 + paddingRatio * 2);
  const viewHeight = safeZ * (1 + paddingRatio * 2);

  return {
    viewBounds: {
      minX: center.x - viewWidth / 2,
      maxX: center.x + viewWidth / 2,
      minZ: center.z - viewHeight / 2,
      maxZ: center.z + viewHeight / 2,
    },
    viewWidth,
    viewHeight,
  };
}

function computeRenderSize(viewWidth: number, viewHeight: number, maximumRenderSize: number): { renderWidth: number; renderHeight: number } {
  const longest = Math.max(viewWidth, viewHeight, 1e-6);
  const scale = maximumRenderSize / longest;
  return {
    renderWidth: Math.max(64, Math.round(viewWidth * scale)),
    renderHeight: Math.max(64, Math.round(viewHeight * scale)),
  };
}

function createRenderer(renderWidth: number, renderHeight: number, clearColorHex: number, clearAlpha: number): THREE.WebGLRenderer {
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: true,
    stencil: true,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(renderWidth, renderHeight, false);
  renderer.setClearColor(clearColorHex, clearAlpha);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  return renderer;
}

function buildCamera(viewBounds: BoundsXZ, viewWidth: number, viewHeight: number): THREE.OrthographicCamera {
  const camera = new THREE.OrthographicCamera(
    -viewWidth / 2,
    viewWidth / 2,
    viewHeight / 2,
    -viewHeight / 2,
    0.01,
    Math.max(viewWidth, viewHeight) * 10,
  );

  const centerX = (viewBounds.minX + viewBounds.maxX) / 2;
  const centerZ = (viewBounds.minZ + viewBounds.maxZ) / 2;
  const cameraY = Math.max(viewWidth, viewHeight);

  camera.position.set(centerX, cameraY, centerZ);
  camera.up.set(0, 0, -1);
  camera.lookAt(new THREE.Vector3(centerX, 0, centerZ));
  camera.updateProjectionMatrix();
  return camera;
}

function applyGhostWalls(object: THREE.Object3D, enabled: boolean): void {
  const ghostOpacity = 0.22;
  const stateKey = "__toposyncGhostWallsOriginal";
  object.traverse((node) => {
    const matRaw = (node as any).material as unknown;
    if (!matRaw) return;
    const mats = Array.isArray(matRaw) ? matRaw : [matRaw];
    for (const m of mats) {
      if (!m || !(m as any).isMaterial) continue;
      const mat = m as THREE.Material;
      const userData = (mat.userData ??= {});

      if (enabled) {
        if (!(stateKey in userData)) {
          (userData as any)[stateKey] = {
            opacity: (mat as any).opacity,
            transparent: mat.transparent,
            depthWrite: (mat as any).depthWrite,
          };
        }

        if (typeof (mat as any).opacity === "number") (mat as any).opacity = ghostOpacity;
        mat.transparent = true;
        if (typeof (mat as any).depthWrite === "boolean") (mat as any).depthWrite = false;
        mat.needsUpdate = true;
        continue;
      }

      const original = (userData as any)[stateKey];
      if (original && typeof original === "object") {
        if (typeof original.opacity === "number" && typeof (mat as any).opacity === "number") (mat as any).opacity = original.opacity;
        if (typeof original.transparent === "boolean") mat.transparent = original.transparent;
        if (typeof original.depthWrite === "boolean" && typeof (mat as any).depthWrite === "boolean")
          (mat as any).depthWrite = original.depthWrite;
        delete (userData as any)[stateKey];
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

function computeTrackedBounds(instances: Array<{ element: CompositionElement; def: ElementType; instance: Element3DInstance }>): THREE.Box3 | null {
  const out = new THREE.Box3();
  let hasAny = false;
  for (const { instance } of instances) {
    instance.object.updateWorldMatrix(true, true);
    const box = new THREE.Box3().setFromObject(instance.object);
    if (box.isEmpty()) continue;
    out.union(box);
    hasAny = true;
  }
  return hasAny ? out : null;
}

function createBoundsAccumulator(): BoundsXZ & { _empty: boolean } {
  return { minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity, _empty: true };
}

function includeBoundsPoint(bounds: BoundsXZ & { _empty: boolean }, point: { x: number; z: number }): void {
  if (!Number.isFinite(point.x) || !Number.isFinite(point.z)) return;
  bounds.minX = Math.min(bounds.minX, point.x);
  bounds.maxX = Math.max(bounds.maxX, point.x);
  bounds.minZ = Math.min(bounds.minZ, point.z);
  bounds.maxZ = Math.max(bounds.maxZ, point.z);
  bounds._empty = false;
}

function includeBoundsExpandedPoint(bounds: BoundsXZ & { _empty: boolean }, point: { x: number; z: number }, expand: number): void {
  includeBoundsPoint(bounds, { x: point.x - expand, z: point.z - expand });
  includeBoundsPoint(bounds, { x: point.x + expand, z: point.z + expand });
}

function includeBoundsRotatedRect(
  bounds: BoundsXZ & { _empty: boolean },
  center: { x: number; z: number },
  size: { x: number; z: number },
  rotationY: number,
): void {
  const halfX = Math.abs(size.x) / 2;
  const halfZ = Math.abs(size.z) / 2;
  if (!Number.isFinite(halfX) || !Number.isFinite(halfZ) || (halfX < 1e-9 && halfZ < 1e-9)) return;

  const cos = Math.cos(rotationY);
  const sin = Math.sin(rotationY);

  const corners = [
    { x: -halfX, z: -halfZ },
    { x: halfX, z: -halfZ },
    { x: halfX, z: halfZ },
    { x: -halfX, z: halfZ },
  ];

  for (const c of corners) {
    const rx = c.x * cos - c.z * sin;
    const rz = c.x * sin + c.z * cos;
    includeBoundsPoint(bounds, { x: center.x + rx, z: center.z + rz });
  }
}

function computeBoundsFromElements(elements: CompositionElement[], overlayTargets: Array<{ element: CompositionElement }>): BoundsXZ {
  const bounds = createBoundsAccumulator();

  for (const element of elements) {
    includeBoundsPoint(bounds, { x: element.position.x, z: element.position.z });

    const props = asRecord(element.props);
    if (element.type === AREA_ELEMENT_TYPE_ID || element.type === POOL_ELEMENT_TYPE_ID) {
      const vertices = readPlanePointArray(props.vertices);
      for (const p of vertices) includeBoundsPoint(bounds, p);
      continue;
    }

    if (element.type === WALL_ELEMENT_TYPE_ID) {
      const start = readPlanePoint(props.a, { x: element.position.x - 0.5, z: element.position.z });
      const end = readPlanePoint(props.b, { x: element.position.x + 0.5, z: element.position.z });
      const width = Math.max(0.02, readNumber(props.width, 0.12));
      includeBoundsExpandedPoint(bounds, start, width);
      includeBoundsExpandedPoint(bounds, end, width);
      continue;
    }

    if (element.type === MODEL_ELEMENT_TYPE_ID) {
      const size = readVector3(props.size, { x: 1, y: 1, z: 1 });
      const scale = Math.max(1e-6, readNumber(props.scale, 1));
      includeBoundsRotatedRect(bounds, element.position, { x: size.x * scale, z: size.z * scale }, element.rotation.y);
      continue;
    }

    if (element.type === IMAGE_ELEMENT_TYPE_ID) {
      const width = readNumber(props.width_m, 1);
      const depth = readNumber(props.depth_m, 1);
      includeBoundsRotatedRect(bounds, element.position, { x: width, z: depth }, element.rotation.y);
      continue;
    }
  }

  for (const target of overlayTargets) includeBoundsPoint(bounds, { x: target.element.position.x, z: target.element.position.z });

  if (bounds._empty) return { minX: -1, maxX: 1, minZ: -1, maxZ: 1 };
  return { minX: bounds.minX, maxX: bounds.maxX, minZ: bounds.minZ, maxZ: bounds.maxZ };
}

function expandBoundsXZ(bounds: THREE.Box3, p: { x: number; z: number }): void {
  if (!Number.isFinite(p.x) || !Number.isFinite(p.z)) return;
  bounds.expandByPoint(new THREE.Vector3(p.x, 0, p.z));
}

function addDefaultLights(scene: THREE.Scene): THREE.Light[] {
  const ambient = new THREE.AmbientLight(0xffffff, 0.55);
  scene.add(ambient);
  const directional = new THREE.DirectionalLight(0xffffff, 0.85);
  directional.position.set(2.2, 6, 3);
  scene.add(directional);
  return [ambient, directional];
}

function hideNonLightRenderables(object: THREE.Object3D): void {
  object.traverse((node) => {
    const anyNode = node as any;
    if (anyNode?.isLight) return;
    if (anyNode?.isMesh || anyNode?.isLine || anyNode?.isPoints || anyNode?.isSprite) anyNode.visible = false;
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function isAssetReady(entry: { element: CompositionElement; instance: Element3DInstance }): boolean {
  const props = asRecord(entry.element.props);

  if (entry.element.type === MODEL_ELEMENT_TYPE_ID) {
    const dir = readString(props.dir).trim();
    const model = readString(props.model).trim();
    if (!dir || !model) return true;
    return entry.instance.object.children.length > 0;
  }

  if (entry.element.type === IMAGE_ELEMENT_TYPE_ID) {
    const dir = readString(props.dir).trim();
    const file = readString(props.file).trim();
    const mode = readString(props.mode).trim().toLowerCase() || "overlay";
    const shouldRender = Boolean(dir && file && mode === "overlay");
    if (!shouldRender) return true;

    let pending = false;
    entry.instance.object.traverse((node) => {
      const mesh = node as any;
      if (!mesh?.isMesh) return;
      if (!mesh.visible) return;
      const material = mesh.material as any;
      if (!material) return;
      if (material.map == null) pending = true;
    });
    return !pending;
  }

  return true;
}

async function warmupCapture(
  instances: Array<{ element: CompositionElement; instance: Element3DInstance }>,
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.Camera,
  options: { maxWaitMs: number; warmupSeconds: number; stepMs: number },
): Promise<void> {
  const start = performance.now();
  let simulated = 0;

  while (true) {
    const dt = Math.min(0.05, options.stepMs / 1000);
    for (const entry of instances) entry.instance.tick?.(dt);
    renderer.render(scene, camera);
    simulated += dt;

    const elapsed = performance.now() - start;
    const assetsReady = instances.every((entry) => isAssetReady(entry));
    const warmedUp = simulated >= options.warmupSeconds;

    if ((assetsReady && warmedUp) || elapsed >= options.maxWaitMs) return;
    await sleep(options.stepMs);
  }
}

async function sha256Hex(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

type SignatureElement = {
  id: string;
  type: string;
  name: string;
  position: CompositionElement["position"];
  rotation: CompositionElement["rotation"];
  props: Record<string, unknown>;
};

function buildSignatureElements(elements: CompositionElement[]): SignatureElement[] {
  return elements
    .map((el): SignatureElement => {
      const props = asRecord(el.props);
      if (el.type === HOME_ASSISTANT_ELEMENT_TYPE_ID) {
        const { primary_state: _ignoredPrimaryState, ...rest } = props as Record<string, unknown>;
        return {
          id: el.id,
          type: el.type,
          name: el.name,
          position: el.position,
          rotation: el.rotation,
          props: rest,
        };
      }
      return {
        id: el.id,
        type: el.type,
        name: el.name,
        position: el.position,
        rotation: el.rotation,
        props,
      };
    })
    .sort((a, b) => a.id.localeCompare(b.id));
}

function manifestFilePrefix(compositionId: string, signature: string): string {
  return `render2d_v${RENDER_VERSION}_${compositionId}_${signature}`;
}

async function fetchJson<T>(url: string): Promise<T | null> {
  const response = await fetch(url);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Failed to fetch ${url}: ${response.status}`);
  return response.json();
}

async function uploadBlob(dir: string, filename: string, blob: Blob): Promise<UploadFileResponse> {
  const form = new FormData();
  form.append("dir", dir);
  form.append("filename", filename);
  form.append("file", blob, filename);
  const response = await fetch("/api/files/upload", { method: "POST", body: form });
  if (!response.ok) throw new Error(`Failed to upload ${filename}: ${response.status}`);
  return response.json();
}

async function dataUrlToBlob(dataUrl: string): Promise<Blob> {
  const response = await fetch(dataUrl);
  if (!response.ok) throw new Error("Failed to convert dataUrl to blob");
  return response.blob();
}

function pickOverlayTargets(elements: CompositionElement[]): Array<{ elementId: string; specialView: "lamp" | "airflow"; element: CompositionElement }> {
  const out: Array<{ elementId: string; specialView: "lamp" | "airflow"; element: CompositionElement }> = [];
  for (const el of elements) {
    if (el.type !== HOME_ASSISTANT_ELEMENT_TYPE_ID) continue;
    const props = asRecord(el.props);
    const special = readSpecialView(props.special_view);
    if (special === "lamp" || special === "airflow") out.push({ elementId: el.id, specialView: special, element: el });
  }
  return out;
}

type AirflowOverlayMode = "cool" | "heat" | "neutral";

function forceHomeAssistantVisualOn(
  el: CompositionElement,
  args: { specialView: "lamp" } | { specialView: "airflow"; mode: AirflowOverlayMode },
): CompositionElement {
  const props = asRecord(el.props);
  const primaryState =
    args.specialView === "lamp" ? "on" : args.mode === "heat" ? "heat" : args.mode === "neutral" ? "fan_only" : "cool";
  return {
    ...el,
    props: {
      ...props,
      server_id: "",
      primary_state: primaryState,
    },
  };
}

function createInstances(
  scene: THREE.Scene,
  camera: THREE.Camera,
  renderer: THREE.WebGLRenderer,
  elements: CompositionElement[],
  elementTypesById: Record<string, ElementType>,
  view: ViewSettings,
  compositionId: string,
): Array<{ element: CompositionElement; def: ElementType; instance: Element3DInstance }> {
  const instances: Array<{ element: CompositionElement; def: ElementType; instance: Element3DInstance }> = [];

  const areaElements = elements.filter((e) => (elementTypesById[e.type]?.layerGroup ?? "") === "areas");
  const areaOrderById = new Map<string, number>();
  for (let i = 0; i < areaElements.length; i += 1) areaOrderById.set(areaElements[i].id, i);

  for (const element of elements) {
    const def = elementTypesById[element.type];
    if (!def?.create3D) continue;
    const instance = def.create3D({ THREE, scene, camera, renderer, view, compositionId }, element);
    instances.push({ element, def, instance });
    scene.add(instance.object);
    if (def.layerGroup === "walls") applyGhostWalls(instance.object, Boolean(view.ghostWalls));
    if (def.layerGroup === "areas") {
      const order = areaOrderById.get(element.id);
      if (typeof order === "number") {
        const stackIndex = areaElements.length - 1 - order;
        applyPolygonOffsetUnits(instance.object, 1 + stackIndex);
      }
    }
  }

  for (const entry of instances) {
    const { element, instance } = entry;
    instance.object.position.set(element.position.x, element.position.y, element.position.z);
    instance.object.rotation.set(element.rotation.x, element.rotation.y, element.rotation.z);
  }

  return instances;
}

function disposeInstances(instances: Array<{ instance: Element3DInstance }>): void {
  for (const { instance } of instances) {
    try {
      instance.dispose?.();
    } catch {
      // ignore
    }
  }
}

async function captureBaseAndOverlays(args: {
  compositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  maximumRenderSize: number;
}): Promise<
  Omit<Main2DRenderManifest, "base" | "overlays" | "signature"> & {
    baseDataUrl: string;
    overlays: Array<Main2DOverlayManifest & { dataUrl: string }>;
  }
> {
  const { compositionId, elements, elementTypesById } = args;
  const overlayTargets = pickOverlayTargets(elements);
  const nonHomeAssistantElements = elements.filter((el) => el.type !== HOME_ASSISTANT_ELEMENT_TYPE_ID);

  const paddingRatio = 0.08;
  const view: ViewSettings = {
    wallHeightPreset: "high",
    wallHeight: 2.7,
    ghostWalls: false,
    graphicsQuality: "detailed",
  };

  const boundsXZ = computeBoundsFromElements(nonHomeAssistantElements, overlayTargets);
  const boundsBox = new THREE.Box3(new THREE.Vector3(boundsXZ.minX, 0, boundsXZ.minZ), new THREE.Vector3(boundsXZ.maxX, 0, boundsXZ.maxZ));
  const { viewBounds, viewWidth, viewHeight } = computeViewBoundsXZ(boundsBox, paddingRatio);
  const { renderWidth, renderHeight } = computeRenderSize(viewWidth, viewHeight, args.maximumRenderSize);

  const scene = new THREE.Scene();
  const renderer = createRenderer(renderWidth, renderHeight, 0x070a14, 0);
  const camera = buildCamera(viewBounds, viewWidth, viewHeight);
  const defaultLights = addDefaultLights(scene);

  const baseInstances = createInstances(scene, camera, renderer, nonHomeAssistantElements, elementTypesById, view, compositionId);
  await warmupCapture(
    baseInstances.map((entry) => ({ element: entry.element, instance: entry.instance })),
    renderer,
    scene,
    camera,
    { maxWaitMs: 6500, warmupSeconds: 0.25, stepMs: 60 },
  );
  renderer.render(scene, camera);
  const baseDataUrl = renderer.domElement.toDataURL("image/png");

  for (const light of defaultLights) scene.remove(light);
  renderer.setClearColor(0x000000, 1);

  // Overlay captures.
  const overlays: Array<Main2DOverlayManifest & { dataUrl: string }> = [];
  for (const target of overlayTargets) {
    const variants: Array<Main2DOverlayManifest & { forced: CompositionElement; warmupSeconds: number }> =
      target.specialView === "lamp"
        ? [
            {
              elementId: target.elementId,
              kind: "lamp",
              url: "",
              forced: forceHomeAssistantVisualOn(target.element, { specialView: "lamp" }),
              warmupSeconds: 0.35,
            },
          ]
        : ([
            {
              elementId: target.elementId,
              kind: "airflow",
              mode: "cool",
              url: "",
              forced: forceHomeAssistantVisualOn(target.element, { specialView: "airflow", mode: "cool" }),
              warmupSeconds: 2.0,
            },
            {
              elementId: target.elementId,
              kind: "airflow",
              mode: "heat",
              url: "",
              forced: forceHomeAssistantVisualOn(target.element, { specialView: "airflow", mode: "heat" }),
              warmupSeconds: 2.0,
            },
            {
              elementId: target.elementId,
              kind: "airflow",
              mode: "neutral",
              url: "",
              forced: forceHomeAssistantVisualOn(target.element, { specialView: "airflow", mode: "neutral" }),
              warmupSeconds: 2.0,
            },
          ] satisfies Array<Main2DOverlayManifest & { forced: CompositionElement; warmupSeconds: number }>);

    for (const variant of variants) {
      const { forced, warmupSeconds, ...overlayManifest } = variant;
      // No extra lighting: we want the special-view element to drive the glow.
      const overlayInstances = createInstances(scene, camera, renderer, [forced], elementTypesById, view, compositionId);
      if (overlayManifest.kind === "lamp") {
        const forcedInstance = overlayInstances[0]?.instance ?? null;
        if (forcedInstance) hideNonLightRenderables(forcedInstance.object);
      }
      await warmupCapture(
        overlayInstances.map((entry) => ({ element: entry.element, instance: entry.instance })),
        renderer,
        scene,
        camera,
        {
          maxWaitMs: 5000,
          warmupSeconds,
          stepMs: 50,
        },
      );
      renderer.render(scene, camera);
      const dataUrl = renderer.domElement.toDataURL("image/png");
      overlays.push({ ...overlayManifest, dataUrl });

      disposeInstances(overlayInstances);
      for (const entry of overlayInstances) scene.remove(entry.instance.object);
    }
  }

  disposeInstances(baseInstances);
  for (const entry of baseInstances) scene.remove(entry.instance.object);
  renderer.dispose();

  return {
    version: RENDER_VERSION,
    compositionId,
    widthPx: renderWidth,
    heightPx: renderHeight,
    bounds: viewBounds,
    baseDataUrl,
    overlays,
  };
}

const inflight = new Map<string, Promise<Main2DRenderManifest>>();

export async function getOrCreateMain2DRenderManifest(args: {
  compositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  maximumRenderSize?: number;
}): Promise<Main2DRenderManifest> {
  const signatureInput = stableStringify(buildSignatureElements(args.elements));
  const signature = (await sha256Hex(signatureInput)).slice(0, 24);
  const prefix = manifestFilePrefix(args.compositionId, signature);
  const manifestUrl = `/files/${encodeURIComponent(RENDER_DIR_ID)}/${encodeURIComponent(`${prefix}_manifest.json`)}`;

  const inflightKey = `${args.compositionId}:${signature}`;
  const existing = inflight.get(inflightKey);
  if (existing) return existing;

  const promise = (async () => {
    const existingManifest = await fetchJson<Main2DRenderManifest>(manifestUrl);
    if (existingManifest) return existingManifest;

    const maximumRenderSize = Math.max(512, Math.min(8192, args.maximumRenderSize ?? 4096));
    const captured = await captureBaseAndOverlays({
      compositionId: args.compositionId,
      elements: args.elements,
      elementTypesById: args.elementTypesById,
      maximumRenderSize,
    });

    const baseFilename = `${prefix}_base.png`;
    const overlays: Main2DOverlayManifest[] = [];

    const baseBlob = await dataUrlToBlob(captured.baseDataUrl);
    const baseUpload = await uploadBlob(RENDER_DIR_ID, baseFilename, baseBlob);

    for (const overlay of captured.overlays) {
      const filename =
        overlay.kind === "lamp"
          ? `${prefix}_ha_${overlay.elementId}_lamp.png`
          : `${prefix}_ha_${overlay.elementId}_airflow_${overlay.mode}.png`;
      const blob = await dataUrlToBlob(overlay.dataUrl);
      const uploaded = await uploadBlob(RENDER_DIR_ID, filename, blob);
      overlays.push(overlay.kind === "lamp" ? { elementId: overlay.elementId, kind: "lamp", url: uploaded.url } : { elementId: overlay.elementId, kind: "airflow", mode: overlay.mode, url: uploaded.url });
    }

    const manifest: Main2DRenderManifest = {
      version: captured.version,
      compositionId: captured.compositionId,
      signature,
      widthPx: captured.widthPx,
      heightPx: captured.heightPx,
      bounds: captured.bounds,
      base: { url: baseUpload.url },
      overlays,
    };

    const manifestFilename = `${prefix}_manifest.json`;
    const manifestBlob = new Blob([JSON.stringify(manifest)], { type: "application/json" });
    await uploadBlob(RENDER_DIR_ID, manifestFilename, manifestBlob);

    return manifest;
  })();

  inflight.set(inflightKey, promise);
  try {
    return await promise;
  } finally {
    inflight.delete(inflightKey);
  }
}
