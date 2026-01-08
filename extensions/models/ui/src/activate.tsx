import type { TopoSyncHost } from "@toposync/plugin-api";

import { createModelElementType } from "./elements/model_element_type";
import { createImportModelTool } from "./tools/import_model_tool";
import { modelsTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(modelsTranslations);
  host.registerElementType(createModelElementType(host.i18n));
  host.registerEditorTool(createImportModelTool(host.i18n));
}

