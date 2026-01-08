import type { TopoSyncHost } from "@toposync/plugin-api";

import { createCameraElementType } from "./elements/camera_element_type";
import { createCamerasSettingsPanel } from "./settings/cameras_settings_panel";
import { createAddCameraTool } from "./tools/add_camera_tool";
import { camerasNeonBlueTheme } from "./theme";
import { camerasTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(camerasTranslations);
  host.registerTheme(camerasNeonBlueTheme);
  host.registerSettingsPanel(createCamerasSettingsPanel());
  host.registerElementType(createCameraElementType(host));
  host.registerEditorTool(createAddCameraTool(host.i18n));
}

