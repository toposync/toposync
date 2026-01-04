import React, { useEffect, useRef } from "react";

import type { CompositionElement } from "@toposync/plugin-api";

type Props = {
  elements: CompositionElement[];
};

export function Viewport2D({ elements }: Props): React.ReactElement {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const canvasEl: HTMLCanvasElement = canvas;
    const ctx2d: CanvasRenderingContext2D = ctx;

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = canvasEl.clientWidth;
      const h = canvasEl.clientHeight;
      canvasEl.width = Math.max(1, Math.floor(w * dpr));
      canvasEl.height = Math.max(1, Math.floor(h * dpr));
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
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

      const grid = 48;
      ctx2d.strokeStyle = "rgba(255,255,255,0.05)";
      ctx2d.lineWidth = 1;
      for (let x = 0; x <= w; x += grid) {
        ctx2d.beginPath();
        ctx2d.moveTo(x + 0.5, 0);
        ctx2d.lineTo(x + 0.5, h);
        ctx2d.stroke();
      }
      for (let y = 0; y <= h; y += grid) {
        ctx2d.beginPath();
        ctx2d.moveTo(0, y + 0.5);
        ctx2d.lineTo(w, y + 0.5);
        ctx2d.stroke();
      }

      const originX = w / 2;
      const originY = h / 2;
      const scale = 52;

      ctx2d.fillStyle = "rgba(251,191,36,0.95)";
      ctx2d.strokeStyle = "rgba(0,0,0,0.35)";
      ctx2d.font = "12px ui-sans-serif, system-ui";

      for (const el of elements) {
        const x = originX + el.position.x * scale;
        const y = originY + el.position.z * scale;
        ctx2d.beginPath();
        ctx2d.arc(x, y, 7, 0, Math.PI * 2);
        ctx2d.fill();
        ctx2d.stroke();

        ctx2d.fillStyle = "rgba(230,232,242,0.85)";
        ctx2d.fillText(el.name || el.type, x + 10, y + 4);
        ctx2d.fillStyle = "rgba(251,191,36,0.95)";
      }
    }

    const ro = new ResizeObserver(resize);
    ro.observe(canvasEl);
    resize();

    return () => ro.disconnect();
  }, [elements]);

  return <canvas className="viewportCanvas" ref={canvasRef} />;
}
