import React from "react";

import type { PipelineOperatorDefinition } from "../../../../util/api";

import { prettyOperatorLabel } from "../utils";

type Props = {
  presetOperators: PipelineOperatorDefinition[];
  onAddStep: (operatorId: string) => void;
};

export function InteractiveStepsToolbar({
  presetOperators,
  onAddStep,
}: Props): React.ReactElement {
  return (
    <div className="pipelinesInteractiveToolbar">
      <div className="pipelinesInteractiveLabel">Add step</div>
      <div className="pipelinesPresetButtons">
        {presetOperators.map((operator) => (
          <button
            key={operator.id}
            className="pillButton"
            type="button"
            onClick={() => onAddStep(operator.id)}
            title={operator.description || operator.id}
          >
            + {prettyOperatorLabel(operator)}
          </button>
        ))}
      </div>
    </div>
  );
}
