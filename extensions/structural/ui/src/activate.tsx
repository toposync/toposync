import type { ToposyncHost } from "@toposync/plugin-api";

import { createAreaElementType } from "./elements/AreaElementType";
import { createPoolElementType } from "./elements/PoolElementType";
import { createWallElementType } from "./elements/WallElementType";
import { createStructuralTools } from "./tools/structuralTools";
import { structuralTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(structuralTranslations);
  host.registerElementType(createWallElementType(host.i18n));
  host.registerElementType(createAreaElementType(host.i18n));
  host.registerElementType(createPoolElementType(host.i18n));
  for (const tool of createStructuralTools(host.i18n)) host.registerEditorTool(tool);
}
