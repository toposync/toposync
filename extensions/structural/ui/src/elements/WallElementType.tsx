import React, { useMemo } from "react";
import type * as ThreeTypes from "three";

import type { BoundsXZ, CompositionElement, CompositionElementPatch, ElementType, HostI18n, PlanePoint } from "@toposync/plugin-api";

import { rgbaFromHex, shadeHex } from "../colors";
import { DEFAULT_WALL_COLOR, DEFAULT_WALL_WIDTH, GROUND_Y, WALL_ELEMENT_TYPE_ID } from "../constants";
import { addPoints, distanceBetweenPoints, normalizePoint, perpendicularPoint, scalePoint, subtractPoints } from "../geometry";
import { readNumber, readPlanePoint, readString } from "../parsing";
import { getOpeningTexture, getWallTexture, readOpeningTextureId, readWallTextureId } from "../textures";
import {
  createDefaultOpening,
  defaultColorForKind,
  defaultTextureForKind,
  MIN_OPENING_WIDTH_M,
  openingsToProps,
  readWallOpenings,
  resolveWallOpenings,
  type ResolvedWallOpening,
  type WallOpening,
  type WallOpeningKind,
} from "../wallOpenings";

type WallInterval = { start: number; end: number };
type SolidRect = { x0: number; x1: number; y0: number; y1: number };

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function sortAndMergeIntervals(intervals: WallInterval[], min: number, max: number): WallInterval[] {
  const filtered = intervals
    .map((interval) => ({
      start: clamp(Math.min(interval.start, interval.end), min, max),
      end: clamp(Math.max(interval.start, interval.end), min, max),
    }))
    .filter((interval) => interval.end - interval.start > 1e-6)
    .sort((a, b) => a.start - b.start);

  const merged: WallInterval[] = [];
  for (const interval of filtered) {
    const last = merged[merged.length - 1];
    if (!last || interval.start > last.end + 1e-6) {
      merged.push(interval);
      continue;
    }
    last.end = Math.max(last.end, interval.end);
  }
  return merged;
}

function subtractIntervals(length: number, blocked: WallInterval[]): WallInterval[] {
  const merged = sortAndMergeIntervals(blocked, 0, length);
  const solids: WallInterval[] = [];
  let cursor = 0;
  for (const interval of merged) {
    if (interval.start > cursor + 1e-6) solids.push({ start: cursor, end: interval.start });
    cursor = Math.max(cursor, interval.end);
  }
  if (cursor < length - 1e-6) solids.push({ start: cursor, end: length });
  return solids;
}

function buildSolidRects(length: number, height: number, openings: ResolvedWallOpening[]): SolidRect[] {
  const yCuts = [0, height];
  for (const opening of openings) {
    yCuts.push(clamp(opening.y_min_m, 0, height));
    yCuts.push(clamp(opening.y_max_m, 0, height));
  }
  yCuts.sort((a, b) => a - b);

  const uniqueYCuts: number[] = [];
  for (const cut of yCuts) {
    if (uniqueYCuts.length === 0 || Math.abs(cut - uniqueYCuts[uniqueYCuts.length - 1]) > 1e-6) uniqueYCuts.push(cut);
  }

  const rects: SolidRect[] = [];
  for (let i = 0; i < uniqueYCuts.length - 1; i++) {
    const y0 = uniqueYCuts[i];
    const y1 = uniqueYCuts[i + 1];
    if (y1 - y0 <= 1e-6) continue;

    const blocked: WallInterval[] = [];
    for (const opening of openings) {
      if (opening.y_min_m >= y1 - 1e-6 || opening.y_max_m <= y0 + 1e-6) continue;
      blocked.push({ start: opening.start_m, end: opening.end_m });
    }

    const solids = subtractIntervals(length, blocked);
    for (const solid of solids) {
      if (solid.end - solid.start <= 1e-6) continue;
      rects.push({ x0: solid.start, x1: solid.end, y0, y1 });
    }
  }

  return rects;
}

function pointAtDistance(start: PlanePoint, direction: PlanePoint, distanceMeters: number): PlanePoint {
  return addPoints(start, scalePoint(direction, distanceMeters));
}

function wallBounds(element: CompositionElement): BoundsXZ {
  const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
  const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
  const width = Math.max(0.04, readNumber(element.props.width, DEFAULT_WALL_WIDTH));
  return {
    minX: Math.min(startPoint.x, endPoint.x) - width,
    maxX: Math.max(startPoint.x, endPoint.x) + width,
    minZ: Math.min(startPoint.z, endPoint.z) - width,
    maxZ: Math.max(startPoint.z, endPoint.z) + width,
  };
}

function svgPoints(points: PlanePoint[]): string {
  return points.map((point) => `${point.x},${point.z}`).join(" ");
}

function distPointToSegment(point: PlanePoint, a: PlanePoint, b: PlanePoint): number {
  const ab = subtractPoints(b, a);
  const ap = subtractPoints(point, a);
  const denom = ab.x * ab.x + ab.z * ab.z;
  if (denom <= 1e-9) return Math.hypot(ap.x, ap.z);
  const t = clamp((ap.x * ab.x + ap.z * ab.z) / denom, 0, 1);
  const q = addPoints(a, scalePoint(ab, t));
  return distanceBetweenPoints(point, q);
}

function disposeMaterial(material: ThreeTypes.Material | ThreeTypes.Material[], seen: Set<ThreeTypes.Material>): void {
  if (Array.isArray(material)) {
    for (const item of material) {
      if (seen.has(item)) continue;
      seen.add(item);
      item.dispose();
    }
    return;
  }
  if (seen.has(material)) return;
  seen.add(material);
  material.dispose();
}

function applyWorldUvToBoxGeometry(
  geometry: ThreeTypes.BufferGeometry,
  args: { originX: number; originY: number; originZ: number; thicknessWorld: number },
): void {
  const pos = geometry.getAttribute("position") as ThreeTypes.BufferAttribute | null;
  const norm = geometry.getAttribute("normal") as ThreeTypes.BufferAttribute | null;
  const uv = geometry.getAttribute("uv") as ThreeTypes.BufferAttribute | null;
  if (!pos || !norm || !uv) return;

  const zOffset = args.thicknessWorld / 2;

  for (let i = 0; i < uv.count; i += 1) {
    const nx = norm.getX(i);
    const ny = norm.getY(i);
    const nz = norm.getZ(i);
    const px = pos.getX(i);
    const py = pos.getY(i);
    const pz = pos.getZ(i);

    let u = 0;
    let v = 0;
    if (Math.abs(nz) >= 0.5) {
      // Front/back wall faces: map X (length) and Y (height) in world meters.
      u = px + args.originX;
      v = py + args.originY;
    } else if (Math.abs(nx) >= 0.5) {
      // Side faces: map Z (thickness) and Y (height).
      u = pz + args.originZ + zOffset;
      v = py + args.originY;
    } else {
      // Top/bottom faces: map X (length) and Z (thickness).
      u = px + args.originX;
      v = pz + args.originZ + zOffset;
    }

    uv.setXY(i, u, v);
  }
  uv.needsUpdate = true;
}

function clearMeshGroup(group: ThreeTypes.Group, disposeMaterials: boolean): void {
  const materialSeen = new Set<ThreeTypes.Material>();
  for (const child of [...group.children]) {
    const mesh = child as ThreeTypes.Mesh;
    if (mesh.geometry && "dispose" in mesh.geometry) (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
    if (disposeMaterials && mesh.material) disposeMaterial(mesh.material as ThreeTypes.Material | ThreeTypes.Material[], materialSeen);
    group.remove(child);
  }
}

function openingPreviewStyle(kind: WallOpeningKind): { fill: string; stroke: string; dash: number[] } {
  if (kind === "door") return { fill: "rgba(251,146,60,0.20)", stroke: "rgba(251,146,60,0.92)", dash: [7, 4] };
  if (kind === "window") return { fill: "rgba(56,189,248,0.18)", stroke: "rgba(56,189,248,0.92)", dash: [5, 4] };
  return { fill: "rgba(251,191,36,0.12)", stroke: "rgba(251,191,36,0.92)", dash: [9, 5] };
}

function readOpeningTextureForKind(kind: WallOpeningKind, value: unknown): ReturnType<typeof readOpeningTextureId> {
  const texture = readOpeningTextureId(value, defaultTextureForKind(kind));
  if (kind !== "window" && texture === "transparent") return defaultTextureForKind(kind);
  return texture;
}

function openingVectorStyle(opening: ResolvedWallOpening): { fill: string; stroke: string; accent: string; highlight: string } {
  const fallback = defaultColorForKind(opening.kind) ?? "#cbd5e1";
  const color = opening.color ?? fallback;
  const texture = readOpeningTextureForKind(opening.kind, opening.texture);

  if (opening.kind === "window") {
    if (texture === "transparent") {
      return {
        fill: "rgba(255,255,255,0.02)",
        stroke: rgbaFromHex(shadeHex(color, -0.08), 0.34),
        accent: "rgba(255,255,255,0.08)",
        highlight: "rgba(255,255,255,0.10)",
      };
    }
    const glass = texture === "glass" ? color : "#b9d8f4";
    return {
      fill: rgbaFromHex(glass, 0.30),
      stroke: rgbaFromHex(shadeHex(glass, -0.16), 0.56),
      accent: rgbaFromHex(shadeHex(glass, 0.34), 0.82),
      highlight: "rgba(255,255,255,0.38)",
    };
  }

  if (opening.kind === "door") {
    return {
      fill: rgbaFromHex(color, texture === "wood" ? 0.50 : 0.38),
      stroke: rgbaFromHex(shadeHex(color, -0.28), 0.52),
      accent: rgbaFromHex(shadeHex(color, 0.26), 0.42),
      highlight: "rgba(255,255,255,0.16)",
    };
  }

  return {
    fill: "rgba(255,255,255,0.08)",
    stroke: rgbaFromHex(color, 0.26),
    accent: rgbaFromHex(shadeHex(color, 0.20), 0.22),
    highlight: "rgba(255,255,255,0.10)",
  };
}

function readOpeningKind(value: unknown, fallback: WallOpeningKind): WallOpeningKind {
  return value === "opening" || value === "door" || value === "window" ? value : fallback;
}

function addDoorInsert(
  THREE: typeof import("three"),
  group: ThreeTypes.Group,
  opening: ResolvedWallOpening,
  thicknessWorld: number,
): void {
  const openingHeight = opening.y_max_m - opening.y_min_m;
  if (opening.width_m <= 0.12 || openingHeight <= 0.12) return;

  const frameThickness = clamp(Math.min(opening.width_m, openingHeight) * 0.11, 0.03, 0.09);
  const frameDepth = thicknessWorld * 0.48;
  const panelDepth = thicknessWorld * 0.34;

  const textureId = readOpeningTextureForKind("door", opening.texture);
  const textureMap = getOpeningTexture(THREE, textureId);

  const frameMaterial = new THREE.MeshStandardMaterial({
    color: "#5b5044",
    roughness: 0.82,
    metalness: 0.05,
    map: textureMap,
  });
  const panelMaterial = new THREE.MeshStandardMaterial({
    color: opening.color ?? defaultColorForKind("door") ?? "#8f806a",
    roughness: 0.74,
    metalness: 0.08,
    map: textureMap,
  });

  const leftGeometry = new THREE.BoxGeometry(frameThickness, openingHeight, frameDepth);
  const rightGeometry = new THREE.BoxGeometry(frameThickness, openingHeight, frameDepth);
  const topGeometry = new THREE.BoxGeometry(opening.width_m, frameThickness, frameDepth);

  const left = new THREE.Mesh(leftGeometry, frameMaterial);
  const right = new THREE.Mesh(rightGeometry, frameMaterial);
  const top = new THREE.Mesh(topGeometry, frameMaterial);
  for (const mesh of [left, right, top]) (mesh.userData as any).__toposyncAllowPickWhenGhostWalls = true;

  const centerY = GROUND_Y + opening.y_min_m + openingHeight / 2;
  left.position.set(opening.start_m + frameThickness / 2, centerY, 0);
  right.position.set(opening.end_m - frameThickness / 2, centerY, 0);
  top.position.set(opening.center_m, GROUND_Y + opening.y_max_m - frameThickness / 2, 0);

  // Keep the door panel nearly flush with the frame while avoiding coplanar faces (z-fighting).
  const seam = clamp(frameThickness * 0.02, 0.001, 0.004);
  const panelWidth = opening.width_m - frameThickness * 2 - seam * 2;
  const panelHeight = openingHeight - frameThickness - seam;
  if (panelWidth > 0.08 && panelHeight > 0.2) {
    const panelGeometry = new THREE.BoxGeometry(panelWidth, panelHeight, panelDepth);
    const panel = new THREE.Mesh(panelGeometry, panelMaterial);
    (panel.userData as any).__toposyncAllowPickWhenGhostWalls = true;
    panel.position.set(opening.center_m, GROUND_Y + opening.y_min_m + panelHeight / 2, 0);
    panel.castShadow = true;
    panel.receiveShadow = true;
    group.add(panel);
  } else {
    panelMaterial.dispose();
  }

  for (const mesh of [left, right, top]) {
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    group.add(mesh);
  }
}

function addWindowInsert(
  THREE: typeof import("three"),
  group: ThreeTypes.Group,
  opening: ResolvedWallOpening,
  thicknessWorld: number,
): void {
  const openingHeight = opening.y_max_m - opening.y_min_m;
  if (opening.width_m <= 0.12 || openingHeight <= 0.12) return;

  const frameThickness = clamp(Math.min(opening.width_m, openingHeight) * 0.09, 0.025, 0.07);
  const frameDepth = thicknessWorld * 0.35;
  const paneDepth = thicknessWorld * 0.14;

  const textureId = readOpeningTextureForKind("window", opening.texture);
  const textureMap = getOpeningTexture(THREE, textureId);

  const frameMaterial = new THREE.MeshStandardMaterial({
    color: opening.color ?? defaultColorForKind("window") ?? "#b9d8f4",
    roughness: 0.55,
    metalness: 0.18,
    map: textureMap,
  });

  const topGeometry = new THREE.BoxGeometry(opening.width_m, frameThickness, frameDepth);
  const bottomGeometry = new THREE.BoxGeometry(opening.width_m, frameThickness, frameDepth);
  const leftGeometry = new THREE.BoxGeometry(frameThickness, openingHeight, frameDepth);
  const rightGeometry = new THREE.BoxGeometry(frameThickness, openingHeight, frameDepth);

  const top = new THREE.Mesh(topGeometry, frameMaterial);
  const bottom = new THREE.Mesh(bottomGeometry, frameMaterial);
  const left = new THREE.Mesh(leftGeometry, frameMaterial);
  const right = new THREE.Mesh(rightGeometry, frameMaterial);
  for (const mesh of [top, bottom, left, right]) (mesh.userData as any).__toposyncAllowPickWhenGhostWalls = true;

  const centerY = GROUND_Y + opening.y_min_m + openingHeight / 2;
  top.position.set(opening.center_m, GROUND_Y + opening.y_max_m - frameThickness / 2, 0);
  bottom.position.set(opening.center_m, GROUND_Y + opening.y_min_m + frameThickness / 2, 0);
  left.position.set(opening.start_m + frameThickness / 2, centerY, 0);
  right.position.set(opening.end_m - frameThickness / 2, centerY, 0);

  for (const mesh of [top, bottom, left, right]) {
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    group.add(mesh);
  }

  const paneWidth = opening.width_m - frameThickness * 2.2;
  const paneHeight = openingHeight - frameThickness * 2.2;
  if (textureId === "transparent") return;
  if (paneWidth <= 0.08 || paneHeight <= 0.08) return;

  const paneGeometry = new THREE.BoxGeometry(paneWidth, paneHeight, paneDepth);
  let paneMaterial: ThreeTypes.Material;
  if (textureId === "glass") {
    // Glass should tint the scene without hiding projected floor/video content behind it.
    paneMaterial = new THREE.MeshPhysicalMaterial({
      color: opening.color ?? defaultColorForKind("window") ?? "#b9d8f4",
      roughness: 0.1,
      metalness: 0,
      transmission: 0.88,
      thickness: 0.05,
      transparent: true,
      opacity: 0.55,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
  } else {
    paneMaterial = new THREE.MeshStandardMaterial({
      color: opening.color ?? defaultColorForKind("window") ?? "#b9d8f4",
      roughness: 0.42,
      metalness: 0.12,
      map: textureMap,
    });
  }

  const pane = new THREE.Mesh(paneGeometry, paneMaterial);
  (pane.userData as any).__toposyncAllowPickWhenGhostWalls = true;
  pane.position.set(opening.center_m, centerY, 0);
  if (textureId === "glass") pane.renderOrder = 12;
  pane.castShadow = false;
  pane.receiveShadow = textureId !== "glass";
  group.add(pane);
}

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
      openings: [],
    },
    getMain2DBounds: wallBounds,
    renderMain2DVector: ({ element }) => {
      const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
      const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
      const color = readString(element.props.color, DEFAULT_WALL_COLOR);
      const textureId = readWallTextureId(element.props.texture, "none");
      const widthWorld = Math.max(0.04, readNumber(element.props.width, DEFAULT_WALL_WIDTH));
      const length = Math.max(0.001, distanceBetweenPoints(startPoint, endPoint));
      const direction = normalizePoint(subtractPoints(endPoint, startPoint));
      const normal = perpendicularPoint(direction);
      const openings = resolveWallOpenings(readWallOpenings(element.props.openings), length, 3.2);
      const blocked = openings.map((opening) => ({ start: opening.start_m, end: opening.end_m }));
      const solids = subtractIntervals(length, blocked);
      const openingHalfThickness = Math.max(widthWorld / 2, 0.09);
      const wallFill =
        textureId === "brick"
          ? rgbaFromHex(shadeHex(color, -0.16), 0.98)
          : textureId === "concrete"
            ? rgbaFromHex(shadeHex(color, -0.10), 0.98)
            : rgbaFromHex(shadeHex(color, -0.12), 0.98);

      return (
        <g className="mainVector2dWall">
          {solids.map((interval, index) => {
            const a = pointAtDistance(startPoint, direction, interval.start);
            const b = pointAtDistance(startPoint, direction, interval.end);
            const halfWidth = Math.max(widthWorld / 2, 0.02);
            const p0 = addPoints(a, scalePoint(normal, halfWidth));
            const p1 = addPoints(b, scalePoint(normal, halfWidth));
            const p2 = addPoints(b, scalePoint(normal, -halfWidth));
            const p3 = addPoints(a, scalePoint(normal, -halfWidth));
            return (
              <polygon
                key={`solid:${index}`}
                points={svgPoints([p0, p1, p2, p3])}
                fill={wallFill}
              />
            );
          })}
          {openings.map((opening) => {
            const s = pointAtDistance(startPoint, direction, opening.start_m);
            const e = pointAtDistance(startPoint, direction, opening.end_m);
            const p0 = addPoints(s, scalePoint(normal, openingHalfThickness));
            const p1 = addPoints(e, scalePoint(normal, openingHalfThickness));
            const p2 = addPoints(e, scalePoint(normal, -openingHalfThickness));
            const p3 = addPoints(s, scalePoint(normal, -openingHalfThickness));
            const style = openingVectorStyle(opening);
            const centerA = pointAtDistance(startPoint, direction, opening.start_m + opening.width_m * 0.18);
            const centerB = pointAtDistance(startPoint, direction, opening.end_m - opening.width_m * 0.18);
            return (
              <g key={opening.id} className={`mainVector2dWallOpening mainVector2dWallOpening-${opening.kind}`}>
                <polygon
                  points={svgPoints([p0, p1, p2, p3])}
                  fill={style.fill}
                  stroke={style.stroke}
                  strokeWidth={0.018}
                  vectorEffect="non-scaling-stroke"
                />
                <line
                  x1={centerA.x}
                  y1={centerA.z}
                  x2={centerB.x}
                  y2={centerB.z}
                  stroke={style.accent}
                  strokeWidth={Math.max(0.008, opening.kind === "window" ? widthWorld * 0.32 : widthWorld * 0.18)}
                  strokeLinecap="round"
                />
                {opening.kind === "window" ? (
                  <line
                    x1={addPoints(centerA, scalePoint(normal, -openingHalfThickness * 0.46)).x}
                    y1={addPoints(centerA, scalePoint(normal, -openingHalfThickness * 0.46)).z}
                    x2={addPoints(centerB, scalePoint(normal, -openingHalfThickness * 0.46)).x}
                    y2={addPoints(centerB, scalePoint(normal, -openingHalfThickness * 0.46)).z}
                    stroke={style.highlight}
                    strokeWidth={0.01}
                    strokeLinecap="round"
                    vectorEffect="non-scaling-stroke"
                  />
                ) : null}
              </g>
            );
          })}
        </g>
      );
    },
    create3D: ({ THREE, view }, element) => {
      const root = new THREE.Group();
      const wallAxisGroup = new THREE.Group();
      const solidsGroup = new THREE.Group();
      const insertsGroup = new THREE.Group();
      const picksGroup = new THREE.Group();

      wallAxisGroup.add(solidsGroup);
      wallAxisGroup.add(insertsGroup);
      wallAxisGroup.add(picksGroup);
      root.add(wallAxisGroup);

      const wallMaterial = new THREE.MeshStandardMaterial({
        color: DEFAULT_WALL_COLOR,
        roughness: 0.82,
        metalness: 0.05,
        flatShading: true,
      });

      // Untextured material for thickness faces (sides/top/bottom). Mapping the wall texture on thin
      // faces (especially around openings) looks stretched and inconsistent.
      const wallCapMaterial = new THREE.MeshStandardMaterial({
        color: DEFAULT_WALL_COLOR,
        roughness: 0.86,
        metalness: 0.04,
        flatShading: true,
      });

      // Invisible mesh used only for raycasting/picking in 3D.
      // Without this, clicking through a cutout can "fall through" to the floor/area behind it.
      const pickMaterial = new THREE.MeshBasicMaterial({
        color: "#000000",
        transparent: true,
        opacity: 0,
        depthWrite: false,
      });
      pickMaterial.colorWrite = false;

      let lastKey = "";

      function apply(el: CompositionElement) {
        const startPoint = readPlanePoint(el.props.a, { x: el.position.x - 0.5, z: el.position.z });
        const endPoint = readPlanePoint(el.props.b, { x: el.position.x + 0.5, z: el.position.z });
        const direction = normalizePoint(subtractPoints(endPoint, startPoint));

        const thicknessWorld = Math.max(0.04, readNumber(el.props.width, DEFAULT_WALL_WIDTH));
        const height = Math.max(0.15, view.wallHeight);
        const length = Math.max(0.001, distanceBetweenPoints(startPoint, endPoint));

        const color = readString(el.props.color, DEFAULT_WALL_COLOR);
        const textureId = readWallTextureId(el.props.texture, "none");
        const wallTexture = getWallTexture(THREE, textureId);

        wallAxisGroup.position.set(startPoint.x - el.position.x, GROUND_Y, startPoint.z - el.position.z);
        // Align local +X with the wall direction in XZ (three.js Y-rotation maps +X -> (cos, -sin)).
        wallAxisGroup.rotation.set(0, Math.atan2(-direction.z, direction.x), 0);

        const openings = readWallOpenings(el.props.openings);
        const resolvedOpenings = resolveWallOpenings(openings, length, height);

        const key = JSON.stringify({
          length: round3(length),
          thickness: round3(thicknessWorld),
          height: round3(height),
          openings: resolvedOpenings.map((opening) => ({
            id: opening.id,
            kind: opening.kind,
            start: round3(opening.start_m),
            end: round3(opening.end_m),
            y0: round3(opening.y_min_m),
            y1: round3(opening.y_max_m),
            color: opening.color ?? "",
            texture: opening.texture ?? "",
          })),
        });

        if (key !== lastKey) {
          lastKey = key;
          clearMeshGroup(solidsGroup, false);
          clearMeshGroup(insertsGroup, true);
          clearMeshGroup(picksGroup, false);

          const solidRects = buildSolidRects(length, height, resolvedOpenings);
          for (const rect of solidRects) {
            if (rect.x1 - rect.x0 <= 1e-6 || rect.y1 - rect.y0 <= 1e-6) continue;
            const geometry = new THREE.BoxGeometry(rect.x1 - rect.x0, rect.y1 - rect.y0, thicknessWorld);
            applyWorldUvToBoxGeometry(geometry, {
              originX: (rect.x0 + rect.x1) / 2,
              originY: GROUND_Y + (rect.y0 + rect.y1) / 2,
              originZ: 0,
              thicknessWorld,
            });
            // Always apply the cap material on thickness faces. Otherwise, walls created with "no texture"
            // would keep a single-material mesh and later end up stretching textures on thin faces.
            // BoxGeometry groups: 0 right, 1 left, 2 top, 3 bottom, 4 front, 5 back.
            const material = [wallCapMaterial, wallCapMaterial, wallCapMaterial, wallCapMaterial, wallMaterial, wallMaterial];
            const mesh = new THREE.Mesh(geometry, material);
            mesh.castShadow = true;
            mesh.receiveShadow = true;
            mesh.position.set((rect.x0 + rect.x1) / 2, GROUND_Y + (rect.y0 + rect.y1) / 2, 0);
            solidsGroup.add(mesh);
          }

          for (const opening of resolvedOpenings) {
            if (opening.kind === "door") {
              addDoorInsert(THREE, insertsGroup, opening, thicknessWorld);
              continue;
            }
            if (opening.kind === "window") {
              addWindowInsert(THREE, insertsGroup, opening, thicknessWorld);
            }
          }

          for (const opening of resolvedOpenings) {
            const openingHeight = opening.y_max_m - opening.y_min_m;
            if (opening.width_m <= 0.08 || openingHeight <= 0.08) continue;
            const geometry = new THREE.BoxGeometry(opening.width_m, openingHeight, thicknessWorld * 1.06);
            const mesh = new THREE.Mesh(geometry, pickMaterial);
            (mesh.userData as any).__toposyncAllowPickWhenGhostWalls = true;
            mesh.position.set(opening.center_m, GROUND_Y + opening.y_min_m + openingHeight / 2, 0);
            mesh.castShadow = false;
            mesh.receiveShadow = false;
            picksGroup.add(mesh);
          }
        }

        wallMaterial.color.set(color);
        wallCapMaterial.color.set(color);
        if (wallMaterial.map !== wallTexture) {
          wallMaterial.map = wallTexture;
          wallMaterial.needsUpdate = true;
        }
        wallMaterial.roughness = textureId === "brick" ? 0.9 : 0.82;
        wallMaterial.metalness = textureId === "brick" ? 0.02 : 0.05;
      }

      apply(element);
      return {
        object: root,
        update: apply,
        dispose: () => {
          clearMeshGroup(solidsGroup, false);
          clearMeshGroup(insertsGroup, true);
          clearMeshGroup(picksGroup, false);
          wallMaterial.dispose();
          wallCapMaterial.dispose();
          pickMaterial.dispose();
        },
      };
    },
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
      const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
      const color = readString(element.props.color, DEFAULT_WALL_COLOR);
      const widthWorld = Math.max(0.04, readNumber(element.props.width, DEFAULT_WALL_WIDTH));

      const length = Math.max(0.001, distanceBetweenPoints(startPoint, endPoint));
      const direction = normalizePoint(subtractPoints(endPoint, startPoint));
      const normal = perpendicularPoint(direction);

      const openings = resolveWallOpenings(readWallOpenings(element.props.openings), length, 3.2);
      const blocked = openings.map((opening) => ({ start: opening.start_m, end: opening.end_m }));
      const solids = subtractIntervals(length, blocked);

      const widthPx = Math.max(2, widthWorld * viewport.scale);

      canvasContext.save();
      canvasContext.lineCap = "round";
      canvasContext.lineJoin = "round";

      for (const interval of solids) {
        const a = viewport.worldToScreen(pointAtDistance(startPoint, direction, interval.start));
        const b = viewport.worldToScreen(pointAtDistance(startPoint, direction, interval.end));

        canvasContext.strokeStyle = "rgba(0,0,0,0.35)";
        canvasContext.lineWidth = widthPx + 3;
        canvasContext.beginPath();
        canvasContext.moveTo(a.x, a.y);
        canvasContext.lineTo(b.x, b.y);
        canvasContext.stroke();

        canvasContext.strokeStyle = color;
        canvasContext.lineWidth = widthPx;
        canvasContext.beginPath();
        canvasContext.moveTo(a.x, a.y);
        canvasContext.lineTo(b.x, b.y);
        canvasContext.stroke();
      }

      const openingHalfThickness = Math.max(widthWorld / 2, 0.09);
      for (const opening of openings) {
        const s = pointAtDistance(startPoint, direction, opening.start_m);
        const e = pointAtDistance(startPoint, direction, opening.end_m);
        const p0 = viewport.worldToScreen(addPoints(s, scalePoint(normal, openingHalfThickness)));
        const p1 = viewport.worldToScreen(addPoints(e, scalePoint(normal, openingHalfThickness)));
        const p2 = viewport.worldToScreen(addPoints(e, scalePoint(normal, -openingHalfThickness)));
        const p3 = viewport.worldToScreen(addPoints(s, scalePoint(normal, -openingHalfThickness)));
        const style = openingPreviewStyle(opening.kind);

        canvasContext.beginPath();
        canvasContext.moveTo(p0.x, p0.y);
        canvasContext.lineTo(p1.x, p1.y);
        canvasContext.lineTo(p2.x, p2.y);
        canvasContext.lineTo(p3.x, p3.y);
        canvasContext.closePath();
        canvasContext.fillStyle = style.fill;
        canvasContext.fill();
        canvasContext.strokeStyle = style.stroke;
        canvasContext.lineWidth = 1.8;
        canvasContext.setLineDash(style.dash);
        canvasContext.stroke();
        canvasContext.setLineDash([]);
      }

      canvasContext.restore();
    },
    hitTest2D: ({ element, world, viewport }) => {
      const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
      const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
      const widthWorld = Math.max(0.04, readNumber(element.props.width, DEFAULT_WALL_WIDTH));
      // Walls are stored as a single line segment. Even if the wall has openings, clicking the gap
      // should still select the wall (otherwise selection/double-click falls through to areas).
      //
      // We keep a minimum "grab width" (9cm or 10px) so openings are easy to pick at any zoom.
      const threshold = Math.max(widthWorld / 2, 0.09, 10 / Math.max(1, viewport.scale));
      return distPointToSegment(world, startPoint, endPoint) <= threshold;
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

  const openings = useMemo(() => readWallOpenings(element.props.openings), [element.props.openings]);

  const numberFormatter = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  const wallLengthMeters = useMemo(() => {
    const startPoint = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
    const endPoint = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
    return distanceBetweenPoints(startPoint, endPoint);
  }, [element]);

  const sortedOpenings = useMemo(
    () => [...openings].sort((a, b) => a.center_m - b.center_m || a.id.localeCompare(b.id)),
    [openings],
  );

  const resolvedOpeningsById = useMemo(() => {
    const resolved = resolveWallOpenings(openings, wallLengthMeters, 3.2);
    const out = new Map<string, ResolvedWallOpening>();
    for (const item of resolved) out.set(item.id, item);
    return out;
  }, [openings, wallLengthMeters]);

  function saveOpenings(nextOpenings: WallOpening[]): void {
    update({ props: { openings: openingsToProps(nextOpenings) } });
  }

  function patchOpening(openingId: string, patch: Partial<WallOpening>): void {
    saveOpenings(openings.map((opening) => (opening.id === openingId ? { ...opening, ...patch } : opening)));
  }

  function removeOpening(openingId: string): void {
    saveOpenings(openings.filter((opening) => opening.id !== openingId));
  }

  function addOpening(kind: WallOpeningKind): void {
    const defaultWidth = kind === "door" ? 0.9 : kind === "window" ? 1.2 : 1.0;
    const safeLength = Math.max(0, wallLengthMeters);
    const minWidth = Math.min(MIN_OPENING_WIDTH_M, safeLength);
    const width = clamp(defaultWidth, minWidth, safeLength);
    const opening = createDefaultOpening({
      kind,
      center_m: wallLengthMeters / 2,
      width_m: width,
    });
    saveOpenings([...openings, opening]);
  }

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">{t("ext.structural.wall.name")}</div>
          <div className="cardMeta">{numberFormatter.format(wallLengthMeters)} m</div>
        </div>
        <div className="cardBody">
          {t("ext.structural.editor.preview")} • {sortedOpenings.length} {t("ext.structural.editor.openings")}
        </div>
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

      <div className="rowWrap" style={{ alignItems: "center", justifyContent: "space-between" }}>
        <div className="label">{t("ext.structural.editor.openings")}</div>
        <div className="rowWrap" style={{ gap: 8 }}>
          <button className="chipButton" type="button" onClick={() => addOpening("opening")}>+ {t("ext.structural.tools.wall_opening")}</button>
          <button className="chipButton" type="button" onClick={() => addOpening("door")}>+ {t("ext.structural.tools.wall_door")}</button>
          <button className="chipButton" type="button" onClick={() => addOpening("window")}>+ {t("ext.structural.tools.wall_window")}</button>
        </div>
      </div>

      {sortedOpenings.length === 0 ? <div className="cardBody">{t("ext.structural.editor.openings_empty")}</div> : null}

      {sortedOpenings.map((opening) => {
        const kind = opening.kind;
        const openingTexture = readOpeningTextureForKind(kind, opening.texture);
        const openingColor = opening.color ?? defaultColorForKind(kind) ?? "#8f806a";
        const resolved = resolvedOpeningsById.get(opening.id) ?? null;
        const heightLabel =
          kind === "opening"
            ? t("ext.structural.editor.opening_height_full")
            : resolved
              ? t("ext.structural.editor.opening_height_range", {
                  yMin: numberFormatter.format(resolved.y_min_m),
                  yMax: numberFormatter.format(resolved.y_max_m),
                })
              : null;

        const maxWidth = Math.max(0, wallLengthMeters);
        const minWidth = Math.min(MIN_OPENING_WIDTH_M, maxWidth);
        const widthValue = clamp(opening.width_m, minWidth, Math.max(minWidth, maxWidth));
        const centerMin = widthValue / 2;
        const centerMax = Math.max(centerMin, maxWidth - widthValue / 2);
        const centerValue = clamp(opening.center_m, centerMin, centerMax);
        return (
          <div className="card" key={opening.id} style={{ marginTop: 10 }}>
            <div className="cardHeaderRow">
              <div className="cardTitle">{t(`ext.structural.editor.kind.${kind}`)}</div>
              <div className="cardMeta">{numberFormatter.format(widthValue)} m</div>
            </div>
            {heightLabel ? <div className="cardMeta" style={{ marginTop: 6 }}>{heightLabel}</div> : null}

            <div className="rowWrap">
              <div className="field" style={{ flex: 1, minWidth: 120 }}>
                <div className="label">{t("ext.structural.editor.opening_kind")}</div>
                <select
                  className="input"
                  value={kind}
                  onChange={(e) => {
                    const nextKind = readOpeningKind(e.target.value, kind);
                    const nextPatch: Partial<WallOpening> = { kind: nextKind };
                    if (!opening.color && defaultColorForKind(nextKind)) nextPatch.color = defaultColorForKind(nextKind);
                    if (!opening.texture || opening.texture === defaultTextureForKind(kind)) {
                      nextPatch.texture = defaultTextureForKind(nextKind);
                    }
                    patchOpening(opening.id, nextPatch);
                  }}
                >
                  <option value="opening">{t("ext.structural.editor.kind.opening")}</option>
                  <option value="door">{t("ext.structural.editor.kind.door")}</option>
                  <option value="window">{t("ext.structural.editor.kind.window")}</option>
                </select>
              </div>
              <div className="field" style={{ flex: 1, minWidth: 120 }}>
                <div className="label">{t("ext.structural.editor.opening_center")}</div>
                <input
                  className="input"
                  type="number"
                  step={0.05}
                  value={centerValue}
                  onChange={(e) => {
                    const next = Number(e.target.value);
                    if (!Number.isFinite(next)) return;
                    patchOpening(opening.id, { center_m: clamp(next, centerMin, centerMax) });
                  }}
                />
                <div className="cardMeta">
                  {t("ext.structural.editor.opening_center_hint", {
                    start: numberFormatter.format(0),
                    end: numberFormatter.format(wallLengthMeters),
                  })}
                </div>
              </div>
              <div className="field" style={{ flex: 1, minWidth: 120 }}>
                <div className="label">{t("ext.structural.editor.opening_width")}</div>
                <input
                  className="input"
                  type="number"
                  step={0.05}
                  min={minWidth}
                  max={Math.max(minWidth, maxWidth)}
                  value={widthValue}
                  onChange={(e) => {
                    const next = Number(e.target.value);
                    if (!Number.isFinite(next)) return;
                    const nextWidth = clamp(next, minWidth, Math.max(minWidth, maxWidth));
                    const nextCenterMin = nextWidth / 2;
                    const nextCenterMax = Math.max(nextCenterMin, maxWidth - nextWidth / 2);
                    patchOpening(opening.id, {
                      width_m: nextWidth,
                      center_m: clamp(opening.center_m, nextCenterMin, nextCenterMax),
                    });
                  }}
                />
              </div>
            </div>

            {kind !== "opening" ? (
              <div className="rowWrap">
                <div className="field" style={{ flex: 1, minWidth: 120 }}>
                  <div className="label">{t("ext.structural.editor.opening_color")}</div>
                  <input
                    className="input"
                    type="color"
                    value={openingColor}
                    onChange={(e) => patchOpening(opening.id, { color: e.target.value })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 120 }}>
                  <div className="label">{t("ext.structural.editor.opening_texture")}</div>
                  <select
                    className="input"
                    value={openingTexture}
                    onChange={(e) => patchOpening(opening.id, { texture: readOpeningTextureId(e.target.value, openingTexture) })}
                  >
                    <option value="none">{t("ext.structural.editor.texture.none")}</option>
                    <option value="wood">{t("ext.structural.editor.texture.wood")}</option>
                    <option value="concrete">{t("ext.structural.editor.texture.concrete")}</option>
                    <option value="glass">{t("ext.structural.editor.texture.glass")}</option>
                    {kind === "window" ? (
                      <option value="transparent">{t("ext.structural.editor.texture.transparent_cutout")}</option>
                    ) : null}
                  </select>
                </div>
              </div>
            ) : null}

            <div className="rowWrap">
              <button className="dangerButton" type="button" onClick={() => removeOpening(opening.id)}>
                {t("core.actions.delete")}
              </button>
            </div>
          </div>
        );
      })}

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
