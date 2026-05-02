import { resolveToposyncUrl } from "@toposync/plugin-api";

import type {
  AiCatalogResponse,
  AiExtensionSettings,
  OllamaModelsResponse,
  ProviderTestResponse,
  UsageSnapshot,
} from "../types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

async function parseErrorResponse(response: Response): Promise<string> {
  const fallback = `HTTP ${response.status}`;
  try {
    const json = await response.json();
    if (!isRecord(json)) return fallback;
    const detail = json.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (isRecord(detail)) {
      const error = detail.error;
      if (typeof error === "string" && error.trim()) return error.trim();
    }
    return fallback;
  } catch {
    try {
      const text = await response.text();
      return text.trim() || fallback;
    } catch {
      return fallback;
    }
  }
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(resolveToposyncUrl(input), init);
  if (!response.ok) {
    throw new Error(await parseErrorResponse(response));
  }
  return (await response.json()) as T;
}

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
