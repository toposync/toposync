import type {
  EditorTool,
  EditorToolContext,
  EditorToolPointerEvent,
  HostI18n,
  PlanePoint,
} from "@toposync/plugin-api";

import { rgbaFromHex } from "../colors";
import {
  AREA_ELEMENT_TYPE_ID,
  AREA_POLYGON_TOOL_ID,
  AREA_POLYGON_WITH_WALLS_TOOL_ID,
  AREA_SQUARE_TOOL_ID,
  AREA_SQUARE_WITH_WALLS_TOOL_ID,
  DEFAULT_AREA_OPACITY,
  DEFAULT_POOL_DEPTH_METERS,
  DEFAULT_WALL_COLOR,
  DEFAULT_WALL_WIDTH,
  POOL_ELEMENT_TYPE_ID,
  POOL_POLYGON_TOOL_ID,
  POOL_SQUARE_TOOL_ID,
  WALL_DOOR_TOOL_ID,
  WALL_ELEMENT_TYPE_ID,
  WALL_OPENING_TOOL_ID,
  WALL_TOOL_ID,
  WALL_WINDOW_TOOL_ID,
} from "../constants";
import { centerOfPoints, distanceBetweenPoints } from "../geometry";
import {
  drawPolygonMeasurementOverlay,
  drawSegmentLengthLabel,
} from "../measurementOverlay";
import { loadAreaFillColor, readNumber, readPlanePoint } from "../parsing";
import {
  createDefaultOpening,
  MIN_OPENING_WIDTH_M,
  openingsToProps,
  readWallOpenings,
  type WallOpeningKind,
} from "../wallOpenings";
import { findNearestWallEndpoint, findNearestWallNode } from "../wallGeometry";

const TOOL_GROUP_STRUCTURE: NonNullable<EditorTool["group"]> = {
  id: "structure",
  name: { key: "core.ui.tools.group.structure", fallback: "Structure" },
  order: 20,
};

const TOOL_GROUP_AREAS: NonNullable<EditorTool["group"]> = {
  id: "areas",
  name: { key: "core.ui.tools.group.areas", fallback: "Areas" },
  order: 30,
};

export function createStructuralTools(i18n: HostI18n): EditorTool[] {
  return [
    createWallTool(i18n),
    createWallOpeningTool(i18n, { kind: "door" }),
    createWallOpeningTool(i18n, { kind: "window" }),
    createWallOpeningTool(i18n, { kind: "opening" }),
    createAreaRectangleTool(i18n, { withWalls: true }),
    createAreaPolygonTool(i18n, { withWalls: true }),
    createAreaRectangleTool(i18n, { withWalls: false }),
    createAreaPolygonTool(i18n, { withWalls: false }),
    createPoolSquareTool(i18n),
    createPoolPolygonTool(i18n),
  ];
}

function createWallElement(
  toolContext: EditorToolContext,
  startPoint: PlanePoint,
  endPoint: PlanePoint,
): string | null {
  const center = {
    x: (startPoint.x + endPoint.x) / 2,
    z: (startPoint.z + endPoint.z) / 2,
  };
  return toolContext.createElement(WALL_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: {
      a: startPoint,
      b: endPoint,
      color: DEFAULT_WALL_COLOR,
      width: DEFAULT_WALL_WIDTH,
      openings: [],
    },
  });
}

function createAreaElement(
  toolContext: EditorToolContext,
  vertices: PlanePoint[],
): string | null {
  const center = centerOfPoints(vertices);
  const fill = loadAreaFillColor();
  return toolContext.createElement(AREA_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: { vertices, fill, opacity: DEFAULT_AREA_OPACITY },
  });
}

function createPoolElement(
  toolContext: EditorToolContext,
  vertices: PlanePoint[],
): string | null {
  const center = centerOfPoints(vertices);
  return toolContext.createElement(POOL_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: { vertices, depth_m: DEFAULT_POOL_DEPTH_METERS },
  });
}

function createWallsForPolygon(
  toolContext: EditorToolContext,
  vertices: PlanePoint[],
): void {
  const n = vertices.length;
  if (n < 2) return;
  for (let i = 0; i < n; i++) {
    const startPoint = vertices[i];
    const endPoint = vertices[(i + 1) % n];
    createWallElement(toolContext, startPoint, endPoint);
  }
}

function snapPointForAreaTool(
  toolContext: EditorToolContext,
  event: EditorToolPointerEvent,
): PlanePoint {
  if (event.altKey) return event.rawWorld;
  const nodeSnap = findNearestWallNode(
    toolContext.getElements(),
    event.rawWorld,
  );
  return nodeSnap?.point ?? event.world;
}

type WallTarget = {
  id: string;
  a: PlanePoint;
  b: PlanePoint;
  dir: PlanePoint;
  normal: PlanePoint;
  length: number;
  width: number;
  props: Record<string, unknown>;
};

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function dot(a: PlanePoint, b: PlanePoint): number {
  return a.x * b.x + a.z * b.z;
}

function add(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x + b.x, z: a.z + b.z };
}

function sub(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x - b.x, z: a.z - b.z };
}

function mul(v: PlanePoint, scalar: number): PlanePoint {
  return { x: v.x * scalar, z: v.z * scalar };
}

function nearestPointOnSegment(
  point: PlanePoint,
  a: PlanePoint,
  dir: PlanePoint,
  length: number,
): { point: PlanePoint; scalar: number } {
  const projected = dot(sub(point, a), dir);
  const scalar = Math.max(0, Math.min(length, projected));
  return { point: add(a, mul(dir, scalar)), scalar };
}

function distancePointToSegment(
  point: PlanePoint,
  a: PlanePoint,
  b: PlanePoint,
): number {
  const len = distanceBetweenPoints(a, b);
  if (len <= 1e-6) return distanceBetweenPoints(point, a);
  const dir = { x: (b.x - a.x) / len, z: (b.z - a.z) / len };
  const nearest = nearestPointOnSegment(point, a, dir, len).point;
  return distanceBetweenPoints(point, nearest);
}

function readWalls(toolContext: EditorToolContext): WallTarget[] {
  const out: WallTarget[] = [];
  for (const el of toolContext.getElements()) {
    if (el.type !== WALL_ELEMENT_TYPE_ID) continue;
    const a = readPlanePoint(el.props.a, {
      x: el.position.x - 0.5,
      z: el.position.z,
    });
    const b = readPlanePoint(el.props.b, {
      x: el.position.x + 0.5,
      z: el.position.z,
    });
    const length = distanceBetweenPoints(a, b);
    if (length <= 1e-6) continue;
    const dir = { x: (b.x - a.x) / length, z: (b.z - a.z) / length };
    const normal = { x: -dir.z, z: dir.x };
    out.push({
      id: el.id,
      a,
      b,
      dir,
      normal,
      length,
      width: Math.max(0.04, readNumber(el.props.width, DEFAULT_WALL_WIDTH)),
      props: el.props,
    });
  }
  return out;
}

function pickWallTarget(
  walls: WallTarget[],
  world: PlanePoint,
): WallTarget | null {
  let best: WallTarget | null = null;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const wall of walls) {
    const dist = distancePointToSegment(world, wall.a, wall.b);
    const threshold = Math.max(0.45, wall.width * 3.5);
    if (dist > threshold) continue;
    if (dist < bestDist) {
      bestDist = dist;
      best = wall;
    }
  }
  return best;
}

function kindStyle(kind: WallOpeningKind): {
  stroke: string;
  fill: string;
  dash: number[];
  labelKey: string;
  fallback: string;
  descriptionKey: string;
  descriptionFallback: string;
  icon: string;
  order: number;
} {
  if (kind === "door") {
    return {
      stroke: rgbaFromHex("#fb923c", 0.92),
      fill: rgbaFromHex("#fb923c", 0.22),
      dash: [8, 6],
      labelKey: "ext.structural.tools.wall_door",
      fallback: "Door",
      descriptionKey: "ext.structural.tools.wall_door_desc",
      descriptionFallback: "Place a door cutout on a wall.",
      icon: "door-open",
      order: 20,
    };
  }
  if (kind === "window") {
    return {
      stroke: rgbaFromHex("#38bdf8", 0.92),
      fill: rgbaFromHex("#38bdf8", 0.22),
      dash: [5, 5],
      labelKey: "ext.structural.tools.wall_window",
      fallback: "Window",
      descriptionKey: "ext.structural.tools.wall_window_desc",
      descriptionFallback: "Place a window cutout on a wall.",
      icon: "window-maximize",
      order: 30,
    };
  }
  return {
    stroke: rgbaFromHex("#fbbf24", 0.92),
    fill: rgbaFromHex("#fbbf24", 0.22),
    dash: [9, 4],
    labelKey: "ext.structural.tools.wall_opening",
    fallback: "Opening",
    descriptionKey: "ext.structural.tools.wall_opening_desc",
    descriptionFallback:
      "Hover a wall, click and drag to size. Click to place default width.",
    icon: "crop-simple",
    order: 40,
  };
}

function createWallOpeningTool(
  i18n: HostI18n,
  options: { kind: WallOpeningKind },
): EditorTool {
  const style = kindStyle(options.kind);
  const toolId =
    options.kind === "door"
      ? WALL_DOOR_TOOL_ID
      : options.kind === "window"
        ? WALL_WINDOW_TOOL_ID
        : WALL_OPENING_TOOL_ID;

  return {
    id: toolId,
    name: { key: style.labelKey, fallback: style.fallback },
    description: {
      key: style.descriptionKey,
      fallback: style.descriptionFallback,
    },
    icon: style.icon,
    group: TOOL_GROUP_STRUCTURE,
    order: style.order,
    createSession: (toolContext) => {
      let hoverWall: WallTarget | null = null;
      let hoverScalar: number | null = null;
      let dragWall: WallTarget | null = null;
      let dragStartScalar: number | null = null;
      let dragCurrentScalar: number | null = null;
      let dragSymmetric = false;

      function clearDrag(): void {
        dragWall = null;
        dragStartScalar = null;
        dragCurrentScalar = null;
        dragSymmetric = false;
      }

      function defaultWidthForKind(kind: WallOpeningKind): number {
        if (kind === "door") return 0.9;
        if (kind === "window") return 1.2;
        return 1.0;
      }

      function openingBand(
        length: number,
        scalarA: number,
        scalarB: number,
        symmetric: boolean,
      ): { start: number; end: number; center: number; width: number } {
        const minWidth = Math.min(Math.max(MIN_OPENING_WIDTH_M, 0.2), length);
        const delta = Math.abs(scalarB - scalarA);

        let width = delta;
        let center = (scalarA + scalarB) / 2;
        if (symmetric) {
          width = delta * 2;
          center = scalarA;
        } else if (delta < 0.04) {
          width = Math.min(
            length,
            Math.max(minWidth, defaultWidthForKind(options.kind)),
          );
          center = scalarA;
        }

        width = clamp(width, minWidth, length);
        center = clamp(center, width / 2, length - width / 2);
        return {
          start: center - width / 2,
          end: center + width / 2,
          center,
          width,
        };
      }

      function updateHover(world: PlanePoint): void {
        if (dragWall) return;
        const walls = readWalls(toolContext);
        const selected = pickWallTarget(walls, world);
        hoverWall = selected;
        hoverScalar = selected
          ? nearestPointOnSegment(
              world,
              selected.a,
              selected.dir,
              selected.length,
            ).scalar
          : null;
      }

      function commit(): void {
        if (!dragWall || dragStartScalar == null || dragCurrentScalar == null)
          return;
        const band = openingBand(
          dragWall.length,
          dragStartScalar,
          dragCurrentScalar,
          dragSymmetric,
        );

        const latest = toolContext
          .getElements()
          .find((item) => item.id === dragWall?.id);
        const current = readWallOpenings(
          latest?.props.openings ?? dragWall.props.openings,
        );
        const opening = createDefaultOpening({
          kind: options.kind,
          center_m: band.center,
          width_m: band.width,
        });
        toolContext.updateElement(dragWall.id, {
          props: { openings: openingsToProps([...current, opening]) },
        });
        toolContext.openEditor(dragWall.id);
      }

      function drawWallFocus(
        canvasContext: CanvasRenderingContext2D,
        viewport: {
          worldToScreen: (p: PlanePoint) => { x: number; y: number };
        },
        wall: WallTarget,
      ): void {
        const wa = viewport.worldToScreen(wall.a);
        const wb = viewport.worldToScreen(wall.b);
        canvasContext.beginPath();
        canvasContext.moveTo(wa.x, wa.y);
        canvasContext.lineTo(wb.x, wb.y);
        canvasContext.strokeStyle = "rgba(255,255,255,0.55)";
        canvasContext.lineWidth = 2.25;
        canvasContext.setLineDash([6, 6]);
        canvasContext.stroke();
      }

      function drawOpeningPreview(
        canvasContext: CanvasRenderingContext2D,
        viewport: {
          worldToScreen: (p: PlanePoint) => { x: number; y: number };
          scale: number;
        },
        wall: WallTarget,
        startScalar: number,
        endScalar: number,
        symmetric: boolean,
      ): void {
        const band = openingBand(
          wall.length,
          startScalar,
          endScalar,
          symmetric,
        );
        const startPoint = add(wall.a, mul(wall.dir, band.start));
        const endPoint = add(wall.a, mul(wall.dir, band.end));
        const halfThickness = Math.max(0.09, wall.width / 2);

        const p0 = add(startPoint, mul(wall.normal, halfThickness));
        const p1 = add(endPoint, mul(wall.normal, halfThickness));
        const p2 = add(endPoint, mul(wall.normal, -halfThickness));
        const p3 = add(startPoint, mul(wall.normal, -halfThickness));

        const points = [p0, p1, p2, p3].map((point) =>
          viewport.worldToScreen(point),
        );
        canvasContext.beginPath();
        canvasContext.moveTo(points[0].x, points[0].y);
        for (let i = 1; i < points.length; i++)
          canvasContext.lineTo(points[i].x, points[i].y);
        canvasContext.closePath();
        canvasContext.fillStyle = style.fill;
        canvasContext.fill();
        canvasContext.strokeStyle = style.stroke;
        canvasContext.lineWidth = 2;
        canvasContext.setLineDash(style.dash);
        canvasContext.stroke();
        canvasContext.setLineDash([]);

        const labelCenter = viewport.worldToScreen(
          add(wall.a, mul(wall.dir, band.center)),
        );
        const labelText = `${band.width.toFixed(2)} m`;
        canvasContext.font = "12px ui-sans-serif, system-ui";
        const textWidth = canvasContext.measureText(labelText).width;
        const boxWidth = textWidth + 16;
        const boxHeight = 20;
        const x = labelCenter.x - boxWidth / 2;
        const y =
          labelCenter.y - Math.max(28, 24 + 12 / Math.max(1, viewport.scale));

        canvasContext.fillStyle = "rgba(8,12,26,0.86)";
        canvasContext.fillRect(x, y, boxWidth, boxHeight);
        canvasContext.strokeStyle = "rgba(255,255,255,0.18)";
        canvasContext.lineWidth = 1;
        canvasContext.strokeRect(x + 0.5, y + 0.5, boxWidth - 1, boxHeight - 1);
        canvasContext.fillStyle = "rgba(230,232,242,0.95)";
        canvasContext.textAlign = "center";
        canvasContext.textBaseline = "middle";
        canvasContext.fillText(labelText, labelCenter.x, y + boxHeight / 2);
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            clearDrag();
            return;
          }
          if (event.kind === "move") {
            if (dragWall) {
              dragSymmetric = event.shiftKey;
              dragCurrentScalar = nearestPointOnSegment(
                event.world,
                dragWall.a,
                dragWall.dir,
                dragWall.length,
              ).scalar;
              return;
            }
            updateHover(event.world);
            return;
          }
          if (event.kind === "down") {
            if (event.button !== 0) return;
            updateHover(event.world);
            if (!hoverWall || hoverScalar == null) {
              clearDrag();
              return;
            }
            dragWall = hoverWall;
            dragStartScalar = hoverScalar;
            dragCurrentScalar = hoverScalar;
            dragSymmetric = event.shiftKey;
            return;
          }
          if (event.kind === "up") {
            if (event.button !== 0) return;
            if (!dragWall || dragStartScalar == null) return;
            dragSymmetric = event.shiftKey;
            dragCurrentScalar = nearestPointOnSegment(
              event.world,
              dragWall.a,
              dragWall.dir,
              dragWall.length,
            ).scalar;
            commit();
            clearDrag();
            updateHover(event.world);
          }
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") clearDrag();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          canvasContext.save();
          if (
            dragWall &&
            dragStartScalar != null &&
            dragCurrentScalar != null
          ) {
            drawWallFocus(canvasContext, viewport, dragWall);
            drawOpeningPreview(
              canvasContext,
              viewport,
              dragWall,
              dragStartScalar,
              dragCurrentScalar,
              dragSymmetric,
            );
          } else if (hoverWall && hoverScalar != null) {
            drawWallFocus(canvasContext, viewport, hoverWall);
            drawOpeningPreview(
              canvasContext,
              viewport,
              hoverWall,
              hoverScalar,
              hoverScalar,
              false,
            );
          }
          canvasContext.restore();
        },
        getCursor: () => (hoverWall ? "copy" : "crosshair"),
      };
    },
  };
}

function createWallTool(i18n: HostI18n): EditorTool {
  return {
    id: WALL_TOOL_ID,
    name: { key: "ext.structural.tools.wall", fallback: "Wall" },
    icon: "grip-lines",
    group: TOOL_GROUP_STRUCTURE,
    order: 10,
    createSession: (toolContext) => {
      let startPoint: PlanePoint | null = null;
      let currentPoint: PlanePoint | null = null;
      let lastViewportScale = 0;

      function reset() {
        startPoint = null;
        currentPoint = null;
      }

      function snapPointForWallTool(event: EditorToolPointerEvent): PlanePoint {
        if (event.altKey) return event.rawWorld;
        const radiusMeters = Math.max(
          0.08,
          lastViewportScale > 0 ? 12 / lastViewportScale : 0,
        );
        const endpointSnap = findNearestWallEndpoint(
          toolContext.getElements(),
          event.rawWorld,
          radiusMeters,
        );
        return endpointSnap?.point ?? event.world;
      }

      function commit(endPoint: PlanePoint) {
        if (!startPoint) return;
        if (distanceBetweenPoints(startPoint, endPoint) < 0.05) {
          reset();
          return;
        }
        createWallElement(toolContext, startPoint, endPoint);
        reset();
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            reset();
            return;
          }
          if (event.kind === "move") {
            if (startPoint) currentPoint = snapPointForWallTool(event);
            return;
          }
          if (event.kind !== "down") return;
          if (event.button !== 0) return;

          if (!startPoint) {
            const point = snapPointForWallTool(event);
            startPoint = point;
            currentPoint = point;
            return;
          }
          commit(snapPointForWallTool(event));
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") reset();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          lastViewportScale = viewport.scale;
          if (!startPoint || !currentPoint) return;
          const a = viewport.worldToScreen(startPoint);
          const b = viewport.worldToScreen(currentPoint);
          const width = Math.max(2, DEFAULT_WALL_WIDTH * viewport.scale);

          canvasContext.save();
          canvasContext.setLineDash([8, 6]);
          canvasContext.lineCap = "round";
          canvasContext.strokeStyle = rgbaFromHex("#fbbf24", 0.85);
          canvasContext.lineWidth = width;
          canvasContext.beginPath();
          canvasContext.moveTo(a.x, a.y);
          canvasContext.lineTo(b.x, b.y);
          canvasContext.stroke();
          canvasContext.restore();

          drawSegmentLengthLabel({
            ctx: canvasContext,
            viewport,
            aWorld: startPoint,
            bWorld: currentPoint,
            i18n,
          });
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function createAreaRectangleTool(
  i18n: HostI18n,
  options: { withWalls: boolean },
): EditorTool {
  return {
    id: options.withWalls
      ? AREA_SQUARE_WITH_WALLS_TOOL_ID
      : AREA_SQUARE_TOOL_ID,
    name: {
      key: options.withWalls
        ? "ext.structural.tools.area_square_walls"
        : "ext.structural.tools.area_square",
      fallback: options.withWalls ? "Rectangular room" : "Rectangular area",
    },
    icon: options.withWalls ? "border-all" : "vector-square",
    group: TOOL_GROUP_AREAS,
    order: options.withWalls ? 10 : 30,
    createSession: (toolContext) => {
      let startPoint: PlanePoint | null = null;
      let currentPoint: PlanePoint | null = null;

      function reset() {
        startPoint = null;
        currentPoint = null;
      }

      function rectVertices(a: PlanePoint, b: PlanePoint): PlanePoint[] {
        const x0 = Math.min(a.x, b.x);
        const x1 = Math.max(a.x, b.x);
        const z0 = Math.min(a.z, b.z);
        const z1 = Math.max(a.z, b.z);
        return [
          { x: x0, z: z0 },
          { x: x1, z: z0 },
          { x: x1, z: z1 },
          { x: x0, z: z1 },
        ];
      }

      function commit(endPoint: PlanePoint) {
        if (!startPoint) return;
        const vertices = rectVertices(startPoint, endPoint);
        if (distanceBetweenPoints(vertices[0], vertices[2]) < 0.12) {
          reset();
          return;
        }
        const areaId = createAreaElement(toolContext, vertices);
        if (options.withWalls) {
          createWallsForPolygon(toolContext, vertices);
        }
        if (areaId) toolContext.openEditor(areaId);
        reset();
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            reset();
            return;
          }
          if (event.kind === "move") {
            if (startPoint)
              currentPoint = snapPointForAreaTool(toolContext, event);
            return;
          }
          if (event.kind !== "down") return;
          if (event.button !== 0) return;

          if (!startPoint) {
            const point = snapPointForAreaTool(toolContext, event);
            startPoint = point;
            currentPoint = point;
            return;
          }
          commit(snapPointForAreaTool(toolContext, event));
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") reset();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (!startPoint || !currentPoint) return;
          const vertices = [
            startPoint,
            { x: currentPoint.x, z: startPoint.z },
            currentPoint,
            { x: startPoint.x, z: currentPoint.z },
          ];
          const pts = vertices.map((p) => viewport.worldToScreen(p));

          canvasContext.save();
          canvasContext.beginPath();
          canvasContext.moveTo(pts[0].x, pts[0].y);
          for (let i = 1; i < pts.length; i++)
            canvasContext.lineTo(pts[i].x, pts[i].y);
          canvasContext.closePath();
          canvasContext.fillStyle = rgbaFromHex("#fbbf24", 0.12);
          canvasContext.fill();
          canvasContext.strokeStyle = rgbaFromHex("#fbbf24", 0.8);
          canvasContext.lineWidth = 2;
          canvasContext.setLineDash([6, 6]);
          canvasContext.stroke();
          canvasContext.restore();

          drawPolygonMeasurementOverlay({
            ctx: canvasContext,
            viewport,
            vertices,
            i18n,
            includeClosing: true,
            includeArea: true,
          });
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function createPoolSquareTool(i18n: HostI18n): EditorTool {
  return {
    id: POOL_SQUARE_TOOL_ID,
    name: {
      key: "ext.structural.tools.pool_square",
      fallback: "Rectangular pool",
    },
    icon: "water-ladder",
    group: TOOL_GROUP_AREAS,
    order: 50,
    createSession: (toolContext) => {
      let startPoint: PlanePoint | null = null;
      let currentPoint: PlanePoint | null = null;

      function reset() {
        startPoint = null;
        currentPoint = null;
      }

      function rectVertices(a: PlanePoint, b: PlanePoint): PlanePoint[] {
        const x0 = Math.min(a.x, b.x);
        const x1 = Math.max(a.x, b.x);
        const z0 = Math.min(a.z, b.z);
        const z1 = Math.max(a.z, b.z);
        return [
          { x: x0, z: z0 },
          { x: x1, z: z0 },
          { x: x1, z: z1 },
          { x: x0, z: z1 },
        ];
      }

      function commit(endPoint: PlanePoint) {
        if (!startPoint) return;
        const vertices = rectVertices(startPoint, endPoint);
        if (distanceBetweenPoints(vertices[0], vertices[2]) < 0.12) {
          reset();
          return;
        }
        const poolId = createPoolElement(toolContext, vertices);
        if (poolId) toolContext.openEditor(poolId);
        reset();
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            reset();
            return;
          }
          if (event.kind === "move") {
            if (startPoint) currentPoint = event.world;
            return;
          }
          if (event.kind !== "down") return;
          if (event.button !== 0) return;

          if (!startPoint) {
            startPoint = event.world;
            currentPoint = event.world;
            return;
          }
          commit(event.world);
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") reset();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (!startPoint || !currentPoint) return;
          const vertices = [
            startPoint,
            { x: currentPoint.x, z: startPoint.z },
            currentPoint,
            { x: startPoint.x, z: currentPoint.z },
          ];
          const points = vertices.map((p) => viewport.worldToScreen(p));

          canvasContext.save();
          canvasContext.beginPath();
          canvasContext.moveTo(points[0].x, points[0].y);
          for (let i = 1; i < points.length; i++)
            canvasContext.lineTo(points[i].x, points[i].y);
          canvasContext.closePath();
          canvasContext.fillStyle = rgbaFromHex("#0ea5e9", 0.12);
          canvasContext.fill();
          canvasContext.strokeStyle = rgbaFromHex("#38bdf8", 0.8);
          canvasContext.lineWidth = 2;
          canvasContext.setLineDash([6, 6]);
          canvasContext.stroke();
          canvasContext.restore();
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function createAreaPolygonTool(
  i18n: HostI18n,
  options: { withWalls: boolean },
): EditorTool {
  return {
    id: options.withWalls
      ? AREA_POLYGON_WITH_WALLS_TOOL_ID
      : AREA_POLYGON_TOOL_ID,
    name: {
      key: options.withWalls
        ? "ext.structural.tools.area_polygon_walls"
        : "ext.structural.tools.area_polygon",
      fallback: options.withWalls ? "Freeform room" : "Freeform area",
    },
    icon: options.withWalls ? "object-group" : "draw-polygon",
    group: TOOL_GROUP_AREAS,
    order: options.withWalls ? 20 : 40,
    createSession: (toolContext) => {
      const vertices: PlanePoint[] = [];
      let hoverPoint: PlanePoint | null = null;

      function reset() {
        vertices.splice(0, vertices.length);
        hoverPoint = null;
      }

      function commit() {
        if (vertices.length < 3) {
          reset();
          return;
        }
        const areaId = createAreaElement(toolContext, [...vertices]);
        if (options.withWalls) {
          createWallsForPolygon(toolContext, vertices);
        }
        if (areaId) toolContext.openEditor(areaId);
        reset();
      }

      function shouldCloseByClick(point: PlanePoint): boolean {
        if (vertices.length < 3) return false;
        const first = vertices[0];
        return distanceBetweenPoints(point, first) < 0.22;
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            reset();
            return;
          }
          if (event.kind === "move") {
            hoverPoint = snapPointForAreaTool(toolContext, event);
            return;
          }
          if (event.kind === "dblclick") {
            commit();
            return;
          }
          if (event.kind !== "down") return;
          if (event.button !== 0) return;

          const point = snapPointForAreaTool(toolContext, event);
          if (shouldCloseByClick(point)) {
            commit();
            return;
          }
          vertices.push(point);
          hoverPoint = point;
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") reset();
          if (event.key === "Enter") commit();
          if (
            (event.key === "Backspace" || event.key === "Delete") &&
            vertices.length > 0
          )
            vertices.pop();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (vertices.length === 0) return;
          const pts = vertices.map((p) => viewport.worldToScreen(p));
          const preview = hoverPoint
            ? viewport.worldToScreen(hoverPoint)
            : null;
          const measurementVertices = hoverPoint
            ? [...vertices, hoverPoint]
            : [...vertices];

          canvasContext.save();

          if (vertices.length >= 2 || preview) {
            canvasContext.beginPath();
            canvasContext.moveTo(pts[0].x, pts[0].y);
            for (let i = 1; i < pts.length; i++)
              canvasContext.lineTo(pts[i].x, pts[i].y);
            if (preview) canvasContext.lineTo(preview.x, preview.y);
            canvasContext.strokeStyle = rgbaFromHex("#fbbf24", 0.85);
            canvasContext.lineWidth = 2;
            canvasContext.setLineDash([6, 6]);
            canvasContext.stroke();
          }

          if (measurementVertices.length >= 3) {
            const first = pts[0];
            const last = preview ?? pts[pts.length - 1];
            canvasContext.beginPath();
            canvasContext.moveTo(last.x, last.y);
            canvasContext.lineTo(first.x, first.y);
            canvasContext.strokeStyle = rgbaFromHex("#fbbf24", 0.34);
            canvasContext.lineWidth = 1.5;
            canvasContext.setLineDash([4, 7]);
            canvasContext.stroke();
            canvasContext.setLineDash([]);
          }

          for (let i = 0; i < pts.length; i++) {
            const p = pts[i];
            const isFirst = i === 0 && vertices.length >= 3;
            canvasContext.beginPath();
            canvasContext.arc(p.x, p.y, isFirst ? 6 : 5, 0, Math.PI * 2);
            canvasContext.fillStyle = isFirst
              ? rgbaFromHex("#22c55e", 0.85)
              : rgbaFromHex("#fbbf24", 0.85);
            canvasContext.fill();
            canvasContext.strokeStyle = "rgba(0,0,0,0.35)";
            canvasContext.lineWidth = 2;
            canvasContext.stroke();
          }

          canvasContext.restore();

          drawPolygonMeasurementOverlay({
            ctx: canvasContext,
            viewport,
            vertices: measurementVertices,
            i18n,
            includeClosing: measurementVertices.length >= 3,
            includeArea: measurementVertices.length >= 3,
          });
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function createPoolPolygonTool(i18n: HostI18n): EditorTool {
  return {
    id: POOL_POLYGON_TOOL_ID,
    name: {
      key: "ext.structural.tools.pool_polygon",
      fallback: "Freeform pool",
    },
    icon: "water",
    group: TOOL_GROUP_AREAS,
    order: 60,
    createSession: (toolContext) => {
      const vertices: PlanePoint[] = [];
      let hoverPoint: PlanePoint | null = null;

      function reset() {
        vertices.splice(0, vertices.length);
        hoverPoint = null;
      }

      function commit() {
        if (vertices.length < 3) {
          reset();
          return;
        }
        const poolId = createPoolElement(toolContext, [...vertices]);
        if (poolId) toolContext.openEditor(poolId);
        reset();
      }

      function shouldCloseByClick(event: EditorToolPointerEvent): boolean {
        if (vertices.length < 3) return false;
        const first = vertices[0];
        return distanceBetweenPoints(event.world, first) < 0.22;
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            reset();
            return;
          }
          if (event.kind === "move") {
            hoverPoint = event.world;
            return;
          }
          if (event.kind === "dblclick") {
            commit();
            return;
          }
          if (event.kind !== "down") return;
          if (event.button !== 0) return;

          if (shouldCloseByClick(event)) {
            commit();
            return;
          }
          vertices.push(event.world);
          hoverPoint = event.world;
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") reset();
          if (event.key === "Enter") commit();
          if (
            (event.key === "Backspace" || event.key === "Delete") &&
            vertices.length > 0
          )
            vertices.pop();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (vertices.length === 0) return;
          const points = vertices.map((p) => viewport.worldToScreen(p));
          const preview = hoverPoint
            ? viewport.worldToScreen(hoverPoint)
            : null;

          canvasContext.save();

          if (vertices.length >= 2) {
            canvasContext.beginPath();
            canvasContext.moveTo(points[0].x, points[0].y);
            for (let i = 1; i < points.length; i++)
              canvasContext.lineTo(points[i].x, points[i].y);
            if (preview) canvasContext.lineTo(preview.x, preview.y);
            canvasContext.strokeStyle = rgbaFromHex("#38bdf8", 0.85);
            canvasContext.lineWidth = 2;
            canvasContext.setLineDash([6, 6]);
            canvasContext.stroke();
          }

          for (let i = 0; i < points.length; i++) {
            const point = points[i];
            const isFirst = i === 0 && vertices.length >= 3;
            canvasContext.beginPath();
            canvasContext.arc(
              point.x,
              point.y,
              isFirst ? 6 : 5,
              0,
              Math.PI * 2,
            );
            canvasContext.fillStyle = isFirst
              ? rgbaFromHex("#22c55e", 0.85)
              : rgbaFromHex("#38bdf8", 0.85);
            canvasContext.fill();
            canvasContext.strokeStyle = "rgba(0,0,0,0.35)";
            canvasContext.lineWidth = 2;
            canvasContext.stroke();
          }

          canvasContext.restore();
        },
        getCursor: () => "crosshair",
      };
    },
  };
}
