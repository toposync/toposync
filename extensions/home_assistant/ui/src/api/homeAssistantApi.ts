import { requestJson } from "@toposync/plugin-api";

import type { HomeAssistantRegistryResponse, HomeAssistantServerPublic } from "../types";

async function isHomeAssistantExtensionLoaded(): Promise<boolean> {
  try {
    const data = await requestJson<unknown>("/api/extensions");
    if (!Array.isArray(data)) return false;
    return data.some((item) => item && typeof item === "object" && (item as any).id === "com.toposync.home_assistant");
  } catch {
    return false;
  }
}

function missingExtensionError(status: number): Error {
  return new Error(
    `Home Assistant extension not loaded on backend (HTTP ${status}). ` +
      `If you're running from source, run \`uv sync --group extensions\` (or start the backend with \`uv run --group extensions ...\`) and restart the server.`,
  );
}

function errorStatus(error: unknown): number | null {
  return error && typeof error === "object" && "status" in error ? Number((error as { status?: unknown }).status) : null;
}

export async function fetchHomeAssistantServers(): Promise<HomeAssistantServerPublic[]> {
  const data = await requestJson<unknown>("/api/home_assistant/servers").catch(async (error) => {
    if (errorStatus(error) === 404 && !(await isHomeAssistantExtensionLoaded())) {
      throw missingExtensionError(404);
    }
    throw error;
  });
  return Array.isArray(data) ? (data as HomeAssistantServerPublic[]) : [];
}

export async function fetchHomeAssistantRegistry(serverId: string): Promise<HomeAssistantRegistryResponse> {
  return requestJson<HomeAssistantRegistryResponse>(`/api/home_assistant/${encodeURIComponent(serverId)}/registry`).catch(
    async (error) => {
      if (errorStatus(error) === 404 && !(await isHomeAssistantExtensionLoaded())) {
        throw missingExtensionError(404);
      }
      throw error;
    },
  );
}

export async function fetchHomeAssistantStates(serverId: string, entityIds: string[]): Promise<Record<string, any>> {
  const ids = entityIds.map((s) => s.trim()).filter(Boolean);
  if (ids.length === 0) return {};
  const data = await requestJson<unknown>(`/api/home_assistant/${encodeURIComponent(serverId)}/states`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ entity_ids: ids }),
  }).catch(async (error) => {
    if (errorStatus(error) === 404 && !(await isHomeAssistantExtensionLoaded())) {
      throw missingExtensionError(404);
    }
    throw error;
  });
  return data && typeof data === "object" ? (data as Record<string, any>) : {};
}
