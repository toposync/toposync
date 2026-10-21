import type { PipelineReferenceEvidence } from "@toposync/plugin-api";

export type VisualClass = { class_id: string; key: string; label: string; description: string; enabled: boolean; reference_ids: string[]; [key: string]: unknown };
export type VisualConfiguration = {
  sensor_id: string; source_identity: string | null; preparation_revision: string | null;
  region_id: string | null; use_full_image: boolean; reference_revision: string; model_revision: string;
  classes: VisualClass[]; strategy: "examples" | "fallback" | "direct"; scene_description: string;
  model_id?: string | null; [key: string]: unknown;
};
export type VisualDraft = { draft_id: string; revision: number; expires_at: number; configuration: VisualConfiguration };
export type VisualModel = { model_id: string; name: string; revision: string; artifact_present: boolean };

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
    region_id: previous.region_id, use_full_image: previous.use_full_image,
    classes: [...current.classes.map(item => {
      const before = previous.classes.find(category => category.class_id === item.class_id);
      return before ? { ...item, reference_ids: before.reference_ids } : item;
    }), ...previous.classes.filter(item => !current.classes.some(category => category.class_id === item.class_id))] };
}
