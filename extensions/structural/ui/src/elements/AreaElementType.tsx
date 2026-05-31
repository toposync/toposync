import React from "react";
import type * as ThreeTypes from "three";

import type {
  BoundsXZ,
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostI18n,
  PlanePoint,
} from "@toposync/plugin-api";

import { rgbaFromHex, shadeHex } from "../colors";
import {
  AREA_ELEMENT_TYPE_ID,
  DEFAULT_AREA_FILL_COLOR,
  DEFAULT_AREA_OPACITY,
  FLOOR_EPSILON,
  GROUND_Y,
} from "../constants";
import {
  readNumber,
  readPlanePointArray,
  readString,
  saveAreaFillColor,
} from "../parsing";
import { getFloorTexture, readFloorTextureId } from "../textures";
import { snapAreaVerticesToWallNodes } from "../wallGeometry";

let cachedGrassBladeAlphaTexture: ThreeTypes.Texture | null = null;

function getGrassBladeAlphaTexture(
  THREE: typeof import("three"),
): ThreeTypes.Texture {
  if (cachedGrassBladeAlphaTexture) return cachedGrassBladeAlphaTexture;

  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = "black";
    ctx.fillRect(0, 0, size, size);

    const grad = ctx.createLinearGradient(0, size, 0, 0);
    grad.addColorStop(0.0, "rgba(255,255,255,0)");
    grad.addColorStop(0.18, "rgba(255,255,255,1)");
    grad.addColorStop(1.0, "rgba(255,255,255,1)");
    ctx.fillStyle = grad;

    ctx.beginPath();
    ctx.moveTo(size * 0.5, size * 0.02);
    ctx.quadraticCurveTo(size * 0.58, size * 0.45, size * 0.62, size * 0.98);
    ctx.quadraticCurveTo(size * 0.5, size * 0.9, size * 0.38, size * 0.98);
    ctx.quadraticCurveTo(size * 0.42, size * 0.45, size * 0.5, size * 0.02);
    ctx.closePath();
    ctx.fill();

    ctx.globalCompositeOperation = "destination-in";
    const edge = ctx.createRadialGradient(
      size * 0.5,
      size * 0.8,
      0,
      size * 0.5,
      size * 0.8,
      size * 0.55,
    );
    edge.addColorStop(0, "rgba(255,255,255,1)");
    edge.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = edge;
    ctx.fillRect(0, 0, size, size);
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.ClampToEdgeWrapping;
  texture.wrapT = THREE.ClampToEdgeWrapping;
  texture.magFilter = THREE.LinearFilter;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  (texture as any).colorSpace =
    (THREE as any).NoColorSpace ?? (THREE as any).LinearSRGBColorSpace;
  cachedGrassBladeAlphaTexture = texture;
  return texture;
}

function polygonAreaXZ(vertices: Array<{ x: number; z: number }>): number {
  if (vertices.length < 3) return 0;
  let sum = 0;
  for (let i = 0; i < vertices.length; i += 1) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    sum += a.x * b.z - b.x * a.z;
  }
  return Math.abs(sum) / 2;
}

type TriangleXZ = {
  ax: number;
  az: number;
  bx: number;
  bz: number;
  cx: number;
  cz: number;
  area: number;
};

function boundsForPoints(points: PlanePoint[]): BoundsXZ | null {
  if (points.length === 0) return null;
  let minX = points[0].x;
  let maxX = points[0].x;
  let minZ = points[0].z;
  let maxZ = points[0].z;
  for (let i = 1; i < points.length; i += 1) {
    const point = points[i];
    minX = Math.min(minX, point.x);
    maxX = Math.max(maxX, point.x);
    minZ = Math.min(minZ, point.z);
    maxZ = Math.max(maxZ, point.z);
  }
  return { minX, maxX, minZ, maxZ };
}

function svgPolygonPoints(points: PlanePoint[]): string {
  return points.map((point) => `${point.x},${point.z}`).join(" ");
}

function buildTriangleTable(geometry: ThreeTypes.BufferGeometry): {
  triangles: TriangleXZ[];
  cdf: number[];
  totalArea: number;
} {
  const pos = geometry.getAttribute("position") as ThreeTypes.BufferAttribute;
  const idx = geometry.getIndex();
  const triangles: TriangleXZ[] = [];
  const cdf: number[] = [];
  let total = 0;

  function pushTriangle(ai: number, bi: number, ci: number) {
    const ax = pos.getX(ai);
    const az = pos.getZ(ai);
    const bx = pos.getX(bi);
    const bz = pos.getZ(bi);
    const cx = pos.getX(ci);
    const cz = pos.getZ(ci);
    const area = Math.abs((bx - ax) * (cz - az) - (cx - ax) * (bz - az)) * 0.5;
    if (!(area > 1e-9)) return;
    total += area;
    triangles.push({ ax, az, bx, bz, cx, cz, area });
    cdf.push(total);
  }

  if (idx) {
    for (let i = 0; i < idx.count; i += 3) {
      pushTriangle(idx.getX(i), idx.getX(i + 1), idx.getX(i + 2));
    }
  } else {
    for (let i = 0; i < pos.count; i += 3) {
      pushTriangle(i, i + 1, i + 2);
    }
  }

  return { triangles, cdf, totalArea: total };
}

function sampleTrianglePoint(tri: TriangleXZ): { x: number; z: number } {
  const r1 = Math.random();
  const r2 = Math.random();
  const sqrtR1 = Math.sqrt(r1);
  const u = 1 - sqrtR1;
  const v = r2 * sqrtR1;
  const w = 1 - u - v;
  return {
    x: tri.ax * u + tri.bx * v + tri.cx * w,
    z: tri.az * u + tri.bz * v + tri.cz * w,
  };
}

export function createAreaElementType(i18n: HostI18n): ElementType {
  return {
    type: AREA_ELEMENT_TYPE_ID,
    layerGroup: "areas",
    name: { key: "ext.structural.area.name", fallback: "Area" },
    description: {
      key: "ext.structural.area.desc",
      fallback: "Area (polygon) in 2D.",
    },
    defaultProps: {
      fill: DEFAULT_AREA_FILL_COLOR,
      opacity: DEFAULT_AREA_OPACITY,
      texture: "none",
      vertices: [
        { x: -1, z: -1 },
        { x: 1, z: -1 },
        { x: 1, z: 1 },
        { x: -1, z: 1 },
      ],
    },
    getMain2DBounds: (element) =>
      boundsForPoints(readPlanePointArray(element.props.vertices)),
    renderMain2DVector: ({ element, elements }) => {
      const vertices = snapAreaVerticesToWallNodes(
        elements,
        readPlanePointArray(element.props.vertices),
      );
      if (vertices.length < 3) return null;
      const fill = readString(element.props.fill, DEFAULT_AREA_FILL_COLOR);
      const opacityRaw = readNumber(
        element.props.opacity,
        DEFAULT_AREA_OPACITY,
      );
      const opacity = opacityRaw < 0.001 ? 0 : opacityRaw;
      if (opacity <= 0) return null;
      const textureId = readFloorTextureId(element.props.texture, "none");
      const points = svgPolygonPoints(vertices);
      const edgeColor = rgbaFromHex(
        shadeHex(fill, textureId === "grass" ? -0.24 : -0.16),
        Math.min(0.34, opacity * 0.34),
      );
      const highlightColor = rgbaFromHex(
        shadeHex(fill, 0.42),
        Math.min(0.2, opacity * 0.2),
      );
      const patternId =
        textureId === "grass"
          ? "mainVector2dGrassPattern"
          : textureId === "concrete"
            ? "mainVector2dConcretePattern"
            : "";
      return (
        <g className="mainVector2dArea">
          <polygon
            points={points}
            fill={rgbaFromHex(fill, Math.min(0.92, Math.max(0, opacity)))}
            stroke={edgeColor}
            strokeWidth={0.026}
            vectorEffect="non-scaling-stroke"
            filter="url(#mainVector2dSoftShadow)"
          />
          {patternId ? (
            <polygon
              points={points}
              fill={`url(#${patternId})`}
              opacity={Math.min(0.42, opacity * 0.38)}
            />
          ) : null}
          <polygon
            points={points}
            fill="none"
            stroke={highlightColor}
            strokeWidth={0.01}
            vectorEffect="non-scaling-stroke"
          />
        </g>
      );
    },
    create3D: ({ THREE, view, elements }, element) => {
      const group = new THREE.Group();

      const material = new THREE.MeshStandardMaterial({
        color: DEFAULT_AREA_FILL_COLOR,
        roughness: 0.95,
        metalness: 0.0,
        side: THREE.DoubleSide,
        transparent: false,
        opacity: 1.0,
        polygonOffset: true,
        polygonOffsetFactor: 1,
        polygonOffsetUnits: 1,
      });
      material.stencilWrite = true;
      material.stencilRef = 1;
      material.stencilFunc = THREE.NotEqualStencilFunc;
      material.stencilFail = THREE.KeepStencilOp;
      material.stencilZFail = THREE.KeepStencilOp;
      material.stencilZPass = THREE.KeepStencilOp;
      material.stencilWriteMask = 0x00;
      material.stencilFuncMask = 0xff;

      let mesh: ThreeTypes.Mesh | null = null;
      let lastKey = "";
      let grassBlades: ThreeTypes.InstancedMesh | null = null;
      let grassBladeMaterial: ThreeTypes.MeshStandardMaterial | null = null;
      let lastGrassKey = "";
      let contextElements = elements;

      function readRenderedVertices(el: CompositionElement): PlanePoint[] {
        return snapAreaVerticesToWallNodes(
          contextElements,
          readPlanePointArray(el.props.vertices),
        );
      }

      function buildGeometry(
        el: CompositionElement,
      ): ThreeTypes.BufferGeometry | null {
        const vertices = readRenderedVertices(el);
        if (vertices.length < 3) return null;

        const local = vertices.map((p) => ({
          x: p.x - el.position.x,
          z: p.z - el.position.z,
        }));
        const shape = new THREE.Shape();
        shape.moveTo(local[0].x, -local[0].z);
        for (let i = 1; i < local.length; i++)
          shape.lineTo(local[i].x, -local[i].z);
        shape.closePath();

        const geometry = new THREE.ShapeGeometry(shape);
        geometry.rotateX(-Math.PI / 2);

        const pos = geometry.getAttribute(
          "position",
        ) as ThreeTypes.BufferAttribute;
        const uv = new THREE.BufferAttribute(
          new Float32Array(pos.count * 2),
          2,
        );
        for (let i = 0; i < pos.count; i++) {
          const worldX = pos.getX(i) + el.position.x;
          const worldZ = pos.getZ(i) + el.position.z;
          uv.setXY(i, worldX, worldZ);
        }
        geometry.setAttribute("uv", uv);
        return geometry;
      }

      function ensureGrassBladeMaterial(): ThreeTypes.MeshStandardMaterial {
        if (grassBladeMaterial) return grassBladeMaterial;

        const alpha = getGrassBladeAlphaTexture(THREE);
        const m = new THREE.MeshStandardMaterial({
          color: 0x16a34a,
          roughness: 0.88,
          metalness: 0.0,
          side: THREE.DoubleSide,
          alphaMap: alpha,
          alphaTest: 0.35,
        });

        m.stencilWrite = true;
        m.stencilRef = 1;
        m.stencilFunc = THREE.NotEqualStencilFunc;
        m.stencilFail = THREE.KeepStencilOp;
        m.stencilZFail = THREE.KeepStencilOp;
        m.stencilZPass = THREE.KeepStencilOp;
        m.stencilWriteMask = 0x00;
        m.stencilFuncMask = 0xff;

        m.onBeforeCompile = (shader) => {
          shader.uniforms.uTime = { value: 0 };
          shader.vertexShader = shader.vertexShader
            .replace(
              "#include <common>",
              "#include <common>\nuniform float uTime;\nattribute float instanceSeed;",
            )
            .replace(
              "#include <begin_vertex>",
              `#include <begin_vertex>
float h01 = clamp(position.y, 0.0, 1.0);
float wind = sin(uTime * 1.2 + instanceSeed + position.x * 2.4) * 0.12;
float wind2 = cos(uTime * 0.9 + instanceSeed * 1.7) * 0.08;
transformed.x += (wind + wind2) * h01 * h01;
transformed.z += sin(uTime * 1.1 + instanceSeed * 0.7) * 0.06 * h01 * h01;`,
            );
          (m.userData as any).shader = shader;
        };

        grassBladeMaterial = m;
        return m;
      }

      function rebuildGrassBlades(
        geometry: ThreeTypes.BufferGeometry,
        bladeCount: number,
        fill: string,
      ) {
        if (grassBlades) {
          group.remove(grassBlades);
          (grassBlades.geometry as ThreeTypes.BufferGeometry).dispose();
          grassBlades = null;
        }

        if (!(bladeCount > 0)) return;

        const baseGeometry = new THREE.PlaneGeometry(1, 1, 1, 4);
        baseGeometry.translate(0, 0.5, 0);
        const instancedGeometry = baseGeometry;
        const seeds = new THREE.InstancedBufferAttribute(
          new Float32Array(bladeCount),
          1,
        );
        instancedGeometry.setAttribute("instanceSeed", seeds);

        const bladesMaterial = ensureGrassBladeMaterial();
        bladesMaterial.color.set(fill);

        const blades = new THREE.InstancedMesh(
          instancedGeometry,
          bladesMaterial,
          bladeCount,
        );
        blades.frustumCulled = false;
        blades.renderOrder = 5;

        const table = buildTriangleTable(geometry);
        if (!(table.totalArea > 1e-9) || table.triangles.length === 0) {
          instancedGeometry.dispose();
          return;
        }

        const mat = new THREE.Matrix4();
        const quat = new THREE.Quaternion();
        const pos = new THREE.Vector3();
        const scale = new THREE.Vector3();
        const axisY = new THREE.Vector3(0, 1, 0);

        for (let i = 0; i < bladeCount; i += 1) {
          const r = Math.random() * table.totalArea;
          let lo = 0;
          let hi = table.cdf.length - 1;
          while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (r <= table.cdf[mid]) hi = mid;
            else lo = mid + 1;
          }
          const tri = table.triangles[lo];
          const point = sampleTrianglePoint(tri);

          const yaw = Math.random() * Math.PI * 2;
          quat.setFromAxisAngle(axisY, yaw);

          const height = 0.1 + Math.random() * 0.2;
          const width = height * (0.12 + Math.random() * 0.1);
          scale.set(width, height, 1);

          pos.set(point.x, GROUND_Y + FLOOR_EPSILON + 0.002, point.z);
          mat.compose(pos, quat, scale);
          blades.setMatrixAt(i, mat);
          seeds.setX(i, Math.random() * 1000);
        }

        blades.instanceMatrix.needsUpdate = true;
        seeds.needsUpdate = true;

        grassBlades = blades;
        group.add(blades);
      }

      function apply(
        el: CompositionElement,
        updateContext?: { elements: CompositionElement[] },
      ) {
        contextElements = updateContext?.elements ?? contextElements;
        const vertices = readRenderedVertices(el);
        const localKey = JSON.stringify(
          vertices.map((p) => ({
            x: Math.round((p.x - el.position.x) * 1000) / 1000,
            z: Math.round((p.z - el.position.z) * 1000) / 1000,
          })),
        );

        if (localKey !== lastKey) {
          lastKey = localKey;
          const geometry = buildGeometry(el);
          if (geometry) {
            if (!mesh) {
              mesh = new THREE.Mesh(geometry, material);
              mesh.receiveShadow = true;
              group.add(mesh);
            } else {
              (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
              mesh.geometry = geometry;
            }
          } else if (mesh) {
            group.remove(mesh);
            (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
            mesh = null;
          }
        }

        const fill = readString(el.props.fill, DEFAULT_AREA_FILL_COLOR);
        const opacityRaw = Math.max(
          0,
          Math.min(1, readNumber(el.props.opacity, DEFAULT_AREA_OPACITY)),
        );
        const isHidden = opacityRaw < 0.001;
        const opacity = isHidden ? 0 : 1;
        const textureId = readFloorTextureId(el.props.texture, "none");
        const quality = view.graphicsQuality ?? "simplified";
        const nextMap = getFloorTexture(THREE, textureId, quality);
        material.color.set(fill);
        if (material.map !== nextMap) {
          material.map = nextMap;
          material.needsUpdate = true;
        }
        material.roughness = textureId === "grass" ? 0.98 : 0.95;
        material.metalness = 0.0;
        material.opacity = opacity;
        material.transparent = isHidden;
        material.depthWrite = !isHidden;

        if (mesh) {
          mesh.visible = !isHidden;
          mesh.position.y = GROUND_Y + FLOOR_EPSILON;
        }

        const wantsGrassBlades =
          !isHidden &&
          textureId === "grass" &&
          quality === "detailed" &&
          Boolean(mesh && mesh.geometry);
        if (!wantsGrassBlades) {
          if (grassBlades) grassBlades.visible = false;
          return;
        }

        if (grassBlades) grassBlades.visible = true;

        const area = polygonAreaXZ(vertices);
        const density = 35;
        const bladeCount = Math.max(
          240,
          Math.min(1800, Math.floor(area * density)),
        );
        const grassKey = `${localKey}:${bladeCount}`;
        if (grassKey !== lastGrassKey && mesh) {
          lastGrassKey = grassKey;
          rebuildGrassBlades(
            mesh.geometry as ThreeTypes.BufferGeometry,
            bladeCount,
            fill,
          );
        } else if (grassBladeMaterial) {
          grassBladeMaterial.color.set(fill);
        }
      }

      apply(element);
      return {
        object: group,
        update: apply,
        tick: () => {
          if (!grassBladeMaterial || !grassBlades?.visible) return false;
          const shader = (grassBladeMaterial.userData as any).shader;
          if (!shader) return false;
          shader.uniforms.uTime.value = performance.now() / 1000;
          return true;
        },
        dispose: () => {
          if (mesh) (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
          material.dispose();
          if (grassBlades)
            (grassBlades.geometry as ThreeTypes.BufferGeometry).dispose();
          grassBladeMaterial?.dispose();
        },
      };
    },
    render2D: ({ ctx: canvasContext, element, elements, viewport }) => {
      const vertices = snapAreaVerticesToWallNodes(
        elements,
        readPlanePointArray(element.props.vertices),
      );
      if (vertices.length < 3) return;

      const fill = readString(element.props.fill, DEFAULT_AREA_FILL_COLOR);
      const opacityRaw = readNumber(
        element.props.opacity,
        DEFAULT_AREA_OPACITY,
      );
      const opacity = opacityRaw < 0.001 ? 0 : 1;

      const points = vertices.map((p) => viewport.worldToScreen(p));

      canvasContext.save();
      canvasContext.beginPath();
      canvasContext.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i++)
        canvasContext.lineTo(points[i].x, points[i].y);
      canvasContext.closePath();

      canvasContext.fillStyle = rgbaFromHex(fill, opacity);
      canvasContext.fill();

      canvasContext.strokeStyle = rgbaFromHex("#e6e8f2", 0.22);
      canvasContext.lineWidth = 2;
      canvasContext.stroke();
      canvasContext.restore();
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <AreaEditor
        element={element}
        update={update}
        remove={remove}
        close={close}
        i18n={i18n}
      />
    ),
  };
}

type AreaEditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function AreaEditor({
  element,
  update,
  remove,
  close,
  i18n,
}: AreaEditorProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const fill = readString(element.props.fill, DEFAULT_AREA_FILL_COLOR);
  const opacity = readNumber(element.props.opacity, DEFAULT_AREA_OPACITY);
  const transparent = opacity < 0.001;
  const texture = readFloorTextureId(element.props.texture, "none");
  const defaultGrassFill = "#16a34a";

  return (
    <div>
      <div className="field">
        <div className="label">{t("ext.structural.editor.area_name")}</div>
        <input
          className="input"
          value={element.name}
          onChange={(e) => update({ name: e.target.value })}
        />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.structural.editor.area_color")}</div>
          <input
            className="input"
            type="color"
            value={fill}
            onChange={(e) => {
              const next = e.target.value;
              saveAreaFillColor(next);
              update({ props: { fill: next } });
            }}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">
            {t("ext.structural.editor.floor_texture")}
          </div>
          <select
            className="input"
            value={texture}
            onChange={(e) => {
              const nextTexture = readFloorTextureId(e.target.value, texture);
              const nextFill =
                nextTexture === "grass"
                  ? defaultGrassFill
                  : nextTexture === "concrete"
                    ? DEFAULT_AREA_FILL_COLOR
                    : fill;
              saveAreaFillColor(nextFill);
              update({ props: { texture: nextTexture, fill: nextFill } });
            }}
          >
            <option value="none">
              {t("ext.structural.editor.texture.none")}
            </option>
            <option value="grass">
              {t("ext.structural.editor.texture.grass")}
            </option>
            <option value="concrete">
              {t("ext.structural.editor.texture.concrete")}
            </option>
          </select>
        </div>
        <label
          className="chipButton"
          style={{ display: "inline-flex", alignItems: "center", gap: 10 }}
        >
          <input
            type="checkbox"
            checked={transparent}
            onChange={(e) =>
              update({
                props: { opacity: e.target.checked ? 0 : DEFAULT_AREA_OPACITY },
              })
            }
          />
          <span>{t("ext.structural.editor.transparent")}</span>
        </label>
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
