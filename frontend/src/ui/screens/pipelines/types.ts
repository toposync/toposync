export type EditorMode = "interactive" | "json" | "python";
export type DragInsertPosition = "before" | "after";

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

export type TelemetryFieldInspectorRequest = {
  stepUid: string;
  nodeId: string;
  operatorId: string;
  configKey: string;
  metricId: string;
  label: string;
  value: number;
};
