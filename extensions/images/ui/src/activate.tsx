import type { ToposyncHost } from "@toposync/plugin-api";

import { createImageFileDropHandler } from "./dropHandlers/imageDropHandler";
import { createImageElementType } from "./elements/ImageElementType";
import { createAddOverlayImageTool, createAddTracingImageTool } from "./tools/addImageTool";
import { imagesTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(imagesTranslations);
  host.registerElementType(createImageElementType(host.i18n));
  host.registerEditorTool(createAddOverlayImageTool(host.i18n));
  host.registerEditorTool(createAddTracingImageTool(host.i18n));
  host.registerFileDropHandler(createImageFileDropHandler(host.i18n));
}
