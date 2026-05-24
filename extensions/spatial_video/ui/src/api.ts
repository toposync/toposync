import type { CameraLiveView, PtzPreset, PtzStatus, StreamingPlaybackResponse } from "./types";

async function parseError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json();
    const detail = payload && typeof payload === "object" ? (payload as { detail?: unknown }).detail : null;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
  } catch {
    try {
      const text = await response.text();
      if (text.trim()) return text.trim();
    } catch {
      // ignore
    }
  }
  return fallback;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(await parseError(response, `HTTP ${response.status}`));
  return (await response.json()) as T;
}

export async function fetchLiveViews(signal?: AbortSignal): Promise<CameraLiveView[]> {
  return requestJson<CameraLiveView[]>("/api/streams/live-views", { signal });
}

export async function fetchLiveViewPlayback(
  liveViewId: string,
  variantId: string | null | undefined,
  signal?: AbortSignal,
): Promise<StreamingPlaybackResponse> {
  const params = new URLSearchParams({ context: "spatial_map" });
  if (variantId) params.set("variant_id", variantId);
  return requestJson<StreamingPlaybackResponse>(
    `/api/streams/live-views/${encodeURIComponent(liveViewId)}/playback?${params.toString()}`,
    { signal },
  );
}

export async function primeTransmissionDemand(
  transmissionId: string,
  outputId: string | null,
  qualityProfileId: string | null,
  signal?: AbortSignal,
): Promise<void> {
  const params = new URLSearchParams();
  if (outputId) params.set("output_id", outputId);
  if (qualityProfileId) params.set("quality_profile_id", qualityProfileId);
  const query = params.toString();
  const response = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/demand/prime${query ? `?${query}` : ""}`, {
    method: "POST",
    signal,
  });
  if (!response.ok) throw new Error(await parseError(response, `Demand prime failed: ${response.status}`));
}

export async function heartbeatTransmissionDemand(args: {
  transmissionId: string;
  playbackSessionId: string;
  transport: string;
  outputId: string | null;
  qualityProfileId: string | null;
  ttlSeconds: number;
  signal?: AbortSignal;
}): Promise<void> {
  const response = await fetch(`/api/streams/transmissions/${encodeURIComponent(args.transmissionId)}/demand/heartbeat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      playback_session_id: args.playbackSessionId,
      transport: args.transport,
      output_id: args.outputId || undefined,
      quality_profile_id: args.qualityProfileId || undefined,
      ttl_seconds: args.ttlSeconds,
    }),
    signal: args.signal,
  });
  if (!response.ok) throw new Error(await parseError(response, `Demand heartbeat failed: ${response.status}`));
}

export async function fetchCameraPtzStatus(
  cameraId: string,
  sourceId: string | null | undefined,
  signal?: AbortSignal,
): Promise<PtzStatus | null> {
  const query = sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : "";
  const response = await requestJson<{ camera_id: string; status: PtzStatus | null }>(
    `/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/status${query}`,
    { signal },
  );
  return response.status ?? null;
}

export async function fetchCameraPtzPresets(
  cameraId: string,
  sourceId: string | null | undefined,
  signal?: AbortSignal,
): Promise<PtzPreset[]> {
  const query = sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : "";
  const response = await requestJson<{ camera_id: string; presets?: PtzPreset[] }>(
    `/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/presets${query}`,
    { signal },
  );
  return Array.isArray(response.presets) ? response.presets : [];
}
