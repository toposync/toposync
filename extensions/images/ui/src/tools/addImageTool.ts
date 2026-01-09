import type { EditorTool, HostI18n } from "@toposync/plugin-api";

import { uploadToFilesDir } from "../api/filesApi";
import {
  ADD_OVERLAY_IMAGE_TOOL_ID,
  ADD_TRACING_IMAGE_TOOL_ID,
  DEFAULT_IMAGE_OPACITY_OVERLAY,
  DEFAULT_IMAGE_OPACITY_TRACING,
  DEFAULT_IMAGE_WIDTH_METERS,
  IMAGE_ELEMENT_TYPE_ID,
} from "../constants";
import { debugLog } from "../debug";

type Mode = "overlay" | "tracing";

function filenameStem(filename: string): string {
  const base = filename.replace(/^.*[\\/]/, "");
  const idx = base.lastIndexOf(".");
  if (idx <= 0) return base;
  return base.slice(0, idx);
}

async function readImageDimensions(file: File): Promise<{ width: number; height: number } | null> {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    return { width: img.naturalWidth, height: img.naturalHeight };
  } catch {
    return null;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function createAddImageTool(i18n: HostI18n, mode: Mode): EditorTool {
  const id = mode === "tracing" ? ADD_TRACING_IMAGE_TOOL_ID : ADD_OVERLAY_IMAGE_TOOL_ID;
  const nameKey = mode === "tracing" ? "ext.images.tool.add_tracing" : "ext.images.tool.add_overlay";
  const descKey = mode === "tracing" ? "ext.images.tool.add_tracing_desc" : "ext.images.tool.add_overlay_desc";
  const icon = mode === "tracing" ? "layer-group" : "image";

  return {
    id,
    name: { key: nameKey, fallback: mode === "tracing" ? "Tracing image" : "Overlay image" },
    description: { key: descKey },
    icon,
    createSession: ({ createElement, openEditor }) => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = false;
      input.accept = "image/*";
      input.style.position = "fixed";
      input.style.left = "-9999px";
      input.style.width = "1px";
      input.style.height = "1px";
      document.body.appendChild(input);

      let pendingPlacementPoint: { x: number; z: number } | null = null;
      let picking = false;
      let pendingDownAt: { x: number; y: number } | null = null;
      let status: "idle" | "uploading" | "error" = "idle";
      let errorMessage: string | null = null;
      let invalidateCanvas: HTMLCanvasElement | null = null;

      const t = i18n.t;

      function invalidate() {
        invalidateCanvas?.dispatchEvent(new Event("toposync:invalidate"));
      }

      input.addEventListener("change", () => {
        const file = input.files?.[0] ?? null;
        input.value = "";
        if (!file) return;
        void (async () => {
          try {
            if (!pendingPlacementPoint) throw new Error("No placement point selected");

            status = "uploading";
            errorMessage = null;
            invalidate();

            const dims = await readImageDimensions(file);
            const upload = await uploadToFilesDir(file, { filename: file.name });

            const aspect = dims && dims.height > 0 ? dims.width / dims.height : null;
            const widthM = DEFAULT_IMAGE_WIDTH_METERS;
            const depthM = aspect ? widthM / aspect : widthM;

            const opacity = mode === "tracing" ? DEFAULT_IMAGE_OPACITY_TRACING : DEFAULT_IMAGE_OPACITY_OVERLAY;
            const blend = mode === "tracing" ? "multiply" : "normal";

            const createdElementId = createElement(IMAGE_ELEMENT_TYPE_ID, {
              name: filenameStem(file.name) || (mode === "tracing" ? t("ext.images.editor.mode.tracing") : t("ext.images.editor.mode.overlay")),
              position: { x: pendingPlacementPoint.x, y: 0, z: pendingPlacementPoint.z },
              props: {
                dir: upload.dir,
                file: upload.filename,
                width_m: widthM,
                depth_m: depthM,
                opacity,
                mode,
                blend,
                pixel_width: dims?.width ?? null,
                pixel_height: dims?.height ?? null,
              },
            });

            if (createdElementId) openEditor(createdElementId);

            status = "idle";
            pendingPlacementPoint = null;
            invalidate();
          } catch (err) {
            status = "error";
            errorMessage = err instanceof Error ? err.message : String(err);
            console.error("[images:tool] import failed", err);
            invalidate();
          }
        })();
      });

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            picking = false;
            pendingDownAt = null;
            pendingPlacementPoint = null;
            return;
          }

          if (event.kind === "down") {
            if (event.button !== 0) return;
            if (status !== "idle") return;
            pendingPlacementPoint = { x: event.world.x, z: event.world.z };
            picking = true;
            pendingDownAt = { x: event.screen.x, y: event.screen.y };
            debugLog("[images:tool] pointer down", { world: event.world, screen: event.screen });
            return;
          }

          if (event.kind === "move") {
            if (!picking || !pendingDownAt) return;
            const dx = event.screen.x - pendingDownAt.x;
            const dy = event.screen.y - pendingDownAt.y;
            if (dx * dx + dy * dy > 16) {
              picking = false;
              pendingDownAt = null;
            }
            return;
          }

          if (event.kind === "up") {
            if (!picking) return;
            picking = false;
            pendingDownAt = null;
            if (event.button !== 0) return;
            if (status !== "idle") return;
            if (!pendingPlacementPoint) return;
            debugLog("[images:tool] open picker", { pendingPlacementPoint });
            input.click();
          }
        },
        onKeyDown: (e) => {
          if (e.key === "Escape") {
            pendingPlacementPoint = null;
            picking = false;
            pendingDownAt = null;
            status = "idle";
            errorMessage = null;
            invalidate();
          }
        },
        renderOverlay2D: ({ ctx, viewport }) => {
          invalidateCanvas = viewport.canvas;

          const message =
            status === "uploading"
              ? t("ext.images.editor.uploading")
              : status === "error"
                ? `${t("ext.images.editor.failed")}${errorMessage ? `: ${errorMessage}` : ""}`
                : null;

          if (!message) return;

          ctx.save();
          ctx.font = "12px ui-sans-serif, system-ui";
          ctx.textBaseline = "top";
          const w = viewport.width;
          const textWidth = ctx.measureText(message).width;
          const boxWidth = Math.min(w - 24, textWidth + 20);
          const x = (w - boxWidth) / 2;
          const y = 14;
          ctx.fillStyle = "rgba(8,12,26,0.82)";
          ctx.strokeStyle = "rgba(255,255,255,0.14)";
          ctx.lineWidth = 1;
          drawRoundedRect(ctx, x, y, boxWidth, 28, 10);
          ctx.fillStyle = "rgba(230,232,242,0.92)";
          ctx.fillText(message, x + 10, y + 8);
          ctx.restore();
        },
        getCursor: () => (status === "idle" ? "copy" : "wait"),
        dispose: () => {
          debugLog("[images:tool] disposed");
          input.remove();
        },
      };
    },
  };
}

function drawRoundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): void {
  const r = Math.max(0, Math.min(radius, Math.min(width, height) / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
}

export function createAddOverlayImageTool(i18n: HostI18n): EditorTool {
  return createAddImageTool(i18n, "overlay");
}

export function createAddTracingImageTool(i18n: HostI18n): EditorTool {
  return createAddImageTool(i18n, "tracing");
}
