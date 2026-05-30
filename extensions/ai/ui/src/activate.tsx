import type { ToposyncHost } from "@toposync/plugin-api";

import { createAiConditionFilterOperatorPanel, createAiSmartCropOperatorPanel } from "./operators/AiOperatorPanels";
import { createAiSettingsPanel } from "./settings/AiSettingsPanel";
import { aiTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(aiTranslations);
  host.registerSettingsPanel(createAiSettingsPanel());
  host.registerPipelineOperatorPanel(createAiSmartCropOperatorPanel());
  host.registerPipelineOperatorPanel(createAiConditionFilterOperatorPanel());
}
