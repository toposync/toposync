import type {
  CameraControlPointSet,
  CameraContextsResponse,
  CameraPipelineWizardRequest,
  CameraPipelineWizardResponse,
  CameraPtzPreset,
  CameraSourceHealthResponse,
  CamerasIndex,
  OnvifDiscoverRequest,
  OnvifDiscoverResponse,
  OnvifInspectRequest,
  OnvifInspectResponse,
  OnvifStreamUriRequest,
  OnvifStreamUriResponse,
  PanTiltZoomState,
  RtspProbeResponse,
} from "../types";
import { readRecord } from "../parsing";

type ControlPointMapQuery = { kind: "image"; x: number; y: number } | { kind: "world"; x: number; z: number };
type ControlPointMapResponse = {
  world?: { x: number; z: number } | null;
  image?: { x: number; y: number } | null;
  quality?: Record<string, unknown> | null;
};

export async function fetchCamerasIndex(): Promise<CamerasIndex> {
  const response = await fetch("/api/cameras/index");
  if (!response.ok) throw new Error(`Failed to load cameras index: ${response.status}`);
  const data = await response.json();
  const record = readRecord(data);
  return {
    cameras: Array.isArray(record.cameras) ? (record.cameras as any[]).filter(Boolean) : [],
  };
}

export async function fetchCameraSourceHealth(signal?: AbortSignal): Promise<CameraSourceHealthResponse> {
  const response = await fetch("/api/cameras/runtime/source-health", { signal });
  if (!response.ok) throw new Error(`Failed to load camera source health: ${response.status}`);
  return response.json();
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

export async function probeRtsp(
  options: { url: string; username?: string; password?: string; timeout_ms?: number },
  signal?: AbortSignal,
): Promise<RtspProbeResponse> {
  const response = await fetch("/api/cameras/rtsp/probe", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url: options.url,
      username: options.username ?? "",
      password: options.password ?? "",
      timeout_ms: options.timeout_ms ?? 5000,
    }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `RTSP probe failed: ${response.status}`);
  }
  return response.json();
}

export async function probeCameraRtsp(
  cameraId: string,
  options: { timeout_ms?: number } = {},
  signal?: AbortSignal,
): Promise<RtspProbeResponse> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/rtsp/probe`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ timeout_ms: options.timeout_ms ?? 5000 }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `RTSP probe failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchCameraSnapshot(cameraId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/snapshot`, { signal });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${response.status}`);
  }
  return response.blob();
}

export async function fetchCameraPtzPresets(
  cameraId: string,
  signal?: AbortSignal,
): Promise<{ camera_id: string; presets: CameraPtzPreset[] }> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/presets`, { signal });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to load PTZ presets: ${response.status}`);
  }
  return response.json();
}

export async function fetchCameraPtzStatus(
  cameraId: string,
  signal?: AbortSignal,
): Promise<{ camera_id: string; status: PanTiltZoomState | null }> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/status`, { signal });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to load PTZ status: ${response.status}`);
  }
  return response.json();
}

export async function gotoCameraPtzPreset(
  cameraId: string,
  presetToken: string,
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/goto-preset`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ preset_token: presetToken }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to move camera to preset: ${response.status}`);
  }
  return response.json();
}

export async function moveCameraPtz(
  cameraId: string,
  body: { pan: number; tilt: number; zoom: number; timeout_s?: number | null },
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/move`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to move PTZ camera: ${response.status}`);
  }
  return response.json();
}

export async function stopCameraPtz(
  cameraId: string,
  body: { pan_tilt?: boolean; zoom?: boolean },
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/stop`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to stop PTZ camera: ${response.status}`);
  }
  return response.json();
}

export async function mapControlPoint(
  controlPointSet: CameraControlPointSet,
  query: ControlPointMapQuery,
  signal?: AbortSignal,
): Promise<ControlPointMapResponse> {
  const response = await fetch("/api/cameras/control_points/map", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ control_point_set: controlPointSet, query }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Mapping failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchCameraContexts(cameraId: string, signal?: AbortSignal): Promise<CameraContextsResponse> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/contexts`, { signal });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to load camera contexts: ${response.status}`);
  }
  return response.json();
}

export async function createCameraPipelineFromWizard(
  cameraId: string,
  body: CameraPipelineWizardRequest,
  signal?: AbortSignal,
): Promise<CameraPipelineWizardResponse> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/pipeline-wizard`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Failed to create pipeline: ${response.status}`);
  }
  return response.json();
}

export async function inspectOnvif(
  body: OnvifInspectRequest,
  signal?: AbortSignal,
): Promise<OnvifInspectResponse> {
  const response = await fetch("/api/cameras/onvif/inspect", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      xaddr: body.xaddr,
      username: body.username ?? "",
      password: body.password ?? "",
      timeout_ms: body.timeout_ms,
      auth: body.auth ?? "auto",
    }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `ONVIF inspect failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchOnvifStreamUri(
  body: OnvifStreamUriRequest,
  signal?: AbortSignal,
): Promise<OnvifStreamUriResponse> {
  const response = await fetch("/api/cameras/onvif/stream-uri", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      xaddr: body.xaddr,
      media_xaddr: body.media_xaddr ?? "",
      profile_token: body.profile_token,
      username: body.username ?? "",
      password: body.password ?? "",
      timeout_ms: body.timeout_ms,
      auth: body.auth ?? "auto",
    }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `ONVIF stream URI failed: ${response.status}`);
  }
  return response.json();
}

export async function discoverOnvifDevices(
  body: OnvifDiscoverRequest,
  signal?: AbortSignal,
): Promise<OnvifDiscoverResponse> {
  const response = await fetch("/api/cameras/onvif/discover", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      timeout_ms: body.timeout_ms,
      force: body.force ?? false,
      exclude_known: body.exclude_known ?? true,
    }),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `ONVIF discovery failed: ${response.status}`);
  }
  return response.json();
}
