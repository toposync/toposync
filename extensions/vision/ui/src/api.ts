import { requestJson, resolveToposyncUrl } from "@toposync/plugin-api";
import type { PipelineReferenceFrame } from "@toposync/plugin-api";
import type { VisualConfiguration, VisualDraft, VisualModel, VisualPreview } from "./visualState";
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
