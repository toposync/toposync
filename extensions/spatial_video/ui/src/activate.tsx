import React from "react";
import type { TopoSyncHost } from "@toposync/plugin-api";

import { SpatialVideoView } from "./SpatialVideoView";
import {
  readSpatialVideoSettings,
  SPATIAL_VIDEO_MESH_DENSITY_OPTIONS,
  SPATIAL_VIDEO_PROJECTION_OPTIONS,
  SPATIAL_VIDEO_RENDER_VIEW_ID,
} from "./spatialSettings";
import { spatialVideoTranslations } from "./translations";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(spatialVideoTranslations);
  host.registerRenderView({
    id: SPATIAL_VIDEO_RENDER_VIEW_ID,
    name: { key: "ext.spatial_video.render.title", fallback: "Visão 360" },
    description: {
      key: "ext.spatial_video.render.desc",
      fallback: "Projeta câmeras ao vivo mapeadas sobre a composição.",
    },
    icon: "street-view",
    order: 40,
    render: (ctx) => <SpatialVideoView {...ctx} />,
    renderSettings: ({ settings, updateSettings }) => {
      const current = readSpatialVideoSettings(settings);
      return (
        <div>
          <div className="modalSectionTitle">Projeção do vídeo</div>
          <div className="choiceList">
            {SPATIAL_VIDEO_PROJECTION_OPTIONS.map((option) => {
              const selected = current.projectionStrategyId === option.id;
              return (
                <div
                  key={option.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => updateSettings({ projection_strategy_id: option.id })}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") updateSettings({ projection_strategy_id: option.id });
                  }}
                >
                  <div className="choiceTitle">{option.label}</div>
                  <div className="choiceDesc">{option.description}</div>
                </div>
              );
            })}
          </div>

          <div className="sectionDivider" />
          <div className="modalSectionTitle">Densidade da malha</div>
          <div className="choiceList">
            {SPATIAL_VIDEO_MESH_DENSITY_OPTIONS.map((option) => {
              const selected = current.meshDensity === option.id;
              return (
                <div
                  key={option.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => updateSettings({ mesh_density: option.id })}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") updateSettings({ mesh_density: option.id });
                  }}
                >
                  <div className="choiceTitle">{option.label}</div>
                  <div className="choiceDesc">{option.description}</div>
                </div>
              );
            })}
          </div>
        </div>
      );
    },
  });
}
