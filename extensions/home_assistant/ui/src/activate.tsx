import React from "react";
import { SpeechPanel } from "./pipelines/SpeechPanel";
import type { ToposyncHost } from "@toposync/plugin-api";

import { createHomeAssistantElementType } from "./elements/HomeAssistantElementType";
import { createHomeAssistantSettingsPanel } from "./settings/HomeAssistantSettingsPanel";
import { createAddHomeAssistantTool } from "./tools/addHomeAssistantTool";
import { homeAssistantTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(homeAssistantTranslations);
  host.registerPipelineOperatorPanel({ id: "home-assistant-speech", operatorId: "home_assistant.speak", render: (args) => <SpeechPanel {...args} /> });
  host.registerSettingsPanel(createHomeAssistantSettingsPanel());
  host.registerElementType(createHomeAssistantElementType(host.i18n));
  host.registerEditorTool(createAddHomeAssistantTool(host.i18n));
}
