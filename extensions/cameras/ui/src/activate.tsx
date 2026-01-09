import type { TopoSyncHost } from "@toposync/plugin-api";

import { createCameraElementType } from "./elements/CameraElementType";
import { createCamerasSettingsPanel } from "./settings/CamerasSettingsPanel";
import { createAddCameraTool } from "./tools/addCameraTool";
import { camerasNeonBlueTheme } from "./theme";
import { camerasTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(camerasTranslations);
  host.registerTheme(camerasNeonBlueTheme);
  host.registerSettingsPanel(createCamerasSettingsPanel());
  host.registerElementType(createCameraElementType(host));
  host.registerEditorTool(createAddCameraTool(host.i18n));
}
