import type { HomeAssistantRegistryResponse, HomeAssistantServerPublic } from "../types";

async function isHomeAssistantExtensionLoaded(): Promise<boolean> {
  try {
    const response = await fetch("/api/extensions");
    if (!response.ok) return false;
    const data = await response.json();
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

export async function fetchHomeAssistantServers(): Promise<HomeAssistantServerPublic[]> {
  const response = await fetch("/api/home_assistant/servers");
  if (response.status === 404 && !(await isHomeAssistantExtensionLoaded())) throw missingExtensionError(404);
  if (!response.ok) throw new Error(`Failed to list Home Assistant servers: ${response.status}`);
  const data = await response.json();
  return Array.isArray(data) ? (data as HomeAssistantServerPublic[]) : [];
}

export async function fetchHomeAssistantRegistry(serverId: string): Promise<HomeAssistantRegistryResponse> {
  const response = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/registry`);
  if (response.status === 404 && !(await isHomeAssistantExtensionLoaded())) throw missingExtensionError(404);
  if (!response.ok) throw new Error(`Failed to load Home Assistant registry: ${response.status}`);
  return response.json();
}

export async function fetchHomeAssistantStates(serverId: string, entityIds: string[]): Promise<Record<string, any>> {
  const ids = entityIds.map((s) => s.trim()).filter(Boolean);
  if (ids.length === 0) return {};
  const response = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/states`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ entity_ids: ids }),
  });
  if (response.status === 404 && !(await isHomeAssistantExtensionLoaded())) throw missingExtensionError(404);
  if (!response.ok) throw new Error(`Failed to fetch entity states: ${response.status}`);
  const data = await response.json();
  return data && typeof data === "object" ? (data as Record<string, any>) : {};
}
