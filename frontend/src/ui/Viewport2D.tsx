import React, { useEffect, useRef } from "react";

import type { CompositionElement, EditorToolSession, ElementType, PlanePoint, Vector2, Viewport2DContext } from "@toposync/plugin-api";

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  activeToolSession?: EditorToolSession | null;
};

function toVector2(x: number, y: number): Vector2 {
  return { x, y };
}

function toPlanePoint(x: number, z: number): PlanePoint {
  return { x, z };
}

export function Viewport2D({ elements, elementTypesById, activeToolSession }: Props): React.ReactElement {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawRef = useRef<(() => void) | null>(null);

  const elementsRef = useRef<CompositionElement[]>(elements);
  const elementTypesRef = useRef<Record<string, ElementType>>(elementTypesById);
  const toolSessionRef = useRef<EditorToolSession | null>(activeToolSession ?? null);

  useEffect(() => {
    elementsRef.current = elements;
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
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const canvasEl: HTMLCanvasElement = canvas;
    const ctx2d: CanvasRenderingContext2D = ctx;

    let raf = 0;
    let lastViewport: Viewport2DContext | null = null;

    function requestDraw() {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        draw();
      });
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
      const scale = 52;

      function worldToScreen(p: PlanePoint): Vector2 {
        return toVector2(originX + p.x * scale, originY + p.z * scale);
      }

      function screenToWorld(p: Vector2): PlanePoint {
        return toPlanePoint((p.x - originX) / scale, (p.y - originY) / scale);
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
      lastViewport = viewport;

      const tl = screenToWorld(toVector2(0, 0));
      const br = screenToWorld(toVector2(w, h));
      const minX = Math.min(tl.x, br.x);
      const maxX = Math.max(tl.x, br.x);
      const minZ = Math.min(tl.z, br.z);
      const maxZ = Math.max(tl.z, br.z);

      const gridWorld = 1;
      const firstX = Math.floor(minX / gridWorld) * gridWorld;
      const firstZ = Math.floor(minZ / gridWorld) * gridWorld;

      ctx2d.strokeStyle = "rgba(255,255,255,0.055)";
      ctx2d.lineWidth = 1;
      for (let x = firstX; x <= maxX; x += gridWorld) {
        const sx = Math.round(worldToScreen(toPlanePoint(x, 0)).x) + 0.5;
        ctx2d.beginPath();
        ctx2d.moveTo(sx, 0);
        ctx2d.lineTo(sx, h);
        ctx2d.stroke();
      }
      for (let z = firstZ; z <= maxZ; z += gridWorld) {
        const sy = Math.round(worldToScreen(toPlanePoint(0, z)).y) + 0.5;
        ctx2d.beginPath();
        ctx2d.moveTo(0, sy);
        ctx2d.lineTo(w, sy);
        ctx2d.stroke();
      }

      // Axes
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

      const session = toolSessionRef.current;
      if (session?.renderOverlay2D) {
        try {
          session.renderOverlay2D({ ctx: ctx2d, viewport });
        } catch (err) {
          console.error("[toolOverlay]", err);
        }
      }

      const cursor = session?.getCursor?.() ?? (session ? "crosshair" : "default");
      canvasEl.style.cursor = cursor;
    }

    const ro = new ResizeObserver(resize);
    ro.observe(canvasEl);
    resize();

    function toToolEvent(kind: "down" | "move" | "up" | "cancel" | "dblclick", e: PointerEvent): void {
      const session = toolSessionRef.current;
      if (!session?.onPointerEvent) return;
      if (!lastViewport) return;

      const rect = canvasEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const screen = toVector2(x, y);
      const world = lastViewport.screenToWorld(screen);
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

    function handlePointerDown(e: PointerEvent) {
      if (!toolSessionRef.current) return;
      e.preventDefault();
      canvasEl.setPointerCapture(e.pointerId);
      toToolEvent("down", e);
    }

    function handlePointerMove(e: PointerEvent) {
      if (!toolSessionRef.current) return;
      e.preventDefault();
      toToolEvent("move", e);
    }

    function handlePointerUp(e: PointerEvent) {
      if (!toolSessionRef.current) return;
      e.preventDefault();
      toToolEvent("up", e);
    }

    function handlePointerCancel(e: PointerEvent) {
      if (!toolSessionRef.current) return;
      e.preventDefault();
      toToolEvent("cancel", e);
    }

    function handleDoubleClick(e: MouseEvent) {
      const session = toolSessionRef.current;
      if (!session?.onPointerEvent) return;
      if (!lastViewport) return;

      const rect = canvasEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const screen = toVector2(x, y);
      const world = lastViewport.screenToWorld(screen);
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
    }

    function handleKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) return;

      const handler = toolSessionRef.current?.onKeyDown;
      if (!handler) return;
      handler(e);
      requestDraw();
    }

    canvasEl.addEventListener("pointerdown", handlePointerDown);
    canvasEl.addEventListener("pointermove", handlePointerMove);
    canvasEl.addEventListener("pointerup", handlePointerUp);
    canvasEl.addEventListener("pointercancel", handlePointerCancel);
    canvasEl.addEventListener("dblclick", handleDoubleClick);
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      canvasEl.removeEventListener("pointerdown", handlePointerDown);
      canvasEl.removeEventListener("pointermove", handlePointerMove);
      canvasEl.removeEventListener("pointerup", handlePointerUp);
      canvasEl.removeEventListener("pointercancel", handlePointerCancel);
      canvasEl.removeEventListener("dblclick", handleDoubleClick);
      window.removeEventListener("keydown", handleKeyDown);
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
      drawRef.current = null;
    };
  }, []);

  return <canvas className="viewportCanvas" ref={canvasRef} style={{ touchAction: "none" }} />;
}
