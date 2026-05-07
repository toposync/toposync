import type {
  CameraIndexResponse,
  EngineStatusResponse,
  ProcessingServer,
  ProcessingServersListResponse,
  StreamingOutputsRuntimeResponse,
  StreamingHlsProbeResponse,
  StreamingRuntimeHealthResponse,
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
  camera_controls?: { enabled: boolean; camera_id: string } | null;
  outputs?: Array<{
    id?: string;
    protocol: "hls" | "rtsp" | "webrtc";
    enabled?: boolean;
    resolution?: { width: number; height: number };
    fps_limit?: number;
  }>;
};

type PatchStreamingSettingsRequest = {
  transmissions?: Transmission[];
  engine?: {
    enabled?: boolean;
    expose_to_lan?: boolean;
    preferred_ports?: StreamingPreferredPorts;
    mediamtx_version?: string;
    webrtc_ice_servers?: string[];
  };
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

export async function fetchStreamingOutputsRuntime(signal?: AbortSignal): Promise<StreamingOutputsRuntimeResponse> {
  return requestJson<StreamingOutputsRuntimeResponse>("/api/streams/runtime/outputs", { signal });
}

export async function fetchStreamingRuntimeHealth(signal?: AbortSignal): Promise<StreamingRuntimeHealthResponse> {
  return requestJson<StreamingRuntimeHealthResponse>("/api/streams/runtime/health", { signal });
}

export async function fetchStreamingRuntimeDiagnostics(signal?: AbortSignal): Promise<unknown> {
  return requestJson<unknown>("/api/streams/runtime/diagnostics", { signal });
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
