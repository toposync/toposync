import React from "react";

import type { PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";

import { PIPELINE_OPERATOR_GROUPS } from "../constants";
import { prettyOperatorDescription, prettyOperatorLabel, resolvePipelineOperatorUx } from "../utils";

type Props = {
  presetOperators: PipelineOperatorDefinition[];
  onAddStep: (operatorId: string) => void;
};

export function InteractiveStepsToolbar({
  presetOperators,
  onAddStep,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [showAdvanced, setShowAdvanced] = React.useState(false);
  const hasAdvanced = React.useMemo(
    () => presetOperators.some((operator) => resolvePipelineOperatorUx(operator).level === "advanced"),
    [presetOperators],
  );
  const visibleOperators = React.useMemo(
    () => presetOperators.filter((operator) => showAdvanced || resolvePipelineOperatorUx(operator).level !== "advanced"),
    [presetOperators, showAdvanced],
  );

  return (
    <div className="pipelinesInteractiveToolbar">
      <div className="pipelinesInteractiveToolbarHeader">
        <div className="pipelinesInteractiveLabel">{t("core.ui.pipelines.editor.add_step")}</div>
        {hasAdvanced ? (
          <button
            className="pillButton pipelinesAdvancedToggle"
            type="button"
            onClick={() => setShowAdvanced((value) => !value)}
          >
            {showAdvanced
              ? t("core.ui.pipelines.editor.hide_advanced_steps")
              : t("core.ui.pipelines.editor.show_advanced_steps")}
          </button>
        ) : null}
      </div>
      <div className="pipelinesOperatorButtons">
        {visibleOperators.map((operator) => {
          const ux = resolvePipelineOperatorUx(operator);
          const group = PIPELINE_OPERATOR_GROUPS[ux.group];
          const groupLabel = t(group.labelKey);
          const description = prettyOperatorDescription(operator);
          return (
            <button
              key={operator.id}
              className="pillButton pipelinesOperatorButton"
              type="button"
              onClick={() => onAddStep(operator.id)}
              title={`${t("core.ui.pipelines.editor.operator_group_tooltip", { group: groupLabel })}\n${description}`}
              style={{ "--operator-group-color": group.color } as React.CSSProperties}
            >
              <span className="pipelinesOperatorButtonAccent" aria-hidden="true" />
              <span>+ {prettyOperatorLabel(operator)}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
