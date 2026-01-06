import React, { useEffect, useRef } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorToolSession,
  ElementType,
  PlanePoint,
  Vector2,
  Viewport2DContext,
} from "@toposync/plugin-api";

import { i18n } from "../util/i18n";

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  activeToolSession?: EditorToolSession | null;
  interactionMode?: "navigate" | "select";
  enableKeyboardShortcuts?: boolean;
  toolSnapToGrid?: boolean;
  selectedElementIds?: string[];
  onSelectElements?: (elementIds: string[]) => void;
  onOpenEditor?: (elementId: string) => void;
  updateElement?: (elementId: string, patch: CompositionElementPatch) => void;
  removeElement?: (elementId: string) => void;
  duplicateElements?: (elements: CompositionElement[]) => string[];
  onBeginUndoGroup?: () => void;
  onEndUndoGroup?: () => void;
  onUndo?: () => void;
  onRedo?: () => void;
};

function toVector2(x: number, y: number): Vector2 {
  return { x, y };
}

function toPlanePoint(x: number, z: number): PlanePoint {
  return { x, z };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return Boolean(v) && typeof v === "object" && !Array.isArray(v);
}

function readNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function readPlanePoint(v: unknown): PlanePoint | null {
  if (!isRecord(v)) return null;
  const x = v.x;
  const z = v.z;
  if (typeof x !== "number" || typeof z !== "number") return null;
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  return { x, z };
}

function readVertices(v: unknown): PlanePoint[] {
  if (!Array.isArray(v)) return [];
  const out: PlanePoint[] = [];
  for (const item of v) {
    const p = readPlanePoint(item);
    if (p) out.push(p);
  }
  return out;
}

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

const SNAP_STEP = 0.1; // meters

function snapScalar(v: number, step: number): number {
  const inv = 1 / step;
  return Math.round(v * inv) / inv;
}

function snapPoint(p: PlanePoint, step: number): PlanePoint {
  return { x: snapScalar(p.x, step), z: snapScalar(p.z, step) };
}

function dot(a: PlanePoint, b: PlanePoint): number {
  return a.x * b.x + a.z * b.z;
}

function sub(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x - b.x, z: a.z - b.z };
}

function add(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x + b.x, z: a.z + b.z };
}

function mul(v: PlanePoint, s: number): PlanePoint {
  return { x: v.x * s, z: v.z * s };
}

function rotateAround(p: PlanePoint, center: PlanePoint, angleRad: number): PlanePoint {
  const dx = p.x - center.x;
  const dz = p.z - center.z;
  const c = Math.cos(angleRad);
  const s = Math.sin(angleRad);
  return { x: center.x + dx * c - dz * s, z: center.z + dx * s + dz * c };
}

function distPointToSegment(p: PlanePoint, a: PlanePoint, b: PlanePoint): number {
  const ab = sub(b, a);
  const ap = sub(p, a);
  const denom = dot(ab, ab);
  if (denom <= 1e-9) return Math.hypot(ap.x, ap.z);
  const t = clamp(dot(ap, ab) / denom, 0, 1);
  const q = add(a, mul(ab, t));
  return Math.hypot(p.x - q.x, p.z - q.z);
}

function pointInPolygon(p: PlanePoint, vertices: PlanePoint[]): boolean {
  let inside = false;
  for (let i = 0, j = vertices.length - 1; i < vertices.length; j = i++) {
    const xi = vertices[i].x;
    const zi = vertices[i].z;
    const xj = vertices[j].x;
    const zj = vertices[j].z;

    const intersects =
      zi > p.z !== zj > p.z && p.x < ((xj - xi) * (p.z - zi)) / (zj - zi + 1e-12) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

function polygonArea(vertices: PlanePoint[]): number {
  if (vertices.length < 3) return 0;
  let sum = 0;
  for (let i = 0; i < vertices.length; i++) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    sum += a.x * b.z - b.x * a.z;
  }
  return Math.abs(sum) / 2;
}

function polygonCentroid(vertices: PlanePoint[]): PlanePoint {
  if (vertices.length === 0) return { x: 0, z: 0 };
  if (vertices.length < 3) {
    const sum = vertices.reduce((acc, p) => ({ x: acc.x + p.x, z: acc.z + p.z }), { x: 0, z: 0 });
    return { x: sum.x / vertices.length, z: sum.z / vertices.length };
  }

  let area2 = 0;
  let cx = 0;
  let cz = 0;
  for (let i = 0; i < vertices.length; i++) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    const cross = a.x * b.z - b.x * a.z;
    area2 += cross;
    cx += (a.x + b.x) * cross;
    cz += (a.z + b.z) * cross;
  }

  if (Math.abs(area2) < 1e-9) {
    const sum = vertices.reduce((acc, p) => ({ x: acc.x + p.x, z: acc.z + p.z }), { x: 0, z: 0 });
    return { x: sum.x / vertices.length, z: sum.z / vertices.length };
  }

  const denom = 3 * area2;
  return { x: cx / denom, z: cz / denom };
}

function roundRectPath(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const anyCtx = ctx as unknown as { roundRect?: (x: number, y: number, w: number, h: number, r: number) => void };
  if (typeof anyCtx.roundRect === "function") {
    anyCtx.roundRect(x, y, w, h, r);
    return;
  }

  const radius = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
}

type Camera2D = { cx: number; cz: number; scale: number };

type Interaction =
  | { kind: "none" }
  | { kind: "tool"; pointerId: number }
  | {
      kind: "select-box";
      pointerId: number;
      startScreen: Vector2;
      currentScreen: Vector2;
      additive: boolean;
      baseSelection: string[];
    }
  | {
      kind: "pan";
      pointerId: number;
      startScreen: Vector2;
      startCamera: Camera2D;
      startedByLeft: boolean;
      moved: boolean;
    }
  | {
      kind: "drag";
      pointerId: number;
      startElements: CompositionElement[];
      targetIds: string[];
      startScreen: Vector2;
      startWorldSnapped: PlanePoint;
      moved: boolean;
      duplicateRequested: boolean;
      duplicated: boolean;
      toggleOffId: string | null;
      selectOnlyOnClickId: string | null;
    }
  | {
      kind: "rotate";
      pointerId: number;
      elementId: string;
      pivot: PlanePoint;
      startAngle: number;
      startElement: CompositionElement;
      currentScreen: Vector2;
      snappedDelta: number;
      stepDeg: number;
    };

export function Viewport2D({
  elements,
  elementTypesById,
  activeToolSession,
  interactionMode = "select",
  enableKeyboardShortcuts = true,
  toolSnapToGrid = true,
  selectedElementIds,
  onSelectElements,
  onOpenEditor,
  updateElement,
  removeElement,
  duplicateElements,
  onBeginUndoGroup,
  onEndUndoGroup,
  onUndo,
  onRedo,
}: Props): React.ReactElement {
  const { locale } = i18n.useI18n();

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawRef = useRef<(() => void) | null>(null);

  const numberFmtRef = useRef<Intl.NumberFormat>(
    new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  );

  const elementsRef = useRef<CompositionElement[]>(elements);
  const elementTypesRef = useRef<Record<string, ElementType>>(elementTypesById);
  const toolSessionRef = useRef<EditorToolSession | null>(activeToolSession ?? null);
  const interactionModeRef = useRef<"navigate" | "select">(interactionMode);
  const enableKeyboardShortcutsRef = useRef<boolean>(enableKeyboardShortcuts);
  const toolSnapToGridRef = useRef<boolean>(toolSnapToGrid);

  const selectedRef = useRef<string[]>(selectedElementIds ?? []);
  const onSelectRef = useRef<Props["onSelectElements"]>(onSelectElements);
  const onOpenEditorRef = useRef<Props["onOpenEditor"]>(onOpenEditor);
  const updateElementRef = useRef<Props["updateElement"]>(updateElement);
  const removeElementRef = useRef<Props["removeElement"]>(removeElement);
  const duplicateElementsRef = useRef<Props["duplicateElements"]>(duplicateElements);
  const onBeginUndoGroupRef = useRef<Props["onBeginUndoGroup"]>(onBeginUndoGroup);
  const onEndUndoGroupRef = useRef<Props["onEndUndoGroup"]>(onEndUndoGroup);
  const onUndoRef = useRef<Props["onUndo"]>(onUndo);
  const onRedoRef = useRef<Props["onRedo"]>(onRedo);

  const cameraRef = useRef<Camera2D>({ cx: 0, cz: 0, scale: 52 });
  const interactionRef = useRef<Interaction>({ kind: "none" });
  const hoverRef = useRef<string | null>(null);
  const rotateHoverRef = useRef(false);
  const spacePressedRef = useRef(false);

  useEffect(() => {
    elementsRef.current = elements;
    const existing = new Set(elements.map((e) => e.id));
    const next = selectedRef.current.filter((id) => existing.has(id));
    if (next.length !== selectedRef.current.length) {
      selectedRef.current = next;
      onSelectRef.current?.(next);
    }
    drawRef.current?.();
  }, [elements]);

  useEffect(() => {
    elementTypesRef.current = elementTypesById;
    drawRef.current?.();
  }, [elementTypesById]);

  useEffect(() => {
    toolSessionRef.current = activeToolSession ?? null;
    drawRef.current?.();
  }, [activeToolSession]);

  useEffect(() => {
    interactionModeRef.current = interactionMode;
    drawRef.current?.();
  }, [interactionMode]);

  useEffect(() => {
    enableKeyboardShortcutsRef.current = enableKeyboardShortcuts;
  }, [enableKeyboardShortcuts]);

  useEffect(() => {
    toolSnapToGridRef.current = toolSnapToGrid;
  }, [toolSnapToGrid]);

  useEffect(() => {
    selectedRef.current = selectedElementIds ?? [];
    rotateHoverRef.current = false;
    drawRef.current?.();
  }, [selectedElementIds]);

  useEffect(() => {
    onSelectRef.current = onSelectElements;
  }, [onSelectElements]);

  useEffect(() => {
    duplicateElementsRef.current = duplicateElements;
  }, [duplicateElements]);

  useEffect(() => {
    onOpenEditorRef.current = onOpenEditor;
  }, [onOpenEditor]);

  useEffect(() => {
    updateElementRef.current = updateElement;
  }, [updateElement]);

  useEffect(() => {
    removeElementRef.current = removeElement;
  }, [removeElement]);

  useEffect(() => {
    onBeginUndoGroupRef.current = onBeginUndoGroup;
  }, [onBeginUndoGroup]);

  useEffect(() => {
    onEndUndoGroupRef.current = onEndUndoGroup;
  }, [onEndUndoGroup]);

  useEffect(() => {
    onUndoRef.current = onUndo;
  }, [onUndo]);

  useEffect(() => {
    onRedoRef.current = onRedo;
  }, [onRedo]);

  useEffect(() => {
    numberFmtRef.current = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    drawRef.current?.();
  }, [locale]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const canvasEl: HTMLCanvasElement = canvas;
    const ctx2d: CanvasRenderingContext2D = ctx;

    let raf = 0;
    let dragRaf = 0;
    let pendingDragPatch: Array<{ id: string; patch: CompositionElementPatch }> | null = null;
    let rotateRaf = 0;
    let pendingRotatePatch: { id: string; patch: CompositionElementPatch } | null = null;

    function requestDraw() {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        draw();
      });
    }

    function flushDragPatch() {
      if (!pendingDragPatch) return;
      for (const item of pendingDragPatch) {
        updateElementRef.current?.(item.id, item.patch);
      }
      pendingDragPatch = null;
      requestDraw();
    }

    function flushRotatePatch() {
      if (!pendingRotatePatch) return;
      updateElementRef.current?.(pendingRotatePatch.id, pendingRotatePatch.patch);
      pendingRotatePatch = null;
      requestDraw();
    }

    drawRef.current = requestDraw;

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = canvasEl.clientWidth;
      const h = canvasEl.clientHeight;
      canvasEl.width = Math.max(1, Math.floor(w * dpr));
      canvasEl.height = Math.max(1, Math.floor(h * dpr));
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      requestDraw();
    }

    function draw() {
      const w = canvasEl.clientWidth;
      const h = canvasEl.clientHeight;

      ctx2d.clearRect(0, 0, w, h);

      const g = ctx2d.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, "#070a14");
      g.addColorStop(1, "#050713");
      ctx2d.fillStyle = g;
      ctx2d.fillRect(0, 0, w, h);

      const originX = w / 2;
      const originY = h / 2;
      const camera = cameraRef.current;
      const scale = camera.scale;
      const cx = camera.cx;
      const cz = camera.cz;

      function worldToScreen(p: PlanePoint): Vector2 {
        return toVector2(originX + (p.x - cx) * scale, originY + (p.z - cz) * scale);
      }

      function screenToWorld(p: Vector2): PlanePoint {
        return toPlanePoint((p.x - originX) / scale + cx, (p.y - originY) / scale + cz);
      }

      const viewport: Viewport2DContext = {
        canvas: canvasEl,
        width: w,
        height: h,
        dpr: window.devicePixelRatio || 1,
        worldToScreen,
        screenToWorld,
        scale,
      };

      const tl = screenToWorld(toVector2(0, 0));
      const br = screenToWorld(toVector2(w, h));
      const minX = Math.min(tl.x, br.x);
      const maxX = Math.max(tl.x, br.x);
      const minZ = Math.min(tl.z, br.z);
      const maxZ = Math.max(tl.z, br.z);

      const targetPx = 52;
      const raw = targetPx / Math.max(1e-6, scale);
      const exp = Math.floor(Math.log10(raw));
      const base = Math.pow(10, exp);
      const frac = raw / base;
      const niceFrac = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
      const majorStep = niceFrac * base;

      function drawGrid(stepWorld: number, style: { stroke: string; width: number }) {
        const inv = 1 / stepWorld;
        const startX = Math.floor(minX * inv);
        const endX = Math.ceil(maxX * inv);
        const startZ = Math.floor(minZ * inv);
        const endZ = Math.ceil(maxZ * inv);

        ctx2d.strokeStyle = style.stroke;
        ctx2d.lineWidth = style.width;

        for (let ix = startX; ix <= endX; ix++) {
          const x = ix / inv;
          const sx = Math.round(worldToScreen(toPlanePoint(x, cz)).x) + 0.5;
          ctx2d.beginPath();
          ctx2d.moveTo(sx, 0);
          ctx2d.lineTo(sx, h);
          ctx2d.stroke();
        }

        for (let iz = startZ; iz <= endZ; iz++) {
          const z = iz / inv;
          const sy = Math.round(worldToScreen(toPlanePoint(cx, z)).y) + 0.5;
          ctx2d.beginPath();
          ctx2d.moveTo(0, sy);
          ctx2d.lineTo(w, sy);
          ctx2d.stroke();
        }
      }

      const minorPx = SNAP_STEP * scale;
      if (minorPx >= 8) {
        drawGrid(SNAP_STEP, { stroke: "rgba(255,255,255,0.028)", width: 1 });
      }
      drawGrid(majorStep, { stroke: "rgba(255,255,255,0.055)", width: 1 });

      ctx2d.strokeStyle = "rgba(251,191,36,0.20)";
      ctx2d.lineWidth = 1.25;
      const axisX = Math.round(worldToScreen(toPlanePoint(0, 0)).x) + 0.5;
      const axisY = Math.round(worldToScreen(toPlanePoint(0, 0)).y) + 0.5;
      ctx2d.beginPath();
      ctx2d.moveTo(axisX, 0);
      ctx2d.lineTo(axisX, h);
      ctx2d.stroke();
      ctx2d.beginPath();
      ctx2d.moveTo(0, axisY);
      ctx2d.lineTo(w, axisY);
      ctx2d.stroke();

      ctx2d.fillStyle = "rgba(251,191,36,0.95)";
      ctx2d.strokeStyle = "rgba(0,0,0,0.35)";
      ctx2d.font = "12px ui-sans-serif, system-ui";

      const groupRank = (typeId: string): number => {
        const group = elementTypesRef.current[typeId]?.layerGroup ?? "";
        if (group === "areas") return 0;
        if (group === "walls") return 1;
        return 2;
      };

      const ordered = elementsRef.current
        .map((el, idx) => ({ el, idx }))
        .sort((a, b) => groupRank(a.el.type) - groupRank(b.el.type) || a.idx - b.idx)
        .map((v) => v.el);

      for (const el of ordered) {
        const def = elementTypesRef.current[el.type];
        if (def?.render2D) {
          try {
            def.render2D({ ctx: ctx2d, element: el, viewport });
          } catch (err) {
            console.error(`[render2D:${el.type}]`, err);
          }
          continue;
        }

        const p = worldToScreen(toPlanePoint(el.position.x, el.position.z));
        ctx2d.beginPath();
        ctx2d.arc(p.x, p.y, 7, 0, Math.PI * 2);
        ctx2d.fill();
        ctx2d.stroke();

        ctx2d.fillStyle = "rgba(230,232,242,0.85)";
        ctx2d.fillText(el.name || el.type, p.x + 10, p.y + 4);
        ctx2d.fillStyle = "rgba(251,191,36,0.95)";
      }

      const selectedIds = selectedRef.current;
      const primaryId = selectedIds.length === 1 ? selectedIds[0] : null;
      const selectedById = selectedIds.length
        ? new Map(elementsRef.current.map((e) => [e.id, e] as const))
        : null;

      if (selectedById) {
        for (const id of selectedIds) {
          const el = selectedById.get(id);
          if (!el) continue;

          const isPrimary = primaryId === id;
          const verts = readVertices(el.props.vertices);
          const a = readPlanePoint(el.props.a);
          const b = readPlanePoint(el.props.b);

          ctx2d.save();
          ctx2d.strokeStyle = isPrimary ? "rgba(251,191,36,0.92)" : "rgba(251,191,36,0.55)";
          ctx2d.lineWidth = isPrimary ? 3 : 2;
          ctx2d.shadowColor = isPrimary ? "rgba(251,191,36,0.35)" : "rgba(0,0,0,0)";
          ctx2d.shadowBlur = isPrimary ? 10 : 0;

          if (verts.length >= 3) {
            const pts = verts.map((p) => worldToScreen(p));
            ctx2d.beginPath();
            ctx2d.moveTo(pts[0].x, pts[0].y);
            for (let i = 1; i < pts.length; i++) ctx2d.lineTo(pts[i].x, pts[i].y);
            ctx2d.closePath();
            ctx2d.stroke();
          } else if (a && b) {
            const pa = worldToScreen(a);
            const pb = worldToScreen(b);
            ctx2d.beginPath();
            ctx2d.moveTo(pa.x, pa.y);
            ctx2d.lineTo(pb.x, pb.y);
            ctx2d.stroke();
          } else {
            const p = worldToScreen(toPlanePoint(el.position.x, el.position.z));
            ctx2d.beginPath();
            ctx2d.arc(p.x, p.y, 11, 0, Math.PI * 2);
            ctx2d.stroke();
          }
          ctx2d.restore();
        }

        if (primaryId) {
          const selectedEl = selectedById.get(primaryId) ?? null;
          if (selectedEl) {
            const group = elementTypesRef.current[selectedEl.type]?.layerGroup ?? "";
            const verts = readVertices(selectedEl.props.vertices);
            const a = readPlanePoint(selectedEl.props.a);
            const b = readPlanePoint(selectedEl.props.b);

            let label: string | null = null;
            let anchorWorld: PlanePoint | null = null;
            if (group === "walls" && a && b) {
              label = `${numberFmtRef.current.format(Math.hypot(a.x - b.x, a.z - b.z))} m`;
              anchorWorld = { x: (a.x + b.x) / 2, z: (a.z + b.z) / 2 };
            } else if (group === "areas" && verts.length >= 3) {
              label = `${numberFmtRef.current.format(polygonArea(verts))} m²`;
              anchorWorld = polygonCentroid(verts);
            }

            if (label && anchorWorld) {
              const anchor = worldToScreen(anchorWorld);
              const anchorX = anchor.x;
              const anchorY = anchor.y - 18;

              ctx2d.save();
              ctx2d.font = "12px ui-sans-serif, system-ui";
              ctx2d.textAlign = "center";
              ctx2d.textBaseline = "middle";

              const metrics = ctx2d.measureText(label);
              const padX = 10;
              const boxW = metrics.width + padX * 2;
              const boxH = 24;
              const x0 = anchorX - boxW / 2;
              const y0 = anchorY - boxH / 2;

              ctx2d.shadowColor = "rgba(0,0,0,0.38)";
              ctx2d.shadowBlur = 14;
              ctx2d.fillStyle = "rgba(8,12,26,0.78)";
              ctx2d.strokeStyle = "rgba(255,255,255,0.14)";
              ctx2d.lineWidth = 1;
              ctx2d.beginPath();
              roundRectPath(ctx2d, x0, y0, boxW, boxH, 999);
              ctx2d.fill();
              ctx2d.shadowBlur = 0;
              ctx2d.stroke();

              ctx2d.fillStyle = "rgba(230,232,242,0.92)";
              ctx2d.fillText(label, anchorX, anchorY);
              ctx2d.restore();
            }

            if (!toolSessionRef.current && interactionModeRef.current === "select") {
              const info = getRotateHandleInfo(selectedEl, viewport);
              const pivot = info.pivotScreen;

              const baseHandle = info.handleScreen;
              const baseRadius = Math.hypot(baseHandle.x - pivot.x, baseHandle.y - pivot.y);

              const interaction = interactionRef.current;
              const isRotating = interaction.kind === "rotate" && interaction.elementId === selectedEl.id;
              const isHot = isRotating || rotateHoverRef.current;

              let handle = baseHandle;
              let deltaDeg: number | null = null;
              let absoluteDeg: number | null = null;
              if (isRotating) {
                const dx = interaction.currentScreen.x - pivot.x;
                const dy = interaction.currentScreen.y - pivot.y;
                const len = Math.hypot(dx, dy);
                if (len > 1e-6) {
                  handle = toVector2(pivot.x + (dx / len) * baseRadius, pivot.y + (dy / len) * baseRadius);
                }
                deltaDeg = Math.round((interaction.snappedDelta * 180) / Math.PI);
                if (group !== "walls" && group !== "areas") {
                  const currentRotY = normalizeAngleRad(interaction.startElement.rotation.y - interaction.snappedDelta);
                  absoluteDeg = Math.round(normalizeDeg360((-currentRotY * 180) / Math.PI));
                }
              }

              ctx2d.save();
              ctx2d.lineWidth = 2;
              ctx2d.strokeStyle = isHot ? "rgba(251,191,36,0.78)" : "rgba(255,255,255,0.10)";
              ctx2d.beginPath();
              ctx2d.moveTo(pivot.x, pivot.y);
              ctx2d.lineTo(handle.x, handle.y);
              ctx2d.stroke();

              ctx2d.shadowColor = isHot ? "rgba(251,191,36,0.35)" : "rgba(0,0,0,0)";
              ctx2d.shadowBlur = isHot ? 12 : 0;
              ctx2d.fillStyle = "rgba(8,12,26,0.92)";
              ctx2d.strokeStyle = isHot ? "rgba(251,191,36,0.92)" : "rgba(255,255,255,0.18)";
              ctx2d.lineWidth = 2;
              ctx2d.beginPath();
              ctx2d.arc(handle.x, handle.y, 8, 0, Math.PI * 2);
              ctx2d.fill();
              ctx2d.shadowBlur = 0;
              ctx2d.stroke();

              if (deltaDeg !== null) {
                const label = `${deltaDeg}°`;
                const hint = absoluteDeg !== null ? ` (${absoluteDeg}°)` : "";
                const text = `${label}${hint}`;
                ctx2d.font = "12px ui-sans-serif, system-ui";
                ctx2d.textAlign = "center";
                ctx2d.textBaseline = "middle";
                const metrics = ctx2d.measureText(text);
                const boxW = metrics.width + 18;
                const boxH = 24;
                const x0 = handle.x - boxW / 2;
                const y0 = handle.y - 24;
                ctx2d.fillStyle = "rgba(8,12,26,0.82)";
                ctx2d.strokeStyle = "rgba(255,255,255,0.14)";
                ctx2d.lineWidth = 1;
                ctx2d.beginPath();
                roundRectPath(ctx2d, x0, y0, boxW, boxH, 999);
                ctx2d.fill();
                ctx2d.stroke();
                ctx2d.fillStyle = "rgba(230,232,242,0.92)";
                ctx2d.fillText(text, handle.x, y0 + boxH / 2);
              }

              ctx2d.restore();
            }
          }
        }
      }

      const session = toolSessionRef.current;
      if (session?.renderOverlay2D) {
        try {
          session.renderOverlay2D({ ctx: ctx2d, viewport });
        } catch (err) {
          console.error("[toolOverlay]", err);
        }
      }

      const interaction = interactionRef.current;
      const interactionModeValue = interactionModeRef.current;
      const spacePressed = spacePressedRef.current;
      const hoverId = hoverRef.current;

      if (interaction.kind === "select-box") {
        const left = Math.min(interaction.startScreen.x, interaction.currentScreen.x);
        const right = Math.max(interaction.startScreen.x, interaction.currentScreen.x);
        const top = Math.min(interaction.startScreen.y, interaction.currentScreen.y);
        const bottom = Math.max(interaction.startScreen.y, interaction.currentScreen.y);
        const rectW = right - left;
        const rectH = bottom - top;
        if (rectW >= 3 || rectH >= 3) {
          ctx2d.save();
          ctx2d.fillStyle = "rgba(251,191,36,0.08)";
          ctx2d.strokeStyle = "rgba(251,191,36,0.55)";
          ctx2d.lineWidth = 1;
          ctx2d.setLineDash([6, 5]);
          ctx2d.fillRect(left, top, rectW, rectH);
          ctx2d.strokeRect(left + 0.5, top + 0.5, rectW, rectH);
          ctx2d.restore();
        }
      }

      const cursor =
        interaction.kind === "pan" || interaction.kind === "drag" || interaction.kind === "rotate"
          ? "grabbing"
          : interaction.kind === "select-box"
            ? "crosshair"
            : spacePressed
              ? "grab"
              : session
                ? session.getCursor?.() ?? "crosshair"
                : interactionModeValue === "navigate"
                  ? "grab"
                  : rotateHoverRef.current
                    ? "grab"
                    : hoverId
                      ? "move"
                      : "default";

      canvasEl.style.cursor = cursor;
    }

    const ro = new ResizeObserver(resize);
    ro.observe(canvasEl);
    resize();

    function makeViewportContext(): Viewport2DContext {
      const w = canvasEl.clientWidth;
      const h = canvasEl.clientHeight;
      const originX = w / 2;
      const originY = h / 2;
      const camera = cameraRef.current;
      const scale = camera.scale;
      const cx = camera.cx;
      const cz = camera.cz;

      const worldToScreen = (p: PlanePoint): Vector2 =>
        toVector2(originX + (p.x - cx) * scale, originY + (p.z - cz) * scale);
      const screenToWorld = (p: Vector2): PlanePoint =>
        toPlanePoint((p.x - originX) / scale + cx, (p.y - originY) / scale + cz);

      return {
        canvas: canvasEl,
        width: w,
        height: h,
        dpr: window.devicePixelRatio || 1,
        worldToScreen,
        screenToWorld,
        scale,
      };
    }

    function screenToWorld(screen: Vector2): PlanePoint {
      const w = canvasEl.clientWidth;
      const h = canvasEl.clientHeight;
      const originX = w / 2;
      const originY = h / 2;
      const { cx, cz, scale } = cameraRef.current;
      return toPlanePoint((screen.x - originX) / scale + cx, (screen.y - originY) / scale + cz);
    }

    function toToolEvent(kind: "down" | "move" | "up" | "cancel" | "dblclick", e: PointerEvent): void {
      const session = toolSessionRef.current;
      if (!session?.onPointerEvent) return;

      const rect = canvasEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const screen = toVector2(x, y);
      const worldRaw = screenToWorld(screen);
      const shouldSnap = toolSnapToGridRef.current && !e.altKey;
      const world = shouldSnap ? snapPoint(worldRaw, SNAP_STEP) : worldRaw;

      session.onPointerEvent({
        kind,
        world,
        screen,
        button: e.button,
        buttons: e.buttons,
        pointerType: e.pointerType,
        shiftKey: e.shiftKey,
        altKey: e.altKey,
        metaKey: e.metaKey,
        ctrlKey: e.ctrlKey,
      });
      requestDraw();
    }

    function hitTestElement(el: CompositionElement, world: PlanePoint, viewport: Viewport2DContext): boolean {
      const def = elementTypesRef.current[el.type];
      if (def?.hitTest2D) {
        try {
          return Boolean(def.hitTest2D({ element: el, world, viewport }));
        } catch (err) {
          console.error(`[hitTest2D:${el.type}]`, err);
        }
      }

      const verts = readVertices(el.props.vertices);
      if (verts.length >= 3) return pointInPolygon(world, verts);

      const a = readPlanePoint(el.props.a);
      const b = readPlanePoint(el.props.b);
      if (a && b) {
        const widthWorld = Math.max(0.04, readNumber(el.props.width, 0.12));
        const threshold = Math.max(widthWorld / 2, 10 / Math.max(1, viewport.scale));
        return distPointToSegment(world, a, b) <= threshold;
      }

      const p = toPlanePoint(el.position.x, el.position.z);
      const radius = clamp(12 / Math.max(1, viewport.scale), 0.08, 0.6);
      return Math.hypot(world.x - p.x, world.z - p.z) <= radius;
    }

    function findHitElement(world: PlanePoint): string | null {
      const viewport = makeViewportContext();

      const groupRank = (typeId: string): number => {
        const group = elementTypesRef.current[typeId]?.layerGroup ?? "";
        if (group === "areas") return 0;
        if (group === "walls") return 1;
        return 2;
      };

      const ordered = elementsRef.current
        .map((el, idx) => ({ el, idx }))
        .sort((a, b) => groupRank(a.el.type) - groupRank(b.el.type) || a.idx - b.idx)
        .map((v) => v.el);

      for (let i = ordered.length - 1; i >= 0; i--) {
        const el = ordered[i];
        if (hitTestElement(el, world, viewport)) return el.id;
      }
      return null;
    }

    function elementScreenBounds(
      el: CompositionElement,
      viewport: Viewport2DContext,
    ): { minX: number; minY: number; maxX: number; maxY: number } {
      const verts = readVertices(el.props.vertices);
      if (verts.length >= 3) {
        const pts = verts.map((p) => viewport.worldToScreen(p));
        return {
          minX: Math.min(...pts.map((p) => p.x)),
          maxX: Math.max(...pts.map((p) => p.x)),
          minY: Math.min(...pts.map((p) => p.y)),
          maxY: Math.max(...pts.map((p) => p.y)),
        };
      }

      const a = readPlanePoint(el.props.a);
      const b = readPlanePoint(el.props.b);
      if (a && b) {
        const pa = viewport.worldToScreen(a);
        const pb = viewport.worldToScreen(b);
        const widthWorld = Math.max(0.04, readNumber(el.props.width, 0.12));
        const pad = Math.max(10, (widthWorld * viewport.scale) / 2 + 6);
        return {
          minX: Math.min(pa.x, pb.x) - pad,
          maxX: Math.max(pa.x, pb.x) + pad,
          minY: Math.min(pa.y, pb.y) - pad,
          maxY: Math.max(pa.y, pb.y) + pad,
        };
      }

      const p = viewport.worldToScreen(toPlanePoint(el.position.x, el.position.z));
      const pad = 14;
      return { minX: p.x - pad, maxX: p.x + pad, minY: p.y - pad, maxY: p.y + pad };
    }

    function findElementsInScreenRect(a: Vector2, b: Vector2): string[] {
      const left = Math.min(a.x, b.x);
      const right = Math.max(a.x, b.x);
      const top = Math.min(a.y, b.y);
      const bottom = Math.max(a.y, b.y);

      const viewport = makeViewportContext();
      const out: string[] = [];
      for (const el of elementsRef.current) {
        const bounds = elementScreenBounds(el, viewport);
        const intersects = !(bounds.maxX < left || bounds.minX > right || bounds.maxY < top || bounds.minY > bottom);
        if (intersects) out.push(el.id);
      }
      return out;
    }

    function translateElement(el: CompositionElement, delta: PlanePoint): CompositionElementPatch {
      const def = elementTypesRef.current[el.type];
      if (def?.translate2D) {
        try {
          return def.translate2D({ element: el, delta }) ?? {};
        } catch (err) {
          console.error(`[translate2D:${el.type}]`, err);
        }
      }

      const propsPatch: Record<string, unknown> = {};

      const a = readPlanePoint(el.props.a);
      const b = readPlanePoint(el.props.b);
      const aPrev = readPlanePoint((el.props as any).a_prev);
      const bNext = readPlanePoint((el.props as any).b_next);
      const vertices = readVertices(el.props.vertices);

      if (a) propsPatch.a = add(a, delta);
      if (b) propsPatch.b = add(b, delta);
      if (aPrev) propsPatch.a_prev = add(aPrev, delta);
      if (bNext) propsPatch.b_next = add(bNext, delta);
      if (vertices.length >= 3) propsPatch.vertices = vertices.map((p) => add(p, delta));

      const patch: CompositionElementPatch = {
        position: { x: el.position.x + delta.x, z: el.position.z + delta.z },
      };
      if (Object.keys(propsPatch).length > 0) patch.props = propsPatch;
      return patch;
    }

    function getRotateHandleInfo(el: CompositionElement, viewport: Viewport2DContext): {
      pivotWorld: PlanePoint;
      pivotScreen: Vector2;
      handleScreen: Vector2;
      hitRadiusPx: number;
      radiusPx: number;
    } {
      const pivotWorld = toPlanePoint(el.position.x, el.position.z);
      const pivotScreen = viewport.worldToScreen(pivotWorld);

      const verts = readVertices(el.props.vertices);
      const a = readPlanePoint(el.props.a);
      const b = readPlanePoint(el.props.b);

      const points: PlanePoint[] = [];
      if (verts.length >= 3) points.push(...verts);
      else if (a && b) points.push(a, b);
      else points.push(pivotWorld);

      const pts = points.map((p) => viewport.worldToScreen(p));
      let minX = Math.min(...pts.map((p) => p.x));
      let maxX = Math.max(...pts.map((p) => p.x));
      let minY = Math.min(...pts.map((p) => p.y));
      let maxY = Math.max(...pts.map((p) => p.y));

      if (pts.length === 1) {
        minX = pivotScreen.x - 18;
        maxX = pivotScreen.x + 18;
        minY = pivotScreen.y - 18;
        maxY = pivotScreen.y + 18;
      }

      const extent = Math.max(maxX - minX, maxY - minY, 36);
      const radiusPx = Math.max(34, Math.min(92, extent / 2 + 34));
      const baseAngle = -Math.PI / 2;
      const angle = baseAngle - el.rotation.y;
      const handleScreen = toVector2(pivotScreen.x + Math.cos(angle) * radiusPx, pivotScreen.y + Math.sin(angle) * radiusPx);
      const hitRadiusPx = 12;
      return { pivotWorld, pivotScreen, handleScreen, hitRadiusPx, radiusPx };
    }

    function dist2(a: Vector2, b: Vector2): number {
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      return dx * dx + dy * dy;
    }

    function normalizeAngleRad(angle: number): number {
      return Math.atan2(Math.sin(angle), Math.cos(angle));
    }

    function normalizeDeg360(deg: number): number {
      const d = deg % 360;
      return d < 0 ? d + 360 : d;
    }

    function buildRotationPatch(startElement: CompositionElement, pivot: PlanePoint, deltaRad: number): CompositionElementPatch {
      const group = elementTypesRef.current[startElement.type]?.layerGroup ?? "";

      if (group === "walls") {
        const propsPatch: Record<string, unknown> = {};
        const a = readPlanePoint(startElement.props.a);
        const b = readPlanePoint(startElement.props.b);
        const aPrev = readPlanePoint((startElement.props as any).a_prev);
        const bNext = readPlanePoint((startElement.props as any).b_next);
        if (a) propsPatch.a = rotateAround(a, pivot, deltaRad);
        if (b) propsPatch.b = rotateAround(b, pivot, deltaRad);
        if (aPrev) propsPatch.a_prev = rotateAround(aPrev, pivot, deltaRad);
        if (bNext) propsPatch.b_next = rotateAround(bNext, pivot, deltaRad);
        return Object.keys(propsPatch).length ? { props: propsPatch } : {};
      }

      if (group === "areas") {
        const vertices = readVertices(startElement.props.vertices);
        if (vertices.length >= 3) return { props: { vertices: vertices.map((p) => rotateAround(p, pivot, deltaRad)) } };
        return {};
      }

      return { rotation: { y: startElement.rotation.y - deltaRad } };
    }

    function handlePointerDown(e: PointerEvent) {
      e.preventDefault();
      canvasEl.setPointerCapture(e.pointerId);

      const spacePressed = spacePressedRef.current;
      const mode = interactionModeRef.current;
      const session = toolSessionRef.current;
      const panRequested =
        spacePressed ||
        e.button === 1 ||
        e.button === 2 ||
        (mode === "navigate" && e.button === 0 && !session);

      const rect = canvasEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const screen = toVector2(x, y);
      const world = screenToWorld(screen);

      if (panRequested) {
        interactionRef.current = {
          kind: "pan",
          pointerId: e.pointerId,
          startScreen: screen,
          startCamera: { ...cameraRef.current },
          startedByLeft: e.button === 0 && !spacePressed,
          moved: e.button !== 0,
        };
        requestDraw();
        return;
      }

      const selectedIds = selectedRef.current;
      const primaryId = selectedIds.length === 1 ? selectedIds[0] : null;
      if (primaryId && !session && !spacePressed && mode === "select" && e.button === 0) {
        const selectedEl = elementsRef.current.find((it) => it.id === primaryId) ?? null;
        if (selectedEl) {
          const viewport = makeViewportContext();
          const info = getRotateHandleInfo(selectedEl, viewport);
          if (dist2(screen, info.handleScreen) <= info.hitRadiusPx * info.hitRadiusPx) {
            const pivot = info.pivotWorld;
            const startAngle = Math.atan2(world.z - pivot.z, world.x - pivot.x);
            onBeginUndoGroupRef.current?.();
            interactionRef.current = {
              kind: "rotate",
              pointerId: e.pointerId,
              elementId: primaryId,
              pivot,
              startAngle,
              startElement: selectedEl,
              currentScreen: screen,
              snappedDelta: 0,
              stepDeg: 15,
            };
            rotateHoverRef.current = false;
            requestDraw();
            return;
          }
        }
      }

      if (session) {
        onBeginUndoGroupRef.current?.();
        interactionRef.current = { kind: "tool", pointerId: e.pointerId };
        toToolEvent("down", e);
        return;
      }

      if (e.button !== 0) return;

      const hitId = findHitElement(world);
      if (hitId) {
        const prevSelected = selectedRef.current;
        const multiKey = e.metaKey || e.ctrlKey;
        let nextSelected = prevSelected;
        let toggleOffId: string | null = null;
        let selectOnlyOnClickId: string | null = null;

        if (multiKey) {
          if (prevSelected.includes(hitId)) toggleOffId = hitId;
          else nextSelected = [...prevSelected, hitId];
        } else {
          if (prevSelected.includes(hitId)) {
            nextSelected = prevSelected;
            if (prevSelected.length > 1) selectOnlyOnClickId = hitId;
          } else {
            nextSelected = [hitId];
          }
        }

        if (nextSelected !== prevSelected) {
          selectedRef.current = nextSelected;
          onSelectRef.current?.(nextSelected);
        }

        const startElements: CompositionElement[] = [];
        const targetIds: string[] = [];
        for (const id of nextSelected) {
          const el = elementsRef.current.find((it) => it.id === id) ?? null;
          if (!el) continue;
          startElements.push(el);
          targetIds.push(id);
        }

        if (startElements.length > 0 && updateElementRef.current) {
          onBeginUndoGroupRef.current?.();
          interactionRef.current = {
            kind: "drag",
            pointerId: e.pointerId,
            startElements,
            targetIds,
            startScreen: screen,
            startWorldSnapped: snapPoint(world, SNAP_STEP),
            moved: false,
            duplicateRequested: e.altKey,
            duplicated: false,
            toggleOffId,
            selectOnlyOnClickId,
          };
        } else {
          interactionRef.current = { kind: "none" };
        }
        requestDraw();
        return;
      }

      if (mode === "select") {
        interactionRef.current = {
          kind: "select-box",
          pointerId: e.pointerId,
          startScreen: screen,
          currentScreen: screen,
          additive: e.metaKey || e.ctrlKey,
          baseSelection: [...selectedRef.current],
        };
      } else {
        interactionRef.current = { kind: "none" };
      }
      requestDraw();
    }

    function handlePointerMove(e: PointerEvent) {
      e.preventDefault();

      const interaction = interactionRef.current;

      const rect = canvasEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const screen = toVector2(x, y);

      if (interaction.kind === "tool") {
        toToolEvent("move", e);
        return;
      }

      if (interaction.kind === "rotate") {
        if (interaction.pointerId !== e.pointerId) return;

        const world = screenToWorld(screen);
        const currentAngle = Math.atan2(world.z - interaction.pivot.z, world.x - interaction.pivot.x);
        const rawDelta = normalizeAngleRad(currentAngle - interaction.startAngle);

        const stepDeg = e.shiftKey ? 5 : 15;
        const stepRad = (stepDeg * Math.PI) / 180;
        const snappedDelta = e.altKey ? rawDelta : Math.round(rawDelta / stepRad) * stepRad;

        interaction.currentScreen = screen;
        interaction.snappedDelta = snappedDelta;
        interaction.stepDeg = stepDeg;

        const patch = buildRotationPatch(interaction.startElement, interaction.pivot, snappedDelta);
        pendingRotatePatch = { id: interaction.elementId, patch };
        if (!rotateRaf) {
          rotateRaf = requestAnimationFrame(() => {
            rotateRaf = 0;
            flushRotatePatch();
          });
        }
        requestDraw();
        return;
      }

      if (interaction.kind === "select-box") {
        if (interaction.pointerId !== e.pointerId) return;
        interaction.currentScreen = screen;
        requestDraw();
        return;
      }

      if (interaction.kind === "pan") {
        if (interaction.pointerId !== e.pointerId) return;
        const dx = screen.x - interaction.startScreen.x;
        const dy = screen.y - interaction.startScreen.y;
        if (!interaction.moved && dx * dx + dy * dy >= 9) interaction.moved = true;
        if (!interaction.moved) return;

        const { scale } = interaction.startCamera;
        cameraRef.current.cx = interaction.startCamera.cx - dx / scale;
        cameraRef.current.cz = interaction.startCamera.cz - dy / scale;
        requestDraw();
        return;
      }

      if (interaction.kind === "drag") {
        if (interaction.pointerId !== e.pointerId) return;
        const dx = screen.x - interaction.startScreen.x;
        const dy = screen.y - interaction.startScreen.y;
        const movedNow = dx * dx + dy * dy >= 9;
        if (!interaction.moved && movedNow) {
          interaction.moved = true;
          interaction.toggleOffId = null;
          interaction.selectOnlyOnClickId = null;

          if (!interaction.duplicated && (interaction.duplicateRequested || e.altKey) && duplicateElementsRef.current) {
            const nextIds = duplicateElementsRef.current(interaction.startElements);
            if (nextIds.length === interaction.startElements.length) {
              interaction.targetIds = nextIds;
              interaction.duplicated = true;
              selectedRef.current = nextIds;
              onSelectRef.current?.(nextIds);
            }
          }
        }
        if (!interaction.moved) return;

        const world = snapPoint(screenToWorld(screen), SNAP_STEP);
        const delta = sub(world, interaction.startWorldSnapped);

        pendingDragPatch = interaction.startElements.map((el, idx) => {
          const id = interaction.targetIds[idx] ?? el.id;
          return { id, patch: translateElement(el, delta) };
        });
        if (!dragRaf) {
          dragRaf = requestAnimationFrame(() => {
            dragRaf = 0;
            flushDragPatch();
          });
        }
        return;
      }

      if (toolSessionRef.current && !spacePressedRef.current) {
        toToolEvent("move", e);
        return;
      }

      if (!toolSessionRef.current && !spacePressedRef.current) {
        const world = screenToWorld(screen);
        const hitId = findHitElement(world);
        if (hitId !== hoverRef.current) {
          hoverRef.current = hitId;
          requestDraw();
        }

        const primaryId = selectedRef.current.length === 1 ? selectedRef.current[0] : null;
        if (primaryId && interactionModeRef.current === "select") {
          const selectedEl = elementsRef.current.find((it) => it.id === primaryId) ?? null;
          if (selectedEl) {
            const viewport = makeViewportContext();
            const info = getRotateHandleInfo(selectedEl, viewport);
            const overRotate = dist2(screen, info.handleScreen) <= info.hitRadiusPx * info.hitRadiusPx;
            if (overRotate !== rotateHoverRef.current) {
              rotateHoverRef.current = overRotate;
              requestDraw();
            }
          }
        } else if (rotateHoverRef.current) {
          rotateHoverRef.current = false;
          requestDraw();
        }
      }
    }

    function handlePointerUp(e: PointerEvent) {
      e.preventDefault();

      const interaction = interactionRef.current;
      if (interaction.kind === "tool") {
        toToolEvent("up", e);
        onEndUndoGroupRef.current?.();
        interactionRef.current = { kind: "none" };
        return;
      }

      if (interaction.kind === "rotate") {
        if (interaction.pointerId !== e.pointerId) return;
        flushRotatePatch();
        onEndUndoGroupRef.current?.();
        interactionRef.current = { kind: "none" };
        requestDraw();
        return;
      }

      if (interaction.kind === "select-box") {
        if (interaction.pointerId !== e.pointerId) return;

        const left = Math.min(interaction.startScreen.x, interaction.currentScreen.x);
        const right = Math.max(interaction.startScreen.x, interaction.currentScreen.x);
        const top = Math.min(interaction.startScreen.y, interaction.currentScreen.y);
        const bottom = Math.max(interaction.startScreen.y, interaction.currentScreen.y);
        const rectW = right - left;
        const rectH = bottom - top;

        let nextSelection: string[] = [];
        if (rectW < 3 && rectH < 3) {
          nextSelection = interaction.additive ? interaction.baseSelection : [];
        } else {
          const hits = findElementsInScreenRect(interaction.startScreen, interaction.currentScreen);
          if (interaction.additive) {
            const set = new Set(interaction.baseSelection);
            for (const id of hits) set.add(id);
            nextSelection = Array.from(set);
          } else {
            nextSelection = hits;
          }
        }

        selectedRef.current = nextSelection;
        onSelectRef.current?.(nextSelection);
        interactionRef.current = { kind: "none" };
        requestDraw();
        return;
      }

      if (interaction.kind === "pan") {
        if (interaction.pointerId !== e.pointerId) return;
        if (interaction.startedByLeft && !interaction.moved && interactionModeRef.current === "select") {
          selectedRef.current = [];
          onSelectRef.current?.([]);
        }
        interactionRef.current = { kind: "none" };
        requestDraw();
        return;
      }

      if (interaction.kind === "drag") {
        if (interaction.pointerId !== e.pointerId) return;
        flushDragPatch();
        if (!interaction.moved) {
          if (interaction.toggleOffId) {
            const next = selectedRef.current.filter((id) => id !== interaction.toggleOffId);
            selectedRef.current = next;
            onSelectRef.current?.(next);
          } else if (interaction.selectOnlyOnClickId) {
            const next = [interaction.selectOnlyOnClickId];
            selectedRef.current = next;
            onSelectRef.current?.(next);
          }
        }
        onEndUndoGroupRef.current?.();
        interactionRef.current = { kind: "none" };
        requestDraw();
        return;
      }
    }

    function handlePointerCancel(e: PointerEvent) {
      e.preventDefault();

      const interaction = interactionRef.current;
      if (interaction.kind === "tool") {
        toToolEvent("cancel", e);
      }
      if (interaction.kind === "tool" || interaction.kind === "drag" || interaction.kind === "rotate") {
        if (interaction.kind === "rotate") flushRotatePatch();
        if (interaction.kind === "drag") flushDragPatch();
        onEndUndoGroupRef.current?.();
      }
      interactionRef.current = { kind: "none" };
      requestDraw();
    }

    function handleDoubleClick(e: MouseEvent) {
      const session = toolSessionRef.current;
      if (session?.onPointerEvent && !spacePressedRef.current) {
        const rect = canvasEl.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const screen = toVector2(x, y);
        const world = screenToWorld(screen);

        session.onPointerEvent({
          kind: "dblclick",
          world,
          screen,
          button: 0,
          buttons: 0,
          pointerType: "mouse",
          shiftKey: e.shiftKey,
          altKey: e.altKey,
          metaKey: e.metaKey,
          ctrlKey: e.ctrlKey,
        });
        requestDraw();
        return;
      }

      const rect = canvasEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const screen = toVector2(x, y);
      const world = screenToWorld(screen);

      const hitId = findHitElement(world);
      if (hitId) {
        const next = [hitId];
        selectedRef.current = next;
        onSelectRef.current?.(next);
        onOpenEditorRef.current?.(hitId);
      }
      requestDraw();
    }

    function handleWheel(event: Event) {
      const e = event as WheelEvent;
      const canvasRect = canvasEl.getBoundingClientRect();
      const x = e.clientX - canvasRect.left;
      const y = e.clientY - canvasRect.top;
      if (x < 0 || y < 0 || x > canvasRect.width || y > canvasRect.height) return;

      e.preventDefault();

      const w = canvasEl.clientWidth;
      const h = canvasEl.clientHeight;
      const originX = w / 2;
      const originY = h / 2;

      const camera = cameraRef.current;
      const before = toPlanePoint((x - originX) / camera.scale + camera.cx, (y - originY) / camera.scale + camera.cz);

      const zoomFactor = Math.pow(2, -e.deltaY / 420);
      const nextScale = clamp(camera.scale * zoomFactor, 18, 240);
      camera.scale = nextScale;

      camera.cx = before.x - (x - originX) / nextScale;
      camera.cz = before.z - (y - originY) / nextScale;
      requestDraw();
    }

    function handleContextMenu(event: Event) {
      (event as MouseEvent).preventDefault();
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (!enableKeyboardShortcutsRef.current) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) return;

      const meta = e.metaKey || e.ctrlKey;
      if (meta && !e.altKey) {
        const key = e.key.toLowerCase();
        if (key === "z") {
          e.preventDefault();
          if (e.shiftKey) onRedoRef.current?.();
          else onUndoRef.current?.();
          requestDraw();
          return;
        }
        if (key === "y") {
          e.preventDefault();
          onRedoRef.current?.();
          requestDraw();
          return;
        }
        if (key === "a" && interactionModeRef.current === "select") {
          e.preventDefault();
          const ids = elementsRef.current.map((el) => el.id);
          selectedRef.current = ids;
          onSelectRef.current?.(ids);
          requestDraw();
          return;
        }
      }

      if (e.key === " ") {
        e.preventDefault();
        spacePressedRef.current = true;
        requestDraw();
      }

      const selectedIds = selectedRef.current;
      if (selectedIds.length > 0) {
        if ((e.key === "Delete" || e.key === "Backspace") && removeElementRef.current) {
          e.preventDefault();
          onBeginUndoGroupRef.current?.();
          for (const id of selectedIds) removeElementRef.current(id);
          onEndUndoGroupRef.current?.();
          selectedRef.current = [];
          onSelectRef.current?.([]);
          requestDraw();
          return;
        }

        const step = (e.shiftKey ? 10 : 1) * SNAP_STEP;
        let delta: PlanePoint | null = null;
        if (e.key === "ArrowLeft") delta = toPlanePoint(-step, 0);
        if (e.key === "ArrowRight") delta = toPlanePoint(step, 0);
        if (e.key === "ArrowUp") delta = toPlanePoint(0, -step);
        if (e.key === "ArrowDown") delta = toPlanePoint(0, step);
        if (delta && updateElementRef.current) {
          e.preventDefault();
          onBeginUndoGroupRef.current?.();
          for (const id of selectedIds) {
            const el = elementsRef.current.find((it) => it.id === id) ?? null;
            if (!el) continue;
            updateElementRef.current(id, translateElement(el, delta));
          }
          onEndUndoGroupRef.current?.();
          requestDraw();
          return;
        }

        if (selectedIds.length === 1 && updateElementRef.current) {
          const selectedId = selectedIds[0];
          const lower = e.key.toLowerCase();
          if (lower === "q" || lower === "e") {
            e.preventDefault();
            const el = elementsRef.current.find((it) => it.id === selectedId) ?? null;
            if (!el) return;

            const sign = lower === "q" ? -1 : 1;
            const stepDeg = e.shiftKey ? 5 : 15;
            const deltaRad = (sign * stepDeg * Math.PI) / 180;

            const group = elementTypesRef.current[el.type]?.layerGroup ?? "";
            const pivot = toPlanePoint(el.position.x, el.position.z);

            if (group === "walls") {
              const propsPatch: Record<string, unknown> = {};
              const a = readPlanePoint(el.props.a);
              const b = readPlanePoint(el.props.b);
              const aPrev = readPlanePoint((el.props as any).a_prev);
              const bNext = readPlanePoint((el.props as any).b_next);
              if (a) propsPatch.a = rotateAround(a, pivot, deltaRad);
              if (b) propsPatch.b = rotateAround(b, pivot, deltaRad);
              if (aPrev) propsPatch.a_prev = rotateAround(aPrev, pivot, deltaRad);
              if (bNext) propsPatch.b_next = rotateAround(bNext, pivot, deltaRad);
              if (Object.keys(propsPatch).length > 0) updateElementRef.current(selectedId, { props: propsPatch });
              requestDraw();
              return;
            }

            if (group === "areas") {
              const vertices = readVertices(el.props.vertices);
              if (vertices.length >= 3) {
                updateElementRef.current(selectedId, {
                  props: { vertices: vertices.map((p) => rotateAround(p, pivot, deltaRad)) },
                });
              }
              requestDraw();
              return;
            }

            updateElementRef.current(selectedId, { rotation: { y: el.rotation.y - deltaRad } });
            requestDraw();
            return;
          }
        }
      }

      const handler = toolSessionRef.current?.onKeyDown;
      if (!handler) return;
      handler(e);
      requestDraw();
    }

    function handleKeyUp(e: KeyboardEvent) {
      if (!enableKeyboardShortcutsRef.current) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) return;

      if (e.key === " ") {
        e.preventDefault();
        spacePressedRef.current = false;
        requestDraw();
      }
    }

    canvasEl.addEventListener("pointerdown", handlePointerDown);
    canvasEl.addEventListener("pointermove", handlePointerMove);
    canvasEl.addEventListener("pointerup", handlePointerUp);
    canvasEl.addEventListener("pointercancel", handlePointerCancel);
    canvasEl.addEventListener("dblclick", handleDoubleClick);
    canvasEl.addEventListener("wheel", handleWheel, { passive: false });
    canvasEl.addEventListener("contextmenu", handleContextMenu);
    canvasEl.addEventListener("toposync:invalidate", requestDraw as unknown as EventListener);
    window.addEventListener("toposync:invalidate", requestDraw as unknown as EventListener);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("keyup", handleKeyUp);

    return () => {
      canvasEl.removeEventListener("pointerdown", handlePointerDown);
      canvasEl.removeEventListener("pointermove", handlePointerMove);
      canvasEl.removeEventListener("pointerup", handlePointerUp);
      canvasEl.removeEventListener("pointercancel", handlePointerCancel);
      canvasEl.removeEventListener("dblclick", handleDoubleClick);
      canvasEl.removeEventListener("wheel", handleWheel);
      canvasEl.removeEventListener("contextmenu", handleContextMenu);
      canvasEl.removeEventListener("toposync:invalidate", requestDraw as unknown as EventListener);
      window.removeEventListener("toposync:invalidate", requestDraw as unknown as EventListener);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("keyup", handleKeyUp);
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
      if (dragRaf) cancelAnimationFrame(dragRaf);
      if (rotateRaf) cancelAnimationFrame(rotateRaf);
      drawRef.current = null;
    };
  }, []);

  return <canvas className="viewportCanvas" ref={canvasRef} style={{ touchAction: "none" }} />;
}
