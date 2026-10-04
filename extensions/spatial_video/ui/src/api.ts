import { requestJson, requestVoid } from "@toposync/plugin-api";

import type { CameraLiveView, PtzPreset, PtzStatus, StreamingPlaybackResponse } from "./types";

export async function fetchLiveViews(signal?: AbortSignal): Promise<CameraLiveView[]> {
  const [views, index] = await Promise.all([
    requestJson<CameraLiveView[]>("/api/streams/live-views", { signal }),
    requestJson<{ cameras: Array<{
      id: string;
      control?: { has_ptz?: boolean };
      sources?: Array<{ id: string; has_ptz?: boolean }>;
    }> }>("/api/cameras/index", { signal }).catch((error) => {
      if (signal?.aborted) throw error;
      return null;
    }),
  ]);
  // Missing metadata remains unknown; only an explicit capability rules out polling.
  return views.map((view) => {
    const camera = index?.cameras.find((item) => item.id === view.camera_id);
    return {
      ...view,
      has_ptz: camera?.control?.has_ptz,
      variants: view.variants?.map((variant) => ({
        ...variant,
        has_ptz: camera?.control?.has_ptz === false
          ? false
          : camera?.sources?.find((source) => source.id === variant.camera_source_id)?.has_ptz,
      })),
    };
  });
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
  await requestVoid(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/demand/prime${query ? `?${query}` : ""}`, {
    method: "POST",
    signal,
  });
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
  await requestVoid(`/api/streams/transmissions/${encodeURIComponent(args.transmissionId)}/demand/heartbeat`, {
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
