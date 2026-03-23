import React, { useCallback, useMemo, useState } from "react";

import type { CamerasIndexResponse, PipelineOperatorDefinition } from "../../../util/api";

import { PIPELINE_PRESET_OPERATOR_IDS } from "./constants";
import type { DragInsertPosition, InteractiveBuildResult, InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "./types";
import { createInteractiveStep, isRecord, jsonPretty, moveStep, safeJsonParse } from "./utils";

import { InteractiveStepsList } from "./editor/InteractiveStepsList";
import { InteractiveStepsToolbar } from "./editor/InteractiveStepsToolbar";
import { useCameraContexts } from "./editor/useCameraContexts";

type Props = {
  operatorsById: Record<string, PipelineOperatorDefinition>;
  camerasIndex: CamerasIndexResponse;
  pipelineName: string | null;
  processingServerId: string;
  stepOutputsByNodeId: Record<string, number> | null;
  interactiveSteps: InteractiveStep[];
  setInteractiveSteps: React.Dispatch<React.SetStateAction<InteractiveStep[]>>;
  interactiveWarning: string | null;
  setInteractiveWarning: React.Dispatch<React.SetStateAction<string | null>>;
  interactiveGraph: InteractiveBuildResult;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
};

export function InteractivePipelineEditor({
  operatorsById,
  camerasIndex,
  pipelineName,
  processingServerId,
  stepOutputsByNodeId,
  interactiveSteps,
  setInteractiveSteps,
  interactiveWarning,
  setInteractiveWarning,
  interactiveGraph,
  onOpenTelemetryField,
}: Props): React.ReactElement {
  const [draggingStepUid, setDraggingStepUid] = useState<string | null>(null);
  const [dragOverStep, setDragOverStep] = useState<{ uid: string; position: DragInsertPosition } | null>(null);

  const isCameraSourceOperator = useCallback(
    (operatorId: string) => {
      const definition = operatorsById[operatorId];
      const capabilities = new Set((definition?.capabilities ?? []).map((value) => String(value || "").trim().toLowerCase()));
      return capabilities.has("source") && capabilities.has("camera");
    },
    [operatorsById],
  );

  const presetOperators = useMemo(
    () => PIPELINE_PRESET_OPERATOR_IDS.map((id) => operatorsById[id]).filter(Boolean) as PipelineOperatorDefinition[],
    [operatorsById],
  );

  const interactiveCameraId = useMemo(() => {
    const sourceStep = interactiveSteps.find((step) => isCameraSourceOperator(step.operatorId));
    if (!sourceStep) return "";
    const parsed = safeJsonParse(sourceStep.configText || "{}");
    if (!parsed.ok) return "";
    if (!isRecord(parsed.data)) return "";
    return String((parsed.data as any).camera_id ?? "").trim();
  }, [interactiveSteps, isCameraSourceOperator]);

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

  const { activeCameraContexts, activeCameraContextsError, cameraAreaOptions } = useCameraContexts(interactiveCameraId);

  const addInteractiveStep = useCallback(
    (operatorId: string) => {
      const operator = operatorsById[operatorId];
      if (!operator) return;
      setInteractiveSteps((prev) => {
        const used = new Set(prev.map((item) => item.nodeId));
        const next = createInteractiveStep(operatorId, operator.defaults ?? {}, used);
        if (operatorId === "core.schedule_gate") {
          const cameraIndex = prev.findIndex((item) => isCameraSourceOperator(item.operatorId));
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
    },
    [isCameraSourceOperator, operatorsById, setInteractiveSteps, setInteractiveWarning],
  );

  const updateInteractiveStep = useCallback(
    (uid: string, patch: Partial<InteractiveStep>) => {
      setInteractiveSteps((prev) => prev.map((step) => (step.uid === uid ? { ...step, ...patch } : step)));
    },
    [setInteractiveSteps],
  );

  const removeInteractiveStep = useCallback(
    (uid: string) => {
      setInteractiveSteps((prev) => prev.filter((step) => step.uid !== uid));
    },
    [setInteractiveSteps],
  );

  const updateInteractiveStepScalar = useCallback(
    (uid: string, key: string, value: string | number | boolean) => {
      setInteractiveSteps((prev) =>
        prev.map((step) => {
          if (step.uid !== uid) return step;
          const parsed = safeJsonParse(step.configText || "{}");
          const nextConfig = parsed.ok && isRecord(parsed.data) ? { ...(parsed.data as Record<string, unknown>) } : {};
          nextConfig[key] = value;
          return { ...step, configText: jsonPretty(nextConfig) };
        }),
      );
    },
    [setInteractiveSteps],
  );

  const updateInteractiveStepConfig = useCallback(
    (uid: string, updater: (config: Record<string, unknown>) => Record<string, unknown>) => {
      setInteractiveSteps((prev) =>
        prev.map((step) => {
          if (step.uid !== uid) return step;
          const parsed = safeJsonParse(step.configText || "{}");
          const base = parsed.ok && isRecord(parsed.data) ? { ...(parsed.data as Record<string, unknown>) } : {};
          const next = updater(base);
          return { ...step, configText: jsonPretty(next) };
        }),
      );
    },
    [setInteractiveSteps],
  );

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
      <InteractiveStepsToolbar
        presetOperators={presetOperators}
        onAddStep={addInteractiveStep}
      />

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

      <InteractiveStepsList
        steps={interactiveSteps}
        operatorsById={operatorsById}
        pipelineName={pipelineName}
        processingServerId={processingServerId}
        interactiveCameraId={interactiveCameraId}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        activeCameraContexts={activeCameraContexts}
        activeCameraContextsError={activeCameraContextsError}
        cameraAreaOptions={cameraAreaOptions}
        stepOutputsByNodeId={stepOutputsByNodeId}
        draggingStepUid={draggingStepUid}
        dragOverStep={dragOverStep}
        onBeginDrag={beginStepDrag}
        onEndDrag={endStepDrag}
        onDragOver={updateStepDragOver}
        onDrop={dropStep}
        onUpdateStep={updateInteractiveStep}
        onRemoveStep={removeInteractiveStep}
        onUpdateStepScalar={updateInteractiveStepScalar}
        onUpdateStepConfig={updateInteractiveStepConfig}
        onOpenTelemetryField={onOpenTelemetryField}
      />
    </div>
  );
}
