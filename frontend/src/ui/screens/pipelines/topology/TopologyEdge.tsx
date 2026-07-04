import type React from "react";
import { BaseEdge, getBezierPath, type EdgeProps } from "@xyflow/react";

import type { TopologyEdge } from "./topologyTypes";

export function TopologyEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
  data,
}: EdgeProps<TopologyEdge>): React.ReactElement {
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const pressure = data?.pressureState ?? "none";
  const label = data?.label ?? "";
  return (
    <BaseEdge
      id={id}
      path={edgePath}
      className={["pipelineTopologyEdge", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
      data-pressure={pressure}
      label={label}
      labelX={labelX}
      labelY={labelY}
      labelShowBg
      labelBgPadding={[7, 4]}
      labelBgBorderRadius={6}
      interactionWidth={24}
    />
  );
}
