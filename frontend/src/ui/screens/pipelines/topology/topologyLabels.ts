export type TopologyTranslate = (key: string, params?: Record<string, unknown>, fallback?: string) => string;

type TopologyValueKind = "pressure_behavior" | "pressure_cause" | "pressure_state" | "resource" | "runtime";

function normalizedValue(value: unknown): string {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function topologyValueLabel(kind: TopologyValueKind, value: unknown, t: TopologyTranslate, emptyKey = "value.none"): string {
  const raw = String(value ?? "").trim();
  const normalized = normalizedValue(raw);
  if (!normalized) return t(`core.ui.pipelines.topology.${emptyKey}`, {}, "none");
  return t(`core.ui.pipelines.topology.${kind}.${normalized}`, {}, raw);
}

export function runtimeStateLabel(value: unknown, t: TopologyTranslate): string {
  return topologyValueLabel("runtime", value, t, "runtime.no_runtime");
}

export function resourceKindLabel(value: unknown, t: TopologyTranslate): string {
  return topologyValueLabel("resource", value, t);
}

export function pressureStateLabel(value: unknown, t: TopologyTranslate): string {
  return topologyValueLabel("pressure_state", value, t);
}

export function pressureBehaviorLabel(value: unknown, t: TopologyTranslate): string {
  return topologyValueLabel("pressure_behavior", value, t);
}

export function pressureCauseLabel(value: unknown, t: TopologyTranslate): string {
  return topologyValueLabel("pressure_cause", value, t);
}
