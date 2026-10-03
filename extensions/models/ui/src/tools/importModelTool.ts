import { requestForm, resolveToposyncUrl, type EditorTool, type HostI18n, type HostUi, type PlanePoint } from "@toposync/plugin-api";

import { openModelSourcePicker } from "./modelSourcePicker";
import { prepareObjectImage, reviewObjectImage } from "./imagePreparation";
import { editObjectContour, type ObjectContour } from "./objectContour";
import type { UploadFileResponse } from "../types";

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

export function createImportModelTool(i18n: HostI18n, ui: HostUi): EditorTool {
  return {
    id: IMPORT_MODEL_TOOL_ID,
    name: { key: "ext.models.tool.import", fallback: "3D model" },
    description: { key: "ext.models.tool.hint", fallback: "Click to import a model or generate one from an object image." },
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
      let activityState: "idle" | "uploading" | "preparing" | "reviewing" | "generating" | "processing" = "idle";
      let lastError: string | null = null;
      let operation: AbortController | null = null;
      let disposed = false;
      let closePicker: (() => void) | null = null;
      let lastCanvas: HTMLCanvasElement | null = null;

      const t = i18n.t;

      const progress = document.createElement("dialog");
      progress.setAttribute("aria-label", t("ext.models.tool.import"));
      progress.style.cssText = "width:min(420px,calc(100vw - 32px));box-sizing:border-box;padding:24px;border:1px solid var(--color-border-strong,#64748b);border-radius:18px;background:var(--color-surface-solid,#fff);color:var(--color-text-primary,#101418)";
      const status = document.createElement("p");
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const dismiss = document.createElement("button");
      dismiss.className = "chipButton";
      dismiss.style.minHeight = "44px";
      dismiss.addEventListener("click", cancel);
      progress.addEventListener("cancel", (event) => { event.preventDefault(); cancel(); });
      progress.append(status, dismiss);
      document.body.appendChild(progress);

      function invalidateViewport() {
        const message = lastError ?? (["idle", "reviewing"].includes(activityState) ? "" : t(`ext.models.editor.${activityState}`));
        status.textContent = message;
        dismiss.textContent = t(lastError ? "ext.models.editor.close" : "ext.models.editor.cancel");
        if (message && !progress.open && !disposed) progress.showModal();
        if (!message && progress.open) progress.close();
        lastCanvas?.dispatchEvent(new Event("toposync:invalidate"));
      }

      function cancel() {
        closePicker?.(); closePicker = null;
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
        const imageFile = !entryFile && files.length === 1 && /\.(png|jpe?g|webp)$/i.test(files[0].name) ? files[0] : null;
        if (!entryFile && !imageFile) throw new Error(t("ext.models.error.pick_entry"));
        if (imageFile && imageFile.size > 8 * 1024 ** 2) throw new Error(t("ext.models.error.generation_image_too_large"));
        if (!placement) throw new Error("No placement point selected");
        signal.throwIfAborted();

        debugLog(
          "[models:tool] handleFiles",
          files.map((file) => ({ name: file.name, size: file.size, type: file.type })),
        );

        activityState = imageFile ? "preparing" : "uploading";
        lastError = null;
        invalidateViewport();

        let generation: Record<string, unknown> | undefined;
        let entryUpload: UploadFileResponse;
        if (imageFile) {
          try {
            let prepared = await prepareObjectImage(imageFile, signal);
            let contour: ObjectContour | undefined;
            signal.throwIfAborted();
            activityState = "reviewing";
            invalidateViewport();
            while (true) {
              const choice = await reviewObjectImage(imageFile, prepared.image, t, signal, cancel, ui, contour);
              if (choice === "replace") return "choose_another_image";
              if (choice === "generate") break;
              const edited = await editObjectContour(imageFile, ui, t, signal, contour);
              if (edited) { prepared = edited.prepared; contour = edited.points; }
              signal.throwIfAborted();
            }
            signal.throwIfAborted();
            const form = new FormData();
            form.append("file", imageFile);
            form.append("expected_prepared_sha256", prepared.digest);
            if (contour) form.append("foreground_polygon", JSON.stringify(contour));
            activityState = "generating";
            invalidateViewport();
            const result = await requestForm<UploadFileResponse & { generation: Record<string, unknown> }>("/api/models/generate", form, { signal });
            entryUpload = result;
            generation = result.generation;
          } catch (error) {
            signal.throwIfAborted();
            const code = error instanceof Error ? error.message : "";
            const known = ["invalid_prepared_image", "generation_preparation_changed", "background_removal_not_installed", "local_generation_not_installed", "local_generation_busy", "isolated_object_image_required", "invalid_generation_image", "generation_image_too_large", "generation_timeout", "generation_memory_limit"];
            throw new Error(t(`ext.models.error.${known.includes(code) ? code : "local_generation_failed"}`));
          }
        } else {
          entryUpload = await uploadToFilesDir(entryFile!, { filename: entryFile!.name, signal });
        }
        signal.throwIfAborted();
        const dir = entryUpload.dir;
        const entryName = entryUpload.filename;

        if (entryFile) for (const file of files) {
          if (file === entryFile) continue;
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

        const inferredName = stripFileExtension(imageFile?.name ?? entryName);
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
            ...(generation ? { generation } : {}),
          },
        });
        progress.close();
        if (id) toolContext.openEditor(id);
        debugLog("[models:tool] element created", { id });
      }

      function startImport(files: File[]) {
        if (disposed || operation) return;
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
          let chooseAnother = false;
          try {
            chooseAnother = await handleFiles(files, controller.signal, placement) === "choose_another_image";
          } catch (err) {
            if (!controller.signal.aborted && !disposed) {
              lastError = err instanceof Error ? err.message : String(err);
              console.error("[models:tool] import failed", err);
            }
          } finally {
            if (operation === controller) {
              operation = null;
              activityState = "idle";
              pendingPlacementPoint = chooseAnother ? placement : null;
              invalidateViewport();
              if (chooseAnother && !disposed && !controller.signal.aborted) input.click();
            }
          }
        })();
      }
      input.addEventListener("change", () => {
        const files = input.files ? Array.from(input.files) : [];
        input.value = "";
        startImport(files);
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
            closePicker?.();
            closePicker = openModelSourcePicker(t, {
              local: () => { closePicker?.(); closePicker = null; input.click(); },
              stored: file => { closePicker?.(); closePicker = null; startImport([file]); },
              cancel,
            });
          }
        },
        onKeyDown: (event) => {
          if (event.key === "Escape") {
            cancel();
            isPointerArmed = false;
            pointerDownScreen = null;
          }
        },
        renderOverlay2D: ({ viewport }) => {
          lastCanvas = viewport.canvas;
        },
        getCursor: () => (activityState === "idle" ? "copy" : "wait"),
        dispose: () => {
          debugLog("[models:tool] session disposed");
          disposed = true;
          cancel();
          input.remove();
          progress.remove();
        },
      };
    },
  };
}
