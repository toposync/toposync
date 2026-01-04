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

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  activeToolSession?: EditorToolSession | null;
  selectedElementId?: string | null;
  onSelectElement?: (elementId: string | null) => void;
  onOpenEditor?: (elementId: string) => void;
  updateElement?: (elementId: string, patch: CompositionElementPatch) => void;
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

type Camera2D = { cx: number; cz: number; scale: number };

type Interaction =
  | { kind: "none" }
  | { kind: "tool"; pointerId: number }
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
      elementId: string;
      startScreen: Vector2;
      startWorld: PlanePoint;
      startElement: CompositionElement;
      moved: boolean;
    };

export function Viewport2D({
  elements,
  elementTypesById,
  activeToolSession,
  selectedElementId,
  onSelectElement,
  onOpenEditor,
  updateElement,
}: Props): React.ReactElement {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawRef = useRef<(() => void) | null>(null);

  const elementsRef = useRef<CompositionElement[]>(elements);
  const elementTypesRef = useRef<Record<string, ElementType>>(elementTypesById);
  const toolSessionRef = useRef<EditorToolSession | null>(activeToolSession ?? null);

  const selectedRef = useRef<string | null>(selectedElementId ?? null);
  const onSelectRef = useRef<Props["onSelectElement"]>(onSelectElement);
  const onOpenEditorRef = useRef<Props["onOpenEditor"]>(onOpenEditor);
  const updateElementRef = useRef<Props["updateElement"]>(updateElement);

  const cameraRef = useRef<Camera2D>({ cx: 0, cz: 0, scale: 52 });
  const interactionRef = useRef<Interaction>({ kind: "none" });
  const hoverRef = useRef<string | null>(null);
  const spacePressedRef = useRef(false);

  useEffect(() => {
    elementsRef.current = elements;
    if (selectedRef.current && !elements.some((e) => e.id === selectedRef.current)) {
      selectedRef.current = null;
      onSelectRef.current?.(null);
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
    selectedRef.current = selectedElementId ?? null;
    drawRef.current?.();
  }, [selectedElementId]);

  useEffect(() => {
    onSelectRef.current = onSelectElement;
  }, [onSelectElement]);

  useEffect(() => {
    onOpenEditorRef.current = onOpenEditor;
  }, [onOpenEditor]);

  useEffect(() => {
    updateElementRef.current = updateElement;
  }, [updateElement]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const canvasEl: HTMLCanvasElement = canvas;
    const ctx2d: CanvasRenderingContext2D = ctx;

    let raf = 0;
    let dragRaf = 0;
    let pendingDragPatch: { id: string; patch: CompositionElementPatch } | null = null;

    function requestDraw() {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        draw();
      });
    }

    function flushDragPatch() {
      if (!pendingDragPatch) return;
      updateElementRef.current?.(pendingDragPatch.id, pendingDragPatch.patch);
      pendingDragPatch = null;
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
      const gridWorld = niceFrac * base;

      const firstX = Math.floor(minX / gridWorld) * gridWorld;
      const firstZ = Math.floor(minZ / gridWorld) * gridWorld;

      ctx2d.strokeStyle = "rgba(255,255,255,0.055)";
      ctx2d.lineWidth = 1;
      for (let x = firstX; x <= maxX; x += gridWorld) {
        const sx = Math.round(worldToScreen(toPlanePoint(x, cz)).x) + 0.5;
        ctx2d.beginPath();
        ctx2d.moveTo(sx, 0);
        ctx2d.lineTo(sx, h);
        ctx2d.stroke();
      }
      for (let z = firstZ; z <= maxZ; z += gridWorld) {
        const sy = Math.round(worldToScreen(toPlanePoint(cx, z)).y) + 0.5;
        ctx2d.beginPath();
        ctx2d.moveTo(0, sy);
        ctx2d.lineTo(w, sy);
        ctx2d.stroke();
      }

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

      const selectedId = selectedRef.current;
      if (selectedId) {
        const selectedEl = elementsRef.current.find((e) => e.id === selectedId) ?? null;
        if (selectedEl) {
          const verts = readVertices(selectedEl.props.vertices);
          const a = readPlanePoint(selectedEl.props.a);
          const b = readPlanePoint(selectedEl.props.b);

          ctx2d.save();
          ctx2d.strokeStyle = "rgba(251,191,36,0.92)";
          ctx2d.lineWidth = 3;
          ctx2d.shadowColor = "rgba(251,191,36,0.35)";
          ctx2d.shadowBlur = 10;

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
            const p = worldToScreen(toPlanePoint(selectedEl.position.x, selectedEl.position.z));
            ctx2d.beginPath();
            ctx2d.arc(p.x, p.y, 11, 0, Math.PI * 2);
            ctx2d.stroke();
          }
          ctx2d.restore();
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
      const spacePressed = spacePressedRef.current;
      const hoverId = hoverRef.current;

      const cursor =
        interaction.kind === "pan" || interaction.kind === "drag"
          ? "grabbing"
          : spacePressed
            ? "grab"
            : session
              ? session.getCursor?.() ?? "crosshair"
              : hoverId
                ? "move"
                : "grab";

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
      const world = screenToWorld(screen);

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

    function handlePointerDown(e: PointerEvent) {
      e.preventDefault();
      canvasEl.setPointerCapture(e.pointerId);

      const spacePressed = spacePressedRef.current;
      const panRequested = spacePressed || e.button === 1 || e.button === 2;

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

      if (toolSessionRef.current) {
        interactionRef.current = { kind: "tool", pointerId: e.pointerId };
        toToolEvent("down", e);
        return;
      }

      if (e.button !== 0) return;

      const hitId = findHitElement(world);
      if (hitId) {
        selectedRef.current = hitId;
        onSelectRef.current?.(hitId);

        const startElement = elementsRef.current.find((it) => it.id === hitId) ?? null;
        if (startElement && updateElementRef.current) {
          interactionRef.current = {
            kind: "drag",
            pointerId: e.pointerId,
            elementId: hitId,
            startScreen: screen,
            startWorld: world,
            startElement,
            moved: false,
          };
        } else {
          interactionRef.current = { kind: "none" };
        }
        requestDraw();
        return;
      }

      interactionRef.current = {
        kind: "pan",
        pointerId: e.pointerId,
        startScreen: screen,
        startCamera: { ...cameraRef.current },
        startedByLeft: true,
        moved: false,
      };
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
        if (!interaction.moved && dx * dx + dy * dy >= 9) interaction.moved = true;
        if (!interaction.moved) return;

        const world = screenToWorld(screen);
        const delta = sub(world, interaction.startWorld);
        const patch = translateElement(interaction.startElement, delta);
        pendingDragPatch = { id: interaction.elementId, patch };
        if (!dragRaf) {
          dragRaf = requestAnimationFrame(() => {
            dragRaf = 0;
            flushDragPatch();
          });
        }
        return;
      }

      if (!toolSessionRef.current && !spacePressedRef.current) {
        const world = screenToWorld(screen);
        const hitId = findHitElement(world);
        if (hitId !== hoverRef.current) {
          hoverRef.current = hitId;
          requestDraw();
        }
      }
    }

    function handlePointerUp(e: PointerEvent) {
      e.preventDefault();

      const interaction = interactionRef.current;
      if (interaction.kind === "tool") {
        toToolEvent("up", e);
        interactionRef.current = { kind: "none" };
        return;
      }

      if (interaction.kind === "pan") {
        if (interaction.pointerId !== e.pointerId) return;
        if (interaction.startedByLeft && !interaction.moved) {
          selectedRef.current = null;
          onSelectRef.current?.(null);
        }
        interactionRef.current = { kind: "none" };
        requestDraw();
        return;
      }

      if (interaction.kind === "drag") {
        if (interaction.pointerId !== e.pointerId) return;
        flushDragPatch();
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
        selectedRef.current = hitId;
        onSelectRef.current?.(hitId);
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
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) return;

      if (e.key === " ") {
        e.preventDefault();
        spacePressedRef.current = true;
        requestDraw();
      }

      const handler = toolSessionRef.current?.onKeyDown;
      if (!handler) return;
      handler(e);
      requestDraw();
    }

    function handleKeyUp(e: KeyboardEvent) {
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
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("keyup", handleKeyUp);
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
      if (dragRaf) cancelAnimationFrame(dragRaf);
      drawRef.current = null;
    };
  }, []);

  return <canvas className="viewportCanvas" ref={canvasRef} style={{ touchAction: "none" }} />;
}
