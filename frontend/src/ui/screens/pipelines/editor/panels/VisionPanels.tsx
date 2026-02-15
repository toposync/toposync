import React from "react";
import CreatableSelect from "react-select/creatable";
import { type MultiValue } from "react-select";

import { pipelinesReactSelectStyles, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { SelectOption } from "../../types";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type Props = {
  operatorId: string;
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function YoloVisionConfigCard({ operatorId, config, onUpdateConfig }: Props): React.ReactElement {
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

      <label className="pipelinesLabel">
        <span>{isTracking ? "Update interval (seconds)" : "Event interval (seconds)"}</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0}
          max={120}
          step={0.05}
          value={String(defaultInterval)}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 0);
            onUpdateConfig((prev) => ({
              ...prev,
              default_interval_seconds: Number.isFinite(nextValue) ? Math.max(0, Math.min(120, nextValue)) : 0.2,
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        Min seconds between emits per camera + category. Use 0 only if you really want “every frame” (can overload notify/storage/debug).
      </div>

      {isTracking ? (
        <>
          <label className="pipelinesLabel">
            <span>Close after (seconds)</span>
            <input
              className="pipelinesInput"
              type="number"
              min={0.05}
              max={300}
              step={0.1}
              value={String(closeAfter)}
              onChange={(event) => {
                const nextValue = Number(event.target.value || 0);
                onUpdateConfig((prev) => ({
                  ...prev,
                  close_after_seconds: Number.isFinite(nextValue) ? Math.max(0.05, Math.min(300, nextValue)) : 4.0,
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">Closes a track if the object is not seen for this long (higher = more stable, slower close).</div>
        </>
      ) : null}
    </div>
  );
}
