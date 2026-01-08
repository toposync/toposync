import type { CamerasIndex } from "../types";
import { readRecord } from "../parsing";

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

export async function fetchRtspSnapshot(options: { url: string; username?: string; password?: string }): Promise<Blob> {
  const response = await fetch("/api/cameras/rtsp/snapshot", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url: options.url,
      username: options.username ?? "",
      password: options.password ?? "",
    }),
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${response.status}`);
  }
  return response.blob();
}

export async function fetchCameraSnapshot(cameraId: string): Promise<Blob> {
  const response = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/snapshot`);
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${response.status}`);
  }
  return response.blob();
}

