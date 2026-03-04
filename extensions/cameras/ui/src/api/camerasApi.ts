import type {
  CameraContextsResponse,
  CameraPipelineWizardRequest,
  CameraPipelineWizardResponse,
  CamerasIndex,
  OnvifDiscoverRequest,
  OnvifDiscoverResponse,
  OnvifInspectRequest,
  OnvifInspectResponse,
  OnvifStreamUriRequest,
  OnvifStreamUriResponse,
} from "../types";
import { readRecord } from "../parsing";

type ControlPointMapPair = { image: { x: number; y: number }; world: { x: number; z: number } };
type ControlPointMapQuery = { kind: "image"; x: number; y: number } | { kind: "world"; x: number; z: number };
type ControlPointMapResponse = { world?: { x: number; z: number } | null; image?: { x: number; y: number } | null };

export async function fetchCamerasIndex(): Promise<CamerasIndex> {
  const response = await fetch("/api/cameras/index");
  if (!response.ok) throw new Error(`Failed to load cameras index: ${response.status}`);
  const data = await response.json();
  const record = readRecord(data);
  return {
    cameras: Array.isArray(record.cameras) ? (record.cameras as any[]).filter(Boolean) : [],
  };
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

export async function fetchCameraSnapshot(cameraId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/snapshot`, { signal });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${response.status}`);
  }
  return response.blob();
}

export async function mapControlPoint(
  pairs: ControlPointMapPair[],
  query: ControlPointMapQuery,
  signal?: AbortSignal,
): Promise<ControlPointMapResponse> {
  const response = await fetch("/api/cameras/control_points/map", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ pairs, query }),
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
