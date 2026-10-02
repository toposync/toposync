import React, { useEffect, useState } from "react";
import { requestJson, resolveToposyncUrl, type HostUi } from "@toposync/plugin-api";
import type { CameraConfig, CameraPipelinePresetRequest } from "../types";
import { createCameraPipelinePreset } from "../api/camerasApi";
import { SubModal } from "../ui/SubModal";
import type { CameraPipelinePresetModal } from "./CameraPipelinePresetModal";

export function CameraObservationPresetModal({
  open, preset, camera, cameras = [], activeSourceId, mappedCompositions, processingServers, i18n, onClose, onCreated, ui,
}: React.ComponentProps<typeof CameraPipelinePresetModal> & { ui?: HostUi; cameras?: CameraConfig[] }): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const sources = camera.sources.filter((source) => source.enabled && source.kind === "video");
  const [sourceId, setSourceId] = useState("");
  const regionContext = JSON.stringify([camera.control, sources.find(source => source.id === sourceId)]);
  const [region, setRegion] = useState<CameraPipelinePresetRequest["observation_region"]>();
  const [regionEditorOpen, setRegionEditorOpen] = useState(false);
  const [draftRegion, setDraftRegion] = useState({ left: 0, top: 0, right: 100, bottom: 100 });
  useEffect(() => { setRegion(undefined); setRegionEditorOpen(false); }, [open, preset, camera.id, sourceId, regionContext]);
  const [name, setName] = useState("");
  const [areaId, setAreaId] = useState("");
  const [categories, setCategories] = useState<Array<"person" | "cat" | "dog">>(["person", "cat", "dog"]);
  const [serverId, setServerId] = useState("local");
  const [signals, setSignals] = useState<{ topic: string; item_name: string; label: string }[]>([]);
  const [signalId, setSignalId] = useState("");
  const [loadingSignals, setLoadingSignals] = useState(false);
  const [signalsUnavailable, setSignalsUnavailable] = useState(false);
  const [saving, setSaving] = useState(false);
  const [reference, setReference] = useState<File | null>(null);
  const [peerId, setPeerId] = useState("");
  const [peerReference, setPeerReference] = useState<File | null>(null);
  const peers = cameras.filter(item => item.id !== camera.id).flatMap(item => item.sources
    .filter(source => source.enabled && source.kind === "video")
    .map(source => ({ id: JSON.stringify([item.id, source.id]), cameraId: item.id, sourceId: source.id,
      label: `${item.name || item.id} · ${source.name || source.id}` })));
  const selectedPeer = peers.find(item => item.id === peerId);
  const [retainAudio, setRetainAudio] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setSourceId(sources.find((source) => source.id === activeSourceId)?.id || sources[0]?.id || "");
    setName("");
    setAreaId("");
    setCategories(["person", "cat", "dog"]);
    setSignalId("");
    setServerId("local");
    setError(null);
    setReference(null);
    setRetainAudio(false); setPeerId(""); setPeerReference(null);
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
  const selectableCategories = preset === "area_presence" || preset === "area_entry";
  const visitor = preset === "doorbell_visitor";
  const audio = preset === "audio_anomaly";
  const cat = preset === "cat_litter_use" || preset === "cat_drinking";
  const RegionEditor = ui?.CameraRegionEditor;
  const regionAvailable = Boolean(RegionEditor) && sources.find(source => source.id === sourceId)?.origin?.has_ptz !== true;
  const regionReady = Boolean(regionAvailable && region && region.snapshot_binding.camera_id === camera.id && region.snapshot_binding.source_id === sourceId);
  const selectedSignal = signals.find(item => JSON.stringify([item.topic, item.item_name]) === signalId);
  const area = visitor || preset === "area_presence" || preset === "area_entry" || preset === "area_group_entry";
  if (!open || (!area && !audio && preset !== "cat_litter_use" && preset !== "cat_drinking")) return null;
  const selectedArea = mappedCompositions.flatMap(composition => (composition.areas ?? []).map(item => ({...item, compositionId: composition.id}))).find(item => JSON.stringify([item.compositionId, item.id]) === areaId);
  const key = audio ? "ext.cameras.audioRecipe" : visitor ? "ext.cameras.visitor" : area ? `ext.cameras.area.${preset === "area_presence" ? "presence" : preset === "area_group_entry" ? "group" : "entry"}` : `ext.cameras.cat.${preset === "cat_litter_use" ? "litter" : "water"}`;
  async function save(): Promise<void> {
    if (saving || !sourceId || !preset || (area && !selectedArea) || (visitor && !selectedSignal) || (audio && (!reference || (peerId && (!selectedPeer || !peerReference)))) || (selectableCategories && categories.length === 0) || (cat && !regionReady)) return;
    setSaving(true);
    setError(null);
    try {
      async function analyzeReference(file: File) {
        if (file.size > 12_000_000) throw new Error(t("ext.cameras.audioRecipe.invalid"));
        const response = await fetch(resolveToposyncUrl("/api/cameras/audio/analyze?started_at=0&clock_basis=synthetic"), {
          method: "POST", body: file, headers: { "Content-Type": "audio/wav" },
        });
        if (!response.ok) throw new Error(t("ext.cameras.audioRecipe.invalid"));
        const result = await response.json() as { windows: { duration_seconds: number; rms: number }[] };
        const duration = result.windows.reduce((sum, window) => sum + window.duration_seconds, 0);
        const rms = Math.sqrt(result.windows.reduce((sum, window) => sum + window.rms ** 2 * window.duration_seconds, 0) / duration);
        if (!Number.isFinite(rms) || rms <= 0) throw new Error(t("ext.cameras.audioRecipe.invalid"));
        return { reference: file.name.slice(0, 200), rms };
      }
      const audio_baseline = audio && reference ? await analyzeReference(reference) : undefined;
      const audio_peer = audio && selectedPeer && peerReference ? {
        camera_id: selectedPeer.cameraId, source_id: selectedPeer.sourceId,
        baseline: await analyzeReference(peerReference),
      } : undefined;
      const result = await createCameraPipelinePreset(camera.id, {
        audio_baseline, audio_peer,
        observation_region: cat ? region : undefined,
        retain_audio: audio && retainAudio,
        area_categories: selectableCategories ? categories : undefined,
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
  return <><SubModal open={open && !regionEditorOpen} title={t(`${key}.title`)} onClose={() => { if (!saving) onClose(); }}
    panelStyle={{ width: "min(540px, calc(100vw - 28px))" }}>
    <p>{t(`${key}.description`)}</p>
    <p>{t(audio ? "ext.cameras.audioRecipe.limits" : "ext.cameras.cat.preview")}</p>
    {sources.length > 1 ? <div className="field">
      <label className="label" htmlFor="cat-recipe-source">{t("ext.cameras.cat.source")}</label>
      <select id="cat-recipe-source" className="input" value={sourceId} onChange={(event) => { setSourceId(event.target.value); setReference(null); }} disabled={saving}>
        {sources.map((source) => <option key={source.id} value={source.id}>{source.name || source.id}</option>)}
      </select>
    </div> : audio && sources.length === 1 ? <p><strong>{t("ext.cameras.cat.source")}: </strong>{camera.name || camera.id} · {sources[0].name || sources[0].id}</p> : null}
    {audio && <label className="field"><span className="label">{t("ext.cameras.audioRecipe.reference")}</span>
      <input key={sourceId} type="file" accept=".wav,audio/wav" disabled={saving} style={{ maxWidth: "100%" }}
        onChange={event => { setReference(event.target.files?.[0] ?? null); setError(null); }} />
      <span className="settingsDescription">{t("ext.cameras.audioRecipe.referenceHelp")}</span>
    </label>}
    {audio && peers.length > 0 && <details style={{ marginBottom: 16 }}>
      <summary>{t("ext.cameras.audioRecipe.compare")}</summary>
      <label className="field"><span className="label">{t("ext.cameras.audioRecipe.peer")}</span>
        <select className="input" aria-label={t("ext.cameras.audioRecipe.peer")} value={peerId} disabled={saving}
          onChange={event => { setPeerId(event.target.value); setPeerReference(null); }}>
          <option value="">{t("ext.cameras.audioRecipe.single")}</option>
          {peers.map(peer => <option key={peer.id} value={peer.id}>{peer.label}</option>)}
        </select>
      </label>
      {peerId && <>
        <label className="field"><span className="label">{t("ext.cameras.audioRecipe.peerReference")}</span>
          <input key={peerId} type="file" accept=".wav,audio/wav" disabled={saving} style={{ maxWidth: "100%" }}
            onChange={event => { setPeerReference(event.target.files?.[0] ?? null); setError(null); }} />
        </label>
        <p className="settingsDescription">{t("ext.cameras.audioRecipe.clockLimit")}</p>
      </>}
    </details>}
    {audio && <label className="field" style={{ display: "flex", alignItems: "start", gap: 8 }}>
      <input type="checkbox" checked={retainAudio} disabled={saving} onChange={event => setRetainAudio(event.target.checked)} />
      <span>{t("ext.cameras.audioHistory.optIn")}</span>
    </label>}
    {cat && <div className="field">
      <button className="chipButton" type="button" disabled={saving || !sourceId || !regionAvailable}
        onClick={() => { setDraftRegion(region ?? { left: 0, top: 0, right: 100, bottom: 100 }); setRegionEditorOpen(true); }}>
        {t(regionReady ? "ext.cameras.cat.regionChange" : "ext.cameras.cat.regionSelect")}
      </button>
      <p role="status" className="settingsDescription">{t(!regionAvailable ? "ext.cameras.cat.regionUnavailable" : regionReady ? "ext.cameras.cat.regionSelected" : "ext.cameras.cat.regionRequired")}</p>
      <p className="settingsDescription">{t("ext.cameras.cat.regionFixed")}</p>
    </div>}
    {area ? <label className="field"><span className="label">{t("ext.cameras.area.required")}</span>
      <select className="input" value={areaId} onChange={event => setAreaId(event.target.value)} disabled={saving}>
        <option value="">{t("ext.cameras.area.select")}</option>
        {mappedCompositions.map(composition => <optgroup key={composition.id} label={composition.name || composition.id}>
          {(composition.areas ?? []).filter(item => item.vertices_count >= 3).map(item => <option key={item.id} value={JSON.stringify([composition.id, item.id])}>{item.name || item.id}</option>)}
        </optgroup>)}
      </select>
    </label> : !audio ? <p className="settingsStatusMuted" style={{ whiteSpace: "normal", overflow: "visible" }}>{t("ext.cameras.cat.profile")}</p> : null}
    {selectableCategories && <fieldset disabled={saving} style={{ margin: "0 0 16px", minWidth: 0 }}>
      <legend>{t("ext.cameras.area.categories")}</legend>
      {(["person", "cat", "dog"] as const).map(category => <label key={category} style={{ display: "inline-flex", alignItems: "center", gap: 8, minHeight: 44, marginRight: 16 }}>
        <input type="checkbox" checked={categories.includes(category)} onChange={event => setCategories(previous => event.target.checked ? [...previous, category] : previous.filter(value => value !== category))} />
        {t(`ext.cameras.area.category.${category}`)}
      </label>)}
      <p className="settingsDescription">{t("ext.cameras.area.categoriesHelp")}</p>
      {categories.length === 0 && <p role="alert">{t("ext.cameras.area.categoriesRequired")}</p>}
    </fieldset>}
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
    <button className="primaryButton" type="button" disabled={saving || !sourceId || (area && !selectedArea) || (visitor && !selectedSignal) || (audio && (!reference || (peerId && (!selectedPeer || !peerReference)))) || (selectableCategories && categories.length === 0) || (cat && !regionReady)} onClick={() => void save()}>
      {t(saving ? "ext.cameras.cat.saving" : "ext.cameras.cat.save")}
    </button>
  </SubModal>
    {cat && regionEditorOpen && RegionEditor && <RegionEditor open cameraId={camera.id} sourceId={sourceId}
      values={draftRegion} onChange={setDraftRegion} onClose={() => setRegionEditorOpen(false)}
      onConfirm={(values, binding) => {
        if (binding.camera_id !== camera.id || binding.source_id !== sourceId || values.left >= values.right || values.top >= values.bottom) return;
        setRegion({ ...values, snapshot_binding: binding }); setRegionEditorOpen(false);
      }} />}
  </>;
}
