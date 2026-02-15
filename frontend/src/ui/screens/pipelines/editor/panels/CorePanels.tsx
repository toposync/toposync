import React from "react";
import Select, { type MultiValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import { ARTIFACT_SUGGESTIONS, pipelinesReactSelectStyles, SCHEDULE_WEEKDAY_OPTIONS, YOLO_CATEGORY_OPTIONS } from "../../constants";
import type { SelectOption } from "../../types";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type ScheduleGateProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ScheduleGateConfigCard({ config, showAdvanced, onUpdateConfig }: ScheduleGateProps): React.ReactElement {
  const enabled = Boolean((config as any).enabled ?? true);
  const timezone = String((config as any).timezone ?? "").trim();
  const weekdaysRaw = (config as any).weekdays;
  const weekdayValues = Array.isArray(weekdaysRaw)
    ? weekdaysRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
    : ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const uniqueWeekdayValues = [...new Set(weekdayValues)];
  const selectedWeekdayOptions = uniqueWeekdayValues.map((value) => SCHEDULE_WEEKDAY_OPTIONS.find((option) => option.value === value) ?? { value, label: value });

  const startTimeRaw = String((config as any).start_time ?? "00:00").trim() || "00:00";
  const endTimeRaw = String((config as any).end_time ?? "00:00").trim() || "00:00";
  const startTimeValue = startTimeRaw.length >= 5 ? startTimeRaw.slice(0, 5) : "00:00";
  const endTimeValue = endTimeRaw.length >= 5 ? endTimeRaw.slice(0, 5) : "00:00";

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Enabled</span>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => {
            onUpdateConfig((prev) => ({ ...prev, enabled: event.target.checked }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Days</span>
        <Select<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={SCHEDULE_WEEKDAY_OPTIONS}
          value={selectedWeekdayOptions}
          placeholder="No days (closed)"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              weekdays: value.map((item) => item.value),
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Start time</span>
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
        <span>End time</span>
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
      <div className="pipelinesStepHint">Place this before Camera source to pause RTSP reads while the gate is closed.</div>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>Time zone (optional)</span>
          <input
            className="pipelinesInput"
            type="text"
            value={timezone}
            placeholder="Leave empty for local time"
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
        <span>Mode</span>
        <select
          className="pipelinesSelect"
          value={mode}
          onChange={(event) => {
            const nextMode = String(event.target.value || "include").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, mode: nextMode === "exclude" ? "exclude" : "include" }));
          }}
        >
          <option value="include">Include only</option>
          <option value="exclude">Exclude</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>Categories</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={YOLO_CATEGORY_OPTIONS}
          value={selectedCategoryOptions}
          placeholder="All categories"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              categories: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        Matches <code>payload.object_category_label</code> (set by YOLO operators). Empty selection means “all categories”.
      </div>
    </div>
  );
}

type FilterProps = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function FilterConfigCard({ config, onUpdateConfig }: FilterProps): React.ReactElement {
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
  const selectedArtifactOptions = artifactNames.map((value) => ARTIFACT_SUGGESTIONS.find((opt) => opt.value === value) ?? { value, label: value });

  const presetOptions: Array<{ value: string; label: string; hint: string }> = [
    { value: "", label: "Custom expression", hint: "Write a safe expression referencing payload/metadata." },
    { value: "object_category_in", label: "Object category in list", hint: "Matches payload.object_category_label (YOLO)." },
    { value: "object_category_not_in", label: "Object category not in list", hint: "Excludes payload.object_category_label (YOLO)." },
    { value: "lifecycle_is", label: "Lifecycle is", hint: "Filters by packet lifecycle (open/update/close)." },
    { value: "has_artifact", label: "Has artifact", hint: "Requires at least one artifact name to be present." },
  ];
  const presetSelected = presetOptions.find((opt) => opt.value === presetId) ?? presetOptions[0];

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Preset</span>
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
            <span>Expression</span>
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
          <div className="pipelinesStepHint">
            Available names: <code>payload</code>, <code>metadata</code>, <code>stream_id</code>, <code>lifecycle</code>, <code>artifacts</code>.
            No function calls; only boolean logic, comparisons, and literals.
          </div>
        </>
      ) : presetSelected.value === "object_category_in" || presetSelected.value === "object_category_not_in" ? (
        <label className="pipelinesLabel">
          <span>Categories</span>
          <CreatableSelect<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={YOLO_CATEGORY_OPTIONS}
            value={selectedCategoryOptions}
            placeholder="All categories"
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
          <span>Lifecycles</span>
          <Select<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={lifecycleOptions}
            value={selectedLifecycleOptions}
            placeholder="All lifecycles"
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
          <span>Artifacts</span>
          <CreatableSelect<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={ARTIFACT_SUGGESTIONS}
            value={selectedArtifactOptions}
            placeholder="Select artifacts…"
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
        <span>Invert</span>
        <input type="checkbox" checked={invert} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, invert: event.target.checked }))} />
      </label>
      <div className="pipelinesStepHint">Tip: place Filter before camera.source only when you are filtering gate packets (schedule, HA, etc.).</div>
    </div>
  );
}

type ThrottleProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ThrottleConfigCard({ config, showAdvanced, onUpdateConfig }: ThrottleProps): React.ReactElement {
  const intervalSeconds = Number((config as any).interval_seconds ?? 1.0);
  const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
  const keyFieldRaw = String((config as any).key_field ?? "stream_id").trim() || "stream_id";

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Interval (seconds)</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0.01}
          max={120}
          step={0.05}
          value={Number.isFinite(intervalSeconds) ? String(intervalSeconds) : "1.0"}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 1);
            onUpdateConfig((prev) => ({
              ...prev,
              interval_seconds: Number.isFinite(nextValue) ? nextValue : 1.0,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Mode</span>
        <select
          className="pipelinesSelect"
          value={modeRaw}
          onChange={(event) => {
            const nextMode = String(event.target.value || "first").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, mode: nextMode }));
          }}
        >
          <option value="first">First (recommended)</option>
        </select>
      </label>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>Key</span>
          <select
            className="pipelinesSelect"
            value={keyFieldRaw}
            onChange={(event) => {
              const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
              onUpdateConfig((prev) => ({ ...prev, key_field: nextKey }));
            }}
          >
            <option value="stream_id">Stream (per object/camera)</option>
            <option value="payload.tracking_id">Tracking ID</option>
            <option value="payload.correlation_id">Correlation ID</option>
            <option value="payload.camera_id">Camera ID</option>
          </select>
        </label>
      ) : null}

      <div className="pipelinesStepHint">
        Emits OPEN/CLOSE packets always. Mode “first” emits the first UPDATE in each interval window (keyed).
      </div>
    </div>
  );
}

type DebounceProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function DebounceConfigCard({ config, showAdvanced, onUpdateConfig }: DebounceProps): React.ReactElement {
  const quietSeconds = Number((config as any).quiet_period_seconds ?? 1.0);
  const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
  const keyFieldRaw = String((config as any).key_field ?? "stream_id").trim() || "stream_id";

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Quiet period (seconds)</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0.01}
          max={120}
          step={0.05}
          value={Number.isFinite(quietSeconds) ? String(quietSeconds) : "1.0"}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 1);
            onUpdateConfig((prev) => ({
              ...prev,
              quiet_period_seconds: Number.isFinite(nextValue) ? nextValue : 1.0,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Mode</span>
        <select
          className="pipelinesSelect"
          value={modeRaw}
          onChange={(event) => {
            const nextMode = String(event.target.value || "first").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, mode: nextMode }));
          }}
        >
          <option value="first">First (recommended)</option>
        </select>
      </label>

      {showAdvanced ? (
        <label className="pipelinesLabel">
          <span>Key</span>
          <select
            className="pipelinesSelect"
            value={keyFieldRaw}
            onChange={(event) => {
              const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
              onUpdateConfig((prev) => ({ ...prev, key_field: nextKey }));
            }}
          >
            <option value="stream_id">Stream (per object/camera)</option>
            <option value="payload.tracking_id">Tracking ID</option>
            <option value="payload.correlation_id">Correlation ID</option>
            <option value="payload.camera_id">Camera ID</option>
          </select>
        </label>
      ) : null}

      <div className="pipelinesStepHint">
        Emits OPEN/CLOSE packets always. Mode “first” emits the first UPDATE right away, then debounces subsequent updates.
      </div>
    </div>
  );
}

type DebugProps = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function DebugConfigCard({ config, onUpdateConfig }: DebugProps): React.ReactElement {
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
        <span>Enabled</span>
        <input type="checkbox" checked={enabled} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, enabled: event.target.checked }))} />
      </label>
      <div className="pipelinesStepHint">Prints packets to stdout and optionally writes images to a temporary folder.</div>

      <label className="pipelinesLabel">
        <span>Save images</span>
        <input type="checkbox" checked={saveImages} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, save_images: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>Max images per packet</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0}
          max={64}
          step={1}
          value={Number.isFinite(maxImagesPerPacket) ? String(maxImagesPerPacket) : "4"}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 0);
            onUpdateConfig((prev) => ({
              ...prev,
              max_images_per_packet: Number.isFinite(nextValue) ? Math.max(0, Math.min(64, nextValue)) : 4,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Output dir (optional)</span>
        <input
          className="pipelinesInput"
          type="text"
          value={outputDir}
          placeholder="System temp"
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, output_dir: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Print payload</span>
        <input type="checkbox" checked={printPayload} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, print_payload: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>Print metadata</span>
        <input type="checkbox" checked={printMetadata} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, print_metadata: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>Print artifacts</span>
        <input type="checkbox" checked={printArtifacts} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, print_artifacts: event.target.checked }))} />
      </label>
    </div>
  );
}

type StoreImagesProps = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function StoreImagesConfigCard({ config, onUpdateConfig }: StoreImagesProps): React.ReactElement {
  const formatRaw = String((config as any).format ?? "png").trim().toLowerCase() || "png";
  const format = formatRaw === "jpeg" ? "jpeg" : "png";
  const subdir = String((config as any).subdir ?? "pipelines").trim() || "pipelines";
  const keepData = Boolean((config as any).keep_data ?? false);

  const artifactNamesRaw = (config as any).artifact_names;
  const artifactNames = Array.isArray(artifactNamesRaw)
    ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : [];
  const selectedArtifactOptions = artifactNames.map((value) => ARTIFACT_SUGGESTIONS.find((option) => option.value === value) ?? { value, label: value });

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Artifacts</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={ARTIFACT_SUGGESTIONS}
          value={selectedArtifactOptions}
          placeholder="Full frame"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              artifact_names: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Stores artifacts locally on the origin. Notify uses stored references only.</div>

      <label className="pipelinesLabel">
        <span>Subdir</span>
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

      <label className="pipelinesLabel">
        <span>Format</span>
        <select
          className="pipelinesSelect"
          value={format}
          onChange={(event) => {
            const nextValue = String(event.target.value || "png").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, format: nextValue === "jpeg" ? "jpeg" : "png" }));
          }}
        >
          <option value="png">PNG</option>
          <option value="jpeg">JPEG</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>Keep data in memory</span>
        <input type="checkbox" checked={keepData} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, keep_data: event.target.checked }))} />
      </label>
      <div className="pipelinesStepHint">If disabled, pixel data is dropped after storing to keep memory stable.</div>
    </div>
  );
}

type NotifyProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function NotifyConfigCard({ config, showAdvanced, onUpdateConfig }: NotifyProps): React.ReactElement {
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
    : ["best_frame", "frame_original"];
  const notifySelectedFallbackOptions = notifyFallback.map((value: string) => ({ value, label: value }));

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Title template</span>
        <input
          className="pipelinesInput"
          type="text"
          value={title}
          placeholder="{{object_category_label}} detected"
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, title: nextValue }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        Use templates like <code>{"{{object_category_label}}"}</code>, <code>{"{{area_label}}"}</code>, <code>{"{{pose_label}}"}</code>.
      </div>

      <label className="pipelinesLabel">
        <span>Description template</span>
        <input
          className="pipelinesInput"
          type="text"
          value={description}
          placeholder="Optional"
          onChange={(event) => {
            const nextValue = String(event.target.value ?? "");
            onUpdateConfig((prev) => ({ ...prev, description: nextValue }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Priority</span>
        <select
          className="pipelinesSelect"
          value={priority}
          onChange={(event) => {
            const nextPriority = String(event.target.value || "medium").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, priority: nextPriority }));
          }}
        >
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>Realtime updates</span>
        <input type="checkbox" checked={realtime} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, realtime: event.target.checked }))} />
      </label>

      <label className="pipelinesLabel">
        <span>Update interval (seconds)</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0}
          max={60}
          step={0.1}
          value={Number.isFinite(updateIntervalSeconds) ? String(updateIntervalSeconds) : "1.0"}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 0);
            onUpdateConfig((prev) => ({
              ...prev,
              update_interval_seconds: Number.isFinite(nextValue) ? Math.max(0, Math.min(60, nextValue)) : 1.0,
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Avoids spamming UI updates while an event is open. Set to 0 to emit every change.</div>

      <label className="pipelinesLabel">
        <span>Thumbnail fallback</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={ARTIFACT_SUGGESTIONS}
          value={notifySelectedFallbackOptions}
          placeholder="Best frame → Face → Segmented → Full frame"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              thumbnail_with_fallback: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">
        Registers notifications only (never stores images). To include images, add Store Images before this step.
      </div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>Notification type</span>
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
            <span>Dedupe key template</span>
            <input
              className="pipelinesInput"
              type="text"
              value={dedupeKeyTemplate}
              placeholder="Leave empty for default"
              onChange={(event) => {
                const nextValue = String(event.target.value ?? "");
                onUpdateConfig((prev) => ({ ...prev, dedupe_key_template: nextValue }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">
            Use templates like <code>{"{{tracking_id}}"}</code>, <code>{"{{camera_id}}"}</code>, <code>{"{{object_category_label}}"}</code>.
          </div>
        </>
      ) : null}
    </div>
  );
}
