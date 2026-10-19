import { resolveToposyncUrl, type Notification } from "@toposync/plugin-api";
import { requestJson } from "./api";

export type ActivityEvidence = {
  participant_id: string; camera_id: string; source_id: string; camera_name?: string; source_name?: string;
  observed_at: number; clock_domain: string; gap: boolean;
  position: [number, number] | null; coordinate_reference: string | null;
  mapping_revision: string | null; position_error_metres: number | null;
};
export type ActivityParticipant = {
  id: string; category: string;
  identification: { status: "recognized" | "unknown" | "restricted" | "unavailable"; name?: string; provenance?: string };
};
export type ActivityDetail = {
  occurrence: {
    id: string; title: string; revision: number; status: "OPEN" | "UPDATE" | "CLOSE";
    started_at: number; ended_at: number | null; last_observed_at: number;
    duration_seconds: number | null; duration_status: string; clock_domain: string;
    close_reason: string | null; expires_at: number; enrichment_allowed: boolean;
    participants: Record<string, ActivityParticipant>; evidence: Record<string, ActivityEvidence>;
    coverage: { gaps: boolean; continuous_video: boolean };
    enrichments: Array<{ kind: string; text: string | null; status: string; participant_id: string; observed_at: number; recorded_at: number }>;
  };
  permissions: { manage: boolean };
  maintenance_error: string | null;
  revisions: Array<{ revision: number; kind: string; recorded_at: number }>;
  deliveries: Array<{ id: string; status: string; simulated: boolean; reason: string | null }>;
};

export function activityIdentifier(notification: Notification): string | null {
  const payload = notification.payload;
  const value = payload && typeof payload === "object" && "occurrence_id" in payload ? payload.occurrence_id : null;
  return notification.type === "occurrences.activity" && typeof value === "string" && /^[a-f0-9]{64}$/.test(value) ? value : null;
}
export function activityUrl(identifier: string, suffix = ""): string {
  return resolveToposyncUrl(`/api/activity/${encodeURIComponent(identifier)}${suffix}`);
}
export function getActivity(identifier: string, signal: AbortSignal): Promise<ActivityDetail> {
  return requestJson(activityUrl(identifier), { signal, cache: "no-store" }, "Activity unavailable");
}
export function changeActivity(identifier: string, revision: number, action: "delete" | "revoke", signal: AbortSignal): Promise<unknown> {
  return requestJson(activityUrl(identifier, action === "revoke" ? "/revoke-enrichment" : ""), {
    signal, method: action === "delete" ? "DELETE" : "POST", cache: "no-store",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ expected_revision: revision }),
  }, "Activity change unavailable");
}
export function activityError(error: unknown): "expired" | "unavailable" | "changed" | "failed" {
  const status = error && typeof error === "object" && "status" in error ? error.status : null;
  return status === 410 ? "expired" : status === 401 || status === 403 || status === 404 ? "unavailable" : status === 409 ? "changed" : "failed";
}
export function participantEvidence(document: ActivityDetail["occurrence"], participant: string): Array<[string, ActivityEvidence]> {
  return Object.entries(document.evidence).filter(([, item]) => item.participant_id === participant)
    .sort(([leftId, left], [rightId, right]) => left.clock_domain.localeCompare(right.clock_domain)
      || left.observed_at - right.observed_at || leftId.localeCompare(rightId));
}
