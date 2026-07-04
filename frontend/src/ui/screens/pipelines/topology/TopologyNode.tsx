import type React from "react";
import { useLayoutEffect } from "react";
import { Handle, Position, type NodeProps, useUpdateNodeInternals } from "@xyflow/react";

import { i18n } from "../../../../util/i18n";
import type { TopologyNode } from "./topologyTypes";

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

export function TopologyNodeComponent({ id, data, selected, isConnectable }: NodeProps<TopologyNode>): React.ReactElement {
  const { t } = i18n.useI18n();
  const updateNodeInternals = useUpdateNodeInternals();
  const inputPorts = data.inputPorts.length ? data.inputPorts : [];
  const outputPorts = data.outputPorts.length ? data.outputPorts : [];
  const portKey = `${inputPorts.join("|")}=>${outputPorts.join("|")}`;
  const runtimeState = data.runtime
    ? String(data.runtime.runtime_state ?? data.runtime.task_state ?? t("core.ui.pipelines.topology.runtime.idle", {}, "idle"))
    : t("core.ui.pipelines.topology.runtime.no_runtime", {}, "no runtime");
  const alertLabel =
    data.alertCount === 1
      ? t("core.ui.pipelines.topology.alert_count_one", {}, "1 alert")
      : t("core.ui.pipelines.topology.alert_count_many", { count: data.alertCount }, "{{count}} alerts");
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
          <div className="pipelineTopologyNodeSubtitle" title={data.nodeId}>
            {data.nodeId}
          </div>
        </div>
      </div>
      <div className="pipelineTopologyNodeMeta">
        <span>{runtimeState}</span>
        {data.resourceKind !== "none" ? <span>{data.resourceKind}</span> : null}
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
