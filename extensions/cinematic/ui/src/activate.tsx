import type { ToposyncHost } from "@toposync/plugin-api";

import { createCinematicSettingsPanel } from "./settings/CinematicSettingsPanel";
import { cinematicTranslations } from "./translations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(cinematicTranslations);
  host.registerSettingsPanel(createCinematicSettingsPanel());
}
