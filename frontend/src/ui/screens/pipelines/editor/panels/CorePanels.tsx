import React, { useEffect, useMemo, useState } from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import {
  getHomeAssistantRegistry,
  isAbortError,
  listHomeAssistantServers,
  listHomeAssistantServices,
  type HomeAssistantRegistryResponse,
  type HomeAssistantServerInfo,
  type HomeAssistantServiceInfo,
  type CamerasIndexResponse,
  type PipelineStorageSummary,
  type PipelineOperatorDefinition,
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
  const keyFieldRaw = String((config as any).key_field ?? "payload.subject.id").trim() || "payload.subject.id";

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
            <option value="payload.subject.id">{t("core.ui.pipelines.panels.throttle.key.event_id")}</option>
            <option value="stream_id">{t("core.ui.pipelines.panels.throttle.key.stream_id")}</option>
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

type StationaryEventProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function StationaryEventConfigCard({
  config,
  showAdvanced,
  onUpdateConfig,
}: StationaryEventProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const maxSpeedMpsRaw = Number((config as any).max_speed_mps ?? 1.0 / 3.6);
  const maxSpeedKmh = Number.isFinite(maxSpeedMpsRaw) ? maxSpeedMpsRaw * 3.6 : 1.0;
  const minStationarySeconds = Number((config as any).min_stationary_seconds ?? 1.25);
  const minValidSamples = Number((config as any).min_valid_samples ?? 3);
  const requireArrival = Boolean((config as any).require_arrival ?? false);

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.stationary_event.max_speed")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={4000}
          step={0.05}
          value={Number.isFinite(maxSpeedKmh) ? Math.max(0, maxSpeedKmh) : 1.0}
          onChange={(kmh) => {
            const mps = Number.isFinite(kmh) ? Math.max(0, kmh) / 3.6 : 0;
            onUpdateConfig((prev) => ({ ...prev, max_speed_mps: mps }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.stationary_event.min_stationary_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={3600}
          step={0.25}
          value={Number.isFinite(minStationarySeconds) ? Math.max(0, minStationarySeconds) : 1.25}
          onChange={(seconds) => {
            onUpdateConfig((prev) => ({ ...prev, min_stationary_seconds: Number.isFinite(seconds) ? Math.max(0, seconds) : 0 }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.stationary_event.min_valid_samples")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={1}
          max={10000}
          step={1}
          value={Number.isFinite(minValidSamples) ? Math.max(1, Math.round(minValidSamples)) : 3}
          onChange={(samples) => {
            onUpdateConfig((prev) => ({ ...prev, min_valid_samples: Number.isFinite(samples) ? Math.max(1, Math.round(samples)) : 1 }));
          }}
        />
      </label>

      <label className="pipelinesLabel pipelinesCheckboxLabel">
        <input
          type="checkbox"
          checked={requireArrival}
          onChange={(event) => {
            onUpdateConfig((prev) => ({ ...prev, require_arrival: event.target.checked }));
          }}
        />
        <span>{t("core.ui.pipelines.panels.stationary_event.require_arrival")}</span>
      </label>

      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.stationary_event.hint")}</div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.stationary_event.key_field")}</span>
            <input
              className="pipelinesInput"
              value={textConfigValue((config as any).key_field) || "payload.subject.id"}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, key_field: event.target.value }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.stationary_event.stopped_field")}</span>
            <input
              className="pipelinesInput"
              value={textConfigValue((config as any).stopped_field) || "payload.velocity.stopped"}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, stopped_field: event.target.value }))}
            />
          </label>
        </>
      ) : null}
    </div>
  );
}

type CinematicDirectorProps = {
  config: Record<string, unknown>;
  camerasIndex: CamerasIndexResponse;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

type CinematicBehavior = "rotation_with_events" | "primary_with_events";
type CinematicCameraMode = "all" | "include" | "exclude";
type CinematicPriorityMinimum = "all" | "medium" | "high" | "custom";

function cinematicBehavior(value: unknown): CinematicBehavior {
  return String(value ?? "").trim() === "primary_with_events" ? "primary_with_events" : "rotation_with_events";
}

function cinematicCameraMode(value: unknown): CinematicCameraMode {
  const raw = String(value ?? "").trim();
  if (raw === "include" || raw === "exclude") return raw;
  return "all";
}

function cleanStringList(value: unknown): string[] {
  const raw = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    const text = String(item ?? "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function cameraOptionsForIds(ids: readonly string[], cameraSelectOptionById: Map<string, SelectOption>): SelectOption[] {
  return ids.map((id) => cameraSelectOptionById.get(id) ?? { value: id, label: id });
}

function isCinematicUsableCamera(camera: CamerasIndexResponse["cameras"][number]): boolean {
  if (!camera || !String(camera.id || "").trim()) return false;
  if (camera.enabled === false) return false;
  const sources = Array.isArray(camera.sources) ? camera.sources : [];
  if (sources.length === 0) return true;
  return sources.some((source) => {
    const kind = String(source.kind || "video").trim().toLowerCase();
    return kind === "video" && source.enabled !== false;
  });
}

function buildCinematicCameraSelectOptions(
  camerasIndex: CamerasIndexResponse,
  fallbackOptions: SelectOption[],
): SelectOption[] {
  const cameras = Array.isArray(camerasIndex.cameras) ? camerasIndex.cameras : [];
  const options = cameras
    .filter(isCinematicUsableCamera)
    .map((camera) => {
      const name = String(camera.name || "").trim();
      const id = String(camera.id || "").trim();
      return { value: id, label: name && name !== id ? `${name} (${id})` : id };
    })
    .filter((option) => option.value.length > 0)
    .sort((a, b) => a.label.localeCompare(b.label));
  return cameras.length > 0 ? options : fallbackOptions;
}

function priorityMinimumFromFilter(value: unknown): CinematicPriorityMinimum {
  const priorities = cleanStringList(value).map((item) => item.toLowerCase());
  if (priorities.length === 0) return "all";
  const key = [...new Set(priorities)].sort().join(",");
  if (key === "high") return "high";
  if (key === "high,medium") return "medium";
  return "custom";
}

function priorityFilterForMinimum(value: CinematicPriorityMinimum): string[] {
  if (value === "high") return ["high"];
  if (value === "medium") return ["high", "medium"];
  return [];
}

function normalizeCinematicCameraIds(
  ids: readonly string[],
  options: {
    behavior: CinematicBehavior;
    cameraMode: CinematicCameraMode;
    primaryCameraId: string;
  },
): string[] {
  const { behavior, cameraMode, primaryCameraId } = options;
  const out: string[] = [];
  const seen = new Set<string>();
  for (const id of ids) {
    const text = String(id ?? "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  if (cameraMode === "all") return [];
  if (behavior === "primary_with_events" && primaryCameraId) {
    if (cameraMode === "include" && !seen.has(primaryCameraId)) out.unshift(primaryCameraId);
    if (cameraMode === "exclude") return out.filter((id) => id !== primaryCameraId);
  }
  return out;
}

export function CinematicDirectorConfigCard({
  config,
  camerasIndex,
  cameraSelectOptions,
  cameraSelectOptionById,
  showAdvanced,
  onUpdateConfig,
}: CinematicDirectorProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const cinematicCameraSelectOptions = useMemo(
    () => buildCinematicCameraSelectOptions(camerasIndex, cameraSelectOptions),
    [camerasIndex, cameraSelectOptions],
  );
  const cinematicCameraSelectOptionById = useMemo(() => {
    const map = new Map<string, SelectOption>();
    for (const option of cinematicCameraSelectOptions) map.set(option.value, option);
    return map;
  }, [cinematicCameraSelectOptions]);
  const behavior = cinematicBehavior((config as any).behavior);
  const cameraMode = cinematicCameraMode((config as any).cameras_mode);
  const primaryCameraId = String((config as any).primary_camera_id ?? "").trim();
  const cameraIds = normalizeCinematicCameraIds(cleanStringList((config as any).camera_ids), {
    behavior,
    cameraMode,
    primaryCameraId,
  });
  const priorityMinimum = priorityMinimumFromFilter((config as any).priority_filter);
  const selectedPrimaryCameraOption = primaryCameraId
    ? cinematicCameraSelectOptionById.get(primaryCameraId) ??
      cameraSelectOptionById.get(primaryCameraId) ?? { value: primaryCameraId, label: primaryCameraId }
    : null;
  const selectedCameraOptions = cameraOptionsForIds(cameraIds, cameraSelectOptionById);
  const cameraCount = cinematicCameraSelectOptions.length;
  const defaultPrimaryCameraId = cinematicCameraSelectOptions[0]?.value ?? "";
  const fpsRaw = Number((config as any).fps ?? 8.0);
  const widthRaw = Number((config as any).width ?? 1280);
  const heightRaw = Number((config as any).height ?? 720);
  const idleDwellRaw = Number((config as any).idle_dwell_seconds ?? 8.0);
  const eventMinRaw = Number((config as any).event_min_seconds ?? 10.0);
  const cutCooldownRaw = Number((config as any).cut_cooldown_seconds ?? 1.5);
  const maxEventHoldRaw = Number((config as any).max_event_hold_seconds ?? 60.0);
  const maxCutsRaw = Number((config as any).max_cuts_per_minute ?? 12);
  const staleFrameRaw = Number((config as any).stale_frame_max_age_seconds ?? 2.0);
  const sourceRoleRaw = String((config as any).preferred_source_role ?? "auto").trim();
  const sourceRole = ["auto", "main", "sub", "zoom"].includes(sourceRoleRaw) ? sourceRoleRaw : "auto";
  const warmupModeRaw = String((config as any).warmup_mode ?? "off").trim();
  const warmupMode = ["off", "next_idle", "event_high", "adaptive"].includes(warmupModeRaw) ? warmupModeRaw : "off";
  const behaviorOptions: SelectOption[] = [
    {
      value: "rotation_with_events",
      label: t("core.ui.pipelines.panels.cinematic_director.behavior.rotation_with_events"),
    },
    {
      value: "primary_with_events",
      label: t("core.ui.pipelines.panels.cinematic_director.behavior.primary_with_events"),
    },
  ];
  const selectedBehaviorOption = behaviorOptions.find((option) => option.value === behavior) ?? behaviorOptions[0] ?? null;
  const cameraModeOptions: SelectOption[] = [
    { value: "all", label: t("core.ui.pipelines.panels.cinematic_director.cameras_scope.all") },
    { value: "include", label: t("core.ui.pipelines.panels.cinematic_director.cameras_scope.include") },
    { value: "exclude", label: t("core.ui.pipelines.panels.cinematic_director.cameras_scope.exclude") },
  ];
  const selectedCameraModeOption = cameraModeOptions.find((option) => option.value === cameraMode) ?? cameraModeOptions[0] ?? null;
  const priorityMinimumOptions: SelectOption[] = [
    { value: "all", label: t("core.ui.pipelines.panels.cinematic_director.priority_minimum.all") },
    { value: "medium", label: t("core.ui.pipelines.panels.cinematic_director.priority_minimum.medium") },
    { value: "high", label: t("core.ui.pipelines.panels.cinematic_director.priority_minimum.high") },
  ];
  const priorityMinimumOptionsWithCustom =
    priorityMinimum === "custom"
      ? [
          ...priorityMinimumOptions,
          {
            value: "custom",
            label: t("core.ui.pipelines.panels.cinematic_director.priority_minimum.custom"),
          },
        ]
      : priorityMinimumOptions;
  const selectedPriorityMinimumOption =
    priorityMinimumOptionsWithCustom.find((option) => option.value === priorityMinimum) ?? priorityMinimumOptions[0] ?? null;

  useEffect(() => {
    if (behavior !== "primary_with_events" || primaryCameraId || !defaultPrimaryCameraId) return;
    onUpdateConfig((prev) => ({
      ...prev,
      primary_camera_id: defaultPrimaryCameraId,
      camera_ids: normalizeCinematicCameraIds(cleanStringList((prev as any).camera_ids), {
        behavior: "primary_with_events",
        cameraMode: cinematicCameraMode((prev as any).cameras_mode),
        primaryCameraId: defaultPrimaryCameraId,
      }),
    }));
  }, [behavior, cameraMode, defaultPrimaryCameraId, onUpdateConfig, primaryCameraId]);

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.cinematic_director.behavior")}</span>
        <Select<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={behaviorOptions}
          value={selectedBehaviorOption}
          onChange={(value: SingleValue<SelectOption>) => {
            const nextBehavior = cinematicBehavior(value?.value);
            onUpdateConfig((prev) => {
              const next = { ...prev, behavior: nextBehavior };
              const nextMode = cinematicCameraMode((prev as any).cameras_mode);
              const nextPrimary =
                nextBehavior === "primary_with_events"
                  ? String((prev as any).primary_camera_id ?? "").trim() || defaultPrimaryCameraId
                  : "";
              (next as any).primary_camera_id = nextPrimary;
              (next as any).camera_ids = normalizeCinematicCameraIds(cleanStringList((prev as any).camera_ids), {
                behavior: nextBehavior,
                cameraMode: nextMode,
                primaryCameraId: nextPrimary,
              });
              return next;
            });
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {behavior === "primary_with_events"
          ? t("core.ui.pipelines.panels.cinematic_director.behavior_hint.primary")
          : t("core.ui.pipelines.panels.cinematic_director.behavior_hint.rotation")}
      </div>

      {behavior === "primary_with_events" ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.primary_camera")}</span>
            <Select<SelectOption, false>
              styles={pipelinesReactSelectStyles}
              options={cinematicCameraSelectOptions}
              value={selectedPrimaryCameraOption}
              isDisabled={cinematicCameraSelectOptions.length === 0}
              placeholder={t("core.ui.pipelines.panels.cinematic_director.primary_camera_placeholder")}
              onChange={(value: SingleValue<SelectOption>) => {
                onUpdateConfig((prev) => {
                  const nextPrimary = value?.value ?? "";
                  const nextMode = cinematicCameraMode((prev as any).cameras_mode);
                  return {
                    ...prev,
                    primary_camera_id: nextPrimary,
                    camera_ids: normalizeCinematicCameraIds(cleanStringList((prev as any).camera_ids), {
                      behavior: "primary_with_events",
                      cameraMode: nextMode,
                      primaryCameraId: nextPrimary,
                    }),
                  };
                });
              }}
            />
          </label>
          {!primaryCameraId ? (
            <div className="pipelinesInlineError">
              {t("core.ui.pipelines.panels.cinematic_director.primary_required")}
            </div>
          ) : null}
        </>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.cinematic_director.cameras_scope")}</span>
        <Select<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={cameraModeOptions}
          value={selectedCameraModeOption}
          onChange={(value: SingleValue<SelectOption>) => {
            const nextMode = cinematicCameraMode(value?.value);
            onUpdateConfig((prev) => ({
              ...prev,
              cameras_mode: nextMode,
              camera_ids: normalizeCinematicCameraIds(cleanStringList((prev as any).camera_ids), {
                behavior,
                cameraMode: nextMode,
                primaryCameraId,
              }),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {cameraMode === "include"
          ? t("core.ui.pipelines.panels.cinematic_director.camera_ids_hint.include")
          : cameraMode === "exclude"
            ? t("core.ui.pipelines.panels.cinematic_director.camera_ids_hint.exclude")
            : t("core.ui.pipelines.panels.cinematic_director.camera_ids_hint.all", { count: cameraCount })}
      </div>

      {cameraMode !== "all" ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.cinematic_director.camera_ids")}</span>
          <Select<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={cinematicCameraSelectOptions}
            value={selectedCameraOptions}
            placeholder={t("core.ui.pipelines.panels.cinematic_director.camera_ids_placeholder")}
            onChange={(value: MultiValue<SelectOption>) => {
              const selected = value.map((item) => item.value);
              onUpdateConfig((prev) => ({
                ...prev,
                camera_ids: normalizeCinematicCameraIds(selected, {
                  behavior,
                  cameraMode,
                  primaryCameraId,
                }),
              }));
            }}
          />
        </label>
      ) : null}
      {cameraMode === "include" && selectedCameraOptions.length === 0 ? (
        <div className="pipelinesInlineError">
          {t("core.ui.pipelines.panels.cinematic_director.camera_selection_required")}
        </div>
      ) : null}

      {cinematicCameraSelectOptions.length === 0 ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.cinematic_director.no_cameras")}</div>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.cinematic_director.priority_minimum")}</span>
        <Select<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={priorityMinimumOptionsWithCustom}
          value={selectedPriorityMinimumOption}
          isOptionDisabled={(option) => option.value === "custom"}
          onChange={(value: SingleValue<SelectOption>) => {
            const nextValue = String(value?.value || "all") as CinematicPriorityMinimum;
            if (nextValue === "custom") return;
            onUpdateConfig((prev) => ({ ...prev, priority_filter: priorityFilterForMinimum(nextValue) }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.cinematic_director.priority_minimum_hint")}</div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.source_role")}</span>
            <select
              className="pipelinesSelect"
              value={sourceRole}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, preferred_source_role: String(event.target.value || "auto") }))}
            >
              <option value="auto">{t("core.ui.pipelines.panels.cinematic_director.source_role.auto")}</option>
              <option value="main">{t("core.ui.pipelines.panels.cinematic_director.source_role.main")}</option>
              <option value="sub">{t("core.ui.pipelines.panels.cinematic_director.source_role.sub")}</option>
              <option value="zoom">{t("core.ui.pipelines.panels.cinematic_director.source_role.zoom")}</option>
            </select>
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.warmup_mode")}</span>
            <select
              className="pipelinesSelect"
              value={warmupMode}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, warmup_mode: String(event.target.value || "off") }))}
            >
              <option value="off">{t("core.ui.pipelines.panels.cinematic_director.warmup_mode.off")}</option>
              <option value="next_idle">{t("core.ui.pipelines.panels.cinematic_director.warmup_mode.next_idle")}</option>
              <option value="event_high">{t("core.ui.pipelines.panels.cinematic_director.warmup_mode.event_high")}</option>
              <option value="adaptive">{t("core.ui.pipelines.panels.cinematic_director.warmup_mode.adaptive")}</option>
            </select>
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.fps")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={1} max={60} step={1} value={Number.isFinite(fpsRaw) ? fpsRaw : 8} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, fps: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.width")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={160} max={7680} step={16} value={Number.isFinite(widthRaw) ? widthRaw : 1280} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, width: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.height")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={90} max={4320} step={16} value={Number.isFinite(heightRaw) ? heightRaw : 720} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, height: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.idle_dwell_seconds")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={2} max={120} step={1} value={Number.isFinite(idleDwellRaw) ? idleDwellRaw : 8} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, idle_dwell_seconds: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.event_min_seconds")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={1} max={300} step={1} value={Number.isFinite(eventMinRaw) ? eventMinRaw : 10} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, event_min_seconds: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.cut_cooldown_seconds")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={0} max={60} step={0.5} value={Number.isFinite(cutCooldownRaw) ? cutCooldownRaw : 1.5} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, cut_cooldown_seconds: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.max_event_hold_seconds")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={5} max={3600} step={5} value={Number.isFinite(maxEventHoldRaw) ? maxEventHoldRaw : 60} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, max_event_hold_seconds: value }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.max_cuts_per_minute")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={1} max={120} step={1} value={Number.isFinite(maxCutsRaw) ? maxCutsRaw : 12} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, max_cuts_per_minute: Math.round(value) }))} />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.cinematic_director.stale_frame_max_age_seconds")}</span>
            <PipelinesNumberInput className="pipelinesInput" min={0.1} max={30} step={0.1} value={Number.isFinite(staleFrameRaw) ? staleFrameRaw : 2} onChange={(value) => onUpdateConfig((prev) => ({ ...prev, stale_frame_max_age_seconds: value }))} />
          </label>
          <label className="pipelinesLabel pipelinesCheckboxRow">
            <input
              type="checkbox"
              checked={Boolean((config as any).ignore_own_pipeline_events ?? true)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, ignore_own_pipeline_events: event.target.checked }))}
            />
            <span>{t("core.ui.pipelines.panels.cinematic_director.ignore_own_pipeline_events")}</span>
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.cinematic_director.advanced_hint")}</div>
        </>
      ) : null}
    </div>
  );
}

export function VelocityThrottleConfigCard({
  config,
  showAdvanced,
  onUpdateConfig,
}: VelocityThrottleProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const movingIntervalSeconds = Number((config as any).moving_interval_seconds ?? 2.0);
  const stoppedIntervalSeconds = Number((config as any).stopped_interval_seconds ?? 300.0);
  const keyFieldRaw = String((config as any).key_field ?? "payload.subject.id").trim() || "payload.subject.id";
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
              <option value="payload.subject.id">{t("core.ui.pipelines.panels.throttle.key.event_id")}</option>
              <option value="stream_id">{t("core.ui.pipelines.panels.throttle.key.stream_id")}</option>
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
  const keyFieldRaw = String((config as any).key_field ?? "payload.subject.id").trim() || "payload.subject.id";

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
            <option value="payload.subject.id">{t("core.ui.pipelines.panels.debounce.key.event_id")}</option>
            <option value="stream_id">{t("core.ui.pipelines.panels.debounce.key.stream_id")}</option>
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
  const sourceId = String((config as any).source_id ?? "").trim();
  if (cameraId) return sourceId ? `${cameraId}:${sourceId}` : cameraId;
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
  const dropDataAfterStore = dropDataRaw === undefined || dropDataRaw === null ? true : Boolean(dropDataRaw);

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
  const transmissionId = String((config as any).transmission_id ?? "").trim();
  const publicationEnabled = Boolean((config as any).publication_enabled ?? !transmissionId);
  const publicationLiveViewLabel = String((config as any).publication_live_view_label ?? "").trim();
  const publicationVariantId = String((config as any).publication_variant_id ?? "").trim();
  const publicationVariantLabel = String(
    (config as any).publication_variant_label ?? (config as any).publication_label ?? "",
  ).trim();
  const publicationRoleRaw = String((config as any).publication_role ?? "custom").trim().toLowerCase();
  const publicationRole = ["main", "sub", "zoom", "custom"].includes(publicationRoleRaw) ? publicationRoleRaw : "custom";
  const publicationQualityProfileId = String((config as any).publication_quality_profile_id ?? "").trim();
  const publicationShowInDashboard = Boolean((config as any).publication_show_in_dashboard ?? true);
  const publicationShowInHomeAssistant = Boolean((config as any).publication_show_in_home_assistant ?? false);
  const resizeModeRaw = String((config as any).resize_mode ?? "contain").trim().toLowerCase();
  const resizeMode = resizeModeRaw === "none" ? "none" : "contain";
  const bypassModeRaw = String((config as any).bypass_mode ?? "auto").trim().toLowerCase();
  const bypassMode = bypassModeRaw === "force_on" || bypassModeRaw === "force_off" ? bypassModeRaw : "auto";
  const writerPriorityRaw = Number((config as any).writer_priority ?? 0);
  const writerPriority = Number.isFinite(writerPriorityRaw) ? writerPriorityRaw : 0;
  const roleFallbackLabel =
    publicationRole === "main"
      ? t("core.ui.pipelines.panels.publish_video.publication_role.main", {}, "Principal")
      : publicationRole === "sub"
        ? t("core.ui.pipelines.panels.publish_video.publication_role.sub", {}, "Baixa resolução")
        : publicationRole === "zoom"
          ? t("core.ui.pipelines.panels.publish_video.publication_role.zoom", {}, "Zoom")
          : t("core.ui.pipelines.panels.publish_video.publication_role.custom", {}, "Personalizada");

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel pipelinesCheckboxRow">
        <input
          type="checkbox"
          checked={publicationEnabled}
          onChange={(event) => {
            const checked = event.target.checked;
            onUpdateConfig((prev) => ({
              ...prev,
              publication_enabled: checked,
              publication_role: String((prev as any).publication_role || "custom").trim() || "custom",
              publication_variant_label: String(
                (prev as any).publication_variant_label || (prev as any).publication_label || "",
              ).trim(),
              publication_label: String((prev as any).publication_variant_label || (prev as any).publication_label || "").trim(),
              transmission_id: checked ? "" : String((prev as any).transmission_id || "").trim(),
            }));
          }}
        />
        <span>{t("core.ui.pipelines.panels.publish_video.publication_enabled", {}, "Publicar este vídeo")}</span>
      </label>

      {publicationEnabled ? (
        <>
          <div className="pipelinesStepHint">
            {t(
              "core.ui.pipelines.panels.publish_video.publication_hint",
              {},
              "O Toposync gera a transmissão técnica, a variante e os outputs necessários quando o fluxo é salvo.",
            )}
          </div>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.publication_live_view_label", {}, "Nome da transmissão")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={publicationLiveViewLabel}
              placeholder={t("core.ui.pipelines.panels.publish_video.publication_live_view_label_placeholder", {}, "Garagem tratada")}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, publication_live_view_label: String(event.target.value || "") }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.publication_variant_label", {}, "Nome da variante")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={publicationVariantLabel}
              placeholder={roleFallbackLabel}
              onChange={(event) => {
                const nextValue = String(event.target.value || "");
                onUpdateConfig((prev) => ({
                  ...prev,
                  publication_variant_label: nextValue,
                  publication_label: nextValue,
                }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.publication_role", {}, "Papel")}</span>
            <select
              className="pipelinesSelect"
              value={publicationRole}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, publication_role: String(event.target.value || "custom") }));
              }}
            >
              <option value="main">{t("core.ui.pipelines.panels.publish_video.publication_role.main", {}, "Principal")}</option>
              <option value="sub">{t("core.ui.pipelines.panels.publish_video.publication_role.sub", {}, "Baixa resolução")}</option>
              <option value="zoom">{t("core.ui.pipelines.panels.publish_video.publication_role.zoom", {}, "Zoom")}</option>
              <option value="custom">{t("core.ui.pipelines.panels.publish_video.publication_role.custom", {}, "Personalizada")}</option>
            </select>
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.publication_variant_id", {}, "Chave da variante")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={publicationVariantId}
              placeholder={t("core.ui.pipelines.panels.publish_video.publication_variant_id_placeholder", {}, "automatico")}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, publication_variant_id: String(event.target.value || "") }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">
            {t(
              "core.ui.pipelines.panels.publish_video.publication_variant_id_hint",
              {},
              "Deixe em branco para gerar uma chave estável pelo papel. Use a mesma transmissão com papéis diferentes para agrupar principal, baixa resolução e zoom.",
            )}
          </div>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.publication_quality", {}, "Perfil de saída")}</span>
            <select
              className="pipelinesSelect"
              value={publicationQualityProfileId}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, publication_quality_profile_id: String(event.target.value || "") }));
              }}
            >
              <option value="">{t("core.ui.pipelines.panels.publish_video.publication_quality.auto", {}, "Automático pelo papel")}</option>
              <option value="quad_grid">{t("core.ui.pipelines.panels.publish_video.publication_quality.quad_grid", {}, "Grade leve")}</option>
              <option value="stable_apple_tv">{t("core.ui.pipelines.panels.publish_video.publication_quality.stable", {}, "Estável")}</option>
              <option value="fullscreen_quality">{t("core.ui.pipelines.panels.publish_video.publication_quality.fullscreen", {}, "Tela cheia")}</option>
              <option value="diagnostic_low">{t("core.ui.pipelines.panels.publish_video.publication_quality.diagnostic", {}, "Diagnóstico")}</option>
            </select>
          </label>
          <label className="pipelinesLabel pipelinesCheckboxRow">
            <input
              type="checkbox"
              checked={publicationShowInDashboard}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, publication_show_in_dashboard: event.target.checked }));
              }}
            />
            <span>{t("core.ui.pipelines.panels.publish_video.publication_dashboard", {}, "Aparecer no dashboard")}</span>
          </label>
          <label className="pipelinesLabel pipelinesCheckboxRow">
            <input
              type="checkbox"
              checked={publicationShowInHomeAssistant}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, publication_show_in_home_assistant: event.target.checked }));
              }}
            />
            <span>{t("core.ui.pipelines.panels.publish_video.publication_home_assistant", {}, "Exportar para Home Assistant")}</span>
          </label>
        </>
      ) : showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.publish_video.transmission", {}, "Transmission técnica")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={transmissionId}
              placeholder={t("core.ui.pipelines.panels.publish_video.transmission_placeholder", {}, "uso avançado")}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, transmission_id: String(event.target.value || "").trim() }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">
            {t(
              "core.ui.pipelines.panels.publish_video.transmission_hint",
              {},
              "Campo técnico para diagnóstico. No fluxo normal, deixe Publicar este vídeo ativo e o reconciliador gerará o ID interno.",
            )}
          </div>
        </>
      ) : null}

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
  const dedupeKeyTemplate = textConfigValue((config as any).dedupe_key_template, "{{subject.id}}");

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.title_template")}</span>
        <input
          className="pipelinesInput"
          type="text"
          value={title}
          placeholder={t("core.ui.pipelines.panels.notify.title_placeholder", { subject_category: "{{subject.category}}" })}
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, title: nextValue }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.notify.template_hint_prefix")} <code>{"{{subject.category}}"}</code>, <code>{"{{area_label}}"}</code>,{" "}
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
          <option value="silent">{t("core.ui.pipelines.panels.notify.priority.silent")}</option>
          <option value="low">{t("core.ui.pipelines.panels.notify.priority.low")}</option>
          <option value="medium">{t("core.ui.pipelines.panels.notify.priority.medium")}</option>
          <option value="high">{t("core.ui.pipelines.panels.notify.priority.high")}</option>
        </select>
      </label>
      {priority === "silent" ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.notify.priority.silent_hint")}</div>
      ) : null}

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
            {t("core.ui.pipelines.panels.notify.dedupe_key_hint_prefix")} <code>{"{{subject.id}}"}</code>, <code>{"{{event_code}}"}</code>,{" "}
            <code>{"{{camera_id}}"}</code>, <code>{"{{subject.category}}"}</code>.
          </div>
        </>
      ) : null}
    </div>
  );
}

const HA_BOOLEAN_DEVICE_CLASSES = ["motion", "occupancy", "presence", "opening", "problem", "tamper", ""] as const;

function slugifyHomeAssistantEntityKey(value: string): string {
  const normalized = String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!normalized) return "";
  return /^[0-9]/.test(normalized) ? `s_${normalized}` : normalized.slice(0, 80);
}

export function HomeAssistantBooleanStateConfigCard({ config, showAdvanced, onUpdateConfig }: NotifyProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const [servers, setServers] = useState<HomeAssistantServerInfo[]>([]);
  const [loadingServers, setLoadingServers] = useState(false);
  const [serversError, setServersError] = useState<string | null>(null);
  const [registry, setRegistry] = useState<HomeAssistantRegistryResponse | null>(null);
  const [loadingRegistry, setLoadingRegistry] = useState(false);
  const [registryError, setRegistryError] = useState<string | null>(null);

  const serverId = String((config as any).server_id ?? "").trim();
  const targetModeRaw = String((config as any).target_mode ?? "managed_state").trim();
  const targetMode = targetModeRaw === "existing_input_boolean" ? "existing_input_boolean" : "managed_state";
  const managedName = textConfigValue((config as any).managed_name);
  const managedEntityKey = String((config as any).managed_entity_key ?? "").trim();
  const managedEntityKeyPreview = managedEntityKey || slugifyHomeAssistantEntityKey(managedName);
  const managedEntityPreview = managedEntityKeyPreview ? `binary_sensor.toposync_${managedEntityKeyPreview}` : "";
  const deviceClassRaw = String((config as any).device_class ?? "motion").trim();
  const deviceClass = HA_BOOLEAN_DEVICE_CLASSES.includes(deviceClassRaw as any) ? deviceClassRaw : "motion";
  const existingEntityId = String((config as any).existing_entity_id ?? "").trim();
  const booleanPath = textConfigValue((config as any).boolean_path);
  const shutdownBehaviorRaw = String((config as any).shutdown_behavior ?? "off").trim();
  const shutdownBehavior = ["off", "unavailable", "keep"].includes(shutdownBehaviorRaw) ? shutdownBehaviorRaw : "off";

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setLoadingServers(true);
    setServersError(null);

    void listHomeAssistantServers({ signal: controller.signal })
      .then((payload) => {
        if (cancelled || controller.signal.aborted) return;
        setServers(Array.isArray(payload) ? payload : []);
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setServersError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled || controller.signal.aborted) return;
        setLoadingServers(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    if (!serverId || targetMode !== "existing_input_boolean") {
      setRegistry(null);
      setRegistryError(null);
      setLoadingRegistry(false);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    setLoadingRegistry(true);
    setRegistryError(null);

    void getHomeAssistantRegistry(serverId, { signal: controller.signal })
      .then((payload) => {
        if (cancelled || controller.signal.aborted) return;
        setRegistry(payload);
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setRegistryError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled || controller.signal.aborted) return;
        setLoadingRegistry(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [serverId, targetMode]);

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

  const inputBooleanOptions = useMemo(
    () =>
      (registry?.entities ?? [])
        .map((entity) => {
          const entityId = String(entity.entity_id || "").trim();
          const domain = String(entity.domain || entityId.split(".", 1)[0] || "").trim();
          if (!entityId || domain !== "input_boolean") return null;
          const name = String(entity.name || "").trim();
          return { value: entityId, label: name && name !== entityId ? `${name} (${entityId})` : entityId };
        })
        .filter((option): option is SelectOption => Boolean(option))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [registry],
  );

  const selectedInputBoolean = existingEntityId
    ? inputBooleanOptions.find((option) => option.value === existingEntityId) ?? { value: existingEntityId, label: existingEntityId }
    : null;

  const deviceClassOptions = useMemo(
    () =>
      HA_BOOLEAN_DEVICE_CLASSES.map((value) => ({
        value,
        label: t(`core.ui.pipelines.panels.home_assistant_boolean_state.device_class.${value || "none"}`),
      })),
    [t],
  );

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
    if (targetMode !== "managed_state" || managedEntityKey || !managedName.trim()) return;
    const nextKey = slugifyHomeAssistantEntityKey(managedName);
    if (!nextKey) return;
    onUpdateConfig((prev) => {
      const currentKey = String((prev as any).managed_entity_key ?? "").trim();
      if (currentKey) return prev;
      return { ...prev, managed_entity_key: nextKey };
    });
  }, [managedEntityKey, managedName, onUpdateConfig, targetMode]);

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.server")}</span>
        <select
          className="pipelinesSelect"
          value={serverId}
          onChange={(event) => {
            const nextServerId = String(event.target.value || "").trim();
            onUpdateConfig((prev) => ({
              ...prev,
              server_id: nextServerId,
              existing_entity_id: nextServerId === serverId ? String((prev as any).existing_entity_id ?? "") : "",
            }));
          }}
        >
          <option value="">{t("core.ui.pipelines.panels.home_assistant_boolean_state.server_placeholder")}</option>
          {serverOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {loadingServers ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_boolean_state.server_loading")}</div>
      ) : serversError ? (
        <div className="pipelinesInlineError">
          {t("core.ui.pipelines.panels.home_assistant_boolean_state.server_load_failed", { error: serversError })}
        </div>
      ) : servers.length === 0 ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_boolean_state.server_empty")}</div>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.target_mode")}</span>
        <select
          className="pipelinesSelect"
          value={targetMode}
          onChange={(event) => {
            const nextMode = String(event.target.value || "managed_state").trim();
            onUpdateConfig((prev) => ({
              ...prev,
              target_mode: nextMode === "existing_input_boolean" ? "existing_input_boolean" : "managed_state",
            }));
          }}
        >
          <option value="managed_state">{t("core.ui.pipelines.panels.home_assistant_boolean_state.target_mode.managed_state")}</option>
          <option value="existing_input_boolean">
            {t("core.ui.pipelines.panels.home_assistant_boolean_state.target_mode.existing_input_boolean")}
          </option>
        </select>
      </label>

      {targetMode === "managed_state" ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.managed_name")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={managedName}
              placeholder={t("core.ui.pipelines.panels.home_assistant_boolean_state.managed_name_placeholder")}
              onChange={(event) => {
                const nextName = String(event.target.value ?? "");
                onUpdateConfig((prev) => {
                  const currentKey = String((prev as any).managed_entity_key ?? "").trim();
                  const next = { ...prev, managed_name: nextName };
                  if (!currentKey) {
                    const nextKey = slugifyHomeAssistantEntityKey(nextName);
                    if (nextKey) (next as any).managed_entity_key = nextKey;
                  }
                  return next;
                });
              }}
            />
          </label>
          {managedEntityPreview ? (
            <div className="pipelinesStepHint">
              {t("core.ui.pipelines.panels.home_assistant_boolean_state.managed_entity_preview", { entity_id: managedEntityPreview })}
            </div>
          ) : (
            <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.home_assistant_boolean_state.managed_name_required")}</div>
          )}

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.device_class")}</span>
            <select
              className="pipelinesSelect"
              value={deviceClass}
              onChange={(event) => {
                const nextValue = String(event.target.value || "").trim();
                onUpdateConfig((prev) => ({ ...prev, device_class: HA_BOOLEAN_DEVICE_CLASSES.includes(nextValue as any) ? nextValue : "motion" }));
              }}
            >
              {deviceClassOptions.map((option) => (
                <option key={option.value || "none"} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </>
      ) : (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_entity")}</span>
            <Select<SelectOption, false>
              styles={pipelinesReactSelectStyles}
              options={inputBooleanOptions}
              value={selectedInputBoolean}
              isClearable
              isDisabled={!serverId}
              placeholder={t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_entity_placeholder")}
              onChange={(value) => {
                onUpdateConfig((prev) => ({ ...prev, existing_entity_id: String(value?.value || "").trim() }));
              }}
            />
          </label>
          {!serverId ? (
            <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_select_server_first")}</div>
          ) : loadingRegistry ? (
            <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_loading")}</div>
          ) : registryError ? (
            <div className="pipelinesInlineError">
              {t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_load_failed", { error: registryError })}
            </div>
          ) : inputBooleanOptions.length === 0 ? (
            <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_empty")}</div>
          ) : !existingEntityId ? (
            <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.home_assistant_boolean_state.existing_required")}</div>
          ) : null}
        </>
      )}

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.boolean_path")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={booleanPath}
              placeholder={t("core.ui.pipelines.panels.home_assistant_boolean_state.boolean_path_placeholder")}
              onChange={(event) => {
                onUpdateConfig((prev) => ({ ...prev, boolean_path: String(event.target.value ?? "") }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.home_assistant_boolean_state.shutdown_behavior")}</span>
            <select
              className="pipelinesSelect"
              value={shutdownBehavior}
              onChange={(event) => {
                const nextValue = String(event.target.value || "off").trim();
                onUpdateConfig((prev) => ({ ...prev, shutdown_behavior: ["off", "unavailable", "keep"].includes(nextValue) ? nextValue : "off" }));
              }}
            >
              <option value="off">{t("core.ui.pipelines.panels.home_assistant_boolean_state.shutdown_behavior.off")}</option>
              <option value="unavailable">{t("core.ui.pipelines.panels.home_assistant_boolean_state.shutdown_behavior.unavailable")}</option>
              <option value="keep">{t("core.ui.pipelines.panels.home_assistant_boolean_state.shutdown_behavior.keep")}</option>
            </select>
          </label>
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
    const controller = new AbortController();
    setLoadingServers(true);
    setServersError(null);

    void listHomeAssistantServers({ signal: controller.signal })
      .then((payload) => {
        if (cancelled || controller.signal.aborted) return;
        setServers(Array.isArray(payload) ? payload : []);
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setServersError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled || controller.signal.aborted) return;
        setLoadingServers(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
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
    const controller = new AbortController();
    setLoadingNotifyServices(true);
    setNotifyServicesError(null);

    void listHomeAssistantServices(serverId, {
      domain: "notify",
      signal: controller.signal,
    })
      .then((payload) => {
        if (cancelled || controller.signal.aborted) return;
        setNotifyServices(Array.isArray(payload) ? payload : []);
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setNotifyServicesError(String(error instanceof Error ? error.message : error || "unknown error"));
      })
      .finally(() => {
        if (cancelled || controller.signal.aborted) return;
        setLoadingNotifyServices(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
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
        <code>{"{{subject.category}}"}</code>, <code>{"{{area_label}}"}</code>, <code>{"{{payload.some_field}}"}</code>.
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
