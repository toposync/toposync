export type EmitEventResponse = {
  payload: unknown;
  result: any;
  prevented_default: boolean;
  stopped: boolean;
};

import type { CompositionElement } from "@toposync/plugin-api";

export type Composition = {
  id: string;
  name: string;
  elements: CompositionElement[];
};

export type CompositionSummary = {
  id: string;
  name: string;
};

export type CompositionsIndex = {
  active_composition_id: string;
  compositions: CompositionSummary[];
};

export type DeleteCompositionResponse = {
  active_composition_id: string;
  compositions: CompositionSummary[];
  active_composition: Composition;
};

export type AppSettings = {
  core: Record<string, unknown>;
  extensions: Record<string, Record<string, unknown>>;
};

export async function fetchExtensions(): Promise<any[]> {
  const res = await fetch("/api/extensions");
  if (!res.ok) throw new Error(`Failed to list extensions: ${res.status}`);
  return res.json();
}

export async function getSettings(): Promise<AppSettings> {
  const res = await fetch("/api/settings");
  if (!res.ok) throw new Error(`Failed to fetch settings: ${res.status}`);
  return res.json();
}

export async function patchExtensionSettings(
  extensionId: string,
  patch: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const res = await fetch(`/api/settings/extensions/${encodeURIComponent(extensionId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch ?? {}),
  });
  if (!res.ok) throw new Error(`Failed to update settings for ${extensionId}: ${res.status}`);
  const body = await res.json();
  return body?.settings ?? {};
}

export async function emitEvent(eventName: string, payload: unknown, context: Record<string, unknown> = {}): Promise<EmitEventResponse> {
  const res = await fetch(`/api/events/${encodeURIComponent(eventName)}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ payload, context }),
  });
  if (!res.ok) throw new Error(`Failed to emit ${eventName}: ${res.status}`);
  return res.json();
}

export async function getDevice(deviceId: string): Promise<{ device_id: string; state: boolean }> {
  const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}`);
  if (!res.ok) throw new Error(`Failed to fetch device ${deviceId}: ${res.status}`);
  return res.json();
}

export async function getComposition(): Promise<Composition> {
  const res = await fetch("/api/composition");
  if (!res.ok) throw new Error(`Failed to fetch composition: ${res.status}`);
  return res.json();
}

export async function putComposition(composition: Composition): Promise<Composition> {
  const res = await fetch("/api/composition", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(composition),
  });
  if (!res.ok) throw new Error(`Failed to save composition: ${res.status}`);
  return res.json();
}

export async function listCompositions(): Promise<CompositionsIndex> {
  const res = await fetch("/api/compositions");
  if (!res.ok) throw new Error(`Failed to list compositions: ${res.status}`);
  return res.json();
}

export async function createComposition(name: string): Promise<Composition> {
  const res = await fetch("/api/compositions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(`Failed to create composition: ${res.status}`);
  return res.json();
}

export async function activateComposition(compositionId: string): Promise<Composition> {
  const res = await fetch(`/api/compositions/${encodeURIComponent(compositionId)}/activate`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to activate composition: ${res.status}`);
  return res.json();
}

export async function renameComposition(compositionId: string, name: string): Promise<Composition> {
  const res = await fetch(`/api/compositions/${encodeURIComponent(compositionId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(`Failed to rename composition: ${res.status}`);
  return res.json();
}

export async function deleteComposition(compositionId: string): Promise<DeleteCompositionResponse> {
  const res = await fetch(`/api/compositions/${encodeURIComponent(compositionId)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to delete composition: ${res.status}`);
  return res.json();
}
