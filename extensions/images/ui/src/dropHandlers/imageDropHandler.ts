import type { EditorFileDropEvent, FileDropHandler, FileDropHandlerContext, HostI18n, PlanePoint } from "@toposync/plugin-api";

import { uploadToFilesDir } from "../api/filesApi";
import {
  DEFAULT_IMAGE_OPACITY_OVERLAY,
  DEFAULT_IMAGE_OPACITY_TRACING,
  DEFAULT_IMAGE_WIDTH_METERS,
  IMAGE_ELEMENT_TYPE_ID,
} from "../constants";
import { filenameStem, isImageFile, readImageDimensions } from "../imageUtils";

function viewportCenterWorld(event: EditorFileDropEvent): PlanePoint {
  return event.viewport.screenToWorld({ x: event.viewport.width / 2, y: event.viewport.height / 2 });
}

export function createImageFileDropHandler(i18n: HostI18n): FileDropHandler {
  const t = i18n.t;

  return {
    id: "com.toposync.images.dropHandler.image",
    canHandle: (event) => event.files.some((file) => isImageFile(file)),
    handle: async (ctx: FileDropHandlerContext, event: EditorFileDropEvent) => {
      const file = event.files.find((f) => isImageFile(f)) ?? null;
      if (!file) return false;

      const dims = await readImageDimensions(file);
      const upload = await uploadToFilesDir(file, { filename: file.name });

      const hasNonImageElements = ctx.elements.some((el) => el.type !== IMAGE_ELEMENT_TYPE_ID);
      const mode = hasNonImageElements ? "overlay" : "tracing";

      const aspect = dims && dims.height > 0 ? dims.width / dims.height : null;
      const widthM = DEFAULT_IMAGE_WIDTH_METERS;
      const depthM = aspect ? widthM / aspect : widthM;

      const opacity = mode === "tracing" ? DEFAULT_IMAGE_OPACITY_TRACING : DEFAULT_IMAGE_OPACITY_OVERLAY;
      const blend = mode === "tracing" ? "multiply" : "normal";

      const center = viewportCenterWorld(event);
      const name =
        filenameStem(file.name) ||
        (mode === "tracing" ? t("ext.images.editor.mode.tracing") : t("ext.images.editor.mode.overlay"));

      const createdElementId = ctx.createElement(IMAGE_ELEMENT_TYPE_ID, {
        name,
        position: { x: center.x, y: 0, z: center.z },
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

      if (createdElementId) ctx.openEditor(createdElementId);
      return true;
    },
  };
}

