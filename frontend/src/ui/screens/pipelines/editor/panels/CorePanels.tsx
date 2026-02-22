import React from "react";
import Select, { type MultiValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import { buildArtifactSuggestions, buildScheduleWeekdayOptions, pipelinesReactSelectStyles, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { SelectOption } from "../../types";
import { i18n } from "../../../../../util/i18n";
import { PipelinesNumberInput } from "../PipelinesNumberInput";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type ScheduleGateProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ScheduleGateConfigCard({ config, showAdvanced, onUpdateConfig }: ScheduleGateProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const enabled = Boolean((config as any).enabled ?? true);
  const timezone = String((config as any).timezone ?? "").trim();
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
  onUpdateConfig: UpdateConfig;
};

export function FilterConfigCard({ config, onUpdateConfig }: FilterProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const presetId = String((config as any).preset_id ?? "").trim();
  const expression = String((config as any).expression ?? "").trim();
  const invert = Boolean((config as any).invert ?? false);

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
  const artifactSuggestions = buildArtifactSuggestions(t);
  const selectedArtifactOptions = artifactNames.map((value) => artifactSuggestions.find((opt) => opt.value === value) ?? { value, label: value });

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
            <textarea
              className="pipelinesTextArea"
              rows={4}
              value={expression}
              placeholder={'payload.object_category_label == "person" and metadata.motion_gate_open'}
              onChange={(event) => {
                const nextValue = String(event.target.value ?? "");
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
            options={artifactSuggestions}
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
  const intervalSeconds = Number((config as any).interval_seconds ?? 1.0);
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
          value={Number.isFinite(intervalSeconds) ? intervalSeconds : 1.0}
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
  const movingFieldRaw = String((config as any).moving_field ?? "payload.velocity.moving").trim() || "payload.velocity.moving";

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
                const nextField = String(event.target.value || "payload.velocity.moving").trim() || "payload.velocity.moving";
                onUpdateConfig((prev) => ({ ...prev, moving_field: nextField }));
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
  onUpdateConfig: UpdateConfig;
};

export function DebugConfigCard({ config, onUpdateConfig }: DebugProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const enabled = Boolean((config as any).enabled ?? true);
  const saveImages = Boolean((config as any).save_images ?? true);
  const printPayload = Boolean((config as any).print_payload ?? true);
  const printMetadata = Boolean((config as any).print_metadata ?? true);
  const printArtifacts = Boolean((config as any).print_artifacts ?? true);
  const maxImagesPerPacket = Number((config as any).max_images_per_packet ?? 4);
  const outputDir = String((config as any).output_dir ?? "").trim();

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
    </div>
  );
}

type StoreImagesProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function StoreImagesConfigCard({ config, showAdvanced, onUpdateConfig }: StoreImagesProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const formatRaw = String((config as any).format ?? "png").trim().toLowerCase() || "png";
  const format = formatRaw === "jpg" || formatRaw === "jpeg" ? "jpg" : "png";
  const subdir = String((config as any).subdir ?? "pipelines").trim() || "pipelines";
  const jpegQualityRaw = Number((config as any).jpeg_quality ?? 85);
  const jpegQuality = Number.isFinite(jpegQualityRaw) ? Math.max(1, Math.min(100, jpegQualityRaw)) : 85;
  const overwrite = Boolean((config as any).overwrite ?? false);

  const dropDataRaw = (config as any).drop_data_after_store;
  const legacyKeepData = Boolean((config as any).keep_data ?? false);
  const dropDataAfterStore = dropDataRaw === undefined || dropDataRaw === null ? !legacyKeepData : Boolean(dropDataRaw);

  const artifactNamesRaw = (config as any).artifact_names;
  const artifactNames = Array.isArray(artifactNamesRaw)
    ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : [];

  const fallbackRaw = String((config as any).image_with_fallback ?? "best_frame,original,treated,segmented");
  const fallbackKeys = fallbackRaw
    .split(",")
    .map((value) => String(value || "").trim())
    .filter((value) => value.length > 0);
  const artifactSuggestions = buildArtifactSuggestions(t);
  const selectedFallbackOptions = fallbackKeys.map(
    (value) => artifactSuggestions.find((option) => option.value === value) ?? { value, label: value },
  );

  return (
    <div className="pipelinesOperatorConfigCard">
      {artifactNames.length > 0 ? (
        <div className="pipelinesInlineError">
          {t("core.ui.pipelines.panels.store_images.using_explicit_artifact_names")}
          <div style={{ marginTop: 8 }}>
            <button
              className="chipButton"
              type="button"
              onClick={() =>
                onUpdateConfig((prev) => ({
                  ...prev,
                  artifact_names: [],
                  image_with_fallback: String((prev as any).image_with_fallback ?? "best_frame,original,treated,segmented"),
                }))
              }
            >
              {t("core.ui.pipelines.panels.store_images.use_fallback_button")}
            </button>
          </div>
        </div>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.store_images.image_with_fallback")}</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={artifactSuggestions}
          value={selectedFallbackOptions}
          placeholder={t("core.ui.pipelines.panels.store_images.image_with_fallback_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              image_with_fallback: value.map((item) => item.value).join(","),
              // Avoid subtle bugs: explicit artifact_names overrides fallback selection.
              artifact_names: [],
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.store_images.hint")}</div>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.store_images.subdir")}</span>
          <input
            className="pipelinesInput"
            type="text"
            value={subdir}
            placeholder="pipelines"
            onChange={(event) => {
              const nextValue = String(event.target.value ?? "");
              onUpdateConfig((prev) => ({ ...prev, subdir: nextValue }));
            }}
          />
        </label>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.store_images.format")}</span>
        <select
          className="pipelinesSelect"
          value={format}
          onChange={(event) => {
            const nextValue = String(event.target.value || "png").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, format: nextValue === "jpg" ? "jpg" : "png" }));
          }}
        >
          <option value="png">PNG</option>
          <option value="jpg">JPG</option>
        </select>
      </label>

      {format === "jpg" ? (
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

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.store_images.overwrite")}</span>
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(event) => onUpdateConfig((prev) => ({ ...prev, overwrite: event.target.checked }))}
          />
        </label>
      ) : null}
    </div>
  );
}

type NotifyProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function NotifyConfigCard({ config, showAdvanced, onUpdateConfig }: NotifyProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const title = String((config as any).title ?? "").trim();
  const description = String((config as any).description ?? "").trim();
  const priority = String((config as any).priority ?? "medium").trim().toLowerCase() || "medium";
  const realtime = Boolean((config as any).realtime ?? true);
  const updateIntervalSecondsRaw = Number((config as any).update_interval_seconds ?? 1.0);
  const updateIntervalSeconds = Number.isFinite(updateIntervalSecondsRaw) ? Math.max(0, Math.min(60, updateIntervalSecondsRaw)) : 1.0;
  const notificationType = String((config as any).notification_type ?? "pipelines.event").trim() || "pipelines.event";
  const dedupeKeyTemplate = String((config as any).dedupe_key_template ?? "").trim();

  const notifyFallbackRaw = (config as any).thumbnail_with_fallback;
  const notifyFallback = Array.isArray(notifyFallbackRaw)
    ? notifyFallbackRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : ["best_frame", "original", "treated", "segmented"];
  const notifySelectedFallbackOptions = notifyFallback.map((value: string) => ({ value, label: value }));
  const artifactSuggestions = buildArtifactSuggestions(t);

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

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.notify.thumbnail_fallback")}</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={artifactSuggestions}
          value={notifySelectedFallbackOptions}
          placeholder={t("core.ui.pipelines.panels.notify.thumbnail_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              thumbnail_with_fallback: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.notify.thumbnail_hint")}
      </div>

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
