import React from "react";
import type * as ThreeTypes from "three";

import type { CompositionElement, CompositionElementPatch, ElementType, HostI18n } from "@toposync/plugin-api";

import { rgbaFromHex } from "../colors";
import { AREA_ELEMENT_TYPE_ID, DEFAULT_AREA_FILL_COLOR, DEFAULT_AREA_OPACITY, FLOOR_EPSILON, GROUND_Y } from "../constants";
import { readNumber, readPlanePointArray, readString, saveAreaFillColor } from "../parsing";
import { getFloorTexture, readFloorTextureId } from "../textures";

export function createAreaElementType(i18n: HostI18n): ElementType {
  return {
    type: AREA_ELEMENT_TYPE_ID,
    layerGroup: "areas",
    name: { key: "ext.structural.area.name", fallback: "Area" },
    description: { key: "ext.structural.area.desc", fallback: "Area (polygon) in 2D." },
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
    create3D: ({ THREE }, element) => {
      const group = new THREE.Group();

      const material = new THREE.MeshStandardMaterial({
        color: DEFAULT_AREA_FILL_COLOR,
        roughness: 0.95,
        metalness: 0.0,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: DEFAULT_AREA_OPACITY,
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

      function buildGeometry(el: CompositionElement): ThreeTypes.BufferGeometry | null {
        const vertices = readPlanePointArray(el.props.vertices);
        if (vertices.length < 3) return null;

        const local = vertices.map((p) => ({ x: p.x - el.position.x, z: p.z - el.position.z }));
        const shape = new THREE.Shape();
        shape.moveTo(local[0].x, -local[0].z);
        for (let i = 1; i < local.length; i++) shape.lineTo(local[i].x, -local[i].z);
        shape.closePath();

        const geometry = new THREE.ShapeGeometry(shape);
        geometry.rotateX(-Math.PI / 2);

        const pos = geometry.getAttribute("position") as ThreeTypes.BufferAttribute;
        const uv = new THREE.BufferAttribute(new Float32Array(pos.count * 2), 2);
        for (let i = 0; i < pos.count; i++) {
          const worldX = pos.getX(i) + el.position.x;
          const worldZ = pos.getZ(i) + el.position.z;
          uv.setXY(i, worldX, worldZ);
        }
        geometry.setAttribute("uv", uv);
        return geometry;
      }

      function apply(el: CompositionElement) {
        const vertices = readPlanePointArray(el.props.vertices);
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
        const opacity = Math.max(0, Math.min(1, readNumber(el.props.opacity, DEFAULT_AREA_OPACITY)));
        const textureId = readFloorTextureId(el.props.texture, "none");
        const nextMap = getFloorTexture(THREE, textureId);
        material.color.set(fill);
        if (material.map !== nextMap) {
          material.map = nextMap;
          material.needsUpdate = true;
        }
        material.roughness = textureId === "grass" ? 0.98 : 0.95;
        material.metalness = 0.0;
        material.opacity = opacity;
        material.transparent = opacity < 0.999;
        material.depthWrite = opacity >= 0.999;

        if (mesh) mesh.position.y = GROUND_Y + FLOOR_EPSILON;
      }

      apply(element);
      return {
        object: group,
        update: apply,
        dispose: () => {
          if (mesh) (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
          material.dispose();
        },
      };
    },
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const vertices = readPlanePointArray(element.props.vertices);
      if (vertices.length < 3) return;

      const fill = readString(element.props.fill, DEFAULT_AREA_FILL_COLOR);
      const opacity = readNumber(element.props.opacity, DEFAULT_AREA_OPACITY);

      const points = vertices.map((p) => viewport.worldToScreen(p));

      canvasContext.save();
      canvasContext.beginPath();
      canvasContext.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i++) canvasContext.lineTo(points[i].x, points[i].y);
      canvasContext.closePath();

      canvasContext.fillStyle = rgbaFromHex(fill, opacity);
      canvasContext.fill();

      canvasContext.strokeStyle = rgbaFromHex("#e6e8f2", 0.22);
      canvasContext.lineWidth = 2;
      canvasContext.stroke();
      canvasContext.restore();
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <AreaEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
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

function AreaEditor({ element, update, remove, close, i18n }: AreaEditorProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const fill = readString(element.props.fill, DEFAULT_AREA_FILL_COLOR);
  const opacity = readNumber(element.props.opacity, DEFAULT_AREA_OPACITY);
  const transparent = opacity < 0.001;
  const texture = readFloorTextureId(element.props.texture, "none");

  return (
    <div>
      <div className="field">
        <div className="label">{t("ext.structural.editor.area_name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
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
          <div className="label">{t("ext.structural.editor.floor_texture")}</div>
          <select
            className="input"
            value={texture}
            onChange={(e) => update({ props: { texture: readFloorTextureId(e.target.value, texture) } })}
          >
            <option value="none">{t("ext.structural.editor.texture.none")}</option>
            <option value="grass">{t("ext.structural.editor.texture.grass")}</option>
            <option value="concrete">{t("ext.structural.editor.texture.concrete")}</option>
          </select>
        </div>
        <label className="chipButton" style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <input
            type="checkbox"
            checked={transparent}
            onChange={(e) => update({ props: { opacity: e.target.checked ? 0 : DEFAULT_AREA_OPACITY } })}
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
