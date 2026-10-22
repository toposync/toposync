import { requestJson, resolveToposyncUrl } from "@toposync/plugin-api";
import type { PipelineReferenceEvidence, PipelineReferenceFrame } from "@toposync/plugin-api";
import type { VisualConfiguration, VisualDraft, VisualModel, VisualPreview } from "./visualState";
import type { VisualAiOptions } from "./visualStateAi";
import type { VisualSuggestion, VisualSuggestions } from "./visualStateSuggestions";
export type Species = "person" | "cat" | "dog";
export type Identity = { id: string; name: string; species: Species; references: number; occurrences: number; representative_id: string | null };
export type GalleryData = { identities: Identity[]; revision: number; permissions: { curate: boolean; history: boolean } };
export type Observation = { editable: boolean; id: string; occurrence_id?: string; species?: Species; identity_id: string | null; eligible: boolean; confirmed: boolean; reference: boolean; cluster_id: string | null };
export type Decision = { status: string; identity_id: string | null; candidate_ids: string[]; reason: string };
export type Occurrence = { closed: boolean; identities: Identity[]; editable: boolean; id: string; species: Species; decision: Decision; observations: Observation[]; revision: number };
export type Operation = { id: string; action: string; created_at: number; undone: boolean };
const base = "/api/vision/identities";
export const read = <T,>(path = "", signal?: AbortSignal) => requestJson<T>(`${base}${path}`, { signal });
export const write = <T,>(path: string, body: unknown, method = "POST") => requestJson<T>(`${base}${path}`, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
export const photoUrl = (id: string) => resolveToposyncUrl(`${base}/observations/${encodeURIComponent(id)}/image`);
export const changed = () => window.dispatchEvent(new Event("toposync:identity-gallery-changed"));

const visualBase = "/api/vision/visual-state/drafts";
export const visualBackupLimit = 64 * 1024 * 1024;
export async function exportVisualBackup(draft: VisualDraft, signal: AbortSignal): Promise<Blob> {
  const response = await fetch(resolveToposyncUrl(`${visualBase}/${encodeURIComponent(draft.draft_id)}/backup?expected_revision=${draft.revision}`), { signal });
  if (!response.ok) throw Object.assign(new Error("visual_backup_failed"), { status: response.status });
  const result = await response.blob();
  signal.throwIfAborted();
  if (result.type !== "application/zip" || result.size === 0 || result.size > visualBackupLimit) throw new Error("visual_backup_invalid");
  return result;
}
export const restoreVisualBackup = (file: File, operationId: string, signal: AbortSignal) => requestJson<VisualDraft>(
  `/api/vision/visual-state/backups/restore?operation_id=${encodeURIComponent(operationId)}`,
  { method: "POST", headers: { "content-type": "application/zip" }, body: file, signal });
export const readVisualSuggestions = (draftId: string, offset: number, signal?: AbortSignal) => requestJson<VisualSuggestions>(
  `${visualBase}/${encodeURIComponent(draftId)}/suggestions?offset=${offset}`, { signal });
export const reviewVisualSuggestion = (draft: VisualDraft, item: VisualSuggestion, action: "accept" | "reject" | "undo",
  operationId: string, classId: string | null, signal?: AbortSignal) => requestJson<{ draft: VisualDraft; suggestion: Omit<VisualSuggestion, "compatible"> }>(
  `${visualBase}/${encodeURIComponent(draft.draft_id)}/suggestions/${encodeURIComponent(item.suggestion_id)}/review`, {
    method: "POST", headers: { "content-type": "application/json" }, signal,
    body: JSON.stringify({ action, operation_id: operationId, expected_revision: draft.revision, expected_suggestion_revision: item.revision, class_id: classId }),
  });
export const visualSuggestionUrl = (draftId: string, suggestionId: string, thumbnail = true) => resolveToposyncUrl(
  `${visualBase}/${encodeURIComponent(draftId)}/suggestions/${encodeURIComponent(suggestionId)}/image?thumbnail=${thumbnail}`);
export const readVisualAiOptions = (sensorId: string, signal?: AbortSignal) => requestJson<VisualAiOptions>(
  `/api/vision/visual-state/ai-options?sensor_id=${encodeURIComponent(sensorId)}`, { signal });
export async function captureVisualFrame(configuration: VisualConfiguration, pipeline: unknown, signal: AbortSignal): Promise<PipelineReferenceFrame> {
  const response = await fetch(resolveToposyncUrl("/api/vision/visual-state/capture-frame"), {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ configuration, pipeline }), signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw Object.assign(new Error(body?.detail?.code ?? body?.detail ?? "request_failed"), { status: response.status });
  }
  let evidence: PipelineReferenceEvidence;
  try {
    evidence = JSON.parse(response.headers.get("X-Toposync-Capture-Evidence") ?? "null");
    const sample = evidence?.sample;
    if (!evidence || typeof evidence.source_identity !== "string" || !evidence.source_identity.trim()
      || evidence.source_identity.length > 200 || typeof evidence.preparation_revision !== "string" || !/^[a-f0-9]{64}$/.test(evidence.preparation_revision)
      || typeof sample?.capture_instance !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(sample.capture_instance)
      || !Number.isSafeInteger(sample?.generation) || sample.generation < 0
      || !Number.isSafeInteger(sample?.sequence) || sample.sequence <= 0
      || !Number.isFinite(evidence.observed_at) || evidence.observed_at < 0
      || !["capture", "receipt"].includes(evidence.freshness_basis) || evidence.historical_fallback !== false
      || evidence.prepared_region !== true) throw new Error("invalid evidence");
  } catch { throw new Error("reference_capture_evidence_invalid"); }
  const blob = await response.blob();
  signal.throwIfAborted();
  const receipt = response.headers.get("X-Toposync-Capture-Receipt") ?? "";
  if (!receipt || receipt.length > 4096) throw new Error("reference_capture_receipt_invalid");
  if (blob.type !== "image/png" || blob.size === 0 || blob.size > 8 * 1024 * 1024) throw new Error("reference_capture_image_invalid");
  return { blob, evidence, receipt };
}
export const readVisualModels = (signal?: AbortSignal) => requestJson<{ choices: VisualModel[] }>("/api/vision/visual-state/models", { signal });
export const testVisualState = (configuration: VisualConfiguration, pipeline: unknown, signal: AbortSignal) => requestJson<VisualPreview>(
  "/api/vision/visual-state/preview", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ configuration, pipeline }), signal });
export const createVisualDraft = (configuration: VisualConfiguration, signal?: AbortSignal) => requestJson<VisualDraft>(visualBase,
  { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(configuration), signal });
export const updateVisualDraft = (draft: VisualDraft, configuration: VisualConfiguration, signal?: AbortSignal) => requestJson<VisualDraft>(`${visualBase}/${encodeURIComponent(draft.draft_id)}`,
  { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ configuration, expected_revision: draft.revision }), signal });
export const readVisualDraft = (draftId: string, signal?: AbortSignal) => requestJson<VisualDraft>(`${visualBase}/${encodeURIComponent(draftId)}`, { signal });
export const captureVisualReference = (draft: VisualDraft, classId: string, operationId: string, frame: PipelineReferenceFrame, signal?: AbortSignal) => {
  const query = new URLSearchParams({ class_id: classId, operation_id: operationId, expected_revision: String(draft.revision) });
  return requestJson<{ draft: VisualDraft }>(`${visualBase}/${encodeURIComponent(draft.draft_id)}/captures?${query}`,
    { method: "POST", headers: { "content-type": "image/png", "X-Toposync-Capture-Receipt": frame.receipt }, body: frame.blob, signal });
};
export const visualReferenceUrl = (draftId: string, referenceId: string, thumbnail = true) => resolveToposyncUrl(
  `${visualBase}/${encodeURIComponent(draftId)}/references/${encodeURIComponent(referenceId)}/image?thumbnail=${thumbnail}`);
