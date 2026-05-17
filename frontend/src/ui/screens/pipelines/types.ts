import type { PipelineOperatorDefinition } from "../../../util/api";

import type { PipelineOperatorRecipeDefinition } from "./constants";

export type EditorMode = "interactive" | "json" | "python";
export type DragInsertPosition = "before" | "after";

export type PipelineCatalogItem =
  | { kind: "operator"; id: string; operator: PipelineOperatorDefinition }
  | { kind: "recipe"; id: string; recipe: PipelineOperatorRecipeDefinition };

export type InteractiveStep = {
  uid: string;
  nodeId: string;
  operatorId: string;
  configText: string;
  collapsed: boolean;
  showAdvanced: boolean;
};

export type InteractiveBuildResult = {
  graph: Record<string, unknown> | null;
  error: string | null;
};

export type InteractiveFromGraphResult = {
  steps: InteractiveStep[];
  warning: string | null;
};

export type SelectOption = { value: string; label: string };

export type CameraAreaPoint = {
  x: number;
  z: number;
};

export type CameraAreaOption = SelectOption & {
  compositionId: string;
  areaId: string;
  areaName: string;
  points: CameraAreaPoint[];
};

export type TelemetryFieldInspectorRequest = {
  stepUid: string;
  nodeId: string;
  operatorId: string;
  configKey: string;
  metricId: string;
  label: string;
  value: number;
};
