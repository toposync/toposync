import React from "react";
import type { CompositionElement, ToposyncHost } from "@toposync/plugin-api";

import { SpatialVideo3DView } from "./SpatialVideo3DView";
import { SpatialVideoView } from "./SpatialVideoView";
import {
  readSpatialVideoSettings,
  SPATIAL_VIDEO_3D_RENDER_VIEW_ID,
  SPATIAL_VIDEO_MESH_DENSITY_OPTIONS,
  SPATIAL_VIDEO_PROJECTION_OPTIONS,
  SPATIAL_VIDEO_RENDER_VIEW_ID,
} from "./spatialSettings";
import { spatialVideoTranslations } from "./translations";

function projectionAvailability({ elements }: { elements: CompositionElement[] }) {
  const configured = elements.filter(element => element.props.panorama_mapping || (Array.isArray(element.props.calibrated_views) && element.props.calibrated_views.length > 0));
  return { available: configured.length > 0, reason: { key: configured.length ? "ext.spatial_video.availability_configured" : "ext.spatial_video.availability_missing", params: { count: configured.length } },
    elementId: elements.find(element => typeof element.props.camera_id === "string")?.id, initialAction: "panorama-mapping" };
}

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(spatialVideoTranslations);
  host.registerRenderView({
    id: SPATIAL_VIDEO_RENDER_VIEW_ID,
    group: "panorama",
    availability: projectionAvailability,
    name: { key: "ext.spatial_video.render.title", fallback: "Visão 360 2D" },
    description: {
      key: "ext.spatial_video.render.desc",
      fallback: "Projeta câmeras mapeadas no ambiente em uma vista plana.",
    },
    icon: "street-view",
    order: 40,
    render: (ctx) => <SpatialVideoView {...ctx} i18n={host.i18n} />,
    renderSettings: ({ settings, updateSettings }) => {
      const current = readSpatialVideoSettings(settings);
      return (
        <div>
          <p>{host.i18n.t("ext.spatial_video.settings_shared")}</p>
          <div className="modalSectionTitle">{host.i18n.t("ext.spatial_video.projection")}</div>
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
                  <div className="choiceTitle">{host.i18n.t(`ext.spatial_video.option_${option.id}`)}</div>
                  <div className="choiceDesc">{host.i18n.t(`ext.spatial_video.option_${option.id}_help`)}</div>
                </div>
              );
            })}
          </div>

          <div className="sectionDivider" />
          <div className="modalSectionTitle">{host.i18n.t("ext.spatial_video.detail")}</div>
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
                  <div className="choiceTitle">{host.i18n.t(`ext.spatial_video.option_${option.id}`)}</div>
                  <div className="choiceDesc">{host.i18n.t(`ext.spatial_video.option_${option.id}_help`)}</div>
                </div>
              );
            })}
          </div>
        </div>
      );
    },
  });

  host.registerRenderView({
    id: SPATIAL_VIDEO_3D_RENDER_VIEW_ID,
    group: "panorama",
    availability: projectionAvailability,
    name: { key: "ext.spatial_video.render_3d.title", fallback: "Visão 360 3D" },
    description: {
      key: "ext.spatial_video.render_3d.desc",
      fallback: "Projeta câmeras mapeadas dentro da cena 3D.",
    },
    icon: "cube",
    order: 41,
    render: (ctx) => <SpatialVideo3DView {...ctx} i18n={host.i18n} />,
  });
}
