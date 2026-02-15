import React from "react";

import type { CameraContextsResponse, PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";

import type { DragInsertPosition, InteractiveStep, SelectOption } from "../types";
import { humanizeIdentifier, isRecord, prettyConfigKeyLabel, prettyOperatorName, safeJsonParse } from "../utils";

import { OperatorConfigPanel } from "./panels/OperatorConfigPanel";

type Props = {
  step: InteractiveStep;
  index: number;
  steps: InteractiveStep[];
  operatorsById: Record<string, PipelineOperatorDefinition>;

  interactiveCameraId: string;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: SelectOption[];
  stepOutputsByNodeId: Record<string, number> | null;

  draggingStepUid: string | null;
  dragOverStep: { uid: string; position: DragInsertPosition } | null;

  onBeginDrag: (event: React.DragEvent, uid: string) => void;
  onEndDrag: () => void;
  onDragOver: (event: React.DragEvent<HTMLElement>, uid: string) => void;
  onDrop: (event: React.DragEvent<HTMLElement>, uid: string) => void;

  onUpdateStep: (uid: string, patch: Partial<InteractiveStep>) => void;
  onRemoveStep: (uid: string) => void;
  onUpdateStepScalar: (uid: string, key: string, value: string | number | boolean) => void;
  onUpdateStepConfig: (uid: string, updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;
};

function shouldHideScalarGrid(operatorId: string): boolean {
  return (
    operatorId === "core.schedule_gate" ||
    operatorId === "camera.source" ||
    operatorId === "camera.image_crop" ||
    operatorId === "camera.image_adjust" ||
    operatorId === "camera.image_resize" ||
    operatorId === "camera.camera_mapping" ||
    operatorId === "camera.area_restriction" ||
    operatorId === "camera.velocity_estimation" ||
    operatorId === "core.throttle" ||
    operatorId === "core.debounce" ||
    operatorId === "core.debug" ||
    operatorId === "core.notify" ||
    operatorId === "core.store_images" ||
    operatorId === "core.category_gate" ||
    operatorId === "core.filter" ||
    operatorId === "vision.object_tracking_yolo" ||
    operatorId === "vision.object_detection_yolo"
  );
}

export function InteractiveStepCard({
  step,
  index,
  steps,
  operatorsById,
  interactiveCameraId,
  cameraSelectOptions,
  cameraSelectOptionById,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  stepOutputsByNodeId,
  draggingStepUid,
  dragOverStep,
  onBeginDrag,
  onEndDrag,
  onDragOver,
  onDrop,
  onUpdateStep,
  onRemoveStep,
  onUpdateStepScalar,
  onUpdateStepConfig,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const operator = operatorsById[step.operatorId];

  const configParsed = safeJsonParse(step.configText || "{}");
  const configRecordOk = configParsed.ok && isRecord(configParsed.data);
  const config = configRecordOk ? (configParsed.data as Record<string, unknown>) : {};
  const configObjectError = !configParsed.ok
    ? t("core.ui.pipelines.editor.step.invalid_config_json", { error: configParsed.error })
    : !configRecordOk
      ? t("core.ui.pipelines.editor.step.config_must_be_object")
      : null;

  const scalarEntries = Object.entries(config)
    .filter(([, value]) => {
      const valueType = typeof value;
      return valueType === "string" || valueType === "number" || valueType === "boolean";
    })
    .filter(([key]) => {
      if (step.operatorId === "core.notify") {
        return !["title", "description", "priority", "realtime", "update_interval_seconds", "notification_type", "dedupe_key_template"].includes(key);
      }
      return true;
    });

  const shouldShowScalarGrid = scalarEntries.length > 0 && (!shouldHideScalarGrid(step.operatorId) || step.showAdvanced);
  const shouldShowConfigJson = step.showAdvanced;

  const rowClass = ["pipelinesStepCard"];
  if (draggingStepUid === step.uid) rowClass.push("isDragSource");
  if (dragOverStep?.uid === step.uid) {
    rowClass.push(dragOverStep.position === "before" ? "isDropBefore" : "isDropAfter");
  }

  const operatorName = operator ? prettyOperatorName(operator.id) : prettyOperatorName(step.operatorId);
  const stepIndexLabel = `${index + 1}.`;
  const stepOutputCount = stepOutputsByNodeId ? Number(stepOutputsByNodeId[step.nodeId] ?? 0) : null;

  return (
    <div
      className={rowClass.join(" ")}
      draggable
      onDragStart={(event) => onBeginDrag(event, step.uid)}
      onDragEnd={onEndDrag}
      onDragOver={(event) => onDragOver(event, step.uid)}
      onDrop={(event) => onDrop(event, step.uid)}
    >
      <div className="pipelinesStepHeader">
        <div className="pipelinesStepHeaderMain">
          <div className="pipelinesStepIndex">{stepIndexLabel}</div>
          <div className="pipelinesStepTitle">{operatorName}</div>
          {stepOutputCount !== null ? (
            <div className="pipelinesStepStatBadge" title={t("core.ui.pipelines.stats.step.outputs_tooltip")}>
              <i className="fa-solid fa-arrow-right" aria-hidden="true" />
              {stepOutputCount.toLocaleString()}
            </div>
          ) : null}
        </div>

        <div className="pipelinesStepHeaderActions">
          <button
            className="iconButton"
            type="button"
            onClick={() => onUpdateStep(step.uid, { collapsed: !step.collapsed })}
            title={step.collapsed ? t("core.ui.pipelines.editor.step.expand") : t("core.ui.pipelines.editor.step.collapse")}
          >
            <i className={step.collapsed ? "fa-solid fa-chevron-down" : "fa-solid fa-chevron-up"} aria-hidden="true" />
          </button>

          <button
            className={["iconButton", step.showAdvanced ? "isActive" : ""].filter(Boolean).join(" ")}
            type="button"
            onClick={() => onUpdateStep(step.uid, { showAdvanced: !step.showAdvanced })}
            title={
              step.showAdvanced ? t("core.ui.pipelines.editor.step.hide_advanced") : t("core.ui.pipelines.editor.step.show_advanced")
            }
          >
            <i className="fa-solid fa-sliders" aria-hidden="true" />
          </button>

          <button
            className="iconButton"
            type="button"
            onClick={() => onRemoveStep(step.uid)}
            title={t("core.ui.pipelines.editor.step.remove")}
          >
            <i className="fa-solid fa-trash" aria-hidden="true" />
          </button>
        </div>
      </div>

      {!step.collapsed ? (
        <div className="pipelinesStepBody">
          {operator ? <div className="pipelinesStepDescription">{operator.description || prettyOperatorName(operator.id)}</div> : null}
          {operator && operator.capabilities.length > 0 && step.showAdvanced ? (
            <div className="pipelinesStepCapabilities">
              {t("core.ui.pipelines.editor.step.capabilities_prefix")}{" "}
              {operator.capabilities.map((cap) => humanizeIdentifier(cap) || cap).join(", ")}
            </div>
          ) : null}

          {step.showAdvanced ? (
            <div className="pipelinesOperatorConfigCard">
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.editor.step.step_id")}</span>
                <input
                  className="pipelinesInput"
                  value={step.nodeId}
                  onChange={(event) => onUpdateStep(step.uid, { nodeId: event.target.value })}
                  placeholder={t("core.ui.pipelines.editor.step.step_id_placeholder")}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.editor.step.step_id_hint")}</div>
            </div>
          ) : null}

          <OperatorConfigPanel
            step={step}
            index={index}
            steps={steps}
            config={config}
            interactiveCameraId={interactiveCameraId}
            cameraSelectOptions={cameraSelectOptions}
            cameraSelectOptionById={cameraSelectOptionById}
            activeCameraContexts={activeCameraContexts}
            activeCameraContextsError={activeCameraContextsError}
            cameraAreaOptions={cameraAreaOptions}
            showAdvanced={step.showAdvanced}
            onUpdateConfig={(updater) => onUpdateStepConfig(step.uid, updater)}
          />

          {shouldShowScalarGrid ? (
            <div className="pipelinesScalarGrid">
              {scalarEntries.map(([key, value]) => (
                <label key={`${step.uid}:${key}`} className="pipelinesLabel pipelinesScalarLabel">
                  <span>{prettyConfigKeyLabel(key)}</span>
                  {typeof value === "boolean" ? (
                    <input type="checkbox" checked={value} onChange={(event) => onUpdateStepScalar(step.uid, key, event.target.checked)} />
                  ) : typeof value === "number" ? (
                    <input
                      className="pipelinesInput"
                      type="number"
                      value={Number.isFinite(value) ? String(value) : "0"}
                      onChange={(event) => onUpdateStepScalar(step.uid, key, Number(event.target.value || 0))}
                    />
                  ) : (
                    <input className="pipelinesInput" type="text" value={String(value)} onChange={(event) => onUpdateStepScalar(step.uid, key, event.target.value)} />
                  )}
                </label>
              ))}
            </div>
          ) : null}

          {shouldShowConfigJson ? (
            <div className="pipelinesOperatorConfigCard">
              <label className="pipelinesLabel">
                <span>{t("core.ui.pipelines.editor.step.config_json")}</span>
                <textarea
                  className="pipelinesTextArea"
                  value={step.configText}
                  rows={10}
                  placeholder="{ }"
                  onChange={(event) => onUpdateStep(step.uid, { configText: event.target.value })}
                />
              </label>
              <div className="pipelinesStepHint">{t("core.ui.pipelines.editor.step.config_json_hint")}</div>
            </div>
          ) : null}

          {configObjectError ? <div className="pipelinesInlineError">{configObjectError}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
