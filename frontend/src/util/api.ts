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

export async function fetchExtensions(): Promise<any[]> {
  const res = await fetch("/api/extensions");
  if (!res.ok) throw new Error(`Failed to list extensions: ${res.status}`);
  return res.json();
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
