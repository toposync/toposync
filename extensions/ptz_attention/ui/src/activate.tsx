import type { ToposyncHost } from "@toposync/plugin-api";

import { createPtzAttentionRequestOperatorPanel } from "./operators/PtzAttentionRequestPanel";
import { createPtzAttentionSettingsPanel } from "./settings/PtzAttentionSettingsPanel";
import { installPtzAttentionStyles } from "./styles";
import { ptzAttentionTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  installPtzAttentionStyles();
  host.i18n.registerTranslations(ptzAttentionTranslations);
  host.registerSettingsPanel(createPtzAttentionSettingsPanel());
  host.registerPipelineOperatorPanel(createPtzAttentionRequestOperatorPanel(host.api));
}
