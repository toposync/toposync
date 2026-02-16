import React from "react";

import type { CameraContextsResponse } from "../../../../../util/api";

import type { InteractiveStep, SelectOption } from "../../types";

import {
  CategoryGateConfigCard,
  DebounceConfigCard,
  DebugConfigCard,
  FilterConfigCard,
  NotifyConfigCard,
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
  ImageResizeConfigCard,
  VelocityEstimationConfigCard,
} from "./CameraPanels";
import { YoloVisionConfigCard } from "./VisionPanels";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  step: InteractiveStep;
  index: number;
  steps: InteractiveStep[];
  config: Record<string, unknown>;

  interactiveCameraId: string;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: SelectOption[];

  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function OperatorConfigPanel({
  step,
  index,
  steps,
  config,
  interactiveCameraId,
  cameraSelectOptions,
  cameraSelectOptionById,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  showAdvanced,
  onUpdateConfig,
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
  if (operatorId === "vision.object_tracking_yolo" || operatorId === "vision.object_detection_yolo") {
    return <YoloVisionConfigCard operatorId={operatorId} config={config} onUpdateConfig={onUpdateConfig} />;
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
  if (operatorId === "camera.image_crop") {
    return <ImageCropConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.image_adjust") {
    return <ImageAdjustConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.throttle") {
    return <ThrottleConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.debounce") {
    return <DebounceConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "camera.image_resize") {
    return <ImageResizeConfigCard config={config} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.debug") {
    return <DebugConfigCard config={config} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.store_images") {
    return <StoreImagesConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }
  if (operatorId === "core.notify") {
    return <NotifyConfigCard config={config} showAdvanced={showAdvanced} onUpdateConfig={onUpdateConfig} />;
  }

  return null;
}
