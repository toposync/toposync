export type AiProviderKind = "ollama" | "openai" | "anthropic" | "google" | "litellm";

export type AiProviderConfig = {
  id: string;
  name: string;
  kind: AiProviderKind;
  host: string;
  api_key: string;
  enabled: boolean;
  local: boolean;
  allow_image_upload: boolean;
};

export type AiProfileConfig = {
  id: string;
  name: string;
  provider_id: string;
  model: string;
  fallback_profile_ids: string[];
  capabilities: string[];
  timeout_seconds: number;
  max_image_side_px: number;
  jpeg_quality: number;
  temperature: number;
  enabled: boolean;
};

export type AiLimitSettings = {
  max_concurrency: number;
  requests_per_minute: number | null;
  requests_per_hour: number | null;
  requests_per_day: number | null;
  requests_per_month: number | null;
};

export type AiExtensionSettings = {
  default_profile_id: string;
  providers: AiProviderConfig[];
  profiles: AiProfileConfig[];
  limits: AiLimitSettings;
  model_catalog_version: string;
};

export type AiModelCatalogEntry = {
  id: string;
  provider: string;
  model: string;
  name: string;
  recommendation: string;
  tasks: string[];
  capabilities: string[];
  input_modalities: string[];
  local: boolean;
  estimated_size?: string;
  min_ollama_version?: string;
  last_verified_at?: string;
  notes?: string;
};

export type AiCatalogResponse = {
  models: AiModelCatalogEntry[];
};

export type OllamaModel = {
  name?: string;
  model?: string;
  modified_at?: string;
  size?: number;
  digest?: string;
  details?: {
    family?: string;
    parameter_size?: string;
    quantization_level?: string;
  };
};

export type OllamaModelsResponse = {
  models: OllamaModel[];
};

export type ProviderTestResponse = {
  ok: boolean;
  provider?: Partial<AiProviderConfig>;
  profile?: Partial<AiProfileConfig>;
  requires_image_upload_opt_in?: boolean;
  model_installed?: boolean;
  models?: OllamaModel[];
  litellm_available?: boolean;
  missing_api_key?: boolean;
};

export type UsageSnapshot = {
  profiles: Record<string, Record<string, number>>;
  raw_counters: Record<string, number>;
};
