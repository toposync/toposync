import React from "react";

import type { PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";

import { prettyOperatorDescription, prettyOperatorLabel } from "../utils";

type Props = {
  presetOperators: PipelineOperatorDefinition[];
  onAddStep: (operatorId: string) => void;
};

export function InteractiveStepsToolbar({
  presetOperators,
  onAddStep,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  return (
    <div className="pipelinesInteractiveToolbar">
      <div className="pipelinesInteractiveLabel">{t("core.ui.pipelines.editor.add_step")}</div>
      <div className="pipelinesPresetButtons">
        {presetOperators.map((operator) => (
          <button
            key={operator.id}
            className="pillButton"
            type="button"
            onClick={() => onAddStep(operator.id)}
            title={prettyOperatorDescription(operator)}
          >
            + {prettyOperatorLabel(operator)}
          </button>
        ))}
      </div>
    </div>
  );
}
