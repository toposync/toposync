import React, { useEffect, useMemo, useState } from "react";
import type { HostApi, PipelineOperatorPanel } from "@toposync/plugin-api";

import { fetchAttentionProfiles } from "../api";
import type { AttentionProfile, AttentionRequestOperatorConfig } from "../types";

type PanelArgs = Parameters<PipelineOperatorPanel["render"]>[0];

const DEFAULT_CONFIG: AttentionRequestOperatorConfig = {
  profile_id: "",
  event_type: "",
  event_type_field: "payload.event_type",
  event_id_field: "payload.event_id",
  world_envelope_field: "payload.world_envelope",
  world_anchor_field: "payload.world_anchor",
  bbox01_field: "payload.subject.bbox01",
  preferred_view_id_field: "payload.ptz_attention.preferred_view_id",
};

export function createPtzAttentionRequestOperatorPanel(api: HostApi): PipelineOperatorPanel {
  return {
    id: "com.toposync.ptz_attention.operator.request",
    operatorId: "ptz_attention.request",
    render: (args) => <PtzAttentionRequestPanel {...args} api={api} />,
  };
}

function PtzAttentionRequestPanel({
  api,
  i18n,
  config,
  showAdvanced,
  updateConfig,
}: PanelArgs & { api: HostApi }): React.ReactElement {
  const { t } = i18n.useI18n();
  const [profiles, setProfiles] = useState<AttentionProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const current = useMemo<AttentionRequestOperatorConfig>(
    () => ({
      ...DEFAULT_CONFIG,
      ...config,
    }) as AttentionRequestOperatorConfig,
    [config],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchAttentionProfiles(api, controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) {
          setProfiles(Array.isArray(payload.profiles) ? payload.profiles : []);
          setError(null);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [api]);

  const selectedProfile = profiles.find((profile) => profile.id === current.profile_id) ?? null;

  return (
    <div className="pipelinesOperatorConfigCard ptzAttentionOperator">
      <label className="pipelinesLabel">
        <span>{t("ext.ptz_attention.operator.profile", {}, "Attention profile")}</span>
        <select
          className="pipelinesSelect"
          value={current.profile_id}
          disabled={loading}
          onChange={(event) => updateConfig({ profile_id: event.target.value, event_type: "" })}
        >
          <option value="">{t("ext.ptz_attention.operator.profile.placeholder", {}, "Select a profile")}</option>
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>{profile.name}</option>
          ))}
        </select>
      </label>
      {!loading && profiles.length === 0 ? (
        <div className="pipelinesStepHint">{t("ext.ptz_attention.operator.profile.empty", {}, "No attention profile is configured.")}</div>
      ) : null}
      {selectedProfile ? (
        <div className="pipelinesStepHint">
          {selectedProfile.camera_id} · {t(`ext.ptz_attention.profile.mode.${selectedProfile.mode}`, {}, selectedProfile.mode)}
        </div>
      ) : null}
      {selectedProfile && !selectedProfile.event_policies.some((policy) => policy.enabled) ? (
        <div className="pipelinesInlineError">
          {t("ext.ptz_attention.operator.profile.no_events", {}, "This profile has no enabled event policy.")}
        </div>
      ) : null}
      {error ? <div className="pipelinesInlineError">{error}</div> : null}

      <label className="pipelinesLabel">
        <span>{t("ext.ptz_attention.operator.event_type", {}, "Semantic event type")}</span>
        <select
          className="pipelinesSelect"
          value={current.event_type}
          disabled={!selectedProfile}
          onChange={(event) => updateConfig({ event_type: event.target.value })}
        >
          <option value="">{t("ext.ptz_attention.operator.target.auto", {}, "Read from event")}</option>
          {(selectedProfile?.event_policies ?? []).filter((policy) => policy.enabled).map((policy) => (
            <option key={policy.event_type} value={policy.event_type}>{policy.event_type}</option>
          ))}
        </select>
      </label>

      <div className="pipelinesStepHint">
        {t(
          "ext.ptz_attention.operator.target.order",
          {},
          "Target resolution uses the world envelope, then the world anchor, then the image bounding box.",
        )}
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <PacketPathField
            label={t("ext.ptz_attention.operator.event_id_path", {}, "Event identifier path")}
            value={current.event_id_field}
            onChange={(event_id_field) => updateConfig({ event_id_field })}
          />
          <PacketPathField
            label={t("ext.ptz_attention.operator.event_type_path", {}, "Event type path")}
            value={current.event_type_field}
            onChange={(event_type_field) => updateConfig({ event_type_field })}
          />
          <PacketPathField
            label={t("ext.ptz_attention.operator.target.world_envelope", {}, "World envelope")}
            value={current.world_envelope_field}
            onChange={(world_envelope_field) => updateConfig({ world_envelope_field })}
          />
          <PacketPathField
            label={t("ext.ptz_attention.operator.target.world_anchor", {}, "World anchor")}
            value={current.world_anchor_field}
            onChange={(world_anchor_field) => updateConfig({ world_anchor_field })}
          />
          <PacketPathField
            label={t("ext.ptz_attention.operator.target.bbox01", {}, "Image bounding box")}
            value={current.bbox01_field}
            onChange={(bbox01_field) => updateConfig({ bbox01_field })}
          />
          <PacketPathField
            label={t("ext.ptz_attention.operator.preferred_view_path", {}, "Preferred calibrated view path")}
            value={current.preferred_view_id_field}
            onChange={(preferred_view_id_field) => updateConfig({ preferred_view_id_field })}
          />
        </>
      ) : null}
    </div>
  );
}

function PacketPathField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }): React.ReactElement {
  return (
    <label className="pipelinesLabel">
      <span>{label}</span>
      <input className="pipelinesInput" value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} />
    </label>
  );
}
