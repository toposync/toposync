import React from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";
import CreatableSelect from "react-select/creatable";
import type { StylesConfig } from "react-select";

import type { CameraContextsResponse, PipelineOperatorDefinition } from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";

import { buildArtifactSuggestions, pipelinesReactSelectStyles } from "../../constants";
import type { CameraAreaOption, InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "../../types";
import { prettyOperatorName } from "../../utils";
import { PipelinesNumberInput } from "../PipelinesNumberInput";
import {
  CropRectangleDrawModal,
  MotionMaskDrawModal,
  PerspectiveCropDrawModal,
  PrivacyRegionDrawModal,
  type SnapshotSource,
} from "./ImageDrawModals";
import { buildPipelineStepPreviewRequest } from "./pipelineStepSnapshots";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

type ImageDrawUnavailableReason =
  | { code: "invalid_graph"; detail?: string }
  | { code: "no_camera_source" | "no_camera_selected" | "no_pipeline_name" }
  | { code: "blocked"; operatorId?: string };

type ImageDrawEligibility =
  | { enabled: true; snapshotSource: SnapshotSource }
  | {
      enabled: false;
      snapshotSource: SnapshotSource | null;
      reason: ImageDrawUnavailableReason;
    };

function resolveImageDrawEligibility(
  steps: InteractiveStep[],
  currentIndex: number,
  pipelineName: string | null,
  nodeId: string,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): ImageDrawEligibility {
  const built = buildPipelineStepPreviewRequest(steps, currentIndex, pipelineName, nodeId, operatorsById);
  if (!built.enabled) {
    return { enabled: false, snapshotSource: null, reason: built.reason };
  }
  return { enabled: true, snapshotSource: { kind: "pipeline_step", request: built.request } };
}

function imageDrawUnavailableMessage(
  t: (key: string, vars?: Record<string, unknown>) => string,
  reason: ImageDrawUnavailableReason,
): string {
  if (reason.code === "no_camera_source") return t("core.ui.pipelines.panels.image_draw.unavailable.no_source");
  if (reason.code === "no_camera_selected") return t("core.ui.pipelines.panels.image_draw.unavailable.no_camera");
  if (reason.code === "no_pipeline_name") return t("core.ui.pipelines.panels.image_draw.unavailable.no_pipeline");
  if (reason.code === "invalid_graph") return String(reason.detail || t("core.ui.pipelines.panels.image_draw.unavailable.no_pipeline"));
  if (reason.code === "blocked") {
    return t("core.ui.pipelines.panels.image_draw.unavailable.blocked", { operator: prettyOperatorName(reason.operatorId ?? "") });
  }
  return t("core.ui.pipelines.panels.image_draw.unavailable.no_pipeline");
}

type CameraSourceProps = {
  config: Record<string, unknown>;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  onUpdateConfig: UpdateConfig;
};

export function CameraSourceConfigCard({
  config,
  cameraSelectOptions,
  cameraSelectOptionById,
  onUpdateConfig,
}: CameraSourceProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const cameraIdInConfig = String((config as any).camera_id ?? "").trim();
  const backendRaw = String((config as any).backend ?? "auto").trim().toLowerCase() || "auto";
  const backend = backendRaw === "opencv" || backendRaw === "ffmpeg" ? backendRaw : "auto";
  const selectedCameraOption = cameraIdInConfig
    ? (cameraSelectOptionById.get(cameraIdInConfig) ?? { value: cameraIdInConfig, label: cameraIdInConfig })
    : null;

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.camera_source.camera")}</span>
        <Select<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={cameraSelectOptions}
          value={selectedCameraOption}
          isClearable
          placeholder={t("core.ui.pipelines.panels.camera_source.camera_placeholder")}
          onChange={(value: SingleValue<SelectOption>) => {
            onUpdateConfig((prev) => {
              const next = { ...prev };
              (next as any).camera_id = value?.value ?? "";
              if (value?.value) {
                (next as any).rtsp_url = "";
                (next as any).username = "";
                (next as any).password = "";
              }
              return next;
            });
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.camera_source.hint_infer")}</div>
      {cameraSelectOptions.length === 0 ? (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.camera_source.hint_no_cameras")}</div>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.camera_source.backend")}</span>
        <select
          className="pipelinesSelect"
          value={backend}
          onChange={(event) => {
            const next = String(event.target.value || "auto").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, backend: next || "auto" }));
          }}
        >
          <option value="auto">{t("core.ui.pipelines.panels.camera_source.backend.auto")}</option>
          <option value="opencv">OpenCV</option>
          <option value="ffmpeg">FFmpeg</option>
        </select>
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.camera_source.hint_backend")}</div>
    </div>
  );
}

type CameraMappingProps = {
  interactiveCameraId: string;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
};

export function CameraMappingConfigCard({
  interactiveCameraId,
  activeCameraContexts,
  activeCameraContextsError,
}: CameraMappingProps): React.ReactElement {
  const { t } = i18n.useI18n();
  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">
        {t("core.ui.pipelines.panels.camera_mapping.hint")}
      </div>
      {!interactiveCameraId ? (
        <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.camera_mapping.select_camera_error")}</div>
      ) : activeCameraContexts ? (
        <div className="pipelinesContextList">
          {activeCameraContexts.compositions.map((composition) => {
            const hasMapping = composition.camera_elements.some((element) => element.has_mapping);
            const areasCount = composition.areas.length;
            const elementNames = composition.camera_elements.map((item) => item.name).filter((value) => value.length > 0);
            return (
              <div key={composition.id} className="pipelinesContextRow">
                <div className="pipelinesContextMain">
                  <div className="pipelinesContextName">{composition.name}</div>
                  <div className="pipelinesContextMeta">
                    {hasMapping ? t("core.ui.pipelines.panels.camera_mapping.mapping_ready") : t("core.ui.pipelines.panels.camera_mapping.mapping_missing")}
                    {areasCount ? ` • ${t("core.ui.pipelines.panels.camera_mapping.areas_count", { count: areasCount })}` : ""}
                    {elementNames.length ? ` • ${t("core.ui.pipelines.panels.camera_mapping.camera_nodes", { names: elementNames.join(", ") })}` : ""}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : activeCameraContextsError ? (
        <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.camera_mapping.load_failed", { error: activeCameraContextsError })}</div>
      ) : (
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.camera_mapping.loading")}</div>
      )}
    </div>
  );
}

type AreaRestrictionProps = {
  config: Record<string, unknown>;
  interactiveCameraId: string;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: CameraAreaOption[];
  onUpdateConfig: UpdateConfig;
};

export function AreaRestrictionConfigCard({
  config,
  interactiveCameraId,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  onUpdateConfig,
}: AreaRestrictionProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const areaNamesRaw = (config as any).include_area_names;
  const selectedAreaKeys = Array.isArray(areaNamesRaw)
    ? areaNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : [];
  const cameraAreaOptionByName = React.useMemo(() => {
    const optionByName = new Map<string, CameraAreaOption>();
    for (const option of cameraAreaOptions) {
      if (!optionByName.has(option.areaName)) optionByName.set(option.areaName, option);
    }
    return optionByName;
  }, [cameraAreaOptions]);
  const selectedAreaOptions = selectedAreaKeys.map(
    (value) =>
      cameraAreaOptionByName.get(value) ?? {
        value: `missing:${value}`,
        label: value,
        compositionId: "",
        areaId: "",
        areaName: value,
        points: [],
      },
  );
  const invalidAreaSelections = selectedAreaKeys.filter((value) => !cameraAreaOptionByName.has(value));

  return (
    <>
      <div className="pipelinesOperatorConfigCard">
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.area_restriction.areas")}</span>
          <Select<CameraAreaOption, true>
            isMulti
            styles={pipelinesReactSelectStyles as unknown as StylesConfig<CameraAreaOption, true>}
            options={cameraAreaOptions}
            value={selectedAreaOptions}
            isDisabled={!interactiveCameraId || !activeCameraContexts || Boolean(activeCameraContextsError) || cameraAreaOptions.length === 0}
            placeholder={
              !interactiveCameraId ? t("core.ui.pipelines.panels.area_restriction.select_camera_first") : t("core.ui.pipelines.panels.area_restriction.select_areas")
            }
            onChange={(value: MultiValue<CameraAreaOption>) => {
              const selectedOptions = value.map((item) => ({
                areaName: String(item.areaName || "").trim(),
                points: Array.isArray(item.points)
                  ? item.points
                      .map((point) => {
                        const x = Number((point as any)?.x);
                        const z = Number((point as any)?.z);
                        return Number.isFinite(x) && Number.isFinite(z) ? { x, z } : null;
                      })
                      .filter((point): point is { x: number; z: number } => point !== null)
                  : [],
              }));
              const includeAreaNames = Array.from(new Set(selectedOptions.map((item) => item.areaName).filter(Boolean)));
              onUpdateConfig((prev) => ({
                ...prev,
                areas: selectedOptions
                  .filter((item) => item.areaName && item.points.length >= 3)
                  .map((item) => ({
                    name: item.areaName,
                    points: item.points.map((point) => ({ x: point.x, z: point.z })),
                  })),
                exclude_area_names: [],
                include_area_names: includeAreaNames,
              }));
            }}
          />
        </label>
        {!interactiveCameraId ? (
          <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.area_restriction.select_camera_step_error")}</div>
        ) : activeCameraContextsError ? (
          <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.area_restriction.load_failed", { error: activeCameraContextsError })}</div>
        ) : !activeCameraContexts ? (
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.area_restriction.loading")}</div>
        ) : cameraAreaOptions.length === 0 ? (
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.area_restriction.no_areas")}</div>
        ) : (
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.area_restriction.hint_areas")}</div>
        )}
      </div>

      {invalidAreaSelections.length > 0 ? (
        <div className="pipelinesInlineError">
          {t("core.ui.pipelines.panels.area_restriction.invalid_areas", { areas: invalidAreaSelections.join(", ") })}
        </div>
      ) : null}
    </>
  );
}

type VelocityProps = {
  config: Record<string, unknown>;
  steps: InteractiveStep[];
  index: number;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function VelocityEstimationConfigCard({
  config,
  steps,
  index,
  showAdvanced,
  onUpdateConfig,
}: VelocityProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const modeRaw = String((config as any).filter_mode ?? "annotate").trim().toLowerCase() || "annotate";
  const stoppedMpsRaw = Number((config as any).stopped_speed_threshold ?? 0.04);
  const stoppedKmh = Number.isFinite(stoppedMpsRaw) ? stoppedMpsRaw * 3.6 : 0.0;
  const hasMappingBefore = steps.slice(0, index).some((item) => item.operatorId === "camera.camera_mapping");

  const modeOptions: Array<{ value: string; label: string; hint: string }> = [
    { value: "annotate", label: t("core.ui.pipelines.panels.velocity.mode.annotate.label"), hint: t("core.ui.pipelines.panels.velocity.mode.annotate.hint") },
    { value: "stopped_now", label: t("core.ui.pipelines.panels.velocity.mode.stopped_now.label"), hint: t("core.ui.pipelines.panels.velocity.mode.stopped_now.hint") },
    { value: "moving_now", label: t("core.ui.pipelines.panels.velocity.mode.moving_now.label"), hint: t("core.ui.pipelines.panels.velocity.mode.moving_now.hint") },
  ];
  if (showAdvanced) {
    modeOptions.push(
      { value: "stopped_once", label: t("core.ui.pipelines.panels.velocity.mode.stopped_once.label"), hint: t("core.ui.pipelines.panels.velocity.mode.stopped_once.hint") },
      { value: "always_moving", label: t("core.ui.pipelines.panels.velocity.mode.always_moving.label"), hint: t("core.ui.pipelines.panels.velocity.mode.always_moving.hint") },
    );
  }
  const selected = modeOptions.find((item) => item.value === modeRaw) ?? modeOptions[0];

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.velocity.flow_mode")}</span>
        <select
          className="pipelinesSelect"
          value={selected.value}
          onChange={(event) => {
            const nextMode = String(event.target.value || "annotate").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, filter_mode: nextMode }));
          }}
        >
          {modeOptions.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <div className="pipelinesStepHint">{selected.hint}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.velocity.stopped_threshold")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={4000}
          step={0.05}
          value={Number.isFinite(stoppedKmh) ? Math.max(0, stoppedKmh) : 0}
          onChange={(kmh) => {
            const mps = Number.isFinite(kmh) ? Math.max(0, kmh) / 3.6 : 0;
            onUpdateConfig((prev) => ({ ...prev, stopped_speed_threshold: mps }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.velocity.hint")}</div>
      {!hasMappingBefore ? <div className="pipelinesInlineError">{t("core.ui.pipelines.panels.velocity.mapping_required")}</div> : null}
    </div>
  );
}

type MotionGateProps = {
  config: Record<string, unknown>;
  stepUid: string;
  nodeId: string;
  pipelineName: string | null;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  index: number;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
};

type MotionMaskMode = "include" | "exclude";

function parseMotionMaskMode(value: unknown): MotionMaskMode {
  const normalized = String(value ?? "").trim().toLowerCase();
  return normalized === "exclude" ? "exclude" : "include";
}

type MotionMaskStroke = { op?: "paint" | "erase"; points01?: Array<[number, number] | { x: number; y: number }> };

function parseMotionMaskStrokes(value: unknown): MotionMaskStroke[] {
  return Array.isArray(value) ? (value as MotionMaskStroke[]) : [];
}

export function MotionGateConfigCard({
  config,
  stepUid,
  nodeId,
  pipelineName,
  steps,
  operatorsById,
  index,
  showAdvanced,
  onUpdateConfig,
  onOpenTelemetryField,
}: MotionGateProps): React.ReactElement {
  const { t } = i18n.useI18n();

  const thresholdRaw = Number((config as any).threshold ?? 0.01);
  const threshold = Number.isFinite(thresholdRaw) ? Math.max(0, Math.min(1, thresholdRaw)) : 0.01;

  const holdSecondsRaw = Number((config as any).hold_seconds ?? 2.5);
  const holdSeconds = Number.isFinite(holdSecondsRaw) ? Math.max(0, Math.min(120, holdSecondsRaw)) : 2.5;

  const activationFramesRaw = Number((config as any).activation_frames ?? 1);
  const activationFrames = Number.isFinite(activationFramesRaw) ? Math.max(1, Math.min(100, Math.round(activationFramesRaw))) : 1;

	  const emitWhenIdle = Boolean((config as any).emit_when_idle ?? false);

	  const maskEnabled = Boolean((config as any).mask_enabled ?? false);
	  const maskMode = parseMotionMaskMode((config as any).mask_mode);
	  const maskBrushDiameter01Raw = Number((config as any).mask_brush_diameter01 ?? 0.1);
	  const maskBrushDiameter01 = Number.isFinite(maskBrushDiameter01Raw)
	    ? Math.max(0.002, Math.min(0.25, maskBrushDiameter01Raw))
	    : 0.1;
	  const maskStrokes = parseMotionMaskStrokes((config as any).mask_strokes);

  const inputWithFallback = String((config as any).input_with_fallback ?? "segmented,treated,original").trim() || "segmented,treated,original";
  const fallbackToStreamFrame = (config as any).fallback_to_stream_frame ?? (config as any).fallback_to_payload_frame ?? true;

  const drawEligibility = React.useMemo(
    () => resolveImageDrawEligibility(steps, index, pipelineName, nodeId, operatorsById),
    [steps, index, pipelineName, nodeId, operatorsById],
  );

  const [isDrawOpen, setIsDrawOpen] = React.useState(false);

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.hint")}</div>

      <label className="pipelinesLabel">
        <div className="pipelinesScalarLabelHeader">
          <span>{t("core.ui.pipelines.panels.motion_gate.threshold")}</span>
          {onOpenTelemetryField ? (
            <button
              className="iconButton pipelinesTelemetryFieldButton"
              type="button"
              title={t("core.ui.pipelines.telemetry.field.open_histogram")}
              onClick={() =>
                onOpenTelemetryField({
                  stepUid,
                  nodeId,
                  operatorId: "camera.motion_gate",
                  configKey: "threshold",
                  metricId: "motion.score",
                  label: t("core.ui.pipelines.panels.motion_gate.threshold"),
                  value: threshold,
                })
              }
            >
              <i className="fa-solid fa-chart-column" aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={1}
          step={0.001}
          value={threshold}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.01;
            onUpdateConfig((prev) => ({ ...prev, threshold: normalized }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.hold_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={120}
          step={0.05}
          value={holdSeconds}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(120, nextValue)) : 2.5;
            onUpdateConfig((prev) => ({ ...prev, hold_seconds: normalized }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.hold_seconds_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.activation_frames")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={1}
          max={100}
          step={1}
          value={activationFrames}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(100, Math.round(nextValue))) : 1;
            onUpdateConfig((prev) => ({ ...prev, activation_frames: normalized }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.activation_frames_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.emit_when_idle")}</span>
        <input
          type="checkbox"
          checked={emitWhenIdle}
          onChange={(event) => onUpdateConfig((prev) => ({ ...prev, emit_when_idle: event.target.checked }))}
        />
      </label>

      <div className="sectionDivider" />
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.mask.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.mask.enabled")}</span>
        <input
          type="checkbox"
          checked={maskEnabled}
          onChange={(event) => onUpdateConfig((prev) => ({ ...prev, mask_enabled: event.target.checked }))}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.mask.mode")}</span>
        <select
          className="pipelinesSelect"
          value={maskMode}
          onChange={(event) => {
            const next = parseMotionMaskMode(event.target.value);
            onUpdateConfig((prev) => ({ ...prev, mask_mode: next }));
          }}
        >
          <option value="include">{t("core.ui.pipelines.panels.motion_gate.mask.mode.include")}</option>
          <option value="exclude">{t("core.ui.pipelines.panels.motion_gate.mask.mode.exclude")}</option>
        </select>
      </label>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!drawEligibility.enabled}
          onClick={() => setIsDrawOpen(true)}
        >
          {t("core.ui.pipelines.panels.motion_gate.mask.draw")}
        </button>

        <button
          className="chipButton"
          type="button"
          disabled={maskStrokes.length === 0}
          onClick={() => onUpdateConfig((prev) => ({ ...prev, mask_strokes: [] }))}
        >
          {t("core.ui.pipelines.panels.motion_gate.mask.clear")}
        </button>
      </div>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.mask.strokes_count", { count: maskStrokes.length })}</div>

      {!drawEligibility.enabled ? (
        <div className="pipelinesStepHint" style={{ textAlign: "right" }}>
          {imageDrawUnavailableMessage(t, drawEligibility.reason)}
        </div>
      ) : null}

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.input_with_fallback")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={inputWithFallback}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, input_with_fallback: event.target.value }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.fallback_to_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))}
            />
          </label>
	          <label className="pipelinesLabel">
	            <span>{t("core.ui.pipelines.panels.motion_gate.mask.brush_diameter")}</span>
	            <PipelinesNumberInput
	              className="pipelinesInput"
	              min={0.002}
	              max={0.25}
	              step={0.001}
	              value={maskBrushDiameter01}
	              onChange={(nextValue) => {
	                const normalized = Number.isFinite(nextValue) ? Math.max(0.002, Math.min(0.25, nextValue)) : 0.05;
	                onUpdateConfig((prev) => ({ ...prev, mask_brush_diameter01: normalized }));
	              }}
	            />
	          </label>
	        </>
	      ) : null}

      <MotionMaskDrawModal
        open={isDrawOpen}
        onClose={() => setIsDrawOpen(false)}
        snapshotSource={drawEligibility.snapshotSource}
        mode={maskMode}
        brushDiameter01={maskBrushDiameter01}
        strokes={maskStrokes}
        onApply={(next) =>
          onUpdateConfig((prev) => ({
            ...prev,
            mask_enabled: true,
            mask_mode: next.mode,
            mask_strokes: next.strokes,
          }))
        }
      />
    </div>
  );
}

type MotionBgSubAdaptiveProps = MotionGateProps;

export function MotionBgSubAdaptiveConfigCard({
  config,
  stepUid,
  nodeId,
  pipelineName,
  steps,
  operatorsById,
  index,
  showAdvanced,
  onUpdateConfig,
  onOpenTelemetryField,
}: MotionBgSubAdaptiveProps): React.ReactElement {
  const { t } = i18n.useI18n();

  const thresholdRaw = Number((config as any).threshold ?? 0.01);
  const threshold = Number.isFinite(thresholdRaw) ? Math.max(0, Math.min(1, thresholdRaw)) : 0.01;

  const thresholdLowRaw = Number((config as any).threshold_low ?? 0.0075);
  const thresholdLow = Number.isFinite(thresholdLowRaw) ? Math.max(0, Math.min(threshold, thresholdLowRaw)) : 0.0075;

  const holdSecondsRaw = Number((config as any).hold_seconds ?? 2.5);
  const holdSeconds = Number.isFinite(holdSecondsRaw) ? Math.max(0, Math.min(120, holdSecondsRaw)) : 2.5;

  const activationFramesRaw = Number((config as any).activation_frames ?? 1);
  const activationFrames = Number.isFinite(activationFramesRaw) ? Math.max(1, Math.min(100, Math.round(activationFramesRaw))) : 1;

  const filterWhenInactive = Boolean((config as any).filter_when_inactive ?? true);
  const backend = String((config as any).backend ?? "mog2").trim().toLowerCase() === "knn" ? "knn" : "mog2";
  const downscaleHeightRaw = Number((config as any).downscale_height ?? 180);
  const downscaleHeight = Number.isFinite(downscaleHeightRaw) ? Math.max(0, Math.min(2160, Math.round(downscaleHeightRaw))) : 180;
  const historyRaw = Number((config as any).history ?? 300);
  const history = Number.isFinite(historyRaw) ? Math.max(1, Math.min(10000, Math.round(historyRaw))) : 300;
  const detectShadows = Boolean((config as any).detect_shadows ?? true);
  const shadowMode = String((config as any).shadow_mode ?? "exclude").trim().toLowerCase() === "count" ? "count" : "exclude";
  const varThresholdRaw = Number((config as any).var_threshold ?? 16);
  const varThreshold = Number.isFinite(varThresholdRaw) ? Math.max(0, Math.min(2048, varThresholdRaw)) : 16;
  const dist2ThresholdRaw = Number((config as any).dist2_threshold ?? 400);
  const dist2Threshold = Number.isFinite(dist2ThresholdRaw) ? Math.max(0, Math.min(32768, dist2ThresholdRaw)) : 400;
  const knnSamplesRaw = Number((config as any).knn_samples ?? 2);
  const knnSamples = Number.isFinite(knnSamplesRaw) ? Math.max(1, Math.min(32, Math.round(knnSamplesRaw))) : 2;
  const blurKernelSizeRaw = Number((config as any).blur_kernel_size ?? 5);
  const blurKernelSize = Number.isFinite(blurKernelSizeRaw) ? Math.max(0, Math.min(63, Math.round(blurKernelSizeRaw))) : 5;
  const morphologyOpenPxRaw = Number((config as any).morphology_open_px ?? 3);
  const morphologyOpenPx = Number.isFinite(morphologyOpenPxRaw) ? Math.max(0, Math.min(63, Math.round(morphologyOpenPxRaw))) : 3;
  const morphologyClosePxRaw = Number((config as any).morphology_close_px ?? 5);
  const morphologyClosePx = Number.isFinite(morphologyClosePxRaw) ? Math.max(0, Math.min(63, Math.round(morphologyClosePxRaw))) : 5;
  const minBlobAreaRatioRaw = Number((config as any).min_blob_area_ratio ?? 0.0005);
  const minBlobAreaRatio = Number.isFinite(minBlobAreaRatioRaw) ? Math.max(0, Math.min(1, minBlobAreaRatioRaw)) : 0.0005;
  const maxBlobsRaw = Number((config as any).max_blobs ?? 8);
  const maxBlobs = Number.isFinite(maxBlobsRaw) ? Math.max(1, Math.min(64, Math.round(maxBlobsRaw))) : 8;

  const maskEnabled = Boolean((config as any).mask_enabled ?? false);
  const maskMode = parseMotionMaskMode((config as any).mask_mode);
  const maskBrushDiameter01Raw = Number((config as any).mask_brush_diameter01 ?? 0.1);
  const maskBrushDiameter01 = Number.isFinite(maskBrushDiameter01Raw)
    ? Math.max(0.002, Math.min(0.25, maskBrushDiameter01Raw))
    : 0.1;
  const maskStrokes = parseMotionMaskStrokes((config as any).mask_strokes);

  const inputWithFallback = String((config as any).input_with_fallback ?? "segmented,treated,original").trim() || "segmented,treated,original";
  const fallbackToStreamFrame = (config as any).fallback_to_stream_frame ?? true;

  const drawEligibility = React.useMemo(
    () => resolveImageDrawEligibility(steps, index, pipelineName, nodeId, operatorsById),
    [steps, index, pipelineName, nodeId, operatorsById],
  );

  const [isDrawOpen, setIsDrawOpen] = React.useState(false);

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_bgsub_adaptive.hint")}</div>

      <label className="pipelinesLabel">
        <div className="pipelinesScalarLabelHeader">
          <span>{t("core.ui.pipelines.panels.motion_gate.threshold")}</span>
          {onOpenTelemetryField ? (
            <button
              className="iconButton pipelinesTelemetryFieldButton"
              type="button"
              title={t("core.ui.pipelines.telemetry.field.open_histogram")}
              onClick={() =>
                onOpenTelemetryField({
                  stepUid,
                  nodeId,
                  operatorId: "camera.motion_bgsub_adaptive",
                  configKey: "threshold",
                  metricId: "motion.score",
                  label: t("core.ui.pipelines.panels.motion_gate.threshold"),
                  value: threshold,
                })
              }
            >
              <i className="fa-solid fa-chart-column" aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={1}
          step={0.001}
          value={threshold}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.01;
            onUpdateConfig((prev) => ({
              ...prev,
              threshold: normalized,
              threshold_low:
                Number((prev as any).threshold_low ?? thresholdLow) > normalized
                  ? normalized
                  : (prev as any).threshold_low ?? thresholdLow,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.backend")}</span>
        <select
          className="pipelinesSelect"
          value={backend}
          onChange={(event) => {
            const next = String(event.target.value || "mog2").trim().toLowerCase() === "knn" ? "knn" : "mog2";
            onUpdateConfig((prev) => ({ ...prev, backend: next }));
          }}
        >
          <option value="mog2">{t("core.ui.pipelines.panels.motion_bgsub_adaptive.backend.mog2")}</option>
          <option value="knn">{t("core.ui.pipelines.panels.motion_bgsub_adaptive.backend.knn")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.hold_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={120}
          step={0.05}
          value={holdSeconds}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(120, nextValue)) : 2.5;
            onUpdateConfig((prev) => ({ ...prev, hold_seconds: normalized }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.hold_seconds_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.activation_frames")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={1}
          max={100}
          step={1}
          value={activationFrames}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(100, Math.round(nextValue))) : 1;
            onUpdateConfig((prev) => ({ ...prev, activation_frames: normalized }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.activation_frames_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.filter_when_inactive")}</span>
        <input
          type="checkbox"
          checked={filterWhenInactive}
          onChange={(event) => onUpdateConfig((prev) => ({ ...prev, filter_when_inactive: event.target.checked }))}
        />
      </label>

      <div className="sectionDivider" />
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.mask.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.mask.enabled")}</span>
        <input
          type="checkbox"
          checked={maskEnabled}
          onChange={(event) => onUpdateConfig((prev) => ({ ...prev, mask_enabled: event.target.checked }))}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.mask.mode")}</span>
        <select
          className="pipelinesSelect"
          value={maskMode}
          onChange={(event) => {
            const next = parseMotionMaskMode(event.target.value);
            onUpdateConfig((prev) => ({ ...prev, mask_mode: next }));
          }}
        >
          <option value="include">{t("core.ui.pipelines.panels.motion_gate.mask.mode.include")}</option>
          <option value="exclude">{t("core.ui.pipelines.panels.motion_gate.mask.mode.exclude")}</option>
        </select>
      </label>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!drawEligibility.enabled}
          onClick={() => setIsDrawOpen(true)}
        >
          {t("core.ui.pipelines.panels.motion_gate.mask.draw")}
        </button>

        <button
          className="chipButton"
          type="button"
          disabled={maskStrokes.length === 0}
          onClick={() => onUpdateConfig((prev) => ({ ...prev, mask_strokes: [] }))}
        >
          {t("core.ui.pipelines.panels.motion_gate.mask.clear")}
        </button>
      </div>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.mask.strokes_count", { count: maskStrokes.length })}</div>

      {!drawEligibility.enabled ? (
        <div className="pipelinesStepHint" style={{ textAlign: "right" }}>
          {imageDrawUnavailableMessage(t, drawEligibility.reason)}
        </div>
      ) : null}

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.input_with_fallback")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={inputWithFallback}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, input_with_fallback: event.target.value }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.fallback_to_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.mask.brush_diameter")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.002}
              max={0.25}
              step={0.001}
              value={maskBrushDiameter01}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0.002, Math.min(0.25, nextValue)) : 0.1;
                onUpdateConfig((prev) => ({ ...prev, mask_brush_diameter01: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.threshold_low")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={threshold}
              step={0.001}
              value={thresholdLow}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(threshold, nextValue)) : thresholdLow;
                onUpdateConfig((prev) => ({ ...prev, threshold_low: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.downscale_height")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={2160}
              step={1}
              value={downscaleHeight}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(2160, Math.round(nextValue))) : 180;
                onUpdateConfig((prev) => ({ ...prev, downscale_height: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.history")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={10000}
              step={1}
              value={history}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(10000, Math.round(nextValue))) : 300;
                onUpdateConfig((prev) => ({ ...prev, history: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.detect_shadows")}</span>
            <input
              type="checkbox"
              checked={detectShadows}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, detect_shadows: event.target.checked }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.shadow_mode")}</span>
            <select
              className="pipelinesSelect"
              value={shadowMode}
              onChange={(event) => {
                const next = String(event.target.value || "exclude").trim().toLowerCase() === "count" ? "count" : "exclude";
                onUpdateConfig((prev) => ({ ...prev, shadow_mode: next }));
              }}
            >
              <option value="exclude">{t("core.ui.pipelines.panels.motion_bgsub_adaptive.shadow_mode.exclude")}</option>
              <option value="count">{t("core.ui.pipelines.panels.motion_bgsub_adaptive.shadow_mode.count")}</option>
            </select>
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.var_threshold")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={2048}
              step={1}
              value={varThreshold}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(2048, nextValue)) : 16;
                onUpdateConfig((prev) => ({ ...prev, var_threshold: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.dist2_threshold")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={32768}
              step={1}
              value={dist2Threshold}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(32768, nextValue)) : 400;
                onUpdateConfig((prev) => ({ ...prev, dist2_threshold: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.knn_samples")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={32}
              step={1}
              value={knnSamples}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(32, Math.round(nextValue))) : 2;
                onUpdateConfig((prev) => ({ ...prev, knn_samples: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.blur_kernel_size")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={63}
              step={1}
              value={blurKernelSize}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(63, Math.round(nextValue))) : 5;
                onUpdateConfig((prev) => ({ ...prev, blur_kernel_size: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.morphology_open_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={63}
              step={1}
              value={morphologyOpenPx}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(63, Math.round(nextValue))) : 3;
                onUpdateConfig((prev) => ({ ...prev, morphology_open_px: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.morphology_close_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={63}
              step={1}
              value={morphologyClosePx}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(63, Math.round(nextValue))) : 5;
                onUpdateConfig((prev) => ({ ...prev, morphology_close_px: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.min_blob_area_ratio")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1}
              step={0.0001}
              value={minBlobAreaRatio}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.0005;
                onUpdateConfig((prev) => ({ ...prev, min_blob_area_ratio: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_bgsub_adaptive.max_blobs")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={64}
              step={1}
              value={maxBlobs}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(64, Math.round(nextValue))) : 8;
                onUpdateConfig((prev) => ({ ...prev, max_blobs: normalized }));
              }}
            />
          </label>
        </>
      ) : null}

      <MotionMaskDrawModal
        open={isDrawOpen}
        onClose={() => setIsDrawOpen(false)}
        snapshotSource={drawEligibility.snapshotSource}
        mode={maskMode}
        brushDiameter01={maskBrushDiameter01}
        strokes={maskStrokes}
        onApply={(next) =>
          onUpdateConfig((prev) => ({
            ...prev,
            mask_enabled: true,
            mask_mode: next.mode,
            mask_strokes: next.strokes,
          }))
        }
      />
    </div>
  );
}

type MotionSampleBgProps = MotionGateProps;

export function MotionSampleBgConfigCard({
  config,
  stepUid,
  nodeId,
  pipelineName,
  steps,
  operatorsById,
  index,
  showAdvanced,
  onUpdateConfig,
  onOpenTelemetryField,
}: MotionSampleBgProps): React.ReactElement {
  const { t } = i18n.useI18n();

  const thresholdRaw = Number((config as any).threshold ?? 0.01);
  const threshold = Number.isFinite(thresholdRaw) ? Math.max(0, Math.min(1, thresholdRaw)) : 0.01;

  const thresholdLowRaw = Number((config as any).threshold_low ?? 0.0075);
  const thresholdLow = Number.isFinite(thresholdLowRaw) ? Math.max(0, Math.min(threshold, thresholdLowRaw)) : 0.0075;

  const holdSecondsRaw = Number((config as any).hold_seconds ?? 2.5);
  const holdSeconds = Number.isFinite(holdSecondsRaw) ? Math.max(0, Math.min(120, holdSecondsRaw)) : 2.5;

  const activationFramesRaw = Number((config as any).activation_frames ?? 1);
  const activationFrames = Number.isFinite(activationFramesRaw) ? Math.max(1, Math.min(100, Math.round(activationFramesRaw))) : 1;

  const filterWhenInactive = Boolean((config as any).filter_when_inactive ?? true);
  const backendRaw = String((config as any).backend ?? "pbas_lite").trim().toLowerCase();
  const backend = backendRaw === "vibe_core" ? "vibe_core" : "pbas_lite";
  const featureModeRaw = String((config as any).feature_mode ?? "gray_gradient").trim().toLowerCase();
  const featureMode =
    featureModeRaw === "gray" || featureModeRaw === "ycrcb_gradient" ? featureModeRaw : "gray_gradient";
  const downscaleHeightRaw = Number((config as any).downscale_height ?? 180);
  const downscaleHeight = Number.isFinite(downscaleHeightRaw) ? Math.max(0, Math.min(2160, Math.round(downscaleHeightRaw))) : 180;
  const sampleCountRaw = Number((config as any).sample_count ?? 20);
  const sampleCount = Number.isFinite(sampleCountRaw) ? Math.max(4, Math.min(128, Math.round(sampleCountRaw))) : 20;
  const minMatchesRaw = Number((config as any).min_matches ?? 2);
  const minMatches = Number.isFinite(minMatchesRaw) ? Math.max(1, Math.min(sampleCount, Math.round(minMatchesRaw))) : 2;
  const rLowerRaw = Number((config as any).r_lower ?? 18);
  const rLower = Number.isFinite(rLowerRaw) ? Math.max(1, Math.min(255, rLowerRaw)) : 18;
  const rScaleRaw = Number((config as any).r_scale ?? 5);
  const rScale = Number.isFinite(rScaleRaw) ? Math.max(0.5, Math.min(64, rScaleRaw)) : 5;
  const rIncdecRaw = Number((config as any).r_incdec ?? 0.05);
  const rIncdec = Number.isFinite(rIncdecRaw) ? Math.max(0.001, Math.min(10, rIncdecRaw)) : 0.05;
  const tLowerRaw = Number((config as any).t_lower ?? 2);
  const tLower = Number.isFinite(tLowerRaw) ? Math.max(1, Math.min(512, tLowerRaw)) : 2;
  const tUpperRaw = Number((config as any).t_upper ?? 200);
  const tUpper = Number.isFinite(tUpperRaw) ? Math.max(tLower, Math.min(4096, tUpperRaw)) : 200;
  const tIncRaw = Number((config as any).t_inc ?? 1);
  const tInc = Number.isFinite(tIncRaw) ? Math.max(0.01, Math.min(128, tIncRaw)) : 1;
  const tDecRaw = Number((config as any).t_dec ?? 0.05);
  const tDec = Number.isFinite(tDecRaw) ? Math.max(0.001, Math.min(10, tDecRaw)) : 0.05;
  const enableNeighborPropagation = Boolean((config as any).enable_neighbor_propagation ?? true);
  const warmupFramesRaw = Number((config as any).warmup_frames ?? 30);
  const warmupFrames = Number.isFinite(warmupFramesRaw) ? Math.max(1, Math.min(600, Math.round(warmupFramesRaw))) : 30;
  const sceneResetScoreRaw = Number((config as any).scene_reset_score ?? 0.6);
  const sceneResetScore = Number.isFinite(sceneResetScoreRaw) ? Math.max(0, Math.min(1, sceneResetScoreRaw)) : 0.6;
  const randomSeedRaw = Number((config as any).random_seed ?? 0);
  const randomSeed = Number.isFinite(randomSeedRaw) ? Math.max(0, Math.min(2147483647, Math.round(randomSeedRaw))) : 0;
  const morphologyOpenPxRaw = Number((config as any).morphology_open_px ?? 2);
  const morphologyOpenPx = Number.isFinite(morphologyOpenPxRaw) ? Math.max(0, Math.min(63, Math.round(morphologyOpenPxRaw))) : 2;
  const morphologyClosePxRaw = Number((config as any).morphology_close_px ?? 4);
  const morphologyClosePx = Number.isFinite(morphologyClosePxRaw) ? Math.max(0, Math.min(63, Math.round(morphologyClosePxRaw))) : 4;
  const minBlobAreaRatioRaw = Number((config as any).min_blob_area_ratio ?? 0.0005);
  const minBlobAreaRatio = Number.isFinite(minBlobAreaRatioRaw) ? Math.max(0, Math.min(1, minBlobAreaRatioRaw)) : 0.0005;
  const maxBlobsRaw = Number((config as any).max_blobs ?? 8);
  const maxBlobs = Number.isFinite(maxBlobsRaw) ? Math.max(1, Math.min(64, Math.round(maxBlobsRaw))) : 8;

  const maskEnabled = Boolean((config as any).mask_enabled ?? false);
  const maskMode = parseMotionMaskMode((config as any).mask_mode);
  const maskBrushDiameter01Raw = Number((config as any).mask_brush_diameter01 ?? 0.1);
  const maskBrushDiameter01 = Number.isFinite(maskBrushDiameter01Raw)
    ? Math.max(0.002, Math.min(0.25, maskBrushDiameter01Raw))
    : 0.1;
  const maskStrokes = parseMotionMaskStrokes((config as any).mask_strokes);

  const inputWithFallback = String((config as any).input_with_fallback ?? "segmented,treated,original").trim() || "segmented,treated,original";
  const fallbackToStreamFrame = (config as any).fallback_to_stream_frame ?? true;

  const drawEligibility = React.useMemo(
    () => resolveImageDrawEligibility(steps, index, pipelineName, nodeId, operatorsById),
    [steps, index, pipelineName, nodeId, operatorsById],
  );

  const [isDrawOpen, setIsDrawOpen] = React.useState(false);

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_sample_bg.hint")}</div>

      <label className="pipelinesLabel">
        <div className="pipelinesScalarLabelHeader">
          <span>{t("core.ui.pipelines.panels.motion_gate.threshold")}</span>
          {onOpenTelemetryField ? (
            <button
              className="iconButton pipelinesTelemetryFieldButton"
              type="button"
              title={t("core.ui.pipelines.telemetry.field.open_histogram")}
              onClick={() =>
                onOpenTelemetryField({
                  stepUid,
                  nodeId,
                  operatorId: "camera.motion_sample_bg",
                  configKey: "threshold",
                  metricId: "motion.score",
                  label: t("core.ui.pipelines.panels.motion_gate.threshold"),
                  value: threshold,
                })
              }
            >
              <i className="fa-solid fa-chart-column" aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={1}
          step={0.001}
          value={threshold}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.01;
            onUpdateConfig((prev) => ({
              ...prev,
              threshold: normalized,
              threshold_low:
                Number((prev as any).threshold_low ?? thresholdLow) > normalized
                  ? normalized
                  : (prev as any).threshold_low ?? thresholdLow,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_sample_bg.backend")}</span>
        <select
          className="pipelinesSelect"
          value={backend}
          onChange={(event) => {
            const next = String(event.target.value || "pbas_lite").trim().toLowerCase() === "vibe_core" ? "vibe_core" : "pbas_lite";
            onUpdateConfig((prev) => ({ ...prev, backend: next }));
          }}
        >
          <option value="pbas_lite">{t("core.ui.pipelines.panels.motion_sample_bg.backend.pbas_lite")}</option>
          <option value="vibe_core">{t("core.ui.pipelines.panels.motion_sample_bg.backend.vibe_core")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_sample_bg.feature_mode")}</span>
        <select
          className="pipelinesSelect"
          value={featureMode}
          onChange={(event) => {
            const nextRaw = String(event.target.value || "gray_gradient").trim().toLowerCase();
            const next = nextRaw === "gray" || nextRaw === "ycrcb_gradient" ? nextRaw : "gray_gradient";
            onUpdateConfig((prev) => ({ ...prev, feature_mode: next }));
          }}
        >
          <option value="gray_gradient">{t("core.ui.pipelines.panels.motion_sample_bg.feature_mode.gray_gradient")}</option>
          <option value="gray">{t("core.ui.pipelines.panels.motion_sample_bg.feature_mode.gray")}</option>
          <option value="ycrcb_gradient">{t("core.ui.pipelines.panels.motion_sample_bg.feature_mode.ycrcb_gradient")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.hold_seconds")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={0}
          max={120}
          step={0.05}
          value={holdSeconds}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(120, nextValue)) : 2.5;
            onUpdateConfig((prev) => ({ ...prev, hold_seconds: normalized }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.hold_seconds_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.activation_frames")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={1}
          max={100}
          step={1}
          value={activationFrames}
          onChange={(nextValue) => {
            const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(100, Math.round(nextValue))) : 1;
            onUpdateConfig((prev) => ({ ...prev, activation_frames: normalized }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.activation_frames_hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_sample_bg.filter_when_inactive")}</span>
        <input
          type="checkbox"
          checked={filterWhenInactive}
          onChange={(event) => onUpdateConfig((prev) => ({ ...prev, filter_when_inactive: event.target.checked }))}
        />
      </label>

      <div className="sectionDivider" />
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.mask.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.mask.enabled")}</span>
        <input
          type="checkbox"
          checked={maskEnabled}
          onChange={(event) => onUpdateConfig((prev) => ({ ...prev, mask_enabled: event.target.checked }))}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.motion_gate.mask.mode")}</span>
        <select
          className="pipelinesSelect"
          value={maskMode}
          onChange={(event) => {
            const next = parseMotionMaskMode(event.target.value);
            onUpdateConfig((prev) => ({ ...prev, mask_mode: next }));
          }}
        >
          <option value="include">{t("core.ui.pipelines.panels.motion_gate.mask.mode.include")}</option>
          <option value="exclude">{t("core.ui.pipelines.panels.motion_gate.mask.mode.exclude")}</option>
        </select>
      </label>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!drawEligibility.enabled}
          onClick={() => setIsDrawOpen(true)}
        >
          {t("core.ui.pipelines.panels.motion_gate.mask.draw")}
        </button>

        <button
          className="chipButton"
          type="button"
          disabled={maskStrokes.length === 0}
          onClick={() => onUpdateConfig((prev) => ({ ...prev, mask_strokes: [] }))}
        >
          {t("core.ui.pipelines.panels.motion_gate.mask.clear")}
        </button>
      </div>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.motion_gate.mask.strokes_count", { count: maskStrokes.length })}</div>

      {!drawEligibility.enabled ? (
        <div className="pipelinesStepHint" style={{ textAlign: "right" }}>
          {imageDrawUnavailableMessage(t, drawEligibility.reason)}
        </div>
      ) : null}

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.input_with_fallback")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={inputWithFallback}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, input_with_fallback: event.target.value }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.fallback_to_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_gate.mask.brush_diameter")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.002}
              max={0.25}
              step={0.001}
              value={maskBrushDiameter01}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0.002, Math.min(0.25, nextValue)) : 0.1;
                onUpdateConfig((prev) => ({ ...prev, mask_brush_diameter01: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.threshold_low")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={threshold}
              step={0.001}
              value={thresholdLow}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(threshold, nextValue)) : thresholdLow;
                onUpdateConfig((prev) => ({ ...prev, threshold_low: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.downscale_height")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={2160}
              step={1}
              value={downscaleHeight}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(2160, Math.round(nextValue))) : 180;
                onUpdateConfig((prev) => ({ ...prev, downscale_height: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.sample_count")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={4}
              max={128}
              step={1}
              value={sampleCount}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(4, Math.min(128, Math.round(nextValue))) : 20;
                onUpdateConfig((prev) => ({
                  ...prev,
                  sample_count: normalized,
                  min_matches:
                    Number((prev as any).min_matches ?? minMatches) > normalized
                      ? normalized
                      : (prev as any).min_matches ?? minMatches,
                }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.min_matches")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={sampleCount}
              step={1}
              value={minMatches}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(sampleCount, Math.round(nextValue))) : minMatches;
                onUpdateConfig((prev) => ({ ...prev, min_matches: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.r_lower")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={255}
              step={0.1}
              value={rLower}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(255, nextValue)) : 18;
                onUpdateConfig((prev) => ({ ...prev, r_lower: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.r_scale")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.5}
              max={64}
              step={0.1}
              value={rScale}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0.5, Math.min(64, nextValue)) : 5;
                onUpdateConfig((prev) => ({ ...prev, r_scale: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.r_incdec")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.001}
              max={10}
              step={0.001}
              value={rIncdec}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0.001, Math.min(10, nextValue)) : 0.05;
                onUpdateConfig((prev) => ({ ...prev, r_incdec: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.t_lower")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={512}
              step={0.1}
              value={tLower}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(512, nextValue)) : 2;
                onUpdateConfig((prev) => ({
                  ...prev,
                  t_lower: normalized,
                  t_upper:
                    Number((prev as any).t_upper ?? tUpper) < normalized
                      ? normalized
                      : (prev as any).t_upper ?? tUpper,
                }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.t_upper")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={tLower}
              max={4096}
              step={1}
              value={tUpper}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(tLower, Math.min(4096, nextValue)) : tUpper;
                onUpdateConfig((prev) => ({ ...prev, t_upper: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.t_inc")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.01}
              max={128}
              step={0.01}
              value={tInc}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0.01, Math.min(128, nextValue)) : 1;
                onUpdateConfig((prev) => ({ ...prev, t_inc: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.t_dec")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0.001}
              max={10}
              step={0.001}
              value={tDec}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0.001, Math.min(10, nextValue)) : 0.05;
                onUpdateConfig((prev) => ({ ...prev, t_dec: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.enable_neighbor_propagation")}</span>
            <input
              type="checkbox"
              checked={enableNeighborPropagation}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, enable_neighbor_propagation: event.target.checked }))}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.warmup_frames")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={600}
              step={1}
              value={warmupFrames}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(600, Math.round(nextValue))) : 30;
                onUpdateConfig((prev) => ({ ...prev, warmup_frames: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.scene_reset_score")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1}
              step={0.01}
              value={sceneResetScore}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.6;
                onUpdateConfig((prev) => ({ ...prev, scene_reset_score: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.random_seed")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={2147483647}
              step={1}
              value={randomSeed}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(2147483647, Math.round(nextValue))) : 0;
                onUpdateConfig((prev) => ({ ...prev, random_seed: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.morphology_open_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={63}
              step={1}
              value={morphologyOpenPx}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(63, Math.round(nextValue))) : 2;
                onUpdateConfig((prev) => ({ ...prev, morphology_open_px: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.morphology_close_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={63}
              step={1}
              value={morphologyClosePx}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(63, Math.round(nextValue))) : 4;
                onUpdateConfig((prev) => ({ ...prev, morphology_close_px: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.min_blob_area_ratio")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={1}
              step={0.0001}
              value={minBlobAreaRatio}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(1, nextValue)) : 0.0005;
                onUpdateConfig((prev) => ({ ...prev, min_blob_area_ratio: normalized }));
              }}
            />
          </label>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.motion_sample_bg.max_blobs")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={64}
              step={1}
              value={maxBlobs}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(64, Math.round(nextValue))) : 8;
                onUpdateConfig((prev) => ({ ...prev, max_blobs: normalized }));
              }}
            />
          </label>
        </>
      ) : null}

      <MotionMaskDrawModal
        open={isDrawOpen}
        onClose={() => setIsDrawOpen(false)}
        snapshotSource={drawEligibility.snapshotSource}
        mode={maskMode}
        brushDiameter01={maskBrushDiameter01}
        strokes={maskStrokes}
        onApply={(next) =>
          onUpdateConfig((prev) => ({
            ...prev,
            mask_enabled: true,
            mask_mode: next.mode,
            mask_strokes: next.strokes,
          }))
        }
      />
    </div>
  );
}

type ImageCropProps = {
  config: Record<string, unknown>;
  pipelineName: string | null;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  index: number;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ImageCropConfigCard({ config, pipelineName, steps, operatorsById, index, showAdvanced, onUpdateConfig }: ImageCropProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const unitsRaw = String((config as any).units ?? "percent").trim().toLowerCase();
  const units = unitsRaw === "pixels" ? "pixels" : "percent";
  const left = Number((config as any).left ?? 0);
  const top = Number((config as any).top ?? 0);
  const right = Number((config as any).right ?? 100);
  const bottom = Number((config as any).bottom ?? 100);
  const outputArtifactName = String((config as any).output_artifact_name ?? "frame").trim() || "frame";
  const minCropSizePx = Number((config as any).min_crop_size_px ?? 8);
  const setStreamFrame = (config as any).set_stream_frame ?? (config as any).set_payload_frame ?? true;

  const percentMax = 100;
  const clampPercent = (value: number) => Math.max(0, Math.min(percentMax, value));

  const nodeId = String(steps[index]?.nodeId ?? "").trim();
  const drawEligibility = React.useMemo(
    () => resolveImageDrawEligibility(steps, index, pipelineName, nodeId, operatorsById),
    [steps, index, pipelineName, nodeId, operatorsById],
  );
  const [isDrawOpen, setIsDrawOpen] = React.useState(false);

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_crop.hint")}</div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!drawEligibility.enabled}
          onClick={() => setIsDrawOpen(true)}
        >
          {t("core.ui.pipelines.panels.image_draw.button")}
        </button>
        {!drawEligibility.enabled ? (
          <div className="pipelinesStepHint" style={{ textAlign: "right" }}>
            {imageDrawUnavailableMessage(t, drawEligibility.reason)}
          </div>
        ) : null}
      </div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_crop.units")}</span>
        <select
          className="pipelinesSelect"
          value={units}
          onChange={(event) => {
            const next = String(event.target.value || "percent").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, units: next === "pixels" ? "pixels" : "percent" }));
          }}
        >
          <option value="percent">{t("core.ui.pipelines.panels.image_crop.units.percent")}</option>
          <option value="pixels">{t("core.ui.pipelines.panels.image_crop.units.pixels")}</option>
        </select>
      </label>

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_crop.left")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(left) ? (units === "percent" ? clampPercent(left) : Math.max(0, left)) : 0}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, left: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_crop.top")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(top) ? (units === "percent" ? clampPercent(top) : Math.max(0, top)) : 0}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, top: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_crop.right")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(right) ? (units === "percent" ? clampPercent(right) : Math.max(0, right)) : 100}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, right: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_crop.bottom")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(bottom) ? (units === "percent" ? clampPercent(bottom) : Math.max(0, bottom)) : 100}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, bottom: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>
      </div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <div className="pipelinesStepHint">
          {t("core.ui.pipelines.panels.image_crop.rectangle_hint")}
        </div>
        <button
          className="chipButton"
          type="button"
          onClick={() => onUpdateConfig((prev) => ({ ...prev, left: 0, top: 0, right: 100, bottom: 100, units: "percent" }))}
        >
          {t("core.ui.pipelines.panels.image_crop.reset")}
        </button>
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_crop.output_artifact_name")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_crop.min_crop_size_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={4096}
              step={1}
              value={Number.isFinite(minCropSizePx) ? Math.max(1, Math.min(4096, minCropSizePx)) : 8}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(4096, nextValue)) : 8;
                onUpdateConfig((prev) => ({ ...prev, min_crop_size_px: normalized }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_crop.use_cropped_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(setStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, set_stream_frame: event.target.checked }))}
            />
          </label>
        </>
      ) : null}

      <CropRectangleDrawModal
        open={isDrawOpen}
        onClose={() => setIsDrawOpen(false)}
        snapshotSource={drawEligibility.snapshotSource}
        units={units}
        values={{ left, top, right, bottom }}
        onChange={(nextValues) =>
          onUpdateConfig((prev) => ({
            ...prev,
            left: nextValues.left,
            top: nextValues.top,
            right: nextValues.right,
            bottom: nextValues.bottom,
          }))
        }
      />
    </div>
  );
}

type ImagePerspectiveCropProps = {
  config: Record<string, unknown>;
  pipelineName: string | null;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  index: number;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

function readPerspectivePoints(config: Record<string, unknown>, units: "percent" | "pixels"): number[][] {
  const raw = (config as any).points;
  const defaultPoints = [
    [0, 0],
    [100, 0],
    [100, 100],
    [0, 100],
  ];

  const base: number[][] = Array.isArray(raw) ? raw : [];
  const out: number[][] = [];
  for (let i = 0; i < 4; i += 1) {
    const fallback = defaultPoints[i] ?? [0, 0];
    const item = base[i];
    if (Array.isArray(item) && item.length >= 2) {
      const x = Number(item[0]);
      const y = Number(item[1]);
      out.push([Number.isFinite(x) ? x : fallback[0], Number.isFinite(y) ? y : fallback[1]]);
      continue;
    }
    if (item && typeof item === "object") {
      const x = Number((item as any).x);
      const y = Number((item as any).y);
      out.push([Number.isFinite(x) ? x : fallback[0], Number.isFinite(y) ? y : fallback[1]]);
      continue;
    }
    out.push(fallback);
  }

  const clampPercent = (value: number) => Math.max(0, Math.min(100, value));
  return out.map(([x, y]) => [
    units === "percent" ? clampPercent(x) : Math.max(0, x),
    units === "percent" ? clampPercent(y) : Math.max(0, y),
  ]);
}

export function ImagePerspectiveCropConfigCard({
  config,
  pipelineName,
  steps,
  operatorsById,
  index,
  showAdvanced,
  onUpdateConfig,
}: ImagePerspectiveCropProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const unitsRaw = String((config as any).units ?? "percent").trim().toLowerCase();
  const units = unitsRaw === "pixels" ? "pixels" : "percent";

  const points = readPerspectivePoints(config, units);
  const nodeId = String(steps[index]?.nodeId ?? "").trim();
  const drawEligibility = React.useMemo(
    () => resolveImageDrawEligibility(steps, index, pipelineName, nodeId, operatorsById),
    [steps, index, pipelineName, nodeId, operatorsById],
  );
  const [isDrawOpen, setIsDrawOpen] = React.useState(false);

  const outputRatioRaw = String((config as any).output_ratio_preset ?? "auto").trim().toLowerCase();
  const outputRatio =
    outputRatioRaw === "1:1" || outputRatioRaw === "4:3" || outputRatioRaw === "16:9" || outputRatioRaw === "3:4" || outputRatioRaw === "9:16"
      ? outputRatioRaw
      : "auto";

  const outputArtifactName = String((config as any).output_artifact_name ?? "frame").trim() || "frame";
  const minOutputEdgePx = Number((config as any).min_output_edge_px ?? 8);
  const maxOutputEdgePx = Number((config as any).max_output_edge_px ?? 0);
  const setStreamFrame = (config as any).set_stream_frame ?? (config as any).set_payload_frame ?? true;

  const interpolationRaw = String((config as any).interpolation ?? "linear").trim().toLowerCase();
  const interpolation =
    interpolationRaw === "nearest" || interpolationRaw === "cubic" || interpolationRaw === "area" ? interpolationRaw : "linear";
  const borderModeRaw = String((config as any).border_mode ?? "constant").trim().toLowerCase();
  const borderMode = borderModeRaw === "replicate" ? "replicate" : "constant";
  const borderValueRaw = Number((config as any).border_value ?? 0);
  const borderValue = Number.isFinite(borderValueRaw) ? Math.max(0, Math.min(255, borderValueRaw)) : 0;

  const updatePoint = (index: number, axis: 0 | 1, value: number) => {
    onUpdateConfig((prev) => {
      const prevUnitsRaw = String((prev as any).units ?? "percent").trim().toLowerCase();
      const prevUnits = prevUnitsRaw === "pixels" ? "pixels" : "percent";
      const prevPoints = readPerspectivePoints(prev, prevUnits);

      const normalizedValue = Number.isFinite(value)
        ? prevUnits === "percent"
          ? Math.max(0, Math.min(100, value))
          : Math.max(0, value)
        : 0;

      const nextPoints = prevPoints.map((p) => p.slice());
      if (!nextPoints[index]) nextPoints[index] = [0, 0];
      nextPoints[index]![axis] = normalizedValue;
      return { ...prev, points: nextPoints };
    });
  };

  const step = units === "percent" ? 0.5 : 1;
  const max = units === "percent" ? 100 : undefined;

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_perspective_crop.hint")}</div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!drawEligibility.enabled}
          onClick={() => setIsDrawOpen(true)}
        >
          {t("core.ui.pipelines.panels.image_draw.button")}
        </button>
        {!drawEligibility.enabled ? (
          <div className="pipelinesStepHint" style={{ textAlign: "right" }}>
            {imageDrawUnavailableMessage(t, drawEligibility.reason)}
          </div>
        ) : null}
      </div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_perspective_crop.units")}</span>
        <select
          className="pipelinesSelect"
          value={units}
          onChange={(event) => {
            const next = String(event.target.value || "percent").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, units: next === "pixels" ? "pixels" : "percent" }));
          }}
        >
          <option value="percent">{t("core.ui.pipelines.panels.image_perspective_crop.units.percent")}</option>
          <option value="pixels">{t("core.ui.pipelines.panels.image_perspective_crop.units.pixels")}</option>
        </select>
      </label>

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        {points.map(([x, y], idx) => (
          <React.Fragment key={`point-${idx}`}>
            <label className="pipelinesLabel pipelinesScalarLabel">
              <span>{t(`core.ui.pipelines.panels.image_perspective_crop.p${idx + 1}_x`)}</span>
              <PipelinesNumberInput
                className="pipelinesInput"
                min={0}
                max={max}
                step={step}
                value={Number.isFinite(x) ? x : 0}
                onChange={(nextValue) => updatePoint(idx, 0, nextValue)}
              />
            </label>
            <label className="pipelinesLabel pipelinesScalarLabel">
              <span>{t(`core.ui.pipelines.panels.image_perspective_crop.p${idx + 1}_y`)}</span>
              <PipelinesNumberInput
                className="pipelinesInput"
                min={0}
                max={max}
                step={step}
                value={Number.isFinite(y) ? y : 0}
                onChange={(nextValue) => updatePoint(idx, 1, nextValue)}
              />
            </label>
          </React.Fragment>
        ))}
      </div>

      <label className="pipelinesLabel" style={{ marginTop: 10 }}>
        <span>{t("core.ui.pipelines.panels.image_perspective_crop.output_ratio")}</span>
        <select
          className="pipelinesSelect"
          value={outputRatio}
          onChange={(event) => {
            const next = String(event.target.value || "auto").trim().toLowerCase() || "auto";
            onUpdateConfig((prev) => ({ ...prev, output_ratio_preset: next }));
          }}
        >
          <option value="auto">{t("core.ui.pipelines.panels.image_perspective_crop.output_ratio.auto")}</option>
          <option value="1:1">1:1</option>
          <option value="4:3">4:3</option>
          <option value="16:9">16:9</option>
          <option value="3:4">3:4</option>
          <option value="9:16">9:16</option>
        </select>
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_perspective_crop.output_ratio_hint")}</div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_perspective_crop.points_hint")}</div>
        <button
          className="chipButton"
          type="button"
          onClick={() =>
            onUpdateConfig((prev) => ({
              ...prev,
              units: "percent",
              points: [
                [0, 0],
                [100, 0],
                [100, 100],
                [0, 100],
              ],
              output_ratio_preset: "auto",
            }))
          }
        >
          {t("core.ui.pipelines.panels.image_perspective_crop.reset")}
        </button>
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.output_artifact_name")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.min_output_edge_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={4096}
              step={1}
              value={Number.isFinite(minOutputEdgePx) ? Math.max(1, Math.min(4096, minOutputEdgePx)) : 8}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(4096, nextValue)) : 8;
                onUpdateConfig((prev) => ({ ...prev, min_output_edge_px: normalized }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.max_output_edge_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={16384}
              step={8}
              value={Number.isFinite(maxOutputEdgePx) ? Math.max(0, Math.min(16384, maxOutputEdgePx)) : 0}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(16384, nextValue)) : 0;
                onUpdateConfig((prev) => ({ ...prev, max_output_edge_px: normalized }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.interpolation")}</span>
            <select
              className="pipelinesSelect"
              value={interpolation}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, interpolation: event.target.value }))}
            >
              <option value="linear">{t("core.ui.pipelines.panels.image_perspective_crop.interpolation.linear")}</option>
              <option value="cubic">{t("core.ui.pipelines.panels.image_perspective_crop.interpolation.cubic")}</option>
              <option value="area">{t("core.ui.pipelines.panels.image_perspective_crop.interpolation.area")}</option>
              <option value="nearest">{t("core.ui.pipelines.panels.image_perspective_crop.interpolation.nearest")}</option>
            </select>
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.border_mode")}</span>
            <select
              className="pipelinesSelect"
              value={borderMode}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, border_mode: event.target.value }))}
            >
              <option value="constant">{t("core.ui.pipelines.panels.image_perspective_crop.border_mode.constant")}</option>
              <option value="replicate">{t("core.ui.pipelines.panels.image_perspective_crop.border_mode.replicate")}</option>
            </select>
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.border_value")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={0}
              max={255}
              step={1}
              value={borderValue}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(0, Math.min(255, nextValue)) : 0;
                onUpdateConfig((prev) => ({ ...prev, border_value: normalized }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_perspective_crop.use_warped_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(setStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, set_stream_frame: event.target.checked }))}
            />
          </label>
        </>
      ) : null}

      <PerspectiveCropDrawModal
        open={isDrawOpen}
        onClose={() => setIsDrawOpen(false)}
        snapshotSource={drawEligibility.snapshotSource}
        units={units}
        points={points}
        onChange={(nextPoints) => onUpdateConfig((prev) => ({ ...prev, points: nextPoints }))}
      />
    </div>
  );
}

type ImageAdjustProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

type PrivacyEffect = "black" | "white" | "gray" | "blur_medium" | "blur_high";

function parsePrivacyEffect(value: unknown): PrivacyEffect {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (normalized === "black" || normalized === "white" || normalized === "gray" || normalized === "blur_high") return normalized;
  return "blur_medium";
}

type ImagePrivacyProps = {
  config: Record<string, unknown>;
  pipelineName: string | null;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  index: number;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ImagePrivacyConfigCard({
  config,
  pipelineName,
  steps,
  operatorsById,
  index,
  showAdvanced,
  onUpdateConfig,
}: ImagePrivacyProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const inputArtifactNamesRaw = (config as any).input_artifact_names;
  const inputArtifactNames = Array.isArray(inputArtifactNamesRaw)
    ? inputArtifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : ["treated", "original"];
  const artifactSuggestions = buildArtifactSuggestions(t);
  const selectedInputOptions = inputArtifactNames.map(
    (value) => artifactSuggestions.find((opt) => opt.value === value) ?? { value, label: value },
  );

  const unitsRaw = String((config as any).units ?? "percent").trim().toLowerCase();
  const units = unitsRaw === "pixels" ? "pixels" : "percent";
  const left = Number((config as any).left ?? 0);
  const top = Number((config as any).top ?? 0);
  const right = Number((config as any).right ?? 0);
  const bottom = Number((config as any).bottom ?? 0);
  const effect = parsePrivacyEffect((config as any).effect);
  const outputArtifactName = String((config as any).output_artifact_name ?? "frame").trim() || "frame";
  const minRegionSizePx = Number((config as any).min_region_size_px ?? 8);
  const setStreamFrame = (config as any).set_stream_frame ?? (config as any).set_payload_frame ?? true;
  const preserveAlpha = (config as any).preserve_alpha !== false;
  const fallbackToStreamFrame = (config as any).fallback_to_stream_frame ?? (config as any).fallback_to_payload_frame ?? true;

  const clampPercent = (value: number, fallback: number) => {
    if (!Number.isFinite(value)) return fallback;
    return Math.max(0, Math.min(100, value));
  };
  const clampNonNegative = (value: number, fallback: number) => {
    if (!Number.isFinite(value)) return fallback;
    return Math.max(0, value);
  };
  const regionDefined = Number.isFinite(left) && Number.isFinite(top) && Number.isFinite(right) && Number.isFinite(bottom) && right > left && bottom > top;

  const nodeId = String(steps[index]?.nodeId ?? "").trim();
  const drawEligibility = React.useMemo(
    () => resolveImageDrawEligibility(steps, index, pipelineName, nodeId, operatorsById),
    [steps, index, pipelineName, nodeId, operatorsById],
  );
  const [isDrawOpen, setIsDrawOpen] = React.useState(false);

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_privacy.hint")}</div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between", alignItems: "center" }}>
        <button
          className="chipButton"
          type="button"
          disabled={!drawEligibility.enabled}
          onClick={() => setIsDrawOpen(true)}
        >
          {t("core.ui.pipelines.panels.image_draw.button")}
        </button>
        <button
          className="chipButton"
          type="button"
          disabled={!regionDefined}
          onClick={() => onUpdateConfig((prev) => ({ ...prev, left: 0, top: 0, right: 0, bottom: 0, units: "percent" }))}
        >
          {t("core.ui.pipelines.panels.image_privacy.clear_region")}
        </button>
      </div>

      {!drawEligibility.enabled ? (
        <div className="pipelinesStepHint" style={{ textAlign: "right", marginTop: 8 }}>
          {imageDrawUnavailableMessage(t, drawEligibility.reason)}
        </div>
      ) : null}

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_privacy.effect")}</span>
        <select
          className="pipelinesSelect"
          value={effect}
          onChange={(event) => {
            const next = parsePrivacyEffect(event.target.value);
            onUpdateConfig((prev) => ({ ...prev, effect: next }));
          }}
        >
          <option value="black">{t("core.ui.pipelines.panels.image_privacy.effect.black")}</option>
          <option value="white">{t("core.ui.pipelines.panels.image_privacy.effect.white")}</option>
          <option value="gray">{t("core.ui.pipelines.panels.image_privacy.effect.gray")}</option>
          <option value="blur_medium">{t("core.ui.pipelines.panels.image_privacy.effect.blur_medium")}</option>
          <option value="blur_high">{t("core.ui.pipelines.panels.image_privacy.effect.blur_high")}</option>
        </select>
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_privacy.units")}</span>
        <select
          className="pipelinesSelect"
          value={units}
          onChange={(event) => {
            const next = String(event.target.value || "percent").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, units: next === "pixels" ? "pixels" : "percent" }));
          }}
        >
          <option value="percent">{t("core.ui.pipelines.panels.image_privacy.units.percent")}</option>
          <option value="pixels">{t("core.ui.pipelines.panels.image_privacy.units.pixels")}</option>
        </select>
      </label>

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_privacy.left")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? 100 : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={units === "percent" ? clampPercent(left, 0) : clampNonNegative(left, 0)}
            onChange={(nextValue) =>
              onUpdateConfig((prev) => ({
                ...prev,
                left: units === "percent" ? clampPercent(nextValue, 0) : clampNonNegative(nextValue, 0),
              }))
            }
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_privacy.top")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? 100 : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={units === "percent" ? clampPercent(top, 0) : clampNonNegative(top, 0)}
            onChange={(nextValue) =>
              onUpdateConfig((prev) => ({
                ...prev,
                top: units === "percent" ? clampPercent(nextValue, 0) : clampNonNegative(nextValue, 0),
              }))
            }
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_privacy.right")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? 100 : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={units === "percent" ? clampPercent(right, 0) : clampNonNegative(right, 0)}
            onChange={(nextValue) =>
              onUpdateConfig((prev) => ({
                ...prev,
                right: units === "percent" ? clampPercent(nextValue, 0) : clampNonNegative(nextValue, 0),
              }))
            }
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_privacy.bottom")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={units === "percent" ? 100 : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={units === "percent" ? clampPercent(bottom, 0) : clampNonNegative(bottom, 0)}
            onChange={(nextValue) =>
              onUpdateConfig((prev) => ({
                ...prev,
                bottom: units === "percent" ? clampPercent(nextValue, 0) : clampNonNegative(nextValue, 0),
              }))
            }
          />
        </label>
      </div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <div className="pipelinesStepHint">
          {regionDefined
            ? t("core.ui.pipelines.panels.image_privacy.region_ready")
            : t("core.ui.pipelines.panels.image_privacy.region_missing")}
        </div>
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_privacy.region_hint")}</div>
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_privacy.input_artifacts")}</span>
            <CreatableSelect<SelectOption, true>
              isMulti
              styles={pipelinesReactSelectStyles}
              options={artifactSuggestions}
              value={selectedInputOptions}
              placeholder={t("core.ui.pipelines.panels.image_privacy.input_artifacts_placeholder")}
              onChange={(value: MultiValue<SelectOption>) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  input_artifact_names: value.map((item) => item.value),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_privacy.input_artifacts_hint")}</div>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_privacy.output_artifact_name")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_privacy.min_region_size_px")}</span>
            <PipelinesNumberInput
              className="pipelinesInput"
              min={1}
              max={4096}
              step={1}
              value={Number.isFinite(minRegionSizePx) ? Math.max(1, Math.min(4096, minRegionSizePx)) : 8}
              onChange={(nextValue) => {
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(4096, nextValue)) : 8;
                onUpdateConfig((prev) => ({ ...prev, min_region_size_px: normalized }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_privacy.apply_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(setStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, set_stream_frame: event.target.checked }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_privacy.fallback_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_privacy.preserve_alpha")}</span>
            <input
              type="checkbox"
              checked={preserveAlpha}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, preserve_alpha: event.target.checked }))}
            />
          </label>
        </>
      ) : null}

      <PrivacyRegionDrawModal
        open={isDrawOpen}
        onClose={() => setIsDrawOpen(false)}
        snapshotSource={drawEligibility.snapshotSource}
        units={units}
        values={{ left, top, right, bottom }}
        effect={effect}
        onApply={(next) =>
          onUpdateConfig((prev) => ({
            ...prev,
            left: next.values.left,
            top: next.values.top,
            right: next.values.right,
            bottom: next.values.bottom,
            effect: next.effect,
          }))
        }
      />
    </div>
  );
}

export function ImageAdjustConfigCard({ config, showAdvanced, onUpdateConfig }: ImageAdjustProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const inputArtifactNamesRaw = (config as any).input_artifact_names;
  const inputArtifactNames = Array.isArray(inputArtifactNamesRaw)
    ? inputArtifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : ["segmented", "treated", "original"];
  const artifactSuggestions = buildArtifactSuggestions(t);
  const selectedInputOptions = inputArtifactNames.map(
    (value) => artifactSuggestions.find((opt) => opt.value === value) ?? { value, label: value },
  );

  const saturation = Number((config as any).saturation ?? 1.0);
  const brightness = Number((config as any).brightness ?? 0.0);
  const contrast = Number((config as any).contrast ?? 1.0);
  const gamma = Number((config as any).gamma ?? 1.0);

  const outputArtifactName = String((config as any).output_artifact_name ?? "frame").trim() || "frame";
  const setStreamFrame = (config as any).set_stream_frame ?? (config as any).set_payload_frame ?? true;
  const preserveAlpha = (config as any).preserve_alpha !== false;
  const fallbackToStreamFrame = (config as any).fallback_to_stream_frame ?? (config as any).fallback_to_payload_frame ?? true;

  const clamp = (value: number, min: number, max: number, fallback: number) => {
    if (!Number.isFinite(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  };

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_adjust.input_artifacts")}</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={artifactSuggestions}
          value={selectedInputOptions}
          placeholder={t("core.ui.pipelines.panels.image_adjust.input_artifacts_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              input_artifact_names: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_adjust.input_artifacts_hint")}</div>

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_adjust.saturation")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={3}
            step={0.05}
            value={clamp(saturation, 0, 3, 1)}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, saturation: clamp(nextValue, 0, 3, 1) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_adjust.brightness")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={-1}
            max={1}
            step={0.02}
            value={clamp(brightness, -1, 1, 0)}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, brightness: clamp(nextValue, -1, 1, 0) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_adjust.contrast")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={3}
            step={0.05}
            value={clamp(contrast, 0, 3, 1)}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, contrast: clamp(nextValue, 0, 3, 1) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.image_adjust.gamma")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0.1}
            max={5}
            step={0.05}
            value={clamp(gamma, 0.1, 5, 1)}
            onChange={(nextValue) => {
              onUpdateConfig((prev) => ({ ...prev, gamma: clamp(nextValue, 0.1, 5, 1) }));
            }}
          />
        </label>
      </div>

      <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
        {t("core.ui.pipelines.panels.image_adjust.brightness_hint")}
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_adjust.output_artifact_name")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_adjust.apply_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(setStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, set_stream_frame: event.target.checked }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_adjust.fallback_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) =>
                onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))
              }
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.image_adjust.preserve_alpha")}</span>
            <input
              type="checkbox"
              checked={preserveAlpha}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, preserve_alpha: event.target.checked }))}
            />
          </label>
        </>
      ) : null}
    </div>
  );
}

type ImageResizeProps = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function ImageResizeConfigCard({ config, onUpdateConfig }: ImageResizeProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const maxEdgePx = Number((config as any).max_edge_px ?? 1280);
  const allowUpscale = Boolean((config as any).allow_upscale ?? false);
  const artifactNamesRaw = (config as any).artifact_names;
  const artifactNames = Array.isArray(artifactNamesRaw)
    ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : ["segmented", "treated"];
  const artifactSuggestions = buildArtifactSuggestions(t);
  const selectedOptions = artifactNames.map((value) => artifactSuggestions.find((opt) => opt.value === value) ?? { value, label: value });

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_resize.artifacts")}</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={artifactSuggestions}
          value={selectedOptions}
          placeholder={t("core.ui.pipelines.panels.image_resize.artifacts_placeholder")}
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              artifact_names: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_resize.hint")}</div>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_resize.max_edge_px")}</span>
        <PipelinesNumberInput
          className="pipelinesInput"
          min={16}
          max={16384}
          step={1}
          value={Number.isFinite(maxEdgePx) ? maxEdgePx : 1280}
          onChange={(nextValue) => {
            onUpdateConfig((prev) => ({
              ...prev,
              max_edge_px: Number.isFinite(nextValue) ? Math.max(16, Math.min(16384, nextValue)) : 1280,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.image_resize.allow_upscale")}</span>
        <input type="checkbox" checked={allowUpscale} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, allow_upscale: event.target.checked }))} />
      </label>
    </div>
  );
}

type ObjectSegmentationProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

function normalizeStringArray(value: unknown, fallback: string[]): string[] {
  if (!Array.isArray(value)) return fallback;
  const items = value.map((item) => String(item || "").trim()).filter((item) => item.length > 0);
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    const key = item.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(item);
  }
  return unique.length > 0 ? unique : fallback;
}

export function ObjectSegmentationConfigCard({
  config,
  showAdvanced,
  onUpdateConfig,
}: ObjectSegmentationProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const fallbackToStreamFrame = (config as any).fallback_to_stream_frame ?? (config as any).fallback_to_payload_frame ?? true;
  const paddingRatio = Number((config as any).padding_ratio ?? 0.08);
  const minCropSizePx = Number((config as any).min_crop_size_px ?? 8);
  const outputArtifactName = String((config as any).output_artifact_name ?? "segmented").trim() || "segmented";
  const bboxField = String((config as any).bbox_field ?? "object_bbox01").trim() || "object_bbox01";

  const inputNames = normalizeStringArray((config as any).input_artifact_names, ["original", "treated"]);
  const preferOriginal = String(inputNames[0] || "").trim().toLowerCase() !== "treated";

  const artifactSuggestions = buildArtifactSuggestions(t);
  const selectedInputOptions = inputNames.map((value) => artifactSuggestions.find((opt) => opt.value === value) ?? { value, label: value });

  const clamp = (value: number, min: number, max: number, fallback: number) => {
    if (!Number.isFinite(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  };

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("core.ui.pipelines.panels.object_segmentation.quality")}</span>
        <select
          className="pipelinesSelect"
          value={preferOriginal ? "best" : "fast"}
          onChange={(event) => {
            const next = String(event.target.value || "best").trim().toLowerCase();
            onUpdateConfig((prev) => ({
              ...prev,
              input_artifact_names: next === "fast" ? ["treated", "original"] : ["original", "treated"],
            }));
          }}
        >
          <option value="best">{t("core.ui.pipelines.panels.object_segmentation.quality.best")}</option>
          <option value="fast">{t("core.ui.pipelines.panels.object_segmentation.quality.fast")}</option>
        </select>
      </label>
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.object_segmentation.quality_hint")}</div>

      {showAdvanced ? (
        <>
          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.object_segmentation.input_images")}</span>
            <CreatableSelect<SelectOption, true>
              isMulti
              styles={pipelinesReactSelectStyles}
              options={artifactSuggestions}
              value={selectedInputOptions}
              placeholder={t("core.ui.pipelines.panels.object_segmentation.input_images_placeholder")}
              onChange={(value: MultiValue<SelectOption>) => {
                onUpdateConfig((prev) => ({
                  ...prev,
                  input_artifact_names: value.map((item) => item.value),
                }));
              }}
            />
          </label>
          <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.object_segmentation.input_images_hint")}</div>
        </>
      ) : null}

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.object_segmentation.padding")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={0.5}
            step={0.01}
            value={clamp(paddingRatio, 0, 0.5, 0.08)}
            onChange={(nextValue) => onUpdateConfig((prev) => ({ ...prev, padding_ratio: clamp(nextValue, 0, 0.5, 0.08) }))}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>{t("core.ui.pipelines.panels.object_segmentation.min_crop_size_px")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={1}
            max={4096}
            step={1}
            value={clamp(minCropSizePx, 1, 4096, 8)}
            onChange={(nextValue) => onUpdateConfig((prev) => ({ ...prev, min_crop_size_px: clamp(nextValue, 1, 4096, 8) }))}
          />
        </label>
      </div>
      <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
        {t("core.ui.pipelines.panels.object_segmentation.hint")}
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.object_segmentation.output_artifact_name")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.object_segmentation.bbox_field")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={bboxField}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, bbox_field: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.pipelines.panels.object_segmentation.fallback_stream_frame")}</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))}
            />
          </label>
        </>
      ) : null}
    </div>
  );
}
