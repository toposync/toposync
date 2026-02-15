import React from "react";

import type { CameraContextsResponse, PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";

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
  stepOutputsByNodeId: Record<string, number> | null;

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
  const { t } = i18n.useI18n();

  return (
    <div className="pipelinesStepsList">
      {steps.map((step, index) => (
        <InteractiveStepCard key={step.uid} step={step} index={index} {...props} />
      ))}

      {steps.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("core.ui.pipelines.editor.no_steps")}</div>
        </div>
      ) : null}
    </div>
  );
}
