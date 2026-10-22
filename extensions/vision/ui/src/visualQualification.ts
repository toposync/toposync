export type VisualQualificationRecord = {
  operation_id: string; sensor_id: string; actor_id: string; created_at: number;
  context_revision: string; scope: "laboratory" | "replay" | "isolated_consumer"; decision: "passed" | "failed";
  report_sha256: string; protocol_sha256: string; environment_sha256: string;
};
export type VisualQualification = {
  context_revision: string | null; next_offset: number | null; physical_qualification: false; activation_authorized: false;
  operational_suspension_reasons?: string[];
  items: { record: VisualQualificationRecord; status: "recorded_pass" | "recorded_failure" | "suspended" | "superseded" }[];
};

export function validateVisualQualification(value: VisualQualification, sensorId: string): VisualQualification {
  const digest = (item: unknown) => typeof item === "string" && /^[a-f0-9]{64}$/.test(item);
  const identifier = (item: unknown) => typeof item === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(item);
  if (!value || value.physical_qualification !== false || value.activation_authorized !== false
    || !(value.context_revision === null || digest(value.context_revision))
    || !(value.next_offset === null || (Number.isSafeInteger(value.next_offset) && value.next_offset >= 0 && value.next_offset <= 1_000_000))
    || !Array.isArray(value.items) || value.items.length > 20
    || (value.operational_suspension_reasons !== undefined && (!Array.isArray(value.operational_suspension_reasons)
      || value.operational_suspension_reasons.length > 5 || value.operational_suspension_reasons.some(reason =>
        !["configuration_unverified", "cadence_incompatible", "execution_unavailable", "observation_unconfirmed", "observation_too_old"].includes(reason))
      || (value.context_revision === null && !value.operational_suspension_reasons.includes("configuration_unverified"))))
    || value.items.some(item => {
      const record = item?.record;
      return !record || record.sensor_id !== sensorId || !identifier(record.operation_id) || !identifier(record.actor_id)
        || !Number.isFinite(record.created_at) || record.created_at < 0 || record.created_at > 253402300799
        || !["laboratory", "replay", "isolated_consumer"].includes(record.scope) || !["passed", "failed"].includes(record.decision)
        || !["recorded_pass", "recorded_failure", "suspended", "superseded"].includes(item.status)
        || ![record.context_revision, record.report_sha256, record.protocol_sha256, record.environment_sha256].every(digest)
        || (item.status.startsWith("recorded_") && (record.context_revision !== value.context_revision
          || record.decision !== (item.status === "recorded_pass" ? "passed" : "failed")));
    })) throw new Error("qualification_invalid");
  return value;
}
