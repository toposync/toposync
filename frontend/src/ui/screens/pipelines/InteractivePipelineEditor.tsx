import React, { useCallback, useMemo, useState } from "react";
import type { PipelineOperatorPanel } from "@toposync/plugin-api";

import type { CamerasIndexResponse, PipelineAlert, PipelineOperatorDefinition } from "../../../util/api";
import { cleanupPipelineStorage } from "../../../util/api";
import { i18n } from "../../../util/i18n";

import { NODE_ID_RE, PIPELINE_PRESET_OPERATOR_IDS } from "./constants";
import type { DragInsertPosition, InteractiveBuildResult, InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "./types";
import { createInteractiveStep, isRecord, jsonPretty, moveStep, prettyOperatorName, safeJsonParse } from "./utils";
import {
  bytesToGiBValue,
  findStorageLayerForNode,
  formatStorageBytes,
  formatStorageTime,
  giBToBytes,
  loadCachedPipelineStorage,
  updatePipelineStorageCache,
} from "./storageMetrics";

import { InteractiveStepsList } from "./editor/InteractiveStepsList";
import { InteractiveStepsToolbar } from "./editor/InteractiveStepsToolbar";
import { useCameraContexts } from "./editor/useCameraContexts";
import { PipelinesNumberInput } from "./editor/PipelinesNumberInput";

type Props = {
  operatorsById: Record<string, PipelineOperatorDefinition>;
  camerasIndex: CamerasIndexResponse;
  pipelineName: string | null;
  processingServerId: string;
  onOpenProcessingServers?: () => void;
  stepOutputsByNodeId: Record<string, number> | null;
  interactiveSteps: InteractiveStep[];
  setInteractiveSteps: React.Dispatch<React.SetStateAction<InteractiveStep[]>>;
  interactiveWarning: string | null;
  setInteractiveWarning: React.Dispatch<React.SetStateAction<string | null>>;
  interactiveGraph: InteractiveBuildResult;
  pipelineAlerts?: PipelineAlert[];
  operatorPanels?: Record<string, PipelineOperatorPanel>;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
};

function severityRank(severity: PipelineAlert["severity"]): number {
  if (severity === "error") return 3;
  if (severity === "warning") return 2;
  return 1;
}

function alertToneClass(severity: PipelineAlert["severity"]): string {
  if (severity === "error") return "isError";
  if (severity === "warning") return "isWarning";
  return "isInfo";
}

type PipelineStorageCardProps = {
  pipelineName: string | null;
  limitBytes: number | null;
  onUpdateLimitBytes: (value: number | null) => void;
  steps: InteractiveStep[];
};

export function PipelineStorageCard({
  pipelineName,
  limitBytes,
  onUpdateLimitBytes,
  steps,
}: PipelineStorageCardProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const [storage, setStorage] = useState<Awaited<ReturnType<typeof loadCachedPipelineStorage>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);

  React.useEffect(() => {
    if (!pipelineName) {
      setStorage(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void loadCachedPipelineStorage(pipelineName, {
      force: refreshNonce > 0,
      signal: controller.signal,
    })
      .then((summary) => {
        if (controller.signal.aborted) return;
        setStorage(summary);
      })
      .catch((err: any) => {
        if (controller.signal.aborted) return;
        setStorage(null);
        setError(String(err?.message ?? err));
      })
      .finally(() => {
        if (controller.signal.aborted) return;
        setLoading(false);
      });
    return () => controller.abort();
  }, [pipelineName, refreshNonce]);

  const effectiveLimitBytes = limitBytes ?? storage?.limit_bytes ?? null;
  const usedBytes = Number(storage?.used_bytes ?? 0);
  const usageRatio = effectiveLimitBytes && effectiveLimitBytes > 0 ? Math.min(1, usedBytes / effectiveLimitBytes) : 0;
  const displayLimitGiB = bytesToGiBValue(limitBytes ?? storage?.limit_bytes ?? 0);
  const isLowDisk = Boolean(storage && Number(storage.free_bytes || 0) < Number(storage.min_free_bytes || 0));
  const layers = Array.isArray(storage?.layers) ? storage.layers : [];
  const storeNodeIds = new Set(steps.filter((step) => step.operatorId === "core.store_images").map((step) => step.nodeId));
  const visibleLayers = layers.length > 0
    ? layers
    : [...storeNodeIds].map((nodeId) => ({
        layer_key: nodeId,
        layer_label: nodeId,
        node_id: nodeId,
        artifact_name: "",
        used_bytes: 0,
        limit_bytes: null,
        file_count: 0,
        avg_file_bytes: 0,
        oldest_at: 0,
        newest_at: 0,
        over_limit: false,
      }));

  const runCleanup = async () => {
    if (!pipelineName) return;
    setCleaning(true);
    setError(null);
    try {
      const summary = await cleanupPipelineStorage(pipelineName);
      updatePipelineStorageCache(summary);
      setStorage(summary);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setCleaning(false);
    }
  };

  return (
    <div className="pipelinesStorageCard">
      <div className="pipelinesStorageHeader">
        <div>
          <div className="pipelinesStorageTitle">{t("core.ui.pipelines.storage.title", {}, "Storage")}</div>
          <div className="pipelinesStepHint">
            {t("core.ui.pipelines.storage.subtitle", {}, "Managed retention for files written by Store Images.")}
          </div>
        </div>
        <div className="pipelinesStorageActions">
          <button className="iconButton" type="button" onClick={() => setRefreshNonce((prev) => prev + 1)} title={t("core.actions.refresh", {}, "Refresh")}>
            <i className="fa-solid fa-rotate" aria-hidden="true" />
          </button>
          <button className="pillButton" type="button" onClick={() => void runCleanup()} disabled={!pipelineName || cleaning}>
            <i className="fa-solid fa-broom" aria-hidden="true" />
            {cleaning ? t("core.ui.pipelines.storage.cleaning", {}, "Cleaning") : t("core.ui.pipelines.storage.clean_now", {}, "Clean now")}
          </button>
        </div>
      </div>

      <div className="pipelinesStorageUsageRow">
        <div className="pipelinesStorageUsageMain">
          <div className="pipelinesStorageUsageText">
            <span>{formatStorageBytes(usedBytes)}</span>
            <span>{effectiveLimitBytes ? `/ ${formatStorageBytes(effectiveLimitBytes)}` : ""}</span>
          </div>
          <div className="pipelinesStorageBar" aria-hidden="true">
            <div
              className={["pipelinesStorageBarFill", storage?.over_limit ? "isWarn" : ""].filter(Boolean).join(" ")}
              style={{ width: `${Math.round(usageRatio * 100)}%` }}
            />
          </div>
        </div>
        <label className="pipelinesStorageLimitField">
          <span>{t("core.ui.pipelines.storage.limit_gib", {}, "Pipeline budget (GiB)")}</span>
          <PipelinesNumberInput
            className="pipelinesInput"
            min={0}
            max={4096}
            step={0.25}
            value={displayLimitGiB}
            onChange={(nextValue) => {
              onUpdateLimitBytes(giBToBytes(nextValue));
            }}
          />
        </label>
      </div>

      {loading ? <div className="pipelinesStepHint">{t("core.ui.loading")}</div> : null}
      {error ? <div className="pipelinesStorageNotice isDanger">{error}</div> : null}
      {storage?.over_limit ? (
        <div className="pipelinesStorageNotice isWarn">{t("core.ui.pipelines.storage.over_limit", {}, "Usage is above the current budget. Retention will remove older files first.")}</div>
      ) : null}
      {isLowDisk ? (
        <div className="pipelinesStorageNotice isDanger">
          {t("core.ui.pipelines.storage.low_disk", {}, "Disk free space is below the configured safety margin. New images may be skipped.")}
        </div>
      ) : null}

      <div className="pipelinesStorageLayerList">
        {visibleLayers.length > 0 ? (
          visibleLayers.map((layer) => {
            const layerUsed = Number(layer.used_bytes || 0);
            const layerLimit = layer.limit_bytes == null ? null : Number(layer.limit_bytes || 0);
            const layerRatio = layerLimit && layerLimit > 0 ? Math.min(1, layerUsed / layerLimit) : 0;
            const matchedLayer = findStorageLayerForNode(storage, layer.node_id, layer.layer_label);
            const newestAt = matchedLayer?.newest_at ?? layer.newest_at;
            return (
              <div key={layer.layer_key} className="pipelinesStorageLayerRow">
                <div className="pipelinesStorageLayerMain">
                  <div className="pipelinesStorageLayerName">{layer.layer_label || layer.node_id}</div>
                  <div className="pipelinesStorageLayerMeta">
                    {formatStorageBytes(layerUsed)}
                    {" · "}
                    {t("core.ui.pipelines.storage.files", { count: layer.file_count }, `${layer.file_count} files`)}
                    {" · "}
                    {t("core.ui.pipelines.storage.avg", {}, "avg")} {formatStorageBytes(layer.avg_file_bytes)}
                    {newestAt ? ` · ${formatStorageTime(newestAt, locale)}` : ""}
                  </div>
                  {layerLimit ? (
                    <div className="pipelinesStorageMiniBar" aria-hidden="true">
                      <div className={["pipelinesStorageMiniBarFill", layer.over_limit ? "isWarn" : ""].filter(Boolean).join(" ")} style={{ width: `${Math.round(layerRatio * 100)}%` }} />
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })
        ) : (
          <div className="pipelinesStepHint">{t("core.ui.pipelines.storage.empty", {}, "No stored files yet.")}</div>
        )}
      </div>
    </div>
  );
}

export function InteractivePipelineEditor({
  operatorsById,
  camerasIndex,
  pipelineName,
  processingServerId,
  onOpenProcessingServers,
  stepOutputsByNodeId,
  interactiveSteps,
  setInteractiveSteps,
  interactiveWarning,
  setInteractiveWarning,
  interactiveGraph,
  pipelineAlerts = [],
  operatorPanels = {},
  onOpenTelemetryField,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
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
    () => {
      const seen = new Set<string>();
      const out: PipelineOperatorDefinition[] = [];
      for (const id of PIPELINE_PRESET_OPERATOR_IDS) {
        const operator = operatorsById[id];
        if (!operator) continue;
        seen.add(id);
        out.push(operator);
      }
      for (const id of Object.keys(operatorPanels).sort()) {
        const operator = operatorsById[id];
        if (!operator || seen.has(id)) continue;
        seen.add(id);
        out.push(operator);
      }
      return out;
    },
    [operatorPanels, operatorsById],
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

  const localStepAlerts = useMemo<PipelineAlert[]>(() => {
    const alerts: PipelineAlert[] = [];
    const firstStepByNodeId = new Map<string, InteractiveStep>();
    interactiveSteps.forEach((step, index) => {
      const stepLabel = `${index + 1}. ${prettyOperatorName(step.operatorId)}`;
      const operatorId = String(step.operatorId || "").trim();
      if (!operatorId || !operatorsById[operatorId]) {
        alerts.push({
          severity: "error",
          code: "interactive_unknown_operator",
          message: t(
            "core.ui.pipelines.checks.unknown_operator",
            { step: stepLabel, operator: operatorId || "" },
            `Step ${stepLabel} uses an unknown operator.`,
          ),
          node_id: step.nodeId || null,
          operator_id: operatorId || null,
        });
      }

      const nodeId = String(step.nodeId || "").trim();
      if (!nodeId) {
        alerts.push({
          severity: "error",
          code: "interactive_missing_node_id",
          message: t("core.ui.pipelines.checks.missing_node_id", { step: stepLabel }, `Step ${stepLabel} needs a node id.`),
          node_id: null,
          operator_id: operatorId || null,
        });
      } else if (!NODE_ID_RE.test(nodeId)) {
        alerts.push({
          severity: "error",
          code: "interactive_invalid_node_id",
          message: t(
            "core.ui.pipelines.checks.invalid_node_id",
            { step: stepLabel, node_id: nodeId },
            `Step ${stepLabel} has an invalid node id.`,
          ),
          suggestion: t(
            "core.ui.pipelines.checks.invalid_node_id_suggestion",
            {},
            "Use letters, numbers, and underscores, starting with a letter or underscore.",
          ),
          node_id: nodeId,
          operator_id: operatorId || null,
        });
      } else if (firstStepByNodeId.has(nodeId)) {
        alerts.push({
          severity: "error",
          code: "interactive_duplicate_node_id",
          message: t(
            "core.ui.pipelines.checks.duplicate_node_id",
            { step: stepLabel, node_id: nodeId },
            `Step ${stepLabel} duplicates node id '${nodeId}'.`,
          ),
          suggestion: t("core.ui.pipelines.checks.duplicate_node_id_suggestion", {}, "Give each step a unique node id."),
          node_id: nodeId,
          operator_id: operatorId || null,
        });
      } else {
        firstStepByNodeId.set(nodeId, step);
      }

      const parsed = safeJsonParse(step.configText || "{}");
      if (!parsed.ok) {
        alerts.push({
          severity: "error",
          code: "interactive_invalid_config_json",
          message: t(
            "core.ui.pipelines.checks.invalid_config_json",
            { step: stepLabel, error: parsed.error },
            `Step ${stepLabel} has invalid config JSON: ${parsed.error}`,
          ),
          node_id: nodeId || null,
          operator_id: operatorId || null,
        });
      } else if (!isRecord(parsed.data)) {
        alerts.push({
          severity: "error",
          code: "interactive_config_must_be_object",
          message: t(
            "core.ui.pipelines.checks.config_must_be_object",
            { step: stepLabel },
            `Step ${stepLabel} config must be a JSON object.`,
          ),
          node_id: nodeId || null,
          operator_id: operatorId || null,
        });
      }
    });
    return alerts;
  }, [interactiveSteps, operatorsById, t]);

  const visibleAlerts = useMemo(() => {
    const seen = new Set<string>();
    const combined = [...localStepAlerts, ...pipelineAlerts].filter((alert) => {
      const message = String(alert.message || "").trim();
      if (!message) return false;
      const key = [
        alert.severity,
        alert.code,
        alert.node_id ?? "",
        alert.operator_id ?? "",
        message,
      ].join("\u0000");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const stepIndexByNodeId = new Map(interactiveSteps.map((step, index) => [step.nodeId, index]));
    return combined.sort((a, b) => {
      const severityDelta = severityRank(b.severity) - severityRank(a.severity);
      if (severityDelta) return severityDelta;
      const aIndex = stepIndexByNodeId.get(String(a.node_id || "")) ?? Number.MAX_SAFE_INTEGER;
      const bIndex = stepIndexByNodeId.get(String(b.node_id || "")) ?? Number.MAX_SAFE_INTEGER;
      if (aIndex !== bIndex) return aIndex - bIndex;
      return String(a.message || "").localeCompare(String(b.message || ""));
    });
  }, [interactiveSteps, localStepAlerts, pipelineAlerts]);

  const alertsByNodeId = useMemo(() => {
    const map = new Map<string, PipelineAlert[]>();
    for (const alert of visibleAlerts) {
      const nodeId = String(alert.node_id || "").trim();
      if (!nodeId) continue;
      map.set(nodeId, [...(map.get(nodeId) ?? []), alert]);
    }
    return map;
  }, [visibleAlerts]);

  const focusAlertStep = useCallback(
    (alert: PipelineAlert) => {
      const nodeId = String(alert.node_id || "").trim();
      if (!nodeId) return;
      const step = interactiveSteps.find((item) => item.nodeId === nodeId);
      if (!step) return;
      setInteractiveSteps((prev) => prev.map((item) => (item.uid === step.uid ? { ...item, collapsed: false } : item)));
      window.setTimeout(() => {
        const el = document.querySelector(`[data-pipeline-step-uid="${step.uid}"]`);
        if (el instanceof HTMLElement) {
          el.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      }, 0);
    },
    [interactiveSteps, setInteractiveSteps],
  );

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

  const moveInteractiveStep = useCallback(
    (uid: string, direction: "up" | "down") => {
      setInteractiveSteps((prev) => {
        const currentIndex = prev.findIndex((step) => step.uid === uid);
        if (currentIndex < 0) return prev;
        const targetIndex = direction === "up" ? currentIndex - 1 : currentIndex + 1;
        if (targetIndex < 0 || targetIndex >= prev.length) return prev;

        const next = prev.slice();
        const [step] = next.splice(currentIndex, 1);
        next.splice(targetIndex, 0, step);
        return next;
      });
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

  const insertInteractiveStepAfter = useCallback(
    (afterUid: string, operatorId: string, defaultsOverride?: Record<string, unknown>) => {
      const operator = operatorsById[operatorId];
      if (!operator) return;
      setInteractiveSteps((prev) => {
        const used = new Set(prev.map((item) => item.nodeId));
        const next = createInteractiveStep(
          operatorId,
          { ...(operator.defaults ?? {}), ...(defaultsOverride ?? {}) },
          used,
        );
        const targetIndex = prev.findIndex((item) => item.uid === afterUid);
        if (targetIndex < 0) return [...prev, next];
        const copy = prev.slice();
        copy.splice(targetIndex + 1, 0, next);
        return copy;
      });
      setInteractiveWarning(null);
    },
    [operatorsById, setInteractiveSteps, setInteractiveWarning],
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

      {visibleAlerts.length > 0 ? (
        <div className="card pipelinesChecksCard">
          <div className="cardTitle">{t("core.ui.pipelines.checks.title", {}, "Pipeline checks")}</div>
          <div className="cardBody">
            <div className="pipelinesAlerts">
              {visibleAlerts.map((alert, index) => {
                const nodeId = String(alert.node_id || "").trim();
                const step = nodeId ? interactiveSteps.find((item) => item.nodeId === nodeId) : null;
                const stepLabel = step
                  ? `${interactiveSteps.indexOf(step) + 1}. ${prettyOperatorName(step.operatorId)}`
                  : t("core.ui.pipelines.checks.pipeline_scope", {}, "Pipeline");
                const canFocus = Boolean(step);
                return (
                  <button
                    key={`${alert.code}:${nodeId}:${index}`}
                    className={["pipelinesAlertRow", "pipelinesCheckRow", alertToneClass(alert.severity)].join(" ")}
                    type="button"
                    disabled={!canFocus}
                    onClick={() => focusAlertStep(alert)}
                    title={canFocus ? t("core.ui.pipelines.checks.open_step", {}, "Open step") : undefined}
                  >
                    <div className="pipelinesAlertBadge">{alert.severity}</div>
                    <div className="pipelinesAlertText">
                      <div className="pipelinesAlertMessage">{alert.message}</div>
                      {alert.suggestion ? <div className="pipelinesAlertSuggestion">{alert.suggestion}</div> : null}
                      <div className="pipelinesHint">{stepLabel}</div>
                    </div>
                    {canFocus ? (
                      <div className="pipelinesCheckOpenIcon">
                        <i className="fa-solid fa-arrow-turn-down" aria-hidden="true" />
                      </div>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      ) : null}

      <InteractiveStepsList
        steps={interactiveSteps}
        operatorsById={operatorsById}
        pipelineName={pipelineName}
        processingServerId={processingServerId}
        onOpenProcessingServers={onOpenProcessingServers}
        interactiveCameraId={interactiveCameraId}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        activeCameraContexts={activeCameraContexts}
        activeCameraContextsError={activeCameraContextsError}
        cameraAreaOptions={cameraAreaOptions}
        stepOutputsByNodeId={stepOutputsByNodeId}
        alertsByNodeId={alertsByNodeId}
        operatorPanels={operatorPanels}
        draggingStepUid={draggingStepUid}
        dragOverStep={dragOverStep}
        onBeginDrag={beginStepDrag}
        onEndDrag={endStepDrag}
        onDragOver={updateStepDragOver}
        onDrop={dropStep}
        onUpdateStep={updateInteractiveStep}
        onRemoveStep={removeInteractiveStep}
        onMoveStep={moveInteractiveStep}
        onUpdateStepScalar={updateInteractiveStepScalar}
        onUpdateStepConfig={updateInteractiveStepConfig}
        onInsertStepAfter={insertInteractiveStepAfter}
        onOpenTelemetryField={onOpenTelemetryField}
      />
    </div>
  );
}
