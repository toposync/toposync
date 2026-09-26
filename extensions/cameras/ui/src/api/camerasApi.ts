import { requestForm, requestJson, requestVoid, resolveToposyncUrl } from "@toposync/plugin-api";

import type {
  CameraCalibratedView,
  CameraContextsResponse,
  CameraPipelinePresetRequest,
  CameraPipelinePresetResponse,
  CameraPipelinesResponse,
  CameraPtzPreset,
  CameraProjectionSolveResult,
  CameraRayGroundCalibratedView,
  CameraSourceHealthResponse,
  CameraVisualCalibrationResult,
  CamerasIndex,
  OnvifDiscoverRequest,
  OnvifDiscoverResponse,
  OnvifInspectRequest,
  OnvifInspectResponse,
  OnvifStreamUriRequest,
  OnvifStreamUriResponse,
  PanTiltZoomState,
  ProcessingServer,
  RtspProbeResponse,
  StreamPublication,
} from "../types";
import { readRecord } from "../parsing";

async function readErrorDetail(response: Response, fallback: string): Promise<string> {
  const text = await response.text().catch(() => "");
  if (!text) return fallback;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    const detail = parsed?.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (Array.isArray(detail) && detail.length) return detail.map((item) => String(item)).join(" ");
  } catch {
    // Non-JSON error bodies are already useful enough to show directly.
  }
  return text;
}

const CAMERA_SNAPSHOT_FRESHNESS_UNVERIFIABLE = "camera_snapshot_freshness_unverifiable";

class CameraSnapshotFreshnessUnverifiableError extends Error {
  readonly code = CAMERA_SNAPSHOT_FRESHNESS_UNVERIFIABLE;

  constructor(message: string) {
    super(message);
    this.name = "CameraSnapshotFreshnessUnverifiableError";
  }
}

export function isCameraSnapshotFreshnessUnverifiableError(
  error: unknown,
): error is CameraSnapshotFreshnessUnverifiableError {
  return (
    error instanceof CameraSnapshotFreshnessUnverifiableError ||
    (typeof error === "object" &&
      error !== null &&
      "code" in error &&
      error.code === CAMERA_SNAPSHOT_FRESHNESS_UNVERIFIABLE)
  );
}

async function requestBlob(input: string, init: RequestInit | undefined, fallback: string): Promise<Blob> {
  const response = await fetch(resolveToposyncUrl(input), init);
  if (!response.ok) {
    const detail = await readErrorDetail(response, fallback);
    if (response.headers.get("X-Toposync-Snapshot-Freshness")?.toLowerCase() === "unverifiable") {
      throw new CameraSnapshotFreshnessUnverifiableError(detail);
    }
    throw new Error(detail);
  }
  return response.blob();
}

function splitSourceAndSignal(
  sourceIdOrSignal?: string | AbortSignal,
  signal?: AbortSignal,
): { sourceId: string; signal?: AbortSignal } {
  if (sourceIdOrSignal instanceof AbortSignal) return { sourceId: "", signal: sourceIdOrSignal };
  return { sourceId: String(sourceIdOrSignal || "").trim(), signal };
}

export async function fetchCamerasIndex(signal?: AbortSignal): Promise<CamerasIndex> {
  const data = await requestJson<unknown>("/api/cameras/index", { signal });
  const record = readRecord(data);
  return {
    cameras: Array.isArray(record.cameras) ? (record.cameras as any[]).filter(Boolean) : [],
  };
}

export async function fetchProcessingServers(signal?: AbortSignal): Promise<ProcessingServer[]> {
  const data = await requestJson<unknown>("/api/processing-servers", { signal });
  const record = readRecord(data);
  return Array.isArray(record.servers) ? (record.servers as ProcessingServer[]).filter(Boolean) : [];
}

export async function fetchProcessingServerStatus(serverId: string, signal?: AbortSignal): Promise<unknown> {
  return requestJson<unknown>(`/api/processing-servers/${encodeURIComponent(serverId)}/status`, { signal });
}

export async function installProcessingServerVisionModel(
  serverId: string,
  modelId: string,
  body: { mode?: string; acknowledge_upstream_terms?: boolean } = {},
  signal?: AbortSignal,
): Promise<unknown> {
  return requestJson<unknown>(
    `/api/processing-servers/${encodeURIComponent(serverId)}/vision/models/${encodeURIComponent(modelId)}/install`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export async function fetchCameraSourceHealth(signal?: AbortSignal): Promise<CameraSourceHealthResponse> {
  return requestJson<CameraSourceHealthResponse>("/api/cameras/runtime/source-health", { signal });
}

export async function fetchStreamPublications(cameraId?: string, signal?: AbortSignal): Promise<StreamPublication[]> {
  const params = new URLSearchParams();
  const normalizedCameraId = String(cameraId || "").trim();
  if (normalizedCameraId) params.set("camera_id", normalizedCameraId);
  const suffix = params.toString();
  const data = await requestJson<unknown>(`/api/streams/publications${suffix ? `?${suffix}` : ""}`, { signal });
  return Array.isArray(data) ? (data as StreamPublication[]).filter(Boolean) : [];
}

export async function updateCameraSourcePublication(
  cameraId: string,
  sourceId: string,
  patch: Partial<Pick<StreamPublication, "enabled" | "label" | "role" | "host_server_id" | "quality_policy" | "transport_policy">>,
  signal?: AbortSignal,
): Promise<StreamPublication> {
  return requestJson<StreamPublication>(
    `/api/streams/publications/camera-sources/${encodeURIComponent(cameraId)}/${encodeURIComponent(sourceId)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
      signal,
    },
  );
}

export async function reconcileStreamPublications(signal?: AbortSignal): Promise<void> {
  await requestVoid("/api/streams/reconcile", { method: "POST", signal });
}

export async function fetchRtspSnapshot(
  options: { url: string; username?: string; password?: string },
  signal?: AbortSignal,
): Promise<Blob> {
  return requestBlob("/api/cameras/rtsp/snapshot", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url: options.url,
      username: options.username ?? "",
      password: options.password ?? "",
    }),
    signal,
  }, "Snapshot failed");
}

export async function probeRtsp(
  options: { url: string; username?: string; password?: string; timeout_ms?: number },
  signal?: AbortSignal,
): Promise<RtspProbeResponse> {
  return requestJson<RtspProbeResponse>("/api/cameras/rtsp/probe", {
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
}

export async function probeCameraRtsp(
  cameraId: string,
  options: { source_id?: string; timeout_ms?: number } = {},
  signal?: AbortSignal,
): Promise<RtspProbeResponse> {
  return requestJson<RtspProbeResponse>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/rtsp/probe`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      source_id: options.source_id ?? "",
      timeout_ms: options.timeout_ms ?? 5000,
    }),
    signal,
  });
}

export async function fetchCameraSnapshot(
  cameraId: string,
  sourceIdOrSignal: string | AbortSignal = "",
  signal?: AbortSignal,
  fresh = false,
  freshness: "physical" | "decoder" = "physical",
): Promise<Blob> {
  const resolved = splitSourceAndSignal(sourceIdOrSignal, signal);
  const search = new URLSearchParams();
  if (resolved.sourceId) search.set("source_id", resolved.sourceId);
  if (fresh) search.set("fresh", "true");
  if (fresh && freshness !== "physical") search.set("freshness", freshness);
  const query = search.size ? `?${search.toString()}` : "";
  return requestBlob(
    `/api/cameras/cameras/${encodeURIComponent(cameraId)}/snapshot${query}`,
    { signal: resolved.signal },
    "Snapshot failed",
  );
}

export async function solveCameraProjection(
  calibratedView: CameraRayGroundCalibratedView,
  signal?: AbortSignal,
): Promise<CameraProjectionSolveResult> {
  return requestJson<CameraProjectionSolveResult>("/api/cameras/projection/solve", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ calibrated_view: calibratedView }),
    signal,
  });
}

export async function captureCameraPtzViewAnchor(
  cameraId: string,
  body: { source_id: string; name: string; idempotency_key: string },
  signal?: AbortSignal,
): Promise<CameraPtzPreset> {
  return requestJson<CameraPtzPreset>(
    `/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/presets`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export async function fetchCameraPtzStatus(
  cameraId: string,
  sourceIdOrSignal: string | AbortSignal = "",
  signal?: AbortSignal,
): Promise<{ camera_id: string; status: PanTiltZoomState | null }> {
  const resolved = splitSourceAndSignal(sourceIdOrSignal, signal);
  const query = resolved.sourceId ? `?source_id=${encodeURIComponent(resolved.sourceId)}` : "";
  return requestJson<{ camera_id: string; status: PanTiltZoomState | null }>(
    `/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/status${query}`,
    { signal: resolved.signal },
  );
}

export async function gotoCameraPtzPreset(
  cameraId: string,
  presetToken: string,
  sourceId = "",
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/goto-preset`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ preset_token: presetToken, source_id: sourceId }),
    signal,
  });
}

export async function moveCameraPtzAbsolute(
  cameraId: string,
  body: { source_id?: string; pan?: number | null; tilt?: number | null; zoom?: number | null },
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/absolute-move`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function moveCameraPtz(
  cameraId: string,
  body: { source_id?: string; pan: number; tilt: number; zoom: number; timeout_s?: number | null },
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/move`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function stopCameraPtz(
  cameraId: string,
  body: { source_id?: string; pan_tilt?: boolean; zoom?: boolean },
  signal?: AbortSignal,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/ptz/stop`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function propagateCameraProjection(
  sourceView: CameraCalibratedView,
  sourceId: string,
  sourceImage: Blob,
  targetImage: Blob,
  signal?: AbortSignal,
): Promise<CameraVisualCalibrationResult> {
  const form = new FormData();
  form.append("source_view_json", JSON.stringify(sourceView));
  form.append("source_id", sourceId);
  form.append("source_image", sourceImage, "reference.jpg");
  form.append("target_image", targetImage, "current.jpg");
  return requestForm<CameraVisualCalibrationResult>("/api/cameras/projection/propagate", form, { signal });
}

export async function fetchCameraContexts(cameraId: string, signal?: AbortSignal): Promise<CameraContextsResponse> {
  return requestJson<CameraContextsResponse>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/contexts`, {
    signal,
  });
}

export async function fetchCameraPipelines(cameraId: string, signal?: AbortSignal): Promise<CameraPipelinesResponse> {
  return requestJson<CameraPipelinesResponse>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/pipelines`, {
    signal,
  });
}

export async function createCameraPipelinePreset(
  cameraId: string,
  body: CameraPipelinePresetRequest,
  signal?: AbortSignal,
): Promise<CameraPipelinePresetResponse> {
  return requestJson<CameraPipelinePresetResponse>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/pipelines/presets`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function inspectOnvif(
  body: OnvifInspectRequest,
  signal?: AbortSignal,
): Promise<OnvifInspectResponse> {
  return requestJson<OnvifInspectResponse>("/api/cameras/onvif/inspect", {
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
}

export async function fetchOnvifStreamUri(
  body: OnvifStreamUriRequest,
  signal?: AbortSignal,
): Promise<OnvifStreamUriResponse> {
  return requestJson<OnvifStreamUriResponse>("/api/cameras/onvif/stream-uri", {
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
}

export async function discoverOnvifDevices(
  body: OnvifDiscoverRequest,
  signal?: AbortSignal,
): Promise<OnvifDiscoverResponse> {
  return requestJson<OnvifDiscoverResponse>("/api/cameras/onvif/discover", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      timeout_ms: body.timeout_ms,
      force: body.force ?? false,
      exclude_known: body.exclude_known ?? true,
    }),
    signal,
  });
}

export async function fetchCameraPanoramaContext(cameraId: string, elementId: string, signal?: AbortSignal): Promise<import("../types").CameraPanoramaContext> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama?element_id=${encodeURIComponent(elementId)}`, { signal });
}

export async function fetchCameraPanoramaJob(cameraId: string, jobId: string, signal?: AbortSignal): Promise<import("../types").CameraPanoramaJob> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/${encodeURIComponent(jobId)}`, { signal });
}

export async function createCameraPanorama(cameraId: string, body: { element_id: string; source_id: string; reuse_draft?: boolean; review_job_id?: string; review_revision?: number } & ({ profile: import("../types").CameraPanoramaProfile; scan: import("../types").CameraPanoramaScan } | { source_artifact_id: string; source_artifact_revision: number })): Promise<import("../types").CameraPanoramaJob> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
}

export async function updateCameraPanorama(cameraId: string, jobId: string, action: "capture" | "cancel" | "points" | "check" | "check-result" | "activate" | "return-framing", body: { revision: number; points?: import("../types").CameraPanoramaPoint[]; point_id?: string; check_id?: string; result?: "correct" | "offset" | "unverifiable"; observed_image?: { x: number; y: number } }): Promise<import("../types").CameraPanoramaJob> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/${encodeURIComponent(jobId)}/${action}`, {
    method: action === "points" ? "PUT" : "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
}

export async function aimCameraPanorama(cameraId: string, elementId: string, world: { x: number; z: number }): Promise<import("../types").CameraPanoramaJob> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/aim`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ element_id: elementId, world }),
  });
}

export async function restoreCameraPanorama(cameraId: string, elementId: string, expectedJobId: string, expectedRevision: number): Promise<import("../types").CameraPanoramaJob> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/restore`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ element_id: elementId, expected_job_id: expectedJobId, expected_revision: expectedRevision }),
  });
}

export async function deleteCameraPanorama(cameraId: string, jobId: string, revision: number): Promise<{ deleted: boolean; id: string }> {
  return requestPanorama(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/${encodeURIComponent(jobId)}?revision=${revision}`, { method: "DELETE" });
}

export class CameraPanoramaRequestError extends Error {
  constructor(readonly code: string, message: string, readonly status: number) {
    super(message);
    this.name = "CameraPanoramaRequestError";
  }
}

function requestSourcePanorama<T>(path: string, init?: RequestInit): Promise<T> {
  return requestPanorama(path, init, 20_000);
}

async function requestPanorama<T>(path: string, init?: RequestInit, timeoutMilliseconds = 0): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  init?.signal?.addEventListener("abort", abort, { once: true });
  if (init?.signal?.aborted) controller.abort();
  const timeout = timeoutMilliseconds ? setTimeout(abort, timeoutMilliseconds) : undefined;
  try {
    const response = await fetch(resolveToposyncUrl(path), { ...init, signal: controller.signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => null) as { detail?: { code?: string; message?: string } | string } | null;
      const detail = payload?.detail;
      throw new CameraPanoramaRequestError(
        typeof detail === "object" && detail?.code ? detail.code : "panorama_request_failed",
        typeof detail === "object" && detail?.message ? detail.message : typeof detail === "string" ? detail : "",
        response.status,
      );
    }
    return await response.json() as T;
  } finally {
    clearTimeout(timeout);
    init?.signal?.removeEventListener("abort", abort);
  }
}

function sourcePanoramaPath(cameraId: string, sourceId: string): string {
  return `/api/cameras/cameras/${encodeURIComponent(cameraId)}/sources/${encodeURIComponent(sourceId)}/panorama`;
}

export type PanoramaNativeReference = {
  id: string;
  status: "queued" | "preparing" | "stopping" | "ready" | "unverified";
  physical_state: string;
  cleanup_confirmed: boolean;
};

function nativeReferencePath(cameraId: string): string {
  return `/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/native-reference`;
}

export function fetchPanoramaNativeReferences(cameraId: string, sourceId: string, artifactId: string, revision: number, signal?: AbortSignal): Promise<{ references: PanoramaNativeReference[] }> {
  const query = new URLSearchParams({ source_id: sourceId, artifact_id: artifactId, revision: String(revision) });
  return requestSourcePanorama(`${nativeReferencePath(cameraId)}?${query}`, { signal });
}

export function operatePanoramaNativeReference(cameraId: string, sourceId: string, artifactId: string, revision: number, preparationId: string, action: "prepare" | "stop" | "remove"): Promise<{ id: string; status: string }> {
  return requestPanorama(`${nativeReferencePath(cameraId)}${action === "stop" ? "/stop" : ""}`, {
    method: action === "remove" ? "DELETE" : "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_id: sourceId, artifact_id: artifactId, revision, preparation_id: preparationId }),
  }, action === "prepare" ? 85_000 : 20_000);
}

export function fetchCameraSourcePanorama(cameraId: string, sourceId: string, signal?: AbortSignal): Promise<import("../types").CameraSourcePanorama> {
  return requestSourcePanorama(sourcePanoramaPath(cameraId, sourceId), { signal });
}

export function finalizeCameraSourcePanoramaReplacement(cameraId: string, sourceId: string, signal?: AbortSignal): Promise<import("../types").CameraSourcePanorama> {
  return requestSourcePanorama(`${sourcePanoramaPath(cameraId, sourceId)}/finalize`, { method: "POST", signal });
}

export async function createCameraSourcePanorama(cameraId: string, sourceId: string, idempotencyKey: string): Promise<import("../types").CameraSourcePanoramaJob> {
  const result = await requestSourcePanorama<{ job: import("../types").CameraSourcePanoramaJob }>(`${sourcePanoramaPath(cameraId, sourceId)}/jobs`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
  return result.job;
}

export async function fetchCameraSourcePanoramaJob(jobId: string, signal?: AbortSignal): Promise<import("../types").CameraSourcePanoramaJob> {
  const result = await requestSourcePanorama<{ job: import("../types").CameraSourcePanoramaJob }>(`/api/cameras/panorama-jobs/${encodeURIComponent(jobId)}`, { signal });
  return result.job;
}

export async function operateCameraSourcePanorama(jobId: string, action: "stop" | "resume" | "return" | "reconstruct" | "cleanup"): Promise<import("../types").CameraSourcePanoramaJob> {
  const result = await requestSourcePanorama<{ job: import("../types").CameraSourcePanoramaJob }>(`/api/cameras/panorama-jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
  return result.job;
}

export async function saveCameraSourcePanoramaCrop(cameraId: string, sourceId: string, artifactId: string, expectedRevision: number, crop: import("../types").CameraSourcePanoramaCrop): Promise<import("../types").CameraSourcePanoramaArtifact> {
  const result = await requestSourcePanorama<{ artifact: import("../types").CameraSourcePanoramaArtifact }>(`${sourcePanoramaPath(cameraId, sourceId)}/crop`, {
    method: "PATCH", headers: { "content-type": "application/json" },
    body: JSON.stringify({ artifact_id: artifactId, expected_revision: expectedRevision, crop }),
  });
  return result.artifact;
}
