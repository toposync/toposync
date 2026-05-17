import React, { useEffect, useMemo, useState } from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import {
  listHomeAssistantServers,
  listHomeAssistantServices,
  listStreamingTransmissions,
  type HomeAssistantServerInfo,
  type HomeAssistantServiceInfo,
  type PipelineStorageSummary,
  type PipelineOperatorDefinition,
  type StreamingTransmission,
} from "../../../../../util/api";
import {
  buildPacketArtifactSuggestions,
  buildScheduleWeekdayOptions,
  pipelinesReactSelectStyles,
  YOLO_CATEGORY_OPTIONS,
} from "../../constants";
import type { InteractiveStep, SelectOption } from "../../types";
import { isRecord, safeJsonParse, textConfigValue } from "../../utils";
import { i18n } from "../../../../../util/i18n";
import { PipelinesNumberInput } from "../PipelinesNumberInput";
import type { PipelineStepSnapshotKey } from "./pipelineStepSnapshots";
import { FilterExpressionEditor } from "./FilterExpressionEditor";
import { buildFilterExpressionUpstreamContext } from "./filterExpressionContext";
import { buildPipelineStepSnapshotUrl } from "./pipelineStepSnapshots";
import {
  bytesToGiBValue,
  findStorageLayerForNode,
  formatStorageBytes,
  formatStorageTime,
  giBToBytes,
  loadCachedPipelineStorage,
} from "../../storageMetrics";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type ScheduleGateProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ScheduleGateConfigCard({ config, showAdvanced, onUpdateConfig }: ScheduleGateProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const enabled = Boolean((config as any).enabled ?? true);
  const timezone = textConfigValue((config as any).timezone);
  const weekdaysRaw = (config as any).weekdays;
  const weekdayValues = Array.isArray(weekdaysRaw)
    ? weekdaysRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const uniqueWeekdayValues = [...new Set(weekdayValues)];
  const weekdayOptions = buildScheduleWeekdayOptions(t);
  const selectedWeekdayOptions = uniqueWeekdayValues.map((value) => weekdayOptions.find((option) => option.value === value) ?? { value, label: value });

  const startTimeRaw = String((config as any).start_time ?? "00:00").trim() || "00:00";
  const endTimeRaw = String((config as any).end_time ?? "00:00").trim() || "00:00";
  const startTimeValue = startTimeRaw.length >= 5 ? startTimeRaw.slice(0, 5) : "00:00";
  const endTimeValue = endTimeRaw.length >= 5 ? endTimeRaw.slice(0, 5) : "00:00";

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.schedule_gate.enabled")}</span>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => {
            onUpdateConfig((prev) => ({ ...prev, enabled: event.target.checked }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.schedule_gate.days")}</span>
        <Select<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={weekdayOptions}
          value={selectedWeekdayOptions}
          placeholder={t("core.ui.pipelines.panels.schedule_gate.days_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              weekdays: value.map((item) => item.value),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.schedule_gate.start_time")}</span>
        <input
          className="pipelinesInput"
          type="time"
          step={60}
          value={startTimeValue}
          onChange={(event) => {
            const nextValue = String(event.target.value || "00:00");
            onUpdateConfig((prev) => ({ ...prev, start_time: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.schedule_gate.end_time")}</span>
        <input
          className="pipelinesInput"
          type="time"
          step={60}
          value={endTimeValue}
          onChange={(event) => {
            const nextValue = String(event.target.value || "00:00");
            onUpdateConfig((prev) => ({ ...prev, end_time: nextValue }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.schedule_gate.hint")}</div>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.schedule_gate.timezone_optional")}</span>
          <input
            className="pipelinesInput"
            type="text"
            value={timezone}
            placeholder={t("core.ui.pipelines.panels.schedule_gate.timezone_placeholder")}
            onChange={(event) => {
              const nextValue = String(event.target.value ?? "");
              onUpdateConfig((prev) => ({ ...prev, timezone: nextValue }));
            }}
          />
        </label>
      ) : null}
    </div>
  );
}

type CategoryGateProps = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function CategoryGateConfigCard({ config, onUpdateConfig }: CategoryGateProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const modeRaw = String((config as any).mode ?? "include").trim().toLowerCase() || "include";
  const mode = modeRaw === "exclude" ? "exclude" : "include";
  const categoriesRaw = (config as any).categories;
  const categories = Array.isArray(categoriesRaw)
    ? categoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const selectedCategoryOptions = categories.map((value) => YOLO_CATEGORY_OPTIONS.find((opt) => opt.value === value) ?? { value, label: value });

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.category_gate.mode")}</span>
        <select
          className="pipelinesSelect"
          value={mode}
          onChange={(event) => {
            const nextMode = String(event.target.value || "include").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, mode: nextMode === "exclude" ? "exclude" : "include" }));
          }}
        >
          <option value="include">{t("core.ui.pipelines.panels.category_gate.mode.include_only")}</option>
          <option value="exclude">{t("core.ui.pipelines.panels.category_gate.mode.exclude")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.category_gate.categories")}</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={YOLO_CATEGORY_OPTIONS}
          value={selectedCategoryOptions}
          placeholder={t("core.ui.pipelines.panels.category_gate.categories_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              categories: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.category_gate.hint")}
      </div>
    </div>
  );
}

type FilterProps = {
  config: Record<string, unknown>;
  steps: InteractiveStep[];
  index: number;
  operatorsById: Record<string, PipelineOperatorDefinition>;
  onUpdateConfig: UpdateConfig;
};

export function FilterConfigCard({ config, steps, index, operatorsById, onUpdateConfig }: FilterProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const presetId = String((config as any).preset_id ?? "").trim();
  const expression = textConfigValue((config as any).expression);
  const invert = Boolean((config as any).invert ?? false);
  const upstreamContext = useMemo(
    () => buildFilterExpressionUpstreamContext(steps, index, operatorsById),
    [index, operatorsById, steps],
  );

  const categoriesRaw = (config as any).categories;
  const categories = Array.isArray(categoriesRaw)
    ? categoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const selectedCategoryOptions = categories.map((value) => YOLO_CATEGORY_OPTIONS.find((opt) => opt.value === value) ?? { value, label: value });

  const lifecyclesRaw = (config as any).lifecycles;
  const lifecycles = Array.isArray(lifecyclesRaw)
    ? lifecyclesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : [];
  const lifecycleOptions: SelectOption[] = [
    { value: "open", label: "open" },
    { value: "update", label: "update" },
    { value: "close", label: "close" },
  ];
  const selectedLifecycleOptions = lifecycles.map((value) => lifecycleOptions.find((opt) => opt.value === value) ?? { value, label: value });

  const artifactNamesRaw = (config as any).artifact_names;
  const artifactNames = Array.isArray(artifactNamesRaw)
    ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : [];
  const packetArtifactSuggestions = useMemo(() => {
    const defaults = buildPacketArtifactSuggestions(t);
    const optionByValue = new Map(defaults.map((option) => [String(option.value || "").trim(), option] as const));
    const orderedValues = [
      ...upstreamContext.artifactNames,
      ...defaults.map((option) => String(option.value || "").trim()),
    ];
    const merged: SelectOption[] = [];
    const seen = new Set<string>();
    for (const rawValue of orderedValues) {
      const value = String(rawValue || "").trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      merged.push(optionByValue.get(value) ?? { value, label: value });
    }
    return merged;
  }, [t, upstreamContext.artifactNames]);
  const selectedArtifactOptions = artifactNames.map(
    (value) => packetArtifactSuggestions.find((opt) => opt.value === value) ?? { value, label: value },
  );

  const presetOptions: Array<{ value: string; label: string; hint: string }> = [
    {
      value: "",
      label: t("core.ui.pipelines.panels.filter.preset.custom.label"),
      hint: t("core.ui.pipelines.panels.filter.preset.custom.hint"),
    },
    {
      value: "object_category_in",
      label: t("core.ui.pipelines.panels.filter.preset.object_category_in.label"),
      hint: t("core.ui.pipelines.panels.filter.preset.object_category_in.hint"),
    },
    {
      value: "object_category_not_in",
      label: t("core.ui.pipelines.panels.filter.preset.object_category_not_in.label"),
      hint: t("core.ui.pipelines.panels.filter.preset.object_category_not_in.hint"),
    },
    {
      value: "lifecycle_is",
      label: t("core.ui.pipelines.panels.filter.preset.lifecycle_is.label"),
      hint: t("core.ui.pipelines.panels.filter.preset.lifecycle_is.hint"),
    },
    {
      value: "has_artifact",
      label: t("core.ui.pipelines.panels.filter.preset.has_artifact.label"),
      hint: t("core.ui.pipelines.panels.filter.preset.has_artifact.hint"),
    },
  ];
  const presetSelected = presetOptions.find((opt) => opt.value === presetId) ?? presetOptions[0];

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.filter.preset")}</span>
        <select
          className="pipelinesSelect"
          value={presetSelected.value}
          onChange={(event) => {
            const nextPreset = String(event.target.value ?? "").trim();
            onUpdateConfig((prev) => ({
              ...prev,
              preset_id: nextPreset,
              // Avoid confusion: presets ignore expression.
              expression: nextPreset ? "" : String((prev as any).expression ?? ""),
            }));
          }}
        >
          {presetOptions.map((item) => (
            <option key={item.value || "expression"} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <div className="pipelinesStepHint">{presetSelected.hint}</div>

      {presetSelected.value === "" ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.filter.expression")}</span>
            <FilterExpressionEditor
              value={expression}
              artifactSuggestions={packetArtifactSuggestions}
              payloadPathSuggestions={upstreamContext.payloadPathSuggestions}
              metadataPathSuggestions={upstreamContext.metadataPathSuggestions}
              onChange={(nextValue) => {
                onUpdateConfig((prev) => ({ ...prev, expression: nextValue }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.filter.expression_hint")}</div>
        </>
      ) : presetSelected.value === "object_category_in" || presetSelected.value === "object_category_not_in" ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.filter.categories")}</span>
          <CreatableSelect<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={YOLO_CATEGORY_OPTIONS}
            value={selectedCategoryOptions}
            placeholder={t("core.ui.pipelines.panels.filter.categories_placeholder")}
            onChange={(value: MultiValue<SelectOption>) => {
              onUpdateConfig((prev) => ({
                ...prev,
                categories: value.map((item) => item.value),
              }));
            }}
          />
        </label>
      ) : presetSelected.value === "lifecycle_is" ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.filter.lifecycles")}</span>
          <Select<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={lifecycleOptions}
            value={selectedLifecycleOptions}
            placeholder={t("core.ui.pipelines.panels.filter.lifecycles_placeholder")}
            onChange={(value: MultiValue<SelectOption>) => {
              onUpdateConfig((prev) => ({
                ...prev,
                lifecycles: value.map((item) => item.value),
              }));
            }}
          />
        </label>
      ) : presetSelected.value === "has_artifact" ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.filter.artifacts")}</span>
          <CreatableSelect<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={packetArtifactSuggestions}
            value={selectedArtifactOptions}
            placeholder={t("core.ui.pipelines.panels.filter.artifacts_placeholder")}
            onChange={(value: MultiValue<SelectOption>) => {
              onUpdateConfig((prev) => ({
                ...prev,
                artifact_names: value.map((item) => item.value),
              }));
            }}
          />
        </label>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.filter.invert")}</span>
        <input type="checkbox" checked={invert} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, invert: event.target.checked }))} />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.filter.hint")}</div>
    </div>
  );
}

type ThrottleProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ThrottleConfigCard({ config, showAdvanced, onUpdateConfig }: ThrottleProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const intervalSeconds = Number((config as any).interval_seconds ?? 15.0);
  const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
  const keyFieldRaw = String((config as any).key_field ?? "payload.event_id").trim() || "payload.event_id";

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.throttle.interval_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0.01}
          max={120}
          step={0.05}
          value={Number.isFinite(intervalSeconds) ? intervalSeconds : 15.0}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              interval_seconds: Math.max(0.01, Math.min(120, nextValue)),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.throttle.mode")}</span>
        <select
          className="pipelinesSelect"
          value={modeRaw}
          onChange={(event) => {
            const nextMode = String(event.target.value || "first").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, mode: nextMode }));
          }}
        >
          <option value="first">{t("core.ui.pipelines.panels.throttle.mode.first")}</option>
        </select>
      </label>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.throttle.key")}</span>
          <select
            className="pipelinesSelect"
            value={keyFieldRaw}
            onChange={(event) => {
              const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
              onUpdateConfig((prev) => ({ ...prev, key_field: nextKey }));
            }}
          >
            <option value="payload.event_id">{t("core.ui.pipelines.panels.throttle.key.event_id")}</option>
            <option value="stream_id">{t("core.ui.pipelines.panels.throttle.key.stream_id")}</option>
            <option value="payload.tracking_id">{t("core.ui.pipelines.panels.throttle.key.tracking_id")}</option>
            <option value="payload.correlation_id">{t("core.ui.pipelines.panels.throttle.key.correlation_id")}</option>
            <option value="payload.camera_id">{t("core.ui.pipelines.panels.throttle.key.camera_id")}</option>
          </select>
        </label>
      ) : null}

      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.throttle.hint")}
      </div>
    </div>
  );
}

type VelocityThrottleProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function VelocityThrottleConfigCard({
  config,
  showAdvanced,
  onUpdateConfig,
}: VelocityThrottleProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const movingIntervalSeconds = Number((config as any).moving_interval_seconds ?? 2.0);
  const stoppedIntervalSeconds = Number((config as any).stopped_interval_seconds ?? 300.0);
  const keyFieldRaw = String((config as any).key_field ?? "payload.event_id").trim() || "payload.event_id";
  const movingFieldRaw = textConfigValue((config as any).moving_field, "payload.velocity.moving");

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.velocity_throttle.moving_interval_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0.01}
          max={3600}
          step={0.1}
          value={Number.isFinite(movingIntervalSeconds) ? movingIntervalSeconds : 2.0}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              moving_interval_seconds: Math.max(0.01, Math.min(3600, nextValue)),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.velocity_throttle.stopped_interval_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0.01}
          max={3600}
          step={1.0}
          value={Number.isFinite(stoppedIntervalSeconds) ? stoppedIntervalSeconds : 300.0}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              stopped_interval_seconds: Math.max(0.01, Math.min(3600, nextValue)),
            }));
          }}
        />
      </label>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.throttle.key")}</span>
            <select
              className="pipelinesSelect"
              value={keyFieldRaw}
              onChange={(event) => {
                const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
                onUpdateConfig((prev) => ({ ...prev, key_field: nextKey }));
              }}
            >
              <option value="payload.event_id">{t("core.ui.pipelines.panels.throttle.key.event_id")}</option>
              <option value="stream_id">{t("core.ui.pipelines.panels.throttle.key.stream_id")}</option>
              <option value="payload.tracking_id">{t("core.ui.pipelines.panels.throttle.key.tracking_id")}</option>
              <option value="payload.correlation_id">{t("core.ui.pipelines.panels.throttle.key.correlation_id")}</option>
              <option value="payload.camera_id">{t("core.ui.pipelines.panels.throttle.key.camera_id")}</option>
            </select>
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.velocity_throttle.moving_field")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={movingFieldRaw}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, moving_field: event.target.value }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.velocity_throttle.default_moving")}</span>
            <input
              type="checkbox"
              checked={Boolean((config as any).default_moving ?? true)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, default_moving: event.target.checked }))}
            />
          </label>
        </>
      ) : null}

      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.velocity_throttle.hint")}</div>
    </div>
  );
}

type DebounceProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function DebounceConfigCard({ config, showAdvanced, onUpdateConfig }: DebounceProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const quietSeconds = Number((config as any).quiet_period_seconds ?? 1.0);
  const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
  const keyFieldRaw = String((config as any).key_field ?? "payload.event_id").trim() || "payload.event_id";

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debounce.quiet_period_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0.01}
          max={120}
          step={0.05}
          value={Number.isFinite(quietSeconds) ? quietSeconds : 1.0}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              quiet_period_seconds: Math.max(0.01, Math.min(120, nextValue)),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debounce.mode")}</span>
        <select
          className="pipelinesSelect"
          value={modeRaw}
          onChange={(event) => {
            const nextMode = String(event.target.value || "first").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, mode: nextMode }));
          }}
        >
          <option value="first">{t("core.ui.pipelines.panels.debounce.mode.first")}</option>
        </select>
      </label>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.debounce.key")}</span>
          <select
            className="pipelinesSelect"
            value={keyFieldRaw}
            onChange={(event) => {
              const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
              onUpdateConfig((prev) => ({ ...prev, key_field: nextKey }));
            }}
          >
            <option value="payload.event_id">{t("core.ui.pipelines.panels.debounce.key.event_id")}</option>
            <option value="stream_id">{t("core.ui.pipelines.panels.debounce.key.stream_id")}</option>
            <option value="payload.tracking_id">{t("core.ui.pipelines.panels.debounce.key.tracking_id")}</option>
            <option value="payload.correlation_id">{t("core.ui.pipelines.panels.debounce.key.correlation_id")}</option>
            <option value="payload.camera_id">{t("core.ui.pipelines.panels.debounce.key.camera_id")}</option>
          </select>
        </label>
      ) : null}

      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.debounce.hint")}
      </div>
    </div>
  );
}

type DebugProps = {
  config: Record<string, unknown>;
  pipelineName: string | null;
  steps: InteractiveStep[];
  index: number;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

function parseInteractiveStepConfig(step: InteractiveStep): Record<string, unknown> {
  const parsed = safeJsonParse(step.configText || "{}");
  if (!parsed.ok) return {};
  if (!isRecord(parsed.data)) return {};
  return parsed.data as Record<string, unknown>;
}

function resolvePipelineStepSourceIdFromCameraSourceConfig(config: Record<string, unknown>): string | null {
  const cameraId = String((config as any).camera_id ?? "").trim();
  if (cameraId) return cameraId;
  const rtspUrl = String((config as any).rtsp_url ?? "").trim();
  if (!rtspUrl) return null;
  return "camera:adhoc";
}

type DebugPreviewEligibility =
  | { enabled: true; key: PipelineStepSnapshotKey }
  | { enabled: false; reason: { code: "no_camera_source" | "no_camera_selected" | "no_pipeline_name" } };

function resolveDebugPreviewEligibility(
  steps: InteractiveStep[],
  currentIndex: number,
  pipelineName: string | null,
  nodeId: string,
): DebugPreviewEligibility {
  let sourceIndex = -1;
  for (let idx = currentIndex - 1; idx >= 0; idx -= 1) {
    if (steps[idx]?.operatorId === "camera.source") {
      sourceIndex = idx;
      break;
    }
  }
  if (sourceIndex < 0) return { enabled: false, reason: { code: "no_camera_source" } };
  const sourceConfig = parseInteractiveStepConfig(steps[sourceIndex]!);
  const sourceId = resolvePipelineStepSourceIdFromCameraSourceConfig(sourceConfig);
  if (!sourceId) return { enabled: false, reason: { code: "no_camera_selected" } };
  const safePipeline = String(pipelineName ?? "").trim();
  if (!safePipeline) return { enabled: false, reason: { code: "no_pipeline_name" } };

  return {
    enabled: true,
    key: {
      pipelineName: safePipeline,
      nodeId: String(nodeId || "").trim() || "node",
      sourceId,
      filename: "input.png",
    },
  };
}

export function DebugConfigCard({ config, pipelineName, steps, index, showAdvanced, onUpdateConfig }: DebugProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const enabled = Boolean((config as any).enabled ?? true);
  const saveImages = Boolean((config as any).save_images ?? true);
  const printPayload = Boolean((config as any).print_payload ?? true);
  const printMetadata = Boolean((config as any).print_metadata ?? true);
  const printArtifacts = Boolean((config as any).print_artifacts ?? true);
  const maxImagesPerPacket = Number((config as any).max_images_per_packet ?? 4);
  const outputDir = textConfigValue((config as any).output_dir);
  const snapshotEnabled = (config as any).snapshot_enabled !== false;
  const snapshotIntervalRaw = Number((config as any).snapshot_interval_seconds ?? 10);
  const snapshotIntervalSeconds = Number.isFinite(snapshotIntervalRaw) ? Math.max(0, Math.min(3600, snapshotIntervalRaw)) : 10;

  const nodeId = String(steps[index]?.nodeId ?? "").trim();
  const previewEligibility = React.useMemo(
    () => resolveDebugPreviewEligibility(steps, index, pipelineName, nodeId),
    [steps, index, pipelineName, nodeId],
  );
  const [previewNonce, setPreviewNonce] = useState(0);
  const [previewDims, setPreviewDims] = useState<{ width: number; height: number } | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  const previewUrl = previewEligibility.enabled ? buildPipelineStepSnapshotUrl(previewEligibility.key, previewNonce) : null;

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.enabled")}</span>
        <input type="checkbox" checked={enabled} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, enabled: event.target.checked }))} />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.debug.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.save_images")}</span>
        <input type="checkbox" checked={saveImages} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, save_images: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.max_images_per_packet")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={64}
          step={1}
          value={Number.isFinite(maxImagesPerPacket) ? maxImagesPerPacket : 4}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              max_images_per_packet: Math.max(0, Math.min(64, nextValue)),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.output_dir")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={outputDir}
          placeholder={t("core.ui.pipelines.panels.debug.output_dir_placeholder")}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, output_dir: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.print_payload")}</span>
        <input type="checkbox" checked={printPayload} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, print_payload: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.print_metadata")}</span>
        <input type="checkbox" checked={printMetadata} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, print_metadata: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.debug.print_artifacts")}</span>
        <input type="checkbox" checked={printArtifacts} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, print_artifacts: event.target.checked }))} />
      </label>

      <div className="sectionDivider" />
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.debug.preview_hint")}</div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.debug.snapshot_enabled")}</span>
            <input
              type="checkbox"
              checked={Boolean(snapshotEnabled)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, snapshot_enabled: event.target.checked }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.debug.snapshot_interval_seconds")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={3600}
              step={0.5}
              value={snapshotIntervalSeconds}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(3600, nextValue)) : 10;
                onUpdateConfig((prev) => ({ ...prev, snapshot_interval_seconds: normalized }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.debug.snapshot_interval_hint")}</div>
        </>
      ) : null}

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between", alignItems: "center" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!previewUrl}
          onClick={() => {
            setPreviewFailed(false);
            setPreviewDims(null);
            setPreviewNonce((prev) => prev + 1);
          }}
        >
          {t("core.ui.pipelines.panels.image_draw.refresh")}
        </button>

        {!previewEligibility.enabled ? (
          <div className="pipelinesStepHint" style={{ textAlign: "right" }}>
            {previewEligibility.reason.code === "no_camera_source"
              ? t("core.ui.pipelines.panels.image_draw.unavailable.no_source")
              : previewEligibility.reason.code === "no_camera_selected"
                ? t("core.ui.pipelines.panels.image_draw.unavailable.no_camera")
                : t("core.ui.pipelines.panels.image_draw.unavailable.no_pipeline")}
          </div>
        ) : null}
      </div>

      <div
        style={{
          marginTop: 10,
          borderRadius: 16,
          border: "1px solid var(--color-border-subtle)",
          background: "rgba(0,0,0,0.22)",
          overflow: "hidden",
          padding: 10,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: 120,
        }}
      >
        {previewUrl && !previewFailed ? (
          <img
            src={previewUrl}
            alt={t("core.ui.pipelines.panels.image_draw.snapshot_alt")}
            style={{
              display: "block",
              maxWidth: "100%",
              maxHeight: 320,
              borderRadius: 14,
              border: "1px solid rgba(255,255,255,0.12)",
              userSelect: "none",
              WebkitUserSelect: "none",
            }}
            onLoad={(event) => {
              const img = event.currentTarget;
              const width = Number(img.naturalWidth || 0);
              const height = Number(img.naturalHeight || 0);
              if (width > 1 && height > 1) setPreviewDims({ width, height });
            }}
            onError={() => setPreviewFailed(true)}
            draggable={false}
          />
        ) : (
          <div className="pipelinesHint">{t("core.ui.pipelines.panels.image_draw.no_snapshot")}</div>
        )}
      </div>

      {previewDims ? (
        <div className="pipelinesHint">
          {t("core.ui.pipelines.panels.image_draw.snapshot_meta", { w: previewDims.width, h: previewDims.height, units: "px" })}
        </div>
      ) : null}
    </div>
  );
}

type StoreImagesProps = {
  config: Record<string, unknown>;
  pipelineName: string | null;
  nodeId: string;
  steps: InteractiveStep[];
  index: number;
  operatorsById: Record<string, PipelineOperatorDefinition>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

function defaultStoreLayerLabel(artifactName: string): string {
  const normalized = String(artifactName || "").trim() || "main";
  const lower = normalized.toLowerCase();
  if (normalized === "main") return "Original";
  if (lower.includes("crop") || lower.includes("recorte")) return "Recorte";
  if (lower.includes("debug")) return "Debug";
  return normalized;
}

export function StoreImagesConfigCard({
  config,
  pipelineName,
  nodeId,
  steps,
  index,
  operatorsById,
  showAdvanced,
  onUpdateConfig,
}: StoreImagesProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const formatRaw = String((config as any).format ?? "webp").trim().toLowerCase() || "webp";
  const format =
    formatRaw === "jpg" || formatRaw === "jpeg" ? "jpg" : formatRaw === "png" ? "png" : "webp";
  const jpegQualityRaw = Number((config as any).jpeg_quality ?? 85);
  const jpegQuality = Number.isFinite(jpegQualityRaw) ? Math.max(1, Math.min(100, jpegQualityRaw)) : 85;
  const artifactName = textConfigValue((config as any).input_artifact_name, "main").trim() || "main";
  const layerLabel = textConfigValue((config as any).layer_label, defaultStoreLayerLabel(artifactName)).trim() || defaultStoreLayerLabel(artifactName);
  const maxLayerBytesRaw = Number((config as any).max_bytes_per_layer ?? 0);
  const maxFilesRaw = Number((config as any).max_files_per_layer ?? 0);
  const maxLayerGiB = bytesToGiBValue(Number.isFinite(maxLayerBytesRaw) ? maxLayerBytesRaw : 0);
  const maxFiles = Number.isFinite(maxFilesRaw) ? Math.max(0, Math.round(maxFilesRaw)) : 0;
  const upstreamContext = useMemo(
    () => buildFilterExpressionUpstreamContext(steps, index, operatorsById),
    [index, operatorsById, steps],
  );
  const artifactOptions = useMemo(() => {
    const defaults = buildPacketArtifactSuggestions(t);
    const byValue = new Map(defaults.map((option) => [String(option.value || "").trim(), option] as const));
    const out: SelectOption[] = [];
    const seen = new Set<string>();
    for (const rawValue of [
      artifactName,
      ...upstreamContext.artifactNames,
      ...defaults.map((option) => option.value),
    ]) {
      const value = String(rawValue || "").trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      out.push(byValue.get(value) ?? { value, label: value });
    }
    return out;
  }, [artifactName, t, upstreamContext.artifactNames]);
  const selectedArtifactOption = artifactOptions.find((option) => option.value === artifactName) ?? {
    value: artifactName,
    label: artifactName,
  };
  const [storage, setStorage] = useState<PipelineStorageSummary | null>(null);
  const [storageLoading, setStorageLoading] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [storageRefreshNonce, setStorageRefreshNonce] = useState(0);

  useEffect(() => {
    if (!pipelineName) {
      setStorage(null);
      setStorageLoading(false);
      setStorageError(null);
      return;
    }
    const controller = new AbortController();
    setStorageLoading(true);
    setStorageError(null);
    void loadCachedPipelineStorage(pipelineName, {
      force: storageRefreshNonce > 0,
      signal: controller.signal,
    })
      .then((summary) => {
        if (controller.signal.aborted) return;
        setStorage(summary);
      })
      .catch((err: any) => {
        if (controller.signal.aborted) return;
        setStorage(null);
        setStorageError(String(err?.message ?? err));
      })
      .finally(() => {
        if (controller.signal.aborted) return;
        setStorageLoading(false);
      });
    return () => controller.abort();
  }, [pipelineName, storageRefreshNonce]);

  const storageLayer = useMemo(
    () => findStorageLayerForNode(storage, nodeId, layerLabel),
    [layerLabel, nodeId, storage],
  );

  const dropDataRaw = (config as any).drop_data_after_store;
  const legacyKeepData = Boolean((config as any).keep_data ?? false);
  const dropDataAfterStore = dropDataRaw === undefined || dropDataRaw === null ? !legacyKeepData : Boolean(dropDataRaw);

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.store_images.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.store_images.input_artifact", {}, "Image")}</span>
        <CreatableSelect<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={artifactOptions}
          value={selectedArtifactOption}
          placeholder={t("core.ui.pipelines.panels.store_images.input_artifact_placeholder", {}, "main")}
          onChange={(value: SingleValue<SelectOption>) => {
            const nextValue = String(value?.value || "main").trim() || "main";
            onUpdateConfig((prev) => ({ ...prev, input_artifact_name: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.store_images.layer_label", {}, "Layer")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={layerLabel}
          placeholder={defaultStoreLayerLabel(artifactName)}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, layer_label: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.store_images.format")}</span>
        <select
          className="pipelinesSelect"
          value={format}
          onChange={(event) => {
            const nextValue = String(event.target.value || "webp").trim().toLowerCase();
            const nextFormat = nextValue === "jpg" ? "jpg" : nextValue === "png" ? "png" : "webp";
            onUpdateConfig((prev) => ({ ...prev, format: nextFormat }));
          }}
        >
          <option value="webp">WebP</option>
          <option value="png">PNG</option>
          <option value="jpg">JPG</option>
        </select>
      </label>

      {format === "jpg" || format === "webp" ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.store_images.jpeg_quality")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={1}
            max={100}
            step={1}
            value={jpegQuality}
            onChange={(nextValue) => {
              const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(100, nextValue)) : 85;
              onUpdateConfig((prev) => ({ ...prev, jpeg_quality: normalized }));
            }}
          />
        </label>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.store_images.drop_data_after_store")}</span>
        <input
          type="checkbox"
          checked={dropDataAfterStore}
          onChange={(event) =>
            onUpdateConfig((prev) => {
              const next = { ...prev };
              (next as any).drop_data_after_store = event.target.checked;
              delete (next as any).keep_data;
              return next;
            })
          }
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.store_images.drop_data_after_store_hint")}</div>

      <div className="pipelinesStorageInlinePanel">
        <div className="pipelinesStorageInlineHeader">
          <span>{t("core.ui.pipelines.panels.store_images.layer_stats", {}, "Layer usage")}</span>
          <button
            className="iconButton"
            type="button"
            onClick={() => setStorageRefreshNonce((prev) => prev + 1)}
            title={t("core.actions.refresh", {}, "Refresh")}
          >
            <i className="fa-solid fa-rotate" aria-hidden="true" />
          </button>
        </div>
        {storageLoading ? (
          <div className="pipelinesStepHint">{t("core.ui.loading")}</div>
        ) : storageError ? (
          <div className="pipelinesStorageNotice isDanger">{storageError}</div>
        ) : storageLayer ? (
          <div className="pipelinesStorageMetricsGrid">
            <div>
              <span>{t("core.ui.pipelines.panels.store_images.used", {}, "Used")}</span>
              <strong>{formatStorageBytes(storageLayer.used_bytes)}</strong>
            </div>
            <div>
              <span>{t("core.ui.pipelines.panels.store_images.files", {}, "Files")}</span>
              <strong>{storageLayer.file_count}</strong>
            </div>
            <div>
              <span>{t("core.ui.pipelines.panels.store_images.avg_file", {}, "Average")}</span>
              <strong>{formatStorageBytes(storageLayer.avg_file_bytes)}</strong>
            </div>
            <div>
              <span>{t("core.ui.pipelines.panels.store_images.last_saved", {}, "Last")}</span>
              <strong>{formatStorageTime(storageLayer.newest_at, locale) || "-"}</strong>
            </div>
            <div>
              <span>{t("core.ui.pipelines.panels.store_images.cleanup_state", {}, "Retention")}</span>
              <strong>
                {storageLayer.over_limit
                  ? t("core.ui.pipelines.storage.over_limit_short", {}, "Over limit")
                  : t("core.ui.pipelines.storage.ok", {}, "OK")}
              </strong>
            </div>
          </div>
        ) : (
          <div className="pipelinesStepHint">{t("core.ui.pipelines.storage.empty", {}, "No stored files yet.")}</div>
        )}
      </div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.store_images.max_bytes_per_layer_gib", {}, "Layer budget (GiB)")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={4096}
              step={0.25}
              value={maxLayerGiB}
              onChange={(nextValue) => {
                const nextBytes = giBToBytes(nextValue);
                onUpdateConfig((prev) => {
                  const next = { ...prev };
                  if (nextBytes) (next as any).max_bytes_per_layer = nextBytes;
                  else delete (next as any).max_bytes_per_layer;
                  return next;
                });
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.store_images.max_files_per_layer", {}, "Layer file limit")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1_000_000}
              step={1}
              value={maxFiles}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.round(nextValue)) : 0;
                onUpdateConfig((prev) => {
                  const next = { ...prev };
                  if (normalized > 0) (next as any).max_files_per_layer = normalized;
                  else delete (next as any).max_files_per_layer;
                  return next;
                });
              }}
            />
          </label>
        </>
      ) : null}
    </div>
  );
}

type NotifyProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

type PublishVideoProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function PublishVideoConfigCard({ config, showAdvanced, onUpdateConfig }: PublishVideoProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const [transmissions, setTransmissions] = useState<StreamingTransmission[]>([]);
  const [loadingTransmissions, setLoadingTransmissions] = useState(false);
  const [transmissionsError, setTransmissionsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadingTransmissions(true);
    setTransmissionsError(null);

    void listStreamingTransmissions()
      .then((payload) => {
        if (cancelled) return;
        setTransmissions(Array.isArray(payload) ? payload : []);
      })
      .catch((error) => {
        if (cancelled) return;
        setTransmissionsError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled) return;
        setLoadingTransmissions(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const transmissionId = String((config as any).transmission_id ?? "").trim();
  const resizeModeRaw = String((config as any).resize_mode ?? "contain").trim().toLowerCase();
  const resizeMode = resizeModeRaw === "none" ? "none" : "contain";
  const bypassModeRaw = String((config as any).bypass_mode ?? "auto").trim().toLowerCase();
  const bypassMode = bypassModeRaw === "force_on" || bypassModeRaw === "force_off" ? bypassModeRaw : "auto";
  const writerPriorityRaw = Number((config as any).writer_priority ?? 0);
  const writerPriority = Number.isFinite(writerPriorityRaw) ? writerPriorityRaw : 0;
  const transmissionOptions = useMemo<SelectOption[]>(() => {
    const disabledSuffix = t("core.ui.pipelines.panels.publish_video.transmission_disabled_suffix", {}, "disabled");
    return transmissions
      .map((item) => {
        const id = String(item?.id || "").trim();
        if (!id) return null;
        const name = String(item?.name || "").trim();
        const path = String(item?.path || "").trim();
        const enabled = item?.enabled !== false;
        const title = name || path || id;
        const details = [path ? `/${path}` : "", enabled ? "" : disabledSuffix].filter(Boolean).join(" • ");
        return {
          value: id,
          label: details ? `${title} (${details})` : title,
        };
      })
      .filter((option): option is SelectOption => Boolean(option))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [t, transmissions]);

  const selectedTransmissionOption = transmissionId
    ? transmissionOptions.find((option) => option.value === transmissionId) ?? { value: transmissionId, label: transmissionId }
    : null;

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.publish_video.transmission")}</span>
        <CreatableSelect<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={transmissionOptions}
          value={selectedTransmissionOption}
          isClearable
          placeholder={t("core.ui.pipelines.panels.publish_video.transmission_placeholder")}
          onChange={(value) => {
            onUpdateConfig((prev) => ({ ...prev, transmission_id: String(value?.value || "").trim() }));
          }}
        />
      </label>
      {loadingTransmissions ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.publish_video.transmission_loading")}</div>
      ) : transmissionsError ? (
        <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.publish_video.transmission_load_failed", { error: transmissionsError })}</div>
      ) : transmissionOptions.length === 0 ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.publish_video.transmission_empty")}</div>
      ) : (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.publish_video.transmission_hint")}</div>
      )}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.publish_video.resize_mode")}</span>
        <select
          className="pipelinesSelect"
          value={resizeMode}
          onChange={(event) => {
            const nextValue = String(event.target.value || "contain").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, resize_mode: nextValue === "none" ? "none" : "contain" }));
          }}
        >
          <option value="contain">{t("core.ui.pipelines.panels.publish_video.resize_mode.contain")}</option>
          <option value="none">{t("core.ui.pipelines.panels.publish_video.resize_mode.none")}</option>
        </select>
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.publish_video.resize_mode_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.publish_video.bypass_mode")}</span>
        <select
          className="pipelinesSelect"
          value={bypassMode}
          onChange={(event) => {
            const nextValue = String(event.target.value || "auto").trim().toLowerCase();
            onUpdateConfig((prev) => ({
              ...prev,
              bypass_mode: nextValue === "force_on" || nextValue === "force_off" ? nextValue : "auto",
            }));
          }}
        >
          <option value="auto">{t("core.ui.pipelines.panels.publish_video.bypass_mode.auto")}</option>
          <option value="force_off">{t("core.ui.pipelines.panels.publish_video.bypass_mode.force_off")}</option>
          {showAdvanced ? <option value="force_on">{t("core.ui.pipelines.panels.publish_video.bypass_mode.force_on")}</option> : null}
        </select>
      </label>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.writer_priority")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={-100}
              max={100}
              step={1}
              value={writerPriority}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(-100, Math.min(100, Math.round(nextValue))) : 0;
                onUpdateConfig((prev) => ({ ...prev, writer_priority: normalized }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.publish_video.writer_priority_hint")}</div>
        </>
      ) : null}
    </div>
  );
}

export function NotifyConfigCard({ config, showAdvanced, onUpdateConfig }: NotifyProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const title = textConfigValue((config as any).title);
  const description = textConfigValue((config as any).description);
  const priority = String((config as any).priority ?? "medium").trim().toLowerCase() || "medium";
  const realtime = Boolean((config as any).realtime ?? true);
  const updateIntervalSecondsRaw = Number((config as any).update_interval_seconds ?? 1.0);
  const updateIntervalSeconds = Number.isFinite(updateIntervalSecondsRaw) ? Math.max(0, Math.min(60, updateIntervalSecondsRaw)) : 1.0;
  const notificationType = textConfigValue((config as any).notification_type, "pipelines.event");
  const dedupeKeyTemplate = textConfigValue((config as any).dedupe_key_template);

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.title_template")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={title}
          placeholder={t("core.ui.pipelines.panels.notify.title_placeholder", { object_category_label: "{{object_category_label}}" })}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, title: nextValue }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.notify.template_hint_prefix")} <code>{"{{object_category_label}}"}</code>, <code>{"{{area_label}}"}</code>,{" "}
        <code>{"{{pose_label}}"}</code>.
      </div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.description_template")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={description}
          placeholder={t("core.ui.pipelines.panels.notify.description_placeholder")}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, description: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.priority")}</span>
        <select
          className="pipelinesSelect"
          value={priority}
          onChange={(event) => {
            const nextPriority = String(event.target.value || "medium").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, priority: nextPriority }));
          }}
        >
          <option value="low">{t("core.ui.pipelines.panels.notify.priority.low")}</option>
          <option value="medium">{t("core.ui.pipelines.panels.notify.priority.medium")}</option>
          <option value="high">{t("core.ui.pipelines.panels.notify.priority.high")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.realtime")}</span>
        <input type="checkbox" checked={realtime} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, realtime: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.update_interval_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={60}
          step={0.1}
          value={Number.isFinite(updateIntervalSeconds) ? updateIntervalSeconds : 1.0}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              update_interval_seconds: Math.max(0, Math.min(60, nextValue)),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.notify.update_interval_hint")}</div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.notify.notification_type")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={notificationType}
              placeholder="pipelines.event"
              onChange={(event) => {
                const nextType = String(event.target.value ?? "");
                onUpdateConfig((prev) => ({ ...prev, notification_type: nextType }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.notify.dedupe_key_template")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={dedupeKeyTemplate}
              placeholder={t("core.ui.pipelines.panels.notify.dedupe_key_placeholder")}
              onChange={(event) => {
                const nextValue = String(event.target.value ?? "");
                onUpdateConfig((prev) => ({ ...prev, dedupe_key_template: nextValue }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">
            {t("core.ui.pipelines.panels.notify.dedupe_key_hint_prefix")} <code>{"{{tracking_id}}"}</code>, <code>{"{{camera_id}}"}</code>,{" "}
            <code>{"{{object_category_label}}"}</code>.
          </div>
        </>
      ) : null}
    </div>
  );
}

export function HomeAssistantNotifyConfigCard({ config, showAdvanced, onUpdateConfig }: NotifyProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const [servers, setServers] = useState<HomeAssistantServerInfo[]>([]);
  const [loadingServers, setLoadingServers] = useState(false);
  const [serversError, setServersError] = useState<string | null>(null);
  const [notifyServices, setNotifyServices] = useState<HomeAssistantServiceInfo[]>([]);
  const [loadingNotifyServices, setLoadingNotifyServices] = useState(false);
  const [notifyServicesError, setNotifyServicesError] = useState<string | null>(null);

  const serverId = String((config as any).server_id ?? "").trim();
  const notifyService = String((config as any).notify_service ?? "").trim();
  const notifyWhenRaw = String((config as any).notify_when ?? "open").trim().toLowerCase();
  const notifyWhen = ["open", "open_update", "close", "all"].includes(notifyWhenRaw) ? notifyWhenRaw : "open";
  const closeBehaviorRaw = String((config as any).close_behavior ?? "ignore").trim().toLowerCase();
  const title = textConfigValue((config as any).title);
  const message = textConfigValue((config as any).message);
  const tagTemplate = textConfigValue((config as any).tag_template);

  useEffect(() => {
    if (notifyWhen !== "open" && notifyWhen !== "open_update") return;
    if (closeBehaviorRaw === "ignore") return;
    onUpdateConfig((prev) => ({ ...prev, close_behavior: "ignore" }));
  }, [closeBehaviorRaw, notifyWhen, onUpdateConfig]);

  useEffect(() => {
    let cancelled = false;
    setLoadingServers(true);
    setServersError(null);

    void listHomeAssistantServers()
      .then((payload) => {
        if (cancelled) return;
        setServers(Array.isArray(payload) ? payload : []);
      })
      .catch((error) => {
        if (cancelled) return;
        setServersError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled) return;
        setLoadingServers(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!serverId) {
      setNotifyServices([]);
      setNotifyServicesError(null);
      setLoadingNotifyServices(false);
      return;
    }

    let cancelled = false;
    setLoadingNotifyServices(true);
    setNotifyServicesError(null);

    void listHomeAssistantServices(serverId, { domain: "notify" })
      .then((payload) => {
        if (cancelled) return;
        setNotifyServices(Array.isArray(payload) ? payload : []);
      })
      .catch((error) => {
        if (cancelled) return;
        setNotifyServicesError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled) return;
        setLoadingNotifyServices(false);
      });

    return () => {
      cancelled = true;
    };
  }, [serverId]);

  useEffect(() => {
    if (serverId || servers.length !== 1) return;
    const onlyServerId = String(servers[0]?.id || "").trim();
    if (!onlyServerId) return;
    onUpdateConfig((prev) => ({ ...prev, server_id: onlyServerId }));
  }, [onUpdateConfig, serverId, servers]);

  const serverOptions = useMemo(
    () =>
      servers
        .map((server) => {
          const id = String(server.id || "").trim();
          if (!id) return null;
          const name = String(server.name || "").trim();
          const host = String(server.host || "").trim();
          return { value: id, label: name ? `${name} (${host || id})` : host || id };
        })
        .filter((option): option is SelectOption => Boolean(option))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [servers],
  );

  const notifyServiceOptions = useMemo(
    () =>
      notifyServices
        .map((item) => {
          const domain = String(item.domain || "").trim();
          const service = String(item.service || "").trim();
          if (!domain || !service) return null;
          const fullService = `${domain}.${service}`;
          const name = String(item.name || "").trim();
          return {
            value: fullService,
            label: name ? `${name} (${fullService})` : fullService,
          };
        })
        .filter((option): option is SelectOption => Boolean(option))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [notifyServices],
  );

  const selectedNotifyServiceOption = notifyService
    ? notifyServiceOptions.find((option) => option.value === notifyService) ?? { value: notifyService, label: notifyService }
    : null;

  useEffect(() => {
    if (serverId || serverOptions.length === 0) return;
    const preferredServer = serverOptions[0] ?? null;
    if (!preferredServer) return;
    onUpdateConfig((prev) => {
      const currentServerId = String((prev as any).server_id ?? "").trim();
      if (currentServerId) return prev;
      return { ...prev, server_id: preferredServer.value };
    });
  }, [onUpdateConfig, serverId, serverOptions]);

  useEffect(() => {
    if (!serverId || notifyService || notifyServiceOptions.length === 0) return;
    const preferredOption =
      notifyServiceOptions.find((option) => option.value.startsWith("notify.mobile_app_")) ?? notifyServiceOptions[0] ?? null;
    if (!preferredOption) return;
    onUpdateConfig((prev) => {
      const currentValue = String((prev as any).notify_service ?? "").trim();
      if (currentValue) return prev;
      return { ...prev, notify_service: preferredOption.value };
    });
  }, [notifyService, notifyServiceOptions, onUpdateConfig, serverId]);

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_notify.server")}</span>
        <select
          className="pipelinesSelect"
          value={serverId}
          onChange={(event) => {
            const nextServerId = String(event.target.value || "").trim();
            onUpdateConfig((prev) => ({
              ...prev,
              server_id: nextServerId,
              notify_service: nextServerId === serverId ? String((prev as any).notify_service ?? "") : "",
            }));
          }}
        >
          <option value="">{t("core.ui.pipelines.panels.home_assistant_notify.server_placeholder")}</option>
          {serverOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {loadingServers ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.server_loading")}</div>
      ) : serversError ? (
        <div className="pipelinesInlineError">
          {t("core.ui.pipelines.panels.home_assistant_notify.server_load_failed", { error: serversError })}
        </div>
      ) : servers.length === 0 ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.server_empty")}</div>
      ) : (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.server_hint")}</div>
      )}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_notify.target")}</span>
        <CreatableSelect<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={notifyServiceOptions}
          value={selectedNotifyServiceOption}
          isClearable
          isDisabled={!serverId}
          placeholder={t("core.ui.pipelines.panels.home_assistant_notify.target_placeholder")}
          onChange={(value) => {
            onUpdateConfig((prev) => ({ ...prev, notify_service: String(value?.value || "").trim() }));
          }}
        />
      </label>
      {!serverId ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.target_select_server_first")}</div>
      ) : loadingNotifyServices ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.target_loading")}</div>
      ) : notifyServicesError ? (
        <div className="pipelinesInlineError">
          {t("core.ui.pipelines.panels.home_assistant_notify.target_load_failed", { error: notifyServicesError })}
        </div>
      ) : notifyServiceOptions.length === 0 ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.target_empty")}</div>
      ) : !notifyService ? (
        <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.home_assistant_notify.target_required")}</div>
      ) : (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.target_hint")}</div>
      )}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_notify.notify_when")}</span>
        <select
          className="pipelinesSelect"
          value={notifyWhen}
          onChange={(event) => {
            const nextValue = String(event.target.value || "open").trim().toLowerCase();
            onUpdateConfig((prev) => ({
              ...prev,
              notify_when: ["open", "open_update", "close", "all"].includes(nextValue) ? nextValue : "open",
            }));
          }}
        >
          <option value="open">{t("core.ui.pipelines.panels.home_assistant_notify.notify_when.open")}</option>
          <option value="open_update">{t("core.ui.pipelines.panels.home_assistant_notify.notify_when.open_update")}</option>
          <option value="close">{t("core.ui.pipelines.panels.home_assistant_notify.notify_when.close")}</option>
          <option value="all">{t("core.ui.pipelines.panels.home_assistant_notify.notify_when.all")}</option>
        </select>
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.notify_when_hint")}</div>

      {notifyWhen === "open" || notifyWhen === "open_update" ? (
        <>
          <div className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_notify.close_behavior")}</span>
          </div>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.close_behavior.fixed_ignore")}</div>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.close_behavior_hint")}</div>
        </>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_notify.title")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={title}
          placeholder={t("core.ui.pipelines.panels.home_assistant_notify.title_placeholder")}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, title: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_notify.message")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={message}
          placeholder={t("core.ui.pipelines.panels.home_assistant_notify.message_placeholder")}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, message: nextValue }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.home_assistant_notify.template_hint_prefix")} <code>{"{{camera_name}}"}</code>,{" "}
        <code>{"{{object_category_label}}"}</code>, <code>{"{{area_label}}"}</code>, <code>{"{{payload.some_field}}"}</code>.
      </div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_notify.tag_template")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={tagTemplate}
              placeholder={t("core.ui.pipelines.panels.home_assistant_notify.tag_template_placeholder")}
              onChange={(event) => {
                const nextValue = String(event.target.value ?? "");
                onUpdateConfig((prev) => ({ ...prev, tag_template: nextValue }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.tag_template_hint")}</div>
          {!tagTemplate ? <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_notify.tag_template_blank_hint")}</div> : null}
        </>
      ) : null}
    </div>
  );
}
