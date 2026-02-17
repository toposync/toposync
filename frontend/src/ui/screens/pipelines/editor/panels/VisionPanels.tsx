import React from "react";
import CreatableSelect from "react-select/creatable";
import { type MultiValue } from "react-select";

import { pipelinesReactSelectStyles, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { SelectOption } from "../../types";
import { i18n } from "../../../../../util/i18n";
import { PipelinesNumberInput } from "../PipelinesNumberInput";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  operatorId: string;
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function YoloVisionConfigCard({ operatorId, config, onUpdateConfig }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const yoloCategoriesRaw = (config as any).categories;
  const yoloCategories = Array.isArray(yoloCategoriesRaw)
    ? yoloCategoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const yoloConfidenceRaw = Number((config as any).confidence_threshold ?? 0.4);
  const yoloConfidence = Number.isFinite(yoloConfidenceRaw) ? Math.max(0, Math.min(1, yoloConfidenceRaw)) : 0.4;

  const defaultIntervalRaw = Number((config as any).default_interval_seconds ?? 0.2);
  const defaultInterval = Number.isFinite(defaultIntervalRaw) ? Math.max(0, Math.min(120, defaultIntervalRaw)) : 0.2;
  const isTracking = String(operatorId || "").trim() === "vision.object_tracking_yolo";
  const closeAfterRaw = Number((config as any).close_after_seconds ?? 4.0);
  const closeAfter = Number.isFinite(closeAfterRaw) ? Math.max(0.05, Math.min(300, closeAfterRaw)) : 4.0;

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.yolo.min_confidence")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={1}
          step={0.01}
          value={yoloConfidence}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              confidence_threshold: Math.max(0, Math.min(1, nextValue)),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.min_confidence_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.yolo.categories")}</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={YOLO_CATEGORY_OPTIONS}
          value={yoloCategories.map((value) => YOLO_CATEGORY_OPTIONS.find((opt) => opt.value === value) ?? { value, label: value })}
          placeholder={t("core.ui.pipelines.panels.yolo.categories_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              categories: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.categories_hint")}</div>

      <label className="pipelinesLabel">
        <span>{isTracking ? t("core.ui.pipelines.panels.yolo.update_interval_tracking") : t("core.ui.pipelines.panels.yolo.update_interval_detection")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={120}
          step={0.05}
          value={defaultInterval}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              default_interval_seconds: Math.max(0, Math.min(120, nextValue)),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.yolo.update_interval_hint")}
      </div>

      {isTracking ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.yolo.close_after_seconds")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.05}
              max={300}
              step={0.1}
              value={closeAfter}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  close_after_seconds: Math.max(0.05, Math.min(300, nextValue)),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.yolo.close_after_hint")}</div>
        </>
      ) : null}
    </div>
  );
}
