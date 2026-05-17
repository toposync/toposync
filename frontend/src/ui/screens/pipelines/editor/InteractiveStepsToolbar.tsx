import React from "react";

import { i18n } from "../../../../util/i18n";

import { PIPELINE_OPERATOR_GROUPS } from "../constants";
import type { PipelineCatalogItem } from "../types";
import { prettyOperatorDescription, prettyOperatorLabel, resolvePipelineOperatorUx } from "../utils";

type Props = {
  catalogItems: PipelineCatalogItem[];
  onAddItem: (item: PipelineCatalogItem) => void;
};

export function InteractiveStepsToolbar({
  catalogItems,
  onAddItem,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [showAdvanced, setShowAdvanced] = React.useState(false);
  const hasAdvanced = React.useMemo(
    () => catalogItems.some((item) => resolveCatalogItemUx(item).level === "advanced"),
    [catalogItems],
  );
  const visibleOperators = React.useMemo(
    () => catalogItems.filter((item) => showAdvanced || resolveCatalogItemUx(item).level !== "advanced"),
    [catalogItems, showAdvanced],
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
        {visibleOperators.map((item) => {
          const ux = resolveCatalogItemUx(item);
          const group = PIPELINE_OPERATOR_GROUPS[ux.group];
          const groupLabel = t(group.labelKey);
          const label = catalogItemLabel(item, t);
          const description = catalogItemDescription(item, t);
          return (
            <button
              key={item.id}
              className="pillButton pipelinesOperatorButton"
              type="button"
              onClick={() => onAddItem(item)}
              title={`${t("core.ui.pipelines.editor.operator_group_tooltip", { group: groupLabel })}\n${description}`}
              style={{ "--operator-group-color": group.color } as React.CSSProperties}
            >
              <span className="pipelinesOperatorButtonAccent" aria-hidden="true" />
              <span>+ {label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function resolveCatalogItemUx(item: PipelineCatalogItem): ReturnType<typeof resolvePipelineOperatorUx> {
  if (item.kind === "recipe") {
    return {
      group: item.recipe.group,
      level: item.recipe.level,
      order: item.recipe.order,
      aliases: [],
    };
  }
  return resolvePipelineOperatorUx(item.operator);
}

function catalogItemLabel(item: PipelineCatalogItem, t: ReturnType<typeof i18n.useI18n>["t"]): string {
  if (item.kind === "recipe") return t(item.recipe.labelKey, {}, item.recipe.fallbackLabel);
  return prettyOperatorLabel(item.operator);
}

function catalogItemDescription(item: PipelineCatalogItem, t: ReturnType<typeof i18n.useI18n>["t"]): string {
  if (item.kind === "recipe") return t(item.recipe.descriptionKey, {}, item.recipe.fallbackDescription);
  return prettyOperatorDescription(item.operator);
}
