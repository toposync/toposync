import React, { useEffect, useState } from "react";
import { requestJson, resolveToposyncUrl } from "@toposync/plugin-api";
import { createCameraPipelinePreset } from "../api/camerasApi";
import { SubModal } from "../ui/SubModal";
import type { CameraPipelinePresetModal } from "./CameraPipelinePresetModal";

export function CameraObservationPresetModal({
  open, preset, camera, activeSourceId, mappedCompositions, processingServers, i18n, onClose, onCreated,
}: React.ComponentProps<typeof CameraPipelinePresetModal>): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const sources = camera.sources.filter((source) => source.enabled && source.kind === "video");
  const [sourceId, setSourceId] = useState("");
  const [name, setName] = useState("");
  const [areaId, setAreaId] = useState("");
  const [serverId, setServerId] = useState("local");
  const [signals, setSignals] = useState<{ topic: string; item_name: string; label: string }[]>([]);
  const [signalId, setSignalId] = useState("");
  const [loadingSignals, setLoadingSignals] = useState(false);
  const [signalsUnavailable, setSignalsUnavailable] = useState(false);
  const [saving, setSaving] = useState(false);
  const [reference, setReference] = useState<File | null>(null);
  const [retainAudio, setRetainAudio] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setSourceId(sources.find((source) => source.id === activeSourceId)?.id || sources[0]?.id || "");
    setName("");
    setAreaId("");
    setSignalId("");
    setServerId("local");
    setError(null);
    setReference(null);
    setRetainAudio(false);
  }, [open, preset, camera.id, activeSourceId]);
  useEffect(() => {
    if (!open || preset !== "doorbell_visitor") return;
    const controller = new AbortController();
    setSignals([]); setLoadingSignals(true); setSignalsUnavailable(false);
    requestJson<{ boolean_states: { topic: string; item_name: string; label: string; is_boolean: boolean; is_property: boolean }[] }>(
      `/api/cameras/cameras/${encodeURIComponent(camera.id)}/onvif/events`, { signal: controller.signal },
    ).then(value => {
      if (!controller.signal.aborted) setSignals((value.boolean_states ?? []).filter(item => item.is_boolean && item.is_property && item.topic && item.item_name));
    }).catch(() => { if (!controller.signal.aborted) setSignalsUnavailable(true); })
      .finally(() => { if (!controller.signal.aborted) setLoadingSignals(false); });
    return () => controller.abort();
  }, [open, preset, camera.id]);
  const visitor = preset === "doorbell_visitor";
  const audio = preset === "audio_anomaly";
  const selectedSignal = signals.find(item => JSON.stringify([item.topic, item.item_name]) === signalId);
  const area = visitor || preset === "area_presence" || preset === "area_entry" || preset === "area_group_entry";
  if (!open || (!area && !audio && preset !== "cat_litter_use" && preset !== "cat_drinking")) return null;
  const selectedArea = mappedCompositions.flatMap(composition => (composition.areas ?? []).map(item => ({...item, compositionId: composition.id}))).find(item => JSON.stringify([item.compositionId, item.id]) === areaId);
  const key = audio ? "ext.cameras.audioRecipe" : visitor ? "ext.cameras.visitor" : area ? `ext.cameras.area.${preset === "area_presence" ? "presence" : preset === "area_group_entry" ? "group" : "entry"}` : `ext.cameras.cat.${preset === "cat_litter_use" ? "litter" : "water"}`;
  async function save(): Promise<void> {
    if (saving || !sourceId || !preset || (area && !selectedArea) || (visitor && !selectedSignal) || (audio && !reference)) return;
    setSaving(true);
    setError(null);
    try {
      let audio_baseline: { reference: string; rms: number } | undefined;
      if (audio && reference) {
        if (reference.size > 12_000_000) throw new Error(t("ext.cameras.audioRecipe.invalid"));
        const response = await fetch(resolveToposyncUrl("/api/cameras/audio/analyze?started_at=0&clock_basis=synthetic"), {
          method: "POST", body: reference, headers: { "Content-Type": "audio/wav" },
        });
        if (!response.ok) throw new Error(t("ext.cameras.audioRecipe.invalid"));
        const result = await response.json() as { windows: { duration_seconds: number; rms: number }[] };
        const duration = result.windows.reduce((sum, window) => sum + window.duration_seconds, 0);
        const rms = Math.sqrt(result.windows.reduce((sum, window) => sum + window.rms ** 2 * window.duration_seconds, 0) / duration);
        if (!Number.isFinite(rms) || rms <= 0) throw new Error(t("ext.cameras.audioRecipe.invalid"));
        audio_baseline = { reference: reference.name.slice(0, 200), rms };
      }
      const result = await createCameraPipelinePreset(camera.id, {
        audio_baseline,
        retain_audio: audio && retainAudio,
        preset, source_id: sourceId, pipeline_name: name.trim(), enabled: false,
        event_topic: selectedSignal?.topic, event_item_name: selectedSignal?.item_name,
        processing_server_id: serverId, area_id: selectedArea?.id ?? "", composition_id: selectedArea?.compositionId ?? "",
      });
      onCreated(result.pipeline_name);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }
  return <SubModal open={open} title={t(`${key}.title`)} onClose={() => { if (!saving) onClose(); }}
    panelStyle={{ width: "min(540px, calc(100vw - 28px))" }}>
    <p>{t(`${key}.description`)}</p>
    <p>{t(audio ? "ext.cameras.audioRecipe.limits" : "ext.cameras.cat.preview")}</p>
    {audio && <label className="field"><span className="label">{t("ext.cameras.audioRecipe.reference")}</span>
      <input type="file" accept=".wav,audio/wav" disabled={saving} style={{ maxWidth: "100%" }}
        onChange={event => { setReference(event.target.files?.[0] ?? null); setError(null); }} />
      <span className="settingsDescription">{t("ext.cameras.audioRecipe.referenceHelp")}</span>
    </label>}
    {audio && <label className="field" style={{ display: "flex", alignItems: "start", gap: 8 }}>
      <input type="checkbox" checked={retainAudio} disabled={saving} onChange={event => setRetainAudio(event.target.checked)} />
      <span>{t("ext.cameras.audioHistory.optIn")}</span>
    </label>}
    {sources.length > 1 ? <div className="field">
      <label className="label" htmlFor="cat-recipe-source">{t("ext.cameras.cat.source")}</label>
      <select id="cat-recipe-source" className="input" value={sourceId} onChange={(event) => setSourceId(event.target.value)} disabled={saving}>
        {sources.map((source) => <option key={source.id} value={source.id}>{source.name || source.id}</option>)}
      </select>
    </div> : null}
    {area ? <label className="field"><span className="label">{t("ext.cameras.area.required")}</span>
      <select className="input" value={areaId} onChange={event => setAreaId(event.target.value)} disabled={saving}>
        <option value="">{t("ext.cameras.area.select")}</option>
        {mappedCompositions.map(composition => <optgroup key={composition.id} label={composition.name || composition.id}>
          {(composition.areas ?? []).filter(item => item.vertices_count >= 3).map(item => <option key={item.id} value={JSON.stringify([composition.id, item.id])}>{item.name || item.id}</option>)}
        </optgroup>)}
      </select>
    </label> : !audio ? <p className="settingsStatusMuted" style={{ whiteSpace: "normal", overflow: "visible" }}>{t("ext.cameras.cat.profile")}</p> : null}
    {visitor && <label className="field"><span className="label">{t("ext.cameras.visitor.signal")}</span>
      <select className="input" value={signalId} onChange={event => setSignalId(event.target.value)} disabled={saving || loadingSignals}>
        <option value="">{t("ext.cameras.visitor.select")}</option>
        {signals.map(item => <option key={JSON.stringify([item.topic, item.item_name])} value={JSON.stringify([item.topic, item.item_name])}>{item.label || item.item_name}</option>)}
      </select>
      {loadingSignals ? <span role="status">{t("ext.cameras.visitor.loading")}</span> : signalsUnavailable || !signals.length ? <span role="status">{t("ext.cameras.visitor.unavailable")}</span> : null}
    </label>}
    <details><summary>{t("ext.cameras.cat.advanced")}</summary>
      <label className="field"><span className="label">{t("ext.cameras.pipeline_preset.processing_server", {}, "Processing server")}</span>
        <select className="input" value={serverId} onChange={event => setServerId(event.target.value)} disabled={saving}>
          {processingServers.map(server => <option key={server.id} value={server.id}>{server.name || server.id}</option>)}
        </select>
      </label>
      <div className="field"><label className="label" htmlFor="cat-recipe-name">{t("ext.cameras.cat.name")}</label>
        <input id="cat-recipe-name" className="input" value={name} onChange={(event) => setName(event.target.value)} disabled={saving} />
      </div>
    </details>
    <p className="settingsStatusMuted" style={{ whiteSpace: "normal", overflow: "visible" }}>{t("ext.cameras.cat.paused")}</p>
    {error ? <p role="alert" className="errorText">{error}</p> : null}
    <button className="primaryButton" type="button" disabled={saving || !sourceId || (area && !selectedArea) || (visitor && !selectedSignal) || (audio && !reference)} onClick={() => void save()}>
      {t(saving ? "ext.cameras.cat.saving" : "ext.cameras.cat.save")}
    </button>
  </SubModal>;
}
