import React, { useMemo } from "react";
import type * as ThreeTypes from "three";

import type { CompositionElement, CompositionElementPatch, ElementType, HostI18n, PlanePoint } from "@toposync/plugin-api";

import { DEFAULT_WALL_COLOR, DEFAULT_WALL_WIDTH, GROUND_Y, WALL_ELEMENT_TYPE_ID } from "../constants";
import {
  addPoints,
  computeMiterJoin,
  distanceBetweenPoints,
  normalizePoint,
  perpendicularPoint,
  scalePoint,
  subtractPoints,
} from "../geometry";
import { readNumber, readOptionalPlanePoint, readPlanePoint, readString } from "../parsing";
import { getWallTexture, readWallTextureId } from "../textures";

export function createWallElementType(i18n: HostI18n): ElementType {
  return {
    type: WALL_ELEMENT_TYPE_ID,
    layerGroup: "walls",
    name: { key: "ext.structural.wall.name", fallback: "Wall" },
    description: { key: "ext.structural.wall.desc", fallback: "Simple wall (line) in 2D." },
    defaultProps: {
      color: DEFAULT_WALL_COLOR,
      texture: "none",
      width: DEFAULT_WALL_WIDTH,
      a: { x: 0, z: 0 },
      b: { x: 1, z: 0 },
    },
    create3D: ({ THREE, view }, element) => {
      const group = new THREE.Group();
      const material = new THREE.MeshStandardMaterial({
        color: DEFAULT_WALL_COLOR,
        roughness: 0.82,
        metalness: 0.05,
        flatShading: true,
      });
      const mesh = new THREE.Mesh(new THREE.BufferGeometry(), material);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      group.add(mesh);

      let lastKey = "";

      function apply(el: CompositionElement) {
        const startPoint = readPlanePoint(el.props.a, { x: el.position.x - 0.5, z: el.position.z });
        const endPoint = readPlanePoint(el.props.b, { x: el.position.x + 0.5, z: el.position.z });

        const thicknessWorld = Math.max(0.04, readNumber(el.props.width, DEFAULT_WALL_WIDTH));
        const height = Math.max(0.15, view.wallHeight);

        const color = readString(el.props.color, DEFAULT_WALL_COLOR);
        const textureId = readWallTextureId(el.props.texture, "none");
        const nextMap = getWallTexture(THREE, textureId);

        const half = thicknessWorld / 2;
        const direction = normalizePoint(subtractPoints(endPoint, startPoint));
        const normal = perpendicularPoint(direction);

        const previousStartPoint = readOptionalPlanePoint((el.props as any).a_prev);
        const nextEndPoint = readOptionalPlanePoint((el.props as any).b_next);

        const miterLimit = 6;

        const startBase = previousStartPoint ? startPoint : subtractPoints(startPoint, scalePoint(direction, half));
        const endBase = nextEndPoint ? endPoint : addPoints(endPoint, scalePoint(direction, half));

        const startPlus = previousStartPoint
          ? computeMiterJoin(
              startPoint,
              normalizePoint(subtractPoints(startPoint, previousStartPoint)),
              direction,
              +1,
              half,
              miterLimit,
              direction,
            )
          : addPoints(startBase, scalePoint(normal, +half));
        const startMinus = previousStartPoint
          ? computeMiterJoin(
              startPoint,
              normalizePoint(subtractPoints(startPoint, previousStartPoint)),
              direction,
              -1,
              half,
              miterLimit,
              direction,
            )
          : addPoints(startBase, scalePoint(normal, -half));

        const endPlus = nextEndPoint
          ? computeMiterJoin(
              endPoint,
              direction,
              normalizePoint(subtractPoints(nextEndPoint, endPoint)),
              +1,
              half,
              miterLimit,
              direction,
            )
          : addPoints(endBase, scalePoint(normal, +half));
        const endMinus = nextEndPoint
          ? computeMiterJoin(
              endPoint,
              direction,
              normalizePoint(subtractPoints(nextEndPoint, endPoint)),
              -1,
              half,
              miterLimit,
              direction,
            )
          : addPoints(endBase, scalePoint(normal, -half));

        const elementOrigin: PlanePoint = { x: el.position.x, z: el.position.z };
        const footprintPoints = [startPlus, endPlus, endMinus, startMinus].map((point) =>
          subtractPoints(point, elementOrigin),
        );
        const geometryKey = JSON.stringify({
          fp: footprintPoints.map((p) => ({ x: Math.round(p.x * 1000) / 1000, z: Math.round(p.z * 1000) / 1000 })),
          h: Math.round(height * 1000) / 1000,
        });

        if (geometryKey !== lastKey) {
          lastKey = geometryKey;
          const y0 = GROUND_Y;
          const y1 = GROUND_Y + height;

          const positions = new Float32Array(8 * 3);
          for (let i = 0; i < 4; i++) {
            const p = footprintPoints[i];
            positions[i * 3 + 0] = p.x;
            positions[i * 3 + 1] = y0;
            positions[i * 3 + 2] = p.z;
          }
          for (let i = 0; i < 4; i++) {
            const p = footprintPoints[i];
            const base = (4 + i) * 3;
            positions[base + 0] = p.x;
            positions[base + 1] = y1;
            positions[base + 2] = p.z;
          }

          const uvs = new Float32Array(8 * 2);
          const dotDir = (p: PlanePoint): number => {
            const dx = p.x - startPoint.x;
            const dz = p.z - startPoint.z;
            return dx * direction.x + dz * direction.z;
          };
          const u0 = dotDir(startPlus);
          const u1 = dotDir(endPlus);
          const u2 = dotDir(endMinus);
          const u3 = dotDir(startMinus);
          const us = [u0, u1, u2, u3];
          for (let i = 0; i < 4; i++) {
            uvs[i * 2 + 0] = us[i];
            uvs[i * 2 + 1] = 0;
            uvs[(4 + i) * 2 + 0] = us[i];
            uvs[(4 + i) * 2 + 1] = height;
          }

          const indices = [
            // top (clockwise in XZ => +Y)
            4, 5, 6, 4, 6, 7,
            // bottom (reverse)
            0, 2, 1, 0, 3, 2,
            // sides
            0, 1, 5, 0, 5, 4,
            1, 2, 6, 1, 6, 5,
            2, 3, 7, 2, 7, 6,
            3, 0, 4, 3, 4, 7,
          ];

          const nextGeometry = new THREE.BufferGeometry();
          nextGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
          nextGeometry.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
          nextGeometry.setIndex(indices);
          nextGeometry.computeVertexNormals();

          const old = mesh.geometry as ThreeTypes.BufferGeometry;
          mesh.geometry = nextGeometry;
          old.dispose();
        }

        material.color.set(color);
        if (material.map !== nextMap) {
          material.map = nextMap;
          material.needsUpdate = true;
        }
        material.roughness = textureId === "brick" ? 0.9 : 0.82;
        material.metalness = textureId === "brick" ? 0.02 : 0.05;
      }

      apply(element);
      return {
        object: group,
        update: apply,
        dispose: () => {
          (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
          material.dispose();
        },
      };
    },
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
      const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
      const color = readString(element.props.color, DEFAULT_WALL_COLOR);
      const widthWorld = readNumber(element.props.width, DEFAULT_WALL_WIDTH);

      const pa = viewport.worldToScreen(startPoint);
      const pb = viewport.worldToScreen(endPoint);
      const widthPx = Math.max(2, widthWorld * viewport.scale);

      canvasContext.save();
      canvasContext.lineCap = "round";
      canvasContext.lineJoin = "round";
      canvasContext.strokeStyle = "rgba(0,0,0,0.35)";
      canvasContext.lineWidth = widthPx + 3;
      canvasContext.beginPath();
      canvasContext.moveTo(pa.x, pa.y);
      canvasContext.lineTo(pb.x, pb.y);
      canvasContext.stroke();

      canvasContext.strokeStyle = color;
      canvasContext.lineWidth = widthPx;
      canvasContext.beginPath();
      canvasContext.moveTo(pa.x, pa.y);
      canvasContext.lineTo(pb.x, pb.y);
      canvasContext.stroke();
      canvasContext.restore();
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <WallEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

type WallEditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function WallEditor({ element, update, remove, close, i18n }: WallEditorProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const color = readString(element.props.color, DEFAULT_WALL_COLOR);
  const texture = readWallTextureId(element.props.texture, "none");

  const numberFormatter = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  const wallLengthMeters = useMemo(() => {
    const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
    const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
    return distanceBetweenPoints(startPoint, endPoint);
  }, [element]);

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">{t("ext.structural.wall.name")}</div>
          <div className="cardMeta">{numberFormatter.format(wallLengthMeters)} m</div>
        </div>
        <div className="cardBody">{t("ext.structural.editor.preview")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.structural.editor.wall_color")}</div>
          <input
            className="input"
            type="color"
            value={color}
            onChange={(e) => update({ props: { color: e.target.value } })}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.structural.editor.wall_texture")}</div>
          <select
            className="input"
            value={texture}
            onChange={(e) => update({ props: { texture: readWallTextureId(e.target.value, texture) } })}
          >
            <option value="none">{t("ext.structural.editor.texture.none")}</option>
            <option value="brick">{t("ext.structural.editor.texture.brick")}</option>
            <option value="concrete">{t("ext.structural.editor.texture.concrete")}</option>
          </select>
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
