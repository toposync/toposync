import React, { useMemo } from "react";
import * as THREEStandalone from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  ElementType,
  HostI18n,
  PlanePoint,
  TopoSyncHost,
} from "@toposync/plugin-api";

const MODEL_TYPE = "com.toposync.models.gltf";

const MIN_SCALE = 0.001;
const MAX_SCALE = 1000;

type Vector3 = { x: number; y: number; z: number };

const DEBUG_MODELS = (() => {
  try {
    return localStorage.getItem("toposync.debug.models") === "1";
  } catch {
    return false;
  }
})();

function dbg(...args: unknown[]): void {
  if (DEBUG_MODELS) console.log(...args);
}

type UploadFileResponse = {
  dir: string;
  path: string;
  url: string;
  filename: string;
  content_type?: string | null;
  size_bytes: number;
};

type ModelPreviewResult = {
  dataUrl: string;
  widthPx: number;
  heightPx: number;
  size: Vector3;
  center: Vector3;
  minY: number;
};

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(translations);
  host.registerElementType(modelElementType(host.i18n));
  host.registerEditorTool(importModelTool(host.i18n));
}

const translations = {
  en: {
    "ext.models.element.name": "3D Model",
    "ext.models.element.desc": "GLB/GLTF model placed in the scene.",
    "ext.models.tool.import": "Import 3D model",
    "ext.models.tool.hint": "Click to upload and place a model.",
    "ext.models.editor.file": "File",
    "ext.models.editor.size": "Size",
    "ext.models.editor.preview": "Preview",
    "ext.models.editor.scale": "Scale",
    "ext.models.editor.height": "Height",
    "ext.models.editor.height.floor": "Floor",
    "ext.models.editor.height.mid": "Mid",
    "ext.models.editor.height.ceiling": "Ceiling",
    "ext.models.editor.uploading": "Uploading…",
    "ext.models.editor.processing": "Processing preview…",
    "ext.models.editor.failed": "Failed to import model",
    "ext.models.error.pick_entry": "Pick at least one .glb or .gltf file",
  },
  "pt-BR": {
    "ext.models.element.name": "Modelo 3D",
    "ext.models.element.desc": "Modelo GLB/GLTF posicionado na cena.",
    "ext.models.tool.import": "Importar modelo 3D",
    "ext.models.tool.hint": "Clique para enviar e posicionar um modelo.",
    "ext.models.editor.file": "Arquivo",
    "ext.models.editor.size": "Tamanho",
    "ext.models.editor.preview": "Prévia",
    "ext.models.editor.scale": "Escala",
    "ext.models.editor.height": "Altura",
    "ext.models.editor.height.floor": "Chão",
    "ext.models.editor.height.mid": "Meio",
    "ext.models.editor.height.ceiling": "Teto",
    "ext.models.editor.uploading": "Enviando...",
    "ext.models.editor.processing": "Processando prévia...",
    "ext.models.editor.failed": "Falha ao importar modelo",
    "ext.models.error.pick_entry": "Selecione pelo menos um arquivo .glb ou .gltf",
  },
} as const;

function readNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function readString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function readVector3(v: unknown, fallback: Vector3): Vector3 {
  if (!v || typeof v !== "object" || Array.isArray(v)) return fallback;
  const rec = v as Record<string, unknown>;
  return {
    x: readNumber(rec.x, fallback.x),
    y: readNumber(rec.y, fallback.y),
    z: readNumber(rec.z, fallback.z),
  };
}

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

function readScale(v: unknown, fallback = 1): number {
  return clamp(readNumber(v, fallback), MIN_SCALE, MAX_SCALE);
}

function stripFileExtension(filename: string): string {
  const idx = filename.lastIndexOf(".");
  if (idx <= 0) return filename;
  return filename.slice(0, idx);
}

function suggestInitialScale(size: Vector3): number {
  const maxDim = Math.max(Math.abs(size.x), Math.abs(size.y), Math.abs(size.z));
  if (!Number.isFinite(maxDim) || maxDim <= 1e-6) return 1;

  // Only normalize obviously-wrong sizes; keep realistic ones untouched.
  const minTarget = 0.25; // 25cm

  const oversizedThreshold = 3; // 3m => likely wrong scale
  const oversizedTarget = 1; // normalize to 1m on the largest axis

  if (maxDim > oversizedThreshold) return clamp(oversizedTarget / maxDim, MIN_SCALE, MAX_SCALE);
  if (maxDim < minTarget) return clamp(minTarget / maxDim, MIN_SCALE, MAX_SCALE);
  return 1;
}

async function uploadToFilesDir(file: Blob, opts: { dir?: string; filename: string }): Promise<UploadFileResponse> {
  const form = new FormData();
  form.append("file", file, opts.filename);
  if (opts.dir) form.append("dir", opts.dir);
  form.append("filename", opts.filename);

  dbg("[models:tool] POST /api/files/upload", { dir: opts.dir ?? null, filename: opts.filename, size: file.size });
  const res = await fetch("/api/files/upload", { method: "POST", body: form });
  dbg("[models:tool] upload response", { status: res.status, ok: res.ok });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

function createRenderer(width: number, height: number): THREEStandalone.WebGLRenderer {
  const renderer = new THREEStandalone.WebGLRenderer({
    antialias: true,
    alpha: true,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(width, height, false);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREEStandalone.SRGBColorSpace;
  return renderer;
}

function buildCamera(viewWidth: number, viewHeight: number): THREEStandalone.OrthographicCamera {
  const camera = new THREEStandalone.OrthographicCamera(
    -viewWidth / 2,
    viewWidth / 2,
    viewHeight / 2,
    -viewHeight / 2,
    0.01,
    Math.max(viewWidth, viewHeight) * 8,
  );
  camera.position.set(0, Math.max(viewWidth, viewHeight), 0);
  camera.up.set(0, 0, -1);
  camera.lookAt(new THREEStandalone.Vector3(0, 0, 0));
  return camera;
}

function addLights(scene: THREEStandalone.Scene) {
  const hemi = new THREEStandalone.HemisphereLight(0xffffff, 0x222233, 0.9);
  scene.add(hemi);
  const dir = new THREEStandalone.DirectionalLight(0xffffff, 0.8);
  dir.position.set(3, 6, 3);
  dir.castShadow = false;
  scene.add(dir);
}

async function generateModelTopDownPreview(
  modelUrl: string,
  opts?: { renderSize?: number; paddingRatio?: number },
): Promise<ModelPreviewResult> {
  const targetMax = Math.max(128, Math.min(1024, opts?.renderSize ?? 640));
  const paddingRatio = clamp(opts?.paddingRatio ?? 0.08, 0, 0.5);

  const loader = new GLTFLoader();
  const gltf = await loader.loadAsync(modelUrl);
  const model = gltf.scene || gltf.scenes?.[0];
  if (!model) throw new Error("Empty model");

  const working = model.clone(true);
  working.updateMatrixWorld(true);

  const bbox = new THREEStandalone.Box3().setFromObject(working);
  const sizeVec = bbox.getSize(new THREEStandalone.Vector3());
  const centerVec = bbox.getCenter(new THREEStandalone.Vector3());
  const minY = bbox.min.y;
  if (!Number.isFinite(sizeVec.x) || !Number.isFinite(sizeVec.y) || !Number.isFinite(sizeVec.z) || sizeVec.length() < 1e-6) {
    throw new Error("Could not compute model size");
  }

  working.position.sub(new THREEStandalone.Vector3(centerVec.x, minY, centerVec.z));

  const safeX = Math.max(sizeVec.x, 1e-6);
  const safeZ = Math.max(sizeVec.z, 1e-6);
  const viewWidth = safeX * (1 + paddingRatio * 2);
  const viewHeight = safeZ * (1 + paddingRatio * 2);

  const scale = targetMax / Math.max(viewWidth, viewHeight);
  const renderWidth = Math.max(64, Math.round(viewWidth * scale));
  const renderHeight = Math.max(64, Math.round(viewHeight * scale));

  const camera = buildCamera(viewWidth || 1, viewHeight || 1);
  const renderer = createRenderer(renderWidth, renderHeight);
  const scene = new THREEStandalone.Scene();
  addLights(scene);
  scene.add(working);

  renderer.render(scene, camera);

  const cropScaleX = safeX / Math.max(viewWidth, 1e-9);
  const cropScaleZ = safeZ / Math.max(viewHeight, 1e-9);
  const cropWidth = Math.max(1, Math.min(renderWidth, Math.round(renderWidth * cropScaleX)));
  const cropHeight = Math.max(1, Math.min(renderHeight, Math.round(renderHeight * cropScaleZ)));
  const cropX = Math.max(0, Math.min(renderWidth - cropWidth, Math.round((renderWidth - cropWidth) / 2)));
  const cropY = Math.max(0, Math.min(renderHeight - cropHeight, Math.round((renderHeight - cropHeight) / 2)));

  let outWidth = renderWidth;
  let outHeight = renderHeight;
  let dataUrl = "";
  if (cropWidth !== renderWidth || cropHeight !== renderHeight) {
    const outCanvas = document.createElement("canvas");
    outCanvas.width = cropWidth;
    outCanvas.height = cropHeight;
    const ctx = outCanvas.getContext("2d");
    if (ctx) {
      ctx.drawImage(renderer.domElement, cropX, cropY, cropWidth, cropHeight, 0, 0, cropWidth, cropHeight);
      dataUrl = outCanvas.toDataURL("image/png");
      outWidth = cropWidth;
      outHeight = cropHeight;
    }
  }
  if (!dataUrl) dataUrl = renderer.domElement.toDataURL("image/png");

  try {
    renderer.dispose();
  } catch {
    // ignore
  }

  return {
    dataUrl,
    widthPx: outWidth,
    heightPx: outHeight,
    size: { x: sizeVec.x, y: sizeVec.y, z: sizeVec.z },
    center: { x: centerVec.x, y: centerVec.y, z: centerVec.z },
    minY,
  };
}

function modelElementType(i18n: HostI18n): ElementType {
  const imageCache = new Map<string, HTMLImageElement>();

  function getPreviewUrl(element: CompositionElement): string | null {
    const dir = readString((element.props as any).dir, "");
    const preview = readString((element.props as any).preview, "");
    if (!dir || !preview) return null;
    return `/files/${encodeURIComponent(dir)}/${encodeURIComponent(preview)}`;
  }

  return {
    type: MODEL_TYPE,
    layerGroup: "objects",
    placeable: false,
    name: { key: "ext.models.element.name", fallback: "3D Model" },
    description: { key: "ext.models.element.desc", fallback: "GLB/GLTF model placed in the scene." },
    defaultProps: {
      dir: "",
      model: "",
      preview: "",
      size: { x: 1, y: 1, z: 1 },
      center: { x: 0, y: 0, z: 0 },
      min_y: 0,
      scale: 1,
    },
    create3D: ({ THREE }, element) => {
      const group = new THREE.Group();

      const loader = new GLTFLoader();
      let disposed = false;
      let lastUrl = "";
      let current: THREEStandalone.Object3D | null = null;
      let mixer: THREEStandalone.AnimationMixer | null = null;
      let token = 0;

      function disposeObject(obj: THREEStandalone.Object3D) {
        obj.traverse((child) => {
          const mesh = child as any;
          if (mesh.geometry?.dispose) mesh.geometry.dispose();
          const mat = mesh.material;
          if (Array.isArray(mat)) mat.forEach((m) => m?.dispose?.());
          else mat?.dispose?.();
        });
      }

      function disposeMixer() {
        if (!mixer) return;
        try {
          mixer.stopAllAction();
        } catch {
          // ignore
        }
        if (current) {
          try {
            mixer.uncacheRoot(current);
          } catch {
            // ignore
          }
        }
        mixer = null;
      }

      async function load(url: string, meta: { center: Vector3; minY: number } | null) {
        const myToken = ++token;
        try {
          const gltf = await loader.loadAsync(url);
          if (disposed || myToken !== token) return;

          const model = gltf.scene || gltf.scenes?.[0];
          if (!model) throw new Error("Empty model");

          model.traverse((obj) => {
            const mesh = obj as any;
            if (mesh.isMesh) {
              mesh.castShadow = true;
              mesh.receiveShadow = true;
            }
          });

          const bbox = new THREE.Box3().setFromObject(model);
          const centerVec = bbox.getCenter(new THREE.Vector3());
          const minY = bbox.min.y;
          const center = meta?.center ?? { x: centerVec.x, y: centerVec.y, z: centerVec.z };
          const lift = meta?.minY ?? minY;

          model.position.sub(new THREE.Vector3(center.x, lift, center.z));

          if (current) {
            group.remove(current);
            disposeMixer();
            disposeObject(current);
            current = null;
          }
          current = model;
          group.add(model);

          if (Array.isArray(gltf.animations) && gltf.animations.length > 0) {
            mixer = new THREE.AnimationMixer(model);
            for (const clip of gltf.animations) mixer.clipAction(clip).play();
          }
        } catch (err) {
          console.error(`[models:create3D]`, err);
        }
      }

      function apply(el: CompositionElement) {
        const dir = readString((el.props as any).dir, "");
        const model = readString((el.props as any).model, "");
        const scale = readScale((el.props as any).scale, 1);
        const center = readVector3((el.props as any).center, { x: 0, y: 0, z: 0 });
        const minY = readNumber((el.props as any).min_y, 0);

        group.scale.setScalar(scale);

        const url = dir && model ? `/files/${encodeURIComponent(dir)}/${encodeURIComponent(model)}` : "";
        if (url && url !== lastUrl) {
          lastUrl = url;
          void load(url, { center, minY });
        }
      }

      apply(element);

      return {
        object: group,
        update: apply,
        tick: (dt) => {
          mixer?.update(dt);
        },
        dispose: () => {
          disposed = true;
          disposeMixer();
          if (current) disposeObject(current);
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
      const scale = readScale((element.props as any).scale, 1);
      const previewUrl = getPreviewUrl(element);
      const rotationY = readNumber((element.rotation as any)?.y, 0);

      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const w = Math.max(20, size.x * scale * viewport.scale);
      const h = Math.max(20, size.z * scale * viewport.scale);

      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.rotate(-rotationY);

      if (previewUrl) {
        let img = imageCache.get(previewUrl) ?? null;
        if (!img) {
          img = new Image();
          img.decoding = "async";
          img.onload = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
          img.onerror = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
          img.src = previewUrl;
          imageCache.set(previewUrl, img);
        }

        if (img.complete && img.naturalWidth > 0) {
          ctx.globalAlpha = 0.94;
          ctx.drawImage(img, -w / 2, -h / 2, w, h);
          ctx.globalAlpha = 1;
        } else {
          ctx.fillStyle = "rgba(56,189,248,0.10)";
          ctx.fillRect(-w / 2, -h / 2, w, h);
        }
      } else {
        ctx.fillStyle = "rgba(56,189,248,0.10)";
        ctx.fillRect(-w / 2, -h / 2, w, h);
      }

      ctx.strokeStyle = "rgba(230,232,242,0.22)";
      ctx.lineWidth = 2;
      ctx.strokeRect(-w / 2, -h / 2, w, h);
      ctx.restore();
    },
    hitTest2D: ({ element, world }) => {
      const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
      const scale = readScale((element.props as any).scale, 1);
      const angle = readNumber((element.rotation as any)?.y, 0);
      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      const localX = dx * cos - dz * sin;
      const localZ = dx * sin + dz * cos;
      return Math.abs(localX) <= (size.x * scale) / 2 && Math.abs(localZ) <= (size.z * scale) / 2;
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <ModelEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

type EditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function ModelEditor({ element, update, remove, close, i18n }: EditorProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const numberFmt = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  const dir = readString((element.props as any).dir, "");
  const model = readString((element.props as any).model, "");
  const preview = readString((element.props as any).preview, "");
  const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
  const scale = readScale((element.props as any).scale, 1);
  const heightY = readNumber((element.position as any).y, 0);

  const previewUrl = dir && preview ? `/files/${encodeURIComponent(dir)}/${encodeURIComponent(preview)}` : "";
  const finalSize = useMemo(
    () => ({ x: size.x * scale, y: size.y * scale, z: size.z * scale }),
    [scale, size.x, size.y, size.z],
  );

  return (
    <div>
      <div className="field">
        <div className="label">{t("core.element_editor.name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.models.editor.file")}</div>
          <input className="input" value={model || "-"} readOnly />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.models.editor.size")}</div>
          <input
            className="input"
            value={`${numberFmt.format(finalSize.x)} × ${numberFmt.format(finalSize.y)} × ${numberFmt.format(finalSize.z)} m`}
            readOnly
          />
        </div>
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.models.editor.scale")}</div>
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={MIN_SCALE}
            max={MAX_SCALE}
            step={0.01}
            value={scale}
            onChange={(e) => {
              const next = Number.parseFloat(e.target.value);
              if (!Number.isFinite(next)) return;
              update({ props: { scale: clamp(next, MIN_SCALE, MAX_SCALE) } });
            }}
          />
        </div>
      </div>

      <div className="field">
        <div className="label">
          {t("ext.models.editor.height")}: {numberFmt.format(heightY)} m
        </div>
        <div className="rowWrap">
          {(
            [
              { key: "floor", y: 0 },
              { key: "mid", y: 1.35 },
              { key: "ceiling", y: 2.7 },
            ] as const
          ).map((preset) => {
            const isActive = Math.abs(heightY - preset.y) < 0.01;
            return (
              <button
                key={preset.key}
                className={["chipButton", isActive ? "isActive" : ""].join(" ")}
                type="button"
                onClick={() => update({ position: { y: preset.y } })}
              >
                {t(`ext.models.editor.height.${preset.key}`)}
              </button>
            );
          })}
        </div>
        <input
          className="input"
          type="range"
          min={0}
          max={3}
          step={0.01}
          value={heightY}
          onChange={(e) => update({ position: { y: Number(e.target.value) } })}
        />
      </div>

      {previewUrl ? (
        <>
          <div className="sectionDivider" />
          <div className="card">
            <div className="cardHeaderRow">
              <div className="cardTitle">{t("ext.models.editor.preview")}</div>
              <div className="cardMeta">{dir}</div>
            </div>
            <div className="cardBody">
              <img
                src={previewUrl}
                alt={t("ext.models.editor.preview")}
                style={{ width: "100%", borderRadius: 12, border: "1px solid rgba(255,255,255,0.10)" }}
              />
            </div>
          </div>
        </>
      ) : null}

      <div className="sectionDivider" />
      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}

function importModelTool(i18n: HostI18n): EditorTool {
  return {
    id: "com.toposync.models.tool.import",
    name: { key: "ext.models.tool.import", fallback: "Import 3D model" },
    description: { key: "ext.models.tool.hint", fallback: "Click to upload and place a model." },
    icon: "cube",
    createSession: (ctx) => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.accept = ".glb,.gltf,.bin,.png,.jpg,.jpeg,.webp";
      input.style.position = "fixed";
      input.style.left = "-9999px";
      input.style.width = "1px";
      input.style.height = "1px";
      document.body.appendChild(input);

      let pendingAt: PlanePoint | null = null;
      let armed = false;
      let downScreen: { x: number; y: number } | null = null;
      let busyState: "idle" | "uploading" | "processing" = "idle";
      let lastError: string | null = null;
      let lastCanvas: HTMLCanvasElement | null = null;

      const t = i18n.t;

      function invalidate() {
        lastCanvas?.dispatchEvent(new Event("toposync:invalidate"));
      }

      dbg("[models:tool] session created");

      function drawPill(canvas: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
        const r = Math.min(999, Math.min(w, h) / 2);
        canvas.beginPath();
        canvas.moveTo(x + r, y);
        canvas.lineTo(x + w - r, y);
        canvas.quadraticCurveTo(x + w, y, x + w, y + r);
        canvas.lineTo(x + w, y + h - r);
        canvas.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        canvas.lineTo(x + r, y + h);
        canvas.quadraticCurveTo(x, y + h, x, y + h - r);
        canvas.lineTo(x, y + r);
        canvas.quadraticCurveTo(x, y, x + r, y);
        canvas.closePath();
      }

      async function handleFiles(list: File[]) {
        if (list.length === 0) return;

        const entry =
          list.find((f) => f.name.toLowerCase().endsWith(".glb")) ??
          list.find((f) => f.name.toLowerCase().endsWith(".gltf"));
        if (!entry) throw new Error(t("ext.models.error.pick_entry"));
        if (!pendingAt) throw new Error("No placement point selected");

        dbg(
          "[models:tool] handleFiles",
          list.map((f) => ({ name: f.name, size: f.size, type: f.type })),
        );

        busyState = "uploading";
        lastError = null;
        invalidate();

        dbg("[models:tool] uploading entry", { name: entry.name });
        const entryUpload = await uploadToFilesDir(entry, { filename: entry.name });
        const dir = entryUpload.dir;
        const entryName = entryUpload.filename;

        for (const f of list) {
          if (f === entry) continue;
          dbg("[models:tool] uploading asset", { dir, name: f.name });
          await uploadToFilesDir(f, { dir, filename: f.name });
        }

        const modelUrl = `/files/${encodeURIComponent(dir)}/${encodeURIComponent(entryName)}`;

        busyState = "processing";
        invalidate();
        dbg("[models:tool] generating preview", { modelUrl });
        const preview = await generateModelTopDownPreview(modelUrl);
        const previewBlob = await (await fetch(preview.dataUrl)).blob();
        const previewUpload = await uploadToFilesDir(previewBlob, { dir, filename: "preview.png" });
        dbg("[models:tool] preview uploaded", { url: previewUpload.url });
        const inferredName = stripFileExtension(entryName);
        const id = ctx.createElement(MODEL_TYPE, {
          name: inferredName,
          position: { x: pendingAt.x, y: 0, z: pendingAt.z },
          props: {
            dir,
            model: entryName,
            preview: previewUpload.filename,
            size: preview.size,
            center: preview.center,
            min_y: preview.minY,
            scale: suggestInitialScale(preview.size),
          },
        });
        if (id) ctx.openEditor(id);
        dbg("[models:tool] element created", { id });
      }

      input.addEventListener("change", () => {
        const list = input.files ? Array.from(input.files) : [];
        input.value = "";
        dbg("[models:tool] input change", { count: list.length, pendingAt });
        if (list.length === 0) {
          busyState = "idle";
          pendingAt = null;
          return;
        }

        void (async () => {
          try {
            await handleFiles(list);
            busyState = "idle";
            pendingAt = null;
            invalidate();
          } catch (err) {
            busyState = "idle";
            lastError = err instanceof Error ? err.message : String(err);
            console.error("[models:tool] import failed", err);
            invalidate();
          }
        })();
      });

      return {
        onPointerEvent: (evt) => {
          if (evt.kind === "cancel") {
            armed = false;
            downScreen = null;
            dbg("[models:tool] pointer cancel");
            return;
          }
          if (evt.kind === "down") {
            if (evt.button !== 0) return;
            if (busyState !== "idle") return;
            pendingAt = evt.world;
            armed = true;
            downScreen = { x: evt.screen.x, y: evt.screen.y };
            dbg("[models:tool] pointer down", { world: evt.world, screen: evt.screen });
            return;
          }
          if (evt.kind === "move") {
            if (!armed || !downScreen) return;
            const dx = evt.screen.x - downScreen.x;
            const dy = evt.screen.y - downScreen.y;
            if (dx * dx + dy * dy > 16) {
              armed = false;
              downScreen = null;
            }
            return;
          }
          if (evt.kind === "up") {
            if (!armed) return;
            armed = false;
            downScreen = null;
            if (evt.button !== 0) return;
            if (busyState !== "idle") return;
            if (!pendingAt) return;
            dbg("[models:tool] pointer up -> open picker", { pendingAt });
            input.click();
          }
        },
        onKeyDown: (e) => {
          if (e.key === "Escape") {
            pendingAt = null;
            armed = false;
            downScreen = null;
            busyState = "idle";
            lastError = null;
          }
        },
        renderOverlay2D: ({ ctx: canvas, viewport }) => {
          lastCanvas = viewport.canvas;
          if (busyState === "idle" && !lastError) return;

          const msg =
            busyState === "uploading"
              ? t("ext.models.editor.uploading")
              : busyState === "processing"
                ? t("ext.models.editor.processing")
                : lastError
                  ? `${t("ext.models.editor.failed")}: ${lastError}`
                  : "";

          if (!msg) return;

          canvas.save();
          canvas.font = "12px ui-sans-serif, system-ui";
          canvas.textBaseline = "top";

          const pad = 10;
          const w = viewport.width;
          const textW = canvas.measureText(msg).width;
          const boxW = Math.min(w - 24, textW + pad * 2);
          const x0 = (w - boxW) / 2;
          const y0 = 14;

          canvas.fillStyle = "rgba(8,12,26,0.82)";
          canvas.strokeStyle = "rgba(255,255,255,0.14)";
          canvas.lineWidth = 1;
          drawPill(canvas, x0, y0, boxW, 28);
          canvas.fill();
          canvas.stroke();

          canvas.fillStyle = "rgba(230,232,242,0.92)";
          canvas.fillText(msg, x0 + pad, y0 + 8);

          canvas.restore();
        },
        getCursor: () => (busyState === "idle" ? "copy" : "wait"),
        dispose: () => {
          dbg("[models:tool] session disposed");
          input.remove();
        },
      };
    },
  };
}
