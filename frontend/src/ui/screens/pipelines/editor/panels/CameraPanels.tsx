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
  const backendRaw = String((config as any).backend ?? "auto").trim().toLowerCase() || "auto";
  const backend = backendRaw === "opencv" || backendRaw === "ffmpeg" ? backendRaw : "auto";
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

      <label className="pipelinesLabel">
        <span>Backend</span>
        <select
          className="pipelinesSelect"
          value={backend}
          onChange={(event) => {
            const next = String(event.target.value || "auto").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, backend: next || "auto" }));
          }}
        >
          <option value="auto">Auto (recommended)</option>
          <option value="opencv">OpenCV</option>
          <option value="ffmpeg">FFmpeg</option>
        </select>
      </label>
      <div className="pipelinesStepHint">Auto selects the best available backend and falls back automatically if one fails to initialize.</div>
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

type ImageCropProps = {
  config: Record<string, unknown>;
  showAdvanced: boolean;
  onUpdateConfig: UpdateConfig;
};

export function ImageCropConfigCard({ config, showAdvanced, onUpdateConfig }: ImageCropProps): React.ReactElement {
  const unitsRaw = String((config as any).units ?? "percent").trim().toLowerCase();
  const units = unitsRaw === "pixels" ? "pixels" : "percent";
  const left = Number((config as any).left ?? 0);
  const top = Number((config as any).top ?? 0);
  const right = Number((config as any).right ?? 100);
  const bottom = Number((config as any).bottom ?? 100);
  const outputArtifactName = String((config as any).output_artifact_name ?? "frame_cropped").trim() || "frame_cropped";
  const minCropSizePx = Number((config as any).min_crop_size_px ?? 8);
  const setStreamFrame = (config as any).set_stream_frame ?? (config as any).set_payload_frame ?? true;

  const percentMax = 100;
  const clampPercent = (value: number) => Math.max(0, Math.min(percentMax, value));

  return (
    <div className="pipelinesOperatorConfigCard">
      <div className="pipelinesStepHint">
        Crops the frame for downstream analysis (YOLO). The original full frame is preserved as <code>frame_original</code>.
      </div>

      <label className="pipelinesLabel">
        <span>Units</span>
        <select
          className="pipelinesSelect"
          value={units}
          onChange={(event) => {
            const next = String(event.target.value || "percent").trim().toLowerCase();
            onUpdateConfig((prev) => ({ ...prev, units: next === "pixels" ? "pixels" : "percent" }));
          }}
        >
          <option value="percent">Percent (0–100)</option>
          <option value="pixels">Pixels</option>
        </select>
      </label>

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Left</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(left) ? String(units === "percent" ? clampPercent(left) : Math.max(0, left)) : "0"}
            onChange={(event) => {
              const nextValue = Number(event.target.value || 0);
              onUpdateConfig((prev) => ({ ...prev, left: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Top</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(top) ? String(units === "percent" ? clampPercent(top) : Math.max(0, top)) : "0"}
            onChange={(event) => {
              const nextValue = Number(event.target.value || 0);
              onUpdateConfig((prev) => ({ ...prev, top: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Right</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(right) ? String(units === "percent" ? clampPercent(right) : Math.max(0, right)) : "100"}
            onChange={(event) => {
              const nextValue = Number(event.target.value || 0);
              onUpdateConfig((prev) => ({ ...prev, right: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Bottom</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0}
            max={units === "percent" ? percentMax : undefined}
            step={units === "percent" ? 0.5 : 1}
            value={Number.isFinite(bottom) ? String(units === "percent" ? clampPercent(bottom) : Math.max(0, bottom)) : "100"}
            onChange={(event) => {
              const nextValue = Number(event.target.value || 0);
              onUpdateConfig((prev) => ({ ...prev, bottom: units === "percent" ? clampPercent(nextValue) : Math.max(0, nextValue) }));
            }}
          />
        </label>
      </div>

      <div className="rowWrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <div className="pipelinesStepHint">
          Rectangle is defined as Left/Top/Right/Bottom (percent of frame or pixels from top-left).
        </div>
        <button
          className="chipButton"
          type="button"
          onClick={() => onUpdateConfig((prev) => ({ ...prev, left: 0, top: 0, right: 100, bottom: 100, units: "percent" }))}
        >
          Reset
        </button>
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <label className="pipelinesLabel">
            <span>Cropped artifact name</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>Min crop size (px)</span>
            <input
              className="pipelinesInput"
              type="number"
              min={1}
              max={4096}
              step={1}
              value={Number.isFinite(minCropSizePx) ? String(Math.max(1, Math.min(4096, minCropSizePx))) : "8"}
              onChange={(event) => {
                const nextValue = Number(event.target.value || 0);
                const normalized = Number.isFinite(nextValue) ? Math.max(1, Math.min(4096, nextValue)) : 8;
                onUpdateConfig((prev) => ({ ...prev, min_crop_size_px: normalized }));
              }}
            />
          </label>

          <label className="pipelinesLabel">
            <span>Use cropped frame for downstream</span>
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
  const inputArtifactNamesRaw = (config as any).input_artifact_names;
  const inputArtifactNames = Array.isArray(inputArtifactNamesRaw)
    ? inputArtifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
    : ["frame_original"];
  const selectedInputOptions = inputArtifactNames.map(
    (value) => ARTIFACT_SUGGESTIONS.find((opt) => opt.value === value) ?? { value, label: value },
  );

  const saturation = Number((config as any).saturation ?? 1.0);
  const brightness = Number((config as any).brightness ?? 0.0);
  const contrast = Number((config as any).contrast ?? 1.0);
  const gamma = Number((config as any).gamma ?? 1.0);

  const outputArtifactName = String((config as any).output_artifact_name ?? "frame_adjusted").trim() || "frame_adjusted";
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
        <span>Input artifacts (fallback order)</span>
        <CreatableSelect<SelectOption, true>
          isMulti
          styles={pipelinesReactSelectStyles}
          options={ARTIFACT_SUGGESTIONS}
          value={selectedInputOptions}
          placeholder="Full frame"
          onChange={(value: MultiValue<SelectOption>) => {
            onUpdateConfig((prev) => ({
              ...prev,
              input_artifact_names: value.map((item) => item.value),
            }));
          }}
        />
      </label>
      <div className="pipelinesStepHint">Uses the first available artifact. Keep <code>frame_original</code> as fallback.</div>

      <div className="pipelinesScalarGrid" style={{ marginTop: 8 }}>
        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Saturation</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0}
            max={3}
            step={0.05}
            value={String(clamp(saturation, 0, 3, 1))}
            onChange={(event) => {
              const nextValue = Number(event.target.value);
              onUpdateConfig((prev) => ({ ...prev, saturation: clamp(nextValue, 0, 3, 1) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Brightness</span>
          <input
            className="pipelinesInput"
            type="number"
            min={-1}
            max={1}
            step={0.02}
            value={String(clamp(brightness, -1, 1, 0))}
            onChange={(event) => {
              const nextValue = Number(event.target.value);
              onUpdateConfig((prev) => ({ ...prev, brightness: clamp(nextValue, -1, 1, 0) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Contrast</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0}
            max={3}
            step={0.05}
            value={String(clamp(contrast, 0, 3, 1))}
            onChange={(event) => {
              const nextValue = Number(event.target.value);
              onUpdateConfig((prev) => ({ ...prev, contrast: clamp(nextValue, 0, 3, 1) }));
            }}
          />
        </label>

        <label className="pipelinesLabel pipelinesScalarLabel">
          <span>Gamma</span>
          <input
            className="pipelinesInput"
            type="number"
            min={0.1}
            max={5}
            step={0.05}
            value={String(clamp(gamma, 0.1, 5, 1))}
            onChange={(event) => {
              const nextValue = Number(event.target.value);
              onUpdateConfig((prev) => ({ ...prev, gamma: clamp(nextValue, 0.1, 5, 1) }));
            }}
          />
        </label>
      </div>

      <div className="pipelinesStepHint" style={{ marginTop: 8 }}>
        Brightness is an additive offset in normalized space (e.g. <code>0.10</code> = +10%).
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />

          <label className="pipelinesLabel">
            <span>Output artifact name</span>
            <input
              className="pipelinesInput"
              type="text"
              value={outputArtifactName}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, output_artifact_name: event.target.value }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>Apply to stream frame</span>
            <input
              type="checkbox"
              checked={Boolean(setStreamFrame)}
              onChange={(event) => onUpdateConfig((prev) => ({ ...prev, set_stream_frame: event.target.checked }))}
            />
          </label>

          <label className="pipelinesLabel">
            <span>Fallback to stream frame</span>
            <input
              type="checkbox"
              checked={Boolean(fallbackToStreamFrame)}
              onChange={(event) =>
                onUpdateConfig((prev) => ({ ...prev, fallback_to_stream_frame: event.target.checked }))
              }
            />
          </label>

          <label className="pipelinesLabel">
            <span>Preserve alpha channel</span>
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
