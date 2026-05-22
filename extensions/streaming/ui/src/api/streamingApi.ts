import type {
  CameraLiveView,
  CameraLiveViewGenerateResponse,
  CameraIndexResponse,
  EngineStatusResponse,
  ProcessingServer,
  ProcessingServersListResponse,
  StreamingOutputsRuntimeResponse,
  StreamingCameraIngestAuthResponse,
  StreamingHlsProbeResponse,
  StreamingRuntimeHealthResponse,
  StreamingRuntimeEncodersResponse,
  StreamingRuntimeObservabilityResponse,
  StreamingRuntimePipelinesResponse,
  StreamingQualityProfilesResponse,
  TransmissionDemandResponse,
  StreamingExtensionSettings,
  StreamingPreferredPorts,
  StreamingWizardCreatePipelineRequest,
  StreamingWizardCreatePipelineResponse,
  StreamsHealthResponse,
  Transmission,
  TransmissionUrlsResponse,
} from "../types";

type EngineAction = "start" | "stop" | "restart" | "reclaim";

type CreateTransmissionRequest = {
  name: string;
  path: string;
  enabled?: boolean;
  host_server_id?: string;
  camera_controls?: { enabled: boolean; camera_id: string; camera_source_id?: string | null } | null;
  outputs?: Array<{
    id?: string;
    protocol: "hls" | "rtsp" | "webrtc";
    enabled?: boolean;
    resolution?: { width: number; height: number };
    fps_limit?: number;
  }>;
};

type PatchStreamingSettingsRequest = {
  camera_live_views?: CameraLiveView[];
  transmissions?: Transmission[];
  engine?: {
    enabled?: boolean;
    expose_to_lan?: boolean;
    metrics_enabled?: boolean;
    encoder_policy?: {
      mode?: "auto" | "cpu";
      quarantine_enabled?: boolean;
      quarantine_after_restarts?: number;
      quarantine_window_seconds?: number;
      quarantine_duration_seconds?: number;
      max_restarts_per_minute?: number;
    };
    media_auth?: {
      mode?: "signed_proxy" | "open";
      token_ttl_seconds?: number;
      renew_margin_seconds?: number;
    };
    preferred_ports?: StreamingPreferredPorts;
    mediamtx_version?: string;
    webrtc_ice_servers?: string[];
    webrtc_additional_hosts?: string[];
  };
};

type GenerateCameraLiveViewsRequest = {
  camera_id?: string | null;
  host_server_id?: string;
  replace_existing?: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

async function parseErrorResponse(response: Response): Promise<string> {
  const fallback = `HTTP ${response.status}`;
  try {
    const json = await response.json();
    if (!isRecord(json)) return fallback;
    const detail = json.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    return fallback;
  } catch {
    try {
      const text = await response.text();
      return text.trim() || fallback;
    } catch {
      return fallback;
    }
  }
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    throw new Error(await parseErrorResponse(response));
  }
  return (await response.json()) as T;
}

export async function fetchStreamsHealth(signal?: AbortSignal): Promise<StreamsHealthResponse> {
  return requestJson<StreamsHealthResponse>("/api/streams/health", { signal });
}

export async function fetchEngineStatus(signal?: AbortSignal): Promise<EngineStatusResponse> {
  return requestJson<EngineStatusResponse>("/api/streams/engine/status", { signal });
}

export async function postEngineAction(action: EngineAction): Promise<EngineStatusResponse> {
  return requestJson<EngineStatusResponse>(`/api/streams/engine/${action}`, { method: "POST" });
}

export async function postEngineDownload(): Promise<EngineStatusResponse> {
  return requestJson<EngineStatusResponse>("/api/streams/engine/download", { method: "POST" });
}

export async function fetchStreamingSettings(signal?: AbortSignal): Promise<StreamingExtensionSettings> {
  return requestJson<StreamingExtensionSettings>("/api/streams/settings", { signal });
}

export async function patchStreamingSettings(patch: PatchStreamingSettingsRequest): Promise<StreamingExtensionSettings> {
  return requestJson<StreamingExtensionSettings>("/api/streams/settings", {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch ?? {}),
  });
}

export async function fetchTransmissions(signal?: AbortSignal): Promise<Transmission[]> {
  return requestJson<Transmission[]>("/api/streams/transmissions", { signal });
}

export async function fetchCameraLiveViews(signal?: AbortSignal): Promise<CameraLiveView[]> {
  return requestJson<CameraLiveView[]>("/api/streams/camera-live-views", { signal });
}

export async function generateCameraLiveViews(
  payload: GenerateCameraLiveViewsRequest = {},
): Promise<CameraLiveViewGenerateResponse> {
  return requestJson<CameraLiveViewGenerateResponse>("/api/streams/camera-live-views/generate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateCameraLiveView(liveViewId: string, payload: CameraLiveView): Promise<CameraLiveView> {
  return requestJson<CameraLiveView>(`/api/streams/camera-live-views/${encodeURIComponent(liveViewId)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteCameraLiveView(liveViewId: string): Promise<void> {
  const response = await fetch(`/api/streams/camera-live-views/${encodeURIComponent(liveViewId)}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await parseErrorResponse(response));
  }
}

export async function fetchStreamingQualityProfiles(signal?: AbortSignal): Promise<StreamingQualityProfilesResponse> {
  return requestJson<StreamingQualityProfilesResponse>("/api/streams/quality-profiles", { signal });
}

export async function createTransmission(payload: CreateTransmissionRequest): Promise<Transmission> {
  return requestJson<Transmission>("/api/streams/transmissions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateTransmission(transmissionId: string, payload: Transmission): Promise<Transmission> {
  return requestJson<Transmission>(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteTransmission(transmissionId: string): Promise<void> {
  const response = await fetch(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await parseErrorResponse(response));
  }
}

export async function fetchTransmissionUrls(transmissionId: string): Promise<TransmissionUrlsResponse> {
  return requestJson<TransmissionUrlsResponse>(`/api/streams/transmissions/${encodeURIComponent(transmissionId)}/urls`);
}

export async function applyTransmissionQualityProfiles(transmissionId: string): Promise<Transmission> {
  const payload = await requestJson<{ transmission: Transmission }>(
    `/api/streams/transmissions/${encodeURIComponent(transmissionId)}/quality-profiles/apply`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: "replace_hls_profiles" }),
    },
  );
  return payload.transmission;
}

export async function applyTransmissionWebRtcCompanion(transmissionId: string): Promise<Transmission> {
  const payload = await requestJson<{ transmission: Transmission }>(
    `/api/streams/transmissions/${encodeURIComponent(transmissionId)}/webrtc/companion/apply`,
    { method: "POST" },
  );
  return payload.transmission;
}

export async function fetchStreamingOutputsRuntime(signal?: AbortSignal): Promise<StreamingOutputsRuntimeResponse> {
  return requestJson<StreamingOutputsRuntimeResponse>("/api/streams/runtime/outputs", { signal });
}

export async function fetchStreamingRuntimeHealth(signal?: AbortSignal): Promise<StreamingRuntimeHealthResponse> {
  return requestJson<StreamingRuntimeHealthResponse>("/api/streams/runtime/health", { signal });
}

export async function fetchStreamingRuntimePipelines(signal?: AbortSignal): Promise<StreamingRuntimePipelinesResponse> {
  return requestJson<StreamingRuntimePipelinesResponse>("/api/streams/runtime/pipelines", { signal });
}

export async function fetchStreamingRuntimeObservability(signal?: AbortSignal): Promise<StreamingRuntimeObservabilityResponse> {
  return requestJson<StreamingRuntimeObservabilityResponse>("/api/streams/runtime/observability", { signal });
}

export async function fetchStreamingRuntimeEncoders(signal?: AbortSignal): Promise<StreamingRuntimeEncodersResponse> {
  return requestJson<StreamingRuntimeEncodersResponse>("/api/streams/runtime/encoders", { signal });
}

export async function clearStreamingEncoderQuarantine(encoder?: string | null): Promise<StreamingRuntimeEncodersResponse> {
  const payload = await requestJson<{ encoders: StreamingRuntimeEncodersResponse }>("/api/streams/runtime/encoders/quarantine/clear", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ encoder: encoder ?? null }),
  });
  return payload.encoders;
}

export async function fetchCameraIngestAuth(signal?: AbortSignal): Promise<StreamingCameraIngestAuthResponse> {
  return requestJson<StreamingCameraIngestAuthResponse>("/api/streams/runtime/camera-ingest/auth", { signal });
}

export async function revealCameraIngestAuth(): Promise<StreamingCameraIngestAuthResponse> {
  return requestJson<StreamingCameraIngestAuthResponse>("/api/streams/runtime/camera-ingest/auth/reveal", { method: "POST" });
}

export async function rotateCameraIngestAuth(): Promise<StreamingCameraIngestAuthResponse> {
  return requestJson<StreamingCameraIngestAuthResponse>("/api/streams/runtime/camera-ingest/auth/rotate", { method: "POST" });
}

export async function fetchStreamingRuntimeDiagnostics(signal?: AbortSignal): Promise<unknown> {
  return requestJson<unknown>("/api/streams/runtime/diagnostics", { signal });
}

export async function fetchStreamingDiagnosticSnapshot(signal?: AbortSignal): Promise<unknown> {
  return requestJson<unknown>("/api/streams/runtime/diagnostic-snapshot", { signal });
}

export async function fetchStreamingHlsProbe(
  transmissionId: string,
  outputId?: string,
  signal?: AbortSignal,
): Promise<StreamingHlsProbeResponse> {
  const query = outputId ? `?output_id=${encodeURIComponent(outputId)}` : "";
  return requestJson<StreamingHlsProbeResponse>(
    `/api/streams/transmissions/${encodeURIComponent(transmissionId)}/hls/probe${query}`,
    { signal },
  );
}

export async function fetchTransmissionDemand(transmissionId: string, signal?: AbortSignal): Promise<TransmissionDemandResponse> {
  return requestJson<TransmissionDemandResponse>(
    `/api/streams/transmissions/${encodeURIComponent(transmissionId)}/demand`,
    { signal },
  );
}

export async function fetchCamerasIndex(signal?: AbortSignal): Promise<CameraIndexResponse> {
  return requestJson<CameraIndexResponse>("/api/cameras/index", { signal });
}

export async function fetchProcessingServers(signal?: AbortSignal): Promise<ProcessingServer[]> {
  const payload = await requestJson<ProcessingServersListResponse>("/api/processing-servers", { signal });
  return Array.isArray(payload.servers) ? payload.servers : [];
}

export async function createPipelineFromTransmissionWizard(
  payload: StreamingWizardCreatePipelineRequest,
): Promise<StreamingWizardCreatePipelineResponse> {
  return requestJson<StreamingWizardCreatePipelineResponse>("/api/streams/wizard/create-pipeline", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}
