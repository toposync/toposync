import type { HostApi } from "@toposync/plugin-api";

import type {
  CameraIndexItem,
  CinematicDiagnosticsResponse,
  CinematicStatusResponse,
  CinematicWizardCreatePipelineRequest,
  CinematicWizardCreatePipelineResponse,
  Transmission
} from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

async function parseErrorResponse(response: Response): Promise<string> {
  const fallback = `HTTP ${response.status}`;
  try {
    const json = await response.json();
    if (isRecord(json) && typeof json.detail === "string" && json.detail.trim()) return json.detail.trim();
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

async function requestJson<T>(api: HostApi, input: string, init?: RequestInit): Promise<T> {
  const response = await api.fetch(input, init);
  if (!response.ok) throw new Error(await parseErrorResponse(response));
  return (await response.json()) as T;
}

export async function fetchCinematicStatus(api: HostApi, signal?: AbortSignal): Promise<CinematicStatusResponse> {
  return requestJson<CinematicStatusResponse>(api, "/api/cinematic/status", { signal });
}

export async function fetchCinematicDiagnostics(api: HostApi, signal?: AbortSignal): Promise<CinematicDiagnosticsResponse> {
  return requestJson<CinematicDiagnosticsResponse>(api, "/api/cinematic/diagnostics", { signal });
}

export async function fetchTransmissions(api: HostApi, signal?: AbortSignal): Promise<Transmission[]> {
  const payload = await requestJson<Transmission[]>(api, "/api/streams/transmissions", { signal });
  return Array.isArray(payload) ? payload : [];
}

export async function fetchCamerasIndex(api: HostApi, signal?: AbortSignal): Promise<CameraIndexItem[]> {
  const payload = await requestJson<{ cameras?: CameraIndexItem[] }>(api, "/api/cameras/index", { signal });
  return Array.isArray(payload.cameras) ? payload.cameras : [];
}

export async function createCinematicPipeline(
  api: HostApi,
  payload: CinematicWizardCreatePipelineRequest
): Promise<CinematicWizardCreatePipelineResponse> {
  return requestJson<CinematicWizardCreatePipelineResponse>(api, "/api/cinematic/wizard/create-pipeline", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload)
  });
}
