export type EmitEventResponse = {
  payload: unknown;
  result: any;
  prevented_default: boolean;
  stopped: boolean;
};

import type { CompositionElement } from "@toposync/plugin-api";
import type { Notification } from "@toposync/plugin-api";

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

export type Pipeline = {
  name: string;
  type: "reuse" | "final";
  enabled?: boolean;
  processing_server_id?: string;
  editor_mode?: "interactive" | "json" | "python";
  python_source?: string;
  graph: unknown;
};

export type ProcessingServer = {
  id: string;
  name: string;
  kind: "inprocess" | "http";
  url: string;
  username?: string;
  password?: string;
};

export type ProcessingServerStatus = {
  ok: boolean;
  status?: Record<string, unknown>;
  error?: string | null;
};

export type CameraSummary = {
  id: string;
  name: string;
  connection_type: string;
};

export type CamerasIndexResponse = {
  cameras: CameraSummary[];
};

export type CameraContextArea = {
  id: string;
  name: string;
  vertices_count: number;
};

export type CameraContextCameraElement = {
  id: string;
  name: string;
  control_points_pairs: number;
  has_mapping: boolean;
};

export type CameraContextComposition = {
  id: string;
  name: string;
  camera_elements: CameraContextCameraElement[];
  areas: CameraContextArea[];
};

export type CameraContextsResponse = {
  camera_id: string;
  compositions: CameraContextComposition[];
};

export type PipelineOperatorPort = {
  name: string;
  required: boolean;
  description: string;
};

export type PipelineOperatorDefinition = {
  id: string;
  description: string;
  inputs: PipelineOperatorPort[];
  outputs: PipelineOperatorPort[];
  capabilities: string[];
  defaults: Record<string, unknown>;
  config_schema: Record<string, unknown>;
  share_strategy: "by_signature" | "never";
};

export type NotificationsPage = {
  notifications: Notification[];
  next_cursor: number | null;
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

export async function listNotifications(before: number | null = null, limit = 40): Promise<NotificationsPage> {
  const params = new URLSearchParams();
  if (before != null) params.set("before", String(before));
  params.set("limit", String(limit));
  const res = await fetch(`/api/notifications?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to list notifications: ${res.status}`);
  const body = (await res.json()) as { notifications?: Notification[]; next_cursor?: number | null };
  return { notifications: body.notifications ?? [], next_cursor: body.next_cursor ?? null };
}

export async function getNotification(notificationId: string): Promise<Notification> {
  const res = await fetch(`/api/notifications/${encodeURIComponent(notificationId)}`);
  if (!res.ok) throw new Error(`Failed to fetch notification ${notificationId}: ${res.status}`);
  return res.json();
}

export async function listProcessingServers(): Promise<ProcessingServer[]> {
  const res = await fetch("/api/processing-servers");
  if (!res.ok) throw new Error(`Failed to list processing servers: ${res.status}`);
  const body = (await res.json()) as { servers?: ProcessingServer[] };
  return body.servers ?? [];
}

export async function putProcessingServer(server: ProcessingServer): Promise<ProcessingServer> {
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(server.id)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(server),
  });
  if (!res.ok) throw new Error(`Failed to save processing server ${server.id}: ${res.status}`);
  return res.json();
}

export async function deleteProcessingServer(serverId: string): Promise<ProcessingServer> {
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to delete processing server ${serverId}: ${res.status}`);
  return res.json();
}

export async function getProcessingServerStatus(serverId: string): Promise<ProcessingServerStatus> {
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}/status`);
  if (!res.ok) throw new Error(`Failed to fetch processing server status ${serverId}: ${res.status}`);
  return res.json();
}

export async function listPipelines(): Promise<Pipeline[]> {
  const res = await fetch("/api/pipelines");
  if (!res.ok) throw new Error(`Failed to list pipelines: ${res.status}`);
  const body = (await res.json()) as { pipelines?: Pipeline[] };
  return body.pipelines ?? [];
}

export async function createPipeline(pipeline: Pipeline): Promise<Pipeline> {
  const res = await fetch("/api/pipelines", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(pipeline),
  });
  if (!res.ok) throw new Error(`Failed to create pipeline: ${res.status}`);
  return res.json();
}

export async function putPipeline(name: string, pipeline: Pipeline): Promise<Pipeline> {
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(pipeline),
  });
  if (!res.ok) throw new Error(`Failed to save pipeline ${name}: ${res.status}`);
  return res.json();
}

export async function deletePipeline(name: string): Promise<Pipeline> {
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to delete pipeline ${name}: ${res.status}`);
  return res.json();
}

export async function compilePipeline(pipeline: Pipeline): Promise<any> {
  const res = await fetch("/api/pipelines/compile", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ pipeline }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = (body as any)?.detail ? String((body as any).detail) : String(res.status);
    throw new Error(detail);
  }
  return res.json();
}

export async function listCamerasIndex(): Promise<CamerasIndexResponse> {
  const res = await fetch("/api/cameras/index");
  if (!res.ok) throw new Error(`Failed to list cameras index: ${res.status}`);
  const body = (await res.json()) as { cameras?: CameraSummary[] };
  return { cameras: Array.isArray(body.cameras) ? body.cameras : [] };
}

export async function getCameraContexts(cameraId: string): Promise<CameraContextsResponse> {
  const res = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/contexts`);
  if (!res.ok) throw new Error(`Failed to fetch camera contexts: ${res.status}`);
  return res.json();
}

export async function listPipelineOperators(): Promise<PipelineOperatorDefinition[]> {
  const res = await fetch("/api/pipelines/operators");
  if (!res.ok) throw new Error(`Failed to list pipeline operators: ${res.status}`);
  const body = (await res.json()) as { operators?: PipelineOperatorDefinition[] };
  return body.operators ?? [];
}
