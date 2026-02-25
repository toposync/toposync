import type { TopoSyncHost } from "@toposync/plugin-api";

import { createStreamingSettingsPanel } from "./settings/StreamingSettingsPanel";
import { streamingTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(streamingTranslations);
  host.registerSettingsPanel(createStreamingSettingsPanel());
}
