import React from "react";

import type { CameraContextsResponse } from "../../../../../util/api";

import type { CameraAreaOption, InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "../../types";

import {
  CategoryGateConfigCard,
  DebounceConfigCard,
  DebugConfigCard,
  FilterConfigCard,
  HomeAssistantNotifyConfigCard,
  NotifyConfigCard,
  PublishVideoConfigCard,
  VelocityThrottleConfigCard,
  ScheduleGateConfigCard,
  StoreImagesConfigCard,
  ThrottleConfigCard,
} from "./CorePanels";
import {
  AreaRestrictionConfigCard,
  CameraMappingConfigCard,
  CameraSourceConfigCard,
  ImageAdjustConfigCard,
  ImageCropConfigCard,
  ImagePerspectiveCropConfigCard,
  ImageResizeConfigCard,
  MotionBgSubAdaptiveConfigCard,
  MotionGateConfigCard,
  MotionSampleBgConfigCard,
  ObjectSegmentationConfigCard,
  VelocityEstimationConfigCard,
} from "./CameraPanels";
import { VisionConfigCard } from "./VisionPanels";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  step: InteractiveStep;
  index: number;
  steps: InteractiveStep[];
  config: Record<string, unknown>;
  pipelineName: string | null;
  processingServerId: string;

  interactiveCameraId: string;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: CameraAreaOption[];

  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
};

export function OperatorConfigPanel({
  step,
  index,
  steps,
  config,
  pipelineName,
  processingServerId,
  interactiveCameraId,
  cameraSelectOptions,
  cameraSelectOptionById,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  showAdvanced,
  onUpdateConfig,
  onOpenTelemetryField,
}: Props): React.ReactElement | null {
  const operatorId = step.operatorId;

  if (operatorId === "core.schedule_gate") {
    return <ScheduleGateConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.source") {
    return (
      <CameraSourceConfigCard
        config={config}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (
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
        onOpenTelemetryField={onOpenTelemetryField}
      />
    );
  }
  if (operatorId === "core.category_gate") {
    return <CategoryGateConfigCard config={config} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.filter") {
    return <FilterConfigCard config={config} onUpdateConfig={onUpdateConfig} />;
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
        index={index}
        showAdvanced={showAdvanced}
        onUpdateConfig={onUpdateConfig}
      />
    );
  }
  if (operatorId === "camera.image_adjust") {
    return <ImageAdjustConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
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
  if (operatorId === "camera.object_crop") {
    return <ObjectSegmentationConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
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
    return <StoreImagesConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.notify") {
    return <NotifyConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "home_assistant.notify") {
    return <HomeAssistantNotifyConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "stream.publish_video") {
    return <PublishVideoConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }

  return null;
}
