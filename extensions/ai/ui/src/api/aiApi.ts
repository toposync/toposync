import { requestJson } from "@toposync/plugin-api";

import type {
  AiCatalogResponse,
  AiExtensionSettings,
  OllamaModelsResponse,
  ProviderTestResponse,
  UsageSnapshot,
} from "../types";

export async function fetchAiCatalog(signal?: AbortSignal): Promise<AiCatalogResponse> {
  return requestJson<AiCatalogResponse>("/api/ai/catalog", { signal });
}

export async function fetchAiSettingsDefaults(signal?: AbortSignal): Promise<AiExtensionSettings> {
  return requestJson<AiExtensionSettings>("/api/ai/settings/defaults", { signal });
}

export async function fetchAiSettings(signal?: AbortSignal): Promise<AiExtensionSettings> {
  return requestJson<AiExtensionSettings>("/api/ai/settings", { signal });
}

export async function fetchAiUsage(signal?: AbortSignal): Promise<UsageSnapshot> {
  return requestJson<UsageSnapshot>("/api/ai/usage", { signal });
}

export async function fetchOllamaModels(host?: string, signal?: AbortSignal): Promise<OllamaModelsResponse> {
  const query = host?.trim() ? `?host=${encodeURIComponent(host.trim())}` : "";
  return requestJson<OllamaModelsResponse>(`/api/ai/ollama/models${query}`, { signal });
}

export async function pullOllamaModel(model: string, host?: string): Promise<unknown> {
  return requestJson<unknown>("/api/ai/ollama/pull", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model, host: host?.trim() || undefined }),
  });
}

export async function testAiProvider(payload: {
  provider_id?: string;
  provider?: Record<string, unknown>;
  profile_id?: string;
  model?: string;
}): Promise<ProviderTestResponse> {
  return requestJson<ProviderTestResponse>("/api/ai/providers/test", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}
