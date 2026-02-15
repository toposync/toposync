import React, { useCallback, useEffect, useMemo, useState } from "react";
import Select, { type MultiValue, type SingleValue } from "react-select";
import CreatableSelect from "react-select/creatable";

import type { CameraContextsResponse, CamerasIndexResponse, PipelineOperatorDefinition } from "../../../util/api";
import { getCameraContexts } from "../../../util/api";

import {
  ARTIFACT_SUGGESTIONS,
  PIPELINE_PRESET_OPERATOR_IDS,
  pipelinesReactSelectStyles,
  SCHEDULE_WEEKDAY_OPTIONS,
  YOLO_CATEGORY_OPTIONS,
} from "./constants";
import type { DragInsertPosition, InteractiveBuildResult, InteractiveStep, SelectOption } from "./types";
import {
  createInteractiveStep,
  humanizeIdentifier,
  isRecord,
  jsonPretty,
  moveStep,
  pickDefaultOperatorId,
  prettyConfigKeyLabel,
  prettyOperatorLabel,
  prettyOperatorName,
  safeJsonParse,
} from "./utils";

type Props = {
  operators: PipelineOperatorDefinition[];
  operatorsById: Record<string, PipelineOperatorDefinition>;
  camerasIndex: CamerasIndexResponse;
  interactiveSteps: InteractiveStep[];
  setInteractiveSteps: React.Dispatch<React.SetStateAction<InteractiveStep[]>>;
  interactiveWarning: string | null;
  setInteractiveWarning: React.Dispatch<React.SetStateAction<string | null>>;
  interactiveGraph: InteractiveBuildResult;
};

export function InteractivePipelineEditor({
  operators,
  operatorsById,
  camerasIndex,
  interactiveSteps,
  setInteractiveSteps,
  interactiveWarning,
  setInteractiveWarning,
  interactiveGraph,
}: Props): React.ReactElement {
  const [interactiveAddOperatorId, setInteractiveAddOperatorId] = useState<string>("");
  const [draggingStepUid, setDraggingStepUid] = useState<string | null>(null);
  const [dragOverStep, setDragOverStep] = useState<{ uid: string; position: DragInsertPosition } | null>(null);

  const [cameraContextsById, setCameraContextsById] = useState<Record<string, CameraContextsResponse>>({});
  const [cameraContextsErrorById, setCameraContextsErrorById] = useState<Record<string, string>>({});

  const presetOperators = useMemo(
    () => PIPELINE_PRESET_OPERATOR_IDS.map((id) => operatorsById[id]).filter(Boolean) as PipelineOperatorDefinition[],
    [operatorsById],
  );

  const interactiveCameraId = useMemo(() => {
    const sourceStep = interactiveSteps.find((step) => step.operatorId === "camera.source");
    if (!sourceStep) return "";
    const parsed = safeJsonParse(sourceStep.configText || "{}");
    if (!parsed.ok) return "";
    if (!isRecord(parsed.data)) return "";
    return String((parsed.data as any).camera_id ?? "").trim();
  }, [interactiveSteps]);

  const cameraSelectOptions = useMemo<SelectOption[]>(() => {
    const cameras = Array.isArray(camerasIndex.cameras) ? camerasIndex.cameras : [];
    return cameras
      .map((camera) => {
        const name = String(camera.name || "").trim();
        const id = String(camera.id || "").trim();
        return { value: id, label: name ? `${name} (${id})` : id };
      })
      .filter((option) => option.value.length > 0)
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [camerasIndex]);

  const cameraSelectOptionById = useMemo(() => {
    const map = new Map<string, SelectOption>();
    for (const option of cameraSelectOptions) map.set(option.value, option);
    return map;
  }, [cameraSelectOptions]);

  const activeCameraContexts = useMemo(() => {
    const cameraId = interactiveCameraId;
    if (!cameraId) return null;
    return cameraContextsById[cameraId] ?? null;
  }, [interactiveCameraId, cameraContextsById]);

  const activeCameraContextsError = useMemo(() => {
    const cameraId = interactiveCameraId;
    if (!cameraId) return null;
    return cameraContextsErrorById[cameraId] ?? null;
  }, [interactiveCameraId, cameraContextsErrorById]);

  const cameraAreaOptions = useMemo<SelectOption[]>(() => {
    const contexts = activeCameraContexts;
    if (!contexts) return [];
    const options: SelectOption[] = [];
    for (const composition of contexts.compositions ?? []) {
      for (const area of composition.areas ?? []) {
        options.push({
          value: `${composition.id}:${area.id}`,
          label: `${composition.name} / ${area.name}`,
        });
      }
    }
    options.sort((a, b) => a.label.localeCompare(b.label));
    return options;
  }, [activeCameraContexts]);

  useEffect(() => {
    if (interactiveAddOperatorId && operatorsById[interactiveAddOperatorId]) return;
    setInteractiveAddOperatorId(pickDefaultOperatorId(operators));
  }, [interactiveAddOperatorId, operatorsById, operators]);

  useEffect(() => {
    const cameraId = interactiveCameraId;
    if (!cameraId) return;
    if (cameraContextsById[cameraId]) return;
    if (cameraContextsErrorById[cameraId]) return;

    let cancelled = false;
    void (async () => {
      try {
        const contexts = await getCameraContexts(cameraId);
        if (cancelled) return;
        setCameraContextsById((prev) => ({ ...prev, [cameraId]: contexts }));
      } catch (err: any) {
        if (cancelled) return;
        setCameraContextsErrorById((prev) => ({ ...prev, [cameraId]: String(err?.message ?? err) }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [interactiveCameraId, cameraContextsById, cameraContextsErrorById]);

  const addInteractiveStep = (operatorId: string) => {
    const op = operatorsById[operatorId];
    if (!op) return;
    setInteractiveSteps((prev) => {
      const used = new Set(prev.map((item) => item.nodeId));
      const next = createInteractiveStep(operatorId, op.defaults ?? {}, used);
      if (operatorId === "core.schedule_gate") {
        const cameraIndex = prev.findIndex((item) => item.operatorId === "camera.source");
        if (cameraIndex >= 0) {
          const copy = prev.slice();
          copy.splice(cameraIndex, 0, next);
          return copy;
        }
        return [next, ...prev];
      }
      return [...prev, next];
    });
    setInteractiveWarning(null);
  };

  const updateInteractiveStep = (uid: string, patch: Partial<InteractiveStep>) => {
    setInteractiveSteps((prev) => prev.map((step) => (step.uid === uid ? { ...step, ...patch } : step)));
  };

  const removeInteractiveStep = (uid: string) => {
    setInteractiveSteps((prev) => prev.filter((step) => step.uid !== uid));
  };

  const updateInteractiveStepScalar = (uid: string, key: string, value: string | number | boolean) => {
    setInteractiveSteps((prev) =>
      prev.map((step) => {
        if (step.uid !== uid) return step;
        const parsed = safeJsonParse(step.configText || "{}");
        const nextConfig = isRecord(parsed.ok ? parsed.data : null) ? { ...(parsed.data as Record<string, unknown>) } : {};
        nextConfig[key] = value;
        return { ...step, configText: jsonPretty(nextConfig) };
      }),
    );
  };

  const updateInteractiveStepConfig = (uid: string, updater: (config: Record<string, unknown>) => Record<string, unknown>) => {
    setInteractiveSteps((prev) =>
      prev.map((step) => {
        if (step.uid !== uid) return step;
        const parsed = safeJsonParse(step.configText || "{}");
        const base = isRecord(parsed.ok ? parsed.data : null) ? { ...(parsed.data as Record<string, unknown>) } : {};
        const next = updater(base);
        return { ...step, configText: jsonPretty(next) };
      }),
    );
  };

  const beginStepDrag = useCallback((event: React.DragEvent, uid: string) => {
    setDraggingStepUid(uid);
    setDragOverStep(null);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", uid);
  }, []);

  const endStepDrag = useCallback(() => {
    setDraggingStepUid(null);
    setDragOverStep(null);
  }, []);

  const updateStepDragOver = useCallback(
    (event: React.DragEvent<HTMLElement>, targetUid: string) => {
      const draggedUid = draggingStepUid;
      if (!draggedUid || draggedUid === targetUid) return;
      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      const position: DragInsertPosition = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
      setDragOverStep({ uid: targetUid, position });
    },
    [draggingStepUid],
  );

  const dropStep = useCallback(
    (event: React.DragEvent<HTMLElement>, targetUid: string) => {
      const draggedUid = draggingStepUid || event.dataTransfer.getData("text/plain");
      if (!draggedUid || draggedUid === targetUid) return;
      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      const position: DragInsertPosition = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
      setInteractiveSteps((prev) => moveStep(prev, draggedUid, targetUid, position));
      setDraggingStepUid(null);
      setDragOverStep(null);
    },
    [draggingStepUid, setInteractiveSteps],
  );

  return (
    <div className="pipelinesInteractiveRoot">
      <div className="pipelinesInteractiveToolbar">
        <div className="pipelinesInteractiveLabel">Add step</div>
        <div className="pipelinesPresetButtons">
          {presetOperators.map((operator) => (
            <button
              key={operator.id}
              className="pillButton"
              type="button"
              onClick={() => addInteractiveStep(operator.id)}
              title={operator.description || operator.id}
            >
              + {operator.id.split(".").pop()}
            </button>
          ))}
        </div>
        <div className="pipelinesInlineAddRow">
          <select
            className="pipelinesSelect"
            value={interactiveAddOperatorId}
            onChange={(event) => setInteractiveAddOperatorId(event.target.value)}
          >
            {operators.map((operator) => (
              <option key={operator.id} value={operator.id}>
                {prettyOperatorLabel(operator)}
              </option>
            ))}
          </select>
          <button
            className="pillButton pillButtonPrimary"
            type="button"
            onClick={() => addInteractiveStep(interactiveAddOperatorId)}
          >
            Add
          </button>
        </div>
      </div>

      {interactiveWarning ? (
        <div className="card">
          <div className="cardBody">{interactiveWarning}</div>
        </div>
      ) : null}

      {interactiveGraph.error ? (
        <div className="card cardDanger">
          <div className="cardBody">{interactiveGraph.error}</div>
        </div>
      ) : null}

      <div className="pipelinesStepsList">
        {interactiveSteps.map((step, index) => {
          const operator = operatorsById[step.operatorId];
          const configParsed = safeJsonParse(step.configText || "{}");
          const configRecordOk = configParsed.ok && isRecord(configParsed.data);
          const config = configRecordOk ? (configParsed.data as Record<string, unknown>) : {};
          const configObjectError = !configParsed.ok
            ? `Invalid config JSON: ${configParsed.error}`
            : !configRecordOk
              ? "Config must be a JSON object."
              : null;

          const scalarEntries = Object.entries(config)
            .filter(([, value]) => {
              const valueType = typeof value;
              return valueType === "string" || valueType === "number" || valueType === "boolean";
            })
            .filter(([key]) => {
              if (step.operatorId === "core.notify") {
                return ![
                  "title",
                  "description",
                  "priority",
                  "realtime",
                  "update_interval_seconds",
                  "notification_type",
                  "dedupe_key_template",
                ].includes(key);
              }
              return true;
            });

          const isConfigScalarGridHidden =
            step.operatorId === "core.schedule_gate" ||
            step.operatorId === "camera.source" ||
            step.operatorId === "camera.image_resize" ||
            step.operatorId === "camera.camera_mapping" ||
            step.operatorId === "camera.area_restriction" ||
            step.operatorId === "camera.velocity_estimation" ||
            step.operatorId === "core.throttle" ||
            step.operatorId === "core.debounce" ||
            step.operatorId === "core.debug" ||
            step.operatorId === "core.notify" ||
            step.operatorId === "core.store_images" ||
            step.operatorId === "core.category_gate" ||
            step.operatorId === "vision.object_tracking_yolo" ||
            step.operatorId === "vision.object_detection_yolo";
          const shouldShowScalarGrid = scalarEntries.length > 0 && (!isConfigScalarGridHidden || step.showAdvanced);

          const rowClass = ["pipelinesStepCard"];
          if (draggingStepUid === step.uid) rowClass.push("isDragSource");
          if (dragOverStep?.uid === step.uid) {
            rowClass.push(dragOverStep.position === "before" ? "isDropBefore" : "isDropAfter");
          }

          const cameraIdInConfig = String((config as any).camera_id ?? "").trim();
          const selectedCameraOption = cameraIdInConfig
            ? (cameraSelectOptionById.get(cameraIdInConfig) ?? { value: cameraIdInConfig, label: cameraIdInConfig })
            : null;

          const yoloCategoriesRaw = (config as any).categories;
          const yoloCategories = Array.isArray(yoloCategoriesRaw)
            ? yoloCategoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
            : [];
          const yoloConfidenceRaw = Number((config as any).confidence_threshold ?? 0.4);
          const yoloConfidence = Number.isFinite(yoloConfidenceRaw) ? Math.max(0, Math.min(1, yoloConfidenceRaw)) : 0.4;

          const areaNamesRaw = (config as any).include_area_names;
          const selectedAreaKeys = Array.isArray(areaNamesRaw)
            ? areaNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
            : [];
          const selectedAreaOptions = selectedAreaKeys.map(
            (value) => cameraAreaOptions.find((option) => option.value === value) ?? { value, label: value },
          );

          const artifactNamesRaw = (config as any).artifact_names;
          const artifactNames = Array.isArray(artifactNamesRaw)
            ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
            : [];
          const selectedArtifactOptions = artifactNames.map(
            (value) => ARTIFACT_SUGGESTIONS.find((option) => option.value === value) ?? { value, label: value },
          );

          const notifyFallbackRaw = (config as any).thumbnail_with_fallback;
          const notifyFallback = Array.isArray(notifyFallbackRaw)
            ? notifyFallbackRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
            : ["best_frame", "frame_original"];
          const notifySelectedFallbackOptions = notifyFallback.map((value) => ({ value, label: value }));

          const areaValue = selectedAreaOptions.map((opt) => opt.value);
          const invalidAreaSelections = selectedAreaOptions.filter((opt) => !cameraAreaOptions.some((known) => known.value === opt.value));

          const shouldShowAdvanced = step.showAdvanced;
          const shouldShowConfigJson = shouldShowAdvanced;

          const nodeIdValue = String(step.nodeId || "").trim();
          const operatorName = operator ? prettyOperatorName(operator.id) : prettyOperatorName(step.operatorId);

          const stepIndexLabel = `${index + 1}.`;

          return (
            <div
              key={step.uid}
              className={rowClass.join(" ")}
              draggable
              onDragStart={(event) => beginStepDrag(event, step.uid)}
              onDragEnd={endStepDrag}
              onDragOver={(event) => updateStepDragOver(event, step.uid)}
              onDrop={(event) => dropStep(event, step.uid)}
            >
              <div className="pipelinesStepHeader">
                <div className="pipelinesStepHeaderMain">
                  <div className="pipelinesStepIndex">{stepIndexLabel}</div>
                  <div className="pipelinesStepTitle">{operatorName}</div>
                </div>

                <div className="pipelinesStepHeaderActions">
                  <button
                    className="iconButton"
                    type="button"
                    onClick={() => updateInteractiveStep(step.uid, { collapsed: !step.collapsed })}
                    title={step.collapsed ? "Expand" : "Collapse"}
                  >
                    <i className={step.collapsed ? "fa-solid fa-chevron-down" : "fa-solid fa-chevron-up"} aria-hidden="true" />
                  </button>

                  <button
                    className={["iconButton", step.showAdvanced ? "isActive" : ""].filter(Boolean).join(" ")}
                    type="button"
                    onClick={() => updateInteractiveStep(step.uid, { showAdvanced: !step.showAdvanced })}
                    title={step.showAdvanced ? "Hide advanced" : "Show advanced"}
                  >
                    <i className="fa-solid fa-sliders" aria-hidden="true" />
                  </button>

                  <button className="iconButton" type="button" onClick={() => removeInteractiveStep(step.uid)} title="Remove step">
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                  </button>
                </div>
              </div>

              {!step.collapsed ? (
                <div className="pipelinesStepBody">
                  {operator ? <div className="pipelinesStepDescription">{operator.description || prettyOperatorName(operator.id)}</div> : null}
                  {operator && operator.capabilities.length > 0 && step.showAdvanced ? (
                    <div className="pipelinesStepCapabilities">
                      caps: {operator.capabilities.map((cap) => humanizeIdentifier(cap) || cap).join(", ")}
                    </div>
                  ) : null}

                  {step.showAdvanced ? (
                    <div className="pipelinesOperatorConfigCard">
                      <label className="pipelinesLabel">
                        <span>Step ID</span>
                        <input
                          className="pipelinesInput"
                          value={step.nodeId}
                          onChange={(event) => updateInteractiveStep(step.uid, { nodeId: event.target.value })}
                          placeholder="stepId"
                        />
                      </label>
                      <div className="pipelinesStepHint">Internal identifier used in storage paths, logs, and diagnostics.</div>
                    </div>
                  ) : null}

                  {step.operatorId === "core.schedule_gate" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const enabled = Boolean((config as any).enabled ?? true);
                        const timezone = String((config as any).timezone ?? "").trim();
                        const weekdaysRaw = (config as any).weekdays;
                        const weekdayValues = Array.isArray(weekdaysRaw)
                          ? weekdaysRaw
                              .map((value: any) => String(value || "").trim().toLowerCase())
                              .filter((value: string) => value.length > 0)
                          : ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
                        const uniqueWeekdayValues = [...new Set(weekdayValues)];
                        const selectedWeekdayOptions = uniqueWeekdayValues.map((value) => {
                          const known = SCHEDULE_WEEKDAY_OPTIONS.find((option) => option.value === value);
                          return known ?? { value, label: value };
                        });

                        const startTimeRaw = String((config as any).start_time ?? "00:00").trim() || "00:00";
                        const endTimeRaw = String((config as any).end_time ?? "00:00").trim() || "00:00";
                        const startTimeValue = startTimeRaw.length >= 5 ? startTimeRaw.slice(0, 5) : "00:00";
                        const endTimeValue = endTimeRaw.length >= 5 ? endTimeRaw.slice(0, 5) : "00:00";

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Enabled</span>
                              <input
                                type="checkbox"
                                checked={enabled}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, enabled: event.target.checked }));
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, start_time: nextValue }));
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, end_time: nextValue }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">Place this before Camera source to pause RTSP reads while the gate is closed.</div>

                            {step.showAdvanced ? (
                              <label className="pipelinesLabel">
                                <span>Time zone (optional)</span>
                                <input
                                  className="pipelinesInput"
                                  type="text"
                                  value={timezone}
                                  placeholder="Leave empty for local time"
                                  onChange={(event) => {
                                    const nextValue = String(event.target.value ?? "");
                                    updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, timezone: nextValue }));
                                  }}
                                />
                              </label>
                            ) : null}
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "camera.source" ? (
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
                            updateInteractiveStepConfig(step.uid, (prev) => {
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
                      <div className="pipelinesStepHint">
                        RTSP URL, credentials, and FPS are inferred from the camera registry. Toggle Advanced to override.
                      </div>
                      {cameraSelectOptions.length === 0 ? (
                        <div className="pipelinesStepHint">No cameras found. Configure cameras in the Cameras extension settings.</div>
                      ) : null}
                    </div>
                  ) : null}

                  {step.operatorId === "vision.object_tracking_yolo" || step.operatorId === "vision.object_detection_yolo" ? (
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
                            updateInteractiveStepConfig(step.uid, (prev) => ({
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
                            updateInteractiveStepConfig(step.uid, (prev) => ({
                              ...prev,
                              categories: value.map((item) => item.value),
                            }));
                          }}
                        />
                      </label>
                      <div className="pipelinesStepHint">Empty selection means “all categories”.</div>
                    </div>
                  ) : null}

                  {step.operatorId === "core.category_gate" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const modeRaw = String((config as any).mode ?? "include").trim().toLowerCase() || "include";
                        const mode = modeRaw === "exclude" ? "exclude" : "include";
                        const categoriesRaw = (config as any).categories;
                        const categories = Array.isArray(categoriesRaw)
                          ? categoriesRaw
                              .map((value: any) => String(value || "").trim().toLowerCase())
                              .filter((value: string) => value.length > 0)
                          : [];
                        const selectedCategoryOptions = categories.map((value) => YOLO_CATEGORY_OPTIONS.find((opt) => opt.value === value) ?? { value, label: value });

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Mode</span>
                              <select
                                className="pipelinesSelect"
                                value={mode}
                                onChange={(event) => {
                                  const nextMode = String(event.target.value || "include").trim().toLowerCase();
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, mode: nextMode === "exclude" ? "exclude" : "include" }));
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
                                    ...prev,
                                    categories: value.map((item) => item.value),
                                  }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">
                              Matches <code>payload.object_category_label</code> (set by YOLO operators). Empty selection means “all categories”.
                            </div>
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "camera.camera_mapping" ? (
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
                  ) : null}

                  {step.operatorId === "camera.area_restriction" ? (
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
                            updateInteractiveStepConfig(step.uid, (prev) => ({
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
                  ) : null}

                  {invalidAreaSelections.length > 0 ? (
                    <div className="pipelinesInlineError">
                      Some selected areas are not available for this camera: {invalidAreaSelections.map((opt) => opt.label).join(", ")}
                    </div>
                  ) : null}

                  {step.operatorId === "camera.velocity_estimation" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const modeRaw = String((config as any).filter_mode ?? "annotate").trim().toLowerCase() || "annotate";
                        const stoppedMpsRaw = Number((config as any).stopped_speed_threshold ?? 0.04);
                        const stoppedKmh = Number.isFinite(stoppedMpsRaw) ? stoppedMpsRaw * 3.6 : 0.0;
                        const hasMappingBefore = interactiveSteps.slice(0, index).some((item) => item.operatorId === "camera.camera_mapping");
                        const modeOptions: Array<{ value: string; label: string; hint: string }> = [
                          { value: "annotate", label: "Annotate only", hint: "Always emit packets; adds velocity payload." },
                          { value: "stopped_now", label: "Only when stopped", hint: "Emit packets only while the object is stopped." },
                          { value: "moving_now", label: "Only when moving", hint: "Emit packets only while the object is moving." },
                        ];
                        if (step.showAdvanced) {
                          modeOptions.push(
                            { value: "stopped_once", label: "Only after it stopped once", hint: "Drops packets until it stops at least once, then passes all." },
                            { value: "always_moving", label: "Only while it never stopped", hint: "Passes packets until it stops once, then drops the rest." },
                          );
                        }
                        const selected = modeOptions.find((item) => item.value === modeRaw) ?? modeOptions[0];

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Flow mode</span>
                              <select
                                className="pipelinesSelect"
                                value={selected.value}
                                onChange={(event) => {
                                  const nextMode = String(event.target.value || "annotate").trim().toLowerCase();
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, filter_mode: nextMode }));
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, stopped_speed_threshold: mps }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">
                              Computes speed from mapped world coordinates (Camera Mapping step). Uses m/s internally and also displays km/h.
                            </div>
                            {!hasMappingBefore ? <div className="pipelinesInlineError">Add Camera Mapping before this step to get world speed.</div> : null}
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "core.throttle" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const intervalSeconds = Number((config as any).interval_seconds ?? 1.0);
                        const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
                        const keyFieldRaw = String((config as any).key_field ?? "stream_id").trim() || "stream_id";

                        return (
                          <>
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, mode: nextMode }));
                                }}
                              >
                                <option value="first">First (recommended)</option>
                              </select>
                            </label>

                            {step.showAdvanced ? (
                              <label className="pipelinesLabel">
                                <span>Key</span>
                                <select
                                  className="pipelinesSelect"
                                  value={keyFieldRaw}
                                  onChange={(event) => {
                                    const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
                                    updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, key_field: nextKey }));
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
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "core.debounce" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const quietSeconds = Number((config as any).quiet_period_seconds ?? 1.0);
                        const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
                        const keyFieldRaw = String((config as any).key_field ?? "stream_id").trim() || "stream_id";

                        return (
                          <>
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, mode: nextMode }));
                                }}
                              >
                                <option value="first">First (recommended)</option>
                              </select>
                            </label>

                            {step.showAdvanced ? (
                              <label className="pipelinesLabel">
                                <span>Key</span>
                                <select
                                  className="pipelinesSelect"
                                  value={keyFieldRaw}
                                  onChange={(event) => {
                                    const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
                                    updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, key_field: nextKey }));
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
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "camera.image_resize" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const maxEdgePx = Number((config as any).max_edge_px ?? 1280);
                        const allowUpscale = Boolean((config as any).allow_upscale ?? false);
                        const artifactNamesRaw = (config as any).artifact_names;
                        const artifactNames = Array.isArray(artifactNamesRaw)
                          ? artifactNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
                          : ["frame_original"];
                        const selectedOptions = artifactNames.map((value) => ARTIFACT_SUGGESTIONS.find((opt) => opt.value === value) ?? { value, label: value });

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Artifacts</span>
                              <CreatableSelect<SelectOption, true>
                                isMulti
                                styles={pipelinesReactSelectStyles}
                                options={ARTIFACT_SUGGESTIONS}
                                value={selectedOptions}
                                placeholder="Full frame"
                                onChange={(value: MultiValue<SelectOption>) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
                                    ...prev,
                                    max_edge_px: Number.isFinite(nextValue) ? Math.max(16, Math.min(16384, nextValue)) : 1280,
                                  }));
                                }}
                              />
                            </label>

                            <label className="pipelinesLabel">
                              <span>Allow upscale</span>
                              <input
                                type="checkbox"
                                checked={allowUpscale}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, allow_upscale: event.target.checked }));
                                }}
                              />
                            </label>
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "core.debug" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const enabled = Boolean((config as any).enabled ?? true);
                        const saveImages = Boolean((config as any).save_images ?? true);
                        const printPayload = Boolean((config as any).print_payload ?? true);
                        const printMetadata = Boolean((config as any).print_metadata ?? true);
                        const printArtifacts = Boolean((config as any).print_artifacts ?? true);
                        const maxImagesPerPacket = Number((config as any).max_images_per_packet ?? 4);
                        const outputDir = String((config as any).output_dir ?? "").trim();

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Enabled</span>
                              <input
                                type="checkbox"
                                checked={enabled}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, enabled: event.target.checked }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">Prints packets to stdout and optionally writes images to a temporary folder.</div>

                            <label className="pipelinesLabel">
                              <span>Save images</span>
                              <input
                                type="checkbox"
                                checked={saveImages}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, save_images: event.target.checked }));
                                }}
                              />
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, output_dir: nextValue }));
                                }}
                              />
                            </label>

                            <label className="pipelinesLabel">
                              <span>Print payload</span>
                              <input
                                type="checkbox"
                                checked={printPayload}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, print_payload: event.target.checked }));
                                }}
                              />
                            </label>

                            <label className="pipelinesLabel">
                              <span>Print metadata</span>
                              <input
                                type="checkbox"
                                checked={printMetadata}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, print_metadata: event.target.checked }));
                                }}
                              />
                            </label>

                            <label className="pipelinesLabel">
                              <span>Print artifacts</span>
                              <input
                                type="checkbox"
                                checked={printArtifacts}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, print_artifacts: event.target.checked }));
                                }}
                              />
                            </label>
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "core.store_images" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const formatRaw = String((config as any).format ?? "png").trim().toLowerCase() || "png";
                        const format = formatRaw === "jpeg" ? "jpeg" : "png";
                        const subdir = String((config as any).subdir ?? "pipelines").trim() || "pipelines";
                        const keepData = Boolean((config as any).keep_data ?? false);

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Artifacts</span>
                              <CreatableSelect<SelectOption, true>
                                isMulti
                                styles={pipelinesReactSelectStyles}
                                options={ARTIFACT_SUGGESTIONS}
                                value={selectedArtifactOptions}
                                placeholder="Full frame"
                                onChange={(value: MultiValue<SelectOption>) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, subdir: nextValue }));
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, format: nextValue === "jpeg" ? "jpeg" : "png" }));
                                }}
                              >
                                <option value="png">PNG</option>
                                <option value="jpeg">JPEG</option>
                              </select>
                            </label>

                            <label className="pipelinesLabel">
                              <span>Keep data in memory</span>
                              <input
                                type="checkbox"
                                checked={keepData}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, keep_data: event.target.checked }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">If disabled, pixel data is dropped after storing to keep memory stable.</div>
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {step.operatorId === "core.notify" ? (
                    <div className="pipelinesOperatorConfigCard">
                      {(() => {
                        const title = String((config as any).title ?? "").trim();
                        const description = String((config as any).description ?? "").trim();
                        const priority = String((config as any).priority ?? "medium").trim().toLowerCase() || "medium";
                        const realtime = Boolean((config as any).realtime ?? true);
                        const updateIntervalSecondsRaw = Number((config as any).update_interval_seconds ?? 1.0);
                        const updateIntervalSeconds = Number.isFinite(updateIntervalSecondsRaw) ? Math.max(0, Math.min(60, updateIntervalSecondsRaw)) : 1.0;
                        const notificationType = String((config as any).notification_type ?? "pipelines.event").trim() || "pipelines.event";
                        const dedupeKeyTemplate = String((config as any).dedupe_key_template ?? "").trim();

                        return (
                          <>
                            <label className="pipelinesLabel">
                              <span>Title template</span>
                              <input
                                className="pipelinesInput"
                                type="text"
                                value={title}
                                placeholder="{{object_category_label}} detected"
                                onChange={(event) => {
                                  const nextValue = String(event.target.value ?? "");
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, title: nextValue }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">
                              Use templates like <code>{"{{object_category_label}}"}</code>, <code>{"{{area_label}}"}</code>,{" "}
                              <code>{"{{pose_label}}"}</code>.
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, description: nextValue }));
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, priority: nextPriority }));
                                }}
                              >
                                <option value="low">Low</option>
                                <option value="medium">Medium</option>
                                <option value="high">High</option>
                              </select>
                            </label>

                            <label className="pipelinesLabel">
                              <span>Realtime updates</span>
                              <input
                                type="checkbox"
                                checked={realtime}
                                onChange={(event) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, realtime: event.target.checked }));
                                }}
                              />
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
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
                                    ...prev,
                                    update_interval_seconds: Number.isFinite(nextValue) ? Math.max(0, Math.min(60, nextValue)) : 1.0,
                                  }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">
                              Avoids spamming UI updates while an event is open. Set to 0 to emit every change.
                            </div>

                            <label className="pipelinesLabel">
                              <span>Thumbnail fallback</span>
                              <CreatableSelect<SelectOption, true>
                                isMulti
                                styles={pipelinesReactSelectStyles}
                                options={ARTIFACT_SUGGESTIONS}
                                value={notifySelectedFallbackOptions}
                                placeholder="Best frame → Face → Segmented → Full frame"
                                onChange={(value: MultiValue<SelectOption>) => {
                                  updateInteractiveStepConfig(step.uid, (prev) => ({
                                    ...prev,
                                    thumbnail_with_fallback: value.map((item) => item.value),
                                  }));
                                }}
                              />
                            </label>
                            <div className="pipelinesStepHint">
                              Registers notifications only (never stores images). To include images, add Store Images before this step.
                            </div>

                            {step.showAdvanced ? (
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
                                      updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, notification_type: nextType }));
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
                                      updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, dedupe_key_template: nextValue }));
                                    }}
                                  />
                                </label>
                                <div className="pipelinesStepHint">
                                  Use templates like <code>{"{{tracking_id}}"}</code>, <code>{"{{camera_id}}"}</code>,{" "}
                                  <code>{"{{object_category_label}}"}</code>.
                                </div>
                              </>
                            ) : null}
                          </>
                        );
                      })()}
                    </div>
                  ) : null}

                  {shouldShowScalarGrid ? (
                    <div className="pipelinesScalarGrid">
                      {scalarEntries.map(([key, value]) => (
                        <label key={`${step.uid}:${key}`} className="pipelinesLabel pipelinesScalarLabel">
                          <span>{prettyConfigKeyLabel(key)}</span>
                          {typeof value === "boolean" ? (
                            <input
                              type="checkbox"
                              checked={value}
                              onChange={(event) => updateInteractiveStepScalar(step.uid, key, event.target.checked)}
                            />
                          ) : typeof value === "number" ? (
                            <input
                              className="pipelinesInput"
                              type="number"
                              value={Number.isFinite(value) ? String(value) : "0"}
                              onChange={(event) => updateInteractiveStepScalar(step.uid, key, Number(event.target.value || 0))}
                            />
                          ) : (
                            <input
                              className="pipelinesInput"
                              type="text"
                              value={String(value)}
                              onChange={(event) => updateInteractiveStepScalar(step.uid, key, event.target.value)}
                            />
                          )}
                        </label>
                      ))}
                    </div>
                  ) : null}

                  {shouldShowConfigJson ? (
                    <div className="pipelinesOperatorConfigCard">
                      <label className="pipelinesLabel">
                        <span>Config (JSON)</span>
                        <textarea
                          className="pipelinesTextArea"
                          value={step.configText}
                          rows={10}
                          placeholder="{ }"
                          onChange={(event) => updateInteractiveStep(step.uid, { configText: event.target.value })}
                        />
                      </label>
                      <div className="pipelinesStepHint">Use Advanced only when needed; most fields should be inferred from previous steps.</div>
                    </div>
                  ) : null}

                  {configObjectError ? <div className="pipelinesInlineError">{configObjectError}</div> : null}
                </div>
              ) : null}
            </div>
          );
        })}

        {interactiveSteps.length === 0 ? (
          <div className="card">
            <div className="cardBody">No steps yet. Add operators to build the pipeline chain.</div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
