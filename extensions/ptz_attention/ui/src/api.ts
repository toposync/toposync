import type { HostApi } from "@toposync/plugin-api";

import type {
  AttentionCatalogResponse,
  AttentionCommand,
  AttentionDecisionsResponse,
  AttentionProfile,
  AttentionProfilesResponse,
  AttentionStatusResponse,
  AttentionValidationResponse,
} from "./types";

export class AttentionApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code = "") {
    super(message);
    this.name = "AttentionApiError";
    this.status = status;
    this.code = code;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

async function parseError(response: Response): Promise<AttentionApiError> {
  const fallback = `HTTP ${response.status}`;
  try {
    const payload = (await response.json()) as unknown;
    if (isRecord(payload)) {
      if (Array.isArray(payload.detail)) {
        const messages = payload.detail.flatMap((item) => {
          if (!isRecord(item)) return [];
          const location = Array.isArray(item.loc) ? item.loc.map(textValue).filter(Boolean).join(".") : "";
          const message = textValue(item.msg);
          return message ? [`${location ? `${location}: ` : ""}${message}`] : [];
        });
        if (messages.length) return new AttentionApiError(messages.join(" · "), response.status);
      }
      const detail = isRecord(payload.detail) ? payload.detail : payload;
      const code = textValue(detail.code ?? payload.code);
      const issueMessages = Array.isArray(detail.issues)
        ? detail.issues.flatMap((item) => {
            if (!isRecord(item)) return [];
            const message = textValue(item.message);
            const issueCode = textValue(item.code);
            return message ? [message] : issueCode ? [issueCode.replace(/_/g, " ")] : [];
          })
        : [];
      const message =
        textValue(detail.message) ||
        textValue(detail.detail) ||
        textValue(payload.detail) ||
        textValue(payload.message) ||
        issueMessages.slice(0, 3).join(" · ") ||
        code.replace(/_/g, " ") ||
        fallback;
      return new AttentionApiError(message, response.status, code);
    }
  } catch {
    // Fall through to the status-only error.
  }
  return new AttentionApiError(fallback, response.status);
}

async function requestJson<T>(api: HostApi, input: string, init?: RequestInit): Promise<T> {
  const response = await api.fetch(input, init);
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

export async function fetchAttentionCatalog(api: HostApi, signal?: AbortSignal): Promise<AttentionCatalogResponse> {
  const extensionCatalog = await requestJson<Partial<AttentionCatalogResponse>>(
    api,
    "/api/ptz-attention/catalog",
    { signal },
  );
  return {
    generated_at: Number(extensionCatalog.generated_at) || Date.now() / 1000,
    operator_id: textValue(extensionCatalog.operator_id) || "ptz_attention.request",
    modes: Array.isArray(extensionCatalog.modes) ? extensionCatalog.modes : [],
    states: Array.isArray(extensionCatalog.states) ? extensionCatalog.states : [],
    event_types: Array.isArray(extensionCatalog.event_types) ? extensionCatalog.event_types : [],
    cameras: Array.isArray(extensionCatalog.cameras) ? extensionCatalog.cameras : [],
    bindings: Array.isArray(extensionCatalog.bindings) ? extensionCatalog.bindings : [],
    permissions: isRecord(extensionCatalog.permissions)
      ? {
          configure: extensionCatalog.permissions.configure === true,
          control: extensionCatalog.permissions.control === true,
        }
      : { configure: false, control: false },
    services: isRecord(extensionCatalog.services) ? extensionCatalog.services as Record<string, boolean> : {},
    partial_errors: Array.isArray(extensionCatalog.partial_errors) ? extensionCatalog.partial_errors : [],
  };
}

export async function fetchAttentionProfiles(api: HostApi, signal?: AbortSignal): Promise<AttentionProfilesResponse> {
  return requestJson<AttentionProfilesResponse>(api, "/api/ptz-attention/profiles", { signal });
}

export async function createAttentionProfile(api: HostApi, profile: AttentionProfile): Promise<AttentionProfile> {
  return requestJson<AttentionProfile>(api, "/api/ptz-attention/profiles", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(profile),
  });
}

export async function updateAttentionProfile(api: HostApi, profile: AttentionProfile): Promise<AttentionProfile> {
  return requestJson<AttentionProfile>(api, `/api/ptz-attention/profiles/${encodeURIComponent(profile.id)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(profile),
  });
}

export async function deleteAttentionProfile(api: HostApi, profileId: string): Promise<void> {
  const response = await api.fetch(`/api/ptz-attention/profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw await parseError(response);
}

export async function validateAttentionProfile(
  api: HostApi,
  profile: AttentionProfile,
  signal?: AbortSignal,
): Promise<AttentionValidationResponse> {
  return requestJson<AttentionValidationResponse>(api, "/api/ptz-attention/profiles/validate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(profile),
    signal,
  });
}

export async function validateStoredAttentionProfile(
  api: HostApi,
  profileId: string,
  signal?: AbortSignal,
): Promise<AttentionValidationResponse> {
  return requestJson<AttentionValidationResponse>(
    api,
    `/api/ptz-attention/profiles/${encodeURIComponent(profileId)}/validate`,
    { method: "POST", signal },
  );
}

export async function fetchAttentionStatus(api: HostApi, signal?: AbortSignal): Promise<AttentionStatusResponse> {
  return requestJson<AttentionStatusResponse>(api, "/api/ptz-attention/status", { signal });
}

export async function fetchAttentionDecisions(
  api: HostApi,
  profileId: string,
  signal?: AbortSignal,
  before?: number,
): Promise<AttentionDecisionsResponse> {
  const params = new URLSearchParams({ profile_id: profileId, limit: "40" });
  if (Number.isFinite(before)) params.set("before", String(before));
  return requestJson<AttentionDecisionsResponse>(api, `/api/ptz-attention/decisions?${params.toString()}`, { signal });
}

export async function commandAttentionProfile(
  api: HostApi,
  profileId: string,
  command: AttentionCommand,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(api, `/api/ptz-attention/${command}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ profile_id: profileId }),
  });
}
