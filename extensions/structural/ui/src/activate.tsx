import type { TopoSyncHost } from "@toposync/plugin-api";

import { createAreaElementType } from "./elements/area_element_type";
import { createWallElementType } from "./elements/wall_element_type";
import { createStructuralTools } from "./tools/structural_tools";
import { structuralTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(structuralTranslations);
  host.registerElementType(createWallElementType(host.i18n));
  host.registerElementType(createAreaElementType(host.i18n));
  for (const tool of createStructuralTools(host.i18n)) host.registerEditorTool(tool);
}

