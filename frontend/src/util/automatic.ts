export type AutomaticScope = {
  kind: "camera" | "physical_area" | "image_region";
  camera_ids: string[];
  excluded_camera_ids: string[];
  composition_id: string;
  area_id: string;
  region?: Record<string, unknown> | null;
};
export type AutomaticSource = { camera_id: string; camera_name: string; source_id: string; source_name: string; clock_domain: string; coverage: string };
export type AutomaticTarget = { id: string; name: string; scope: AutomaticScope; sources: AutomaticSource[]; reason: string | null; scope_operator?: string; preparations?: Record<string, Record<string, unknown>> };
export type AutomaticIntent = {
  id: string; revision: number; name: string; capability: string; scope: AutomaticScope;
  requested: boolean; owner_id: string; condition: Record<string, any>;
  consequence: { capability: string; parameters: Record<string, any> };
  customizations: Record<string, any>; consents: string[];
};
export type AutomaticConfiguration = {
  revision: number; enabled: boolean; intents: AutomaticIntent[];
  defaults: Record<string, Record<string, any>>; blocked_camera_ids: string[];
  denied_features: Record<string, string[]>;
};
export type AutomaticField = { name: string; type: "text" | "number" | "multiple" | "select";
  title: Record<string, string>; default?: any; minimum?: number; maximum?: number; step?: number; required?: boolean;
  choices?: Array<{ value: string; title: Record<string, string> }> };
export type AutomaticCapability = {
  id: string; kind: "observation" | "consequence"; extension_id: string;
  title: Record<string, string>; scopes?: AutomaticScope["kind"][];
  available: boolean; required_consents: string[]; advanced?: boolean;
  missing_operators: string[]; fields?: AutomaticField[];
  resource_fields?: AutomaticField[]; resource_hint?: Record<string, string>; resource_operator?: string;
  resource_consent_fields?: string[];
  configuration_panels?: Array<{ operator: string; field: string; minimum_sources: number }>;
};
export type AutomaticActivity = { state: string; reason: string | null; requested?: boolean; sources: AutomaticSource[]; limitations?: string[]; last_result?: { at: number; received: number } };
export type AutomaticSnapshot = {
  configuration: AutomaticConfiguration; catalog: AutomaticCapability[]; targets: AutomaticTarget[];
  activities: Record<string, AutomaticActivity>; provider_errors: string[];
  manual_flows?: Record<string, { state: string; reason: string | null; received?: number; production_revision?: string }>;
};
export type AutomaticPreview = { activities: Record<string, AutomaticActivity>; pipelines: Array<{ name: string; display_name: string }>; preparations: Record<string, string> };

export function replaceAutomaticIntent(configuration: AutomaticConfiguration, intent: AutomaticIntent): AutomaticConfiguration {
  const exists = configuration.intents.some(item => item.id === intent.id);
  return { ...configuration, intents: exists ? configuration.intents.map(item => item.id === intent.id ? intent : item) : [...configuration.intents, intent] };
}

export function replaceAutomaticResources(intent: AutomaticIntent, capability: AutomaticCapability, parameters: Record<string, unknown>): AutomaticIntent {
  const consentChanged = capability.resource_consent_fields?.some(field => intent.condition.resource_parameters?.[field] !== parameters[field]);
  return { ...intent, condition: { ...intent.condition, resource_parameters: parameters },
    consents: consentChanged ? intent.consents.filter(consent => !capability.required_consents.includes(consent)) : intent.consents };
}

export function sameAutomaticTarget(scope: AutomaticScope, target: AutomaticTarget): boolean {
  return scope.kind === target.scope.kind && (scope.kind === "physical_area"
    ? scope.composition_id === target.scope.composition_id && scope.area_id === target.scope.area_id
    : scope.camera_ids[0] === target.scope.camera_ids[0]);
}

export function automaticDraft(snapshot: AutomaticSnapshot, locale: string, targetId?: string): AutomaticIntent | null {
  const place = targetId ? snapshot.targets.find(item => item.id === targetId) : snapshot.targets[0];
  if (!place) return null;
  const observations = snapshot.catalog.filter(item => item.kind === "observation" && item.available && item.scopes?.includes(place.scope.kind));
  const observation = observations.find(item => !item.advanced && item.required_consents.length === 0) || observations[0];
  if (!observation) return null;
  return {
    id: crypto.randomUUID().replace(/-/g, ""), revision: 1,
    name: `${observation.title[locale] || observation.title.en || observation.id} · ${place.name}`,
    capability: observation.id, scope: structuredClone(place.scope), requested: true,
    owner_id: "", condition: {}, consequence: { capability: "core.record", parameters: {} },
    customizations: {}, consents: [],
  };
}
