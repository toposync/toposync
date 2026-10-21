import type { ImageMaskEditorProps, PipelineReferenceEvidence } from "@toposync/plugin-api";

export type CanonicalVisualRegion = {
  camera_id: string; source_id: string; name: string; framing: "passive";
  reference: { artifact_id: string; revision: number; geometry_digest: string };
  strokes: ImageMaskEditorProps["strokes"]; alignment_strokes?: ImageMaskEditorProps["strokes"];
  maximum_side?: number; alignment_margin_pixels?: number; [key: string]: unknown;
};

export type VisualClass = { class_id: string; key: string; label: string; description: string; enabled: boolean; reference_ids: string[]; [key: string]: unknown };
export type VisualConfiguration = {
  sensor_id: string; source_identity: string | null; preparation_revision: string | null;
  region_id: string | null; use_full_image: boolean; reference_revision: string; model_revision: string;
  classes: VisualClass[]; strategy: "examples" | "fallback" | "direct"; scene_description: string;
  model_id?: string | null; [key: string]: unknown;
  canonical_region?: CanonicalVisualRegion | null;
};
export type VisualDraft = { draft_id: string; revision: number; expires_at: number; configuration: VisualConfiguration };
export type VisualModel = { model_id: string; name: string; revision: string; artifact_present: boolean };
export type VisualSample = { capture_instance: string; generation: number; sequence: number };
export type VisualPublishedState = {
  status: "confirmed" | "pending" | "unknown" | "unavailable"; class_id: string | null; reason: string;
  observed_at: number | null; valid_for_ms: number; runtime_epoch: string; state_version: number;
  preparation_revision: string; reference_revision: string; model_revision: string;
  candidate: { class_id: string; started_at: number } | null;
  last_confirmed: { class_id: string; observed_at: number } | null;
  analysis_age_ms: number | null;
  last_analysis: { class_id: string | null; similarity: number | null; margin: number | null;
    declared_confidence: number | null; reference_ids: string[]; sample: VisualSample;
    observed_at: number; duration_ms: number; region_quality: "valid" | "unobservable" | "unavailable";
    freshness_basis: "capture" | "receipt"; method: "local" | "multimodal"; reason: string } | null;
};
export type VisualPreview = { test_only: true; duration_ms: number; completed_at: number; state: VisualPublishedState };

export const visualComparisonFields = ["enter_threshold", "enter_margin", "hold_threshold", "hold_margin"] as const;
export const visualTemporalDefaults = { confirmations: 2, minimum_separation_ms: 150, maximum_gap_ms: 750,
  pending_hold_ms: 500, maximum_frame_age_ms: 750, validity_ms: 2000 };
export function visualDecisionValues(configuration: VisualConfiguration): Record<string, string> {
  const comparison = (configuration.comparison ?? {}) as Record<string, number>;
  const temporal = { ...visualTemporalDefaults, ...(configuration.temporal as object ?? {}) };
  return Object.fromEntries([...visualComparisonFields.map(key => [key, String(comparison[key] ?? "")]),
    ...Object.entries(temporal).map(([key, value]) => [key, String(value)]),
    ["evaluation_interval_ms", String(configuration.evaluation_interval_ms ?? 250)]]);
}
export function applyVisualDecisionValues(configuration: VisualConfiguration, fields: Record<string, string>): VisualConfiguration {
  const parsed = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, value.trim() === "" ? NaN : Number(value)]));
  const between = (key: string, minimum: number, maximum: number, integer = false) =>
    Number.isFinite(parsed[key]) && parsed[key] >= minimum && parsed[key] <= maximum && (!integer || Number.isInteger(parsed[key]));
  if (!visualComparisonFields.every(key => between(key, key.endsWith("margin") ? 0 : -1, key.endsWith("margin") ? 2 : 1))
      || parsed.hold_threshold > parsed.enter_threshold || parsed.hold_margin > parsed.enter_margin
      || !between("confirmations", 2, 30, true) || !between("minimum_separation_ms", 1, 60000, true)
      || !between("evaluation_interval_ms", 50, 60000, true)
      || !["maximum_gap_ms", "maximum_frame_age_ms", "validity_ms"].every(key => between(key, 1, 3600000, true))
      || !between("pending_hold_ms", 0, 3600000, true) || parsed.minimum_separation_ms > parsed.maximum_gap_ms
      || parsed.pending_hold_ms > parsed.validity_ms || parsed.maximum_frame_age_ms > parsed.validity_ms) throw new Error("visual_decision_invalid");
  return { ...configuration, comparison: Object.fromEntries(visualComparisonFields.map(key => [key, parsed[key]])),
    temporal: Object.fromEntries(Object.keys(visualTemporalDefaults).map(key => [key, parsed[key]])),
    evaluation_interval_ms: parsed.evaluation_interval_ms };
}

export function selectVisualModel(configuration: VisualConfiguration, model: VisualModel): VisualConfiguration {
  if (!model.artifact_present) throw new Error("visual_model_unavailable");
  if (configuration.model_id === model.model_id && configuration.model_revision === model.revision) return configuration;
  return { ...configuration, model_id: model.model_id, model_revision: model.revision, comparison: null,
    classes: configuration.classes.map(category => ({ ...category, comparison: null })) };
}

export function newVisualClass(existing: VisualClass[], label: string): VisualClass {
  let index = existing.length + 1;
  while (existing.some(item => item.key === `state_${index}`)) index += 1;
  return { class_id: crypto.randomUUID(), key: `state_${index}`, label, description: "", enabled: true, reference_ids: [] };
}

export function newVisualConfiguration(label: (index: number) => string): VisualConfiguration {
  const first = newVisualClass([], label(1));
  return { sensor_id: crypto.randomUUID(), source_identity: null, preparation_revision: null,
    region_id: null, use_full_image: false, reference_revision: crypto.randomUUID(), model_revision: "unconfigured",
    classes: [first, newVisualClass([first], label(2))], strategy: "examples", scene_description: "" };
}

export function visualClassesValid(classes: VisualClass[]): boolean {
  return classes.length >= 2 && classes.length <= 32 && classes.filter(item => item.enabled).length >= 2
    && new Set(classes.map(item => item.class_id)).size === classes.length
    && new Set(classes.map(item => item.key)).size === classes.length
    && classes.every(item => typeof item.label === "string" && item.label.trim().length > 0 && item.label.trim().length <= 80
      && /^[a-z][a-z0-9_]{0,63}$/.test(item.key) && !["unknown", "unavailable"].includes(item.key)
      && typeof item.description === "string" && item.description.length <= 1000
      && Array.isArray(item.reference_ids) && item.reference_ids.length <= 30)
    && classes.reduce((count, item) => count + item.reference_ids.length, 0) <= 300;
}

export function bindVisualConfiguration(configuration: VisualConfiguration, evidence: PipelineReferenceEvidence): VisualConfiguration {
  const hasReferences = configuration.classes.some(item => item.reference_ids.length > 0);
  if (hasReferences && (configuration.source_identity !== evidence.source_identity
      || configuration.preparation_revision !== evidence.preparation_revision)) throw new Error("reference_context_changed");
  if (!evidence.prepared_region && !configuration.use_full_image) throw new Error("reference_region_required");
  return { ...configuration, source_identity: evidence.source_identity, preparation_revision: evidence.preparation_revision,
    region_id: evidence.prepared_region ? evidence.preparation_revision : null, use_full_image: !evidence.prepared_region };
}

export function restoreVisualReferences(current: VisualConfiguration, previous: VisualConfiguration): VisualConfiguration {
  return { ...current, source_identity: previous.source_identity, preparation_revision: previous.preparation_revision,
    region_id: previous.region_id, use_full_image: previous.use_full_image, canonical_region: previous.canonical_region ?? null,
    classes: [...current.classes.map(item => {
      const before = previous.classes.find(category => category.class_id === item.class_id);
      return before ? { ...item, reference_ids: before.reference_ids } : item;
    }), ...previous.classes.filter(item => !current.classes.some(category => category.class_id === item.class_id))] };
}

export function visualSource(pipeline: unknown): { camera_id: string; source_id: string } | null {
  const graph = (pipeline as { graph?: { nodes?: { operator?: string; config?: Record<string, unknown> }[] } })?.graph;
  if (!Array.isArray(graph?.nodes)) return null;
  const sources = graph.nodes.filter(node => node?.operator === "camera.source");
  const config = sources.length === 1 ? sources[0]?.config : null;
  return typeof config?.camera_id === "string" && config.camera_id && typeof config.source_id === "string" && config.source_id
    ? { camera_id: config.camera_id, source_id: config.source_id } : null;
}

export function replaceVisualRegion(configuration: VisualConfiguration, region: CanonicalVisualRegion | null, useFullImage = false): VisualConfiguration {
  if (configuration.classes.some(category => category.reference_ids.length)) throw new Error("reference_context_changed");
  return { ...configuration, canonical_region: region, use_full_image: useFullImage, region_id: null,
    source_identity: null, preparation_revision: null };
}
