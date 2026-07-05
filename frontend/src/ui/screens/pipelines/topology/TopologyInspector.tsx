import type React from "react";
import { useEffect, useMemo, useState } from "react";
import type { PipelineOperatorPanel } from "@toposync/plugin-api";

import type { CameraContextsResponse, CamerasIndexResponse, PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";
import { localizePipelineAlert } from "../utils";
import type { CameraAreaOption, InteractiveStep, SelectOption, TelemetryFieldInspectorRequest } from "../types";
import { OperatorConfigPanel } from "../editor/panels/OperatorConfigPanel";
import type { TopologyEdgePolicyPatch } from "./topologyGraph";
import type { TopologyEdge, TopologyModel, TopologyNode, TopologyRuntimeStatus, TopologySelection } from "./topologyTypes";

type Props = {
  model: TopologyModel;
  selection: TopologySelection;
  runtimeStatus: TopologyRuntimeStatus;
  runtimeGeneratedAt: number | null;
  editable: boolean;
  pipelineName: string;
  processingServerId: string;
  onOpenProcessingServers?: () => void;
  operatorsById: Record<string, PipelineOperatorDefinition>;
  interactiveCameraId: string;
  camerasIndex: CamerasIndexResponse;
  cameraSelectOptions: SelectOption[];
  cameraSelectOptionById: Map<string, SelectOption>;
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: CameraAreaOption[];
  operatorPanels?: Record<string, PipelineOperatorPanel>;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
  onUpdateNodeConfig?: (nodeId: string, config: Record<string, unknown>) => void;
  onUpdateEdgePolicy?: (edgeId: string, patch: TopologyEdgePolicyPatch) => void;
  onDeleteNode?: (nodeId: string) => void;
  onDeleteEdge?: (edgeId: string) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
};

type NodeInspectorProps = Omit<
  Props,
  | "selection"
  | "runtimeStatus"
  | "runtimeGeneratedAt"
  | "onUpdateEdgePolicy"
  | "onDeleteEdge"
  | "collapsed"
  | "onToggleCollapsed"
> & {
  node: TopologyNode;
};

type Translate = ReturnType<typeof i18n.useI18n>["t"];

function valueText(value: unknown, t: Translate, fallback = t("core.ui.pipelines.topology.value.none", {}, "none")): string {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return fallback;
    return Math.abs(value) >= 100 ? String(Math.round(value)) : value.toFixed(2).replace(/\.?0+$/, "");
  }
  if (typeof value === "boolean") {
    return value
      ? t("core.ui.pipelines.topology.value.yes", {}, "yes")
      : t("core.ui.pipelines.topology.value.no", {}, "no");
  }
  return String(value);
}

function timestampText(value: number | null, t: Translate): string {
  if (!value || !Number.isFinite(value)) return t("core.ui.pipelines.topology.value.none", {}, "none");
  return new Date(value * 1000).toLocaleTimeString();
}

function edgeValueLabel(kind: "drop_policy" | "pressure_mode" | "modality" | "semantic", value: string, t: Translate): string {
  const normalized = String(value || "").trim().replace(/\./g, "_");
  if (!normalized) return t("core.ui.pipelines.topology.value.none", {}, "none");
  return t(`core.ui.pipelines.topology.${kind}.${normalized}`, {}, value);
}

function stepForNode(node: TopologyNode, showAdvanced: boolean): InteractiveStep {
  return {
    uid: node.data.uid || node.id,
    nodeId: node.data.nodeId,
    operatorId: node.data.operatorId,
    configText: JSON.stringify(node.data.config ?? {}, null, 2),
    collapsed: false,
    showAdvanced,
  };
}

function Field({ label, value }: { label: string; value: unknown }): React.ReactElement {
  const { t } = i18n.useI18n();
  const text = valueText(value, t);
  return (
    <div className="pipelineTopologyInspectorField">
      <span>{label}</span>
      <strong title={text}>{text}</strong>
    </div>
  );
}

function EdgeField({
  label,
  kind,
  value,
}: {
  label: string;
  kind: "drop_policy" | "pressure_mode" | "modality" | "semantic";
  value: string;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const text = edgeValueLabel(kind, value, t);
  return (
    <div className="pipelineTopologyInspectorField">
      <span>{label}</span>
      <strong title={value && value !== text ? value : text}>{text}</strong>
    </div>
  );
}

function Alerts({ alerts }: { alerts: TopologyNode["data"]["alerts"] }): React.ReactElement | null {
  const { t } = i18n.useI18n();
  if (!alerts.length) return null;
  return (
    <div className="pipelineTopologyInspectorSection">
      <div className="pipelineTopologyInspectorSectionTitle">{t("core.ui.pipelines.topology.alerts", {}, "Alerts")}</div>
      <div className="pipelineTopologyInspectorAlerts">
        {alerts.map((alert, index) => {
          const localized = localizePipelineAlert(alert, t);
          return (
            <div className="pipelineTopologyInspectorAlert" data-severity={alert.severity} key={`${alert.code}:${index}`}>
              <div className="pipelineTopologyInspectorAlertCode">{alert.code}</div>
              <div>{localized.message}</div>
              {localized.suggestion ? <small>{localized.suggestion}</small> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SummaryInspector({
  model,
  runtimeStatus,
  runtimeGeneratedAt,
  editable,
}: Pick<Props, "model" | "runtimeStatus" | "runtimeGeneratedAt" | "editable">): React.ReactElement {
  const { t } = i18n.useI18n();
  const field = (key: string, fallback: string) => t(`core.ui.pipelines.topology.field.${key}`, {}, fallback);
  const runtimeLabel = runtimeStatus.loading
    ? t("core.ui.pipelines.topology.runtime.loading", {}, "checking")
    : runtimeStatus.error
      ? t("core.ui.pipelines.topology.runtime.error", {}, "error")
      : !runtimeGeneratedAt
        ? t("core.ui.pipelines.topology.runtime.unavailable", {}, "unavailable")
        : runtimeStatus.stale
          ? t("core.ui.pipelines.topology.runtime.stale", {}, "stale")
          : t("core.ui.pipelines.topology.runtime.ready", {}, "ready");
  return (
    <>
      <div className="pipelineTopologyInspectorHeader">
        <div>
          <div className="pipelineTopologyInspectorTitle">{t("core.ui.pipelines.topology.summary", {}, "Topology")}</div>
          <div className="pipelineTopologyInspectorSubtitle">
            {editable
              ? t("core.ui.pipelines.topology.editable", {}, "Editable graph v2")
              : t("core.ui.pipelines.topology.read_only", {}, "Read-only graph view")}
          </div>
        </div>
      </div>
      <div className="pipelineTopologyInspectorGrid">
        <Field label={field("graph", "Graph")} value={model.summary.graphUid} />
        <Field label={field("nodes", "Nodes")} value={model.summary.nodeCount} />
        <Field label={field("edges", "Edges")} value={model.summary.edgeCount} />
        <Field label={field("sources", "Sources")} value={model.summary.sourceCount} />
        <Field label={field("sinks", "Sinks")} value={model.summary.sinkCount} />
        <Field label={field("runtime", "Runtime")} value={runtimeLabel} />
        <Field label={field("pressure", "Pressure")} value={model.summary.pressureActive ? model.summary.pressureCause : undefined} />
        <Field label={field("updated", "Updated")} value={timestampText(runtimeGeneratedAt, t)} />
      </div>
      {runtimeStatus.error ? <div className="pipelineTopologyInspectorNotice">{runtimeStatus.error}</div> : null}
    </>
  );
}

function JsonConfigEditor({
  node,
  editable,
  onApply,
}: {
  node: TopologyNode;
  editable: boolean;
  onApply?: (nodeId: string, config: Record<string, unknown>) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const [open, setOpen] = useState(false);
  const serialized = useMemo(() => JSON.stringify(node.data.config ?? {}, null, 2), [node.data.config]);
  const [text, setText] = useState(serialized);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setText(serialized);
    setError(null);
  }, [node.id, serialized]);

  const apply = () => {
    try {
      const parsed = JSON.parse(text) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setError(t("core.ui.pipelines.topology.config_object", {}, "Config must be a JSON object."));
        return;
      }
      setError(null);
      onApply?.(node.id, parsed as Record<string, unknown>);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  return (
    <details className="pipelineTopologyInspectorSection pipelineTopologyJsonDetails" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="pipelineTopologyInspectorSectionTitle">
        <i className="fa-solid fa-code" aria-hidden="true" />
        {t("core.ui.pipelines.topology.advanced_json", {}, "Advanced JSON")}
      </summary>
      <textarea
        className="pipelineTopologyJsonInput"
        id={`pipeline-topology-node-config-${node.id}`}
        name={`node_config_${node.id}`}
        value={text}
        readOnly={!editable}
        spellCheck={false}
        onChange={(event) => setText(event.target.value)}
      />
      {error ? <div className="pipelineTopologyInspectorNotice">{error}</div> : null}
      {editable ? (
        <button className="pillButton" type="button" onClick={apply} disabled={text === serialized}>
          <i className="fa-solid fa-check" aria-hidden="true" />
          {t("core.ui.pipelines.topology.apply_config", {}, "Apply config")}
        </button>
      ) : null}
    </details>
  );
}

function NodeInspector({
  model,
  node,
  editable,
  pipelineName,
  processingServerId,
  onOpenProcessingServers,
  operatorsById,
  interactiveCameraId,
  camerasIndex,
  cameraSelectOptions,
  cameraSelectOptionById,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  operatorPanels,
  onOpenTelemetryField,
  onUpdateNodeConfig,
  onDeleteNode,
}: NodeInspectorProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [insertError, setInsertError] = useState<string | null>(null);
  const field = (key: string, fallback: string) => t(`core.ui.pipelines.topology.field.${key}`, {}, fallback);
  const runtime = node.data.runtime;
  const steps = useMemo(
    () => model.nodes.map((item) => stepForNode(item, item.id === node.id ? showAdvanced : false)),
    [model.nodes, node.id, showAdvanced],
  );
  const stepIndex = steps.findIndex((item) => item.nodeId === node.data.nodeId);
  const step = stepIndex >= 0 ? steps[stepIndex] : stepForNode(node, showAdvanced);
  const index = Math.max(0, stepIndex);

  useEffect(() => {
    setShowAdvanced(false);
    setInsertError(null);
  }, [node.id]);

  const updateConfig = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => {
    if (!editable) return;
    const current = { ...(node.data.config ?? {}) };
    onUpdateNodeConfig?.(node.id, updater(current));
  };

  return (
    <>
      <div className="pipelineTopologyInspectorHeader">
        <div>
          <div className="pipelineTopologyInspectorTitle">{node.data.label}</div>
          <div className="pipelineTopologyInspectorSubtitle">{node.data.operatorId}</div>
        </div>
        {editable ? (
          <button className="pillButton pillButtonDanger" type="button" onClick={() => onDeleteNode?.(node.id)}>
            <i className="fa-solid fa-trash" aria-hidden="true" />
            {t("core.actions.delete")}
          </button>
        ) : null}
      </div>
      <div className="pipelineTopologyInspectorSection">
        <div className="pipelineTopologyInspectorSectionTitle pipelineTopologyInspectorSectionTitleRow">
          <span>{t("core.ui.pipelines.topology.config", {}, "Config")}</span>
          <button className="pillButton" type="button" onClick={() => setShowAdvanced((prev) => !prev)}>
            {t("core.ui.pipelines.topology.advanced", {}, "Advanced")}
          </button>
        </div>
        <div className={editable ? undefined : "pipelinesReadOnlyPanel"} aria-disabled={!editable || undefined}>
          <OperatorConfigPanel
            step={step}
            index={index}
            steps={steps}
            operatorsById={operatorsById}
            config={node.data.config ?? {}}
            pipelineName={pipelineName}
            processingServerId={processingServerId}
            onOpenProcessingServers={onOpenProcessingServers}
            interactiveCameraId={interactiveCameraId}
            camerasIndex={camerasIndex}
            cameraSelectOptions={cameraSelectOptions}
            cameraSelectOptionById={cameraSelectOptionById}
            activeCameraContexts={activeCameraContexts}
            activeCameraContextsError={activeCameraContextsError}
            cameraAreaOptions={cameraAreaOptions}
            operatorPanels={operatorPanels}
            showAdvanced={showAdvanced}
            onUpdateConfig={updateConfig}
            onInsertStepAfter={() => {
              setInsertError(
                t(
                  "core.ui.pipelines.topology.insert_unavailable",
                  {},
                  "Automatic insertion is not available in topology yet.",
                ),
              );
            }}
            onOpenTelemetryField={editable ? onOpenTelemetryField : undefined}
          />
        </div>
        {insertError ? <div className="pipelineTopologyInspectorNotice">{insertError}</div> : null}
      </div>
      <div className="pipelineTopologyInspectorGrid">
        <Field label={field("node_id", "Node ID")} value={node.data.nodeId} />
        <Field label={field("uid", "UID")} value={node.data.uid} />
        <Field label={field("runtime", "Runtime")} value={runtime?.runtime_state ?? runtime?.task_state} />
        <Field label={field("resource", "Resource")} value={node.data.resourceKind} />
        <Field label={field("pressure", "Pressure")} value={node.data.pressureState} />
        <Field label={field("behavior", "Behavior")} value={node.data.pressureBehavior} />
        <Field label={field("processed", "Processed")} value={runtime?.progress?.processed_packets} />
        <Field label={field("emitted", "Emitted")} value={runtime?.progress?.emitted_packets} />
        <Field label={field("dropped", "Dropped")} value={runtime?.progress?.dropped_packets} />
        <Field label={field("errors", "Errors")} value={runtime?.progress?.error_count} />
      </div>
      {runtime?.last_error ? (
        <div className="pipelineTopologyInspectorNotice">
          {t("core.ui.pipelines.topology.last_error", {}, "Last error")}: {runtime.last_error}
        </div>
      ) : null}
      <Alerts alerts={node.data.alerts} />
      <JsonConfigEditor node={node} editable={editable} onApply={onUpdateNodeConfig} />
    </>
  );
}

function EdgePolicyEditor({
  edge,
  editable,
  onUpdateEdgePolicy,
}: {
  edge: TopologyEdge;
  editable: boolean;
  onUpdateEdgePolicy?: (edgeId: string, patch: TopologyEdgePolicyPatch) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const field = (key: string, fallback: string) => t(`core.ui.pipelines.topology.field.${key}`, {}, fallback);
  const data = edge.data;
  if (!data || !editable) return null;
  const update = (patch: TopologyEdgePolicyPatch) => onUpdateEdgePolicy?.(edge.id, patch);
  const dropPolicyOptions = ["block", "latest_only", "drop_oldest", "drop_newest", "drop_updates", "keyed_latest_only"];
  const pressureModeOptions = ["ignore", "pause_upstream", "reduce_source_rate", "block", "fail_fast"];
  return (
    <div className="pipelineTopologyInspectorSection">
      <div className="pipelineTopologyInspectorSectionTitle">{t("core.ui.pipelines.topology.edge_policy", {}, "Edge policy")}</div>
      <div className="pipelineTopologyEditGrid">
        <label>
          <span>{field("max_items", "Max items")}</span>
          <input
            id={`pipeline-topology-edge-max-items-${edge.id}`}
            name={`edge_${edge.id}_max_items`}
            min={1}
            max={4096}
            type="number"
            value={data.maxItems}
            onChange={(event) => update({ maxItems: Number(event.target.value) })}
          />
        </label>
        <label>
          <span>{field("drop_policy", "Drop policy")}</span>
          <select
            id={`pipeline-topology-edge-drop-policy-${edge.id}`}
            name={`edge_${edge.id}_drop_policy`}
            value={data.dropPolicy}
            onChange={(event) => update({ dropPolicy: event.target.value })}
          >
            {dropPolicyOptions.map((value) => (
              <option key={value} value={value}>
                {edgeValueLabel("drop_policy", value, t)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{field("backpressure", "Backpressure")}</span>
          <select
            id={`pipeline-topology-edge-backpressure-${edge.id}`}
            name={`edge_${edge.id}_backpressure`}
            value={data.pressureMode}
            onChange={(event) => update({ pressureMode: event.target.value })}
          >
            {pressureModeOptions.map((value) => (
              <option key={value} value={value}>
                {edgeValueLabel("pressure_mode", value, t)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{field("modality", "Modality")}</span>
          <input
            id={`pipeline-topology-edge-modality-${edge.id}`}
            name={`edge_${edge.id}_modality`}
            value={data.modality}
            onChange={(event) => update({ modality: event.target.value })}
          />
        </label>
        <label>
          <span>{field("semantic", "Semantic")}</span>
          <input
            id={`pipeline-topology-edge-semantic-${edge.id}`}
            name={`edge_${edge.id}_semantic`}
            value={data.semanticClass}
            onChange={(event) => update({ semanticClass: event.target.value })}
          />
        </label>
        <label className="pipelineTopologyCheckboxField">
          <input
            id={`pipeline-topology-edge-continuous-${edge.id}`}
            name={`edge_${edge.id}_continuous`}
            checked={data.continuous}
            type="checkbox"
            onChange={(event) => update({ continuous: event.target.checked })}
          />
          <span>{t("core.ui.pipelines.topology.field.continuous_stream", {}, "Continuous stream")}</span>
        </label>
      </div>
    </div>
  );
}

function EdgeInspector({
  edge,
  editable,
  onUpdateEdgePolicy,
  onDeleteEdge,
}: {
  edge: TopologyEdge;
  editable: boolean;
  onUpdateEdgePolicy?: (edgeId: string, patch: TopologyEdgePolicyPatch) => void;
  onDeleteEdge?: (edgeId: string) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const field = (key: string, fallback: string) => t(`core.ui.pipelines.topology.field.${key}`, {}, fallback);
  const data = edge.data;
  if (!data) {
    return (
      <div className="pipelineTopologyInspectorNotice">
        {t("core.ui.pipelines.topology.edge_data_unavailable", {}, "Edge data unavailable.")}
      </div>
    );
  }
  const runtime = data.runtime;
  return (
    <>
      <div className="pipelineTopologyInspectorHeader">
        <div>
          <div className="pipelineTopologyInspectorTitle">
            {data.sourceNodeId} {"->"} {data.targetNodeId}
          </div>
          <div className="pipelineTopologyInspectorSubtitle">{data.uid}</div>
        </div>
        {editable ? (
          <button className="pillButton pillButtonDanger" type="button" onClick={() => onDeleteEdge?.(edge.id)}>
            <i className="fa-solid fa-trash" aria-hidden="true" />
            {t("core.actions.delete")}
          </button>
        ) : null}
      </div>
      <div className="pipelineTopologyInspectorGrid">
        <Field label={field("from", "From")} value={`${data.sourceNodeId}.${data.sourcePort}`} />
        <Field label={field("to", "To")} value={`${data.targetNodeId}.${data.targetPort}`} />
        <EdgeField label={field("modality", "Modality")} kind="modality" value={data.modality} />
        <EdgeField label={field("semantic", "Semantic")} kind="semantic" value={data.semanticClass} />
        <Field label={field("continuous", "Continuous")} value={data.continuous} />
        <Field label={field("max_items", "Max items")} value={data.maxItems} />
        <EdgeField label={field("drop_policy", "Drop policy")} kind="drop_policy" value={data.dropPolicy} />
        <EdgeField label={field("backpressure", "Backpressure")} kind="pressure_mode" value={data.pressureMode} />
        <Field label={field("depth", "Depth")} value={runtime?.depth} />
        <Field label={field("utilization", "Utilization")} value={runtime?.utilization} />
        <Field label={field("cause", "Cause")} value={runtime?.pressure_cause} />
        <Field label={field("dropped", "Dropped")} value={runtime?.progress?.dropped_total} />
        <Field label={field("blocked_put_ms", "Blocked put ms")} value={runtime?.metrics?.blocked_put_time_ms} />
        <Field label={field("waiting_get_ms", "Waiting get ms")} value={runtime?.metrics?.waiting_get_time_ms} />
        <Field label={field("oldest_packet_ms", "Oldest packet ms")} value={runtime?.metrics?.oldest_packet_age_ms} />
        <Field label={field("artifact_bytes", "Artifact bytes")} value={runtime?.metrics?.artifact_bytes_current} />
      </div>
      <Alerts alerts={data.alerts} />
      <EdgePolicyEditor edge={edge} editable={editable} onUpdateEdgePolicy={onUpdateEdgePolicy} />
    </>
  );
}

export function TopologyInspector({
  model,
  selection,
  runtimeStatus,
  runtimeGeneratedAt,
  editable,
  pipelineName,
  processingServerId,
  onOpenProcessingServers,
  operatorsById,
  interactiveCameraId,
  camerasIndex,
  cameraSelectOptions,
  cameraSelectOptionById,
  activeCameraContexts,
  activeCameraContextsError,
  cameraAreaOptions,
  operatorPanels,
  onOpenTelemetryField,
  onUpdateNodeConfig,
  onUpdateEdgePolicy,
  onDeleteNode,
  onDeleteEdge,
  collapsed,
  onToggleCollapsed,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const node = selection.kind === "node" ? model.nodes.find((item) => item.id === selection.id) ?? null : null;
  const edge = selection.kind === "edge" ? model.edges.find((item) => item.id === selection.id) ?? null : null;
  return (
    <aside className={["pipelineTopologyInspector", collapsed ? "isCollapsed" : ""].filter(Boolean).join(" ")}>
      <button className="pipelineTopologyInspectorToggle" type="button" onClick={onToggleCollapsed}>
        <i className={`fa-solid ${collapsed ? "fa-chevron-up" : "fa-chevron-down"}`} aria-hidden="true" />
        {collapsed
          ? t("core.ui.pipelines.topology.show_details", {}, "Show details")
          : t("core.ui.pipelines.topology.hide_details", {}, "Hide details")}
      </button>
      <div className="pipelineTopologyInspectorContent">
        {node ? (
          <NodeInspector
            model={model}
            node={node}
            editable={editable}
            pipelineName={pipelineName}
            processingServerId={processingServerId}
            onOpenProcessingServers={onOpenProcessingServers}
            operatorsById={operatorsById}
            interactiveCameraId={interactiveCameraId}
            camerasIndex={camerasIndex}
            cameraSelectOptions={cameraSelectOptions}
            cameraSelectOptionById={cameraSelectOptionById}
            activeCameraContexts={activeCameraContexts}
            activeCameraContextsError={activeCameraContextsError}
            cameraAreaOptions={cameraAreaOptions}
            operatorPanels={operatorPanels}
            onOpenTelemetryField={onOpenTelemetryField}
            onUpdateNodeConfig={onUpdateNodeConfig}
            onDeleteNode={onDeleteNode}
          />
        ) : edge ? (
          <EdgeInspector edge={edge} editable={editable} onUpdateEdgePolicy={onUpdateEdgePolicy} onDeleteEdge={onDeleteEdge} />
        ) : (
          <SummaryInspector
            model={model}
            runtimeStatus={runtimeStatus}
            runtimeGeneratedAt={runtimeGeneratedAt}
            editable={editable}
          />
        )}
      </div>
    </aside>
  );
}
