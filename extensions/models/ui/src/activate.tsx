import type { TopoSyncHost } from "@toposync/plugin-api";

import { createModelElementType } from "./elements/ModelElementType";
import { createImportModelTool } from "./tools/importModelTool";
import { modelsTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(modelsTranslations);
  host.registerElementType(createModelElementType(host.i18n));
  host.registerEditorTool(createImportModelTool(host.i18n));
}
