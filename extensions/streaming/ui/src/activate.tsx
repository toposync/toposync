import type { ToposyncHost } from "@toposync/plugin-api";

import { createStreamingSettingsPanel } from "./settings/StreamingSettingsPanel";
import { streamingTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(streamingTranslations);
  host.registerSettingsPanel(createStreamingSettingsPanel());
}
