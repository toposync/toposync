import React from "react";
import type { PipelineOperatorPanel } from "@toposync/plugin-api";

import type { CameraContextsResponse, CamerasIndexResponse, PipelineOperatorDefinition } from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";

import type { CameraAreaOption, InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "../../types";

import {
  CategoryGateConfigCard,
  CinematicDirectorConfigCard,
  DebounceConfigCard,
  DebugConfigCard,
  FilterConfigCard,
  HomeAssistantBooleanStateConfigCard,
  HomeAssistantNotifyConfigCard,
  NotifyConfigCard,
  PublishVideoConfigCard,
  VelocityThrottleConfigCard,
  ScheduleGateConfigCard,
  StationaryEventConfigCard,
  StoreImagesConfigCard,
  ThrottleConfigCard,
} from "./CorePanels";
import {
  AreaRestrictionConfigCard,
  ArtifactPrivacyConfigCard,
  CameraMappingConfigCard,
  CameraSourceConfigCard,
  ImageAdjustConfigCard,
  ImageCropConfigCard,
  ImagePrivacyConfigCard,
  ImagePerspectiveCropConfigCard,
  ImageResizeConfigCard,
  MotionBgSubAdaptiveConfigCard,
  MotionGateConfigCard,
  MotionSampleBgConfigCard,
  ObjectCropConfigCard,
  OnvifEventSourceConfigCard,
  OnvifStateGateConfigCard,
  VelocityEstimationConfigCard,
} from "./CameraPanels";
import { VisionConfigCard, VisionGroupEventsConfigCard } from "./VisionPanels";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  step: InteractiveStep;
  index: number;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  config: Record<string, unknown>;
  pipelineName: string | null;
  processingServerId: string;
  onOpenProcessingServers?: () => void;

  interactiveCameraId: string;
  camerasIndex: CamerasIndexResponse;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: CameraAreaOption[];
  operatorPanels?: Record<string, PipelineOperatorPanel>;

  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
  onInsertStepAfter: (afterUid: string, operatorId: string, defaultsOverride?: Record<string, unknown>) => void;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
};

export function OperatorConfigPanel({
  step,
  index,
  steps,
  operatorsById,
  config,
  pipelineName,
  processingServerId,
  onOpenProcessingServers,
  interactiveCameraId,
  camerasIndex,
  cameraSelectOptions,
  cameraSelectOptionById,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  operatorPanels = {},
  showAdvanced,
  onUpdateConfig,
  onInsertStepAfter,
  onOpenTelemetryField,
}: Props): React.ReactElement | null {
  const operatorId = step.operatorId;
  const extensionPanel = operatorPanels[operatorId];
  if (extensionPanel) {
    return (
      <>
        {extensionPanel.render({
          i18n,
          operatorId,
          stepUid: step.uid,
          nodeId: step.nodeId,
          config,
          showAdvanced,
          updateConfig: (patch) => onUpdateConfig((prev) => ({ ...prev, ...patch })),
          replaceConfig: (next) => onUpdateConfig(() => next),
        })}
      </>
    );
  }

  if (operatorId === "core.schedule_gate") {
    return <ScheduleGateConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.source") {
    return (
      <CameraSourceConfigCard
        config={config}
        camerasIndex={camerasIndex}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.onvif_state_gate") {
    return (
      <OnvifStateGateConfigCard
        config={config}
        camerasIndex={camerasIndex}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.onvif_event_source") {
    return (
      <OnvifEventSourceConfigCard
        config={config}
        camerasIndex={camerasIndex}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (
    operatorId === "vision.classify_image" ||
    operatorId === "vision.track" ||
    operatorId === "vision.detect" ||
    operatorId === "vision.segment_instances"
  ) {
    return (
      <VisionConfigCard
        operatorId={operatorId}
        stepUid={step.uid}
        nodeId={step.nodeId}
        config={config}
        processingServerId={processingServerId}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
        steps={steps}
        index={index}
        operatorsById={operatorsById}
        onInsertStepAfter={onInsertStepAfter}
        onOpenTelemetryField={onOpenTelemetryField}
        onOpenProcessingServers={onOpenProcessingServers}
      />
    );
  }
  if (operatorId === "vision.group_events") {
    return (
      <VisionGroupEventsConfigCard
        config={config}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "core.category_gate") {
    return <CategoryGateConfigCard config={config} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.filter") {
    return <FilterConfigCard config={config} steps={steps} index={index} operatorsById={operatorsById} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.stationary_event") {
    return <StationaryEventConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.camera_mapping") {
    return (
      <CameraMappingConfigCard
        interactiveCameraId={interactiveCameraId}
        activeCameraContexts={activeCameraContexts}
        activeCameraContextsError={activeCameraContextsError}
      />
    );
  }
  if (operatorId === "camera.area_restriction") {
    return (
      <AreaRestrictionConfigCard
        config={config}
        interactiveCameraId={interactiveCameraId}
        activeCameraContexts={activeCameraContexts}
        activeCameraContextsError={activeCameraContextsError}
        cameraAreaOptions={cameraAreaOptions}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.velocity_estimation") {
    return (
      <VelocityEstimationConfigCard
        config={config}
        steps={steps}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.motion_gate") {
    return (
      <MotionGateConfigCard
        config={config}
        stepUid={step.uid}
        nodeId={step.nodeId}
        pipelineName={pipelineName}
        steps={steps}
        operatorsById={operatorsById}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
        onOpenTelemetryField={onOpenTelemetryField}
      />
    );
  }
  if (operatorId === "camera.motion_bgsub_adaptive") {
    return (
      <MotionBgSubAdaptiveConfigCard
        config={config}
        stepUid={step.uid}
        nodeId={step.nodeId}
        pipelineName={pipelineName}
        steps={steps}
        operatorsById={operatorsById}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
        onOpenTelemetryField={onOpenTelemetryField}
      />
    );
  }
  if (operatorId === "camera.motion_sample_bg") {
    return (
      <MotionSampleBgConfigCard
        config={config}
        stepUid={step.uid}
        nodeId={step.nodeId}
        pipelineName={pipelineName}
        steps={steps}
        operatorsById={operatorsById}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
        onOpenTelemetryField={onOpenTelemetryField}
      />
    );
  }
  if (operatorId === "camera.image_crop") {
    return (
      <ImageCropConfigCard
        config={config}
        pipelineName={pipelineName}
        steps={steps}
        operatorsById={operatorsById}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.image_perspective_crop") {
    return (
      <ImagePerspectiveCropConfigCard
        config={config}
        pipelineName={pipelineName}
        steps={steps}
        operatorsById={operatorsById}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.image_adjust") {
    return <ImageAdjustConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.image_privacy") {
    return (
      <ImagePrivacyConfigCard
        config={config}
        pipelineName={pipelineName}
        steps={steps}
        operatorsById={operatorsById}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.artifact_privacy") {
    return (
      <ArtifactPrivacyConfigCard
        config={config}
        steps={steps}
        index={index}
        operatorsById={operatorsById}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "core.throttle") {
    return <ThrottleConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.velocity_throttle") {
    return (
      <VelocityThrottleConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />
    );
  }
  if (operatorId === "core.debounce") {
    return <DebounceConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.image_resize") {
    return <ImageResizeConfigCard config={config} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "vision.crop_objects") {
    return <ObjectCropConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.debug") {
    return (
      <DebugConfigCard
        config={config}
        pipelineName={pipelineName}
        steps={steps}
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "core.store_images") {
    return (
      <StoreImagesConfigCard
        config={config}
        pipelineName={pipelineName}
        nodeId={step.nodeId}
        steps={steps}
        index={index}
        operatorsById={operatorsById}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "core.notify") {
    return <NotifyConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "home_assistant.notify") {
    return <HomeAssistantNotifyConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "home_assistant.boolean_state") {
    return <HomeAssistantBooleanStateConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "stream.publish_video") {
    return <PublishVideoConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "cinematic.director_source") {
    return (
      <CinematicDirectorConfigCard
        config={config}
        camerasIndex={camerasIndex}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }

  return null;
}
