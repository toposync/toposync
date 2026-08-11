import type React from "react";
import { useLayoutEffect } from "react";
import { Handle, Position, type NodeProps, useUpdateNodeInternals } from "@xyflow/react";

import { i18n } from "../../../../util/i18n";
import { resourceKindLabel, runtimeStateLabel } from "./topologyLabels";
import type { TopologyNode, TopologyNodeData } from "./topologyTypes";

type NodeTelemetry = {
  processed: number;
  emitted: number;
  dropped: number;
  errors: number;
};

type NodeTelemetryKey = keyof NodeTelemetry;

const NODE_TELEMETRY_METRICS: Array<{
  key: NodeTelemetryKey;
  labelKey: string;
  fieldKey: string;
  fallback: string;
  fieldFallback: string;
}> = [
  { key: "emitted", labelKey: "output", fieldKey: "emitted", fallback: "Out {{count}}", fieldFallback: "Emitted" },
  { key: "processed", labelKey: "processed", fieldKey: "processed", fallback: "Proc {{count}}", fieldFallback: "Processed" },
  { key: "dropped", labelKey: "dropped", fieldKey: "dropped", fallback: "Lost {{count}}", fieldFallback: "Dropped" },
  { key: "errors", labelKey: "errors", fieldKey: "errors", fallback: "Err {{count}}", fieldFallback: "Errors" },
];

function handleOffset(index: number, total: number): string {
  if (total <= 1) return "50%";
  return `${Math.round(((index + 1) / (total + 1)) * 100)}%`;
}

function groupIcon(groupId: string, resourceKind: string): string {
  if (resourceKind === "vision_model") return "fa-brain";
  if (groupId === "input") return "fa-video";
  if (groupId === "vision") return "fa-eye";
  if (groupId === "rules") return "fa-code-branch";
  if (groupId === "rate") return "fa-gauge-high";
  if (groupId === "output") return "fa-paper-plane";
  if (groupId === "diagnostics") return "fa-stethoscope";
  return "fa-cube";
}

function metricValue(runtime: TopologyNodeData["runtime"], key: string): number | null {
  const value = runtime?.progress?.[key] ?? runtime?.metrics?.[key];
  const numberValue = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numberValue)) return null;
  return Math.max(0, Math.trunc(numberValue));
}

function nodeTelemetry(runtime: TopologyNodeData["runtime"]): NodeTelemetry | null {
  if (!runtime) return null;
  const processed = metricValue(runtime, "processed_packets");
  const emitted = metricValue(runtime, "emitted_packets");
  const dropped = metricValue(runtime, "dropped_packets");
  const errors = metricValue(runtime, "error_count");
  if (processed === null && emitted === null && dropped === null && errors === null) return null;
  return {
    processed: processed ?? 0,
    emitted: emitted ?? 0,
    dropped: dropped ?? 0,
    errors: errors ?? 0,
  };
}

function metricCount(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: value >= 10000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}

function metricLabel(
  metric: (typeof NODE_TELEMETRY_METRICS)[number],
  telemetry: NodeTelemetry,
  t: ReturnType<typeof i18n.useI18n>["t"],
): string {
  return t(
    `core.ui.pipelines.topology.node_metric.${metric.labelKey}`,
    { count: metricCount(telemetry[metric.key]) },
    metric.fallback,
  );
}

export function TopologyNodeComponent({ id, data, selected, isConnectable }: NodeProps<TopologyNode>): React.ReactElement {
  const { t } = i18n.useI18n();
  const updateNodeInternals = useUpdateNodeInternals();
  const inputPorts = data.inputPorts.length ? data.inputPorts : [];
  const outputPorts = data.outputPorts.length ? data.outputPorts : [];
  const portKey = `${inputPorts.join("|")}=>${outputPorts.join("|")}`;
  const runtimeState = runtimeStateLabel(data.runtime?.runtime_state ?? data.runtime?.task_state ?? (data.runtime ? "idle" : undefined), t);
  const alertLabel =
    data.alertCount === 1
      ? t("core.ui.pipelines.topology.alert_count_one", {}, "1 alert")
      : t("core.ui.pipelines.topology.alert_count_many", { count: data.alertCount }, "{{count}} alerts");
  const telemetry = nodeTelemetry(data.runtime);
  const metricLabels: Record<NodeTelemetryKey, string> | null = telemetry
    ? (Object.fromEntries(
        NODE_TELEMETRY_METRICS.map((metric) => [metric.key, metricLabel(metric, telemetry, t)]),
      ) as Record<NodeTelemetryKey, string>)
    : null;
  const outputLabel = metricLabels
    ? metricLabels.emitted
    : t("core.ui.pipelines.topology.node_metric.no_telemetry", {}, "No telemetry");
  const telemetryTitle = telemetry
    ? [
        `${t("core.ui.pipelines.topology.field.node_id", {}, "Node ID")}: ${data.nodeId}`,
        ...NODE_TELEMETRY_METRICS.map(
          (metric) =>
            `${t(`core.ui.pipelines.topology.field.${metric.fieldKey}`, {}, metric.fieldFallback)}: ${metricCount(telemetry[metric.key])}`,
        ),
      ].join(" | ")
    : data.nodeId;
  useLayoutEffect(() => {
    updateNodeInternals(id);
    const frame = window.requestAnimationFrame(() => updateNodeInternals(id));
    return () => window.cancelAnimationFrame(frame);
  }, [id, portKey, updateNodeInternals]);
  return (
    <div
      className={["pipelineTopologyNode", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
      data-pressure={data.pressureState}
      style={{ "--pipeline-topology-node-color": data.color } as React.CSSProperties}
    >
      {inputPorts.map((port, index) => (
        <Handle
          key={`in:${port}`}
          id={port}
          type="target"
          position={Position.Left}
          isConnectable={Boolean(isConnectable)}
          style={{ top: handleOffset(index, inputPorts.length) }}
        />
      ))}
      <div className="pipelineTopologyNodeHeader">
        <span className="pipelineTopologyNodeIcon" aria-hidden="true">
          <i className={`fa-solid ${groupIcon(String(data.groupId), data.resourceKind)}`} />
        </span>
        <div className="pipelineTopologyNodeTitleBlock">
          <div className="pipelineTopologyNodeTitle" title={data.label}>
            {data.label}
          </div>
          <div className="pipelineTopologyNodeSubtitle" title={telemetryTitle}>
            {outputLabel}
          </div>
        </div>
      </div>
      <div className="pipelineTopologyNodeMeta">
        <span>{runtimeState}</span>
        {telemetry && metricLabels && telemetry.processed !== telemetry.emitted ? <span>{metricLabels.processed}</span> : null}
        {telemetry && metricLabels && telemetry.dropped > 0 ? <span data-tone="warning">{metricLabels.dropped}</span> : null}
        {telemetry && metricLabels && telemetry.errors > 0 ? <span data-tone="error">{metricLabels.errors}</span> : null}
        {data.resourceKind !== "none" ? <span>{resourceKindLabel(data.resourceKind, t)}</span> : null}
        {data.alertCount > 0 ? <span>{alertLabel}</span> : null}
      </div>
      {outputPorts.map((port, index) => (
        <Handle
          key={`out:${port}`}
          id={port}
          type="source"
          position={Position.Right}
          isConnectable={Boolean(isConnectable)}
          style={{ top: handleOffset(index, outputPorts.length) }}
        />
      ))}
    </div>
  );
}
