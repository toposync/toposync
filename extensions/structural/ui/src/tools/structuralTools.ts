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
  WALL_ELEMENT_TYPE_ID,
  WALL_TOOL_ID,
} from "../constants";
import { centerOfPoints, distanceBetweenPoints } from "../geometry";
import { loadAreaFillColor } from "../parsing";

export function createStructuralTools(i18n: HostI18n): EditorTool[] {
  return [
    createWallTool(i18n),
    createAreaSquareTool(i18n, { withWalls: false }),
    createAreaPolygonTool(i18n, { withWalls: false }),
    createPoolSquareTool(i18n),
    createPoolPolygonTool(i18n),
    createAreaSquareTool(i18n, { withWalls: true }),
    createAreaPolygonTool(i18n, { withWalls: true }),
  ];
}

function createWallElement(
  toolContext: EditorToolContext,
  startPoint: PlanePoint,
  endPoint: PlanePoint,
  join?: { previousStartPoint?: PlanePoint; nextEndPoint?: PlanePoint },
): string | null {
  const center = { x: (startPoint.x + endPoint.x) / 2, z: (startPoint.z + endPoint.z) / 2 };
  const joinProps: Record<string, unknown> = {};
  if (join?.previousStartPoint) joinProps.a_prev = join.previousStartPoint;
  if (join?.nextEndPoint) joinProps.b_next = join.nextEndPoint;
  return toolContext.createElement(WALL_ELEMENT_TYPE_ID, {
    name: "",
    position: { x: center.x, y: 0, z: center.z },
    props: { a: startPoint, b: endPoint, color: DEFAULT_WALL_COLOR, width: DEFAULT_WALL_WIDTH, ...joinProps },
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
    const previousStartPoint = vertices[(i - 1 + n) % n];
    const nextEndPoint = vertices[(i + 2) % n];
    createWallElement(toolContext, startPoint, endPoint, { previousStartPoint, nextEndPoint });
  }
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

function createAreaSquareTool(i18n: HostI18n, options: { withWalls: boolean }): EditorTool {
  return {
    id: options.withWalls ? AREA_SQUARE_WITH_WALLS_TOOL_ID : AREA_SQUARE_TOOL_ID,
    name: {
      key: options.withWalls ? "ext.structural.tools.area_square_walls" : "ext.structural.tools.area_square",
      fallback: options.withWalls ? "Area + walls (square)" : "Area (square)",
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
