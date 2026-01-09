import type { TopoSyncHost } from "@toposync/plugin-api";

import { createImageElementType } from "./elements/ImageElementType";
import { createAddOverlayImageTool, createAddTracingImageTool } from "./tools/addImageTool";
import { imagesTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(imagesTranslations);
  host.registerElementType(createImageElementType(host.i18n));
  host.registerEditorTool(createAddOverlayImageTool(host.i18n));
  host.registerEditorTool(createAddTracingImageTool(host.i18n));
}

