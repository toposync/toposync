import type { TopoSyncHost } from "@toposync/plugin-api";

import { createHomeAssistantElementType } from "./elements/HomeAssistantElementType";
import { createHomeAssistantSettingsPanel } from "./settings/HomeAssistantSettingsPanel";
import { createAddHomeAssistantTool } from "./tools/addHomeAssistantTool";
import { homeAssistantTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(homeAssistantTranslations);
  host.registerSettingsPanel(createHomeAssistantSettingsPanel());
  host.registerElementType(createHomeAssistantElementType(host.i18n));
  host.registerEditorTool(createAddHomeAssistantTool(host.i18n));
}
