import { resolveToposyncUrl, type EditorTool, type HostI18n, type PlanePoint } from "@toposync/plugin-api";

import { MAXIMUM_MODEL_BYTES } from "../validation";
import { uploadToFilesDir } from "../api/filesApi";
import { debugLog } from "../debug";
import { IMPORT_MODEL_TOOL_ID, MODEL_ELEMENT_TYPE_ID } from "../constants";
import { generateModelTopDownPreview, stripFileExtension, suggestInitialScale } from "../preview/modelPreview";

const TOOL_GROUP_REFERENCES: NonNullable<EditorTool["group"]> = {
  id: "references",
  name: { key: "core.ui.tools.group.references", fallback: "References" },
  order: 10,
};

export function createImportModelTool(i18n: HostI18n): EditorTool {
  return {
    id: IMPORT_MODEL_TOOL_ID,
    name: { key: "ext.models.tool.import", fallback: "3D model" },
    description: { key: "ext.models.tool.hint", fallback: "Click to upload and place a model." },
    icon: "cube",
    group: TOOL_GROUP_REFERENCES,
    order: 30,
    createSession: (toolContext) => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.accept = ".glb,.gltf,.bin,.png,.jpg,.jpeg,.webp";
      input.style.position = "fixed";
      input.style.left = "-9999px";
      input.style.width = "1px";
      input.style.height = "1px";
      document.body.appendChild(input);

      let pendingPlacementPoint: PlanePoint | null = null;
      let isPointerArmed = false;
      let pointerDownScreen: { x: number; y: number } | null = null;
      let activityState: "idle" | "uploading" | "processing" = "idle";
      let lastError: string | null = null;
      let operation: AbortController | null = null;
      let disposed = false;
      let lastCanvas: HTMLCanvasElement | null = null;

      const t = i18n.t;

      function invalidateViewport() {
        lastCanvas?.dispatchEvent(new Event("toposync:invalidate"));
      }

      debugLog("[models:tool] session created");

      function drawPill(canvas: CanvasRenderingContext2D, x: number, y: number, width: number, height: number) {
        const radius = Math.min(999, Math.min(width, height) / 2);
        canvas.beginPath();
        canvas.moveTo(x + radius, y);
        canvas.lineTo(x + width - radius, y);
        canvas.quadraticCurveTo(x + width, y, x + width, y + radius);
        canvas.lineTo(x + width, y + height - radius);
        canvas.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
        canvas.lineTo(x + radius, y + height);
        canvas.quadraticCurveTo(x, y + height, x, y + height - radius);
        canvas.lineTo(x, y + radius);
        canvas.quadraticCurveTo(x, y, x + radius, y);
        canvas.closePath();
      }

      function cancel() {
        operation?.abort();
        operation = null;
        activityState = "idle";
        pendingPlacementPoint = null;
        lastError = null;
        invalidateViewport();
      }

      async function handleFiles(files: File[], signal: AbortSignal, placement: PlanePoint | null) {
        if (files.length === 0) return;
        if (files.reduce((total, file) => total + file.size, 0) > MAXIMUM_MODEL_BYTES) {
          throw new Error(t("ext.models.error.too_large"));
        }

        const entryFile =
          files.find((file) => file.name.toLowerCase().endsWith(".glb")) ??
          files.find((file) => file.name.toLowerCase().endsWith(".gltf"));
        if (!entryFile) throw new Error(t("ext.models.error.pick_entry"));
        if (!placement) throw new Error("No placement point selected");
        signal.throwIfAborted();

        debugLog(
          "[models:tool] handleFiles",
          files.map((file) => ({ name: file.name, size: file.size, type: file.type })),
        );

        activityState = "uploading";
        lastError = null;
        invalidateViewport();

        debugLog("[models:tool] uploading entry", { name: entryFile.name });
        const entryUpload = await uploadToFilesDir(entryFile, { filename: entryFile.name, signal });
        signal.throwIfAborted();
        const dir = entryUpload.dir;
        const entryName = entryUpload.filename;

        for (const file of files) {
          if (file === entryFile) continue;
          debugLog("[models:tool] uploading asset", { dir, name: file.name });
          await uploadToFilesDir(file, { dir, filename: file.name, signal });
          signal.throwIfAborted();
        }

        const modelUrl = resolveToposyncUrl(`/files/${encodeURIComponent(dir)}/${encodeURIComponent(entryName)}`);

        activityState = "processing";
        invalidateViewport();
        debugLog("[models:tool] generating preview", { modelUrl });
        const preview = await generateModelTopDownPreview(modelUrl);
        signal.throwIfAborted();
        const previewBlob = await (await fetch(preview.dataUrl, { signal })).blob();
        signal.throwIfAborted();
        const previewUpload = await uploadToFilesDir(previewBlob, { dir, filename: "preview.png", signal });
        signal.throwIfAborted();
        debugLog("[models:tool] preview uploaded", { url: previewUpload.url });

        const inferredName = stripFileExtension(entryName);
        const id = toolContext.createElement(MODEL_ELEMENT_TYPE_ID, {
          name: inferredName,
          position: { x: placement.x, y: 0, z: placement.z },
          props: {
            dir,
            model: entryName,
            preview: previewUpload.filename,
            size: preview.size,
            center: preview.center,
            min_y: preview.minY,
            scale: suggestInitialScale(preview.size),
            animation_enabled: false,
          },
        });
        if (id) toolContext.openEditor(id);
        debugLog("[models:tool] element created", { id });
      }

      input.addEventListener("change", () => {
        if (disposed || operation) return;
        const files = input.files ? Array.from(input.files) : [];
        input.value = "";
        debugLog("[models:tool] input change", { count: files.length, pendingPlacementPoint });
        if (files.length === 0) {
          activityState = "idle";
          pendingPlacementPoint = null;
          return;
        }

        const controller = new AbortController();
        const placement = pendingPlacementPoint;
        operation = controller;
        void (async () => {
          try {
            await handleFiles(files, controller.signal, placement);
          } catch (err) {
            if (!controller.signal.aborted && !disposed) {
              lastError = err instanceof Error ? err.message : String(err);
              console.error("[models:tool] import failed", err);
            }
          } finally {
            if (operation === controller) {
              operation = null;
              activityState = "idle";
              pendingPlacementPoint = null;
              invalidateViewport();
            }
          }
        })();
      });

      return {
        onPointerEvent: (event) => {
          if (event.kind === "cancel") {
            isPointerArmed = false;
            pointerDownScreen = null;
            debugLog("[models:tool] pointer cancel");
            return;
          }
          if (event.kind === "down") {
            if (event.button !== 0) return;
            if (activityState !== "idle") return;
            pendingPlacementPoint = event.world;
            isPointerArmed = true;
            pointerDownScreen = { x: event.screen.x, y: event.screen.y };
            debugLog("[models:tool] pointer down", { world: event.world, screen: event.screen });
            return;
          }
          if (event.kind === "move") {
            if (!isPointerArmed || !pointerDownScreen) return;
            const dx = event.screen.x - pointerDownScreen.x;
            const dy = event.screen.y - pointerDownScreen.y;
            if (dx * dx + dy * dy > 16) {
              isPointerArmed = false;
              pointerDownScreen = null;
            }
            return;
          }
          if (event.kind === "up") {
            if (!isPointerArmed) return;
            isPointerArmed = false;
            pointerDownScreen = null;
            if (event.button !== 0) return;
            if (activityState !== "idle") return;
            if (!pendingPlacementPoint) return;
            debugLog("[models:tool] pointer up -> open picker", { pendingPlacementPoint });
            input.click();
          }
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") {
            cancel();
            isPointerArmed = false;
            pointerDownScreen = null;
          }
        },
        renderOverlay2D: ({ ctx: canvas, viewport }) => {
          lastCanvas = viewport.canvas;
          if (activityState === "idle" && !lastError) return;

          const message =
            activityState === "uploading"
              ? t("ext.models.editor.uploading")
              : activityState === "processing"
                ? t("ext.models.editor.processing")
                : lastError
                  ? `${t("ext.models.editor.failed")}: ${lastError}`
                  : "";

          if (!message) return;

          canvas.save();
          canvas.font = "12px ui-sans-serif, system-ui";
          canvas.textBaseline = "top";

          const padding = 10;
          const viewportWidth = viewport.width;
          const textWidth = canvas.measureText(message).width;
          const boxWidth = Math.min(viewportWidth - 24, textWidth + padding * 2);
          const x0 = (viewportWidth - boxWidth) / 2;
          const y0 = 14;

          canvas.fillStyle = "rgba(8,12,26,0.82)";
          canvas.strokeStyle = "rgba(255,255,255,0.14)";
          canvas.lineWidth = 1;
          drawPill(canvas, x0, y0, boxWidth, 28);
          canvas.fill();
          canvas.stroke();

          canvas.fillStyle = "rgba(230,232,242,0.92)";
          canvas.fillText(message, x0 + padding, y0 + 8);

          canvas.restore();
        },
        getCursor: () => (activityState === "idle" ? "copy" : "wait"),
        dispose: () => {
          debugLog("[models:tool] session disposed");
          disposed = true;
          cancel();
          input.remove();
        },
      };
    },
  };
}
