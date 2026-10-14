import { StationaryObservationSummary, supportsStationaryObservation, stationaryObservationTranslations } from "./notifications/StationaryObservationSummary";
import React from "react";
import { VisualRegionPanel, visualRegionTranslations } from "./pipelines/VisualRegionPanel";
import {
  LivePanoramaSelectorContent,
  LivePanoramaSelectorTrigger,
  LivePanoramaView,
} from "./live/LivePanoramaView";
import type { ToposyncHost } from "@toposync/plugin-api";

import { createCameraElementType } from "./elements/CameraElementType";
import { createCameraRecipeReview } from "./settings/CameraRecipeReview";
import { createCamerasSettingsPanel } from "./settings/CamerasSettingsPanel";
import { AudioFileAnalysis } from "./settings/AudioFileAnalysis";
import { createAddCameraTool } from "./tools/addCameraTool";
import { camerasTranslations } from "./translations";
import { createHumanObservationRenderer } from "./notifications/HumanObservationRenderer";
import { humanObservationTranslations } from "./notifications/humanObservationTranslations";
import { AudioObservationDetails, audioHistoryTranslations, supportsAudioObservation } from "./notifications/AudioObservationDetails";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(visualRegionTranslations);
  host.registerPipelineOperatorPanel({ id: "com.toposync.cameras.panorama-region", operatorId: "camera.panorama_region",
    render: props => <VisualRegionPanel host={host} {...props} /> });
  host.registerPipelineOperatorPanel({ id: "com.toposync.cameras.area-preview", operatorId: "camera.area_restriction", placement: "after",
    render: props => <VisualRegionPanel host={host} {...props} area /> });
  host.registerPipelineOperatorPanel({ id: "com.toposync.cameras.region-occurrence", operatorId: "camera.region_occurrence",
    render: ({ i18n, config, updateConfig }) => <div className="pipelinesOperatorConfigCard">
      <p>{i18n.t("ext.cameras.visual_region.observation_connection")}</p>
      <label className="pipelinesLabel"><span>{i18n.t("ext.cameras.visual_region.name")}</span><input value={String(config.region_name || "")} onChange={event => updateConfig({ region_name: event.target.value })} /></label>
      <label className="pipelinesLabel"><span>{i18n.t("ext.cameras.visual_region.occurrence")}</span><select value={String(config.mode || "presence")} onChange={event => updateConfig({ mode: event.target.value })}>
        <option value="presence">{i18n.t("ext.cameras.visual_region.presence")}</option><option value="entry">{i18n.t("ext.cameras.visual_region.entry")}</option>
      </select></label>
      <label className="pipelinesLabel"><span>{i18n.t("ext.cameras.visual_region.duration")}</span><input type="number" min={0} max={3600} step={.1} value={Number(config.minimum_duration_seconds ?? 1)} onChange={event => updateConfig({ minimum_duration_seconds: Number(event.target.value) })} /></label>
    </div> });
  host.i18n.registerTranslations(camerasTranslations);
  host.i18n.registerTranslations(stationaryObservationTranslations);
  host.registerNotificationDetailPanel?.({ id: "com.toposync.cameras.stationary-summary", supports: supportsStationaryObservation,
    replacesRendererSummary: true, renderSummary: notification => <StationaryObservationSummary notification={notification} i18n={host.i18n} />, render: () => null });
  host.i18n.registerTranslations(humanObservationTranslations);
  host.i18n.registerTranslations(audioHistoryTranslations);
  host.registerNotificationDetailPanel?.({ id: "com.toposync.cameras.audio", supports: supportsAudioObservation,
    render: notification => <AudioObservationDetails notification={notification} i18n={host.i18n} /> });
  host.registerNotificationRenderer(createHumanObservationRenderer(host));
  host.registerRenderView({
    id: "com.toposync.cameras.live-panorama",
    group: "cameras",
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
  host.registerSettingsPanel({
    id: "com.toposync.cameras.audio-tools",
    icon: "wave-square",
    managesOwnPersistence: true,
    name: { key: "ext.cameras.audio.toolsTitle", fallback: "Audio tools" },
    description: { key: "ext.cameras.audio.toolsDescription" },
    render: ({ i18n }) => <div className="settingsPanel"><AudioFileAnalysis i18n={i18n} /></div>,
  });
  host.registerPipelineReviewPanel?.(createCameraRecipeReview(host.i18n));
  host.registerElementType(createCameraElementType(host));
  host.registerEditorTool(createAddCameraTool(host.i18n));
}
