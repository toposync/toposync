import React from "react";
import CreatableSelect from "react-select/creatable";
import { type MultiValue } from "react-select";

import { pipelinesReactSelectStyles, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { SelectOption } from "../../types";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function YoloVisionConfigCard({ config, onUpdateConfig }: Props): React.ReactElement {
  const yoloCategoriesRaw = (config as any).categories;
  const yoloCategories = Array.isArray(yoloCategoriesRaw)
    ? yoloCategoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const yoloConfidenceRaw = Number((config as any).confidence_threshold ?? 0.4);
  const yoloConfidence = Number.isFinite(yoloConfidenceRaw) ? Math.max(0, Math.min(1, yoloConfidenceRaw)) : 0.4;

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Min confidence</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0}
          max={1}
          step={0.01}
          value={String(yoloConfidence)}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 0);
            onUpdateConfig((prev) => ({
              ...prev,
              confidence_threshold: Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.4,
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Filters low-confidence detections/tracks (default: 0.40).</div>

      <label className="pipelinesLabel">
        <span>Categories</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={YOLO_CATEGORY_OPTIONS}
          value={yoloCategories.map((value) => YOLO_CATEGORY_OPTIONS.find((opt) => opt.value === value) ?? { value, label: value })}
          placeholder="All categories"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              categories: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Empty selection means “all categories”.</div>
    </div>
  );
}

