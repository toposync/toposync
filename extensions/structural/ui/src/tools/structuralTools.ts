import type { EditorTool, EditorToolContext, EditorToolPointerEvent, HostI18n, PlanePoint } from "@toposync/plugin-api";

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
import { loadAreaFillColor, readNumber, readPlanePoint } from "../parsing";
import { createDefaultOpening, MIN_OPENING_WIDTH_M, openingsToProps, readWallOpenings, type WallOpeningKind } from "../wallOpenings";

export function createStructuralTools(i18n: HostI18n): EditorTool[] {
  return [
    createWallTool(i18n),
    createWallOpeningTool(i18n, { kind: "opening" }),
    createWallOpeningTool(i18n, { kind: "door" }),
    createWallOpeningTool(i18n, { kind: "window" }),
    createAreaRectangleTool(i18n, { withWalls: false }),
    createAreaPolygonTool(i18n, { withWalls: false }),
    createPoolSquareTool(i18n),
    createPoolPolygonTool(i18n),
    createAreaRectangleTool(i18n, { withWalls: true }),
    createAreaPolygonTool(i18n, { withWalls: true }),
  ];
}

function createWallElement(
  toolContext: EditorToolContext,
  startPoint: PlanePoint,
  endPoint: PlanePoint,
): string | null {
  const center = { x: (startPoint.x + endPoint.x) / 2, z: (startPoint.z + endPoint.z) / 2 };
  return toolContext.createElement(WALL_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: { a: startPoint, b: endPoint, color: DEFAULT_WALL_COLOR, width: DEFAULT_WALL_WIDTH, openings: [] },
  });
}

function createAreaElement(toolContext: EditorToolContext, vertices: PlanePoint[]): string | null {
  const center = centerOfPoints(vertices);
  const fill = loadAreaFillColor();
  return toolContext.createElement(AREA_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: { vertices, fill, opacity: DEFAULT_AREA_OPACITY },
  });
}

function createPoolElement(toolContext: EditorToolContext, vertices: PlanePoint[]): string | null {
  const center = centerOfPoints(vertices);
  return toolContext.createElement(POOL_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: { vertices, depth_m: DEFAULT_POOL_DEPTH_METERS },
  });
}

function createWallsForPolygon(toolContext: EditorToolContext, vertices: PlanePoint[]): void {
  const n = vertices.length;
  if (n < 2) return;
  for (let i = 0; i < n; i++) {
    const startPoint = vertices[i];
    const endPoint = vertices[(i + 1) % n];
    createWallElement(toolContext, startPoint, endPoint);
  }
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

function nearestPointOnSegment(point: PlanePoint, a: PlanePoint, dir: PlanePoint, length: number): { point: PlanePoint; scalar: number } {
  const projected = dot(sub(point, a), dir);
  const scalar = Math.max(0, Math.min(length, projected));
  return { point: add(a, mul(dir, scalar)), scalar };
}

function distancePointToSegment(point: PlanePoint, a: PlanePoint, b: PlanePoint): number {
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
    const a = readPlanePoint(el.props.a, { x: el.position.x - 0.5, z: el.position.z });
    const b = readPlanePoint(el.props.b, { x: el.position.x + 0.5, z: el.position.z });
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

function pickWallTarget(walls: WallTarget[], world: PlanePoint): WallTarget | null {
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

function kindStyle(kind: WallOpeningKind): { stroke: string; fill: string; dash: number[]; labelKey: string; fallback: string; icon: string } {
  if (kind === "door") {
    return {
      stroke: rgbaFromHex("#fb923c", 0.92),
      fill: rgbaFromHex("#fb923c", 0.22),
      dash: [8, 6],
      labelKey: "ext.structural.tools.wall_door",
      fallback: "Door",
      icon: "door-open",
    };
  }
  if (kind === "window") {
    return {
      stroke: rgbaFromHex("#38bdf8", 0.92),
      fill: rgbaFromHex("#38bdf8", 0.22),
      dash: [5, 5],
      labelKey: "ext.structural.tools.wall_window",
      fallback: "Window",
      icon: "window-maximize",
    };
  }
  return {
    stroke: rgbaFromHex("#fbbf24", 0.92),
    fill: rgbaFromHex("#fbbf24", 0.22),
    dash: [9, 4],
    labelKey: "ext.structural.tools.wall_opening",
    fallback: "Opening",
    icon: "vectors",
  };
}

function createWallOpeningTool(i18n: HostI18n, options: { kind: WallOpeningKind }): EditorTool {
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
    icon: style.icon,
    createSession: (toolContext) => {
      let wall: WallTarget | null = null;
      let startPoint: PlanePoint | null = null;
      let currentPoint: PlanePoint | null = null;

      function reset(): void {
        wall = null;
        startPoint = null;
        currentPoint = null;
      }

      function commit(endPoint: PlanePoint): void {
        if (!wall || !startPoint) return;
        const aProjected = nearestPointOnSegment(startPoint, wall.a, wall.dir, wall.length).scalar;
        const bProjected = nearestPointOnSegment(endPoint, wall.a, wall.dir, wall.length).scalar;
        const start = Math.min(aProjected, bProjected);
        const end = Math.max(aProjected, bProjected);
        const width = end - start;
        if (width < MIN_OPENING_WIDTH_M) {
          reset();
          return;
        }
        const center = (start + end) / 2;

        const current = readWallOpenings(wall.props.openings);
        const opening = createDefaultOpening({ kind: options.kind, center_m: center, width_m: width });
        toolContext.updateElement(wall.id, {
          props: { openings: openingsToProps([...current, opening]) },
        });
        toolContext.openEditor(wall.id);
        reset();
      }

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            reset();
            return;
          }
          if (event.kind === "down") {
            if (event.button !== 0) return;
            const walls = readWalls(toolContext);
            const selected = pickWallTarget(walls, event.world);
            if (!selected) {
              reset();
              return;
            }
            wall = selected;
            startPoint = event.world;
            currentPoint = event.world;
            return;
          }
          if (event.kind === "move") {
            if (!startPoint || !wall) return;
            currentPoint = event.world;
            return;
          }
          if (event.kind === "up") {
            if (event.button !== 0) return;
            if (!startPoint || !wall) return;
            commit(event.world);
          }
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") reset();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (!wall || !startPoint || !currentPoint) return;
          const aProjected = nearestPointOnSegment(startPoint, wall.a, wall.dir, wall.length).point;
          const bProjected = nearestPointOnSegment(currentPoint, wall.a, wall.dir, wall.length).point;
          const halfThickness = Math.max(0.09, wall.width / 2);
          const p0 = add(aProjected, mul(wall.normal, halfThickness));
          const p1 = add(bProjected, mul(wall.normal, halfThickness));
          const p2 = add(bProjected, mul(wall.normal, -halfThickness));
          const p3 = add(aProjected, mul(wall.normal, -halfThickness));
          const points = [p0, p1, p2, p3].map((p) => viewport.worldToScreen(p));

          canvasContext.save();
          canvasContext.beginPath();
          canvasContext.moveTo(points[0].x, points[0].y);
          for (let i = 1; i < points.length; i++) canvasContext.lineTo(points[i].x, points[i].y);
          canvasContext.closePath();
          canvasContext.fillStyle = style.fill;
          canvasContext.fill();
          canvasContext.strokeStyle = style.stroke;
          canvasContext.lineWidth = 2;
          canvasContext.setLineDash(style.dash);
          canvasContext.stroke();
          canvasContext.restore();
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function createWallTool(i18n: HostI18n): EditorTool {
  return {
    id: WALL_TOOL_ID,
    name: { key: "ext.structural.tools.wall", fallback: "Wall" },
    icon: "ruler-combined",
    createSession: (toolContext) => {
      let startPoint: PlanePoint | null = null;
      let currentPoint: PlanePoint | null = null;

      function reset() {
        startPoint = null;
        currentPoint = null;
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
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function createAreaRectangleTool(i18n: HostI18n, options: { withWalls: boolean }): EditorTool {
  return {
    id: options.withWalls ? AREA_SQUARE_WITH_WALLS_TOOL_ID : AREA_SQUARE_TOOL_ID,
    name: {
      key: options.withWalls ? "ext.structural.tools.area_square_walls" : "ext.structural.tools.area_square",
      fallback: options.withWalls ? "Area + walls (rectangle)" : "Area (rectangle)",
    },
    icon: options.withWalls ? "draw-polygon" : "square",
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
          const pts = vertices.map((p) => viewport.worldToScreen(p));

          canvasContext.save();
          canvasContext.beginPath();
          canvasContext.moveTo(pts[0].x, pts[0].y);
          for (let i = 1; i < pts.length; i++) canvasContext.lineTo(pts[i].x, pts[i].y);
          canvasContext.closePath();
          canvasContext.fillStyle = rgbaFromHex("#fbbf24", 0.12);
          canvasContext.fill();
          canvasContext.strokeStyle = rgbaFromHex("#fbbf24", 0.8);
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

function createPoolSquareTool(i18n: HostI18n): EditorTool {
  return {
    id: POOL_SQUARE_TOOL_ID,
    name: { key: "ext.structural.tools.pool_square", fallback: "Pool (rectangle)" },
    icon: "droplet",
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
          for (let i = 1; i < points.length; i++) canvasContext.lineTo(points[i].x, points[i].y);
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

function createAreaPolygonTool(i18n: HostI18n, options: { withWalls: boolean }): EditorTool {
  return {
    id: options.withWalls ? AREA_POLYGON_WITH_WALLS_TOOL_ID : AREA_POLYGON_TOOL_ID,
    name: {
      key: options.withWalls ? "ext.structural.tools.area_polygon_walls" : "ext.structural.tools.area_polygon",
      fallback: options.withWalls ? "Area + walls (polygon)" : "Area (polygon)",
    },
    icon: "draw-polygon",
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
          if ((event.key === "Backspace" || event.key === "Delete") && vertices.length > 0) vertices.pop();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (vertices.length === 0) return;
          const pts = vertices.map((p) => viewport.worldToScreen(p));
          const preview = hoverPoint ? viewport.worldToScreen(hoverPoint) : null;

          canvasContext.save();

          if (vertices.length >= 2) {
            canvasContext.beginPath();
            canvasContext.moveTo(pts[0].x, pts[0].y);
            for (let i = 1; i < pts.length; i++) canvasContext.lineTo(pts[i].x, pts[i].y);
            if (preview) canvasContext.lineTo(preview.x, preview.y);
            canvasContext.strokeStyle = rgbaFromHex("#fbbf24", 0.85);
            canvasContext.lineWidth = 2;
            canvasContext.setLineDash([6, 6]);
            canvasContext.stroke();
          }

          for (let i = 0; i < pts.length; i++) {
            const p = pts[i];
            const isFirst = i === 0 && vertices.length >= 3;
            canvasContext.beginPath();
            canvasContext.arc(p.x, p.y, isFirst ? 6 : 5, 0, Math.PI * 2);
            canvasContext.fillStyle = isFirst ? rgbaFromHex("#22c55e", 0.85) : rgbaFromHex("#fbbf24", 0.85);
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

function createPoolPolygonTool(i18n: HostI18n): EditorTool {
  return {
    id: POOL_POLYGON_TOOL_ID,
    name: { key: "ext.structural.tools.pool_polygon", fallback: "Pool (polygon)" },
    icon: "droplet",
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
          if ((event.key === "Backspace" || event.key === "Delete") && vertices.length > 0) vertices.pop();
        },
        renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
          if (vertices.length === 0) return;
          const points = vertices.map((p) => viewport.worldToScreen(p));
          const preview = hoverPoint ? viewport.worldToScreen(hoverPoint) : null;

          canvasContext.save();

          if (vertices.length >= 2) {
            canvasContext.beginPath();
            canvasContext.moveTo(points[0].x, points[0].y);
            for (let i = 1; i < points.length; i++) canvasContext.lineTo(points[i].x, points[i].y);
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
            canvasContext.arc(point.x, point.y, isFirst ? 6 : 5, 0, Math.PI * 2);
            canvasContext.fillStyle = isFirst ? rgbaFromHex("#22c55e", 0.85) : rgbaFromHex("#38bdf8", 0.85);
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
