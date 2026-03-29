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
  meta?: Record<string, unknown>;
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

export type AuthRole = "owner" | "admin" | "member" | "guest" | "service";

export type AuthUser = {
  id: string;
  username: string;
  display_name: string;
  role: AuthRole;
  is_disabled: boolean;
  sessions: number;
  grants: Array<{
    id: string;
    action: string;
    resource_type: string;
    include: string[];
    exclude: string[];
    created_at: number;
    updated_at: number;
  }>;
  created_at: number;
  updated_at: number;
};

export type AuthStatus = {
  mode: "enforced" | "bypass" | string;
  requires_setup: boolean;
  authenticated: boolean;
  user: AuthUser | null;
};

export type AccessUsersPayload = {
  users: AuthUser[];
  grants_catalog: Record<string, string[]>;
};

export type AccessOptionItem = {
  id: string;
  name: string;
};

export type AccessOptionsPayload = {
  extensions: AccessOptionItem[];
  compositions: Array<{
    id: string;
    name: string;
    areas: AccessOptionItem[];
  }>;
  event_patterns: string[];
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

export type PipelineAlert = {
  severity: "info" | "warning";
  code: string;
  message: string;
  suggestion?: string;
  node_id?: string | null;
  operator_id?: string | null;
  edge?: unknown;
};

export type PipelineCompileOutput = {
  pipeline: Record<string, unknown>;
  shared_signatures: Record<string, Array<Record<string, unknown>>>;
  alerts: PipelineAlert[];
};

export type PipelineCompilePythonOutput = PipelineCompileOutput & {
  graph: Record<string, unknown>;
};

export type PipelinePreviewFallbackSnapshotRequest = {
  pipeline_name: string;
  node_id: string;
  source_id: string;
};

export type PipelinePreviewFrameRequest = {
  pipeline: Pipeline;
  fallback_snapshot?: PipelinePreviewFallbackSnapshotRequest | null;
  timeout_seconds?: number;
  format?: "png" | "jpg";
  jpeg_quality?: number;
};

export type PipelineStats = {
  pipeline_name: string;
  window_seconds: number;
  bucket_seconds: number;
  node_outputs: Record<string, number>;
  updated_at: number;
};

export type PipelineTelemetryNumericPoint = {
  bucket_start_s: number;
  count: number;
  min: number;
  max: number;
  avg: number;
};

export type PipelineTelemetryNumeric = {
  pipeline_name: string;
  node_id: string;
  metric_id: string;
  window_seconds: number;
  bucket_seconds: number;
  histogram_min: number;
  histogram_max: number;
  histogram_bins: number[];
  points: PipelineTelemetryNumericPoint[];
  total_count: number;
  total_min: number;
  total_max: number;
  total_avg: number;
  updated_at: number;
};

export type PipelineTelemetryAggregateNumeric = {
  metric_id: string;
  aggregation: string;
  pipeline_count: number;
  series_count: number;
  window_seconds: number;
  bucket_seconds: number;
  histogram_min: number;
  histogram_max: number;
  histogram_bins: number[];
  points: PipelineTelemetryNumericPoint[];
  total_count: number;
  total_min: number;
  total_max: number;
  total_avg: number;
  updated_at: number;
};

export type PipelinesTelemetryNumericOverview = {
  aggregation: string;
  series: PipelineTelemetryAggregateNumeric[];
};

export type PipelineTelemetryImageMarker = {
  pipeline_name?: string | null;
  ts: number;
  node_id: string;
  metric_id: string;
  rel_path: string;
  image_key?: string | null;
  confidence?: number | null;
};

export type PipelineTelemetryImageMarkers = {
  pipeline_name: string;
  markers: PipelineTelemetryImageMarker[];
};

export type PipelinesTelemetryImageMarkers = {
  aggregation: string;
  pipeline_count: number;
  markers: PipelineTelemetryImageMarker[];
};

export type PipelineTemplateApplyCamerasRequest = {
  template_pipeline_name: string;
  camera_ids: string[];
  instance_type?: "reuse" | "final";
  enabled?: boolean;
  processing_server_id?: string;
  conflict?: "skip" | "replace" | "error";
  dry_run?: boolean;
};

export type PipelineTemplateApplyCamerasResponse = {
  dry_run: boolean;
  created: string[];
  updated: string[];
  skipped: Array<Record<string, unknown>>;
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

export type ProcessingServerVisionManifestImportRequest = {
  manifest_text: string;
  artifact_path?: string;
  replace_existing?: boolean;
  imported_by?: Record<string, unknown>;
};

export type ProcessingServerVisionManifestImportResponse = {
  model_id: string;
  display_name: string;
  task: string;
  runtime: string;
  artifact_path: string;
  artifact_exists: boolean;
  manifest_path: string;
  custom: boolean;
  replaced: boolean;
  provenance?: Record<string, unknown>;
};

export type ProcessingServerVisionModelInstallRequest = {
  force?: boolean;
  mode?: string;
  acknowledge_upstream_terms?: boolean;
};

export type ProcessingServerVisionModelInstallResponse = {
  job_id: string;
  model_id: string;
  display_name: string;
  artifact_path: string;
  status: string;
  phase: string;
  progress_pct: number;
  bytes_completed: number;
  bytes_total: number;
  source_kind: string;
  source_label: string;
  error?: string | null;
  started_at: number;
  updated_at: number;
  finished_at?: number | null;
};

export type ProcessingServerVisionModelArtifactUploadResponse = {
  model_id: string;
  display_name: string;
  task: string;
  runtime: string;
  artifact_path: string;
  artifact_exists: boolean;
  expected_filename: string;
  uploaded_filename: string;
  sha256: string;
  size_bytes: number;
  replaced: boolean;
  custom: boolean;
};

export type ProcessingServerVisionCustomOnnxTensor = {
  name: string;
  dtype: string;
  shape: Array<number | string | null>;
  rank: number;
};

export type ProcessingServerVisionCustomOnnxSuggestionDefaults = {
  tensor_name: string;
  output_name: string;
  width: number;
  height: number;
  layout: string;
  channels: number;
  color_order: string;
  resize_mode: string;
  rescale_factor: number;
  normalization_mean: number[];
  normalization_std: number[];
  box_format: string;
  labels_count_hint: number;
};

export type ProcessingServerVisionCustomOnnxTaskSuggestion = {
  task: "classification" | "detection";
  adapter_family: string;
  label: string;
  reason: string;
  confidence: string;
  defaults: ProcessingServerVisionCustomOnnxSuggestionDefaults;
};

export type ProcessingServerVisionCustomOnnxInspectResponse = {
  artifact_path: string;
  uploaded_filename: string;
  file_size_bytes: number;
  suggested_display_name: string;
  input_tensors: ProcessingServerVisionCustomOnnxTensor[];
  output_tensors: ProcessingServerVisionCustomOnnxTensor[];
  task_suggestions: ProcessingServerVisionCustomOnnxTaskSuggestion[];
  supported_task_adapters: Array<{
    task: "classification" | "detection";
    adapter_family: string;
    label: string;
  }>;
};

export type ProcessingServerVisionCustomOnnxRequest = {
  artifact_path: string;
  uploaded_filename?: string;
  display_name: string;
  task: "classification" | "detection";
  adapter_family: string;
  tensor_name?: string;
  width?: number;
  height?: number;
  layout?: string;
  color_order?: string;
  resize_mode?: string;
  rescale_factor?: number;
  normalization_mean?: number[];
  normalization_std?: number[];
  output_name?: string;
  box_format?: string;
  class_labels?: string[];
  source_url?: string;
  replace_existing?: boolean;
};

export type ProcessingServerVisionCustomOnnxPreviewResponse = {
  task: "classification" | "detection";
  summary: Record<string, unknown>;
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
  vertices: Array<{
    x: number;
    z: number;
  }>;
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

export type PipelineOperatorExpressionHint = {
  kind: "payload_path" | "metadata_path" | "artifact_name";
  path?: string | null;
  value?: string | null;
  type?: string;
  description?: string;
  examples?: string[];
  enum_values?: string[];
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
  requires_payload_keys?: string[];
  requires_artifacts?: string[];
  requires_source_fields?: string[];
  requires_media_fields?: string[];
  produces_payload_keys?: string[];
  produces_artifacts?: string[];
  produces_source_fields?: string[];
  produces_media_fields?: string[];
  input_modalities?: string[];
  output_modalities?: string[];
  expression_hints?: PipelineOperatorExpressionHint[];
};

export type FilterExpressionValidationMarker = {
  start_line_number: number;
  start_column: number;
  end_line_number: number;
  end_column: number;
};

export type FilterExpressionValidateResponse = {
  ok: boolean;
  normalized_expression: string;
  error?: string | null;
  marker?: FilterExpressionValidationMarker | null;
};

export type HomeAssistantServerInfo = {
  id: string;
  name: string;
  host: string;
};

export type HomeAssistantServiceInfo = {
  domain: string;
  service: string;
  name?: string;
  description?: string;
};

export type NotificationsPage = {
  notifications: Notification[];
  next_cursor: number | null;
};

export type StreamingTransmissionOutput = {
  id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  enabled?: boolean;
  authentication?: {
    enabled?: boolean;
    username?: string | null;
    password?: string | null;
  };
};

export type StreamingTransmission = {
  id: string;
  name: string;
  path: string;
  enabled?: boolean;
  host_server_id?: string;
  camera_controls?: { enabled?: boolean; camera_id?: string | null } | null;
  outputs?: StreamingTransmissionOutput[];
};

export type StreamingTransmissionCameraPreset = {
  token: string;
  name?: string;
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
};

export type StreamingTransmissionCameraPresetsResponse = {
  transmission_id: string;
  camera_id: string;
  presets: StreamingTransmissionCameraPreset[];
};

export type StreamingTransmissionCameraStatus = {
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
  move_status?: string;
  error?: string;
  utc_time?: string;
};

export type StreamingTransmissionCameraStatusResponse = {
  transmission_id: string;
  camera_id: string;
  status: StreamingTransmissionCameraStatus;
};

export type StreamingTransmissionUrlOutput = {
  output_id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  resolved_engine_path: string;
  url: string;
  requires_auth?: boolean;
  auth_username?: string | null;
};

export type StreamingTransmissionUrlsResponse = {
  transmission_id: string;
  engine_running: boolean;
  outputs: StreamingTransmissionUrlOutput[];
  warnings?: string[];
};

export type StreamingTransmissionDemandPrimeResponse = {
  transmission_id: string;
  primed: boolean;
  primed_outputs: number;
};

export type StreamingOutputRuntimeStatus = {
  output_key: string;
  output_id: string;
  transmission_id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  resolved_engine_path: string;
  viewer_count: number;
  demand_signal: boolean;
  publisher_running: boolean;
  publisher_pid?: number | null;
  publisher_frames_sent: number;
  publisher_last_error?: string | null;
  publisher_active_codec?: string | null;
  publisher_hardware_accelerated?: boolean;
  publisher_restart_count?: number;
};

export type StreamingOutputsRuntimeResponse = {
  updated_at_unix: number;
  outputs: StreamingOutputRuntimeStatus[];
};

async function _parseHttpError(res: Response, fallback: string): Promise<string> {
  try {
    const json = (await res.json()) as any;
    const detail = json?.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
  } catch {
    // ignore
  }
  try {
    const text = String(await res.text()).trim();
    if (text) return text;
  } catch {
    // ignore
  }
  return fallback;
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const res = await fetch("/api/auth/status");
  if (!res.ok) throw new Error(`Failed to load auth status: ${res.status}`);
  return res.json();
}

export async function setupOwner(params: {
  username: string;
  password: string;
  display_name?: string;
  device_label?: string;
}): Promise<AuthUser> {
  const res = await fetch("/api/auth/setup", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      username: params.username,
      password: params.password,
      display_name: params.display_name ?? "",
      device_label: params.device_label ?? "browser",
    }),
  });
  if (!res.ok) throw new Error(`Failed to setup owner: ${res.status}`);
  const body = await res.json();
  return body.user;
}

export async function login(params: {
  username: string;
  password: string;
  device_label?: string;
}): Promise<AuthUser> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      username: params.username,
      password: params.password,
      device_label: params.device_label ?? "browser",
    }),
  });
  if (!res.ok) throw new Error(`Failed to login: ${res.status}`);
  const body = await res.json();
  return body.user;
}

export async function logout(): Promise<void> {
  const res = await fetch("/api/auth/logout", { method: "POST" });
  if (!res.ok) throw new Error(`Failed to logout: ${res.status}`);
}

export async function startPairing(params?: { device_label?: string }): Promise<{ code: string; expires_at: number }> {
  const res = await fetch("/api/auth/pair/start", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      device_label: params?.device_label ?? "mobile",
    }),
  });
  if (!res.ok) throw new Error(`Failed to start pairing: ${res.status}`);
  return res.json();
}

export async function completePairing(params: { code: string; device_label?: string }): Promise<AuthUser> {
  const res = await fetch("/api/auth/pair/complete", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      code: params.code,
      device_label: params.device_label ?? "mobile",
    }),
  });
  if (!res.ok) throw new Error(`Failed to complete pairing: ${res.status}`);
  const body = await res.json();
  return body.user;
}

export async function listAccessUsers(): Promise<AccessUsersPayload> {
  const res = await fetch("/api/access/users");
  if (!res.ok) throw new Error(`Failed to list access users: ${res.status}`);
  return res.json();
}

export async function getAccessOptions(): Promise<AccessOptionsPayload> {
  const res = await fetch("/api/access/options");
  if (!res.ok) throw new Error(`Failed to fetch access options: ${res.status}`);
  return res.json();
}

export async function createAccessUser(payload: {
  username: string;
  password: string;
  role: AuthRole;
  display_name?: string;
}): Promise<AuthUser> {
  const res = await fetch("/api/access/users", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to create access user: ${res.status}`);
  return res.json();
}

export async function patchAccessUser(
  userId: string,
  payload: {
    display_name?: string;
    role?: AuthRole;
    password?: string;
    is_disabled?: boolean;
  },
): Promise<AuthUser> {
  const res = await fetch(`/api/access/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to patch access user ${userId}: ${res.status}`);
  return res.json();
}

export async function deleteAccessUser(userId: string): Promise<void> {
  const res = await fetch(`/api/access/users/${encodeURIComponent(userId)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to delete access user ${userId}: ${res.status}`);
}

export async function upsertAccessGrant(
  userId: string,
  payload: {
    action: string;
    resource_type: string;
    include: string[];
    exclude: string[];
  },
): Promise<AuthUser> {
  const res = await fetch(`/api/access/users/${encodeURIComponent(userId)}/grants`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to upsert grant for ${userId}: ${res.status}`);
  return res.json();
}

export async function deleteAccessGrant(userId: string, action: string, resourceType: string): Promise<AuthUser> {
  const query = new URLSearchParams({ action, resource_type: resourceType });
  const res = await fetch(`/api/access/users/${encodeURIComponent(userId)}/grants?${query.toString()}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Failed to delete grant for ${userId}: ${res.status}`);
  return res.json();
}

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

export async function importProcessingServerVisionManifest(
  serverId: string,
  payload: ProcessingServerVisionManifestImportRequest,
): Promise<ProcessingServerVisionManifestImportResponse> {
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}/vision/manifests/import`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(
      await _parseHttpError(res, `Failed to import vision manifest on processing server ${serverId}: ${res.status}`),
    );
  }
  return res.json();
}

export async function inspectProcessingServerCustomOnnx(
  serverId: string,
  file: File,
): Promise<ProcessingServerVisionCustomOnnxInspectResponse> {
  const form = new FormData();
  form.set("file", file, file.name);
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}/vision/custom-onnx/inspect`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    throw new Error(
      await _parseHttpError(res, `Failed to inspect custom ONNX on processing server ${serverId}: ${res.status}`),
    );
  }
  return res.json();
}

export async function previewProcessingServerCustomOnnx(
  serverId: string,
  payload: ProcessingServerVisionCustomOnnxRequest,
  image: File,
): Promise<ProcessingServerVisionCustomOnnxPreviewResponse> {
  const form = new FormData();
  form.set("config_json", JSON.stringify(payload));
  form.set("image", image, image.name);
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}/vision/custom-onnx/preview`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    throw new Error(
      await _parseHttpError(res, `Failed to preview custom ONNX on processing server ${serverId}: ${res.status}`),
    );
  }
  return res.json();
}

export async function importProcessingServerCustomOnnx(
  serverId: string,
  payload: ProcessingServerVisionCustomOnnxRequest,
): Promise<ProcessingServerVisionManifestImportResponse> {
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}/vision/custom-onnx/import`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(
      await _parseHttpError(res, `Failed to import custom ONNX on processing server ${serverId}: ${res.status}`),
    );
  }
  return res.json();
}

export async function installProcessingServerVisionModel(
  serverId: string,
  modelId: string,
  payload: ProcessingServerVisionModelInstallRequest = {},
): Promise<ProcessingServerVisionModelInstallResponse> {
  const res = await fetch(`/api/processing-servers/${encodeURIComponent(serverId)}/vision/models/${encodeURIComponent(modelId)}/install`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(
      await _parseHttpError(res, `Failed to install vision model ${modelId} on processing server ${serverId}: ${res.status}`),
    );
  }
  return res.json();
}

export async function uploadProcessingServerVisionModelArtifact(
  serverId: string,
  modelId: string,
  file: File,
  options: {
    onProgress?: (progressPct: number, bytesUploaded: number, bytesTotal: number) => void;
  } = {},
): Promise<ProcessingServerVisionModelArtifactUploadResponse> {
  return await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      `/api/processing-servers/${encodeURIComponent(serverId)}/vision/models/${encodeURIComponent(modelId)}/artifact`,
    );
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (!options.onProgress) return;
      const total = Number(event.total || file.size || 0);
      const loaded = Number(event.loaded || 0);
      const progress = total > 0 ? Math.max(0, Math.min(100, (loaded / total) * 100)) : 0;
      options.onProgress(progress, loaded, total);
    };
    xhr.onerror = () => reject(new Error(`Failed to upload vision model artifact for ${modelId}`));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response as ProcessingServerVisionModelArtifactUploadResponse);
        return;
      }
      const response = xhr.response;
      if (response && typeof response === "object" && "detail" in response && typeof response.detail === "string") {
        reject(new Error(response.detail));
        return;
      }
      reject(new Error(`Failed to upload vision model artifact ${modelId} on processing server ${serverId}: ${xhr.status}`));
    };
    const form = new FormData();
    form.set("file", file, file.name);
    xhr.send(form);
  });
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

export async function duplicatePipeline(name: string, newName: string): Promise<Pipeline> {
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}/duplicate`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ new_name: String(newName || "") }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = (body as any)?.detail ? String((body as any).detail) : String(res.status);
    throw new Error(detail);
  }
  return res.json();
}

export async function deletePipeline(name: string): Promise<Pipeline> {
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to delete pipeline ${name}: ${res.status}`);
  return res.json();
}

export async function getPipelineStats(name: string): Promise<PipelineStats> {
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}/stats`);
  if (!res.ok) throw new Error(`Failed to fetch pipeline stats ${name}: ${res.status}`);
  return res.json();
}

export async function resetPipelineStats(name: string): Promise<PipelineStats> {
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}/stats/reset`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to reset pipeline stats ${name}: ${res.status}`);
  return res.json();
}

export async function getPipelineTelemetryNumeric(
  name: string,
  nodeId: string,
  metricId: string,
  pointLimit: number = 720,
  windowSeconds?: number,
): Promise<PipelineTelemetryNumeric> {
  const params = new URLSearchParams({
    node_id: String(nodeId || ""),
    metric_id: String(metricId || ""),
    point_limit: String(Math.max(50, Math.min(5000, Math.floor(pointLimit || 720)))),
  });
  if (Number.isFinite(windowSeconds) && (windowSeconds ?? 0) > 0) {
    params.set("window_seconds", String(Math.max(1, Math.floor(windowSeconds ?? 0))));
  }
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}/telemetry/numeric?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to fetch pipeline telemetry numeric ${name}/${nodeId}/${metricId}: ${res.status}`);
  return res.json();
}

export async function getPipelineTelemetryImageMarkers(
  name: string,
  options?: { limit?: number; nodeId?: string; metricId?: string; windowSeconds?: number },
): Promise<PipelineTelemetryImageMarkers> {
  const params = new URLSearchParams();
  const limit = Math.max(1, Math.min(40000, Math.floor(options?.limit ?? 500)));
  params.set("limit", String(limit));
  if (options?.nodeId) params.set("node_id", String(options.nodeId));
  if (options?.metricId) params.set("metric_id", String(options.metricId));
  if (Number.isFinite(options?.windowSeconds) && (options?.windowSeconds ?? 0) > 0) {
    params.set("window_seconds", String(Math.max(1, Math.floor(options?.windowSeconds ?? 0))));
  }
  const res = await fetch(`/api/pipelines/${encodeURIComponent(name)}/telemetry/image-markers?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to fetch pipeline telemetry markers ${name}: ${res.status}`);
  return res.json();
}

export async function getPipelinesTelemetryNumericOverview(
  options?: { metricIds?: string[]; pipelineNames?: string[]; pointLimit?: number; windowSeconds?: number; aggregation?: string },
): Promise<PipelinesTelemetryNumericOverview> {
  const params = new URLSearchParams();
  const aggregation = String(options?.aggregation || "max").trim() || "max";
  params.set("aggregation", aggregation);
  const metricIds = Array.isArray(options?.metricIds) ? options?.metricIds : [];
  for (const metricId of metricIds) {
    const value = String(metricId || "").trim();
    if (value) params.append("metric_id", value);
  }
  const pipelineNames = Array.isArray(options?.pipelineNames) ? options?.pipelineNames : [];
  for (const pipelineName of pipelineNames) {
    const value = String(pipelineName || "").trim();
    if (value) params.append("pipeline_name", value);
  }
  const pointLimit = Math.max(50, Math.min(5000, Math.floor(options?.pointLimit ?? 720)));
  params.set("point_limit", String(pointLimit));
  if (Number.isFinite(options?.windowSeconds) && (options?.windowSeconds ?? 0) > 0) {
    params.set("window_seconds", String(Math.max(1, Math.floor(options?.windowSeconds ?? 0))));
  }
  const res = await fetch(`/api/pipelines/telemetry/all/numeric?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to fetch pipelines telemetry overview: ${res.status}`);
  return res.json();
}

export async function getPipelinesTelemetryImageMarkers(
  options?: { limit?: number; nodeId?: string; metricId?: string; pipelineNames?: string[]; windowSeconds?: number; aggregation?: string },
): Promise<PipelinesTelemetryImageMarkers> {
  const params = new URLSearchParams();
  const aggregation = String(options?.aggregation || "max").trim() || "max";
  params.set("aggregation", aggregation);
  const limit = Math.max(1, Math.min(40000, Math.floor(options?.limit ?? 500)));
  params.set("limit", String(limit));
  if (options?.nodeId) params.set("node_id", String(options.nodeId));
  if (options?.metricId) params.set("metric_id", String(options.metricId));
  const pipelineNames = Array.isArray(options?.pipelineNames) ? options?.pipelineNames : [];
  for (const pipelineName of pipelineNames) {
    const value = String(pipelineName || "").trim();
    if (value) params.append("pipeline_name", value);
  }
  if (Number.isFinite(options?.windowSeconds) && (options?.windowSeconds ?? 0) > 0) {
    params.set("window_seconds", String(Math.max(1, Math.floor(options?.windowSeconds ?? 0))));
  }
  const res = await fetch(`/api/pipelines/telemetry/all/image-markers?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to fetch pipelines telemetry markers: ${res.status}`);
  return res.json();
}

export async function compilePipeline(pipeline: Pipeline): Promise<PipelineCompileOutput> {
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

export async function compilePipelinePython(pipeline: Pipeline): Promise<PipelineCompilePythonOutput> {
  const res = await fetch("/api/pipelines/compile-python", {
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

export async function applyPipelineTemplateToCameras(
  payload: PipelineTemplateApplyCamerasRequest,
): Promise<PipelineTemplateApplyCamerasResponse> {
  const res = await fetch("/api/pipelines/templates/apply-cameras", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
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

export async function fetchCameraSnapshot(cameraId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/snapshot`, { signal });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${response.status}`);
  }
  return response.blob();
}

export async function fetchRtspSnapshot(
  options: { url: string; username?: string; password?: string },
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch("/api/cameras/rtsp/snapshot", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url: options.url,
      username: options.username ?? "",
      password: options.password ?? "",
    }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${response.status}`);
  }
  return response.blob();
}

export async function fetchPipelinePreviewFrame(
  payload: PipelinePreviewFrameRequest,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch("/api/pipelines/preview/frame", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Preview failed: ${response.status}`);
  }
  return response.blob();
}

export async function listPipelineOperators(): Promise<PipelineOperatorDefinition[]> {
  const res = await fetch("/api/pipelines/operators");
  if (!res.ok) throw new Error(`Failed to list pipeline operators: ${res.status}`);
  const body = (await res.json()) as { operators?: PipelineOperatorDefinition[] };
  return body.operators ?? [];
}

export async function validateFilterExpression(
  expression: string,
  options?: { signal?: AbortSignal },
): Promise<FilterExpressionValidateResponse> {
  const res = await fetch("/api/pipelines/filter-expression/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expression }),
    signal: options?.signal,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `Failed to validate filter expression: ${res.status}`);
  }
  return (await res.json()) as FilterExpressionValidateResponse;
}

export async function listHomeAssistantServers(): Promise<HomeAssistantServerInfo[]> {
  const res = await fetch("/api/home_assistant/servers");
  if (!res.ok) throw new Error(`Failed to list Home Assistant servers: ${res.status}`);
  const body = await res.json();
  return Array.isArray(body) ? (body as HomeAssistantServerInfo[]) : [];
}

export async function listHomeAssistantServices(
  serverId: string,
  options?: { domain?: string },
): Promise<HomeAssistantServiceInfo[]> {
  const query = new URLSearchParams();
  if (options?.domain) query.set("domain", options.domain);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const res = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/services${suffix}`);
  if (!res.ok) throw new Error(`Failed to list Home Assistant services: ${res.status}`);
  const body = await res.json();
  return Array.isArray(body) ? (body as HomeAssistantServiceInfo[]) : [];
}

export async function listStreamingTransmissions(): Promise<StreamingTransmission[]> {
  const res = await fetch("/api/streams/transmissions");
  if (!res.ok) throw new Error(`Failed to list streaming transmissions: ${res.status}`);
  return (await res.json()) as StreamingTransmission[];
}

export async function getStreamingTransmissionUrls(transmissionId: string): Promise<StreamingTransmissionUrlsResponse> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/urls`);
  if (!res.ok) throw new Error(`Failed to fetch streaming URLs for ${transmissionId}: ${res.status}`);
  return (await res.json()) as StreamingTransmissionUrlsResponse;
}

export async function primeStreamingTransmissionDemand(
  transmissionId: string,
): Promise<StreamingTransmissionDemandPrimeResponse> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/demand/prime`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Failed to prime streaming demand for ${transmissionId}: ${res.status}`);
  return (await res.json()) as StreamingTransmissionDemandPrimeResponse;
}

export async function getStreamingTransmissionCameraPresets(
  transmissionId: string,
): Promise<StreamingTransmissionCameraPresetsResponse> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/camera/presets`);
  if (!res.ok) {
    throw new Error(await _parseHttpError(res, `Failed to fetch PTZ presets for ${transmissionId}: ${res.status}`));
  }
  return (await res.json()) as StreamingTransmissionCameraPresetsResponse;
}

export async function gotoStreamingTransmissionCameraPreset(
  transmissionId: string,
  presetToken: string,
): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/camera/goto-preset`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ preset_token: presetToken }),
  });
  if (!res.ok) {
    throw new Error(await _parseHttpError(res, `Failed to go to PTZ preset for ${transmissionId}: ${res.status}`));
  }
  return (await res.json()) as { ok: boolean };
}

export async function getStreamingTransmissionCameraStatus(
  transmissionId: string,
): Promise<StreamingTransmissionCameraStatusResponse> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/camera/status`);
  if (!res.ok) {
    throw new Error(await _parseHttpError(res, `Failed to fetch PTZ status for ${transmissionId}: ${res.status}`));
  }
  return (await res.json()) as StreamingTransmissionCameraStatusResponse;
}

export async function moveStreamingTransmissionCamera(
  transmissionId: string,
  payload: { pan: number; tilt: number; zoom: number; timeout_s?: number | null },
): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/camera/move`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(await _parseHttpError(res, `Failed to move PTZ camera for ${transmissionId}: ${res.status}`));
  }
  return (await res.json()) as { ok: boolean };
}

export async function stopStreamingTransmissionCamera(
  transmissionId: string,
  payload?: { pan_tilt?: boolean; zoom?: boolean },
): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/camera/stop`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
  if (!res.ok) {
    throw new Error(await _parseHttpError(res, `Failed to stop PTZ camera for ${transmissionId}: ${res.status}`));
  }
  return (await res.json()) as { ok: boolean };
}

export async function getStreamingOutputsRuntime(): Promise<StreamingOutputsRuntimeResponse> {
  const res = await fetch("/api/streams/runtime/outputs");
  if (!res.ok) throw new Error(`Failed to fetch streaming runtime outputs: ${res.status}`);
  return (await res.json()) as StreamingOutputsRuntimeResponse;
}
