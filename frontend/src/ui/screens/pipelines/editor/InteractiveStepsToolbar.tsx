import React from "react";

import type { PipelineOperatorDefinition } from "../../../../util/api";

import { prettyOperatorLabel } from "../utils";

type Props = {
  presetOperators: PipelineOperatorDefinition[];
  operators: PipelineOperatorDefinition[];
  interactiveAddOperatorId: string;
  onChangeInteractiveAddOperatorId: (operatorId: string) => void;
  onAddStep: (operatorId: string) => void;
};

export function InteractiveStepsToolbar({
  presetOperators,
  operators,
  interactiveAddOperatorId,
  onChangeInteractiveAddOperatorId,
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
            + {operator.id.split(".").pop()}
          </button>
        ))}
      </div>
      <div className="pipelinesInlineAddRow">
        <select
          className="pipelinesSelect"
          value={interactiveAddOperatorId}
          onChange={(event) => onChangeInteractiveAddOperatorId(event.target.value)}
        >
          {operators.map((operator) => (
            <option key={operator.id} value={operator.id}>
              {prettyOperatorLabel(operator)}
            </option>
          ))}
        </select>
        <button className="pillButton pillButtonPrimary" type="button" onClick={() => onAddStep(interactiveAddOperatorId)}>
          Add
        </button>
      </div>
    </div>
  );
}

