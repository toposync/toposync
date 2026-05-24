import React from "react";
import type { TopoSyncHost } from "@toposync/plugin-api";

import { SpatialVideoView } from "./SpatialVideoView";
import { spatialVideoTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(spatialVideoTranslations);
  host.registerRenderView({
    id: "spatial_video",
    name: { key: "ext.spatial_video.render.title", fallback: "Visão 360" },
    description: {
      key: "ext.spatial_video.render.desc",
      fallback: "Projeta câmeras ao vivo mapeadas sobre a composição.",
    },
    icon: "street-view",
    order: 40,
    render: (ctx) => <SpatialVideoView {...ctx} />,
  });
}
