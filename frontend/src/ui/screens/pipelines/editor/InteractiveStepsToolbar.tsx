import React from "react";

import type { PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";

import { PIPELINE_OPERATOR_GROUP_ORDER, PIPELINE_OPERATOR_GROUPS } from "../constants";
import type { PipelineOperatorGroupId } from "../constants";
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
  const groupedOperators = React.useMemo(
    () => {
      const buckets = new Map<PipelineOperatorGroupId, PipelineOperatorDefinition[]>();

      for (const operator of presetOperators) {
        const ux = resolvePipelineOperatorUx(operator);
        if (!showAdvanced && ux.level === "advanced") continue;
        const operators = buckets.get(ux.group) ?? [];
        operators.push(operator);
        buckets.set(ux.group, operators);
      }

      return PIPELINE_OPERATOR_GROUP_ORDER.map((groupId) => ({
        groupId,
        operators: buckets.get(groupId) ?? [],
      })).filter((group) => group.operators.length > 0);
    },
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
      <div className="pipelinesOperatorGroups">
        {groupedOperators.map(({ groupId, operators }) => {
          const group = PIPELINE_OPERATOR_GROUPS[groupId];
          return (
            <section
              key={groupId}
              className="pipelinesOperatorGroup"
              style={{ "--operator-group-color": group.color } as React.CSSProperties}
            >
              <div className="pipelinesOperatorGroupHeader">
                <span className="pipelinesOperatorGroupDot" aria-hidden="true" />
                <div className="pipelinesOperatorGroupTitle">{t(group.labelKey)}</div>
              </div>
              <div className="pipelinesOperatorGroupButtons">
                {operators.map((operator) => (
                  <button
                    key={operator.id}
                    className="pillButton pipelinesOperatorButton"
                    type="button"
                    onClick={() => onAddStep(operator.id)}
                    title={prettyOperatorDescription(operator)}
                  >
                    + {prettyOperatorLabel(operator)}
                  </button>
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
