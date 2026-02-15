import React from "react";

import type { CameraContextsResponse, PipelineOperatorDefinition } from "../../../../util/api";

import type { DragInsertPosition, InteractiveStep, SelectOption } from "../types";

import { InteractiveStepCard } from "./InteractiveStepCard";

type Props = {
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;

  interactiveCameraId: string;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: SelectOption[];

  draggingStepUid: string | null;
  dragOverStep: { uid: string; position: DragInsertPosition } | null;

  onBeginDrag: (event: React.DragEvent, uid: string) => void;
  onEndDrag: () => void;
  onDragOver: (event: React.DragEvent<HTMLElement>, uid: string) => void;
  onDrop: (event: React.DragEvent<HTMLElement>, uid: string) => void;

  onUpdateStep: (uid: string, patch: Partial<InteractiveStep>) => void;
  onRemoveStep: (uid: string) => void;
  onUpdateStepScalar: (uid: string, key: string, value: string | number | boolean) => void;
  onUpdateStepConfig: (uid: string, updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;
};

export function InteractiveStepsList(props: Props): React.ReactElement {
  const { steps } = props;

  return (
    <div className="pipelinesStepsList">
      {steps.map((step, index) => (
        <InteractiveStepCard key={step.uid} step={step} index={index} {...props} />
      ))}

      {steps.length === 0 ? (
        <div className="card">
          <div className="cardBody">No steps yet. Add operators to build the pipeline chain.</div>
        </div>
      ) : null}
    </div>
  );
}

