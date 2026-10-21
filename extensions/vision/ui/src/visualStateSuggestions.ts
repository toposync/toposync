import type { VisualConfiguration } from "./visualState";

export type SuggestionPolicy = { mode: "off" | "review"; maximum_pending: number; retention_days: number };
export type VisualSuggestion = { suggestion_id: string; compatible: boolean; revision: number; expires_at: number;
  status: "pending" | "accepted" | "rejected" | "expired";
  reference: { class_id: string; evidence: { observed_at: number; sample: { capture_instance: string; generation: number; sequence: number } };
    provenance: { proposed_class_id: string; profile_id: string; provider_id: string; model: string; reason: string;
      runtime_epoch: string; reference_revision: string; ai_configuration_revision: string; declared_confidence: number | null } };
  reviews: { action: "accept" | "reject" | "undo"; actor_id: string; reviewed_at: number; class_id: string | null; reference_id: string | null }[] };
export type VisualSuggestions = { items: VisualSuggestion[]; next_offset: number | null;
  summary: { pending: number; by_class: Record<string, number>; maximum_per_class: number; policy: SuggestionPolicy } };
export const suggestionPolicy = (value: VisualConfiguration): SuggestionPolicy =>
  ({ mode: "review", maximum_pending: 20, retention_days: 7, ...(value.suggestions as Partial<SuggestionPolicy> | undefined) });
export function applySuggestionLimits(value: VisualConfiguration, maximum: string, days: string): VisualConfiguration {
  const maximum_pending = Number(maximum), retention_days = Number(days);
  if (!maximum.trim() || !days.trim() || !Number.isInteger(maximum_pending) || maximum_pending < 1 || maximum_pending > 100
      || !Number.isInteger(retention_days) || retention_days < 1 || retention_days > 30) throw new Error("suggestion_invalid_limits");
  return { ...value, suggestions: { ...suggestionPolicy(value), maximum_pending, retention_days } };
}
export function acceptedSuggestion(value: VisualConfiguration, item: VisualSuggestion) {
  const last = item.reviews.at(-1);
  return item.status === "accepted" && last?.action === "accept" && last.reference_id
    ? { ...last, attached: value.classes.some(category => category.reference_ids.includes(last.reference_id!)) } : null;
}
export function restoreApprovedSuggestion(value: VisualConfiguration, item: VisualSuggestion): VisualConfiguration {
  const accepted = acceptedSuggestion(value, item);
  if (!item.compatible || !accepted || accepted.attached
      || !value.classes.some(category => category.class_id === accepted.class_id && category.enabled)) throw new Error("reference_conflict");
  return { ...value, classes: value.classes.map(category => category.class_id === accepted.class_id
    ? { ...category, reference_ids: [...category.reference_ids, accepted.reference_id!] } : category) };
}
