import React, { useMemo } from "react";
import type * as ThreeTypes from "three";

import type { CompositionElement, CompositionElementPatch, ElementType, HostI18n, PlanePoint } from "@toposync/plugin-api";

import { rgbaFromHex } from "../colors";
import { DEFAULT_POOL_DEPTH_METERS, FLOOR_EPSILON, GROUND_Y, POOL_ELEMENT_TYPE_ID } from "../constants";
import { readNumber, readPlanePointArray } from "../parsing";

type PoolProps = {
  depth_m: number;
  vertices: PlanePoint[];
};

let cachedWaterBumpTexture: ThreeTypes.Texture | null = null;
let cachedWaterNormalTexture: ThreeTypes.Texture | null = null;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function createWaterBumpCanvas(size: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  ctx.fillStyle = "rgb(128,128,128)";
  ctx.fillRect(0, 0, size, size);

  ctx.save();
  ctx.globalAlpha = 0.14;
  ctx.fillStyle = "rgb(150,150,150)";
  for (let i = 0; i < 220; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const r = 6 + Math.random() * 26;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.globalAlpha = 0.10;
  ctx.fillStyle = "rgb(100,100,100)";
  for (let i = 0; i < 160; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const r = 4 + Math.random() * 20;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();

  return canvas;
}

function getWaterBumpTexture(THREE: typeof import("three")): ThreeTypes.Texture {
  if (cachedWaterBumpTexture) return cachedWaterBumpTexture;
  const canvas = createWaterBumpCanvas(256);
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(2, 2);
  tex.anisotropy = 4;
  (tex as any).colorSpace = (THREE as any).NoColorSpace ?? (THREE as any).LinearSRGBColorSpace;
  cachedWaterBumpTexture = tex;
  return tex;
}

function createWaterNormalCanvas(size: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  const image = ctx.createImageData(size, size);
  const data = image.data;
  const twoPi = Math.PI * 2;
  const waves: Array<{ dirX: number; dirY: number; freq: number; amp: number; phase: number }> = [
    { dirX: 0.92, dirY: 0.39, freq: 3.0, amp: 0.7, phase: Math.random() * twoPi },
    { dirX: -0.16, dirY: 0.99, freq: 5.0, amp: 0.5, phase: Math.random() * twoPi },
    { dirX: 0.68, dirY: -0.73, freq: 8.0, amp: 0.35, phase: Math.random() * twoPi },
    { dirX: -0.91, dirY: -0.41, freq: 12.0, amp: 0.22, phase: Math.random() * twoPi },
  ];

  const strength = 0.085;

  for (let y = 0; y < size; y += 1) {
    const v = y / size;
    for (let x = 0; x < size; x += 1) {
      const u = x / size;

      let du = 0;
      let dv = 0;
      for (const w of waves) {
        const theta = twoPi * (w.dirX * u + w.dirY * v) * w.freq + w.phase;
        const c = Math.cos(theta);
        const scale = w.amp * w.freq * twoPi;
        du += c * scale * w.dirX;
        dv += c * scale * w.dirY;
      }

      let nx = -du * strength;
      let ny = 1.0;
      let nz = -dv * strength;
      const invLen = 1 / Math.sqrt(nx * nx + ny * ny + nz * nz);
      nx *= invLen;
      ny *= invLen;
      nz *= invLen;

      const idx = (y * size + x) * 4;
      data[idx + 0] = Math.round((nx * 0.5 + 0.5) * 255);
      data[idx + 1] = Math.round((ny * 0.5 + 0.5) * 255);
      data[idx + 2] = Math.round((nz * 0.5 + 0.5) * 255);
      data[idx + 3] = 255;
    }
  }

  ctx.putImageData(image, 0, 0);
  return canvas;
}

function getWaterNormalTexture(THREE: typeof import("three")): ThreeTypes.Texture {
  if (cachedWaterNormalTexture) return cachedWaterNormalTexture;
  const canvas = createWaterNormalCanvas(256);
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(2, 2);
  tex.anisotropy = 8;
  (tex as any).colorSpace = (THREE as any).NoColorSpace ?? (THREE as any).LinearSRGBColorSpace;
  cachedWaterNormalTexture = tex;
  return tex;
}

function parsePoolProps(props: Record<string, unknown>): PoolProps {
  const depth = clamp(readNumber(props.depth_m, DEFAULT_POOL_DEPTH_METERS), 0.1, 20);
  const vertices = readPlanePointArray(props.vertices);
  return { depth_m: depth, vertices };
}

function buildShapeGeometry(
  THREE: typeof import("three"),
  element: CompositionElement,
  vertices: PlanePoint[],
): ThreeTypes.BufferGeometry | null {
  if (vertices.length < 3) return null;
  const local = vertices.map((p) => ({ x: p.x - element.position.x, z: p.z - element.position.z }));
  const shape = new THREE.Shape();
  shape.moveTo(local[0].x, -local[0].z);
  for (let i = 1; i < local.length; i++) shape.lineTo(local[i].x, -local[i].z);
  shape.closePath();

  const geometry = new THREE.ShapeGeometry(shape);
  geometry.rotateX(-Math.PI / 2);
  return geometry;
}

function buildWallGeometry(
  THREE: typeof import("three"),
  element: CompositionElement,
  vertices: PlanePoint[],
  depthMeters: number,
): ThreeTypes.BufferGeometry | null {
  if (vertices.length < 2) return null;

  const local = vertices.map((p) => ({ x: p.x - element.position.x, z: p.z - element.position.z }));
  const depth = -Math.abs(depthMeters);

  const quadCount = local.length;
  const positions = new Float32Array(quadCount * 4 * 3);
  const indices: number[] = [];

  for (let i = 0; i < quadCount; i++) {
    const a = local[i];
    const b = local[(i + 1) % quadCount];
    const base = i * 4;

    positions[(base + 0) * 3 + 0] = a.x;
    positions[(base + 0) * 3 + 1] = 0;
    positions[(base + 0) * 3 + 2] = a.z;

    positions[(base + 1) * 3 + 0] = b.x;
    positions[(base + 1) * 3 + 1] = 0;
    positions[(base + 1) * 3 + 2] = b.z;

    positions[(base + 2) * 3 + 0] = b.x;
    positions[(base + 2) * 3 + 1] = depth;
    positions[(base + 2) * 3 + 2] = b.z;

    positions[(base + 3) * 3 + 0] = a.x;
    positions[(base + 3) * 3 + 1] = depth;
    positions[(base + 3) * 3 + 2] = a.z;

    indices.push(base + 0, base + 1, base + 2, base + 0, base + 2, base + 3);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}

export function createPoolElementType(i18n: HostI18n): ElementType {
  return {
    type: POOL_ELEMENT_TYPE_ID,
    layerGroup: "areas",
    name: { key: "ext.structural.pool.name", fallback: "Pool" },
    description: { key: "ext.structural.pool.desc", fallback: "Swimming pool (hole) in 2D." },
    defaultProps: {
      depth_m: DEFAULT_POOL_DEPTH_METERS,
      vertices: [
        { x: -1, z: -1 },
        { x: 1, z: -1 },
        { x: 1, z: 1 },
        { x: -1, z: 1 },
      ],
    },
    create3D: ({ THREE, view }, element) => {
      const group = new THREE.Group();
      const emptyGeometry = new THREE.BufferGeometry();

      const stencilMaterial = new THREE.MeshBasicMaterial({ color: 0x000000 });
      stencilMaterial.colorWrite = false;
      stencilMaterial.depthWrite = false;
      stencilMaterial.depthTest = false;
      stencilMaterial.stencilWrite = true;
      stencilMaterial.stencilRef = 1;
      stencilMaterial.stencilFunc = THREE.AlwaysStencilFunc;
      stencilMaterial.stencilZPass = THREE.ReplaceStencilOp;

      const waterBump = getWaterBumpTexture(THREE);
      const waterNormal = getWaterNormalTexture(THREE);

      const waterMaterialSimplified = new THREE.MeshStandardMaterial({
        color: 0x0ea5e9,
        roughness: 0.18,
        metalness: 0.02,
        transparent: true,
        opacity: 0.68,
        side: THREE.DoubleSide,
        depthWrite: false,
        emissive: new THREE.Color(0x06243a),
        emissiveIntensity: 0.10,
      });
      waterMaterialSimplified.bumpMap = waterBump;
      waterMaterialSimplified.bumpScale = 0.0;

      const waterMaterialDetailed = new THREE.MeshPhysicalMaterial({
        color: 0x0ea5e9,
        roughness: 0.12,
        metalness: 0.0,
        transmission: 0.92,
        thickness: 0.18,
        ior: 1.33,
        transparent: true,
        opacity: 1.0,
        side: THREE.DoubleSide,
        depthWrite: false,
        clearcoat: 0.62,
        clearcoatRoughness: 0.08,
      });
      waterMaterialDetailed.normalMap = waterNormal;
      waterMaterialDetailed.normalScale = new THREE.Vector2(0.55, 0.55);
      (waterMaterialDetailed as any).attenuationColor = new THREE.Color(0x06243a);
      (waterMaterialDetailed as any).attenuationDistance = 0.8;

      const wallMaterial = new THREE.MeshStandardMaterial({
        color: 0x0f172a,
        roughness: 0.95,
        metalness: 0.02,
        side: THREE.DoubleSide,
      });

      const bottomMaterial = new THREE.MeshStandardMaterial({
        color: 0x0b1f38,
        roughness: 0.98,
        metalness: 0.0,
        side: THREE.DoubleSide,
      });

      const stencilMesh = new THREE.Mesh(emptyGeometry, stencilMaterial);
      stencilMesh.renderOrder = -20;

      const waterMesh = new THREE.Mesh(emptyGeometry, waterMaterialSimplified);
      waterMesh.renderOrder = 10;

      const wallMesh = new THREE.Mesh(emptyGeometry, wallMaterial);
      const bottomMesh = new THREE.Mesh(emptyGeometry, bottomMaterial);

      group.add(stencilMesh);
      group.add(wallMesh);
      group.add(bottomMesh);
      group.add(waterMesh);

      let shapeGeometry: ThreeTypes.BufferGeometry | null = null;
      let wallGeometry: ThreeTypes.BufferGeometry | null = null;
      let lastKey = "";

      function apply(next: CompositionElement) {
        const pool = parsePoolProps(next.props);
        const detailed = (view.graphicsQuality ?? "simplified") === "detailed";

        const localKey = JSON.stringify({
          v: pool.vertices.map((p) => ({
            x: Math.round((p.x - next.position.x) * 1000) / 1000,
            z: Math.round((p.z - next.position.z) * 1000) / 1000,
          })),
          d: Math.round(pool.depth_m * 1000) / 1000,
        });

        if (localKey !== lastKey) {
          lastKey = localKey;

          const nextShapeGeometry = buildShapeGeometry(THREE, next, pool.vertices);
          const nextWallGeometry = buildWallGeometry(THREE, next, pool.vertices, pool.depth_m);

          if (shapeGeometry) shapeGeometry.dispose();
          if (wallGeometry) wallGeometry.dispose();

          shapeGeometry = nextShapeGeometry;
          wallGeometry = nextWallGeometry;

          stencilMesh.geometry = shapeGeometry ?? emptyGeometry;
          waterMesh.geometry = shapeGeometry ?? emptyGeometry;
          bottomMesh.geometry = shapeGeometry ?? emptyGeometry;
          wallMesh.geometry = wallGeometry ?? emptyGeometry;
        }

        const valid = Boolean(shapeGeometry && wallGeometry);
        stencilMesh.visible = valid;
        waterMesh.visible = valid;
        bottomMesh.visible = valid;
        wallMesh.visible = valid;

        if (!valid) return;

        stencilMesh.position.y = GROUND_Y + FLOOR_EPSILON;

        wallMesh.position.y = GROUND_Y;
        bottomMesh.position.y = GROUND_Y - pool.depth_m;

        const waterOffset = Math.min(0.06, Math.max(0.02, pool.depth_m * 0.3));
        waterMesh.position.y = GROUND_Y - waterOffset;

        waterMesh.material = detailed ? waterMaterialDetailed : waterMaterialSimplified;
        if (detailed) {
          waterMaterialDetailed.roughness = 0.10;
          waterMaterialDetailed.transmission = 0.92;
          waterMaterialDetailed.clearcoat = 0.65;
          waterMaterialDetailed.clearcoatRoughness = 0.08;
          (waterMaterialDetailed as any).attenuationDistance = 0.8 + clamp(pool.depth_m, 0.2, 2.5) * 0.35;
        } else {
          waterMaterialSimplified.roughness = 0.22;
          waterMaterialSimplified.opacity = 0.66;
          waterMaterialSimplified.emissiveIntensity = 0.06;
          waterMaterialSimplified.bumpScale = 0.0;
        }
      }

      apply(element);

      return {
        object: group,
        update: apply,
        tick: (dt: number) => {
          if ((view.graphicsQuality ?? "simplified") !== "detailed") return;
          const now = performance.now();
          const userData = ((waterNormal as any).userData ??= {});
          const lastUpdate = typeof userData.lastOffsetUpdateMs === "number" ? userData.lastOffsetUpdateMs : 0;
          if (now - lastUpdate < 4) return;
          userData.lastOffsetUpdateMs = now;
          const speed = 0.022;
          waterNormal.offset.x = (waterNormal.offset.x + dt * speed) % 1;
          waterNormal.offset.y = (waterNormal.offset.y + dt * speed * 0.7) % 1;
        },
        dispose: () => {
          if (shapeGeometry) shapeGeometry.dispose();
          if (wallGeometry) wallGeometry.dispose();
          emptyGeometry.dispose();
          stencilMaterial.dispose();
          wallMaterial.dispose();
          bottomMaterial.dispose();
          waterMaterialSimplified.dispose();
          waterMaterialDetailed.dispose();
        },
      };
    },
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const pool = parsePoolProps(element.props);
      if (pool.vertices.length < 3) return;

      const points = pool.vertices.map((p) => viewport.worldToScreen(p));

      canvasContext.save();
      canvasContext.beginPath();
      canvasContext.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i++) canvasContext.lineTo(points[i].x, points[i].y);
      canvasContext.closePath();

      canvasContext.fillStyle = rgbaFromHex("#0ea5e9", 0.14);
      canvasContext.fill();

      canvasContext.strokeStyle = rgbaFromHex("#38bdf8", 0.65);
      canvasContext.lineWidth = 2;
      canvasContext.stroke();
      canvasContext.restore();
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <PoolEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

type PoolEditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function PoolEditor({ element, update, remove, close, i18n }: PoolEditorProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const pool = parsePoolProps(element.props);

  const numberFormatter = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">{t("ext.structural.pool.name")}</div>
          <div className="cardMeta">{numberFormatter.format(pool.depth_m)} m</div>
        </div>
        <div className="cardBody">{t("ext.structural.pool.desc")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="field">
        <div className="label">{t("ext.structural.editor.pool_name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.structural.editor.pool_depth")}</div>
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={0.1}
            max={20}
            step={0.1}
            value={pool.depth_m}
            onChange={(e) => {
              const nextDepth = Number.parseFloat(e.target.value);
              if (!Number.isFinite(nextDepth)) return;
              update({ props: { depth_m: clamp(nextDepth, 0.1, 20) } });
            }}
          />
        </div>
      </div>

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
