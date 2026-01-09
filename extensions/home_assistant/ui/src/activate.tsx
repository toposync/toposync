import type { TopoSyncHost } from "@toposync/plugin-api";

import { createHomeAssistantElementType } from "./elements/home_assistant_element_type";
import { createHomeAssistantSettingsPanel } from "./settings/home_assistant_settings_panel";
import { createAddHomeAssistantTool } from "./tools/add_home_assistant_tool";
import { homeAssistantTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(homeAssistantTranslations);
  host.registerSettingsPanel(createHomeAssistantSettingsPanel());
  host.registerElementType(createHomeAssistantElementType(host.i18n));
  host.registerEditorTool(createAddHomeAssistantTool(host.i18n));
}

