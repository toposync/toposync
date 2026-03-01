import React from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import type { CameraContextsResponse } from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";

import { buildArtifactSuggestions, pipelinesReactSelectStyles } from "../../constants";
import type { InteractiveStep, SelectOption } from "../../types";
import { PipelinesNumberInput } from "../PipelinesNumberInput";

type UpdateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;

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
  cameraAreaOptions: SelectOption[];
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
  const selectedAreaOptions = selectedAreaKeys.map((value) => cameraAreaOptions.find((option) => option.value === value) ?? { value, label: value });

  const invalidAreaSelections = selectedAreaOptions.filter((opt) => !cameraAreaOptions.some((known) => known.value === opt.value));

  return (
    <>
      <div className="pipelinesOperatorConfigCard">
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.panels.area_restriction.areas")}</span>
          <Select<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={cameraAreaOptions}
            value={selectedAreaOptions}
            isDisabled={!interactiveCameraId || !activeCameraContexts || Boolean(activeCameraContextsError) || cameraAreaOptions.length === 0}
            placeholder={
              !interactiveCameraId ? t("core.ui.pipelines.panels.area_restriction.select_camera_first") : t("core.ui.pipelines.panels.area_restriction.select_areas")
            }
            onChange={(value: MultiValue<SelectOption>) => {
              onUpdateConfig((prev) => ({
                ...prev,
                areas: [],
                exclude_area_names: [],
                include_area_names: value.map((item) => item.value),
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
          {t("core.ui.pipelines.panels.area_restriction.invalid_areas", { areas: invalidAreaSelections.map((opt) => opt.label).join(", ") })}
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

type ImageCropProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ImageCropConfigCard({ config, showAdvanced, onUpdateConfig }: ImageCropProps): React.ReactElement {
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

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_crop.hint")}</div>

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
    </div>
  );
}

type ImagePerspectiveCropProps = {
  config: Record<string, unknown>;
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
  showAdvanced,
  onUpdateConfig,
}: ImagePerspectiveCropProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const unitsRaw = String((config as any).units ?? "percent").trim().toLowerCase();
  const units = unitsRaw === "pixels" ? "pixels" : "percent";

  const points = readPerspectivePoints(config, units);

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
    </div>
  );
}

type ImageAdjustProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

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
