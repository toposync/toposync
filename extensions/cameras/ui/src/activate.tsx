import React from "react";
import {
  LivePanoramaSelectorContent,
  LivePanoramaSelectorTrigger,
  LivePanoramaView,
} from "./live/LivePanoramaView";
import type { ToposyncHost } from "@toposync/plugin-api";

import { createCameraElementType } from "./elements/CameraElementType";
import { createCamerasSettingsPanel } from "./settings/CamerasSettingsPanel";
import { createAddCameraTool } from "./tools/addCameraTool";
import { camerasTranslations } from "./translations";
import { createHumanObservationRenderer } from "./notifications/HumanObservationRenderer";
import { humanObservationTranslations } from "./notifications/humanObservationTranslations";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(camerasTranslations);
  host.i18n.registerTranslations(humanObservationTranslations);
  host.registerNotificationRenderer(createHumanObservationRenderer(host));
  host.registerRenderView({
    id: "com.toposync.cameras.live-panorama",
    name: { key: "ext.cameras.live_panorama.title", fallback: "Panorâmica ao vivo" },
    description: { key: "ext.cameras.live_panorama.description", fallback: "Vídeo atual sobre o panorama capturado, com apontamento visual." },
    icon: "video", order: 35,
    toolbarSelector: {
      title: "Selecionar câmera panorâmica",
      renderTrigger: ({ open }) => <LivePanoramaSelectorTrigger open={open} />,
      renderContent: ({ close }) => <LivePanoramaSelectorContent close={close} />,
    },
    render: () => <LivePanoramaView host={host} />,
  });
  host.registerSettingsPanel(createCamerasSettingsPanel(host.ui));
  host.registerElementType(createCameraElementType(host));
  host.registerEditorTool(createAddCameraTool(host.i18n));
}
