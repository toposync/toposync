import type { CamerasIndex } from "../types";
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
    processing_servers: Array.isArray(record.processing_servers) ? (record.processing_servers as any[]).filter(Boolean) : [],
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
