import React, { useEffect, useMemo, useState } from "react";
import type { HostApi, PipelineOperatorPanel } from "@toposync/plugin-api";

import { fetchAttentionCatalog } from "../api";
import type {
  AttentionCameraCatalogItem,
  AttentionCatalogResponse,
  AttentionRequestOperatorConfig,
  AttentionViewCatalogItem,
} from "../types";

type PanelArgs = Parameters<PipelineOperatorPanel["render"]>[0];

const DEFAULT_CONFIG: AttentionRequestOperatorConfig = {
  profile_id: "",
  camera_id: "",
  source_id: "",
  composition_id: "",
  priority: 0,
  hold_after_close_seconds: 8,
  native_tracking_disabled_confirmed: false,
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
  const [catalog, setCatalog] = useState<AttentionCatalogResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const current = useMemo<AttentionRequestOperatorConfig>(
    () => ({ ...DEFAULT_CONFIG, ...config }) as AttentionRequestOperatorConfig,
    [config],
  );

  useEffect(() => {
    const controller = new AbortController();
    fetchAttentionCatalog(api, controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) {
          setCatalog(payload);
          setError(null);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [api]);

  const selectedCamera = catalog?.cameras.find((camera) => camera.id === current.camera_id) ?? null;
  const sources = (selectedCamera?.sources ?? []).filter((source) => source.enabled && source.kind === "video" && source.has_ptz);
  const selectedSource = sources.find((source) => source.id === current.source_id) ?? null;
  const compositions = Array.from(
    new Map(
      (selectedCamera?.views ?? [])
        .filter((view) => view.pose_bound && view.quality === "ready" && viewSupportsSource(view, selectedCamera, selectedSource?.id ?? ""))
        .map((view) => [view.composition_id, view]),
    ).values(),
  );
  const hasMappedComposition = Boolean(current.composition_id && compositions.some((view) => view.composition_id === current.composition_id));
  const blocked = !selectedCamera || !current.source_id || !hasMappedComposition || !current.native_tracking_disabled_confirmed;

  return (
    <div className="pipelinesOperatorConfigCard ptzAttentionOperator">
      <div className="pipelinesStepHint">
        {t("ext.ptz_attention.operator.summary", {}, "Moves one calibrated PTZ head to an active mapped event. A higher numeric priority may preempt a lower one; equal priority never interrupts focus.")}
      </div>
      <label className="pipelinesLabel">
        <span>{t("ext.ptz_attention.operator.camera", {}, "PTZ camera")}</span>
        <select
          className="pipelinesSelect"
          value={current.camera_id}
          onChange={(event) => updateConfig({ camera_id: event.target.value, source_id: "", composition_id: "" })}
        >
          <option value="">{t("ext.ptz_attention.operator.camera.placeholder", {}, "Select a PTZ camera")}</option>
          {(catalog?.cameras ?? []).filter((camera) => camera.enabled && Boolean(camera.actuator_id)).map((camera) => (
            <option key={camera.id} value={camera.id}>{camera.name || camera.id}</option>
          ))}
        </select>
      </label>
      <label className="pipelinesLabel">
        <span>{t("ext.ptz_attention.operator.source", {}, "PTZ control source")}</span>
        <select
          className="pipelinesSelect"
          value={current.source_id}
          disabled={!selectedCamera}
          onChange={(event) => updateConfig({ source_id: event.target.value, composition_id: "" })}
        >
          <option value="">{t("ext.ptz_attention.operator.source.placeholder", {}, "Select a compatible source")}</option>
          {sources.map((source) => <option key={source.id} value={source.id}>{source.name || source.id}</option>)}
        </select>
      </label>
      <label className="pipelinesLabel">
        <span>{t("ext.ptz_attention.operator.composition", {}, "Calibrated composition")}</span>
        <select
          className="pipelinesSelect"
          value={current.composition_id}
          disabled={!current.source_id}
          onChange={(event) => updateConfig({ composition_id: event.target.value })}
        >
          <option value="">{t("ext.ptz_attention.operator.composition.placeholder", {}, "Select a mapped composition")}</option>
          {compositions.map((view) => <option key={view.composition_id} value={view.composition_id}>{view.composition_name || view.composition_id}</option>)}
        </select>
      </label>
      <div className="pipelinesStepHint">
        {t("ext.ptz_attention.operator.home", {}, "After the event, the camera returns to the first calibrated view in this composition.")}
      </div>
      <div className="pipelinesFieldGrid">
        <label className="pipelinesLabel">
          <span>{t("ext.ptz_attention.operator.priority", {}, "Priority")}</span>
          <input className="pipelinesInput" type="number" value={current.priority} onChange={(event) => updateConfig({ priority: Number(event.target.value) || 0 })} />
        </label>
        <label className="pipelinesLabel">
          <span>{t("ext.ptz_attention.operator.hold", {}, "Hold after event (seconds)")}</span>
          <input className="pipelinesInput" type="number" min="0" value={current.hold_after_close_seconds} onChange={(event) => updateConfig({ hold_after_close_seconds: Math.max(0, Number(event.target.value) || 0) })} />
        </label>
      </div>
      <label className="pipelinesCheckbox">
        <input
          type="checkbox"
          checked={current.native_tracking_disabled_confirmed}
          onChange={(event) => updateConfig({ native_tracking_disabled_confirmed: event.target.checked })}
        />
        <span>{t("ext.ptz_attention.operator.native_tracking_ack", {}, "I confirm native auto-tracking, monitor point, and automatic return are disabled for this camera.")}</span>
      </label>
      {blocked ? <div className="pipelinesInlineError">{t("ext.ptz_attention.operator.blocked", {}, "PTZ movement remains blocked until the camera, calibrated composition, and confirmation are complete.")}</div> : null}
      {error ? <div className="pipelinesInlineError">{error}</div> : null}
      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <PacketPathField label={t("ext.ptz_attention.operator.event_id_path", {}, "Event identifier path")} value={current.event_id_field} onChange={(event_id_field) => updateConfig({ event_id_field })} />
          <PacketPathField label={t("ext.ptz_attention.operator.event_type_path", {}, "Event type path")} value={current.event_type_field} onChange={(event_type_field) => updateConfig({ event_type_field })} />
          <PacketPathField label={t("ext.ptz_attention.operator.target.world_envelope", {}, "World envelope")} value={current.world_envelope_field} onChange={(world_envelope_field) => updateConfig({ world_envelope_field })} />
          <PacketPathField label={t("ext.ptz_attention.operator.target.world_anchor", {}, "World anchor")} value={current.world_anchor_field} onChange={(world_anchor_field) => updateConfig({ world_anchor_field })} />
        </>
      ) : null}
    </div>
  );
}

function viewSupportsSource(
  view: AttentionViewCatalogItem,
  camera: AttentionCameraCatalogItem | null,
  sourceId: string,
): boolean {
  if (!sourceId) return true;
  const source = camera?.sources.find((item) => item.id === sourceId) ?? null;
  if (!source || !source.enabled || source.kind !== "video" || !source.has_ptz) return false;
  if (view.physical_view_id && view.physical_view_id !== source.view_id) return false;
  if (view.compatible_source_ids.length > 0 && !view.compatible_source_ids.includes(source.id)) return false;
  if (view.compatible_roles.length > 0 && !view.compatible_roles.includes(source.role)) return false;
  return true;
}

function PacketPathField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }): React.ReactElement {
  return <label className="pipelinesLabel"><span>{label}</span><input className="pipelinesInput" value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} /></label>;
}
