import type { VisualConfiguration } from "./visualState";

export const aiPeriods = ["minute", "hour", "day", "month"] as const;
export type AiPeriod = typeof aiPeriods[number];
export type VisualAiLimits = { max_concurrency: number } & Record<`requests_per_${AiPeriod}`, number | null>;
export type VisualAiProfile = { id: string; name: string; model: string; provider_id: string; budget_group_id: string | null;
  capabilities: string[]; timeout_seconds: number; max_images_per_request: number; enabled: boolean; local: boolean;
  images_allowed: boolean; chain: string[] };
export type VisualAiOptions = { profiles: VisualAiProfile[]; limits: VisualAiLimits; checked_at: number;
  budget_groups: { id: string; name: string; limits: VisualAiLimits }[];
  usage: { available: boolean; error: string; persistent: boolean; profiles: Record<string, Record<AiPeriod, number>>;
    budget_groups: Record<string, Record<AiPeriod, number>>; sensors: Record<string, Record<AiPeriod, number>>;
    resets_at: Record<AiPeriod, number>; pauses: { scope: string; id: string; reason: string; retry_at: number | null; recovery_pending: boolean }[] } };
export const visualAiDefaults = { ai_timeout_ms: 60000, ai_max_attempts: 1, ai_references_per_class: 2,
  requests_per_minute: 20, requests_per_hour: 300, requests_per_day: 2000, requests_per_month: null };

export function visualAiValues(value: VisualConfiguration): Record<string, string> {
  const limits = value.ai_limits as Partial<VisualAiLimits> | undefined;
  return Object.fromEntries(Object.entries(visualAiDefaults).map(([key, fallback]) => {
    const raw = key.startsWith("requests_") ? limits?.[key as `requests_per_${AiPeriod}`] : value[key];
    return [key, String(raw === undefined ? fallback ?? "" : raw ?? "")];
  }));
}
export function applyVisualAiValues(value: VisualConfiguration, fields: Record<string, string>): VisualConfiguration {
  const parsed: Record<string, number | null> = {};
  for (const key of Object.keys(visualAiDefaults)) {
    const text = (fields[key] ?? "").trim();
    const number = Number(text);
    if (!text && key.startsWith("requests_")) { parsed[key] = null; continue; }
    const maximum = key === "ai_timeout_ms" ? 120000 : key === "ai_max_attempts" ? 3 : key === "ai_references_per_class" ? 30 : Number.MAX_SAFE_INTEGER;
    if (!text || !Number.isSafeInteger(number) || number < 1 || number > maximum) throw new Error("ai_settings_invalid");
    parsed[key] = number;
  }
  return { ...value, ai_timeout_ms: parsed.ai_timeout_ms, ai_max_attempts: parsed.ai_max_attempts,
    ai_references_per_class: parsed.ai_references_per_class,
    ai_limits: { max_concurrency: 1, ...Object.fromEntries(aiPeriods.map(period => [`requests_per_${period}`, parsed[`requests_per_${period}`]])) } };
}
export function visualAiProfiles(value: VisualConfiguration, options: VisualAiOptions): VisualAiProfile[] {
  const selected = options.profiles.find(profile => profile.id === value.ai_profile_id);
  return (selected?.chain ?? []).slice(0, Number(value.ai_max_attempts ?? 1))
    .flatMap(id => options.profiles.filter(profile => profile.id === id));
}
export function visualAiIssue(value: VisualConfiguration, profile: VisualAiProfile): string | null {
  if (!profile.enabled) return "ai_profile_unavailable";
  if (!["vision", "structured_json", "multiple_images"].every(capability => profile.capabilities.includes(capability))) return "ai_profile_incompatible";
  const images = 1 + value.classes.filter(category => category.enabled).reduce((count, category) =>
    count + Math.min(category.reference_ids.length, Number(value.ai_references_per_class ?? 2)), 0);
  if (images > profile.max_images_per_request) return "ai_image_capacity";
  if (!profile.images_allowed) return "ai_images_blocked";
  if (!profile.local && value.ai_allow_remote_images !== true) return "ai_consent_required";
  return null;
}
export function visualAiBudget(value: VisualConfiguration, options: VisualAiOptions, profile: VisualAiProfile, period: AiPeriod) {
  const key = `requests_per_${period}` as const;
  const group = options.budget_groups.find(item => item.id === profile.budget_group_id);
  const local = value.ai_limits as Partial<VisualAiLimits> | undefined;
  const scopes = [
    { limit: local?.[key] === undefined ? visualAiDefaults[key] : local[key], used: options.usage.sensors[value.sensor_id]?.[period] ?? 0 },
    { limit: options.limits[key], used: options.usage.profiles[profile.id]?.[period] ?? 0 },
    { limit: group?.limits[key] ?? null, used: group ? options.usage.budget_groups[group.id]?.[period] ?? 0 : 0 },
  ].filter(scope => scope.limit !== null);
  return { limit: scopes.length ? Math.min(...scopes.map(scope => scope.limit as number)) : null,
    remaining: !options.usage.available ? null : scopes.length ? Math.max(0, Math.min(...scopes.map(scope => (scope.limit as number) - scope.used))) : null };
}
export function visualAiPauses(options: VisualAiOptions, profile: VisualAiProfile) {
  return options.usage.pauses.filter(pause => (pause.scope === "profile" && pause.id === profile.id)
    || (pause.scope === "provider" && pause.id === profile.provider_id)
    || (pause.scope === "group" && pause.id === profile.budget_group_id));
}
export function visualTestReady(value: VisualConfiguration): boolean {
  const enabled = value.classes.filter(item => item.enabled);
  return Boolean(value.source_identity && value.preparation_revision && enabled.length >= 2 && (value.strategy === "direct"
    ? value.ai_profile_id && enabled.every(item => item.reference_ids.length || item.description.trim())
    : value.model_id && value.comparison && enabled.every(item => item.reference_ids.length)));
}
