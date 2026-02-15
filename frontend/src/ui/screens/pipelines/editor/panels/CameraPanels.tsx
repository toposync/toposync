import React from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import type { CameraContextsResponse } from "../../../../../util/api";

import { ARTIFACT_SUGGESTIONS, pipelinesReactSelectStyles } from "../../constants";
import type { InteractiveStep, SelectOption } from "../../types";

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
  const cameraIdInConfig = String((config as any).camera_id ?? "").trim();
  const selectedCameraOption = cameraIdInConfig
    ? (cameraSelectOptionById.get(cameraIdInConfig) ?? { value: cameraIdInConfig, label: cameraIdInConfig })
    : null;

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Camera</span>
        <Select<SelectOption, false>
          styles={pipelinesReactSelectStyles}
          options={cameraSelectOptions}
          value={selectedCameraOption}
          isClearable
          placeholder="Select a camera…"
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
      <div className="pipelinesStepHint">RTSP URL, credentials, and FPS are inferred from the camera registry. Toggle Advanced to override.</div>
      {cameraSelectOptions.length === 0 ? (
        <div className="pipelinesStepHint">No cameras found. Configure cameras in the Cameras extension settings.</div>
      ) : null}
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
  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">
        Uses control points defined in your compositions to map image → world coordinates. Configure control points in the Composition editor.
      </div>
      {!interactiveCameraId ? (
        <div className="pipelinesInlineError">Select a camera in the Camera Source step to show mapping status.</div>
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
                    {hasMapping ? "mapping ready" : "missing mapping"}
                    {areasCount ? ` • areas: ${areasCount}` : ""}
                    {elementNames.length ? ` • camera nodes: ${elementNames.join(", ")}` : ""}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : activeCameraContextsError ? (
        <div className="pipelinesInlineError">Failed to load camera contexts: {activeCameraContextsError}</div>
      ) : (
        <div className="pipelinesStepHint">Loading camera contexts…</div>
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
          <span>Areas</span>
          <Select<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={cameraAreaOptions}
            value={selectedAreaOptions}
            isDisabled={!interactiveCameraId || !activeCameraContexts || Boolean(activeCameraContextsError) || cameraAreaOptions.length === 0}
            placeholder={!interactiveCameraId ? "Select a camera first…" : "Select areas…"}
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
          <div className="pipelinesInlineError">Select a camera in the Camera Source step first.</div>
        ) : activeCameraContextsError ? (
          <div className="pipelinesInlineError">Failed to load camera contexts: {activeCameraContextsError}</div>
        ) : !activeCameraContexts ? (
          <div className="pipelinesStepHint">Loading camera contexts…</div>
        ) : cameraAreaOptions.length === 0 ? (
          <div className="pipelinesStepHint">No areas found in compositions for this camera.</div>
        ) : (
          <div className="pipelinesStepHint">Uses areas from the compositions where the selected camera is present.</div>
        )}
      </div>

      {invalidAreaSelections.length > 0 ? (
        <div className="pipelinesInlineError">
          Some selected areas are not available for this camera: {invalidAreaSelections.map((opt) => opt.label).join(", ")}
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
  const modeRaw = String((config as any).filter_mode ?? "annotate").trim().toLowerCase() || "annotate";
  const stoppedMpsRaw = Number((config as any).stopped_speed_threshold ?? 0.04);
  const stoppedKmh = Number.isFinite(stoppedMpsRaw) ? stoppedMpsRaw * 3.6 : 0.0;
  const hasMappingBefore = steps.slice(0, index).some((item) => item.operatorId === "camera.camera_mapping");

  const modeOptions: Array<{ value: string; label: string; hint: string }> = [
    { value: "annotate", label: "Annotate only", hint: "Always emit packets; adds velocity payload." },
    { value: "stopped_now", label: "Only when stopped", hint: "Emit packets only while the object is stopped." },
    { value: "moving_now", label: "Only when moving", hint: "Emit packets only while the object is moving." },
  ];
  if (showAdvanced) {
    modeOptions.push(
      { value: "stopped_once", label: "Only after it stopped once", hint: "Drops packets until it stops at least once, then passes all." },
      { value: "always_moving", label: "Only while it never stopped", hint: "Passes packets until it stops once, then drops the rest." },
    );
  }
  const selected = modeOptions.find((item) => item.value === modeRaw) ?? modeOptions[0];

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Flow mode</span>
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
        <span>Stopped threshold (km/h)</span>
        <input
          className="pipelinesInput"
          type="number"
          min={0}
          max={4000}
          step={0.05}
          value={Number.isFinite(stoppedKmh) ? String(Math.max(0, stoppedKmh)) : "0"}
          onChange={(event) => {
            const kmh = Number(event.target.value || 0);
            const mps = Number.isFinite(kmh) ? Math.max(0, kmh) / 3.6 : 0;
            onUpdateConfig((prev) => ({ ...prev, stopped_speed_threshold: mps }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Computes speed from mapped world coordinates (Camera Mapping step). Uses m/s internally and also displays km/h.</div>
      {!hasMappingBefore ? <div className="pipelinesInlineError">Add Camera Mapping before this step to get world speed.</div> : null}
    </div>
  );
}

type ImageResizeProps = {
  config: Record<string, unknown>;
  onUpdateConfig: UpdateConfig;
};

export function ImageResizeConfigCard({ config, onUpdateConfig }: ImageResizeProps): React.ReactElement {
  const maxEdgePx = Number((config as any).max_edge_px ?? 1280);
  const allowUpscale = Boolean((config as any).allow_upscale ?? false);
  const artifactNamesRaw = (config as any).artifact_names;
  const artifactNames = Array.isArray(artifactNamesRaw)
    ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : ["frame_original"];
  const selectedOptions = artifactNames.map((value) => ARTIFACT_SUGGESTIONS.find((opt) => opt.value === value) ?? { value, label: value });

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>Artifacts</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={ARTIFACT_SUGGESTIONS}
          value={selectedOptions}
          placeholder="Full frame"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              artifact_names: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Resizes artifacts in-memory before storage to keep file sizes reasonable.</div>

      <label className="pipelinesLabel">
        <span>Max edge (px)</span>
        <input
          className="pipelinesInput"
          type="number"
          min={16}
          max={16384}
          step={1}
          value={Number.isFinite(maxEdgePx) ? String(maxEdgePx) : "1280"}
          onChange={(event) => {
            const nextValue = Number(event.target.value || 0);
            onUpdateConfig((prev) => ({
              ...prev,
              max_edge_px: Number.isFinite(nextValue) ? Math.max(16, Math.min(16384, nextValue)) : 1280,
            }));
          }}
        />
      </label>

      <label className="pipelinesLabel">
        <span>Allow upscale</span>
        <input type="checkbox" checked={allowUpscale} onChange={(event) => onUpdateConfig((prev) => ({ ...prev, allow_upscale: event.target.checked }))} />
      </label>
    </div>
  );
}

