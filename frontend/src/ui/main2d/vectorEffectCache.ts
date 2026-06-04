import {
  resolveToposyncUrl,
  type BoundsXZ,
  type CompositionElement,
  type Element3DInstance,
  type ElementType,
  type Main2DEffectBlendMode,
  type Main2DEffectTarget,
  type ViewSettings,
} from "@toposync/plugin-api";
import * as THREE from "three";

import {
  buildMain2DSignatureElements,
  stableStringify,
  sha256Hex,
} from "./shared";
import {
  computeMain2DEffectDeltaCrop,
  type Main2DEffectDeltaCrop,
  type Main2DEffectPixelBuffer,
} from "./effectDelta";

export { computeMain2DEffectDeltaCrop };
export type { Main2DEffectPixelBuffer };

const EFFECT_RENDER_DIR_ID = "render2d-effects";
const EFFECT_RENDER_VERSION = 2 as const;

export type Main2DEffectOverlayManifest = {
  id: string;
  url: string;
  x: number;
  z: number;
  width: number;
  height: number;
  blendMode: Main2DEffectBlendMode;
};

export type Main2DEffectRenderManifest = {
  version: typeof EFFECT_RENDER_VERSION;
  compositionId: string;
  signature: string;
  bounds: BoundsXZ;
  widthPx: number;
  heightPx: number;
  effects: Main2DEffectOverlayManifest[];
};

type UploadFileResponse = {
  dir: string;
  path: string;
  url: string;
  filename: string;
  content_type: string | null;
  size_bytes: number;
};

function createRenderer(renderWidth: number, renderHeight: number): THREE.WebGLRenderer {
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: true,
    stencil: true,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(renderWidth, renderHeight, false);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  return renderer;
}

function buildCamera(bounds: BoundsXZ, renderWidth: number, renderHeight: number): THREE.OrthographicCamera {
  const viewWidth = Math.max(1e-6, bounds.maxX - bounds.minX);
  const viewHeight = Math.max(1e-6, bounds.maxZ - bounds.minZ);
  const camera = new THREE.OrthographicCamera(-viewWidth / 2, viewWidth / 2, viewHeight / 2, -viewHeight / 2, 0.01, Math.max(viewWidth, viewHeight) * 10);
  const centerX = (bounds.minX + bounds.maxX) / 2;
  const centerZ = (bounds.minZ + bounds.maxZ) / 2;
  camera.position.set(centerX, Math.max(viewWidth, viewHeight), centerZ);
  camera.up.set(0, 0, -1);
  camera.lookAt(new THREE.Vector3(centerX, 0, centerZ));
  camera.updateProjectionMatrix();
  return camera;
}

function addDefaultLights(scene: THREE.Scene): void {
  const ambient = new THREE.AmbientLight(0xffffff, 0.55);
  scene.add(ambient);
  const directional = new THREE.DirectionalLight(0xffffff, 0.85);
  directional.position.set(2.2, 6, 3);
  scene.add(directional);
}

function renderSizeForBounds(bounds: BoundsXZ, maximumRenderSize: number): { width: number; height: number } {
  const spanX = Math.max(1e-6, bounds.maxX - bounds.minX);
  const spanZ = Math.max(1e-6, bounds.maxZ - bounds.minZ);
  const longest = Math.max(spanX, spanZ);
  const scale = maximumRenderSize / longest;
  return {
    width: Math.max(96, Math.round(spanX * scale)),
    height: Math.max(96, Math.round(spanZ * scale)),
  };
}

function hideNonLightRenderables(object: THREE.Object3D): void {
  object.traverse((node) => {
    const anyNode = node as any;
    if (anyNode?.isLight) return;
    if (anyNode?.isMesh || anyNode?.isLine || anyNode?.isPoints || anyNode?.isSprite) anyNode.visible = false;
  });
}

function createInstances(args: {
  scene: THREE.Scene;
  camera: THREE.Camera;
  renderer: THREE.WebGLRenderer;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  view: ViewSettings;
  compositionId: string;
  hideElementIds?: Set<string>;
}): Array<{ element: CompositionElement; instance: Element3DInstance }> {
  const out: Array<{ element: CompositionElement; instance: Element3DInstance }> = [];
  for (const element of args.elements) {
    const def = args.elementTypesById[element.type];
    if (!def?.create3D) continue;
    const instance = def.create3D(
      {
        THREE,
        scene: args.scene,
        camera: args.camera,
        renderer: args.renderer,
        view: args.view,
        elements: args.elements,
        compositionId: args.compositionId,
      },
      element,
    );
    instance.object.position.set(element.position.x, element.position.y, element.position.z);
    instance.object.rotation.set(element.rotation.x, element.rotation.y, element.rotation.z);
    args.scene.add(instance.object);
    if (args.hideElementIds?.has(element.id)) hideNonLightRenderables(instance.object);
    out.push({ element, instance });
  }
  return out;
}

function disposeInstances(scene: THREE.Scene, instances: Array<{ instance: Element3DInstance }>): void {
  for (const entry of instances) {
    scene.remove(entry.instance.object);
    try {
      entry.instance.dispose?.();
    } catch {
      // ignore
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function warmupCapture(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.Camera,
  instances: Array<{ instance: Element3DInstance }>,
  options: { warmupSeconds: number; maxWaitMs: number; stepMs: number },
): Promise<void> {
  const started = performance.now();
  let simulated = 0;
  while (true) {
    const dt = Math.min(0.05, options.stepMs / 1000);
    for (const entry of instances) entry.instance.tick?.(dt);
    renderer.render(scene, camera);
    simulated += dt;
    if (simulated >= options.warmupSeconds || performance.now() - started >= options.maxWaitMs) return;
    await sleep(options.stepMs);
  }
}

function captureImageData(renderer: THREE.WebGLRenderer, width: number, height: number): ImageData | null {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(renderer.domElement, 0, 0, width, height);
  return ctx.getImageData(0, 0, width, height);
}

type EffectDeltaWorkerResponse =
  | {
      id: number;
      ok: true;
      delta: {
        width: number;
        height: number;
        data: ArrayBuffer;
        crop: { x: number; y: number; width: number; height: number };
      } | null;
    }
  | { id: number; ok: false; error: string };

type PendingEffectDeltaRequest = {
  resolve: (delta: Main2DEffectDeltaCrop | null) => void;
  reject: (err: Error) => void;
  timeout: number;
};

let effectDeltaWorker: Worker | null = null;
let effectDeltaWorkerFailed = false;
let effectDeltaWorkerRequestId = 0;
const pendingEffectDeltaRequests = new Map<number, PendingEffectDeltaRequest>();

function clearPendingEffectDeltaRequests(err: Error): void {
  for (const pending of pendingEffectDeltaRequests.values()) {
    window.clearTimeout(pending.timeout);
    pending.reject(err);
  }
  pendingEffectDeltaRequests.clear();
}

function getEffectDeltaWorker(): Worker | null {
  if (effectDeltaWorkerFailed) return null;
  if (effectDeltaWorker) return effectDeltaWorker;

  try {
    effectDeltaWorker = new Worker(new URL("./effectDelta.worker.ts", import.meta.url), { type: "module" });
    effectDeltaWorker.addEventListener("message", (event: MessageEvent<EffectDeltaWorkerResponse>) => {
      const response = event.data;
      const pending = pendingEffectDeltaRequests.get(response.id);
      if (!pending) return;
      pendingEffectDeltaRequests.delete(response.id);
      window.clearTimeout(pending.timeout);
      if (!response.ok) {
        pending.reject(new Error(response.error));
        return;
      }
      if (!response.delta) {
        pending.resolve(null);
        return;
      }
      pending.resolve({
        data: new Uint8ClampedArray(response.delta.data),
        crop: response.delta.crop,
      });
    });
    effectDeltaWorker.addEventListener("error", () => {
      effectDeltaWorkerFailed = true;
      effectDeltaWorker?.terminate();
      effectDeltaWorker = null;
      clearPendingEffectDeltaRequests(new Error("Effect delta worker failed"));
    });
    return effectDeltaWorker;
  } catch (err) {
    effectDeltaWorkerFailed = true;
    console.warn("Effect delta worker unavailable; falling back to main thread", err);
    return null;
  }
}

async function computeMain2DEffectDeltaCropAsync(
  base: ImageData,
  active: ImageData,
  blendMode: Main2DEffectBlendMode,
): Promise<Main2DEffectDeltaCrop | null> {
  const worker = getEffectDeltaWorker();
  if (!worker) {
    return computeMain2DEffectDeltaCrop(base, active, { blendMode });
  }

  const id = ++effectDeltaWorkerRequestId;
  const baseCopy = new Uint8ClampedArray(base.data);
  const activeCopy = new Uint8ClampedArray(active.data);
  const baseBuffer = baseCopy.buffer as ArrayBuffer;
  const activeBuffer = activeCopy.buffer as ArrayBuffer;

  return new Promise<Main2DEffectDeltaCrop | null>((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pendingEffectDeltaRequests.delete(id);
      reject(new Error("Effect delta worker timed out"));
    }, 8000);
    pendingEffectDeltaRequests.set(id, { resolve, reject, timeout });
    worker.postMessage(
      {
        id,
        width: base.width,
        height: base.height,
        blendMode,
        base: baseBuffer,
        active: activeBuffer,
      },
      [baseBuffer, activeBuffer],
    );
  }).catch((err) => {
    console.warn("Effect delta worker failed; falling back to main thread", err);
    return computeMain2DEffectDeltaCrop(base, active, { blendMode });
  });
}

async function diffAndCrop(
  base: ImageData,
  active: ImageData,
  blendMode: Main2DEffectBlendMode,
): Promise<{ canvas: HTMLCanvasElement; crop: { x: number; y: number; width: number; height: number } } | null> {
  const delta = await computeMain2DEffectDeltaCropAsync(base, active, blendMode);
  if (!delta) return null;

  const fullCanvas = document.createElement("canvas");
  fullCanvas.width = base.width;
  fullCanvas.height = base.height;
  const fullCtx = fullCanvas.getContext("2d");
  if (!fullCtx) return null;
  fullCtx.putImageData(new ImageData(delta.data as unknown as ImageDataArray, base.width, base.height), 0, 0);

  const cropCanvas = document.createElement("canvas");
  cropCanvas.width = delta.crop.width;
  cropCanvas.height = delta.crop.height;
  const cropCtx = cropCanvas.getContext("2d");
  if (!cropCtx) return null;
  cropCtx.drawImage(
    fullCanvas,
    delta.crop.x,
    delta.crop.y,
    delta.crop.width,
    delta.crop.height,
    0,
    0,
    delta.crop.width,
    delta.crop.height,
  );
  return { canvas: cropCanvas, crop: delta.crop };
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("Failed to encode effect overlay"));
    }, "image/png");
  });
}

async function uploadBlob(dir: string, filename: string, blob: Blob): Promise<UploadFileResponse> {
  const form = new FormData();
  form.append("dir", dir);
  form.append("filename", filename);
  form.append("file", blob, filename);
  const response = await fetch("/api/files/upload", { method: "POST", body: form });
  if (!response.ok) throw new Error(`Failed to upload ${filename}: ${response.status}`);
  const data = (await response.json()) as UploadFileResponse;
  return { ...data, url: resolveToposyncUrl(data.url) };
}

async function fetchJson<T>(url: string): Promise<T | null> {
  const response = await fetch(url);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Failed to fetch ${url}: ${response.status}`);
  return response.json();
}

async function fileExists(path: string): Promise<boolean> {
  const response = await fetch(`/api/files/exists?path=${encodeURIComponent(path)}`);
  if (!response.ok) throw new Error(`Failed to check ${path}: ${response.status}`);
  const data = (await response.json().catch(() => null)) as { exists?: unknown } | null;
  return Boolean(data && data.exists === true);
}

function manifestFilePrefix(compositionId: string, signature: string): string {
  return `vector2d_effects_v${EFFECT_RENDER_VERSION}_${compositionId}_${signature}`;
}

function environmentElementsForEffects(
  elements: CompositionElement[],
  elementTypesById: Record<string, ElementType>,
  targetElementIds: Set<string>,
): CompositionElement[] {
  return elements.filter((element) => {
    if (targetElementIds.has(element.id)) return false;
    const def = elementTypesById[element.type];
    if (!def?.create3D) return false;
    if (!def.getMain2DEffectTargets) return true;
    try {
      return def.getMain2DEffectTargets({ element, elements }).length === 0;
    } catch {
      return true;
    }
  });
}

async function captureEffectOverlay(args: {
  compositionId: string;
  bounds: BoundsXZ;
  renderWidth: number;
  renderHeight: number;
  environmentElements: CompositionElement[];
  target: Main2DEffectTarget;
  elementTypesById: Record<string, ElementType>;
}): Promise<Omit<Main2DEffectOverlayManifest, "url"> & { blob: Blob } | null> {
  const view: ViewSettings = {
    wallHeightPreset: "high",
    wallHeight: 2.7,
    ghostWalls: true,
    graphicsQuality: "detailed",
  };
  const renderer = createRenderer(args.renderWidth, args.renderHeight);
  const camera = buildCamera(args.bounds, args.renderWidth, args.renderHeight);
  const renderScene = async (targetElement: CompositionElement | null): Promise<ImageData | null> => {
    const scene = new THREE.Scene();
    addDefaultLights(scene);
    const elements = targetElement ? [...args.environmentElements, targetElement] : args.environmentElements;
    const hideIds = args.target.hideNonLightRenderables && targetElement ? new Set([targetElement.id]) : undefined;
    const instances = createInstances({
      scene,
      camera,
      renderer,
      elements,
      elementTypesById: args.elementTypesById,
      view,
      compositionId: args.compositionId,
      hideElementIds: hideIds,
    });
    try {
      await warmupCapture(renderer, scene, camera, instances, {
        warmupSeconds: args.target.warmupSeconds ?? 0.35,
        maxWaitMs: 5000,
        stepMs: 50,
      });
      renderer.render(scene, camera);
      return captureImageData(renderer, args.renderWidth, args.renderHeight);
    } finally {
      disposeInstances(scene, instances);
    }
  };

  try {
    const baseData = await renderScene(args.target.baseElement ?? null);
    const activeData = await renderScene(args.target.element);
    if (!baseData || !activeData) return null;

    const blendMode = args.target.blendMode ?? "source-over";
    const diff = await diffAndCrop(baseData, activeData, blendMode);
    if (!diff) return null;

    const spanX = Math.max(1e-6, args.bounds.maxX - args.bounds.minX);
    const spanZ = Math.max(1e-6, args.bounds.maxZ - args.bounds.minZ);
    const x = args.bounds.minX + (diff.crop.x / args.renderWidth) * spanX;
    const z = args.bounds.minZ + (diff.crop.y / args.renderHeight) * spanZ;
    const width = (diff.crop.width / args.renderWidth) * spanX;
    const height = (diff.crop.height / args.renderHeight) * spanZ;
    const blob = await canvasToBlob(diff.canvas);
    return { id: args.target.id, x, z, width, height, blendMode, blob };
  } finally {
    renderer.dispose();
  }
}

const inflight = new Map<string, Promise<Main2DEffectRenderManifest>>();

export async function getOrCreateMain2DEffectManifest(args: {
  compositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  bounds: BoundsXZ;
  targets: Main2DEffectTarget[];
  maximumRenderSize?: number;
}): Promise<Main2DEffectRenderManifest> {
  const targets = args.targets;
  const targetElementIds = new Set(targets.map((target) => target.element.id));
  const environmentElements = environmentElementsForEffects(args.elements, args.elementTypesById, targetElementIds);
  const signatureInput = stableStringify({
    environment: buildMain2DSignatureElements(environmentElements),
    bounds: args.bounds,
    targets: targets
      .map((target) => ({
        id: target.id,
        element: target.element,
        baseElement: target.baseElement ?? null,
        signature: target.signature ?? null,
        warmupSeconds: target.warmupSeconds ?? null,
        hideNonLightRenderables: Boolean(target.hideNonLightRenderables),
        blendMode: target.blendMode ?? "source-over",
      }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  });
  const signature = (await sha256Hex(signatureInput)).slice(0, 24);
  const prefix = manifestFilePrefix(args.compositionId, signature);
  const manifestFilename = `${prefix}_manifest.json`;
  const manifestPath = `${EFFECT_RENDER_DIR_ID}/${manifestFilename}`;
  const manifestUrl = `/files/${encodeURIComponent(EFFECT_RENDER_DIR_ID)}/${encodeURIComponent(manifestFilename)}`;
  const inflightKey = `${args.compositionId}:${signature}`;
  const existing = inflight.get(inflightKey);
  if (existing) return existing;

  const promise = (async () => {
    if (await fileExists(manifestPath)) {
      const existingManifest = await fetchJson<Main2DEffectRenderManifest>(manifestUrl);
      if (existingManifest) return existingManifest;
    }

    const maximumRenderSize = Math.max(512, Math.min(4096, args.maximumRenderSize ?? 2048));
    const { width: renderWidth, height: renderHeight } = renderSizeForBounds(args.bounds, maximumRenderSize);
    const effects: Main2DEffectOverlayManifest[] = [];

    for (const target of targets) {
      const captured = await captureEffectOverlay({
        compositionId: args.compositionId,
        bounds: args.bounds,
        renderWidth,
        renderHeight,
        environmentElements,
        target,
        elementTypesById: args.elementTypesById,
      });
      if (!captured) continue;
      const filename = `${prefix}_${target.id.replace(/[^a-zA-Z0-9_.-]/g, "_")}.png`;
      const uploaded = await uploadBlob(EFFECT_RENDER_DIR_ID, filename, captured.blob);
      effects.push({
        id: captured.id,
        url: uploaded.url,
        x: captured.x,
        z: captured.z,
        width: captured.width,
        height: captured.height,
        blendMode: captured.blendMode,
      });
    }

    const manifest: Main2DEffectRenderManifest = {
      version: EFFECT_RENDER_VERSION,
      compositionId: args.compositionId,
      signature,
      bounds: args.bounds,
      widthPx: renderWidth,
      heightPx: renderHeight,
      effects,
    };

    const manifestBlob = new Blob([JSON.stringify(manifest)], { type: "application/json" });
    await uploadBlob(EFFECT_RENDER_DIR_ID, manifestFilename, manifestBlob);
    return manifest;
  })();

  inflight.set(inflightKey, promise);
  try {
    return await promise;
  } finally {
    inflight.delete(inflightKey);
  }
}
