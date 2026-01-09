import type { HomeAssistantRegistryResponse, HomeAssistantServerPublic } from "../types";

export async function fetchHomeAssistantServers(): Promise<HomeAssistantServerPublic[]> {
  const response = await fetch("/api/home_assistant/servers");
  if (!response.ok) throw new Error(`Failed to list Home Assistant servers: ${response.status}`);
  const data = await response.json();
  return Array.isArray(data) ? (data as HomeAssistantServerPublic[]) : [];
}

export async function fetchHomeAssistantRegistry(serverId: string): Promise<HomeAssistantRegistryResponse> {
  const response = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/registry`);
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
  if (!response.ok) throw new Error(`Failed to fetch entity states: ${response.status}`);
  const data = await response.json();
  return data && typeof data === "object" ? (data as Record<string, any>) : {};
}

